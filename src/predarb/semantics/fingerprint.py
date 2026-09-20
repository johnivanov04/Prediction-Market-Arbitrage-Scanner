"""Deterministic fingerprinting of settlement evidence.

Why a rules hash is not enough
------------------------------
A market's economically relevant settlement evidence is wider than its rules
text. The notional, the market type, early-close conditions, expiration
metadata, strike definitions, the parent event's structure, the series'
settlement sources and any external contract document all bear on what the
contract can pay. Any of them can change while ``rules_primary`` stays byte
identical.

Binding a certificate to the rules text alone would therefore allow exactly the
failure this module exists to prevent:

    rules unchanged, but materially relevant settlement evidence changed
    => certificate silently remains valid

So a certificate binds to a fingerprint over *all* required evidence, and the
rules hash survives as one visible component of it -- still useful for audit and
diffs, no longer the whole proof.

What is deliberately excluded
-----------------------------
Prices, volume, open interest and order-book state are **not** settlement
semantics. A market whose price moved has not changed what it pays. Including
volatile fields would invalidate every certificate on every tick, which trains
reviewers to ignore drift -- the opposite of the intent.

Encoding rules
--------------
Absent, null and empty are three different facts and encode differently. A field
that vanished from the API is not a field that arrived as ``null``, and neither
is a field that arrived as an empty string. Collapsing them would hide a real
schema change behind an unchanged digest.

Floats are refused outright rather than normalised. There is no canonical text
form of a binary float that round-trips across producers, so accepting one would
make the digest depend on how a value happened to be decoded.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Self

__all__ = [
    "ABSENT",
    "EVIDENCE_SCHEMA_VERSION",
    "Absent",
    "FingerprintDiff",
    "NonCanonicalValueError",
    "SettlementEvidenceFingerprint",
    "canonical_encoding",
    "synthetic_fingerprint",
]

EVIDENCE_SCHEMA_VERSION: Final = "settlement-evidence/1"
"""Version of the encoding itself.

