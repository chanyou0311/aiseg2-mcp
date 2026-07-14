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
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from . import parsers
from .client import AisegClient
from .models import CostHistoryPage, HistoryPage, HistorySeriesPoint

# The fetch sentinel: its mtime marks when the cache was last populated (drives disk-level TTL).
_SENTINEL = ".fetched"

# (kind, granularity) -> how to name the covering CSV(s) and read available tokens back for hints.
# ``fixed`` files span all time; ``token`` files are one-per-period keyed by a date token.
_HISTORY_UNIT = "Wh"
_COST_UNIT = "JPY"
_COST_SCALE = 0.001  # cost CSVs are in 0.001 JPY -> multiply to get JPY


def default_cache_dir() -> str:
    """The default cache directory: ``<tempdir>/aiseg2-mcp-cache``."""
    return str(Path(tempfile.gettempdir()) / "aiseg2-mcp-cache")


def _months(start: str, end: str) -> list[str]:
    """Yield YYYYMM tokens from start..end (inclusive). start/end are YYYY-MM or YYYY-MM-DD."""
    sy, sm = int(start[0:4]), int(start[5:7])
    ey, em = int(end[0:4]), int(end[5:7])
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _days(start: str, end: str) -> list[str]:
    """Yield YYYYMMDD tokens from start..end (inclusive). start/end are YYYY-MM-DD."""
    sd = date(int(start[0:4]), int(start[5:7]), int(start[8:10]))
    ed = date(int(end[0:4]), int(end[5:7]), int(end[8:10]))
    out: list[str] = []
    d = sd
    while d <= ed:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def _years(start: str, end: str) -> list[str]:
    """Yield YYYY tokens from start..end (inclusive)."""
    return [f"{y:04d}" for y in range(int(start[0:4]), int(end[0:4]) + 1)]


# For each (kind, granularity): a function start,end -> covering basenames, and a regex to read the
# date token out of an on-disk filename (for the "available range" hint). ``None`` regex = the file
# spans all time (a single fixed file).
def _history_files(granularity: str, start: str, end: str) -> list[str]:
    if granularity == "30min":
        return [f"30minhistory_rc_{t}.csv" for t in _days(start, end)]
    if granularity == "hour":
        return [f"hourhistory_rc_{t}.csv" for t in _days(start, end)]
    if granularity == "day":
        return [f"dayhistory_rc_{t}.csv" for t in _months(start, end)]
    if granularity == "month":
        return [f"monthhistory_rc_{t}.csv" for t in _years(start, end)]
    if granularity == "year":
        return ["yearhistory_total_rc.csv"]
    raise ToolError(f"unknown granularity: {granularity!r}")


def _cost_files(granularity: str, start: str, end: str) -> list[str]:
    if granularity == "day":
        return [f"daycost_rc_{t}.csv" for t in _months(start, end)]
    if granularity == "month":
        return [f"monthcost_rc_{t}.csv" for t in _years(start, end)]
    if granularity == "year":
        return ["yearcost_total_rc.csv"]
    raise ToolError(f"unknown cost granularity: {granularity!r} (use day/month/year)")


# Regex to extract the date token from an on-disk filename, per granularity, for the range hint.
_TOKEN_RE = {
    ("history", "30min"): re.compile(r"^30minhistory_rc_(\d{8})\.csv$"),
    ("history", "hour"): re.compile(r"^hourhistory_rc_(\d{8})\.csv$"),
    ("history", "day"): re.compile(r"^dayhistory_rc_(\d{6})\.csv$"),
    ("history", "month"): re.compile(r"^monthhistory_rc_(\d{4})\.csv$"),
    ("cost", "day"): re.compile(r"^daycost_rc_(\d{6})\.csv$"),
    ("cost", "month"): re.compile(r"^monthcost_rc_(\d{4})\.csv$"),
}


class HistoryStore:
    """Download-once, cache, and slice the AiSEG2 SD-card history export (shared by both tools)."""

    def __init__(self, client: AisegClient, cache_dir: str | None = None, ttl: int = 3600) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir or default_cache_dir())
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._fetched_at: float | None = None
        # In-memory parsed-CSV cache (basename -> HistoryCsv), cleared on each fresh download.
        self._parsed: dict[str, parsers.HistoryCsv] = {}

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
                self._fetched_at = self._sentinel_path().stat().st_mtime
                self._parsed.clear()
                return
            await self._download_and_extract()
            self._fetched_at = time.time()
            self._parsed.clear()

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

    async def _read_csv(self, basename: str) -> parsers.HistoryCsv | None:
        await self._ensure_fresh()
        if basename in self._parsed:
            return self._parsed[basename]
        path = self._cache_dir / basename
        if not path.exists():
            return None
        parsed = parsers.parse_history_csv(path.read_bytes())
        self._parsed[basename] = parsed
        return parsed

    # --- range hint ----------------------------------------------------------------------------

    def _available_hint(self, kind: str, granularity: str) -> str:
        regex = _TOKEN_RE.get((kind, granularity))
        if regex is None:
            return "no data files are present in the export"
        tokens = sorted(
            m.group(1) for f in self._cache_dir.glob("*.csv") if (m := regex.match(f.name))
        )
        if not tokens:
            return f"no {granularity} {kind} files are present in the export"
        return f"available {granularity} range: {tokens[0]}..{tokens[-1]}"

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
    ) -> HistoryPage:
        points = await self._collect(
            "history", _history_files(granularity, start, end), granularity, start, end,
            metrics, circuits, scale=1.0,
        )
        page, has_more, next_offset = _paginate(points, limit, offset)
        return HistoryPage(
            granularity=granularity,
            unit=_HISTORY_UNIT,
            series=page,
            has_more=has_more,
            total_rows=len(points),
            next_offset=next_offset,
        )

    async def get_cost_history(
        self,
        granularity: str,
        start: str,
        end: str,
        limit: int,
        offset: int,
    ) -> CostHistoryPage:
        points = await self._collect(
            "cost", _cost_files(granularity, start, end), granularity, start, end,
            metrics=None, circuits=None, scale=_COST_SCALE,
        )
        page, has_more, next_offset = _paginate(points, limit, offset)
        return CostHistoryPage(
            granularity=granularity,
            unit=_COST_UNIT,
            series=page,
            has_more=has_more,
            total_rows=len(points),
            next_offset=next_offset,
        )

    async def _collect(
        self,
        kind: str,
        basenames: list[str],
        granularity: str,
        start: str,
        end: str,
        metrics: list[str] | None,
        circuits: list[str] | None,
        scale: float,
    ) -> list[HistorySeriesPoint]:
        found = 0
        points: list[HistorySeriesPoint] = []
        for basename in basenames:
            parsed = await self._read_csv(basename)
            if parsed is None:
                continue
            found += 1
            points.extend(
                parsers.history_points(parsed, granularity, start, end, metrics, circuits, scale)
            )
        if found == 0:
            raise ToolError(
                f"no {granularity} {kind} data covers {start}..{end}; "
                + self._available_hint(kind, granularity)
            )
        points.sort(key=lambda p: p.timestamp)
        return points


def _paginate(
    points: list[HistorySeriesPoint], limit: int, offset: int
) -> tuple[list[HistorySeriesPoint], bool, int | None]:
    """Slice a sorted point list into a page and compute has_more / next_offset."""
    window = points[offset : offset + limit]
    has_more = offset + limit < len(points)
    return window, has_more, (offset + limit if has_more else None)
