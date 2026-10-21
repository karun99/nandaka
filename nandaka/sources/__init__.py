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
import json
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


def _read_meta(path: Path) -> dict:
    """Validator recorded when the partial was written (ETag / total size)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_remote(path: Path, remote: dict) -> None:
    try:
        path.write_text(json.dumps(remote, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                                  # resume stays possible, just unverified


def _resume_verdict(resp, meta: dict) -> tuple[bool, str]:
    """Is it safe to splice our stored prefix onto this response?

    Returns ``(safe, reason_if_not)``. Checked before a single body byte is
    read, so a rejected resume costs one connection and never a wrong file.
    """
    if getattr(resp, "status", None) != 206:
        return False, "server did not honour the Range request"
    total = _content_length(resp)
    if total is None:
        return False, "server did not state the file length"
    expected = meta.get("total")
    if expected is None:
        return False, "no length on record for the partial file"
    if total != expected:
        return False, f"remote length changed ({expected} -> {total})"
    return True, ""


def _open_resumable(url: str, headers: dict, start: int, meta: dict,
                    ppath: Path, res: FetchResult, timeout: float):
    """Open the response, resuming only when the resume is provably safe.

    Returns ``(response, resumed, prefix_bytes)``. On any doubt it re-opens a
    plain full request, so the caller always receives a whole file.
    """
    def opener():
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout)

    def full():
        headers.pop("Range", None)
        headers.pop("If-Range", None)
        return opener()

    if not start:
        return opener(), False, b""
    try:
        resp = opener()
    except urllib.error.HTTPError as exc:
        if exc.code != 416:
            raise
        # Our partial runs past the end of the remote file: it cannot belong
        # to it (file replaced or re-generated since the interrupted pull).
        res.notes.append(
            f"partial file of {start} bytes out of range (HTTP 416); "
            "re-downloading in full")
        return full(), False, b""

    verdict, reason = _resume_verdict(resp, meta)
    prefix = b""
    if verdict:
        try:
            prefix = ppath.read_bytes()
        except OSError:
            verdict, reason = False, "partial file disappeared"
    if verdict:
        return resp, True, prefix
    res.notes.append(
        f"partial file of {start} bytes discarded: {reason}; "
        "re-downloading in full")
    resp.close()
    return full(), False, b""


def _keep_partial(ppath: Path, mpath: Path, data: bytes, remote: dict) -> None:
    """Stash bytes already received so the next run resumes instead of
    starting over (FR-IN-05). The validator travels with them, which is what
    makes the resume provable rather than hopeful."""
    ppath.write_bytes(data)
    _remember_remote(mpath, remote)


def fetch_url(url: str, cfg, *, allow_network: bool = True,
              use_cache: bool = True, max_bytes: int = 64 << 20) -> FetchResult:
    """FR-IN-04: prefer cache; FR-IN-05: resume partial downloads, 1 connection.

    Complete downloads live at *cpath* and are served without touching the
    network. Interrupted ones live at a sibling ``.part`` path, so a partial
    file can never be mistaken for a finished one — and a download cut off by
    ``max_bytes`` is never promoted into the cache (it would otherwise be
    served as a valid, silently-short document for the rest of time).
    """
    res = FetchResult(url=url, ok=False)
    cpath = _cache_path(Path(cfg.cache_dir), url)
    ppath = cpath.with_name(cpath.name + ".part")
    mpath = cpath.with_name(cpath.name + ".part.meta")

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
    start = 0
    meta: dict = {}
    if ppath.exists():
        try:
            start = ppath.stat().st_size          # resume an interrupted pull
        except OSError:
            start = 0
        meta = _read_meta(mpath)
    if start:
        headers["Range"] = f"bytes={start}-"  # resume partial downloads
        # If-Range lets the server answer 200 (whole file) instead of splicing
        # a tail onto a prefix that no longer belongs to it.
        validator = meta.get("etag") or meta.get("last_modified")
        if validator:
            headers["If-Range"] = validator

    req = urllib.request.Request(url, headers=headers)
    remote: dict = {}
    try:
        resp, resumed, prefix = _open_resumable(url, headers, start, meta,
                                                ppath, res, cfg.download_timeout_s)
        with resp:
            remote = {
                "total": _content_length(resp),
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
            }
            chunks: list[bytes] = [prefix] if prefix else []
            total = sum(len(c) for c in chunks)
            dropped: Exception | None = None
            try:
                while True:
                    chunk = resp.read(64 << 10)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        res.notes.append("response truncated at size limit")
                        break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Connection lost mid-transfer: keep what arrived rather than
                # throwing it away — this is the case resume exists for.
                dropped = exc
            res.data = b"".join(chunks)
            res.text = _decode(res.data)
            if dropped is not None:
                res.status = "network-error"
                res.notes.append(
                    f"connection lost after {len(res.data)} bytes: "
                    f"{type(dropped).__name__}")
                _keep_partial(ppath, mpath, res.data, remote)
                res.notes.append(
                    "bytes received are kept as a resumable partial (re-run "
                    "to continue)")
            else:
                res.ok, res.status = True, "resumed" if resumed else "fetched"
                # Integrity check on the assembled file, so a bad splice is
                # never cached as if it were whole (FR-OUT-03).
                expected = remote.get("total")
                if expected is not None and len(res.data) != expected and \
                        not any("truncated" in n for n in res.notes):
                    res.notes.append(
                        f"size mismatch after fetch: got {len(res.data)} "
                        f"bytes, server reports {expected}")
    except urllib.error.HTTPError as exc:
        res.status = f"HTTP {exc.code}"
        res.notes.append(f"{url}: HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        res.status = "network-error"
        res.notes.append(f"{url}: {type(exc).__name__} (offline-first: continuing)")

    if res.ok:
        # Promote to the cache only when the file is complete and verified.
        # A truncated or short download stays a .part so the next run resumes
        # it, rather than being served as a valid document forever after.
        truncated = any("truncated" in n for n in res.notes)
        mismatch = any("size mismatch" in n for n in res.notes)
        complete = not truncated and not mismatch
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            if complete:
                tmp = cpath.with_suffix(".tmp")
                tmp.write_bytes(res.data)
                tmp.replace(cpath)              # atomic: no half-written cache
                ppath.unlink(missing_ok=True)   # resume state no longer needed
                mpath.unlink(missing_ok=True)
            else:
                _keep_partial(ppath, mpath, res.data, remote)
                res.ok = False
                res.notes.append(
                    "download incomplete; kept as a resumable partial and NOT "
                    "cached (re-run to resume)")
        except OSError as exc:
            res.notes.append(f"cache write failed: {exc}")
    return res


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _content_length(resp) -> int | None:
    """Total size of the remote file when the server states it.

    For 206 responses that is the total from Content-Range's "/<total>" part;
    for 200 responses it is the ordinary Content-Length. Absent or unparseable
    headers yield None, meaning "cannot verify".
    """
    crange = resp.headers.get("Content-Range", "")
    if "/" in crange:
        tail = crange.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    length = resp.headers.get("Content-Length")
    if length and str(length).isdigit():
        return int(length)
    return None


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