Part of the digest on purpose: if the way evidence is encoded changes, every
fingerprint must differ even when the underlying evidence did not. Otherwise an
encoding change could silently reconcile two genuinely different bundles.
"""


class Absent:
    """Sentinel for "the source did not contain this field at all".

    Distinct from ``None``, which means the source contained it and it was null.
    """

    _instance: Absent | None = None

    def __new__(cls) -> Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = Absent()


class NonCanonicalValueError(TypeError):
    """A value has no unambiguous canonical encoding.

    Raised rather than coerced. A fingerprint that depends on how a value was
    decoded is not a fingerprint.
    """


def canonical_encoding(value: object) -> str:  # noqa: PLR0911, PLR0912 - one branch per type
    """Encode one evidence value unambiguously.

    Every encoding is type-tagged, so the string ``"1"``, the integer ``1`` and
    the decimal ``1`` produce different bytes. Without the tag, a field that
    changed type between API versions would hash identically.
    """
    if isinstance(value, Absent):
        return "\x00absent"
    if value is None:
        return "\x00null"
    if isinstance(value, bool):
        # Before int: bool is a subclass of int, and conflating them would make
        # True and 1 indistinguishable.
        return f"b:{'true' if value else 'false'}"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NonCanonicalValueError(f"non-finite Decimal has no canonical form: {value}")
        # Exact digits as supplied: Decimal("1.0") and Decimal("1.00") are
        # different evidence about how the venue reported the value.
        return f"d:{value}"
    if isinstance(value, float):
        raise NonCanonicalValueError(
            f"float {value!r} has no canonical text form that round-trips; decode "
            "monetary and numeric evidence as Decimal or int"
        )
    if isinstance(value, str):
        return f"s:{value}"
    if isinstance(value, bytes):
        # External documents are hashed, never inlined: a fingerprint must stay
        # small, and the raw bytes live in the evidence store.
        return f"h:{hashlib.sha256(value).hexdigest()}"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NonCanonicalValueError(
                f"naive datetime {value!r} is ambiguous; evidence timestamps must be aware"
            )
        return f"t:{value.astimezone(tz=None).isoformat()}"
    if isinstance(value, StrEnum):
        return f"s:{value.value}"
    if isinstance(value, (list, tuple)):
        inner = "\x1f".join(canonical_encoding(item) for item in value)
        return f"l:[{inner}]"
    if isinstance(value, Mapping):
        # Sorted by key so producer iteration order cannot change the digest.
        inner = "\x1f".join(f"{key}\x1e{canonical_encoding(value[key])}" for key in sorted(value))
        return f"m:{{{inner}}}"
    raise NonCanonicalValueError(
        f"{type(value).__name__} has no canonical encoding; add one deliberately "
        "rather than relying on repr()"
    )


def _component_digest(name: str, value: object) -> str:
    payload = f"{name}\x1e{canonical_encoding(value)}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class FingerprintDiff:
    """What changed between two fingerprints, component by component.

    The point of keeping component hashes is that an audit can say *which*
    evidence moved. "The aggregate hash changed" tells a reviewer nothing about
    whether to re-read the contract or just re-approve a cosmetic field.
    """

    unchanged: tuple[str, ...]
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    schema_changed: bool

    @property
    def has_drift(self) -> bool:
        return bool(self.changed or self.added or self.removed) or self.schema_changed

    def describe(self) -> str:
        if not self.has_drift:
            return f"no drift ({len(self.unchanged)} components unchanged)"
        parts = []
        if self.schema_changed:
            parts.append("evidence schema version changed")
        for label, names in (
            ("CHANGED", self.changed),
            ("ADDED", self.added),
            ("REMOVED", self.removed),
        ):
            if names:
                parts.append(f"{label}: {', '.join(names)}")
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class SettlementEvidenceFingerprint:
    """A digest over every required piece of settlement evidence.

    Carries the component hashes as well as the aggregate, so drift can be
    explained rather than merely detected.
    """

    schema_version: str
    components: Mapping[str, str]
    digest: str

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("a fingerprint over no components would make every market identical")
        object.__setattr__(self, "components", dict(sorted(self.components.items())))
        expected = self._aggregate(self.schema_version, self.components)
        if self.digest != expected:
            raise ValueError(
                f"fingerprint digest {self.digest[:12]}... does not match its own "
                f"components ({expected[:12]}...); it was not built by over()"
            )

    @staticmethod
    def _aggregate(schema_version: str, components: Mapping[str, str]) -> str:
        payload = (
            schema_version
            + "\x1d"
            + "\x1d".join(f"{name}\x1e{digest}" for name, digest in sorted(components.items()))
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def over(
        cls,
        components: Mapping[str, object],
        *,
        schema_version: str = EVIDENCE_SCHEMA_VERSION,
    ) -> Self:
        """Fingerprint a mapping of component name to raw evidence value.

        Values may be absent (:data:`ABSENT`), null, scalars, byte content of an
        external document, or nested structures. Ordering of the mapping is
        irrelevant; the digest is over sorted names.
        """
        digests = {name: _component_digest(name, value) for name, value in components.items()}
        return cls(
            schema_version=schema_version,
            components=digests,
            digest=cls._aggregate(schema_version, digests),
        )

    def component(self, name: str) -> str | None:
        return self.components.get(name)

    def matches(self, other: SettlementEvidenceFingerprint | None) -> bool:
        """Whether ``other`` is the same evidence. ``None`` never matches.

        Unavailable evidence is not matching evidence: if the current state
        cannot be established, a certificate must not be treated as current.
        """
        if other is None:
            return False
        return self.schema_version == other.schema_version and self.digest == other.digest

    def diff(self, other: SettlementEvidenceFingerprint) -> FingerprintDiff:
        mine, theirs = set(self.components), set(other.components)
        shared = mine & theirs
        return FingerprintDiff(
            unchanged=tuple(sorted(n for n in shared if self.components[n] == other.components[n])),
            changed=tuple(sorted(n for n in shared if self.components[n] != other.components[n])),
            added=tuple(sorted(theirs - mine)),
            removed=tuple(sorted(mine - theirs)),
            schema_changed=self.schema_version != other.schema_version,
        )

    @property
    def short(self) -> str:
        return self.digest[:12]

    def describe(self) -> str:
        return f"{self.schema_version}:{self.short} ({len(self.components)} components)"


def synthetic_fingerprint(**components: Any) -> SettlementEvidenceFingerprint:
    """Build a fingerprint from arbitrary values, for tests and fixtures.

    Deliberately named so it cannot be mistaken for a fingerprint derived from
    real captured evidence.
    """
    if not components:
        raise ValueError("a synthetic fingerprint still needs at least one component")
    return SettlementEvidenceFingerprint.over(components)
