"""MCP server exposing Tesouro Direto bond data and the ANBIMA yield curve."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .catalog import (
    BondType,
    InvalidAssetCode,
    bond_types,
    normalize_asset_codes,
)
from .data import (
    MIN_FIRST_YEAR,
    AggregatedRecord,
    BondRecord,
    DataError,
    aggregate_records,
    load_dataset,
    select_records,
    summarize,
)
from .yield_curve import YieldCurveError, fetch_yield_curve

# MCP clients capture stderr; do not let httpx log every request at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

INSTRUCTIONS = """\
Brazilian government bonds (Tesouro Direto) and the ANBIMA yield curve.

How to work with this server:
1. Call `list_bond_types` when you need the meaning of a bond code
   (LTN, LFT, NTN-B, NTN-B Principal, NTN-F, NTN-C, RendA+, Educa+).
2. Call `get_bond_data` for historical daily prices and yields. Filter with
   `asset_codes` (bond family) and, to isolate one maturity, `asset_code`
   (e.g. "NTN-B Principal 150835").
3. Call `get_current_rates` for the latest snapshot of every bond.
4. Call `get_bond_stats` for compact per-maturity summary statistics.
5. Call `get_yield_curve` for ANBIMA's current real/nominal/inflation curve.

Important:
- `yield_bid` is a decimal annual rate: 0.1183 means 11.83% p.a.
  `yield_bid_pct` and the `*_pct` statistics are already in percent.
- Prices (`price_bid`) are in Brazilian reais (R$).
- Asset codes end with the maturity date as DDMMYY, e.g. "LTN 010123".
- Long daily series are truncated to the most recent `max_rows` rows; use
  `frequency="monthly"` or a narrower date range for long histories.
- Cite the source as "Tesouro Transparente" (bond data) or "ANBIMA" (curve)
  and include the reference/retrieval date.
