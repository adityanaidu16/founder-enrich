"""Exa People Search source — uses Exa's 1B-profile people index.

Two-stage pattern per company:

  Stage 1: category="company" search for the DOMAIN. Picks the org result
           whose URL matches the target domain; extracts its canonical
           Exa entity ID (e.g. "https://exa.ai/library/organization/xyz").
           This pins down the *exact* company the user means, not just
           "anyone named Bagel" — bagel.com (Bagel Labs at canonical ID
           87skq0yw0by) is distinguished from same-name companies like
           Einstein Bros Bagels or that other "Bagel Labs" with a
           different canonical ID.

  Stage 2: category="people" search for founders, then verify each
           result by checking that one of their work_history entries
           has a `company.id` matching Stage 1's canonical ID. Exact-ID
           match — not name match — kills the entire false-positive
           class where Exa's semantic search pulls in founders of
           similarly-named companies.

Falls back to name+domain word-boundary verification when Stage 1 can't
resolve a canonical ID (rare — new/unindexed startups).
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import List, Optional
from urllib.parse import urlparse

from founder_enrich.discover import DiscoveryResult, Founder

log = logging.getLogger(__name__)


def _safe_search(client, **kwargs):
    """Exa /search with retry-with-backoff for rate limits. Exa's free
    tier has aggressive per-second throttling — without retries, ~25%
    of calls fail silently under modest concurrency."""
    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            return client.search(**kwargs)
        except Exception as e:
            last_err = e
            err = str(e).lower()
            transient = (
                "429" in err or "rate" in err or "throttle" in err
                or "timeout" in err or "503" in err or "502" in err
            )
            if not transient or attempt == 3:
                break
            # Exponential backoff with jitter: 0.5s, 1.5s, 3.5s.
            time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.5))
    if last_err is not None:
        raise last_err
    return None

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

    # Stage 1: resolve the target company's canonical Exa entity from
    # the domain. We use both the ID (strongest match) and the name
    # (handles cases where Exa has multiple IDs for the same canonical
    # company). If this fails entirely, fall through to name-based
    # verification using the user's input company name.
    canonical_id, canonical_name = _canonical_company(client, domain)
    if canonical_id or canonical_name:
        result.notes.append(
            f"exa-canonical:{(canonical_id or '').rsplit('/', 1)[-1] or 'no-id'}/"
            f"{(canonical_name or 'no-name')}"
        )
    else:
        result.notes.append("exa-canonical:none")

    # Stage 2: find candidate people.
    try:
        response = _safe_search(
            client,
            query=f"founder co-founder {company}",
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
        if not _verifies(r, canonical_id, canonical_name, company, domain):
            continue
        role = _extract_role(r)
        matches.append(
            Founder(name=name, role=role or "founder", source=f"exa:{_safe_id(r)}")
        )

    # Removed in v0.3.7: a LinkedIn-search fallback used to run here
    # when canonical verification produced no matches. It returned
    # wrong-company founders for ambiguous names (Bagel, Cinch, etc.)
    # because name-based matching cannot disambiguate "Cinch" the
    # database from "Cinch" the insurance company from "Cinch" the
    # dating app — they all match "Founder at Cinch" titles. Empty is
    # the honest answer when canonical verification can't confirm.

    if not matches:
        result.notes.append("exa-no-verified-matches")
    else:
        # Founder/co-founder before plain CEO — same ranking convention as
        # the local parser uses.
        matches.sort(key=lambda f: 0 if "founder" in f.role.lower() else 1)
        result.founders = matches
    return result


# ---------- Stage 1: canonical company ID lookup ----------


def _canonical_company(client, domain: str):
    """Find the Exa canonical organization for the company at `domain`.
    Returns (id, name) tuple — either may be None if not found. The URL
    host must match the target domain to avoid picking a same-named
    competitor."""
    domain_lc = (domain or "").lower().strip()
    if not domain_lc:
        return None, None
    try:
        resp = _safe_search(
            client,
            query=domain_lc,
            category="company",
            type="auto",
            num_results=5,
        )
    except Exception:
        return None, None

    for r in getattr(resp, "results", None) or []:
        url = (getattr(r, "url", "") or "").lower()
        if not url:
            continue
        host = urlparse(url).netloc.replace("www.", "")
        if host == domain_lc or host.endswith("." + domain_lc):
            for e in (getattr(r, "entities", None) or []):
                eid = getattr(e, "id", None)
                ename = getattr(getattr(e, "properties", None), "name", None)
                if eid or ename:
                    return eid, ename
    return None, None


# ---------- Stage 2: per-person verification ----------


def _verifies(
    item, canonical_id: Optional[str], canonical_name: Optional[str],
    company: str, domain: str,
) -> bool:
    """Strict verification: only accept if the person's work_history links
    to the canonical company entity (by ID or by canonical name prefix).

    No name-based fallback. When Stage 1 returned no canonical (Cinch
    case — Exa doesn't have cinchdb.dev), we cannot disambiguate which
    'Cinch' a candidate works at, so we reject. Empty > wrong founders
    of a same-named different company."""
    history = _work_history(item)
    if not history:
        return False

    # 1. Strongest: canonical ID exact match.
    if canonical_id:
        for entry in history:
            ref = getattr(entry, "company", None)
            ref_id = getattr(ref, "id", None) if ref else None
            if ref_id and ref_id == canonical_id:
                return True

    # 2. Canonical NAME prefix-match — handles Exa's duplicate-ID case
    # where the same canonical company has multiple entity IDs. Prefix
    # only, so "Bagel Labs" canonical matches "Bagel Labs Inc" but
    # NOT "Bagel AI".
    if canonical_name:
        canon_lc = canonical_name.lower().strip()
        for entry in history:
            ref = getattr(entry, "company", None)
            name = getattr(ref, "name", None) if ref else None
            if name and str(name).lower().strip().startswith(canon_lc):
                return True

    return False


def _work_history(item):
    """Return the work_history list (sequence of pydantic models) from
    a search result, or None."""
    entities = getattr(item, "entities", None) or []
    if not entities:
        return None
    props = getattr(entities[0], "properties", None)
    if props is None:
        return None
    return getattr(props, "work_history", None)


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
