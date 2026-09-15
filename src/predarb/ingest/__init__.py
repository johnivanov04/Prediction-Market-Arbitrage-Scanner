"""Data acquisition and the raw journal.

``raw_journal`` is append-only. Every REST response and websocket message is
written with its provenance (venue, endpoint/channel, sequence, exchange and
local timestamps, payload hash, collector version) before or alongside
normalisation, so that any stored opportunity can be reproduced from source
bytes.

Nothing in this package may mutate a previously written record.
"""
