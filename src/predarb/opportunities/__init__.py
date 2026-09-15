"""Opportunity records, persistence and audit.

An opportunity record stores everything needed to recompute the claim exactly:
legs, levels consumed, VWAP per leg, fees per leg, payoff in every valid
settlement state, worst-case payoff, capital required, input ages, the relation
and rules-version ids it depended on, and the source message ids for each book.

``audit-opportunity`` renders this; a reviewer should never have to trust a
black box.
"""
