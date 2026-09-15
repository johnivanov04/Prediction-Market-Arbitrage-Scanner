"""Venue adapters.

``base`` defines the protocol a venue must satisfy; each venue package
implements it. Normalisation to domain types happens here and nowhere else, so
that venue quirks (dollar-string prices, fixed-point counts, bids-only books)
never leak into detectors.

Phase 1 implements Kalshi only.
"""
