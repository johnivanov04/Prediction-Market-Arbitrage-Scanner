"""Empirical research into whether AT_LEAST_ONE is provable anywhere on Kalshi.

Run with::

    uv run python tools/research_exhaustiveness.py --events 120

Three studies, reported separately because they answer different questions.

**Structured metadata.** Which documented fields actually carry values, and what
those values are. A field that is always null cannot support any claim, however
promising its documentation sounds.

**Historical settlement.** For events whose markets have all settled, how many
winners were there really? A zero-winner event is a *demonstrated* ALL-NO state
-- the exact thing an AT_LEAST_ONE certificate must rule out. This is
falsification evidence, not proof: past behaviour does not bind future contract
semantics, and an event that has always had a winner may still have a
cancellation clause nobody exercised.

**Candidate families.** Which event families look like they might be provable
from authoritative structure, ranked only by evidence quality. Nothing here is
an economic ranking or a recommendation, and nothing is auto-approved.

Read-only public market data. No account endpoint is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.semantics.membership import CombinedVenueMembershipEvidence, MembershipMember
from predarb.semantics.partition import (
    CoverageStatus,
    UnsupportedStrikeError,
    analyse_coverage,
    interval_from_strike,
)
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.membership_capture import (
    enumerate_event_membership,
    sample_event_tickers,
)

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "exhaustiveness_research.json"

MIN_MEMBERS = 2


class FamilyVerdict(StrEnum):
    """How much evidence this family offers toward AT_LEAST_ONE. Not a ranking."""

    POTENTIALLY_PROVABLE = "POTENTIALLY_PROVABLE"
    """Structured evidence points the right way. A human must still read the
    contract; this is where to look, not a conclusion."""

    REQUIRES_MANUAL_REVIEW = "REQUIRES_MANUAL_REVIEW"
    """Nothing structural to go on either way -- the ordinary case for named
    categorical events, where only the rules text can decide."""

    CLEARLY_NON_EXHAUSTIVE = "CLEARLY_NON_EXHAUSTIVE"
    """Demonstrated ALL-NO, or a structural gap. Not provable as it stands."""

    BLOCKED_BY_SCALAR_OR_SPECIAL_SETTLEMENT = "BLOCKED_BY_SCALAR_OR_SPECIAL_SETTLEMENT"
    """Scalar or otherwise non-binary settlement means "at least one YES" does
    not mean "at least one paid the notional"."""


@dataclass
class EventStudy:
    event_ticker: str
    members: int
    mutually_exclusive: bool | None
    settled_members: int
    yes_winners: int
    scalar_members: int
    strike_types: Counter[str]
    coverage: str
    coverage_detail: str
    verdict: FamilyVerdict
    notes: list[str]

    def payload(self) -> dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "series": self.event_ticker.split("-")[0],
            "members": self.members,
            "mutually_exclusive": self.mutually_exclusive,
            "settled_members": self.settled_members,
            "yes_winners": self.yes_winners,
            "scalar_members": self.scalar_members,
            "strike_types": dict(self.strike_types),
            "coverage": self.coverage,
            "coverage_detail": self.coverage_detail,
            "verdict": self.verdict.value,
            "notes": self.notes,
        }


def study_coverage(members: tuple[MembershipMember, ...]) -> tuple[str, str, list[str]]:
    """Read the members' documented strike intervals, if they have any."""
    intervals = []
    unreadable: list[str] = []
    for member in members:
        try:
            intervals.append(
                interval_from_strike(
                    ticker=member.ticker,
                    strike_type=member.strike_type,
                    floor_strike=(
                        Decimal(member.floor_strike) if member.floor_strike is not None else None
                    ),
                    cap_strike=(
                        Decimal(member.cap_strike) if member.cap_strike is not None else None
                    ),
                )
            )
        except UnsupportedStrikeError as exc:
            unreadable.append(str(exc))
    if not intervals:
        return CoverageStatus.NOT_APPLICABLE.value, "no readable numeric strikes", unreadable
    result = analyse_coverage(intervals, unreadable=unreadable)
    return result.status.value, result.detail, unreadable


def classify_event(evidence: CombinedVenueMembershipEvidence) -> EventStudy:
    members = evidence.members
    settled = [m for m in members if m.result]
    winners = [m for m in settled if (m.result or "").lower() == "yes"]
    # Both the declared type and the realised result. KXGOVCANOMR-26 settled one
    # member to "scalar" while every other member settled NO, so a check on
    # market_type alone would have missed the scalar hazard entirely.
    scalar = [
        m
        for m in members
        if (m.market_type or "").lower() == "scalar" or (m.result or "").lower() == "scalar"
    ]
    strike_types: Counter[str] = Counter(m.strike_type or "(none)" for m in members)
    coverage, coverage_detail, unreadable = study_coverage(members)

    notes: list[str] = []
    verdict = FamilyVerdict.REQUIRES_MANUAL_REVIEW

    if scalar:
        verdict = FamilyVerdict.BLOCKED_BY_SCALAR_OR_SPECIAL_SETTLEMENT
        notes.append(
            f"{len(scalar)} scalar member(s) by declared type or realised result: "
            "'at least one settled YES' would not mean 'at least one paid the notional'"
        )
    elif settled and not winners:
        # A demonstrated ALL-NO. The strongest negative evidence available.
        verdict = FamilyVerdict.CLEARLY_NON_EXHAUSTIVE
        notes.append(
            f"all {len(settled)} settled member(s) resolved NO: this event reached "
            "the exact terminal state AT_LEAST_ONE forbids"
        )
    elif coverage == CoverageStatus.GAP.value:
        verdict = FamilyVerdict.CLEARLY_NON_EXHAUSTIVE
        notes.append(f"structured strike gap: {coverage_detail}")
    elif coverage == CoverageStatus.GAP_FREE.value and not unreadable:
        verdict = FamilyVerdict.POTENTIALLY_PROVABLE
        notes.append(
            "documented strike intervals tile their range without gaps; a reviewer "
            "must still close the domain and rule out cancellation and void"
        )

    if len(members) < MIN_MEMBERS:
        verdict = FamilyVerdict.REQUIRES_MANUAL_REVIEW
        notes.append("fewer than two members; AT_LEAST_ONE says nothing useful here")
    if len(winners) > 1:
        notes.append(
            f"{len(winners)} members settled YES simultaneously: permitted under "
            "AT_LEAST_ONE, and proof that this event is NOT mutually exclusive"
        )
    if unreadable:
        notes.append(f"{len(unreadable)} member(s) have unreadable strike semantics")

    return EventStudy(
        event_ticker=evidence.event_ticker,
        members=len(members),
        mutually_exclusive=evidence.event_mutually_exclusive,
        settled_members=len(settled),
        yes_winners=len(winners),
        scalar_members=len(scalar),
        strike_types=strike_types,
        coverage=coverage,
        coverage_detail=coverage_detail,
        verdict=verdict,
        notes=notes,
    )


