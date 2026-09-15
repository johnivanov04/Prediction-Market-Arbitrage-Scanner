"""Persistence.

PostgreSQL holds the structured catalogue and state (instruments, propositions,
settlement specs, relations, fee schedules, opportunities) with Alembic
migrations. High-volume raw capture goes to partitioned Parquet, read back for
research with Polars and DuckDB.

All version-bearing tables are bitemporal (``valid_from`` / ``valid_to``) so a
replay can ask "what did we know at time T" rather than "what do we know now".
"""
