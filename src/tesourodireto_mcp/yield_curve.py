"""Current Brazilian yield curve from ANBIMA.

Reproduces ``GetTDData::get_yield_curve()``: the ETTJ (estrutura a termo da
taxa de juros) table is scraped from ANBIMA's website and maturity dates are
derived with the Brazil/ANBIMA business-day calendar.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from importlib.resources import files

import httpx
from bs4 import BeautifulSoup

ANBIMA_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ.asp"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CURVE_TYPES = ("real_return", "nominal_return", "implicit_inflation")
CACHE_TTL_SECONDS = 30 * 60


class YieldCurveError(RuntimeError):
    """Raised when ANBIMA's yield curve cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class YieldCurvePoint:
    n_biz_days: int
    type: str
    value: float
    ref_date: date
    current_date: date


@lru_cache(maxsize=1)
def anbima_holidays() -> frozenset[date]:
    """Load the Brazil/ANBIMA holiday calendar bundled with the package."""
    text = files(__package__).joinpath("anbima_holidays.txt").read_text(encoding="utf-8")
    holidays: set[date] = set()
    for line in text.splitlines():
        line = line.strip()
        if line:
            holidays.add(date.fromisoformat(line))
    return frozenset(holidays)


def is_business_day(day: date, holidays: Iterable[date] | None = None) -> bool:
    calendar = anbima_holidays() if holidays is None else frozenset(holidays)
    return day.weekday() < 5 and day not in calendar


def add_business_days(start: date, offset: int, holidays: Iterable[date] | None = None) -> date:
    """Return the business day ``offset`` days after ``start`` (ANBIMA calendar)."""
    calendar = anbima_holidays() if holidays is None else frozenset(holidays)
    step = 1 if offset >= 0 else -1
    remaining = abs(offset)
    current = start
    while remaining:
        current += timedelta(days=step)
        if current.weekday() < 5 and current not in calendar:
            remaining -= 1
    return current


def _parse_number(raw: str) -> float | None:
    text = raw.strip().replace("\xa0", "")
    if not text:
        return None
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _fetch_html() -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, connect=30.0),
                headers=headers,
            ) as client:
                response = client.get(ANBIMA_URL)
                response.raise_for_status()
                return response.content.decode("latin-1")
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise YieldCurveError(f"Could not reach ANBIMA: {last_error}")


def parse_yield_curve(html: str) -> list[YieldCurvePoint]:
    """Parse an ANBIMA ETTJ page into yield curve points."""
    soup = BeautifulSoup(html, "html.parser")

    params = soup.find(id="Parametros")
    table_node = soup.find(id="ETTJs")
    if params is None or table_node is None:
        raise YieldCurveError(
            "Could not locate the ANBIMA yield curve table; the page layout may have changed."
        )

    header_cell = params.find("th")
    raw_date = header_cell.get_text(" ", strip=True) if header_cell else ""
    match = re.search(r"\d{2}/\d{2}/\d{4}", raw_date)
    if not match:
        raise YieldCurveError(f"Could not parse the reference date from ANBIMA: {raw_date!r}")
    current_date = datetime.strptime(match.group(0), "%d/%m/%Y").date()

    table = table_node.find("table")
    if table is None:
        raise YieldCurveError("ANBIMA page does not contain the ETTJ table.")

    points: list[YieldCurvePoint] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 4:
            continue

        texts = [cell.get_text(" ", strip=True) for cell in cells]
        n_value = _parse_number(texts[0])
        if n_value is None:
            continue
        n_biz_days = round(n_value)

        values = [_parse_number(text) for text in texts[1:]]
        for curve_type, value in zip(CURVE_TYPES, values):
            if value is None:
                continue
            points.append(
                YieldCurvePoint(
                    n_biz_days=n_biz_days,
                    type=curve_type,
                    value=value,
                    ref_date=add_business_days(current_date, n_biz_days),
                    current_date=current_date,
                )
            )

    if not points:
        raise YieldCurveError("ANBIMA returned an empty yield curve.")
    return points


_cache_lock = threading.Lock()
_cache: tuple[float, list[YieldCurvePoint]] | None = None


def fetch_yield_curve(force: bool = False) -> list[YieldCurvePoint]:
    """Fetch the current yield curve, with a short in-memory TTL cache."""
    global _cache

    now = time.time()
    with _cache_lock:
        if not force and _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[1]

    points = parse_yield_curve(_fetch_html())

    with _cache_lock:
        _cache = (now, points)
    return points


def reset_cache() -> None:
    """Clear the yield-curve cache (used by tests)."""
    global _cache
    with _cache_lock:
        _cache = None
