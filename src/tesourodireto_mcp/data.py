"""Download, cache and parse Tesouro Direto data.

Data comes from the official Tesouro Transparente CKAN open data portal, the
same source used by ``GetTDData::td_get2()``. The raw file is validated before
it replaces a cached copy, and downloads are retried a few times.
"""

from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

import httpx

from .catalog import titles_for_groups

DEFAULT_CSV_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/"
    "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
    "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv"
)
DATASET_PAGE = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/"
    "taxas-dos-titulos-ofertados-pelo-tesouro-direto/"
)
SOURCE_NAME = "Tesouro Transparente (CKAN)"
CSV_FILENAME = "precotaxatesourodireto.csv"
MIN_FIRST_YEAR = 2005

REQUIRED_COLUMNS = (
    "tipo_titulo",
    "data_base",
    "data_vencimento",
    "taxa_compra_manha",
    "pu_compra_manha",
)

# Raw ``tipo_titulo`` -> short bond symbol. Mirrors ``parse_td_csv()`` in GetTDData.
BOND_SYMBOLS = {
    "Tesouro Prefixado": "LTN",
    "Tesouro Selic": "LFT",
    "Tesouro IPCA+ com Juros Semestrais": "NTN-B",
    "Tesouro IPCA+": "NTN-B Principal",
    "Tesouro Prefixado com Juros Semestrais": "NTN-F",
    "Tesouro IGPM+ com Juros Semestrais": "NTN-C",
    "Tesouro IGP-M com Juros Semestrais": "NTN-C",
    "Tesouro Educa+": "NTN-B1",
    "Tesouro Renda+ Aposentadoria Extra": "NTN-B1",
    "Tesouro RendA+ Aposentadoria Extra": "NTN-B1",
}

USER_AGENT = (
    "tesourodireto-mcp/0.1.0 "
    "(+https://github.com/msperlin/tesourodireto-mcp)"
)


class DataError(RuntimeError):
    """Raised when the Tesouro Direto data cannot be obtained or parsed."""


class BondRecord(NamedTuple):
    ref_date: date
    yield_bid: float
    price_bid: float
    asset_code: str
    matur_date: date
    tipo_titulo: str


@dataclass(frozen=True, slots=True)
class AggregatedRecord:
    period: str
    asset_code: str
    matur_date: date
    n_obs: int
    first_date: date
    last_date: date
    yield_bid_mean: float
    price_bid_mean: float


@dataclass(slots=True)
class Dataset:
    """Parsed dataset plus provenance metadata."""

    records: list[BondRecord]
    source: str
    csv_path: Path
    file_date: date
    retrieved_at: datetime
    stale: bool

    @property
    def latest_ref_date(self) -> date | None:
        return max((row.ref_date for row in self.records), default=None)

    def as_meta(self) -> dict[str, object]:
        return {
            "source": self.source,
            "csv_path": str(self.csv_path),
            "data_file_date": self.file_date.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(timespec="seconds"),
            "stale": self.stale,
            "latest_ref_date": (
                self.latest_ref_date.isoformat() if self.latest_ref_date else None
            ),
        }


# ---------------------------------------------------------------------------
# paths and cache


def cache_dir() -> Path:
    """Return the cache directory (override with ``TESOURO_DIRETO_CACHE_DIR``)."""
    env = os.environ.get("TESOURO_DIRETO_CACHE_DIR")
    if env:
        path = Path(env).expanduser()
    else:
        path = Path.home() / ".cache" / "tesourodireto-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _csv_url() -> str:
    return os.environ.get("TESOURO_DIRETO_CSV_URL", DEFAULT_CSV_URL).strip()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _normalize_column(name: str) -> str:
    return re.sub(r"[ ._]+", "_", name.strip().lstrip("\ufeff")).lower()


def normalize_header(line: str) -> list[str]:
    """Return normalized column names from the CSV header line."""
    return [_normalize_column(item) for item in line.split(";")]


def is_valid_csv_bytes(content: bytes) -> bool:
    """Check that a payload looks like the Tesouro Direto CSV file."""
    if len(content) < 100:
        return False
    first_line = content.splitlines()[0].decode("utf-8-sig", errors="replace")
    columns = set(normalize_header(first_line))
    return all(column in columns for column in REQUIRED_COLUMNS)


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    # Brazilian format: dot as thousands separator, comma as decimal separator.
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# HTTP


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,text/plain,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }


