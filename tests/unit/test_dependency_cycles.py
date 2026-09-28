"""Cycles in the incorporation graph, which are real and are not errors.

The Member Agreement incorporates the Kalshi Rulebook; the Rulebook, in turn,
refers to the Member Agreement. That loop is how the documents are actually
written, so the graph has to traverse it without hanging, produce the same
answer however it was assembled, and still block when a material edge inside
the loop is undischarged. A loop is not a loophole.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from predarb.semantics.dependency import (
    DependencyClosure,
    DependencyDiscovery,
    DependencyGraph,
    DependencyMateriality,
    DependencySet,
    GoverningDocumentDependency,
    PayoutImpact,
    ReferenceResolution,
)
from predarb.semantics.evidence import ExternalDocument, TextExtraction

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
IMPACTS = frozenset(PayoutImpact)


def document(payload: bytes = b"doc") -> ExternalDocument:
    return ExternalDocument.from_bytes(
        url="https://kalshi.test/doc.pdf",
        payload=payload,
        retrieved_at=T0,
        http_status=200,
        content_type="application/pdf",
        extraction=TextExtraction.CLEAN,
        text="text",
    )


def edge(
    parent: str,
    target: str,
    *,
    resolved: bool = True,
    held: bool = True,
    payload: bytes = b"doc",
    reference: str | None = None,
) -> GoverningDocumentDependency:
    return GoverningDocumentDependency(
        parent=parent,
        reference=reference or f"the {target}",
        source_name=target,
        source=document(payload) if held else None,
        payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
        materiality=DependencyMateriality.MATERIAL,
        discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
        reference_resolution=(
            ReferenceResolution.EXACT_CURRENT_REFERENCE if resolved else ReferenceResolution.UNKNOWN
        ),
        resolution_authority="test fixture" if resolved else None,
    )


def graph(*pairs: tuple[str, tuple[GoverningDocumentDependency, ...]]) -> DependencyGraph:
    return DependencyGraph(
        {
            parent: DependencySet(
                parent=parent, closure=DependencyClosure.ENUMERATED, dependencies=deps
            )
            for parent, deps in pairs
        }
    )


class TestTwoNodeCycle:
    """A -> B -> A, the Member Agreement / Rulebook shape."""

    def _graph(self, **kwargs: object) -> DependencyGraph:
        return graph(
            ("A", (edge("A", "B", **kwargs),)),  # type: ignore[arg-type]
            ("B", (edge("B", "A"),)),
        )

    def test_traversal_terminates(self):
        assert len(self._graph().all_dependencies()) == 2

    def test_the_cycle_is_reported(self):
        assert self._graph().cycles() == (("A", "B"),)

    def test_has_cycle_agrees(self):
        assert self._graph().has_cycle()

    def test_a_resolved_cycle_blocks_nothing(self):
        """A loop where every material edge is discharged is fine."""
        assert self._graph().blocking(IMPACTS) == ()

    def test_an_unresolved_edge_inside_the_cycle_still_blocks(self):
        blocking = self._graph(resolved=False).blocking(IMPACTS)
        assert [d.key for d in blocking] == ["A/B/b"]

    def test_an_unheld_source_inside_the_cycle_still_blocks(self):
        assert self._graph(held=False).blocking(IMPACTS) != ()


class TestThreeNodeCycle:
    """A -> B -> C -> A."""

    def _graph(self) -> DependencyGraph:
        return graph(
            ("A", (edge("A", "B"),)),
            ("B", (edge("B", "C"),)),
            ("C", (edge("C", "A"),)),
        )

    def test_traversal_terminates(self):
        assert len(self._graph().all_dependencies()) == 3

    def test_the_cycle_is_reported_once(self):
        assert self._graph().cycles() == (("A", "B", "C"),)

    def test_it_is_reported_in_canonical_rotation(self):
        """Discovered from three entry points, reported as one finding."""
        cycles = self._graph().cycles()
        assert len(cycles) == 1
        assert cycles[0][0] == min(cycles[0])


class TestInsertionOrderIndependence:
    """The same graph built two ways must be indistinguishable."""

    def _pairs(self) -> list[tuple[str, tuple[GoverningDocumentDependency, ...]]]:
        return [
            ("A", (edge("A", "B", reference="Rule 1.1"), edge("A", "C", reference="Rule 2.2"))),
            ("B", (edge("B", "A"),)),
            ("C", (edge("C", "A"),)),
        ]

    def _reversed(self) -> list[tuple[str, tuple[GoverningDocumentDependency, ...]]]:
        pairs = self._pairs()
        flipped = [(parent, tuple(reversed(deps))) for parent, deps in reversed(pairs)]
        return flipped

    def test_closure_is_identical(self):
        first = [d.key for d in graph(*self._pairs()).all_dependencies()]
        second = [d.key for d in graph(*self._reversed()).all_dependencies()]
        assert first == second

    def test_the_fingerprint_is_identical(self):
        assert (
            graph(*self._pairs()).fingerprint_values()
            == graph(*self._reversed()).fingerprint_values()
        )

    def test_the_reported_cycles_are_identical(self):
        assert graph(*self._pairs()).cycles() == graph(*self._reversed()).cycles()

    def test_the_blocking_set_is_identical(self):
        assert [d.key for d in graph(*self._pairs()).blocking(IMPACTS)] == [
            d.key for d in graph(*self._reversed()).blocking(IMPACTS)
        ]

    def test_a_graph_refuses_a_mismatched_key(self):
        """The bug this caught: a mapping that is not what it claims to be."""
        with pytest.raises(ValueError, match="names parent"):
            DependencyGraph({"A": DependencySet(parent="B", closure=DependencyClosure.ENUMERATED)})

    def test_a_set_flattens_in_key_order_whatever_order_it_was_built(self):
        forward = DependencySet(
            parent="A",
            closure=DependencyClosure.ENUMERATED,
            dependencies=(
                edge("A", "B", reference="Rule 1.1"),
                edge("A", "C", reference="Rule 2.2"),
            ),
        )
        backward = DependencySet(
            parent="A",
            closure=DependencyClosure.ENUMERATED,
            dependencies=(
                edge("A", "C", reference="Rule 2.2"),
                edge("A", "B", reference="Rule 1.1"),
            ),
        )
        assert [d.key for d in forward.flatten()] == [d.key for d in backward.flatten()]
        assert forward.fingerprint_values() == backward.fingerprint_values()


class TestDriftInsideACycle:
    def _graph(self, payload: bytes) -> DependencyGraph:
        return graph(
            ("A", (edge("A", "B", payload=payload),)),
            ("B", (edge("B", "A"),)),
        )

    def test_changing_one_source_hash_changes_the_fingerprint(self):
        before = self._graph(b"rulebook v1.29").fingerprint_values()
        after = self._graph(b"rulebook v1.30").fingerprint_values()
        assert before != after

    def test_the_drift_is_deterministic(self):
        assert self._graph(b"x").fingerprint_values() == self._graph(b"x").fingerprint_values()

    def test_only_the_changed_edge_differs(self):
        before = self._graph(b"v1")
        after = self._graph(b"v2")
        changed = {
            key
            for key in before.fingerprint_values()
            if before.fingerprint_values()[key] != after.fingerprint_values().get(key)
        }
        assert all("A/B/" in key for key in changed), changed


class TestSelfLoop:
    def test_a_document_citing_itself_terminates(self):
        loop = graph(("A", (edge("A", "A", reference="Rule 1.1"),)))
        assert len(loop.all_dependencies()) == 1
        assert loop.cycles() == (("A",),)


class TestAcyclicGraphsReportNoCycle:
    def test_a_chain_has_no_cycle(self):
        chain = graph(("A", (edge("A", "B"),)), ("B", (edge("B", "C"),)))
        assert chain.cycles() == ()
        assert not chain.has_cycle()

    def test_a_diamond_has_no_cycle(self):
        diamond = graph(
            ("A", (edge("A", "B", reference="Rule 1.1"), edge("A", "C", reference="Rule 2.2"))),
            ("B", (edge("B", "D"),)),
            ("C", (edge("C", "D"),)),
        )
        assert diamond.cycles() == ()

    def test_a_diamond_expands_each_node_once(self):
        diamond = graph(
            ("A", (edge("A", "B", reference="Rule 1.1"), edge("A", "C", reference="Rule 2.2"))),
            ("B", (edge("B", "D"),)),
            ("C", (edge("C", "D"),)),
        )
        keys = [d.key for d in diamond.all_dependencies()]
        assert len(keys) == len(set(keys))


class TestNestedCycleSafety:
    def test_flatten_deduplicates_a_repeated_citation(self):
        shared = edge("A", "B", reference="Rule 1.1")
        outer = GoverningDocumentDependency(
            parent="A",
            reference="Rule 9.9",
            source_name="C",
            source=document(),
            materiality=DependencyMateriality.MATERIAL,
            payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
            reference_resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
            resolution_authority="test fixture",
            dependencies=(shared,),
        )
        combined = DependencySet(
            parent="A", closure=DependencyClosure.ENUMERATED, dependencies=(shared, outer)
        )
        keys = [d.key for d in combined.flatten()]
        assert keys == sorted(set(keys))
