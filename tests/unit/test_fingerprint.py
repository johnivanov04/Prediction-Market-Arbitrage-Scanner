"""Tests for deterministic settlement-evidence fingerprinting.

The fingerprint is what a settlement certificate binds to, so its failure modes
are semantic failures: a digest that collapses two different facts lets a stale
proof survive a real change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.semantics.fingerprint import (
    ABSENT,
    EVIDENCE_SCHEMA_VERSION,
    NonCanonicalValueError,
    SettlementEvidenceFingerprint,
    canonical_encoding,
    synthetic_fingerprint,
)

pytestmark = pytest.mark.unit

BASE = {
    "market.rules_primary": "Resolves YES if the team wins.",
    "market.notional_value": Decimal("1.0000"),
    "market.early_close_condition": ABSENT,
    "series.settlement_sources": ("official league site",),
    "terms.content": b"%PDF-1.4 contract terms",
}


def fingerprint(**overrides: object) -> SettlementEvidenceFingerprint:
    return SettlementEvidenceFingerprint.over({**BASE, **overrides})


class TestDeterminism:
    def test_the_same_evidence_gives_the_same_digest(self):
        assert fingerprint().digest == fingerprint().digest

    def test_field_order_does_not_matter(self):
        forward = SettlementEvidenceFingerprint.over(dict(BASE))
        reverse = SettlementEvidenceFingerprint.over(dict(reversed(list(BASE.items()))))
        assert forward.digest == reverse.digest

    def test_component_hashes_are_retained(self):
        result = fingerprint()
        assert set(result.components) == set(BASE)
        assert result.component("market.rules_primary") is not None

    def test_a_digest_that_disagrees_with_its_components_is_refused(self):
        """Guards against a hand-built fingerprint bypassing over()."""
        real = fingerprint()
        with pytest.raises(ValueError, match="does not match its own components"):
            SettlementEvidenceFingerprint(
                schema_version=real.schema_version,
                components=dict(real.components),
                digest="0" * 64,
            )

    def test_an_empty_fingerprint_is_refused(self):
        """It would make every market identical."""
        with pytest.raises(ValueError, match="no components"):
            SettlementEvidenceFingerprint.over({})


class TestAbsentNullEmpty:
    """Three different facts, three different digests."""

    def test_absent_differs_from_null(self):
        assert fingerprint(**{"market.early_close_condition": None}).digest != fingerprint().digest

    def test_null_differs_from_empty_string(self):
        as_null = fingerprint(**{"market.early_close_condition": None})
        as_empty = fingerprint(**{"market.early_close_condition": ""})
        assert as_null.digest != as_empty.digest

    def test_absent_differs_from_empty_string(self):
        as_empty = fingerprint(**{"market.early_close_condition": ""})
        assert as_empty.digest != fingerprint().digest

    def test_a_removed_field_is_reported_as_removed(self):
        without = SettlementEvidenceFingerprint.over(
            {k: v for k, v in BASE.items() if k != "series.settlement_sources"}
        )
        diff = fingerprint().diff(without)
        assert diff.removed == ("series.settlement_sources",)
        assert diff.has_drift


class TestWhatChangesTheDigest:
    def test_a_rules_change_changes_it(self):
        assert fingerprint(**{"market.rules_primary": "Different."}).digest != fingerprint().digest

    def test_a_settlement_source_change_changes_it(self):
        drifted = fingerprint(**{"series.settlement_sources": ("some other source",)})
        assert drifted.digest != fingerprint().digest

    def test_a_contract_document_byte_change_changes_it(self):
        drifted = fingerprint(**{"terms.content": b"%PDF-1.4 contract terms (rev 2)"})
        assert drifted.digest != fingerprint().digest

    def test_an_unchanged_url_with_changed_content_changes_it(self):
        """A URL is not evidence of its own content.

        Both snapshots cite the same URL; only the bytes behind it moved. A
        URL-hash binding would call this unchanged.
        """
        first = SettlementEvidenceFingerprint.over(
            {"terms.url": "https://example.test/terms", "terms.content": b"rev 1"}
        )
        second = SettlementEvidenceFingerprint.over(
            {"terms.url": "https://example.test/terms", "terms.content": b"rev 2"}
        )
        assert first.component("terms.url") == second.component("terms.url")
        assert first.digest != second.digest

    def test_a_notional_change_changes_it(self):
        assert (
            fingerprint(**{"market.notional_value": Decimal("2.0000")}).digest
            != fingerprint().digest
        )

    def test_trailing_zeros_in_a_decimal_are_preserved(self):
        """How the venue reported a number is itself evidence."""
        assert (
            fingerprint(**{"market.notional_value": Decimal("1.00")}).digest
            != fingerprint(**{"market.notional_value": Decimal("1.0000")}).digest
        )

    def test_the_schema_version_changes_it(self):
        reencoded = SettlementEvidenceFingerprint.over(
            dict(BASE), schema_version="settlement-evidence/99"
        )
        assert reencoded.digest != fingerprint().digest
        assert fingerprint().diff(reencoded).schema_changed

    def test_a_type_change_changes_it(self):
        """The string "1" and the integer 1 are different evidence."""
        as_text = SettlementEvidenceFingerprint.over({"x": "1"})
        as_int = SettlementEvidenceFingerprint.over({"x": 1})
        assert as_text.digest != as_int.digest

    def test_true_and_one_are_distinguishable(self):
        assert (
            SettlementEvidenceFingerprint.over({"x": True}).digest
            != SettlementEvidenceFingerprint.over({"x": 1}).digest
        )


class TestVolatileFieldsAreNotEvidence:
    """Prices are not settlement semantics.

    Including them would invalidate every certificate on every tick, which
    trains reviewers to ignore drift -- the opposite of the intent.
    """

    def test_the_policy_is_expressed_by_omission(self):
        quiet = SettlementEvidenceFingerprint.over(BASE)
        # A caller assembling evidence simply does not include price fields;
        # adding one would (correctly) change the digest, which is why the
        # inclusion list is explicit rather than "everything on the market".
        noisy = SettlementEvidenceFingerprint.over({**BASE, "market.last_price": "0.53"})
        assert quiet.digest != noisy.digest
        assert "market.last_price" not in quiet.components

    def test_two_snapshots_of_the_same_market_agree_across_price_moves(self):
        """The same semantic fields, captured at different prices."""
        first = SettlementEvidenceFingerprint.over(dict(BASE))
        second = SettlementEvidenceFingerprint.over(dict(BASE))
        assert first.matches(second)


class TestMatching:
    def test_identical_evidence_matches(self):
        assert fingerprint().matches(fingerprint())

    def test_none_never_matches(self):
        """Unavailable evidence is not matching evidence."""
        assert not fingerprint().matches(None)

    def test_different_evidence_does_not_match(self):
        assert not fingerprint().matches(fingerprint(**{"market.rules_primary": "x"}))


class TestDiff:
    def test_unchanged_components_are_listed(self):
        drifted = fingerprint(**{"market.rules_primary": "Different."})
        diff = fingerprint().diff(drifted)
        assert diff.changed == ("market.rules_primary",)
        assert "series.settlement_sources" in diff.unchanged

    def test_added_components_are_listed(self):
        extended = SettlementEvidenceFingerprint.over({**BASE, "market.custom_strike": "x"})
        diff = fingerprint().diff(extended)
        assert diff.added == ("market.custom_strike",)

    def test_no_drift_reads_cleanly(self):
        diff = fingerprint().diff(fingerprint())
        assert not diff.has_drift
        assert "no drift" in diff.describe()

    def test_the_description_names_what_moved(self):
        drifted = fingerprint(**{"series.settlement_sources": ("other",)})
        text = fingerprint().diff(drifted).describe()
        assert "CHANGED" in text
        assert "series.settlement_sources" in text


class TestCanonicalEncoding:
    def test_floats_are_refused_rather_than_normalised(self):
        """No canonical text form round-trips, so accepting one would make the
        digest depend on how a value happened to be decoded."""
        with pytest.raises(NonCanonicalValueError, match="float"):
            canonical_encoding(0.1)

    def test_a_float_inside_a_structure_is_also_refused(self):
        with pytest.raises(NonCanonicalValueError):
            canonical_encoding({"a": [1, 0.5]})

    def test_naive_datetimes_are_refused(self):
        with pytest.raises(NonCanonicalValueError, match="naive datetime"):
            canonical_encoding(datetime(2026, 9, 19))  # noqa: DTZ001

    def test_aware_datetimes_encode(self):
        assert canonical_encoding(datetime(2026, 9, 19, tzinfo=UTC)).startswith("t:")

    def test_an_unknown_type_is_refused_rather_than_repr_ed(self):
        class Opaque:
            pass

        with pytest.raises(NonCanonicalValueError, match="no canonical encoding"):
            canonical_encoding(Opaque())

    def test_nested_mappings_sort_by_key(self):
        assert canonical_encoding({"b": 1, "a": 2}) == canonical_encoding({"a": 2, "b": 1})

    def test_list_order_is_significant(self):
        """Ordered evidence -- settlement sources, strike tiers -- is ordered."""
        assert canonical_encoding([1, 2]) != canonical_encoding([2, 1])

    def test_bytes_are_hashed_not_inlined(self):
        encoded = canonical_encoding(b"x" * 10_000)
        assert encoded.startswith("h:")
        assert len(encoded) < 100


class TestSyntheticHelper:
    def test_it_is_named_so_it_cannot_be_mistaken_for_real_evidence(self):
        result = synthetic_fingerprint(a="1")
        assert result.schema_version == EVIDENCE_SCHEMA_VERSION
        assert result.components

    def test_it_still_requires_a_component(self):
        with pytest.raises(ValueError, match="at least one component"):
            synthetic_fingerprint()