def _http_get(url: str, *, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(120.0, connect=30.0),
                headers=_headers(),
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    raise DataError(f"Failed to download {url}: {last_error}")


def _resolve_csv_url_from_page() -> str | None:
    """Scrape the CKAN dataset page for the official price CSV link."""
    try:
        html = _http_get(DATASET_PAGE, attempts=1).decode("utf-8", errors="replace")
    except DataError:
        return None

    links = re.findall(r"href=[\"']([^\"']+\.csv[^\"']*)[\"']", html, flags=re.IGNORECASE)
    if not links:
        return None

    preferred = [link for link in links if "precotaxatesourodireto" in link.lower()]
    return (preferred or links)[0]


def _download_csv(destination: Path) -> str:
    """Download and validate the CSV, returning the URL that worked."""
    configured = _csv_url()
    candidates = [configured]

    if configured == DEFAULT_CSV_URL:
        fallback = _resolve_csv_url_from_page()
        if fallback and fallback not in candidates:
            candidates.append(fallback)

    errors: list[str] = []
    for url in candidates:
        try:
            local_path = Path(url).expanduser()
            if local_path.is_file():
                content = local_path.read_bytes()
            else:
                content = _http_get(url)
        except (DataError, OSError) as exc:
            errors.append(str(exc))
            continue

        if not is_valid_csv_bytes(content):
            errors.append(f"{url} did not return a valid Tesouro Direto CSV")
            continue

        tmp_path = destination.with_suffix(destination.suffix + ".download")
        tmp_path.write_bytes(content)
        tmp_path.replace(destination)
        return url

    raise DataError(
        "Could not download valid Tesouro Direto data. "
        + " | ".join(errors or ["no candidate URL worked"])
    )


def ensure_csv(force_download: bool = False) -> tuple[Path, bool]:
    """Return a validated CSV path and whether it is stale.

    A cached file from the current UTC day is reused unless ``force_download``
    is set. If a refresh fails, a previously valid cached file is used and
    flagged as stale.
    """
    path = cache_dir() / CSV_FILENAME
    today = _today()

    cached_valid = path.exists() and is_valid_csv_bytes(path.read_bytes())
    if cached_valid and not force_download:
        file_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
        if file_date >= today:
            return path, False

    try:
        _download_csv(path)
        return path, False
    except DataError:
        if cached_valid:
            return path, True
        raise


# ---------------------------------------------------------------------------
# parsing


def parse_csv(path: Path) -> list[BondRecord]:
    """Parse a raw Tesouro Direto CSV into bond records."""
    content = path.read_bytes()
    if not is_valid_csv_bytes(content):
        raise DataError(
            f"The file at {path} is not a valid Tesouro Direto CSV "
            "(it may be an HTML error page or an incomplete download)."
        )

    reader = csv.reader(io.StringIO(_decode(content)), delimiter=";")
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DataError(f"The file at {path} is empty.") from exc

    columns = [_normalize_column(name) for name in header]
    index = {name: position for position, name in enumerate(columns)}
    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if missing:
        raise DataError(f"Missing required columns in {path}: {', '.join(missing)}")

    pos_tipo = index["tipo_titulo"]
    pos_matur = index["data_vencimento"]
    pos_base = index["data_base"]
    pos_yield = index["taxa_compra_manha"]
    pos_price = index["pu_compra_manha"]

    records: list[BondRecord] = []
    for row in reader:
        if len(row) <= max(pos_tipo, pos_matur, pos_base, pos_yield, pos_price):
            continue

        tipo = row[pos_tipo].strip()
        ref_date = _parse_date(row[pos_base])
        matur_date = _parse_date(row[pos_matur])
        yield_bid = _parse_number(row[pos_yield])
        price_bid = _parse_number(row[pos_price])

        if not tipo or ref_date is None or matur_date is None:
            continue
        if yield_bid is None or price_bid is None or price_bid <= 0:
            continue

        symbol = BOND_SYMBOLS.get(tipo, tipo)
        asset_code = f"{symbol} {matur_date:%d%m%y}"
        records.append(
            BondRecord(
                ref_date=ref_date,
                yield_bid=yield_bid / 100.0,
                price_bid=price_bid,
                asset_code=asset_code,
                matur_date=matur_date,
                tipo_titulo=tipo,
            )
        )

    if not records:
        raise DataError(f"No valid rows found in {path}.")

    records.sort(key=lambda item: (item.asset_code, item.ref_date))
    return records


_parsed_lock = threading.Lock()
_parsed_cache: tuple[str, float, list[BondRecord]] | None = None


def reset_cache() -> None:
    """Clear the in-process parsed-data cache (used by tests)."""
    global _parsed_cache
    with _parsed_lock:
        _parsed_cache = None


def load_dataset(force_download: bool = False) -> Dataset:
    """Load the full dataset, downloading and parsing it when needed."""
    global _parsed_cache

    path, stale = ensure_csv(force_download=force_download)
    signature = (str(path), path.stat().st_mtime)

    with _parsed_lock:
        cached = _parsed_cache
        if cached is not None and cached[0] == signature[0] and cached[1] == signature[1]:
            records = cached[2]
        else:
            records = parse_csv(path)
            _parsed_cache = (signature[0], signature[1], records)

    file_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
    return Dataset(
        records=records,
        source=SOURCE_NAME,
        csv_path=path,
        file_date=file_date,
        retrieved_at=datetime.now(timezone.utc),
        stale=stale,
    )


# ---------------------------------------------------------------------------
# selection and aggregation


def select_records(
    records: Iterable[BondRecord],
    *,
    groups: list[str] | None,
    start_date: date,
    end_date: date,
    asset_code: str | None = None,
) -> list[BondRecord]:
    """Filter records by asset group, date range and (optionally) asset code."""
    titles = titles_for_groups(groups)
    needle = asset_code.strip().casefold() if asset_code else None

    selected: list[BondRecord] = []
    for row in records:
        if row.ref_date < start_date or row.ref_date > end_date:
            continue
        if titles is not None and row.tipo_titulo not in titles:
            continue
        if needle is not None and needle not in row.asset_code.casefold():
            continue
        selected.append(row)
    return selected


def aggregate_records(
    records: Iterable[BondRecord], frequency: str
) -> list[BondRecord] | list[AggregatedRecord]:
    """Aggregate daily records to monthly or yearly means."""
    if frequency == "daily":
        return list(records)

    buckets: dict[tuple[str, str], dict[str, object]] = defaultdict(dict)
    for row in records:
        if frequency == "monthly":
            period = f"{row.ref_date.year}-{row.ref_date.month:02d}"
        elif frequency == "yearly":
            period = str(row.ref_date.year)
        else:  # pragma: no cover - validated by the tool
            raise ValueError(f"Unsupported frequency: {frequency}")

        key = (row.asset_code, period)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "n": 1,
                "yield": row.yield_bid,
                "price": row.price_bid,
                "first": row.ref_date,
                "last": row.ref_date,
                "matur": row.matur_date,
            }
        else:
            bucket["n"] = int(bucket["n"]) + 1
            bucket["yield"] = float(bucket["yield"]) + row.yield_bid
            bucket["price"] = float(bucket["price"]) + row.price_bid
            bucket["first"] = min(bucket["first"], row.ref_date)
            bucket["last"] = max(bucket["last"], row.ref_date)
            bucket["matur"] = row.matur_date

    aggregated: list[AggregatedRecord] = []
    for (asset_code, period), bucket in buckets.items():
        n = int(bucket["n"])
        aggregated.append(
            AggregatedRecord(
                period=period,
                asset_code=asset_code,
                matur_date=bucket["matur"],  # type: ignore[arg-type]
                n_obs=n,
                first_date=bucket["first"],  # type: ignore[arg-type]
                last_date=bucket["last"],  # type: ignore[arg-type]
                yield_bid_mean=float(bucket["yield"]) / n,
                price_bid_mean=float(bucket["price"]) / n,
            )
        )

    aggregated.sort(key=lambda item: (item.asset_code, item.period))
    return aggregated


