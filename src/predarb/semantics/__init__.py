"""Propositions, settlement specs and verified relations.

This package is the guardrail between "these markets look related" and "these
markets are provably related". Relations carry a
:class:`predarb.domain.enums.VerificationStatus`; only ``VERIFIED`` relations
may feed a contractual-arbitrage detector.

Kalshi's ``mutually_exclusive`` flag maps to ``AT_MOST_ONE`` and to nothing
else. It never implies exhaustiveness, so it never produces ``EXACTLY_ONE``.
Relations are pinned to the rules hashes they were proven against; when a rules
hash changes the relation is invalidated and returns to review.
"""
