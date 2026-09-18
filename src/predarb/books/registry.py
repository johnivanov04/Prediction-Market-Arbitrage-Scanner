"""Runtime subscription state: connection epochs, sids, and their markets.

Shape of the state
------------------
::

    connection epoch
        sid
            channel
            subscribed market tickers
            sequence tracker

A book belongs to a market, but its *authority* derives from the
``(epoch, sid)`` it was established under. That is why the pairing is tracked
here rather than inferred: after a reconnect the same market is served by a new
epoch, and a book carrying its old levels has no standing in the new one.

Why "one subscribe command == one sid" is not assumed
-----------------------------------------------------
Observed live: two ``subscribe`` commands for the **same channel** are merged by
the server into one existing sid, while different channels get different sids
(A-09). Membership is therefore recorded from what the server confirms --
``subscribed`` acknowledgements and ``ok`` responses -- rather than from what we
asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from predarb.books.sequence import SequenceTracker

__all__ = ["ConnectionEpoch", "SubscriptionRegistry", "SubscriptionState"]


@dataclass(slots=True)
class SubscriptionState:
    """One server-assigned subscription within a connection."""

    sid: int
    channel: str
    markets: set[str] = field(default_factory=set)
    last_activity_at: datetime | None = None
    tracker: SequenceTracker = field(init=False)
    """When this sid last carried a sequenced frame.

    Distinct from a *book's* last change: a sid can be busy while one of its
    markets sits unchanged. Neither is a connection-liveness signal."""

    def __post_init__(self) -> None:
        self.tracker = SequenceTracker(self.sid)

    @property
    def sequence(self) -> SequenceTracker:
        return self.tracker

    @property
    def is_orderbook(self) -> bool:
        """Whether this subscription carries order-book state.

        Only these sids can invalidate books, and only these need the
        fail-closed treatment of unknown sequenced frames.
        """
        return self.channel == "orderbook_delta"


@dataclass(slots=True)
class ConnectionEpoch:
    """One WebSocket connection's lifetime.

    The epoch number is local and monotonically increasing. It exists so that
    sequence authority can never be carried across connections: a frame from
    epoch 3 has nothing to say about a book established in epoch 2, and pairing
    every book with its epoch makes that structural rather than remembered.
    """

    epoch: int
    opened_at: datetime
    subscriptions: dict[int, SubscriptionState] = field(default_factory=dict)
    closed_at: datetime | None = None
    last_frame_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def subscription_for(self, sid: int) -> SubscriptionState | None:
        return self.subscriptions.get(sid)

    def markets(self) -> set[str]:
        return {market for state in self.subscriptions.values() for market in state.markets}

    def orderbook_sids(self) -> list[int]:
        return [sid for sid, state in self.subscriptions.items() if state.is_orderbook]


class SubscriptionRegistry:
    """Tracks connection epochs and their subscriptions.

    One registry per collector process. Epochs accumulate so a closed one can
    still be inspected during an investigation, but only the current epoch can
    confer authority on a book.
    """

    __slots__ = ("_current_epoch", "_epochs", "_next_epoch")

    def __init__(self) -> None:
        self._epochs: dict[int, ConnectionEpoch] = {}
        self._next_epoch = 1
        self._current_epoch: int | None = None

    @property
    def current_epoch(self) -> ConnectionEpoch | None:
        if self._current_epoch is None:
            return None
        return self._epochs.get(self._current_epoch)

    @property
    def current_epoch_number(self) -> int | None:
        return self._current_epoch

    def open_epoch(self, opened_at: datetime) -> ConnectionEpoch:
        """Begin a new connection epoch, closing any previous one.

        Closing the old epoch here rather than leaving it open is deliberate:
        there is exactly one live connection, and an epoch that stays "open"
        after being replaced is a state that can wrongly look authoritative.
        """
        if self._current_epoch is not None:
            self.close_epoch(opened_at)
        epoch = ConnectionEpoch(epoch=self._next_epoch, opened_at=opened_at)
        self._epochs[epoch.epoch] = epoch
        self._current_epoch = epoch.epoch
        self._next_epoch += 1
        return epoch

    def close_epoch(self, closed_at: datetime) -> ConnectionEpoch | None:
        """Mark the current epoch closed. Its sequence state becomes meaningless."""
        epoch = self.current_epoch
        if epoch is None:
            return None
        epoch.closed_at = closed_at
        self._current_epoch = None
        return epoch

    def register_subscription(
        self, *, sid: int, channel: str, markets: set[str] | None = None
    ) -> SubscriptionState:
        """Record a server-confirmed subscription.

        If the sid already exists, markets are **merged** rather than replaced:
        the server merges same-channel subscriptions into one sid, so a second
        acknowledgement for a known sid is an addition, not a redefinition.
        """
        epoch = self.current_epoch
        if epoch is None:
            raise RuntimeError("no open connection epoch; call open_epoch first")
        existing = epoch.subscriptions.get(sid)
        if existing is not None:
            if markets:
                existing.markets |= markets
            return existing
        state = SubscriptionState(sid=sid, channel=channel, markets=set(markets or ()))
        epoch.subscriptions[sid] = state
        return state

    def set_markets(self, sid: int, markets: set[str]) -> SubscriptionState | None:
        """Replace a sid's market set from a server-confirmed membership list.

        The ``ok`` response to ``update_subscription`` reports the resulting set
        outright, which is more trustworthy than tracking our own add/remove
        intentions.
        """
        epoch = self.current_epoch
        if epoch is None:
            return None
        state = epoch.subscriptions.get(sid)
        if state is None:
            return None
        state.markets = set(markets)
        return state

    def markets_for_sid(self, sid: int) -> set[str]:
        epoch = self.current_epoch
        if epoch is None:
            return set()
        state = epoch.subscriptions.get(sid)
        return set(state.markets) if state else set()

    def sid_for_market(self, market_ticker: str, *, channel: str = "orderbook_delta") -> int | None:
        epoch = self.current_epoch
        if epoch is None:
            return None
        for sid, state in epoch.subscriptions.items():
            if state.channel == channel and market_ticker in state.markets:
                return sid
        return None

    def epoch(self, number: int) -> ConnectionEpoch | None:
        return self._epochs.get(number)

    def all_epochs(self) -> list[ConnectionEpoch]:
        return [self._epochs[k] for k in sorted(self._epochs)]

    def __repr__(self) -> str:
        return (
            f"SubscriptionRegistry(current_epoch={self._current_epoch}, epochs={len(self._epochs)})"
        )
