"""Local SQLite cache for (domain → discovery result), per-teammate.

Lives in ~/Library/Application Support/FounderEnrich/cache.db. Never synced,
never published — so the *set of domains a teammate has queried* never
leaves the machine, even though the underlying source data is public.

30-day TTL: long enough that re-running the same prospect list across weeks
is effectively free, short enough that founders changing companies don't
stick around forever.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict
from typing import Optional

from founder_enrich.discover import DiscoveryResult, Founder

TTL_SECONDS = 30 * 24 * 3600

# Bump when discovery/validation semantics change. Entries written under
# an older value are treated as misses on read, so users automatically
# re-fetch with the new logic instead of holding stale wrong-founder
# results across upgrades.
#   v1: pre-versioning (no "schema" field in stored JSON)
#   v2: v0.3.5 — canonical-ID verification + strict LinkedIn-fallback
#       pattern. Invalidates v1 entries that had loose name-based matches
#       like the wrong-bagel-company case.
#   v3: v0.3.6 — fallback gated on Stage 1 failing. Invalidates v2
#       entries that may have shipped wrong-company founders for short
#       common-word domains (Bagel) where the strict pattern still
#       matched unrelated same-name companies.
#   v4: v0.3.7 — LinkedIn fallback removed entirely. Even Stage-1-failed
#       cases (Cinch / cinchdb.dev) were returning founders of unrelated
#       same-name companies via the fallback. Strict-only now: every
#       returned founder is canonical-ID/name verified.
SCHEMA_VERSION = 4


def _db_path() -> str:
    d = os.path.expanduser("~/Library/Application Support/FounderEnrich")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "cache.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        "domain TEXT PRIMARY KEY, "
        "result_json TEXT NOT NULL, "
        "cached_at INTEGER NOT NULL"
        ")"
    )
    return conn


def get(domain: str) -> Optional[DiscoveryResult]:
    if not domain:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT result_json, cached_at FROM cache WHERE domain = ?",
                (domain.lower(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    result_json, cached_at = row
    if time.time() - cached_at > TTL_SECONDS:
        return None
    try:
        data = json.loads(result_json)
    except ValueError:
        return None
    # Schema version check: entries from older versions had different
    # discovery semantics (e.g. loose Bagel matches) — invalidate so
    # the caller re-fetches with current logic.
    if data.get("schema") != SCHEMA_VERSION:
        return None
    return DiscoveryResult(
        founders=[Founder(**f) for f in data.get("founders", [])],
        anchor_emails=list(data.get("anchor_emails", [])),
        notes=list(data.get("notes", [])),
    )


def set(domain: str, result: DiscoveryResult) -> None:
    if not domain or not result.founders:
        # Don't cache empty results — let the next run try afresh.
        return
    payload = json.dumps({
        "schema": SCHEMA_VERSION,
        "founders": [asdict(f) for f in result.founders],
        "anchor_emails": result.anchor_emails,
        "notes": result.notes,
    })
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (domain, result_json, cached_at) "
                "VALUES (?, ?, ?)",
                (domain.lower(), payload, int(time.time())),
            )
    except sqlite3.Error:
        pass


def clear() -> int:
    """Wipe the cache. Returns number of rows removed."""
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM cache")
            return cur.rowcount
    except sqlite3.Error:
        return 0