Do not invent prices or yields; report what the tools return.
"""

mcp = MCPServer(
    "tesourodireto-mcp",
    title="Tesouro Direto — Brazilian Government Bonds",
    description=(
        "Historical prices, yields and current rates for Brazilian government "
        "bonds (Tesouro Direto), plus the ANBIMA yield curve."
    ),
    instructions=INSTRUCTIONS,
    version=__version__,
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_MAX_ROWS_LIMIT = 5000
_MAX_STATS_ASSETS = 200


# ---------------------------------------------------------------------------
# helpers


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _error(message: str, *, hint: str | None = None, **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return _json(payload)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: str, argument: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"`{argument}` must be a date in YYYY-MM-DD format, got {value!r}.")


def _resolve_range(
    first_year: int, last_year: int, start_date: str | None, end_date: str | None
) -> tuple[date, date, list[str]]:
    notes: list[str] = []

    if first_year < MIN_FIRST_YEAR:
        notes.append(
            f"first_year {first_year} is before {MIN_FIRST_YEAR}; using {MIN_FIRST_YEAR}."
        )
        first_year = MIN_FIRST_YEAR
    if last_year < first_year:
        raise ValueError(
            f"`first_year` ({first_year}) cannot be greater than `last_year` ({last_year})."
        )

    start = date(first_year, 1, 1)
    end = date(last_year, 12, 31)

    if start_date:
        start = _parse_date(start_date, "start_date")
    if end_date:
        end = _parse_date(end_date, "end_date")
    if start > end:
        raise ValueError(f"`start_date` ({start}) is after `end_date` ({end}).")

    return start, end, notes


def _fmt(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def _csv_text(
    columns: list[str], rows: list[list[str]], comments: list[str] | None = None
) -> str:
    buffer = io.StringIO()
    if comments:
        for comment in comments:
            buffer.write(f"# {comment}\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def _dataset_comments(meta: dict[str, object], extra: list[str] | None = None) -> list[str]:
    comments = [
        f"Tesouro Direto — source: {meta['source']}",
        (
            f"Retrieved: {meta['retrieved_at']} | data file date: {meta['data_file_date']}"
            f" | latest ref date: {meta['latest_ref_date']} | stale cache: "
            f"{'yes' if meta['stale'] else 'no'}"
        ),
        (
            "yield_bid is a decimal annual rate (0.1183 = 11.83% p.a.); "
            "yield_bid_pct is the same rate in percent. price_bid is in BRL (R$)."
        ),
    ]
    if extra:
        comments.extend(extra)
    return comments


def _keep_most_recent(
    rows: list[BondRecord] | list[AggregatedRecord], max_rows: int
) -> tuple[list[BondRecord] | list[AggregatedRecord], int]:
    """Keep the most recent ``max_rows`` observations, then restore sort order."""
    if len(rows) <= max_rows:
        return rows, 0

    def row_date(row: BondRecord | AggregatedRecord) -> date:
        return row.ref_date if isinstance(row, BondRecord) else row.last_date

    kept = sorted(rows, key=row_date, reverse=True)[:max_rows]
    if isinstance(kept[0], BondRecord):
        kept.sort(key=lambda row: (row.asset_code, row.ref_date))  # type: ignore[union-attr]
    else:
        kept.sort(key=lambda row: (row.asset_code, row.period))  # type: ignore[union-attr]
    return kept, len(rows) - len(kept)


def _daily_rows(rows: list[BondRecord]) -> tuple[list[str], list[list[str]]]:
    columns = [
        "ref_date",
        "yield_bid",
        "yield_bid_pct",
        "price_bid",
        "asset_code",
        "matur_date",
    ]
    data = [
        [
            row.ref_date.isoformat(),
            _fmt(row.yield_bid, 6),
            _fmt(row.yield_bid * 100, 4),
            _fmt(row.price_bid, 2),
            row.asset_code,
            row.matur_date.isoformat(),
        ]
        for row in rows
    ]
    return columns, data


def _aggregated_rows(
    rows: list[AggregatedRecord],
) -> tuple[list[str], list[list[str]]]:
    columns = [
        "period",
        "asset_code",
        "matur_date",
        "yield_bid_mean",
        "yield_bid_mean_pct",
        "price_bid_mean",
        "n_obs",
        "first_date",
        "last_date",
    ]
    data = [
        [
            row.period,
            row.asset_code,
            row.matur_date.isoformat(),
            _fmt(row.yield_bid_mean, 6),
            _fmt(row.yield_bid_mean * 100, 4),
            _fmt(row.price_bid_mean, 2),
            str(row.n_obs),
            row.first_date.isoformat(),
            row.last_date.isoformat(),
        ]
        for row in rows
    ]
    return columns, data


async def _load(force_download: bool):
    return await asyncio.to_thread(load_dataset, force_download)


# ---------------------------------------------------------------------------
# tools


@mcp.tool(annotations=_READ_ONLY)
def list_bond_types(
    asset_codes: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional bond codes or official names to filter, e.g. ['LTN'] or "
                "['NTN-B Principal', 'RendA+']. Omit for the full catalog."
            )
        ),
    ] = None,
) -> str:
    """Describe the Brazilian government bond types available at Tesouro Direto.

    Returns each bond's short code, official name, indexer, coupon structure,
    a short description and the aliases accepted by the other tools.
    """
    try:
        selected: list[BondType] = bond_types(asset_codes)
    except InvalidAssetCode as exc:
        return _error(str(exc), hint="Call list_bond_types without arguments to see all codes.")

    from .catalog import ALIASES

    payload = {
        "count": len(selected),
        "bond_types": [
            {
                **item.as_dict(),
                "aliases": sorted(
                    alias for alias, group in ALIASES.items() if group == item.group
                ),
            }
            for item in selected
        ],
        "notes": [
            "asset_code = bond family + maturity date (DDMMYY), e.g. 'LTN 010123'.",
            "yield_bid from get_bond_data is a decimal rate (0.1183 = 11.83% p.a.).",
        ],
    }
    return _json(payload)


@mcp.tool(annotations=_READ_ONLY)
async def get_bond_data(
    asset_codes: Annotated[
        list[str] | None,
        Field(
            description=(
                "Bond families: LTN, LFT, NTN-B, NTN-B Principal, NTN-F, NTN-C, "
                "RendA+, Educa+ (official names also accepted). Omit for all bonds."
            )
        ),
    ] = None,
    first_year: Annotated[
        int,
        Field(description="First calendar year of the series (minimum 2005).", ge=1900, le=2100),
    ] = MIN_FIRST_YEAR,
    last_year: Annotated[
        int | None,
        Field(
            description="Last calendar year of the series. Defaults to the current year.",
            ge=1900,
            le=2100,
        ),
    ] = None,
    asset_code: Annotated[
        str | None,
        Field(
            description=(
                "Optional single maturity filter, matched as a case-insensitive "
                "substring, e.g. 'NTN-B Principal 150835' or '150835'."
            )
        ),
    ] = None,
    frequency: Annotated[
        Literal["daily", "monthly", "yearly"],
        Field(description="Observation frequency: daily raw data, or monthly/yearly means."),
    ] = "daily",
    start_date: Annotated[
        str | None,
        Field(description="Optional precise start date (YYYY-MM-DD), overriding first_year."),
    ] = None,
    end_date: Annotated[
        str | None,
        Field(description="Optional precise end date (YYYY-MM-DD), overriding last_year."),
    ] = None,
    max_rows: Annotated[
        int,
        Field(
            description="Maximum rows returned (most recent observations are kept).",
            ge=10,
            le=_MAX_ROWS_LIMIT,
        ),
    ] = 500,
    force_download: Annotated[
        bool,
        Field(description="Ignore today's cached file and download the source again."),
    ] = False,
) -> str:
    """Historical daily prices and yields of Tesouro Direto bonds.

    Returns CSV. For daily data the columns are `ref_date`, `yield_bid`
    (decimal annual rate), `yield_bid_pct`, `price_bid` (R$), `asset_code` and
    `matur_date`. For monthly/yearly data each row is the mean over the period,
    with the number of observations and first/last dates.

    If the result exceeds `max_rows`, the most recent observations are kept and
    a truncation note is included in the CSV comments.
    """
    last_year_value = _today().year if last_year is None else last_year

    try:
        groups = normalize_asset_codes(asset_codes)
        start, end, notes = _resolve_range(
            first_year, last_year_value, start_date, end_date
        )
    except (InvalidAssetCode, ValueError) as exc:
        return _error(str(exc))

    try:
        dataset = await _load(force_download)
        selected = select_records(
            dataset.records,
            groups=groups,
            start_date=start,
            end_date=end,
            asset_code=asset_code,
        )
    except DataError as exc:
        return _error(
            str(exc),
            hint=(
                "Check the internet connection or set TESOURO_DIRETO_CSV_URL to a "
                "local/direct CSV file. The raw cache lives in "
                "~/.cache/tesourodireto-mcp."
            ),
        )

    if not selected:
        return _json(
            {
                "count": 0,
                "message": "No Tesouro Direto data found for this selection.",
                "selection": {
                    "asset_codes": asset_codes,
                    "asset_code": asset_code,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
                "hint": "Widen the date range or check the maturity in `asset_code`.",
            }
        )

    aggregated = aggregate_records(selected, frequency)
    kept, dropped = _keep_most_recent(aggregated, max_rows)

    comments = _dataset_comments(dataset.as_meta())
    comments.append(
        f"Selection: {', '.join(asset_codes) if asset_codes else 'all bonds'}"
        f" | {start.isoformat()} to {end.isoformat()}"
        + (f" | asset_code filter: {asset_code}" if asset_code else "")
        + f" | frequency: {frequency}"
    )
    comments.append(f"Rows returned: {len(kept)} of {len(aggregated)}")
    if dropped:
        comments.append(
            f"TRUNCATED: kept the {len(kept)} most recent rows (max_rows={max_rows}); "
            "use frequency='monthly' or a narrower date range for full coverage."
        )
    comments.extend(notes)

    if frequency == "daily":
        columns, rows = _daily_rows(kept)  # type: ignore[arg-type]
    else:
        columns, rows = _aggregated_rows(kept)  # type: ignore[arg-type]

    return _csv_text(columns, rows, comments)


@mcp.tool(annotations=_READ_ONLY)
async def get_current_rates(
    asset_codes: Annotated[
        list[str] | None,
        Field(
            description=(
                "Bond families to include (LTN, LFT, NTN-B, NTN-B Principal, "
                "NTN-F, NTN-C, RendA+, Educa+). Omit for all bonds."
            )
        ),
    ] = None,
    force_download: Annotated[
        bool,
        Field(description="Ignore today's cached file and download the source again."),
    ] = False,
) -> str:
    """Current prices and yields for all Tesouro Direto bonds.

    Returns the latest available snapshot (the most recent reference date in
    the data) as CSV, one row per bond and maturity, ordered by maturity.
    """
    try:
        groups = normalize_asset_codes(asset_codes)
    except InvalidAssetCode as exc:
        return _error(str(exc))

    try:
        dataset = await _load(force_download)
        selected = select_records(
            dataset.records,
            groups=groups,
            start_date=date(1900, 1, 1),
            end_date=date(2100, 12, 31),
        )
    except DataError as exc:
        return _error(str(exc), hint="Check the internet connection or the local cache.")

    if not selected:
        return _json({"count": 0, "message": "No bonds found for this selection."})

    latest = max(row.ref_date for row in selected)
    current = [row for row in selected if row.ref_date == latest]
    current.sort(key=lambda row: (row.matur_date, row.asset_code))

    columns = [
        "ref_date",
        "asset_code",
        "matur_date",
        "price_bid",
        "yield_bid",
        "yield_bid_pct",
    ]
    rows = [
        [
            row.ref_date.isoformat(),
            row.asset_code,
            row.matur_date.isoformat(),
            _fmt(row.price_bid, 2),
            _fmt(row.yield_bid, 6),
            _fmt(row.yield_bid * 100, 4),
        ]
        for row in current
    ]

    comments = _dataset_comments(
        dataset.as_meta(),
        [f"Current snapshot: {len(rows)} bonds at reference date {latest.isoformat()}"],
    )
    return _csv_text(columns, rows, comments)


@mcp.tool(annotations=_READ_ONLY)
async def get_bond_stats(
    asset_codes: Annotated[
        list[str] | None,
        Field(description="Bond families to include. Omit for all bonds."),
    ] = None,
    first_year: Annotated[
        int, Field(description="First calendar year (minimum 2005).", ge=1900, le=2100)
    ] = MIN_FIRST_YEAR,
    last_year: Annotated[
        int | None,
        Field(
            description="Last calendar year. Defaults to the current year.",
            ge=1900,
            le=2100,
        ),
    ] = None,
    start_date: Annotated[
        str | None, Field(description="Optional precise start date (YYYY-MM-DD).")
    ] = None,
    end_date: Annotated[
        str | None, Field(description="Optional precise end date (YYYY-MM-DD).")
    ] = None,
    max_assets: Annotated[
        int,
        Field(
            description="Maximum number of maturities summarized (nearest maturities kept).",
            ge=1,
            le=_MAX_STATS_ASSETS,
        ),
    ] = 50,
    force_download: Annotated[
        bool, Field(description="Ignore today's cached file and download again.")
    ] = False,
) -> str:
    """Summary statistics per bond maturity: last/min/max yield, mean and dates.

    Returns JSON. Useful to compare many maturities without downloading the full
    daily series.
    """
    last_year_value = _today().year if last_year is None else last_year

    try:
        groups = normalize_asset_codes(asset_codes)
        start, end, notes = _resolve_range(
            first_year, last_year_value, start_date, end_date
        )
    except (InvalidAssetCode, ValueError) as exc:
        return _error(str(exc))

    try:
        dataset = await _load(force_download)
        selected = select_records(
            dataset.records, groups=groups, start_date=start, end_date=end
        )
    except DataError as exc:
        return _error(str(exc), hint="Check the internet connection or the local cache.")

    if not selected:
        return _json({"count": 0, "message": "No bonds found for this selection."})

    stats = summarize(selected)
    total = len(stats)
    if total > max_assets:
        stats = stats[:max_assets]

    payload: dict[str, Any] = {
        "count": len(stats),
        "total_assets_matched": total,
        "truncated": total > max_assets,
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "latest_ref_date": max(row.ref_date for row in selected).isoformat(),
        "source": dataset.as_meta(),
        "stats": stats,
        "notes": [
            "Yield statistics are in percent per year.",
            "n_obs counts daily observations for that maturity.",
        ]
        + notes,
    }
    if total > max_assets:
        payload["hint"] = (
            f"{total} maturities matched; only the {max_assets} nearest maturities "
            "are shown. Raise max_assets or filter `asset_codes`."
        )
    return _json(payload)


@mcp.tool(annotations=_READ_ONLY)
async def get_yield_curve(
    force: Annotated[
        bool, Field(description="Ignore the 30-minute cache and fetch ANBIMA again.")
    ] = False,
) -> str:
    """Current Brazilian yield curve from ANBIMA (ETTJ).

    Returns CSV with `n_biz_days`, `type` (real_return, nominal_return or
    implicit_inflation), `value` in percent per year, the derived `ref_date`
    (maturity) and ANBIMA's `current_date`.
    """
    try:
        points = await asyncio.to_thread(fetch_yield_curve, force)
    except YieldCurveError as exc:
        return _error(
            str(exc),
            hint="ANBIMA occasionally changes its page or blocks scraping; try again later.",
        )

    columns = ["n_biz_days", "type", "value", "ref_date", "current_date"]
    rows = [
        [
            str(point.n_biz_days),
            point.type,
            _fmt(point.value, 4),
            point.ref_date.isoformat(),
            point.current_date.isoformat(),
        ]
        for point in points
    ]
    comments = [
        "ANBIMA — Estrutura a Termo da Taxa de Juros (ETTJ)",
        f"Reference date: {points[0].current_date.isoformat()} | points: {len(rows)}",
        (
            "value is in percent per year (% a.a./252). real_return = ETTJ IPCA, "
            "nominal_return = ETTJ PRE, implicit_inflation = inflação implícita."
        ),
        "Maturity dates use the Brazil/ANBIMA business-day calendar.",
    ]
    return _csv_text(columns, rows, comments)


# ---------------------------------------------------------------------------
# resources and prompt


@mcp.resource("tesouro://catalog/bond-types", mime_type="application/json")
def bond_types_resource() -> str:
    """Static catalog of Tesouro Direto bond types and aliases."""
    from .catalog import ALIASES

    return _json(
        [
            {
                **item.as_dict(),
                "aliases": sorted(
                    alias for alias, group in ALIASES.items() if group == item.group
                ),
            }
            for item in bond_types()
        ]
    )


@mcp.prompt(title="Analyze a Tesouro Direto bond")
def analyze_bond(
    asset_code: str = "NTN-B Principal",
    first_year: int = 2015,
) -> str:
    """Prompt an agent to analyze one bond family with this server's data."""
    return f"""\
Analyze the Brazilian government bond family "{asset_code}" from {first_year} to today.

Steps:
1. Call `list_bond_types` with asset_codes=["{asset_code}"] to confirm the code,
   indexer and coupon structure.
2. Call `get_bond_stats` (asset_codes=["{asset_code}"], first_year={first_year})
   to see every maturity and its latest yield.
3. Pick the most liquid/relevant maturity and call `get_bond_data` for it,
   using frequency="monthly" if the series is long.
4. Call `get_current_rates` to put the bond in the context of the whole curve,
   and `get_yield_curve` for ANBIMA's real/nominal/inflation curve.
5. Discuss the yield trajectory, the real vs. nominal rate decomposition
   (NTN-B vs. LTN/NTN-F) and the inflation implied by the curve.

Quote yields in percent per year and cite "Tesouro Transparente" and "ANBIMA"
with the reference dates returned by the tools.
"""


# ---------------------------------------------------------------------------
# entry point


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
    elif transport in {"streamable-http", "sse"}:
        mcp.run(
            transport=transport,  # type: ignore[arg-type]
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        raise SystemExit(
            f"invalid MCP_TRANSPORT: {transport!r} (use stdio, streamable-http or sse)"
        )
