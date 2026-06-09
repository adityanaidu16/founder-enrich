"""Exa People Search source — uses Exa's 1B-profile people index.

Single sync call per company via /search with category="people". Each result
includes structured person entities with work history, which we verify
client-side against the target company. Replaces the parser's noisier
heuristics with index-grounded matches.

Why People Search over Websets:
  - Synchronous: one call, no polling, ~1-2s per company vs 10-60s.
  - Cheaper per company (~$0.007 vs ~$0.02-0.05 in Websets pricing).
  - Returns structured entities (work history, role) for free.
  - Verification is client-side and explicit ("did this person work at
    the target company?") instead of relying on Websets' opaque rules.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from founder_enrich.discover import DiscoveryResult, Founder

log = logging.getLogger(__name__)

FOUNDER_ROLE_RX = re.compile(
    r"\b(co[-\s]?founder|founder|founding|ceo|chief\s+executive)\b",
    re.IGNORECASE,
)


def discover(domain: str, company: str, api_key: Optional[str]) -> DiscoveryResult:
    """Return verified founders for a company. No-op if api_key is missing,
    so callers don't need to gate on key presence themselves."""
    result = DiscoveryResult()
    if not api_key:
        result.notes.append("exa-skipped:no-key")
        return result
    if not domain or not company:
        result.notes.append("exa-skipped:missing-input")
        return result

    try:
        from exa_py import Exa  # type: ignore
    except ImportError:
        result.notes.append("exa-skipped:sdk-missing")
        return result

    client = Exa(api_key=api_key)
    query = f"founder co-founder {company}"

    try:
        # Plain search (not search_and_contents) — we only need the people
        # entities + title/url. Fetching full page text for every result
        # adds ~10-30s per call without improving extraction quality.
        # num_results capped low: we sort + filter client-side, more results
        # just means more verify work for no upside.
        response = client.search(
            query=query,
            category="people",
            type="auto",
            num_results=10,
        )
    except Exception as e:
        result.notes.append(f"exa-search-failed:{type(e).__name__}")
        return result

    raw_results = getattr(response, "results", None) or []
    matches: List[Founder] = []
    for r in raw_results:
        name = _extract_name(r)
        if not name:
            continue
        role = _extract_role(r)
        if not _verifies_at_company(r, company, domain):
            # Person exists in Exa's index but their work history doesn't
            # mention the target company — likely a false match from a
            # similarly-named org or a fuzzy semantic hit.
            continue
        matches.append(
            Founder(name=name, role=role or "founder", source=f"exa:{_safe_id(r)}")
        )

    if not matches:
        result.notes.append("exa-no-verified-matches")
    else:
        # Founder/co-founder before plain CEO — same ranking convention as
        # the local parser uses.
        matches.sort(key=lambda f: 0 if "founder" in f.role.lower() else 1)
        result.founders = matches
    return result


# ---------- defensive entity extraction ----------
# The /search response schema isn't fully documented for the people category.
# Walk a few likely shapes; tolerate missing fields silently.


def _extract_name(item) -> Optional[str]:
    for path in (
        ("author",),
        ("entities", 0, "name"),
        ("properties", "person", "name"),
        ("properties", "name"),
    ):
        v = _walk(item, path)
        if v and isinstance(v, str) and _looks_like_name(v):
            return v.strip()

    # LinkedIn profile titles: "Jane Doe - Co-Founder at Acme | LinkedIn"
    title = _walk(item, ("title",))
    if title:
        name = _name_from_profile_title(str(title))
        if name:
            return name
    return None


def _extract_role(item) -> str:
    """Pull a role string from entity work history or page text."""
    # Prefer explicit work-history role on the entity if present.
    history = _walk(item, ("entities", 0, "work_history")) or _walk(
        item, ("properties", "person", "work_history")
    )
    if isinstance(history, list) and history:
        for entry in history:
            role = _walk(entry, ("role",)) or _walk(entry, ("title",))
            if role and FOUNDER_ROLE_RX.search(str(role)):
                m = FOUNDER_ROLE_RX.search(str(role))
                return m.group(0) if m else str(role)

    # Fall back to the profile title.
    title = _walk(item, ("title",)) or ""
    m = FOUNDER_ROLE_RX.search(str(title))
    return m.group(0) if m else ""


def _verifies_at_company(item, company: str, domain: str) -> bool:
    """Confirm the person's work history mentions the target company at
    the organization-field level, with WORD-BOUNDARY matching.

    Old version was substring-match on combined text. That fails badly
    when company name is also a common given name: 'Sid' matches 'Sid
    Sijbrandij' (GitLab founder) because his name 'Sid' appears in his
    own LinkedIn page everywhere. Word-boundary + restricting to
    organization-name fields fixes the false-positive class."""
    company_lc = (company or "").lower().strip()
    domain_root = (domain or "").lower().split(".")[0]

    # Build word-boundary regex patterns. Skip very-short needles (<2
    # chars) — too noisy regardless of word boundaries.
    patterns = []
    for needle in {company_lc, domain_root}:
        if needle and len(needle) >= 2:
            patterns.append(re.compile(r"\b" + re.escape(needle) + r"\b", re.IGNORECASE))
    if not patterns:
        return False

    # When structured work_history is populated, it's authoritative: if
    # the target company doesn't appear in any org field (word-bounded),
    # this is a false positive — reject regardless of what other fields say.
    # This is what kills the "Sid Sijbrandij at sid.ai" case: his work
    # history says "GitLab", "Co-op", etc., none of which match \bsid\b.
    history = _walk(item, ("entities", 0, "work_history")) or _walk(
        item, ("properties", "person", "work_history")
    )
    if isinstance(history, list) and history:
        for entry in history:
            for org_field in ("company", "company_name", "organization", "name"):
                org = _walk(entry, (org_field,))
                if org and any(p.search(str(org)) for p in patterns):
                    return True
        return False  # structured data present but no org match → reject

    # No structured work history → trust Exa's ranking. The category="people"
    # search with explicit company name in the query is already highly
    # targeted; over-filtering when we lack structured data costs ~50% of
    # coverage for marginal precision gain.
    return True


def _walk(obj, path):
    cur = obj
    for key in path:
        if cur is None:
            return None
        if isinstance(key, int):
            try:
                cur = cur[key]
            except (TypeError, KeyError, IndexError):
                return None
        elif hasattr(cur, key):
            cur = getattr(cur, key)
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _looks_like_name(s: str) -> bool:
    s = s.strip()
    if len(s) < 4 or len(s) > 60:
        return False
    parts = s.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    return all(p[:1].isupper() for p in parts if p)


def _name_from_profile_title(title: str) -> Optional[str]:
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", title, flags=re.IGNORECASE).strip()
    for sep in (" - ", " – ", " — ", ", "):
        if sep in title:
            candidate = title.split(sep, 1)[0].strip()
            if _looks_like_name(candidate):
                return candidate
    if _looks_like_name(title):
        return title
    return None


def _safe_id(item) -> str:
    for path in (("id",), ("url",)):
        v = _walk(item, path)
        if v:
            return str(v)[:60]
    return "item"
