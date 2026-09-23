"""``predarb relations`` -- request, review, show and validate relation claims.

One certification path, shared by both claims. There is deliberately no
"quick issue" command and no flag that skips the checklist: a relation
certificate exists because a person read a contract and said so, and a second
code path that could produce one without that would make the first path
decorative.

AT_LEAST_ONE differs from AT_MOST_ONE in what it asks, not in how it is
approved. Both walk a checklist, both require an exact confirmation phrase, and
both refuse approval on incomplete evidence or on any answer outside the safe
set -- ``UNCERTAIN`` included.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from predarb.config import Settings
from predarb.semantics.exhaustiveness import (
    AT_LEAST_ONE_POLICY_VERSION,
    ProofBasis,
    exhaustiveness_incompleteness,
)
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.partition import CoverageStatus, DomainBound, DomainConstraintEvidence
from predarb.semantics.registry import RelationRegistry
from predarb.semantics.relation import (
    ALL_SELECTED_MEMBERS_LOSE,
    JointStateRule,
    RelationClaim,
    RelationDecision,
    RelationReviewRequest,
    checklist_for_claim,
)
from predarb.semantics.review import ChecklistAnswer, Decision
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.exhaustiveness_capture import (
    ExhaustivenessEvidence,
    capture_exhaustiveness_evidence,
)

relations_app = typer.Typer(
    help="Request, review and validate logical relations between markets.",
    no_args_is_help=True,
)

DEFAULT_ROOT = Path("./data/semantics")

_CLAIMS = {
    "at-most-one": RelationClaim.AT_MOST_ONE,
    "at-least-one": RelationClaim.AT_LEAST_ONE,
}
_ANSWERS = {
    "yes": ChecklistAnswer.YES,
    "y": ChecklistAnswer.YES,
    "no": ChecklistAnswer.NO,
    "n": ChecklistAnswer.NO,
    "uncertain": ChecklistAnswer.UNCERTAIN,
    "u": ChecklistAnswer.UNCERTAIN,
    "n-a": ChecklistAnswer.NOT_APPLICABLE,
    "na": ChecklistAnswer.NOT_APPLICABLE,
}


def _echo(message: str, **kwargs: object) -> None:
    typer.secho(message, **kwargs)  # type: ignore[arg-type]


@relations_app.command("request")
def relations_request(  # noqa: PLR0917 - every option is a distinct capture input
    event_ticker: str,
    member: list[str] = typer.Option(..., "--member", help="A selected member ticker."),
    claim: str = typer.Option("at-least-one", help="at-most-one | at-least-one"),
    basis: str = typer.Option(
        "CONTRACT_LANGUAGE",
        help="What an AT_LEAST_ONE proof rests on: "
        "CONTRACT_LANGUAGE | STRUCTURED_PARTITION | VENUE_MEMBERSHIP_COVERAGE",
    ),
    root: Path = typer.Option(DEFAULT_ROOT, help="Local research-data root."),
    no_documents: bool = typer.Option(False, help="Skip fetching governing documents."),
    domain_step: str = typer.Option(
        "",
        help="Smallest increment the settlement value can take, e.g. 1 for whole "
        "degrees. ON ITS OWN this yields a CONDITIONAL result, never a proof.",
    ),
    domain_lower: str = typer.Option("", help="Contract lower bound on the underlying."),
    domain_upper: str = typer.Option("", help="Contract upper bound on the underlying."),
    domain_variable: str = typer.Option("", help="What the domain constrains."),
    domain_units: str = typer.Option("", help="Units the step is expressed in."),
    domain_source: list[str] = typer.Option(
        [],
        "--domain-source",
        help="Evidence component carrying the language, as component_id=sha256. "
        "Required to discharge a domain condition.",
    ),
    domain_quote: str = typer.Option(
        "", help="The sentence relied on, verbatim, from the named source."
    ),
    domain_reviewer: str = typer.Option(
        "", help="Who read the source and attests to this reading."
    ),
    domain_justification: str = typer.Option("", help="Why those bounds hold."),
) -> None:
    """Capture evidence and generate a review request. Never approves anything.

    The request is stored whether or not its evidence is complete. An incomplete
    packet is a real, reviewable state that says exactly what is missing --
    discarding it would hide the gap rather than close it.
    """
    relation_claim = _CLAIMS.get(claim.lower())
    if relation_claim is None:
        _echo(f"unknown claim {claim!r}; expected one of {sorted(_CLAIMS)}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    try:
        exhaustiveness_basis = ProofBasis(basis.upper())
    except ValueError:
        _echo(f"unknown basis {basis!r}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    domain = _domain_bound(
        lower=domain_lower,
        upper=domain_upper,
        step=domain_step,
        variable=domain_variable,
        units=domain_units,
        sources=list(domain_source),
        quote=domain_quote,
        reviewer=domain_reviewer,
        justification=domain_justification,
    )
    settings = Settings()

    async def capture() -> ExhaustivenessEvidence:
        async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
            return await capture_exhaustiveness_evidence(
                client,
                event_ticker=event_ticker,
                selected_members=list(member),
                basis=exhaustiveness_basis,
                fetch_documents=not no_documents,
                domain=domain,
            )

    evidence = asyncio.run(capture())
    bundle = evidence.bundle
    membership = evidence.membership
    coverage = evidence.coverage

    extra: tuple[str, ...] = ()
    if relation_claim is RelationClaim.AT_LEAST_ONE:
        extra = exhaustiveness_incompleteness(
            basis=exhaustiveness_basis,
            selected_members=bundle.selected_members,
            documents=dict(bundle.documents),
            settlement_sources=bundle.event_fields.get("settlement_sources") or (),
            membership=membership,
            partition_supported=coverage.supports_exhaustiveness_argument,
        )

    request = RelationReviewRequest.create(
        bundle=bundle,
        claim=relation_claim,
        generated_at=datetime.now(tz=UTC),
        extra_incompleteness=extra,
    )

    registry = RelationRegistry(root)
    registry.store_evidence(bundle)
    if membership is not None:
        registry.store_membership(membership)
    path = registry.store_request(request)

    rule = JointStateRule(claim=relation_claim, members=bundle.selected_members)
    _echo(f"\n{request.describe()}")
    _echo(f"  proposition   : {relation_claim.proposition}")
    _echo(f"  forbids       : {rule.forbidden_description()}")
    if relation_claim is RelationClaim.AT_LEAST_ONE:
        _echo(f"  basis         : {exhaustiveness_basis.value} -- {exhaustiveness_basis.describe}")
        for assumption in coverage.assumptions:
            _echo(f"  assumption    : {assumption}")
        _echo(f"  policy        : {AT_LEAST_ONE_POLICY_VERSION}")
        _echo(f"  partition     : {coverage.describe()}")
        if coverage.status is CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE:
            _echo(
                f"  !! CONDITIONAL : gap-free only if the settlement value moves in "
                f"steps of {coverage.required_domain_step}. Over a continuous domain "
                f"there are {len(coverage.unconditional_gaps)} real gap(s):",
                fg=typer.colors.YELLOW,
            )
            for gap in coverage.unconditional_gaps:
                _echo(f"                   {gap}")
            _echo(
                "                   Supply --domain-source/--domain-quote/"
                "--domain-reviewer to attest the domain, or answer the partition "
                "checklist item UNCERTAIN.",
                fg=typer.colors.YELLOW,
            )
            # The components a reviewer could cite, with the hashes that make a
            # citation revocable. Listed, never searched: deciding whether any
            # of them actually establishes the granularity is reading, and
            # reading is the reviewer's job.
            _echo("                   Citable components (read these before attesting):")
            for entry in bundle.member_evidence:
                if entry.ticker in bundle.selected_members and entry.rules_hash:
                    _echo(f"                     market.{entry.ticker}.rules={entry.rules_hash}")
        if membership is not None:
            _echo(f"  membership    : {membership.describe()}")
            _echo(
                f"  membership is : {'MATERIAL' if bundle.membership_is_material else 'audit-only'}"
            )
    if request.incompleteness_reasons:
        _echo(
            "\n  evidence is incomplete; this request cannot be approved:", fg=typer.colors.YELLOW
        )
        for reason in request.incompleteness_reasons:
            _echo(f"    - {reason}")
    else:
        _echo("\n  AWAITING_REVIEW -- no automatic approval exists.", fg=typer.colors.GREEN)
    _echo(f"\nWrote {path}")


@relations_app.command("list")
def relations_list(root: Path = typer.Option(DEFAULT_ROOT)) -> None:
    """Requests and certificates in the local registry."""
    registry = RelationRegistry(root)
    requests = registry.list_requests()
    _echo(f"{len(requests)} relation review request(s):")
    for row in requests:
        state = "AWAITING_REVIEW" if row["completeness"] == "COMPLETE" else "EVIDENCE_INCOMPLETE"
        _echo(
            f"  {row['request_id'][:12]}  {row['claim']:<13s} {row['event_ticker']:<28s} "
            f"{len(row['selected_members'])} member(s)  [{state}]"
        )
    records = registry.list_certificates()
    _echo(f"\n{len(records)} relation certificate(s):")
    for record in records:
        _echo(f"  {record.describe()}")


@relations_app.command("show")
def relations_show(request_id: str, root: Path = typer.Option(DEFAULT_ROOT)) -> None:
    """Everything recorded about one request, including what it relies on."""
    registry = RelationRegistry(root)
    stored = next(
        (r for r in registry.list_requests() if r["request_id"].startswith(request_id)), None
    )
    if stored is None:
        _echo(f"no request matching {request_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _echo(json.dumps(stored, indent=2, sort_keys=True))

    snapshot_path = registry.evidence_dir / f"{stored['snapshot_id']}.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        _echo("\n--- exhaustiveness evidence ---")
        for key in (
            "exhaustiveness_basis",
            "membership_fingerprint",
            "membership_is_material",
            "partition_fingerprint",
            "partition_is_material",
        ):
            _echo(f"  {key}: {snapshot.get(key)}")


@relations_app.command("review")
def relations_review(
    request_id: str,
    reviewer: str = typer.Option(..., help="Who is making this decision."),
    root: Path = typer.Option(DEFAULT_ROOT),
) -> None:
    """Walk the claim's checklist and record an explicit decision.

    Interactive by design, and approval needs the exact phrase
    ``APPROVE AT_LEAST_ONE <REQUEST_ID>``. A phrase that names the claim and the
    request cannot be typed by accident, and cannot be reused for a different
    claim over the same members.
    """
    registry = RelationRegistry(root)
    stored = next(
        (r for r in registry.list_requests() if r["request_id"].startswith(request_id)), None
    )
    if stored is None:
        _echo(f"no request matching {request_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    claim = RelationClaim(stored["claim"])
    members = tuple(stored["selected_members"])
    rule = JointStateRule(claim=claim, members=members)

    _echo(f"\nReviewing {stored['event_ticker']}  [{claim.value}]")
    _echo(f"  members     : {list(members)}")
    _echo(f"  proposition : {claim.proposition}")
    _echo(f"  FORBIDS     : {rule.forbidden_description()}")
    if claim is RelationClaim.AT_LEAST_ONE:
        _echo(
            f"\n  Note: {ALL_SELECTED_MEMBERS_LOSE} is the ONLY state that falsifies "
            "this claim.\n  Multiple members settling YES is permitted and is NOT "
            "mutual exclusion.",
            fg=typer.colors.YELLOW,
        )

    if stored["completeness"] != "COMPLETE":
        _echo("\nEvidence is incomplete; this request cannot be approved:", fg=typer.colors.YELLOW)
        for reason in stored["incompleteness_reasons"]:
            _echo(f"  - {reason}")

    answers: dict[str, ChecklistAnswer] = {}
    _echo("\n--- checklist ---")
    for question in checklist_for_claim(claim):
        _echo(f"\n{question.prompt}")
        _echo(f"  (why: {question.why_it_matters})")
        raw = typer.prompt("  yes / no / uncertain / n-a").strip().lower()
        answers[question.key] = _ANSWERS.get(raw, ChecklistAnswer.UNCERTAIN)

    notes = typer.prompt("\nNotes (optional)", default="", show_default=False)
    phrase = f"APPROVE {claim.value} {stored['request_id']}"
    _echo(f"\nDecision: type '{phrase}' to approve, REJECT, or MORE for more evidence.")
    verdict = typer.prompt("  decision").strip()

    if verdict == phrase:
        decision = Decision.APPROVED
    elif verdict.upper().startswith("APPROVE"):
        _echo(f"  Approval requires the exact phrase: {phrase}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    elif verdict.upper().startswith("REJECT"):
        decision = Decision.REJECTED
    else:
        decision = Decision.NEEDS_MORE_EVIDENCE

    record = RelationDecision(
        request_id=stored["request_id"],
        claim=claim,
        event_ticker=stored["event_ticker"],
        selected_members=members,
        snapshot_id=stored["snapshot_id"],
        evidence_fingerprint=_fingerprint_from(stored["evidence_fingerprint"]),
        decision=decision,
        reviewer=reviewer,
        reviewed_at=datetime.now(tz=UTC),
        checklist_answers=answers,
        notes=notes,
    )
    path = registry.store_decision(record)
    colour = typer.colors.GREEN if decision is Decision.APPROVED else typer.colors.YELLOW
    _echo(f"\n{record.describe()}", fg=colour)
    _echo(f"Wrote {path}")


def _domain_bound(
    *,
    lower: str,
    upper: str,
    step: str,
    variable: str,
    units: str,
    sources: list[str],
    quote: str,
    reviewer: str,
    justification: str,
) -> DomainBound | None:
    """Build a domain bound, and its evidence when the reviewer supplied any.

    A bare ``--domain-step`` is deliberately still accepted, and deliberately
    still not a proof: it produces ``GAP_FREE_IF_DOMAIN_DISCRETE``, which names
    the condition instead of discharging it. Discharging it needs a source
    component, that source's hash, the language relied on, and a named reviewer
    -- the things that make the assertion revocable when the source moves.
    """
    if not any((lower, upper, step, variable, units, sources, quote, reviewer)):
        return None
    if not justification:
        raise typer.BadParameter(
            "--domain-justification is required when asserting a bound or step; "
            "an unexplained domain assertion is an assumption, not evidence"
        )

    hashes: dict[str, str] = {}
    for entry in sources:
        component, _, digest = entry.partition("=")
        if not component or not digest:
            raise typer.BadParameter(
                f"--domain-source {entry!r} must be component_id=sha256; a source "
                "without a content hash cannot be invalidated when it changes"
            )
        hashes[component] = digest

    evidence = None
    if any((variable, units, sources, quote, reviewer)):
        evidence = DomainConstraintEvidence(
            variable=variable or "(unnamed)",
            units=units or "(unspecified)",
            step=Decimal(step) if step else None,
            lower=Decimal(lower) if lower else None,
            upper=Decimal(upper) if upper else None,
            source_component_ids=tuple(hashes),
            source_hashes=hashes,
            quoted_language=quote,
            rationale=justification,
            acknowledged_by=reviewer,
            acknowledged_at=datetime.now(tz=UTC) if reviewer else None,
        )
    return DomainBound(
        lower=Decimal(lower) if lower else None,
        upper=Decimal(upper) if upper else None,
        step=Decimal(step) if step else None,
        evidence=evidence,
        justification=justification,
    )


def _fingerprint_from(payload: Mapping[str, Any]) -> SettlementEvidenceFingerprint:
    return SettlementEvidenceFingerprint(
        schema_version=str(payload["schema_version"]),
        components=dict(payload["components"]),
        digest=str(payload["digest"]),
    )
