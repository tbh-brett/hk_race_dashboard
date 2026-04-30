#!/usr/bin/env python3
"""
hkjc_client.py — shared HTTP / parser / utility layer for all HKJC scrapers.

Phase-1 refactor goals:
  * Single fetch_html() with consistent retry/backoff/timeout semantics
  * Shared HEADERS, URL constants, GOING_ABBREV, date converters, regex helpers
  * Shared requests.Session for connection re-use within a single process
  * OneDrive-safe Excel writer (write to %TEMP% then move) — fixes the
    PermissionError class of bugs when xlsx is opened or syncing
  * Backward-compatible: each existing scraper can `from hkjc_client import …`
    without changing CLI shape or output format

NOTE: scrape_hkjc_live_odds.py uses Playwright and is NOT migrated here.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Constants — URLs
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://racing.hkjc.com"

# Local results / dividends / sectional / incident
LOCALRESULTS_URL = f"{BASE_URL}/en-us/local/information/localresults"
RESULTSALL_URL = f"{BASE_URL}/en-us/local/information/resultsall"
SECTIONAL_URL = f"{BASE_URL}/en-us/local/information/displaysectionaltime"
CORUNNING_URL = f"{BASE_URL}/en-us/local/information/corunning"

# Race card / vet / trials
RACECARD_URL = f"{BASE_URL}/en-us/local/information/racecard"
VET_URL = f"{BASE_URL}/en-us/local/information/veterinaryrecord"
BTRESULT_URL = f"{BASE_URL}/en-us/local/information/btresult"

# Horse profile pages (en + zh)
HORSE_URL = f"{BASE_URL}/en-us/local/information/horse"
HORSE_URL_ZH = f"{BASE_URL}/zh-hk/local/information/horse"
OTHERHORSE_URL = f"{BASE_URL}/en-us/local/information/otherhorse"
OTHERHORSE_URL_ZH = f"{BASE_URL}/zh-hk/local/information/otherhorse"

# Static media (running-position photos)
RP_PHOTO_URL_TMPL = (
    BASE_URL
    + "/general/-/media/Sites/JCRW/RaceResult/{season}/{ymd}/{ymd}R{race}_L.jpg"
)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP — User-Agent + headers
# ─────────────────────────────────────────────────────────────────────────────

# Single canonical User-Agent. All scrapers use this unless they override.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS: Dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Referer": BASE_URL + "/",
}


# ─────────────────────────────────────────────────────────────────────────────
# Going abbreviation
# ─────────────────────────────────────────────────────────────────────────────

GOING_ABBREV: Dict[str, str] = {
    "GOOD": "G",
    "GOOD TO FIRM": "GF",
    "GOOD TO YIELDING": "GY",
    "FIRM": "FT",
    "YIELDING": "Y",
    "SOFT": "SE",
    "YIELDING TO SOFT": "YS",
    "WET FAST": "WF",
    "WET SLOW": "WS",
    "HEAVY": "HV",
}


def abbreviate_going(going: str) -> str:
    """Convert full going description (e.g. 'GOOD TO FIRM') to short code ('GF').
    Returns the original string if no mapping is found."""
    if not going:
        return going
    return GOING_ABBREV.get(going.upper().strip(), going)


# ─────────────────────────────────────────────────────────────────────────────
# Date converters
# ─────────────────────────────────────────────────────────────────────────────

DATE_FMT_UI = "%d/%m/%Y"        # HKJC display format
DATE_FMT_QUERY = "%Y/%m/%d"     # HKJC URL query format
DATE_FMT_ISO = "%Y-%m-%d"       # internal storage


def iso_to_ui(date_iso: str) -> str:
    """YYYY-MM-DD → DD/MM/YYYY"""
    return _dt.datetime.strptime(date_iso, DATE_FMT_ISO).strftime(DATE_FMT_UI)


def iso_to_query(date_iso: str) -> str:
    """YYYY-MM-DD → YYYY/MM/DD"""
    return _dt.datetime.strptime(date_iso, DATE_FMT_ISO).strftime(DATE_FMT_QUERY)


def ui_to_iso(date_ui: str) -> str:
    """DD/MM/YYYY → YYYY-MM-DD"""
    return _dt.datetime.strptime(date_ui, DATE_FMT_UI).strftime(DATE_FMT_ISO)


def ui_to_query(date_ui: str) -> str:
    """DD/MM/YYYY → YYYY/MM/DD"""
    return _dt.datetime.strptime(date_ui, DATE_FMT_UI).strftime(DATE_FMT_QUERY)


# ─────────────────────────────────────────────────────────────────────────────
# Parser helpers
# ─────────────────────────────────────────────────────────────────────────────

_HORSE_ID_RE = re.compile(r"horseid=([^&]+)")


def extract_horse_id(href_or_tag: Any) -> Optional[str]:
    """Extract horse ID from an href string OR a BeautifulSoup tag containing
    an <a href='…horseid=HK_2025_L097…'>."""
    if href_or_tag is None:
        return None
    href: Optional[str] = None
    # BeautifulSoup Tag duck-typing — has .find / .get
    if hasattr(href_or_tag, "find") and not isinstance(href_or_tag, str):
        link = href_or_tag.find("a") if href_or_tag.name != "a" else href_or_tag
        if link is not None and hasattr(link, "get"):
            href = link.get("href")
    elif isinstance(href_or_tag, str):
        href = href_or_tag
    if not href:
        return None
    m = _HORSE_ID_RE.search(href)
    return m.group(1) if m else None


def parse_time_seconds(value: str) -> Optional[float]:
    """Parse 'M:SS.ss' or 'SS.ss' to float seconds. Returns None on failure."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if ":" in value:
        try:
            mins, secs = value.split(":", 1)
            return int(mins) * 60 + float(secs)
        except (ValueError, TypeError):
            return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def clean_whitespace(text: str) -> str:
    """Collapse any run of whitespace (incl. nbsp) to a single space and strip."""
    if text is None:
        return ""
    return re.sub(r"[\xa0\s]+", " ", str(text)).strip()


