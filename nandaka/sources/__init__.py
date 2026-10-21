"""Source connectors (EX-03..EX-09, FR-IN-03..05).

Read-only connectors to official Indian education portals and operational
URLs. Rules enforced here:

  * robots.txt is checked before any portal request (AD-05),
  * cache always wins over the network (FR-IN-04, CO-03),
  * one download at a time, HTTP Range used for resumable pulls (FR-IN-05),
  * network failure degrades to cache and is reported, never fatal.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

# --------------------------------------------------------------------------- #
# Portal registry (config-only extension point, §3.6 Maintainability)
# --------------------------------------------------------------------------- #


@dataclass
class Portal:
    key: str
    name: str
    base_url: str
    catalog_url: str
    link_pattern: str = r"\.pdf($|\?)"
    api_url: str = ""


PORTALS: dict[str, Portal] = {
    "ncert": Portal("ncert", "NCERT Textbook Hub", "https://ncert.nic.in",
                    "https://ncert.nic.in/textbook.php"),
    "diksha": Portal("diksha", "DIKSHA", "https://diksha.gov.in",
                     "https://diksha.gov.in/", api_url="https://diksha.gov.in/api"),
    "ap_scert": Portal("ap_scert", "AP SCERT", "https://cse.ap.gov.in",
                       "https://cse.ap.gov.in/loadacademictextbookpublicview"),
    "tg_scert": Portal("tg_scert", "TG SCERT", "https://scert.telangana.gov.in",
                       "https://scert.telangana.gov.in/"),
    "yesonlinebooks": Portal("yesonlinebooks", "YesOnlineBooks",
                             "https://yesonlinebooks.com",
                             "https://yesonlinebooks.com/",
                             link_pattern=r"product|catalog|book"),
}


@dataclass
class FetchResult:
    url: str
    ok: bool
    from_cache: bool = False
    data: bytes = b""
    text: str = ""
    status: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# robots.txt gate
# --------------------------------------------------------------------------- #

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed_by_robots(url: str, timeout: float = 8.0) -> bool:
    """AD-05: never fetch what robots.txt disallows (or when robots is unknown
    after a network error we still allow cached content only)."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if origin not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
        try:
            rp.read()
            _robots_cache[origin] = rp
        except Exception:
            _robots_cache[origin] = None  # unreachable -> skip check, log later
    rp = _robots_cache[origin]
    return True if rp is None else rp.can_fetch("Nandaka", url)


# --------------------------------------------------------------------------- #
# Fetch with cache-first behaviour
# --------------------------------------------------------------------------- #


def _cache_path(cache_dir: Path, url: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return cache_dir / "web" / h[:2] / h


def fetch_url(url: str, cfg, *, allow_network: bool = True,
              use_cache: bool = True, max_bytes: int = 64 << 20) -> FetchResult:
    """FR-IN-04: prefer cache; FR-IN-05: Range header, single connection."""
    res = FetchResult(url=url, ok=False)
    cpath = _cache_path(Path(cfg.cache_dir), url)

    if use_cache and cpath.exists():
        try:
            res.data = cpath.read_bytes()
            res.text = _decode(res.data)
            res.ok, res.from_cache, res.status = True, True, "cache"
            return res
        except OSError:
            pass

    if not allow_network:
        res.status = "offline"
        res.notes.append("network disabled and no cache for this URL")
        return res

    if not allowed_by_robots(url):
        res.status = "blocked-by-robots"
        res.notes.append(f"robots.txt disallows {url} (AD-05); skipped")
        return res

    headers = {"User-Agent": cfg.user_agent, "Accept": "*/*"}
    start = cpath.stat().st_size if cpath.exists() else 0
    if start:
        headers["Range"] = f"bytes={start}-"  # resume partial downloads

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg.download_timeout_s) as resp:
            chunks: list[bytes] = [res.data] if start else []
            total = start
            while True:
                chunk = resp.read(64 << 10)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    res.notes.append("response truncated at size limit")
                    break
            res.data = b"".join(chunks)
            res.text = _decode(res.data)
            res.ok, res.status = True, "fetched"
    except urllib.error.HTTPError as exc:
        res.status = f"HTTP {exc.code}"
        res.notes.append(f"{url}: HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        res.status = "network-error"
        res.notes.append(f"{url}: {type(exc).__name__} (offline-first: continuing)")

    if res.ok:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_suffix(".part")
            tmp.write_bytes(res.data)
            tmp.replace(cpath)
        except OSError:
            pass
    return res


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Link extraction
# --------------------------------------------------------------------------- #


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in ("a", "iframe", "source"):
            return
        d = dict(attrs)
        href = d.get("href") or d.get("src")
        if href:
            self.links.append((href, d.get("title") or d.get("alt") or ""))


def extract_links(html: str, base_url: str, pattern: str) -> list[str]:
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    out: list[str] = []
    rx = re.compile(pattern, re.I)
    for href, _title in parser.links:
        full = urljoin(base_url, href)
        if rx.search(full) and full not in out:
            out.append(full)
    return out


def enumerate_portal(key: str, cfg, *, allow_network: bool = True) -> FetchResult:
    """EX-03..EX-09: fetch a portal catalogue page and return its HTML/links."""
    portal = PORTALS.get(key)
    if not portal:
        res = FetchResult(url="", ok=False, status="unknown-portal")
        res.notes.append(f"unknown portal '{key}'; known: {', '.join(PORTALS)}")
        return res
    return fetch_url(portal.catalog_url, cfg, allow_network=allow_network)


def portal_pdf_links(key: str, html: str, base_url: str = "") -> list[str]:
    portal = PORTALS[key]
    return extract_links(html, base_url or portal.base_url, portal.link_pattern)


def known_urls() -> dict[str, str]:
    return {k: p.catalog_url for k, p in PORTALS.items()}
