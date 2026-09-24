"""Scan the source tree and the durable dataset for credential-shaped material.

Phase 1's durable dataset should contain public market and research data only.
This checks that claim rather than asserting it, across the places where a leak
would actually land: the repository, the catalogue's textual columns, raw
journals, replay bundles and manifests.

Patterns are deliberately specific. A scan that matched the *word* "key" would
fire on every schema description and teach a reader to ignore it, which is worse
than not scanning.

Exits non-zero on any hit that is not on the allow-list, and prints where.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key_pem": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "auth_header_value": re.compile(
        r"KALSHI-ACCESS-(KEY|SIGNATURE)['\"]?\s*[:=]\s*['\"][^'\"]{8,}", re.I
    ),
    "bearer_token": re.compile(r"\bAuthorization\s*[:=]\s*['\"]?(Bearer|Basic)\s+\S{8,}", re.I),
    "signature_blob": re.compile(r"\bsignature['\"]?\s*[:=]\s*['\"][A-Za-z0-9+/]{60,}={0,2}['\"]"),
    "api_key_id_value": re.compile(
        r"\b(api[_-]?key[_-]?id|key_id)['\"]?\s*[:=]\s*['\"][0-9a-f]{8}-[0-9a-f]{4}", re.I
    ),
    "account_endpoint": re.compile(r"/portfolio/(orders|positions|fills|balance)"),
    "account_identifier": re.compile(
        r"\b(order_id|fill_id|client_order_id|member_id|user_id)['\"]?\s*[:=]"
    ),
    "balance_field": re.compile(r"\b(available_balance|portfolio_value|total_value)\b"),
}

# Files where these strings legitimately appear, allow-listed by path and never
# by pattern -- so a real leak in a new file still fires. Each entry has a
# reason, because an unexplained exclusion is how a scan stops meaning anything.
ALLOWED_PATHS = {
    # Header *names* and the signing rule, documented. No values.
    "docs/api_assumptions.md",
    "docs/kalshi_adapter.md",
    "src/predarb/venues/kalshi/auth.py",
    # A comment explaining that no code path can POST to /portfolio/orders.
    "src/predarb/venues/kalshi/client.py",
    # `client_order_id` is a field name on the public orderbook_delta schema.
    "src/predarb/venues/kalshi/models.py",
    # Signing test vectors, and a deliberately invalid PEM used to assert that a
    # malformed key's contents are NOT echoed into the error message.
    "tests/unit/test_kalshi_auth.py",
    "tests/unit/test_kalshi_client.py",
    # The forbidden-word list that asserts none of this reaches a bundle.
    "tests/integration/test_replay_capture_tool.py",
    "tools/security_scan.py",
}


@dataclass
class Hit:
    where: str
    pattern: str
    excerpt: str


def scan_text(where: str, body: str) -> Iterator[Hit]:
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(body):
            yield Hit(where=where, pattern=name, excerpt=match.group(0)[:70])


def scan_tracked_files() -> list[Hit]:
    listing = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    hits: list[Hit] = []
    for relative in listing:
        if relative in ALLOWED_PATHS:
            continue
        path = ROOT / relative
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits.extend(scan_text(relative, body))
    return hits


def scan_sqlite(database: Path) -> list[Hit]:
    """Every textual column of every table, which is where a payload would land."""
    if not database.exists():
        return []
    hits: list[Hit] = []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            quoted = ", ".join(f'"{c}"' for c in columns)
            for row in connection.execute(f"SELECT {quoted} FROM {table}"):
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, str):
                        hits.extend(scan_text(f"{database.name}:{table}.{column}", value))
    finally:
        connection.close()
    return hits


def scan_directory(directory: Path, suffixes: tuple[str, ...]) -> list[Hit]:
    if not directory.exists():
        return []
    hits: list[Hit] = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        try:
            hits.extend(scan_text(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase-1 credential-shaped scan")
    parser.add_argument("--database", type=Path, default=ROOT / "data/catalogue/phase1.sqlite")
    parser.add_argument("--raw", type=Path, default=ROOT / "data/raw")
    parser.add_argument("--bundles", type=Path, default=ROOT / "data/replay")
    parser.add_argument("--semantics", type=Path, default=ROOT / "data/semantics")
    parser.add_argument("--out", type=Path, default=ROOT / "security_scan.json")
    args = parser.parse_args()

    scans = {
        "tracked_source": scan_tracked_files(),
        "catalogue": scan_sqlite(args.database),
        "raw_journals": scan_directory(args.raw, (".jsonl",)),
        "replay_bundles": scan_directory(args.bundles, (".jsonl", ".json")),
        "semantics_registry": scan_directory(args.semantics, (".json",)),
    }

    total = sum(len(hits) for hits in scans.values())
    payload = {
        "patterns": sorted(PATTERNS),
        "allowed_paths": sorted(ALLOWED_PATHS),
        "results": {
            name: [{"where": h.where, "pattern": h.pattern, "excerpt": h.excerpt} for h in hits]
            for name, hits in scans.items()
        },
        "total_hits": total,
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print("=== CREDENTIAL-SHAPED SCAN ===")
    for name, hits in scans.items():
        print(f"  {name:<20s} {len(hits)} hit(s)")
        for hit in hits[:10]:
            print(f"      {hit.pattern:<20s} {hit.where}  {hit.excerpt!r}")
    print(f"\nWrote {args.out}")
    if total:
        print("\n!! investigate every hit before committing or sharing this dataset")
        raise SystemExit(1)
    print("\nNo credential-shaped material found in source, catalogue, journals or bundles.")


if __name__ == "__main__":
    main()