def summarize(records: Iterable[BondRecord]) -> list[dict[str, object]]:
    """Compute per-asset summary statistics (ordered by maturity)."""
    grouped: dict[str, list[BondRecord]] = defaultdict(list)
    for row in records:
        grouped[row.asset_code].append(row)

    output: list[dict[str, object]] = []
    for asset_code, rows in grouped.items():
        rows.sort(key=lambda item: item.ref_date)
        last = rows[-1]
        min_row = min(rows, key=lambda item: item.yield_bid)
        max_row = max(rows, key=lambda item: item.yield_bid)
        output.append(
            {
                "asset_code": asset_code,
                "matur_date": last.matur_date.isoformat(),
                "n_obs": len(rows),
                "first_date": rows[0].ref_date.isoformat(),
                "last_date": last.ref_date.isoformat(),
                "last_price_bid": round(last.price_bid, 2),
                "last_yield_pct": round(last.yield_bid * 100, 4),
                "mean_yield_pct": round(
                    sum(row.yield_bid for row in rows) / len(rows) * 100, 4
                ),
                "min_yield_pct": round(min_row.yield_bid * 100, 4),
                "min_yield_date": min_row.ref_date.isoformat(),
                "max_yield_pct": round(max_row.yield_bid * 100, 4),
                "max_yield_date": max_row.ref_date.isoformat(),
            }
        )

    output.sort(key=lambda item: (item["matur_date"], item["asset_code"]))
    return output
