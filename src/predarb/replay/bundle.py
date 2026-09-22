"""The replay bundle: a self-describing, hash-verified observation dataset.

A bundle is everything a replay needs and nothing it should reach outside for.
It carries the observation stream (transport lifecycle, frames, metadata, fee
records, evidence and certificate issuances), a manifest with per-file hashes,
and the schema versions needed to interpret it.

Bundle creation time is not knowledge time
------------------------------------------
A bundle may be assembled long after the events it describes, from files that
were captured prospectively. Its own ``created_at`` says nothing about what was
historically known: every observation keeps the ``observed_at`` and ordinal it
was captured with. Stamping the bundle's creation time onto old observations
would silently make the entire dataset "known" at assembly time, which is the
most complete form of lookahead available.

Integrity is checked before anything is replayed
------------------------------------------------
A truncated middle record, an edited journal or an unknown schema version means
the replay cannot speak for the period it claims to cover. Those fail closed
rather than quietly producing "no opportunities", which is indistinguishable
from a clean run that genuinely found nothing.

A truncated *tail* is treated separately: a capture killed mid-write is an
ordinary, recoverable end-of-file, and Step 4's tolerant reader policy applies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from predarb.clock import ensure_utc
from predarb.replay.observation import Observation, ObservationKind, ObservationStream

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleIntegrity",
    "BundleIntegrityError",
    "BundleManifest",
    "IntegrityReport",
    "ReplayBundle",
    "load_bundle",
    "write_bundle",
]

BUNDLE_SCHEMA_VERSION = "replay-bundle/1"
OBSERVATIONS_FILE = "observations.jsonl"
MANIFEST_FILE = "manifest.json"


class BundleIntegrityError(Exception):
    """The bundle cannot be trusted to represent the period it claims."""


class BundleIntegrity(StrEnum):
    VERIFIED = "VERIFIED"
    """Every hash matched and the schema is understood."""

    TRUNCATED_TAIL = "TRUNCATED_TAIL"
    """The final record is incomplete -- an ordinary crash-during-write. The
    prefix is usable; the bundle simply ends earlier than intended."""

    HASH_MISMATCH = "HASH_MISMATCH"
    CORRUPT_RECORD = "CORRUPT_RECORD"
    MISSING_FILE = "MISSING_FILE"
    UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"

    @property
    def permits_replay(self) -> bool:
        """Only a verified bundle, or one whose damage is a known-safe tail."""
        return self in {BundleIntegrity.VERIFIED, BundleIntegrity.TRUNCATED_TAIL}


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    integrity: BundleIntegrity
    detail: str = ""
    observations_read: int = 0
    dropped_tail_bytes: int = 0

    @property
    def permits_replay(self) -> bool:
        return self.integrity.permits_replay

    def describe(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.integrity.value}{suffix}"


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """What the bundle claims to be, and the hashes that prove it."""

    bundle_id: str
    schema_version: str
    created_at: datetime
    """When the bundle was assembled. **Not** when anything was learned."""

    observation_start: datetime | None
    observation_end: datetime | None
    venues: tuple[str, ...]
    markets: tuple[str, ...]
    connection_epochs: tuple[int, ...]
    observation_count: int
    observation_counts_by_kind: dict[str, int]
    file_hashes: dict[str, str]
    stream_hash: str
    detector_plan: dict[str, Any] = field(default_factory=dict)
    """The monitoring configuration this session ran. A replay evaluates only
    what the session said it was watching; inferring groups at replay time would
    let it consider work the live system never did."""

    context_snapshot_counts: dict[str, int] = field(default_factory=dict)
    supports_economic_replay: bool = False
    """Whether the bundle carries the context a detector decision needs. Visible
    in the manifest so this is knowable *before* running anything."""

    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        for name in ("observation_start", "observation_end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value))

    def to_payload(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "created_at_note": ("bundle assembly time; NOT the knowledge time of any observation"),
            "observation_start": (
                self.observation_start.isoformat() if self.observation_start else None
            ),
            "observation_end": (self.observation_end.isoformat() if self.observation_end else None),
            "venues": list(self.venues),
            "markets": list(self.markets),
            "connection_epochs": list(self.connection_epochs),
            "observation_count": self.observation_count,
            "observation_counts_by_kind": dict(self.observation_counts_by_kind),
            "file_hashes": dict(self.file_hashes),
            "stream_hash": self.stream_hash,
            "detector_plan": dict(self.detector_plan),
            "context_snapshot_counts": dict(self.context_snapshot_counts),
            "supports_economic_replay": self.supports_economic_replay,
            "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls(
            bundle_id=payload["bundle_id"],
            schema_version=payload["schema_version"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            observation_start=(
                datetime.fromisoformat(payload["observation_start"])
                if payload["observation_start"]
                else None
            ),
            observation_end=(
                datetime.fromisoformat(payload["observation_end"])
                if payload["observation_end"]
                else None
            ),
            venues=tuple(payload["venues"]),
            markets=tuple(payload["markets"]),
            connection_epochs=tuple(payload["connection_epochs"]),
            observation_count=payload["observation_count"],
            observation_counts_by_kind=dict(payload["observation_counts_by_kind"]),
            file_hashes=dict(payload["file_hashes"]),
            stream_hash=payload["stream_hash"],
            detector_plan=dict(payload.get("detector_plan", {})),
            context_snapshot_counts=dict(payload.get("context_snapshot_counts", {})),
            supports_economic_replay=bool(payload.get("supports_economic_replay", False)),
            notes=payload.get("notes", ""),
        )

    def describe(self) -> str:
        span = ""
        if self.observation_start and self.observation_end:
            span = f" {self.observation_start.isoformat()} .. {self.observation_end.isoformat()}"
        economic = (
            "economic-replay-ready"
            if self.supports_economic_replay
            else ("book-replay only (no detector context captured)")
        )
        return (
            f"{self.bundle_id[:12]} [{self.schema_version}]{span} "
            f"{self.observation_count} observations, {len(self.markets)} markets, "
            f"epochs {list(self.connection_epochs)}, {economic}"
        )


@dataclass(frozen=True, slots=True)
class ReplayBundle:
    """A verified, immutable observation dataset."""

    root: Path
    manifest: BundleManifest
    stream: ObservationStream
    integrity: IntegrityReport = field(
        default_factory=lambda: IntegrityReport(BundleIntegrity.VERIFIED)
    )

    @property
    def permits_replay(self) -> bool:
        return self.integrity.permits_replay

    def describe(self) -> str:
        return f"{self.manifest.describe()} [{self.integrity.describe()}]"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_bundle(
    root: Path,
    stream: ObservationStream,
    *,
    venues: tuple[str, ...] = ("KALSHI",),
    markets: tuple[str, ...] = (),
    created_at: datetime | None = None,
    detector_plan: dict[str, Any] | None = None,
    notes: str = "",
) -> BundleManifest:
    """Serialise a stream plus a manifest that can verify it.

    Observations keep their own ``observed_at`` and ordinal untouched; the
    manifest records assembly time separately and says so in the payload.
    """
    root.mkdir(parents=True, exist_ok=True)
    observations_path = root / OBSERVATIONS_FILE
    observations_path.write_text(
        "".join(observation.to_json_line() + "\n" for observation in stream),
        encoding="utf-8",
    )

    span = stream.span
    counts = stream.counts()
    snapshot_counts = {
        kind.value: counts.get(kind.value, 0)
        for kind in ObservationKind
        if kind.is_knowledge_snapshot
    }
    # Economic replay needs a detector plan plus every knowledge source that a
    # decision consults. Stated up front so nobody runs a bundle expecting
    # decisions it cannot produce.
    supports_economic = bool(detector_plan) and all(
        snapshot_counts.get(kind.value, 0) > 0
        for kind in (
            ObservationKind.METADATA_SNAPSHOT,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
        )
    )
    manifest = BundleManifest(
        bundle_id=stream.content_hash(),
        schema_version=BUNDLE_SCHEMA_VERSION,
        created_at=created_at or datetime.now(tz=UTC),
        observation_start=span[0] if span else None,
        observation_end=span[1] if span else None,
        venues=venues,
        markets=tuple(sorted(markets)),
        connection_epochs=stream.connection_epochs,
        observation_count=len(stream),
        observation_counts_by_kind=stream.counts(),
        file_hashes={OBSERVATIONS_FILE: _file_hash(observations_path)},
        stream_hash=stream.content_hash(),
        detector_plan=detector_plan or {},
        context_snapshot_counts=snapshot_counts,
        supports_economic_replay=supports_economic,
        notes=notes,
    )
    (root / MANIFEST_FILE).write_text(
        json.dumps(manifest.to_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_bundle(root: Path, *, tolerate_truncated_tail: bool = True) -> ReplayBundle:
    """Load and verify a bundle. Fails closed on anything but a clean tail."""
    manifest_path = root / MANIFEST_FILE
    if not manifest_path.exists():
        raise BundleIntegrityError(
            f"{root}: no {MANIFEST_FILE}; a bundle without a manifest cannot state "
            "what period it covers or prove its contents"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = BundleManifest.from_payload(payload)

    if manifest.schema_version != BUNDLE_SCHEMA_VERSION:
        raise BundleIntegrityError(
            f"{root}: bundle schema {manifest.schema_version!r} is not "
            f"{BUNDLE_SCHEMA_VERSION!r}; refusing to guess at its meaning"
        )

    observations_path = root / OBSERVATIONS_FILE
    if not observations_path.exists():
        raise BundleIntegrityError(f"{root}: manifest present but {OBSERVATIONS_FILE} missing")

    raw = observations_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    tail_incomplete = bool(raw) and not raw.endswith("\n")

    observations: list[Observation] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        is_last = index == len(lines) - 1
        try:
            observations.append(Observation.from_json_line(line))
        except (ValueError, KeyError) as exc:
            if is_last and tail_incomplete and tolerate_truncated_tail:
                # A capture killed mid-write. The prefix is still evidence.
                break
            raise BundleIntegrityError(
                f"{root}: record {index} is corrupt ({exc}); a damaged record in the "
                "middle of a bundle means the replay cannot speak for this period"
            ) from exc

    stream = ObservationStream.from_iterable(observations)
    dropped = 0
    integrity = BundleIntegrity.VERIFIED
    detail = ""

    if tail_incomplete:
        integrity = BundleIntegrity.TRUNCATED_TAIL
        dropped = len(lines[-1].encode()) if lines else 0
        detail = "final record incomplete; replay ends earlier than the capture intended"
    else:
        actual = _file_hash(observations_path)
        expected = manifest.file_hashes.get(OBSERVATIONS_FILE)
        if expected != actual:
            raise BundleIntegrityError(
                f"{root}: {OBSERVATIONS_FILE} hash {actual[:12]} does not match the "
                f"manifest's {str(expected)[:12]}; the dataset has been altered"
            )
        if stream.content_hash() != manifest.stream_hash:
            raise BundleIntegrityError(
                f"{root}: observation stream hash does not match the manifest"
            )
        if len(stream) != manifest.observation_count:
            raise BundleIntegrityError(
                f"{root}: manifest claims {manifest.observation_count} observations, "
                f"found {len(stream)}"
            )

    return ReplayBundle(
        root=root,
        manifest=manifest,
        stream=stream,
        integrity=IntegrityReport(
            integrity=integrity,
            detail=detail,
            observations_read=len(stream),
            dropped_tail_bytes=dropped,
        ),
    )
