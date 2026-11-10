#!/usr/bin/env python3
"""Live smoke test: exercises every tool against the real data sources.

Run with:

    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from tesourodireto_mcp import server


def ok(label: str, detail: str = "") -> None:
    print(f"  OK  {label}{f' — {detail}' if detail else ''}")


def fail(label: str, detail: str) -> None:
    print(f"  FAIL {label} — {detail}")


async def main() -> int:
    failures = 0

    catalog = json.loads(server.list_bond_types())
    if catalog["count"] == 9:
        ok("list_bond_types", f"{catalog['count']} bond types")
    else:
        fail("list_bond_types", f"unexpected count {catalog['count']}")
        failures += 1

    data = await server.get_bond_data(["LTN"], 2023, 2025)
    lines = [line for line in data.splitlines() if not line.startswith("#")]
    if len(lines) > 1 and lines[1].startswith("20"):
        ok("get_bond_data", f"{len(lines) - 1} LTN rows")
        print(f"      first row: {lines[1]}")
    else:
        fail("get_bond_data", data[:300])
        failures += 1

    monthly = await server.get_bond_data(["NTN-B Principal"], 2020, 2026, frequency="monthly")
    if "yield_bid_mean" in monthly:
        ok("get_bond_data(monthly)", f"{len(monthly.splitlines())} lines")
    else:
        fail("get_bond_data(monthly)", monthly[:300])
        failures += 1

    current = await server.get_current_rates(["NTN-B Principal", "LTN", "LFT"])
    current_lines = [line for line in current.splitlines() if not line.startswith("#")]
    if len(current_lines) > 1 and "asset_code" in current_lines[0]:
        ok("get_current_rates", f"{len(current_lines) - 1} current bonds")
    else:
        fail("get_current_rates", current[:300])
        failures += 1

    stats = json.loads(await server.get_bond_stats(["NTN-B Principal"], 2020, 2026))
    if stats.get("count", 0) > 0 and stats["stats"][0]["n_obs"] > 0:
        first = stats["stats"][0]
        ok(
            "get_bond_stats",
            f"{first['asset_code']} last yield {first['last_yield_pct']:.2f}%",
        )
    else:
        fail("get_bond_stats", json.dumps(stats)[:300])
        failures += 1

    curve = await server.get_yield_curve()
    curve_lines = [line for line in curve.splitlines() if not line.startswith("#")]
    if len(curve_lines) > 1 and "real_return" in curve:
        ok("get_yield_curve", f"{len(curve_lines) - 1} curve points")
        print(f"      reference: {curve_lines[1]}")
    else:
        fail("get_yield_curve", curve[:300])
        failures += 1

    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print("\nAll tools responded correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
