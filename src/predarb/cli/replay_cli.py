"""``predarb replay`` commands: inspect, verify and run a bundle.

All offline. These commands construct no venue client and make no request; a
bundle is either self-sufficient or the replay reports what is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from predarb.replay.bundle import BundleIntegrityError, ReplayBundle, load_bundle
from predarb.replay.decision import DetectorKind
from predarb.replay.engine import ReplayEngine, ReplayResult
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import ObservationKind
from predarb.replay.plan import DetectorPlan
from predarb.venues.kalshi.replay_context import KalshiReplayContext

replay_app = typer.Typer(
    help="Point-in-time replay of captured observations (offline).",
    no_args_is_help=True,
)


def _load(bundle_path: Path) -> ReplayBundle:
    try:
        return load_bundle(bundle_path)
    except BundleIntegrityError as exc:
        typer.secho(f"bundle integrity failure: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@replay_app.command("inspect")
def replay_inspect(bundle_path: Path) -> None:
    """Describe a bundle without replaying it."""
    bundle = _load(bundle_path)
    manifest = bundle.manifest
    typer.echo(bundle.describe())
    typer.echo(f"  schema        : {manifest.schema_version}")
    typer.echo(f"  assembled at  : {manifest.created_at.isoformat()} (NOT knowledge time)")
    typer.echo(f"  observed      : {manifest.observation_start} .. {manifest.observation_end}")
    typer.echo(f"  venues        : {list(manifest.venues)}")
    typer.echo(f"  markets       : {list(manifest.markets)}")
    typer.echo(f"  epochs        : {list(manifest.connection_epochs)}")
    typer.echo("  observations  :")
    for kind, count in manifest.observation_counts_by_kind.items():
        typer.echo(f"      {count:>6}  {kind}")
    typer.echo(f"  integrity     : {bundle.integrity.describe()}")


@replay_app.command("verify")
def replay_verify(bundle_path: Path) -> None:
    """Check bundle integrity and exit non-zero if it cannot be replayed."""
    bundle = _load(bundle_path)
    colour = typer.colors.GREEN if bundle.permits_replay else typer.colors.RED
    typer.secho(f"{bundle.integrity.describe()}", fg=colour)
    typer.echo(f"  observations read: {bundle.integrity.observations_read}")
    if bundle.integrity.dropped_tail_bytes:
        typer.echo(f"  dropped tail bytes: {bundle.integrity.dropped_tail_bytes}")
    if not bundle.permits_replay:
        raise typer.Exit(code=1)


def _run(bundle: ReplayBundle) -> ReplayResult:
    """Replay a bundle through the production engine. Offline by construction.

    The plan comes from the bundle, never from inference at replay time: a
    replay that grouped markets itself could evaluate a basket the live session
    never considered, and the two would diverge for reasons unrelated to the
    economics.
    """
    plan = DetectorPlan.from_payload(bundle.manifest.detector_plan)
    engine = ReplayEngine(
        plan=plan,
        provider=KalshiReplayContext(knowledge=KnowledgeBase.from_stream(bundle.stream), plan=plan),
    )
    return engine.run(bundle)


@replay_app.command("run")
def replay_run(
    bundle_path: Path,
    report: Path = typer.Option(None, help="Write the run report here."),
    show_decisions: int = typer.Option(20, help="How many decisions to print."),
) -> None:
    """Replay a bundle and report every decision it produced.

    Where the bundle lacks the point-in-time context a decision needs, the run
    records MISSING_POINT_IN_TIME_KNOWLEDGE rather than fetching any of it. That
    is the honest outcome for an under-specified dataset, and the reason there
    is no ``--fill-from-api`` flag.
    """
    bundle = _load(bundle_path)
    typer.echo(bundle.describe())

    if not bundle.manifest.detector_plan:
        typer.secho(
            "\nThis bundle carries no detector plan, so there is nothing to say it was\n"
            "monitoring. Transport can be replayed; economics cannot. Re-capture with\n"
            "tools/capture_replay_bundle.py to record a plan and the context a decision\n"
            "needs.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    result = _run(bundle)
    typer.echo(f"  plan        : {result.plan.describe()}")
    typer.echo(f"  observations: {result.observations_processed}")
    typer.echo(f"  triggers    : {result.triggers}")
    typer.echo(f"  decisions   : {len(result.decisions)} ({result.detector_run_count} ran)")
    typer.echo(f"  network     : {result.network_calls} calls")
    typer.echo(f"  digest      : {result.digest}")

    typer.echo("\n  classifications:")
    for classification, count in result.classification_counts.items():
        typer.echo(f"      {count:>6}  {classification}")

    typer.echo("\n  completeness at the final horizon:")
    for dimension, status in sorted(result.completeness.dimensions.items()):
        typer.echo(f"      {dimension.value:<22s} {status.value}")

    for detector in DetectorKind:
        decisions = result.decisions_for(detector)
        if not decisions:
            continue
        typer.echo(f"\n  {detector.value}: {len(decisions)} decision(s)")
        typer.echo(f"      {result.absence_statement(str(bundle_path), detector)}")
        for decision in decisions[:show_decisions]:
            typer.echo(f"      {decision.describe()}")
        if len(decisions) > show_decisions:
            typer.echo(f"      ... {len(decisions) - show_decisions} more")

    for warning in result.warnings:
        typer.secho(f"\n  ! {warning}", fg=typer.colors.YELLOW)

    if report is not None:
        payload = {
            "note": "derived output; not source evidence",
            "bundle_id": result.bundle_id,
            "stream_hash": result.stream_hash,
            "integrity": result.integrity,
            "mode": result.mode.value,
            "plan": result.plan.to_payload(),
            "observations": result.observations_processed,
            "triggers": result.triggers,
            "network_calls": result.network_calls,
            "digest": result.digest,
            "classification_counts": dict(result.classification_counts),
            "completeness": result.completeness.as_dict(),
            "decisions": [decision.comparable() for decision in result.decisions],
            "warnings": list(result.warnings),
        }
        report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        typer.echo(f"\nWrote {report}")


@replay_app.command("knowledge")
def replay_knowledge(bundle_path: Path) -> None:
    """What the bundle knew, by category, at its final horizon."""
    bundle = _load(bundle_path)
    if not bundle.stream.observations:
        typer.echo("empty bundle")
        return
    knowledge = KnowledgeBase.from_stream(bundle.stream)
    horizon = bundle.stream.horizon_at(bundle.stream.observations[-1].ordinal)
    typer.echo(bundle.describe())
    for category, keys in knowledge.known_keys(horizon).items():
        typer.echo(f"  {category:<24s} {len(keys)}  {list(keys)[:6]}")
    counts = knowledge.counts_at(horizon)
    typer.echo("  explicit knowledge snapshots:")
    for kind in ObservationKind:
        if kind.is_knowledge_snapshot:
            typer.echo(f"      {counts.get(kind.value, 0):>6}  {kind.value}")
    pending = knowledge.scheduled_not_yet_effective(horizon)
    if pending:
        typer.echo(f"  known but not yet effective: {len(pending)}")
        for entry in pending[:10]:
            typer.echo(f"      {entry}")
