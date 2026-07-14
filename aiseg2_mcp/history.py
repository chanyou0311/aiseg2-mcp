"""SD-card long-term history: download the AiSEG2 zip export once, cache it, and serve slices.

The AiSEG2 exports its long-term history as a single zip of per-period CSVs (30-minute / hourly /
daily / monthly / yearly, plus cost variants). Downloading it is slow and heavy on the device, so
``HistoryStore`` fetches it at most once per TTL (serialised by an ``asyncio.Lock``), extracts the
CSVs to a cache directory (surviving a restart within the TTL), and parses on demand. The tools
(get_history / get_cost_history) share one store so a single download backs both.

Everything here is read-only: the export is a data download, and only ``.csv`` members are kept
(``.conf`` / ``.ver`` / ``.ini`` / ``.dat`` settings files are ignored).
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import time
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from . import parsers
from .client import AisegClient
from .models import HistorySeriesPoint, SeriesPage

# The fetch sentinel: its mtime marks when the cache was last populated (drives disk-level TTL).
_SENTINEL = ".fetched"

_HISTORY_UNIT = "Wh"
_COST_UNIT = "JPY"
_COST_SCALE = 0.001  # cost CSVs are in 0.001 JPY -> multiply to get JPY

# Cap on the in-memory parsed-CSV cache (30min/hour files are one-per-day, so this bounds memory).
_PARSED_CACHE_MAX = 512


def default_cache_dir() -> str:
    """The default cache directory: ``<tempdir>/aiseg2-mcp-cache``."""
    return str(Path(tempfile.gettempdir()) / "aiseg2-mcp-cache")


# --- token enumeration (period -> file-name tokens) --------------------------------------------


def _months(start: str, end: str) -> list[str]:
    """Yield YYYYMM tokens from start..end (inclusive). start/end are YYYY-MM or YYYY-MM-DD."""
    ey, em = int(end[0:4]), int(end[5:7])
    out: list[str] = []
    y, m = int(start[0:4]), int(start[5:7])
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _days(start: str, end: str) -> list[str]:
    """Yield YYYYMMDD tokens from start..end (inclusive). start/end are YYYY-MM-DD."""
    d = date(int(start[0:4]), int(start[5:7]), int(start[8:10]))
    ed = date(int(end[0:4]), int(end[5:7]), int(end[8:10]))
    out: list[str] = []
    while d <= ed:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def _years(start: str, end: str) -> list[str]:
    """Yield YYYY tokens from start..end (inclusive)."""
    return [f"{y:04d}" for y in range(int(start[0:4]), int(end[0:4]) + 1)]


# A token type: which enumerator expands a range into file tokens, and the token's digit count
# (used to build the range-hint regex).
@dataclass(frozen=True)
class _Token:
    enumerate_tokens: Callable[[str, str], list[str]]
    digits: int


_TOKENS: dict[str, _Token] = {
    "day": _Token(_days, 8),
    "month": _Token(_months, 6),
    "year": _Token(_years, 4),
}


# The single source of truth for file naming. ``prefix`` + token + ".csv" names a per-period file;
# ``token=None`` means one fixed file spanning all time (``prefix`` is then the full basename stem).
@dataclass(frozen=True)
class _FileSpec:
    prefix: str
    token: str | None  # key into _TOKENS, or None for a single fixed file


_FILESPECS: dict[tuple[str, str], _FileSpec] = {
    ("history", "30min"): _FileSpec("30minhistory_rc_", "day"),
    ("history", "hour"): _FileSpec("hourhistory_rc_", "day"),
    ("history", "day"): _FileSpec("dayhistory_rc_", "month"),
    ("history", "month"): _FileSpec("monthhistory_rc_", "year"),
    ("history", "year"): _FileSpec("yearhistory_total_rc", None),
    ("cost", "day"): _FileSpec("daycost_rc_", "month"),
    ("cost", "month"): _FileSpec("monthcost_rc_", "year"),
    ("cost", "year"): _FileSpec("yearcost_total_rc", None),
}


def _spec(kind: str, granularity: str) -> _FileSpec:
    spec = _FILESPECS.get((kind, granularity))
    if spec is None:
        valid = "/".join(sorted(g for k, g in _FILESPECS if k == kind))
        raise ToolError(f"unsupported {kind} granularity: {granularity!r} (use {valid})")
    return spec


def _covering_files(spec: _FileSpec, start: str, end: str) -> list[str]:
    """The CSV basenames that cover [start, end] for a spec."""
    if spec.token is None:
        return [f"{spec.prefix}.csv"]
    return [f"{spec.prefix}{tok}.csv" for tok in _TOKENS[spec.token].enumerate_tokens(start, end)]


def _available_hint(spec: _FileSpec, cache_dir: Path) -> str:
    """A hint naming the actually-available data for a spec (used when a range has no files)."""
    if spec.token is None:
        return f"{spec.prefix}.csv is not present in the export"
    regex = re.compile(rf"^{re.escape(spec.prefix)}(\d{{{_TOKENS[spec.token].digits}}})\.csv$")
    tokens = sorted(m.group(1) for f in cache_dir.glob("*.csv") if (m := regex.match(f.name)))
    if not tokens:
        return "no matching files are present in the export"
    return f"available range: {tokens[0]}..{tokens[-1]}"


def _page(
    granularity: str, unit: str, points: list[HistorySeriesPoint], limit: int, offset: int
) -> SeriesPage:
    """Slice a sorted point list into a SeriesPage and compute has_more / next_offset."""
    window = points[offset : offset + limit]
    has_more = offset + limit < len(points)
    return SeriesPage(
        granularity=granularity,
        unit=unit,
        series=window,
        has_more=has_more,
        total_rows=len(points),
        next_offset=(offset + limit if has_more else None),
    )


class HistoryStore:
    """Download-once, cache, and slice the AiSEG2 SD-card history export (shared by both tools)."""

    def __init__(self, client: AisegClient, cache_dir: str | None = None, ttl: int = 3600) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir or default_cache_dir())
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._fetched_at: float | None = None
        # In-memory parsed-CSV LRU (basename -> HistoryCsv), cleared on each fresh download.
        self._parsed: OrderedDict[str, parsers.HistoryCsv] = OrderedDict()
        # Memo of the most recent query's sorted points, to skip re-collection on offset-only calls.
        self._memo_key: tuple | None = None
        self._memo_points: list[HistorySeriesPoint] = []
        self._memo_fetched_at: float | None = None

    # --- cache lifecycle -----------------------------------------------------------------------

    def _sentinel_path(self) -> Path:
        return self._cache_dir / _SENTINEL

    def _disk_fresh(self, now: float) -> bool:
        s = self._sentinel_path()
        return s.exists() and now - s.stat().st_mtime < self._ttl

    def _mem_fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self._ttl

    async def _ensure_fresh(self) -> None:
        now = time.time()
        if self._mem_fresh(now):
            return
        async with self._lock:
            now = time.time()
            if self._mem_fresh(now):  # another coroutine refreshed while we waited on the lock
                return
            if self._disk_fresh(now):  # a previous run's extraction is still within TTL
                self._adopt(self._sentinel_path().stat().st_mtime)
                return
            await self._download_and_extract()
            self._adopt(time.time())

    def _adopt(self, fetched_at: float) -> None:
        """Mark the cache fresh as of ``fetched_at`` and drop stale parsed/memo state."""
        self._fetched_at = fetched_at
        self._parsed.clear()
        self._memo_key = None
        self._memo_points = []
        self._memo_fetched_at = None

    async def _download_and_extract(self) -> None:
        zip_bytes = await self._client.download_history_zip()
        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            raise ToolError(
                "AiSEG2 history export was not a valid zip (device busy or SD card missing?); "
                "retry shortly"
            ) from exc
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # Drop stale CSVs so a shrinking export never leaves orphans behind.
        for old in self._cache_dir.glob("*.csv"):
            old.unlink()
        for member in archive.namelist():
            name = os.path.basename(member)
            if name.endswith(".csv"):
                (self._cache_dir / name).write_bytes(archive.read(member))
        self._sentinel_path().write_bytes(b"")

    def _read_csv(self, basename: str) -> parsers.HistoryCsv | None:
        """Read + parse a cached CSV (LRU-memoised). Assumes the cache is already fresh."""
        cached = self._parsed.get(basename)
        if cached is not None:
            self._parsed.move_to_end(basename)
            return cached
        path = self._cache_dir / basename
        if not path.exists():
            return None
        parsed = parsers.parse_history_csv(path.read_bytes())
        self._parsed[basename] = parsed
        if len(self._parsed) > _PARSED_CACHE_MAX:
            self._parsed.popitem(last=False)
        return parsed

    # --- public queries ------------------------------------------------------------------------

    async def get_history(
        self,
        granularity: str,
        start: str,
        end: str,
        metrics: list[str] | None,
        circuits: list[str] | None,
        limit: int,
        offset: int,
    ) -> SeriesPage:
        points = await self._points("history", granularity, start, end, metrics, circuits, 1.0)
        return _page(granularity, _HISTORY_UNIT, points, limit, offset)

    async def get_cost_history(
        self, granularity: str, start: str, end: str, limit: int, offset: int
    ) -> SeriesPage:
        points = await self._points("cost", granularity, start, end, None, None, _COST_SCALE)
        return _page(granularity, _COST_UNIT, points, limit, offset)

    async def _points(
        self,
        kind: str,
        granularity: str,
        start: str,
        end: str,
        metrics: list[str] | None,
        circuits: list[str] | None,
        scale: float,
    ) -> list[HistorySeriesPoint]:
        spec = _spec(kind, granularity)
        await self._ensure_fresh()
        key = (kind, granularity, start, end, tuple(metrics or ()), tuple(circuits or ()))
        if self._memo_key == key and self._memo_fetched_at == self._fetched_at:
            return self._memo_points  # same query, cache unchanged -> reuse (offset-only paging)
        points = self._collect(spec, granularity, start, end, metrics, circuits, scale)
        self._memo_key, self._memo_points, self._memo_fetched_at = key, points, self._fetched_at
        return points

    def _collect(
        self,
        spec: _FileSpec,
        granularity: str,
        start: str,
        end: str,
        metrics: list[str] | None,
        circuits: list[str] | None,
        scale: float,
    ) -> list[HistorySeriesPoint]:
        found = 0
        points: list[HistorySeriesPoint] = []
        for basename in _covering_files(spec, start, end):
            parsed = self._read_csv(basename)
            if parsed is None:
                continue
            found += 1
            points.extend(
                parsers.history_points(parsed, granularity, start, end, metrics, circuits, scale)
            )
        if found == 0:
            raise ToolError(
                f"no data covers {start}..{end}; " + _available_hint(spec, self._cache_dir)
            )
        points.sort(key=lambda p: p.timestamp)
        return points
