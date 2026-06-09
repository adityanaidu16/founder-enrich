"""Find founder names + any anchor email at a company domain.

Scrapes public pages on the company's own site (/team, /about, /founders,
...). Best-effort: returns whatever it finds; downstream stages tolerate
gaps and the Exa source fills them when configured.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; FounderEnrich/0.1; +https://github.com/)"
# Aggressive timeout: most sites that respond do so in <2s. Long timeouts
# here used to dominate per-row wall time when sites were slow or dead.
TIMEOUT = 4
TEAM_PATHS = (
    "/team", "/about", "/about-us", "/founders", "/leadership",
    "/people", "/our-team", "/company", "/who-we-are",
)
ROLE_PATTERN = re.compile(
    r"\b(co[-\s]?founder|founder|ceo|chief\s+executive|cto|cpo|coo|"
    r"president|managing\s+director)\b",
    re.IGNORECASE,
)
# Tokens that often appear adjacent to a name but aren't part of it. Trimmed
# off the end before saving, and not allowed mid-name.
ROLE_TOKENS = {
    "founder", "cofounder", "co-founder", "ceo", "cto", "cpo", "coo",
    "president", "former", "current", "chief", "executive", "officer",
    "vp", "head", "managing", "director", "co", "partner", "principal",
    "investor", "advisor", "board",
}
# Capitalized two-token names. Allows "Mary-Anne", "O'Brien". Three-token
# names (incl. middle particles like "de la") are handled separately.
NAME_PATTERN = re.compile(
    r"\b([A-Z][a-zA-Z'’\-]{1,20}\s+[A-Z][a-zA-Z'’\-]{1,20})\b"
)
NAME_WITH_PARTICLE = re.compile(
    r"\b([A-Z][a-zA-Z'’\-]{1,20}\s+(?:de|la|van|von|del|der|du|le|el)\s+"
    r"[A-Z][a-zA-Z'’\-]{1,20})\b"
)
# Two-token capitalized phrases that look like names but are products /
# orgs / publications. Extend as new false positives appear.
ORG_PHRASE_BLOCKLIST = {
    "react router", "indie hackers", "product hunt", "y combinator",
    "hacker news", "sequoia capital", "andreessen horowitz", "general catalyst",
    "tiger global", "founders fund", "first round", "khosla ventures",
    "open ai", "open source", "machine learning", "data science",
    "san francisco", "new york", "los angeles", "united states",
    "north america", "south america", "great britain", "silicon valley",
    "private equity", "public market", "stock market",
}
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Generic mailboxes we should not treat as personal anchors.
GENERIC_LOCALS = {
    "info", "hello", "contact", "support", "sales", "press", "team",
    "hi", "admin", "help", "careers", "jobs", "legal", "privacy",
    "security", "noreply", "no-reply", "marketing", "billing", "partners",
}


@dataclass
class Founder:
    name: str
    role: str = ""
    source: str = ""  # e.g. "site:/team", "exa:<url>"


@dataclass
class DiscoveryResult:
    founders: List[Founder] = field(default_factory=list)
    anchor_emails: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def discover(domain: str) -> DiscoveryResult:
    domain = _clean_domain(domain)
    result = DiscoveryResult()
    if not domain:
        result.notes.append("invalid-domain")
        return result

    _scrape_site(domain, result)

    # Dedupe founders by lowercased name, preserving first occurrence — but
    # sort first so explicit "founder" / "co-founder" labels rank above
    # generic CEO/president matches (which can be hired execs, not founders).
    def _rank(f: Founder) -> int:
        role = (f.role or "").lower().replace("-", "").replace(" ", "")
        if "cofounder" in role or role == "founder":
            return 0
        if "ceo" in role or "president" in role:
            return 1
        return 2
    result.founders.sort(key=_rank)

    seen: Set[str] = set()
    deduped: List[Founder] = []
    for f in result.founders:
        key = f.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(f)
    result.founders = deduped

    # Dedupe anchor emails, drop generics, keep only ones on the company domain.
    result.anchor_emails = _dedupe_anchor_emails(result.anchor_emails, domain)
    return result


def _clean_domain(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = urlparse(raw).netloc or urlparse(raw).path
    raw = raw.split("/")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def _scrape_site(domain: str, result: DiscoveryResult) -> None:
    """Fetch home + a few likely team-style paths. Cheap on time, generous on hits."""
    visited: Set[str] = set()
    for path in ("",) + TEAM_PATHS:
        for scheme in ("https://", "http://"):
            url = f"{scheme}{domain}{path}"
            if url in visited:
                continue
            visited.add(url)
            html = _safe_get(url)
            if html is None:
                continue
            _extract_from_html(html, source=f"site:{path or '/'}", result=result)
            break  # try only one scheme that worked

    # Follow obvious team/about links found on the homepage, in case the site
    # uses non-standard paths (e.g. /our-story).
    home = _safe_get(f"https://{domain}")
    if home:
        for link in _team_links_in(home, base=f"https://{domain}"):
            if link in visited:
                continue
            visited.add(link)
            html = _safe_get(link)
            if html:
                path = urlparse(link).path or "/"
                _extract_from_html(html, source=f"site:{path}", result=result)


def _team_links_in(html: str, base: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[str] = []
    base_host = urlparse(base).netloc
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").strip().lower()
        href = a["href"]
        if any(k in text for k in ("team", "about", "founders", "leadership", "people")):
            full = urljoin(base, href)
            if urlparse(full).netloc == base_host:
                out.append(full.split("#")[0])
    # Dedupe, cap to keep request budget bounded.
    return list(dict.fromkeys(out))[:6]


def _extract_from_html(html: str, source: str, result: DiscoveryResult) -> None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Anchor emails on the domain — scan raw HTML so we catch mailto: hrefs.
    for match in EMAIL_PATTERN.findall(html):
        result.anchor_emails.append(match.lower())

    # Primary: structured extraction. Look for headings whose text is a
    # name, with a role keyword in the immediately-following text. This is
    # how nearly every team/about page is built and produces few false
    # positives.
    found_any = _extract_structured(soup, source, result)

    # Fallback: only run the flat-text scan if structured extraction came
    # up empty. The flat scan catches more but introduces more noise, so we
    # only reach for it when the structured path failed.
    if not found_any:
        _extract_flat(soup, source, result)


def _extract_structured(soup: BeautifulSoup, source: str, result: DiscoveryResult) -> bool:
    """Headings (h1-h5) whose text is a plausible name + a role keyword
    appearing within ~250 chars of following text. Also handles common
    'team member card' shapes where the role sits in a sibling element."""
    found = False
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = (h.get_text(separator=" ", strip=True) or "")
        cleaned = _clean_name(text)
        if not cleaned or not _looks_like_real_name(cleaned):
            continue

        # Collect ~250 chars of context after the heading from following
        # siblings — that's where the role label usually lives.
        context = ""
        node = h
        for _ in range(6):
            node = node.find_next(string=False) if hasattr(node, "find_next") else None
            if node is None:
                break
            piece = node.get_text(separator=" ", strip=True) if hasattr(node, "get_text") else str(node)
            if piece:
                context += " " + piece
            if len(context) > 250:
                break

        role_match = ROLE_PATTERN.search(context)
        if not role_match:
            # Also check the heading's own parent — some cards wrap name +
            # role inside a single container with no following sibling.
            parent = h.parent
            if parent is not None:
                parent_text = parent.get_text(separator=" ", strip=True)
                role_match = ROLE_PATTERN.search(parent_text)
        if role_match:
            result.founders.append(
                Founder(name=cleaned, role=role_match.group(0), source=source)
            )
            found = True
    return found


def _extract_flat(soup: BeautifulSoup, source: str, result: DiscoveryResult) -> None:
    """Last-resort scan over flattened text. Only run when structured
    extraction failed — accepts more noise."""
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        role_match = ROLE_PATTERN.search(line)
        if not role_match:
            continue
        role = role_match.group(0)
        # Only the same line + the immediately preceding line. Adjacent
        # following lines tend to be paragraph copy that mentions
        # unrelated people.
        window = " ".join(lines[max(0, i - 1): i + 1])
        # Order matters: try the longer particle pattern first so we don't
        # truncate "Maria de la Vega" to "Maria de".
        for pat in (NAME_WITH_PARTICLE, NAME_PATTERN):
            for raw in pat.findall(window):
                name = _clean_name(raw)
                if name and _looks_like_real_name(name):
                    result.founders.append(Founder(name=name, role=role, source=source))


def _clean_name(raw: str) -> str:
    """Trim role tokens off the ends and normalize whitespace."""
    name = re.sub(r"\s+", " ", raw).strip()
    parts = name.split()
    while parts and parts[-1].lower().strip(",.") in ROLE_TOKENS:
        parts.pop()
    while parts and parts[0].lower().strip(",.") in ROLE_TOKENS:
        parts.pop(0)
    return " ".join(parts)


def _looks_like_real_name(name: str) -> bool:
    if len(name) < 4 or len(name) > 60:
        return False
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    # Reject role/navigation tokens anywhere in the phrase.
    if any(p.lower().strip(",.") in ROLE_TOKENS for p in parts):
        return False
    bad_tokens = {
        "Privacy", "Policy", "Terms", "Service", "Cookie", "Cookies",
        "All", "Rights", "Reserved", "Sign", "Log", "Get", "Try",
        "Learn", "More", "Read", "About", "Our", "Team", "Contact",
        "Email", "Address", "Toggle", "Menu", "Open", "Close",
        "Blog", "Docs", "Pricing", "Features", "Customers", "Careers",
        "Home", "Page", "Inc", "LLC", "Ltd", "GmbH",
    }
    if any(p in bad_tokens for p in parts):
        return False
    # Reject known org/product phrases.
    if name.lower() in ORG_PHRASE_BLOCKLIST:
        return False
    # Every part must start uppercase and the rest be letters/marks. The
    # regex already enforces this, but flat-text scans can introduce edge cases.
    for p in parts:
        if not p[0].isupper():
            return False
    return True


def _dedupe_anchor_emails(emails: List[str], domain: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for e in emails:
        e = e.lower().strip()
        if not e.endswith("@" + domain):
            continue
        local = e.split("@", 1)[0]
        if local in GENERIC_LOCALS:
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


def _safe_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except requests.RequestException:
        return None
    return None
