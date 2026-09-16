"""Tests for WebSocket sequence-scope and density analysis."""

from __future__ import annotations

import pytest

from predarb.venues.kalshi.sequence_analysis import (
    SequenceRecord,
    SequenceScope,
    analyse_all_scopes,
    analyse_scope,
    rank_scopes,
    records_from_observations,
)

pytestmark = pytest.mark.unit


def rec(
    *,
    session: int = 1,
    index: int = 0,
    sid: int | None = 1,
    seq: int | None = 1,
    ticker: str = "A",
    kind: str = "orderbook_delta",
) -> SequenceRecord:
    return SequenceRecord(
        session=session,
        arrival_index=index,
        sid=sid,
        seq=seq,
        market_ticker=ticker,
        message_type=kind,
    )


def stream(
    seqs: list[int], *, sid: int = 1, ticker: str = "A", session: int = 1, start: int = 0
) -> list[SequenceRecord]:
    """Build a stream. ``start`` sets arrival index, so two streams can be
    given realistic interleaving rather than both beginning at index 0."""
    return [
        rec(session=session, index=start + i, sid=sid, seq=s, ticker=ticker)
        for i, s in enumerate(seqs)
    ]


class TestDensityStatistics:
    def test_perfectly_dense_stream(self):
        analysis = analyse_scope(stream([1, 2, 3, 4, 5]), SequenceScope.SID)
        stats = next(iter(analysis.streams.values()))
        assert stats.adjacent_pairs == 4
        assert stats.increments_of_one == 4
        assert stats.positive_skips == 0
        assert stats.density == 1.0
        assert stats.is_perfectly_dense

    def test_sparse_stream_is_not_dense(self):
        # 1,2,4,7 -- could be loss, could be a non-dense counter. The analysis
        # reports the shape and draws no conclusion.
        analysis = analyse_scope(stream([1, 2, 4, 7]), SequenceScope.SID)
        stats = next(iter(analysis.streams.values()))
        assert stats.adjacent_pairs == 3
        assert stats.increments_of_one == 1
        assert stats.positive_skips == 2
        assert stats.density == pytest.approx(1 / 3)
        assert not stats.is_perfectly_dense
        assert stats.is_strictly_increasing

    def test_duplicates_counted(self):
        stats = next(iter(analyse_scope(stream([1, 2, 2, 3]), SequenceScope.SID).streams.values()))
        assert stats.duplicates == 1
        assert stats.positive_skips == 0

    def test_decreases_counted(self):
        stats = next(iter(analyse_scope(stream([1, 2, 5, 3]), SequenceScope.SID).streams.values()))
        assert stats.decreases == 1
        assert not stats.is_strictly_increasing

    def test_single_value_has_no_pairs(self):
        stats = next(iter(analyse_scope(stream([7]), SequenceScope.SID).streams.values()))
        assert stats.adjacent_pairs == 0
        assert stats.density is None
        assert not stats.is_perfectly_dense

    def test_empty_input(self):
        analysis = analyse_scope([], SequenceScope.SID)
        assert analysis.streams == {}
        assert analysis.overall_density is None