def structured_metadata_census(
    studies: list[EventStudy], details: list[dict[str, Any]]
) -> dict[str, Any]:
    """What the documented fields actually contain, across the sample."""
    field_presence: Counter[str] = Counter()
    strike_types: Counter[str] = Counter()
    total_members = 0
    for detail in details:
        for member in detail["members"].values():
            total_members += 1
            for name in ("primary_participant_key",):
                if member.get(name):
                    field_presence[name] += 1
    for study in studies:
        strike_types.update(study.strike_types)
    return {
        "members_inspected": total_members,
        "strike_type_values": dict(strike_types),
        "field_presence": dict(field_presence),
        "mutually_exclusive_values": dict(Counter(str(s.mutually_exclusive) for s in studies)),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Exhaustiveness research")
    parser.add_argument("--event", action="append", metavar="EVENT_TICKER")
    parser.add_argument("--events", type=int, default=120)
    parser.add_argument("--series", type=int, default=600)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args()

    settings = Settings()
    studies: list[EventStudy] = []
    details: list[dict[str, Any]] = []

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        cutoff = await client.get_historical_cutoff()
        print(f"historical cutoff: {cutoff.market_settled_ts}")
        tickers = (
            list(args.event)
            if args.event
            else await sample_event_tickers(client, count=args.events, series_limit=args.series)
        )
        print(f"Studying {len(tickers)} event(s) ...\n")

        for index, event_ticker in enumerate(tickers, start=1):
            try:
                evidence = await enumerate_event_membership(client, event_ticker)
            except Exception as exc:
                print(f"  [{index}/{len(tickers)}] {event_ticker}: {type(exc).__name__}")
                continue
            study = classify_event(evidence)
            studies.append(study)
            details.append(evidence.audit_payload())
            print(
                f"  [{index}/{len(tickers)}] {event_ticker:<34s} "
                f"{study.members:>3d} members  {study.yes_winners} winner(s)  "
                f"{study.verdict.value}"
            )

    by_family: dict[str, list[str]] = defaultdict(list)
    for study in studies:
        by_family[study.verdict.value].append(study.event_ticker)

    settled_events = [s for s in studies if s.settled_members == s.members and s.members]
    me_all_no = [s for s in settled_events if s.mutually_exclusive and s.yes_winners == 0]
    zero_winner = [s for s in settled_events if s.yes_winners == 0]
    multi_winner = [s for s in settled_events if s.yes_winners > 1]
    one_winner = [s for s in settled_events if s.yes_winners == 1]

    summary = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "note": "derived research output; not source evidence, and not a recommendation",
        "events_studied": len(studies),
        "verdicts": {k: len(v) for k, v in sorted(by_family.items())},
        "candidate_families": {k: sorted(v)[:20] for k, v in sorted(by_family.items())},
        "historical_settlement": {
            "fully_settled_events": len(settled_events),
            "exactly_one_winner": len(one_winner),
            "zero_winners_DEMONSTRATED_ALL_NO": len(zero_winner),
            "multiple_winners": len(multi_winner),
            "zero_winner_examples": [s.event_ticker for s in zero_winner][:20],
            "multi_winner_examples": [s.event_ticker for s in multi_winner][:20],
            # The decisive refutation of "mutually_exclusive implies at-least-one".
            "mutually_exclusive_AND_zero_winners": len(me_all_no),
            "mutually_exclusive_all_no_examples": [s.event_ticker for s in me_all_no][:20],
        },
        "structured_metadata": structured_metadata_census(studies, details),
        "coverage_statuses": dict(Counter(s.coverage for s in studies)),
        "rows": [s.payload() for s in studies],
    }
    args.out.write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print("\n=== EXHAUSTIVENESS RESEARCH ===")
    for key, value in summary.items():
        if key in {"rows", "note", "candidate_families"}:
            continue
        print(f"  {key}: {value}")
    print(f"\nWrote {args.out}")
    print(
        "\nA zero-winner event is a DEMONSTRATED ALL-NO state. A history of one "
        "winner every time is not proof of anything: past behaviour does not bind "
        "future contract semantics."
    )


if __name__ == "__main__":
    asyncio.run(main())
