"""Per-row orchestration: discover names → resolve pattern → verify top candidate.

Discovery cascade:
  1. Local cache hit?                                       → return
  2. Site parser (free): /team + /about scrape              → result A
  3. If result A has a founder-role hit, trust it.
     Otherwise, if Exa key present, run Exa People Search   → result B
  4. Cache the merged discovery result (30-day TTL).
  5. Resolve email pattern + SMTP-verify top candidate.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from founder_enrich import cache, discover, exa, resolve, verify

log = logging.getLogger(__name__)

# Confidence levels we expose to the user.
HIGH = "high"
MED = "medium"
LOW = "low"
NONE = "none"


@dataclass
class RowResult:
    company: str
    domain: str
    founder_name: str = ""
    email: str = ""
    confidence: str = NONE
    source: str = ""        # where the name came from
    mx_provider: str = ""
    notes: str = ""
    alternates: List[str] = field(default_factory=list)
    # Other founders/co-founders discovered for the same company. Primary
    # outbound goes to founder_name/email; these are here for power users
    # who want to expand the send.
    all_founders: List[str] = field(default_factory=list)
    all_emails: List[str] = field(default_factory=list)


# Cap on how many secondary founders we keep per company. Most startups have
# 1-3 founders; beyond that we're probably picking up exec-team noise.
MAX_FOUNDERS = 3


def enrich_row(
    company: str,
    domain: str,
    exa_api_key: Optional[str] = None,
    do_smtp: bool = True,
) -> RowResult:
    """Find the best founder email for a single company. Always returns a row
    (with empty fields on failure) so the output CSV stays aligned with input."""
    out = RowResult(company=company, domain=domain)
    clean = discover._clean_domain(domain)
    if not clean:
        out.notes = "invalid-domain"
        return out

    disc = _discover_with_cascade(
        company=company,
        clean_domain=clean,
        exa_api_key=exa_api_key,
    )

    # Clean (strip honorifics) and reject obvious garbage (single-letter
    # first names, company-name-as-person matches like "Plakar Korp").
    cleaned_founders = []
    seen_names: set = set()
    for f in disc.founders:
        cf = _clean_and_validate(f, company, clean)
        if cf is None:
            continue
        # Dedup on normalized name — Exa sometimes returns the same person
        # twice via different URLs (e.g., LinkedIn + their personal site).
        key = re.sub(r"[^a-z]", "", cf.name.lower())
        if key in seen_names:
            continue
        seen_names.add(key)
        cleaned_founders.append(cf)
    disc.founders = cleaned_founders

    if not disc.founders:
        out.notes = "; ".join(disc.notes) or "no-founders-found"
        return out

    mx = resolve.lookup_mx(clean)
    out.mx_provider = mx.provider

    pattern = _infer_pattern(disc.anchor_emails, disc.founders)

    # Compute emails with collision detection: when two founders share a
    # first name (e.g. three Sids at sid.ai), the second one falls through
    # to {first}.{last}@ so we don't emit three rows pointing to the
    # same mailbox.
    used_emails: set = set()
    slotted = []  # list of (Founder, chosen_candidate, all_candidates)
    for f in disc.founders[:MAX_FOUNDERS]:
        cands = resolve.candidates_for(f.name, clean, pattern)
        chosen = next((c for c in cands if c.email not in used_emails), None)
        if chosen is None:
            continue
        used_emails.add(chosen.email)
        slotted.append((f, chosen, cands))

    if not slotted:
        out.notes = "; ".join(disc.notes) or "no-emails-derivable"
        return out

    primary_founder, primary_chosen, primary_cands = slotted[0]
    secondary_founders = [f.name for f, _, _ in slotted[1:]]
    secondary_emails = [c.email for _, c, _ in slotted[1:]]

    row = RowResult(
        company=company,
        domain=domain,
        founder_name=primary_founder.name,
        email=primary_chosen.email,
        source=primary_founder.source,
        mx_provider=mx.provider,
        alternates=[c.email for c in primary_cands],
        all_founders=secondary_founders,
        all_emails=secondary_emails,
    )

    if do_smtp:
        v = verify.verify(primary_chosen.email, mx.hosts)
        row.notes = v.reason
        if v.deliverable is True and not v.catch_all:
            row.confidence = HIGH
        elif v.catch_all and pattern:
            row.confidence = MED
        elif v.catch_all:
            row.confidence = LOW
        elif v.deliverable is False:
            row.confidence = NONE
            for alt in primary_cands[1:]:
                if alt.email in used_emails - {primary_chosen.email}:
                    continue  # skip patterns already used by other slots
                v2 = verify.verify(alt.email, mx.hosts)
                if v2.deliverable is True and not v2.catch_all:
                    row.email = alt.email
                    row.confidence = HIGH
                    row.notes = v2.reason
                    break
        else:
            row.confidence = MED if pattern else LOW
    else:
        row.confidence = MED if pattern else LOW
        row.notes = "smtp-skipped"

    # Exa-sourced founders are pre-verified against work history, so
    # treat them as at least medium even when SMTP can't confirm.
    if primary_founder.source.startswith("exa:") and row.confidence == LOW:
        row.confidence = MED

    return row


# Honorifics + corporate suffixes used by the validator below. Kept here
# rather than in resolve.py because the validator needs the full set for
# rejection decisions, not just the lowercased email-derivation set.
_HONORIFICS_ALL = {
    "dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
    "prof", "prof.", "sir", "madam", "miss", "rev", "rev.",
}
_COMPANY_SUFFIXES = {
    "corp", "korp", "inc", "llc", "ltd", "labs", "lab",
    "gmbh", "ag", "co", "company", "holdings",
}
# Common English nouns that surface as fake "last names" when Exa returns
# product/account pages instead of real people (e.g. "Zo Computer" at
# morphllm). Real surnames almost never collide with these.
_NON_SURNAME_WORDS = {
    "computer", "server", "system", "systems", "solutions", "software",
    "cloud", "platform", "network", "service", "services", "tech",
    "technology", "technologies", "research", "studio", "studios",
    "agency", "team", "support", "engineering", "operations", "security",
}


def _clean_and_validate(
    f: discover.Founder, company: str, clean_domain: str
) -> Optional[discover.Founder]:
    """Strip honorifics, reject garbage names. Returns a new Founder with
    cleaned name, or None to drop the candidate entirely."""
    name = (f.name or "").strip()
    if not name:
        return None
    parts = name.split()

    # Strip leading honorifics ("Dr. Richard Carback" → "Richard Carback").
    while parts and parts[0].lower().strip(",.") in _HONORIFICS_ALL:
        parts.pop(0)
    if len(parts) < 2:
        return None

    first = parts[0]
    # Reject single-letter first names — these are LinkedIn profiles that
    # display as initials only ("S Si", "M K"). The resulting "s@" email
    # is almost never valid.
    if len(re.sub(r"[^A-Za-z]", "", first)) < 2:
        return None

    # Reject company-name-as-person matches ("Plakar Korp", "Foo Inc"):
    # if the domain root appears as a token AND another token is a known
    # corporate suffix, it's the company branding, not a real person.
    parts_lc = [p.lower().strip(",.") for p in parts]
    domain_root = (clean_domain or "").split(".")[0]
    if domain_root in parts_lc and any(p in _COMPANY_SUFFIXES for p in parts_lc):
        return None

    # Reject exact name = company match (cleaned of punctuation).
    cleaned_alpha = re.sub(r"[^a-z]", "", " ".join(parts_lc))
    cleaned_company = re.sub(r"[^a-z]", "", (company or "").lower())
    if cleaned_alpha and (cleaned_alpha == cleaned_company or cleaned_alpha == domain_root):
        return None

    # Reject if EITHER first name OR last name equals the company/domain
    # token. Catches "Treena Lynch" at treena.app, "Nicola Vigio" at
    # vigio.io, "Mother Duck" at zettafleet, etc. — Exa sometimes returns
    # results that look person-shaped but are actually the company's
    # brand/mascot/eponymous reference.
    first_lc, last_lc = parts_lc[0], parts_lc[-1]
    for token_lc in (first_lc, last_lc):
        if token_lc and (token_lc == cleaned_company or token_lc == domain_root):
            return None

    # Reject if the "last name" is a common English noun ("Computer",
    # "Solutions"). These leak in when Exa returns a product or team
    # page that was parsed as a person.
    if last_lc in _NON_SURNAME_WORDS:
        return None

    cleaned_name = " ".join(parts)
    return discover.Founder(name=cleaned_name, role=f.role, source=f.source)


def enrich_rows(
    rows: List[Dict[str, str]],
    company_col: str = "company",
    domain_col: str = "domain",
    exa_api_key: Optional[str] = None,
    do_smtp: bool = True,
    max_workers: int = 5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[RowResult]:
    """Process rows in parallel. Returns results in input order."""
    total = len(rows)
    out: List[Optional[RowResult]] = [None] * total
    completed = 0

    def work(i: int, row: Dict[str, str]) -> tuple:
        company = (row.get(company_col) or "").strip()
        domain = (row.get(domain_col) or "").strip()
        if not domain:
            return i, RowResult(company=company, domain=domain, notes="missing-domain")
        return i, enrich_row(
            company, domain,
            exa_api_key=exa_api_key,
            do_smtp=do_smtp,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(work, i, r) for i, r in enumerate(rows)]
        for fut in as_completed(futures):
            i, result = fut.result()
            out[i] = result
            completed += 1
            if on_progress:
                on_progress(completed, total, result.domain)
    return [r for r in out if r is not None]


# ---------- discovery cascade ----------


def _discover_with_cascade(
    company: str,
    clean_domain: str,
    exa_api_key: Optional[str],
) -> discover.DiscoveryResult:
    cached = cache.get(clean_domain)
    if cached is not None:
        return cached

    parser_result = discover.discover(clean_domain)

    if _has_founder_role(parser_result.founders):
        # Local parser produced a high-signal hit; skip the paid call.
        cache.set(clean_domain, parser_result)
        return parser_result

    if not exa_api_key:
        cache.set(clean_domain, parser_result)
        return parser_result

    exa_result = exa.discover(clean_domain, company, exa_api_key)
    merged = _merge(parser_result, exa_result)
    cache.set(clean_domain, merged)
    return merged


def _has_founder_role(founders: List[discover.Founder]) -> bool:
    for f in founders:
        role = (f.role or "").lower().replace("-", "").replace(" ", "")
        if "cofounder" in role or role == "founder":
            return True
    return False


def _merge(
    parser: discover.DiscoveryResult, exa_res: discover.DiscoveryResult
) -> discover.DiscoveryResult:
    """Exa-verified founders take precedence; parser founders are kept as
    fallbacks. Anchor emails from both sources combine (Exa rarely returns
    them, but parser anchors are essential for pattern inference)."""
    merged = discover.DiscoveryResult(
        founders=list(exa_res.founders),
        anchor_emails=list(parser.anchor_emails) + list(exa_res.anchor_emails),
        notes=list(parser.notes) + list(exa_res.notes),
    )
    seen = {f.name.lower().strip() for f in merged.founders}
    for f in parser.founders:
        if f.name.lower().strip() not in seen:
            merged.founders.append(f)
    return merged


def _rank(conf: str) -> int:
    return {HIGH: 3, MED: 2, LOW: 1, NONE: 0}.get(conf, 0)


def split_full_name(full: str):
    """Return (first, last) preserving the original case. Multi-word last
    names (e.g. 'de la Vega', 'Romera Paredes') are kept intact in the last
    field — outbound personalization usually only needs the first name to be
    right, so we err toward keeping the surname unsplit."""
    if not full:
        return "", ""
    parts = full.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _infer_pattern(anchor_emails: List[str], founders: List[discover.Founder]) -> Optional[str]:
    for email in anchor_emails:
        for founder in founders:
            p = resolve.infer_pattern(email, founder.name)
            if p:
                return p
    if anchor_emails:
        return resolve.infer_pattern(anchor_emails[0], None)
    return None