class TestGrouping:
    def test_sid_scope_separates_subscriptions(self):
        records = stream([1, 2, 3], sid=1) + stream([1, 2], sid=2, start=3)
        analysis = analyse_scope(records, SequenceScope.SID)
        assert len(analysis.streams) == 2
        assert analysis.all_streams_dense

    def test_connection_scope_merges_subscriptions(self):
        """Merging two independent per-sid streams looks broken -- correctly.

        This is the shape that distinguishes per-sid numbering from a
        connection-global counter: a second sid restarting at 1 registers as a
        decrease once the streams are merged.
        """
        # The second sid opens after the first has been running, exactly as
        # observed live: sid=2's first frame arrives while sid=1 is mid-stream.
        records = stream([1, 2, 3], sid=1) + stream([1, 2], sid=2, start=3)
        analysis = analyse_scope(records, SequenceScope.CONNECTION)
        assert len(analysis.streams) == 1
        assert analysis.total_decreases >= 1
        assert not analysis.all_streams_dense

    def test_sid_market_scope_splits_by_market(self):
        records = [
            rec(index=0, sid=1, seq=1, ticker="A"),
            rec(index=1, sid=1, seq=2, ticker="B"),
            rec(index=2, sid=1, seq=3, ticker="A"),
        ]
        analysis = analyse_scope(records, SequenceScope.SID_MARKET)
        assert len(analysis.streams) == 2
        # Per-market the sequence skips, because another market took seq 2.
        a_stream = analysis.streams[(1, 1, "A")]
        assert a_stream.values == [1, 3]
        assert a_stream.positive_skips == 1

    def test_market_scope_ignores_sid(self):
        records = stream([1, 2], sid=1, ticker="A") + stream([3, 4], sid=2, ticker="A", start=2)
        analysis = analyse_scope(records, SequenceScope.MARKET)
        assert len(analysis.streams) == 1

    def test_sessions_are_never_merged(self):
        # Reconnect evidence must not be folded into the pre-disconnect stream.
        records = stream([1, 2, 3], session=1) + stream([1, 2, 3], session=2, start=3)
        analysis = analyse_scope(records, SequenceScope.SID)
        assert len(analysis.streams) == 2
        assert analysis.all_streams_dense

    def test_records_are_ordered_by_arrival_not_value(self):
        records = [rec(index=0, seq=5), rec(index=1, seq=3)]
        stats = next(iter(analyse_scope(records, SequenceScope.SID).streams.values()))
        assert stats.values == [5, 3]
        assert stats.decreases == 1


class TestExclusions:
    def test_records_without_seq_are_excluded(self):
        # Real ticker frames carry no seq at all.
        analysis = analyse_scope([rec(seq=None)], SequenceScope.SID)
        assert analysis.excluded_frames == 1
        assert analysis.streams == {}

    def test_records_without_sid_excluded_from_sid_scope(self):
        analysis = analyse_scope([rec(sid=None)], SequenceScope.SID)
        assert analysis.excluded_frames == 1

    def test_records_without_ticker_excluded_from_market_scope(self):
        analysis = analyse_scope([rec(ticker="")], SequenceScope.MARKET)
        assert analysis.excluded_frames == 1

    def test_records_without_ticker_still_counted_under_sid(self):
        # An `ok` frame has a sid and seq but no market; it still consumes a
        # sequence number, so it belongs in the sid stream.
        analysis = analyse_scope([rec(ticker="", kind="ok")], SequenceScope.SID)
        assert analysis.excluded_frames == 0
        assert len(analysis.streams) == 1


class TestRanking:
    def test_sid_outranks_connection_when_two_sids_exist(self):
        records = stream([1, 2, 3, 4], sid=1) + stream([1, 2, 3], sid=2, start=4)
        ranked = rank_scopes(analyse_all_scopes(records))
        assert ranked[0][0] is SequenceScope.SID
        assert ranked[0][1] == 1.0

    def test_scope_with_no_pairs_scores_below_any_real_scope(self):
        ranked = dict(rank_scopes(analyse_all_scopes([rec(ticker="")])))
        assert ranked[SequenceScope.MARKET] == -1.0

    def test_all_scopes_are_reported(self):
        analyses = analyse_all_scopes(stream([1, 2, 3]))
        assert set(analyses) == set(SequenceScope)


class TestSummaries:
    def test_stream_summary_shape(self):
        summary = next(
            iter(analyse_scope(stream([1, 2, 3]), SequenceScope.SID).streams.values())
        ).summary()
        for key in ("count", "first", "last", "density", "perfectly_dense", "message_types"):
            assert key in summary

    def test_scope_summary_shape(self):
        summary = analyse_scope(stream([1, 2, 3]), SequenceScope.SID).summary()
        assert summary["scope"] == "SID"
        assert summary["total_pairs"] == 2
        assert summary["overall_density"] == 1.0

    def test_records_from_observations(self):
        records = records_from_observations([(1, 0, 2, 5, "A", "orderbook_delta")])
        assert records[0].sid == 2
        assert records[0].seq == 5
        assert records[0].market_ticker == "A"
