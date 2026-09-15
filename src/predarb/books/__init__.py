"""Order-book reconstruction and executable-depth calculation.

Kalshi publishes **bids only**, on two sides (``yes`` and ``no``). An ask is
therefore always a derived quantity, computed from the opposing bid against the
contract's notional. Derived levels are tagged ``derived=True`` and keep a
reference to the quoted level they came from; the distinction is never lost.

``reconstruction`` owns sequence correctness: snapshot, in-order deltas, gap
detection, duplicate suppression, reconnect and resnapshot. A book whose
sequence integrity is unknown is marked invalid and is not scannable.
"""
