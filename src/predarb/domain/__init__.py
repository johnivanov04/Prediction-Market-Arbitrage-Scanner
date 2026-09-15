"""Venue-independent core types.

This package must not import from ``venues``, ``ingest`` or ``storage``. It is
the vocabulary everything else is written in.

Implemented in Phase 1 task 1: :mod:`predarb.domain.money`,
:mod:`predarb.domain.enums`. Planned: ``models`` (canonical event, proposition,
settlement spec, venue instrument, relation) and ``payoff`` (settlement-state
enumeration and worst-case payoff).
"""
