"""Point-in-time replay.

The replay engine feeds the raw journal through the same book reconstruction
and the same detector code used live. Its only additional job is to refuse
lookahead: metadata, fee schedules and relations are resolved as of the
simulated timestamp, and future book updates are not visible.

Lookahead avoidance is tested explicitly, not assumed.
"""
