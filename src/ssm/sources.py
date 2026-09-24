"""Source trust scoring and quote verification."""
from __future__ import annotations
import io, re
from urllib.parse import urlparse
import httpx
from .config import trusted

UA = {"User-Agent": "Mozilla/5.0 (StorageSpendModeler research bot)"}


def _match(host_path: str, pattern: str) -> bool:
    if pattern.endswith("."):                       # prefix pattern e.g. "investor."
        return host_path.startswith(pattern) or f".{pattern}" in host_path
    if "/" in pattern:                              # path pattern e.g. linkedin.com/jobs
        return pattern in host_path
    host = host_path.split("/")[0]
    return host == pattern or host.endswith("." + pattern)


def classify(url: str, source_type: str, company_domain: str | None = None) -> tuple[int, float, str]:
    """Return (tier, weight, reason). Tier 0 means blocked."""
    cfg = trusted()
    p = urlparse(url)
    host_path = (p.netloc.lower().removeprefix("www.") + p.path.lower())
    if any(_match(host_path, d) for d in cfg["blocked"]["domains"]):
        return 0, 0.0, "blocked domain"
    if company_domain and _match(host_path, company_domain.lower().removeprefix("www.")):
        return 1, cfg["tier1"]["weight"], "company-owned domain"
    for t in (1, 2, 3, 4):
        c = cfg[f"tier{t}"]
        if any(_match(host_path, d) for d in c["domains"]):
            return t, c["weight"], f"domain matches tier{t}"
    for t in (1, 2, 3, 4):   # fall back on declared source type, capped at tier 3
        c = cfg[f"tier{t}"]
        if source_type in c["source_types"]:
            tt = max(t, 3)
            return tt, cfg[f"tier{tt}"]["weight"], f"unlisted domain; type '{source_type}' capped at tier{tt}"
    return 4, cfg["tier4"]["weight"], "unlisted domain"


def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("’", "'").replace("“", '"').replace("”", '"').replace("&nbsp;", " ")
    return re.sub(r"[^a-z0-9$%.]+", " ", s.lower()).strip()


def fetch_text(url: str, timeout: float = 30) -> str:
    with httpx.Client(headers=UA, follow_redirects=True, timeout=timeout) as c:
        r = c.get(url)
        r.raise_for_status()
        if "pdf" in r.headers.get("content-type", "") or url.lower().endswith(".pdf"):
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages)
        return r.text


def verify_quote(url: str, quote: str) -> tuple[bool | None, str]:
    """Fetch the page and check the quote (or >=80% of its 8-word windows) appears.
    Returns None (not False) when the page cannot be fetched, e.g. bot-blocking."""
    try:
        text = _norm(fetch_text(url))
    except Exception as e:  # network / 403 / parse
        return None, f"could not fetch: {type(e).__name__}: {e}"[:300]
    q = _norm(quote)
    if not q:
        return False, "empty quote"
    if q in text:
        return True, "exact quote found"
    words = q.split()
    if len(words) < 8:
        return False, "short quote not found verbatim"
    wins = [" ".join(words[i:i + 8]) for i in range(0, len(words) - 7)]
    hit = sum(w in text for w in wins) / len(wins)
    return (hit >= 0.8), f"{hit:.0%} of quote windows found"
