"""Turn a founder name + domain into a ranked list of candidate emails.

Strategy: if we have any verified personal email at the domain (an "anchor"),
infer its pattern and apply that pattern to each founder name. Without an
anchor, fall back to the most common startup patterns ranked by frequency.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

try:
    import dns.resolver  # type: ignore

    _HAS_DNS = True
except ImportError:  # pragma: no cover - optional, falls back gracefully
    _HAS_DNS = False


# Stripped off the front of names before splitting. Without this, "Dr.
# Richard Carback" would split to first="Dr.", last="Richard Carback".
HONORIFICS = {
    "dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
    "prof", "prof.", "sir", "madam", "miss", "rev", "rev.",
}


# Patterns ordered by empirical frequency at small startups (Google Workspace
# heavy). The first one is the most common single bet; the full list is the
# fallback when we have nothing better.
DEFAULT_PATTERNS = (
    "{first}",
    "{first}.{last}",
    "{first}{last}",
    "{f}{last}",
    "{first}{l}",
    "{last}",
)


@dataclass
class EmailCandidate:
    email: str
    pattern: str
    source: str  # "anchor-derived" or "default"


@dataclass
class MxInfo:
    provider: str  # "google", "microsoft", "zoho", "proton", "other", "none"
    hosts: List[str]


def split_name(full_name: str) -> Optional[tuple]:
    """Return (first, last) lowercased ASCII, or None if unparseable."""
    if not full_name:
        return None
    normalized = unicodedata.normalize("NFKD", full_name)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    ascii_only = re.sub(r"[^A-Za-z\s\-']", "", ascii_only)
    parts = [p for p in ascii_only.split() if p]
    # Drop leading honorifics ("Dr.", "Prof.", etc.)
    while parts and parts[0].lower().strip(",.") in HONORIFICS:
        parts.pop(0)
    # Drop common particles when computing the surname.
    particles = {"de", "la", "van", "von", "del", "der", "du", "le", "el"}
    significant = [p for p in parts if p.lower() not in particles]
    if len(significant) < 2:
        return None
    first = significant[0].lower()
    last = significant[-1].lower()
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)
    if not first or not last:
        return None
    return first, last


def lookup_mx(domain: str) -> MxInfo:
    if not _HAS_DNS:
        return MxInfo(provider="unknown", hosts=[])
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        hosts = sorted(
            ((int(r.preference), str(r.exchange).rstrip(".").lower()) for r in answers),
            key=lambda x: x[0],
        )
        host_list = [h for _, h in hosts]
    except Exception:
        return MxInfo(provider="none", hosts=[])

    joined = " ".join(host_list)
    if "google" in joined or "googlemail" in joined or "aspmx" in joined:
        provider = "google"
    elif "outlook" in joined or "office365" in joined or "protection.outlook" in joined:
        provider = "microsoft"
    elif "zoho" in joined:
        provider = "zoho"
    elif "protonmail" in joined or "proton.ch" in joined:
        provider = "proton"
    else:
        provider = "other"
    return MxInfo(provider=provider, hosts=host_list)


def infer_pattern(anchor_email: str, anchor_name: Optional[str]) -> Optional[str]:
    """Given a known personal email and (optionally) its owner's name,
    return a format string like '{first}.{last}'."""
    local = anchor_email.split("@", 1)[0].lower()
    if not anchor_name:
        # Without the owner's name we can still guess based on shape: a dot
        # implies first.last; no dot + len <= 6 implies first or flast.
        if "." in local:
            return "{first}.{last}"
        return "{first}"

    nm = split_name(anchor_name)
    if not nm:
        return None
    first, last = nm
    f, l = first[0], last[0]
    # Test patterns in a fixed order, return the first that reproduces the local.
    for pattern in DEFAULT_PATTERNS + ("{f}.{last}", "{first}.{l}", "{f}_{last}"):
        if pattern.format(first=first, last=last, f=f, l=l) == local:
            return pattern
    return None


def candidates_for(name: str, domain: str, pattern: Optional[str]) -> List[EmailCandidate]:
    nm = split_name(name)
    if not nm:
        return []
    first, last = nm
    f, l = first[0], last[0]

    out: List[EmailCandidate] = []
    seen = set()

    def add(p: str, source: str) -> None:
        try:
            local = p.format(first=first, last=last, f=f, l=l)
        except KeyError:
            return
        email = f"{local}@{domain}"
        if email in seen:
            return
        seen.add(email)
        out.append(EmailCandidate(email=email, pattern=p, source=source))

    if pattern:
        add(pattern, "anchor-derived")
    for p in DEFAULT_PATTERNS:
        add(p, "default")
    return out
