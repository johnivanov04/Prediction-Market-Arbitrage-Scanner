"""predarb -- prediction-market arbitrage and mispricing research.

Phase 1 is Kalshi-only, read-only research tooling. There is no order
submission anywhere in this package, and no dependency that could perform one.

The package is a modular monolith. Dependencies run strictly one way::

    domain  <-  books  <-  detectors  <-  opportunities
       ^          ^            ^
       |          |            |
    venues     ingest      semantics

``domain`` depends on nothing. Detectors depend on books and semantics but know
nothing about venues, so the same detector code runs live and in replay.
"""

__version__ = "0.1.0"
