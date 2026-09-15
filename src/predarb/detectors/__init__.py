"""Opportunity detectors.

A detector is a pure function of (book state, verified relations, fee schedule,
clock) to candidate opportunities. Detectors perform no I/O, which is what
allows live scanning and replay to share one implementation.

Phase 1 detectors: ``binary_complement`` and ``group_basket``
(AT_MOST_ONE / AT_LEAST_ONE / EXACTLY_ONE).
"""