def strip_html_to_text(html: str) -> str:
    """Strip HTML tags and normalise whitespace (no BeautifulSoup needed)."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    return " ".join(text.split())


def split_slash_value(value: str, index: int) -> str:
    """'A / B / C' index 1 → 'B'. Empty string if out of range."""
    parts = [p.strip() for p in (value or "").split("/")]
    return parts[index] if 0 <= index < len(parts) else ""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP client — shared session + fetch_html
# ─────────────────────────────────────────────────────────────────────────────

# Module-level session reused across scrapers within the same Python process.
# Each scraper that wants its own can still pass session=requests.Session().
_GLOBAL_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """Return a process-wide requests.Session with HEADERS pre-applied.

    Re-uses a single TCP/TLS connection pool across all HKJC scrapers in the
    same Python process. Safe to call repeatedly.
    """
    global _GLOBAL_SESSION
    if _GLOBAL_SESSION is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _GLOBAL_SESSION = s
    return _GLOBAL_SESSION


def fetch_html(
    session: Optional[requests.Session],
    url: str,
    params: Optional[Mapping[str, Any]] = None,
    retries: int = 3,
    backoff: float = 1.5,
    timeout: int = 30,
    extra_headers: Optional[Mapping[str, str]] = None,
    quiet: bool = False,
) -> str:
    """GET `url` with retry + exponential-ish backoff. Returns body text or
    empty string on persistent failure (matches existing scraper behaviour).

    `session=None` → use the shared module-level session.
    """
    if session is None:
        session = get_session()
    headers = HEADERS
    if extra_headers:
        headers = {**HEADERS, **dict(extra_headers)}
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as err:
            last_err = err
            if not quiet:
                print(f"  warn: attempt {attempt}/{retries} failed for {url} — {err}",
                      file=sys.stderr)
            if attempt < retries:
                time.sleep(backoff * attempt)
    if not quiet and last_err is not None:
        print(f"  giving up on {url}: {last_err}", file=sys.stderr)
    return ""


def fetch_bytes(
    session: Optional[requests.Session],
    url: str,
    params: Optional[Mapping[str, Any]] = None,
    retries: int = 3,
    backoff: float = 1.5,
    timeout: int = 30,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> Optional[requests.Response]:
    """GET returning the raw Response (so caller can inspect status / content
    type / bytes). None on persistent error. Used for RP photo download."""
    if session is None:
        session = get_session()
    headers = HEADERS
    if extra_headers:
        headers = {**HEADERS, **dict(extra_headers)}
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            return resp
        except Exception as err:
            if attempt < retries:
                time.sleep(backoff * attempt)
            else:
                print(f"  giving up on {url}: {err}", file=sys.stderr)
                return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# OneDrive-safe Excel writer
# ─────────────────────────────────────────────────────────────────────────────

def _is_onedrive_path(p: Path) -> bool:
    """Best-effort heuristic — OneDrive holds file locks that cause
    PermissionError on overwrite. Detect by path containing 'OneDrive'."""
    try:
        return "onedrive" in str(p).lower()
    except Exception:
        return False


def safe_excel_write(
    target_path: Path,
    write_fn,
    *,
    backup: bool = False,
) -> Path:
    """Write an xlsx via `write_fn(tmp_path: Path) -> None` to a TEMP location
    first, then atomically move into `target_path`. Avoids OneDrive
    PermissionError when the destination file is being synced or held open.

    `write_fn` receives a Path inside %TEMP% and is expected to fully write
    the workbook. It is called exactly once.

    Returns the final target path on success.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / f"_hkjc_xlsx_{os.getpid()}_{int(time.time()*1000)}_{target_path.name}"

    try:
        write_fn(tmp_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise

    # Optional backup of the existing target before overwriting
    if backup and target_path.exists():
        bak = target_path.with_suffix(target_path.suffix + ".bak")
        try:
            shutil.copy2(target_path, bak)
        except Exception as e:
            print(f"  warn: could not back up existing {target_path.name}: {e}",
                  file=sys.stderr)

    # Move tmp → target. Retry once on OneDrive PermissionError (file lock).
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            shutil.move(str(tmp_path), str(target_path))
            return target_path
        except PermissionError as e:
            last_err = e
            if attempt == 0 and _is_onedrive_path(target_path):
                # Brief pause for OneDrive to release the lock
                time.sleep(1.0)
                continue
            break
        except Exception as e:
            last_err = e
            break

    # Fallback: leave file in TEMP and tell the user where it is
    print(
        f"\n  ERROR: could not move xlsx into {target_path} ({last_err}).\n"
        f"  Workbook remains at: {tmp_path}\n"
        f"  Close the destination file in Excel/Explorer and copy it manually.",
        file=sys.stderr,
    )
    raise last_err if last_err else RuntimeError("safe_excel_write failed")


# ─────────────────────────────────────────────────────────────────────────────
# Atomic JSON cache I/O
# ─────────────────────────────────────────────────────────────────────────────

import json as _json


def safe_json_write(target_path: Path, data: Any, *, indent: int = 2) -> Path:
    """Write JSON atomically: dump to a sibling .tmp file, then rename. Avoids
    leaving half-written caches behind on Ctrl-C or disk error.

    Use this for any per-meeting JSON cache (racecard, results, dividends).
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=indent, default=str)
    # os.replace is atomic on POSIX and Win when src/dst are on the same volume.
    os.replace(str(tmp_path), str(target_path))
    return target_path


def safe_json_read(target_path: Path) -> Optional[Any]:
    """Read a JSON cache; return None on missing/corrupt file. Tolerates a
    leftover .tmp from a crashed write by ignoring it (caller should rewrite).
    """
    target_path = Path(target_path)
    if not target_path.exists():
        return None
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        print(f"  warn: cache corrupt at {target_path.name}: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot rotation (Bug F)
# ─────────────────────────────────────────────────────────────────────────────

def prune_old_snapshots(
    directory: Path,
    pattern: str,
    keep: int = 10,
    *,
    quiet: bool = False,
) -> int:
    """Keep only the `keep` most recent files matching `pattern` inside
    `directory`. Returns the number of files deleted.

    Used by live-odds scraper to stop the cache/live_odds/YYYYMMDD/ folders
    from accumulating thousands of timestamped snapshots over weeks.
    """
    directory = Path(directory)
    if not directory.exists():
        return 0
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    to_delete = files[keep:]
    deleted = 0
    for f in to_delete:
        try:
            f.unlink()
            deleted += 1
        except OSError as e:
            if not quiet:
                print(f"  warn: could not prune {f.name}: {e}", file=sys.stderr)
    if deleted and not quiet:
        print(f"  pruned {deleted} old snapshot(s) from {directory.name}")
    return deleted


# ─────────────────────────────────────────────────────────────────────────────
# Advisory file lock (Bug D)
# ─────────────────────────────────────────────────────────────────────────────

class FileLock:
    """Cross-platform best-effort advisory lock backed by a sibling .lock file
    that records the holder's PID. Use as a context manager around critical
    sections like cache writes that two scrapers might race on.

    Not a true OS file lock — this is a lightweight pidfile guard. Stale lock
    files (process no longer alive) are reclaimed automatically.

    Usage:
        with FileLock(cache_dir / 'racecard_2026-05-03.json'):
            ...write the cache...
    """
    def __init__(self, target_path: Path, *, timeout: float = 30.0,
                 poll_interval: float = 0.25):
        self.target_path = Path(target_path)
        self.lock_path = self.target_path.with_suffix(self.target_path.suffix + ".lock")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if os.name == "nt":
                # On Windows, opening the process is the simplest liveness probe.
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not h:
                    return False
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            else:
                os.kill(pid, 0)
                return True
        except (OSError, PermissionError):
            return False

    def _try_acquire(self) -> bool:
        # O_EXCL ensures atomic create-only-if-not-exists.
        try:
            fd = os.open(str(self.lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Existing lock — check liveness of holder
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    pid_str = f.read().strip()
                pid = int(pid_str) if pid_str.isdigit() else 0
            except (OSError, ValueError):
                pid = 0
            if pid and self._pid_alive(pid):
                return False
            # Stale — try to reclaim
            try:
                self.lock_path.unlink()
            except OSError:
                return False
            return self._try_acquire()
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            if self._try_acquire():
                self._acquired = True
                return self
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Could not acquire lock {self.lock_path} within "
                    f"{self.timeout}s (held by another scraper?)"
                )
            time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc, tb):
        if self._acquired:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._acquired = False
        return False



__all__ = [
    # URLs
    "BASE_URL", "LOCALRESULTS_URL", "RESULTSALL_URL", "SECTIONAL_URL",
    "CORUNNING_URL", "RACECARD_URL", "VET_URL", "BTRESULT_URL",
    "HORSE_URL", "HORSE_URL_ZH", "OTHERHORSE_URL", "OTHERHORSE_URL_ZH",
    "RP_PHOTO_URL_TMPL",
    # HTTP
    "USER_AGENT", "HEADERS", "get_session", "fetch_html", "fetch_bytes",
    # Going
    "GOING_ABBREV", "abbreviate_going",
    # Dates
    "DATE_FMT_UI", "DATE_FMT_QUERY", "DATE_FMT_ISO",
    "iso_to_ui", "iso_to_query", "ui_to_iso", "ui_to_query",
    # Parsers
    "extract_horse_id", "parse_time_seconds", "clean_whitespace",
    "strip_html_to_text", "split_slash_value",
    # Excel
    "safe_excel_write",
    # JSON cache
    "safe_json_write", "safe_json_read",
    # Snapshot rotation + locks
    "prune_old_snapshots", "FileLock",
]
