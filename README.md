# tesourodireto-mcp

[![PyPI](https://img.shields.io/pypi/v/tesourodireto-mcp.svg)](https://pypi.org/project/tesourodireto-mcp/)
[![tests](https://github.com/msperlin/tesourodireto-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/msperlin/tesourodireto-mcp/actions/workflows/tests.yml)

An MCP server for **Brazilian government bonds (Tesouro Direto)** — historical
prices and yields, current rates, per-maturity statistics and the ANBIMA yield
curve — built for LLM agents.

It is the Python/MCP counterpart of the R package
[`GetTDData`](https://github.com/msperlin/GetTDData): same asset codes, same
parsing rules, same fields. It talks directly to the official Tesouro
Transparente CKAN open data portal, so no R installation is required.

## Why this server

- **Official data, no R dependency.** Downloads `precotaxatesourodireto.csv`
  from Tesouro Transparente and reproduces `GetTDData::td_get2()` semantics.
- **Robust downloads.** Retries, validates the CSV header before replacing the
  cache, and falls back to a previously valid cache when the portal is down.
- **Local cache.** One download per day, stored in `~/.cache/tesourodireto-mcp`
  (configurable), with an in-memory parsed cache for fast repeated calls.
- **Token-efficient output.** CSV with a short comment header — not thousands
  of repetitive JSON records. Long series can be aggregated monthly/yearly.
- **The whole curve.** `get_yield_curve` scrapes ANBIMA's ETTJ and computes
  maturity dates with the Brazil/ANBIMA business-day calendar.
- **Agent guidance.** Server instructions, a static bond catalog resource and
  an `analyze_bond` prompt.

## Tools

| Tool | What it does |
| --- | --- |
| `list_bond_types` | Bond catalog: code, official name, indexer, coupon, description and aliases. |
| `get_bond_data` | Historical daily prices/yields, filterable by bond family, maturity, dates; optional monthly/yearly means. |
| `get_current_rates` | Latest snapshot of every bond (one row per maturity). |
| `get_bond_stats` | Per-maturity summary: last/mean/min/max yield, dates, observation counts. |
| `get_yield_curve` | ANBIMA real, nominal and implicit-inflation yield curves. |

Resources: `tesouro://catalog/bond-types`. Prompt: `analyze_bond`.

### Output conventions

- `yield_bid` is a **decimal annual rate**: `0.1183` means **11.83% p.a.**
  `yield_bid_pct` and the `*_pct` statistics are already in percent.
- `price_bid` is the unit purchase price in **BRL (R$)**.
- `asset_code` is the bond family plus maturity as `DDMMYY`, e.g.
  `LTN 010123`, `NTN-B Principal 150835`.
- Data starts in **2005**. Daily series are truncated to the most recent
  `max_rows` rows; use `frequency="monthly"` or a narrower range for long
  histories.

## Install

Requires Python 3.10+.

From [PyPI](https://pypi.org/project/tesourodireto-mcp/), no clone needed:

```sh
uvx tesourodireto-mcp
```

or with pip:

```sh
pip install tesourodireto-mcp
tesourodireto-mcp
```

From source:

```sh
git clone https://github.com/msperlin/tesourodireto-mcp.git
cd tesourodireto-mcp
uv sync
uv run tesourodireto-mcp
```

Or install the checkout as a standalone tool:

```sh
uv tool install .
tesourodireto-mcp
```

## Register with OpenCode

Add to `~/.config/opencode/opencode.json` (or a project `opencode.json`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "tesourodireto-mcp": {
        "type": "local",
        "command": [
          "/home/msperlin/GitRepo/mcp-servers/tesourodireto-mcp/.venv/bin/python",
          "-m",
          "tesourodireto_mcp"
        ]
      }
    }
  }
}
```

Or with `uv`, without hard-coding a virtualenv path:

```jsonc
"command": ["uv", "run", "--directory", "/path/to/tesourodireto-mcp", "tesourodireto-mcp"]
```

## Register with Claude Desktop

```jsonc
{
  "mcpServers": {
    "tesourodireto-mcp": {
      "command": "uvx",
      "args": ["tesourodireto-mcp"]
    }
  }
}
```

## Examples

Ask the agent:

> What are the current NTN-B Principal yields and maturities?

> How did the LTN 010127 yield evolve in 2024?

> Compare the last 10 years of NTN-B Principal yields with the ANBIMA real
> return curve.

> Summarize the inflation implied by today's Brazilian yield curve.

Typical tool calls:

```text
list_bond_types(asset_codes=["NTN-B Principal"])
get_bond_data(asset_codes=["LTN"], first_year=2024, last_year=2024, asset_code="010127")
get_bond_data(asset_codes=["NTN-B Principal"], first_year=2015, frequency="monthly")
get_current_rates(asset_codes=["LFT", "LTN"])
get_bond_stats(asset_codes=["NTN-B Principal"], first_year=2015, max_assets=10)
get_yield_curve()
```

A live call returns:

```text
# Tesouro Direto — source: Tesouro Transparente (CKAN)
# Retrieved: 2026-11-01T12:00:00+00:00 | data file date: 2026-11-01 | latest ref date: 2026-10-30 | stale cache: no
# yield_bid is a decimal annual rate (0.1183 = 11.83% p.a.); ...
ref_date,yield_bid,yield_bid_pct,price_bid,asset_code,matur_date
2026-10-30,0.118300,11.8300,912.34,LTN 010127,2027-01-01
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TESOURO_DIRETO_CACHE_DIR` | `~/.cache/tesourodireto-mcp` | Where the raw CSV is cached. |
| `TESOURO_DIRETO_CSV_URL` | Official CKAN resource URL | Override with a direct CSV URL or a local file path (useful offline/tests). |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http` or `sse`. |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Bind address for HTTP transports. |

## Development

```sh
uv sync
uv run pytest              # offline unit + end-to-end MCP stdio tests
uv run python scripts/smoke_test.py   # live test against Tesouro/ANBIMA
```

The test suite uses a small CSV fixture, so it does not need network access.
The smoke test downloads the real dataset and the current ANBIMA curve.

The ANBIMA business-day calendar (`src/tesourodireto_mcp/anbima_holidays.txt`)
was exported from the `bizdays` Brazil/ANBIMA calendar used by `GetTDData`, so
curve maturity dates match the R package.

## Data sources and disclaimer

- Bond data: [Tesouro Transparente](https://www.tesourotransparente.gov.br/ckan/dataset/taxas-dos-titulos-ofertados-pelo-tesouro-direto),
  the official open data portal of the Brazilian National Treasury.
- Yield curve: [ANBIMA](https://www.anbima.com.br/informacoes/est-termo/CZ.asp).

This project is not affiliated with Tesouro Nacional, ANBIMA or the R package
authors. Data is provided as-is; verify before making financial decisions.

## License

MIT © Marcelo S. Perlin
