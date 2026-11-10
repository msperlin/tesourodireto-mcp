import json
from datetime import date

import pytest

from tesourodireto_mcp import server
from tesourodireto_mcp.catalog import (
    InvalidAssetCode,
    bond_types,
    normalize_asset_codes,
)
from tesourodireto_mcp.data import BondRecord
from tesourodireto_mcp.yield_curve import YieldCurvePoint

RECORDS = [
    BondRecord(date(2024, 1, 2), 0.105, 900.10, "LTN 010125", date(2025, 1, 1), "Tesouro Prefixado"),
    BondRecord(date(2024, 2, 29), 0.106, 915.30, "LTN 010125", date(2025, 1, 1), "Tesouro Prefixado"),
    BondRecord(date(2024, 1, 2), 0.112, 800.50, "LTN 010127", date(2027, 1, 1), "Tesouro Prefixado"),
    BondRecord(date(2024, 2, 29), 0.114, 805.60, "LTN 010127", date(2027, 1, 1), "Tesouro Prefixado"),
    BondRecord(
        date(2024, 1, 2), 0.061, 1500.0, "NTN-B Principal 150835", date(2035, 8, 15), "Tesouro IPCA+"
    ),
    BondRecord(
        date(2024, 2, 29), 0.063, 1540.2, "NTN-B Principal 150835", date(2035, 8, 15), "Tesouro IPCA+"
    ),
]


class FakeDataset:
    def __init__(self, records=RECORDS, stale=False):
        self.records = records
        self.stale = stale

    def as_meta(self):
        return {
            "source": "Tesouro Transparente (CKAN)",
            "csv_path": "/tmp/sample.csv",
            "data_file_date": "2024-03-01",
            "retrieved_at": "2024-03-01T12:00:00+00:00",
            "stale": self.stale,
            "latest_ref_date": "2024-02-29",
        }


@pytest.fixture
def fake_dataset(monkeypatch):
    async def fake_load(force_download=False):
        return FakeDataset()

    monkeypatch.setattr(server, "_load", fake_load)
    return FakeDataset()


def _data_lines(text):
    """Return CSV lines without comments or header."""
    return [line for line in text.splitlines() if line and not line.startswith("#")][1:]


def test_catalog_codes():
    assert bond_types("LTN")[0].asset_code == "LTN"
    assert bond_types("Tesouro Prefixado")[0].asset_code == "LTN"
    assert normalize_asset_codes(["NTN-B Principal"]) == ["ipca_principal"]
    assert normalize_asset_codes(None) is None
    with pytest.raises(InvalidAssetCode):
        normalize_asset_codes(["XYZ"])
    with pytest.raises(InvalidAssetCode):
        normalize_asset_codes([])


def test_list_bond_types_tool():
    payload = json.loads(server.list_bond_types())
    assert payload["count"] == 9
    payload = json.loads(server.list_bond_types(["RendA+"]))
    assert payload["count"] == 1
    assert payload["bond_types"][0]["asset_code"] == "RendA+"


async def test_get_bond_data_daily(fake_dataset):
    text = await server.get_bond_data(["LTN"], 2024, 2024)
    lines = _data_lines(text)
    assert len(lines) == 4
    assert lines[0].startswith("2024-01-02,0.105000,10.5000,900.10,LTN 010125,2025-01-01")
    assert "yield_bid is a decimal annual rate" in text


async def test_get_bond_data_filters_asset_code(fake_dataset):
    text = await server.get_bond_data(["LTN"], 2024, 2024, asset_code="010127")
    lines = _data_lines(text)
    assert len(lines) == 2
    assert all("LTN 010127" in line for line in lines)


async def test_get_bond_data_monthly(fake_dataset):
    text = await server.get_bond_data(["LTN"], 2024, 2024, frequency="monthly")
    lines = _data_lines(text)
    assert "period,asset_code" in text
    assert "2024-01,LTN 010125" in text
    # 2024-01 has one observation; 2024-02 also has one
    assert len(lines) == 4


async def test_get_bond_data_truncates_most_recent(fake_dataset):
    text = await server.get_bond_data(["LTN"], 2024, 2024, max_rows=10)
    assert "TRUNCATED" not in text

    # a narrow max_rows is not allowed by the schema (ge=10); test helper directly
    from tesourodireto_mcp.data import aggregate_records
    from tesourodireto_mcp.server import _keep_most_recent

    rows = aggregate_records(fake_dataset.records, "daily")
    kept, dropped = _keep_most_recent(rows, 2)
    assert dropped == 4
    assert len(kept) == 2


async def test_get_bond_data_invalid_code(fake_dataset):
    payload = json.loads(await server.get_bond_data(["XYZ"], 2024, 2024))
    assert "error" in payload


async def test_get_bond_data_empty_selection(fake_dataset):
    payload = json.loads(await server.get_bond_data(["LTN"], 2020, 2021))
    assert payload["count"] == 0


async def test_get_current_rates(fake_dataset):
    text = await server.get_current_rates(["LTN", "NTN-B Principal"])
    lines = _data_lines(text)
    assert len(lines) == 3
    assert all(line.startswith("2024-02-29,") for line in lines)


async def test_get_bond_stats(fake_dataset):
    payload = json.loads(await server.get_bond_stats(["LTN"], 2024, 2024))
    assert payload["count"] == 2
    assert payload["latest_ref_date"] == "2024-02-29"
    first = payload["stats"][0]
    assert first["asset_code"] == "LTN 010125"
    assert first["n_obs"] == 2
    assert first["last_yield_pct"] == pytest.approx(10.6)


async def test_get_yield_curve(monkeypatch):
    points = [
        YieldCurvePoint(252, "real_return", 6.5, date(2027, 10, 26), date(2026, 10, 22)),
        YieldCurvePoint(252, "nominal_return", 14.3, date(2027, 10, 26), date(2026, 10, 22)),
    ]

    def fake_fetch(force=False):
        return points

    monkeypatch.setattr(server, "fetch_yield_curve", fake_fetch)
    text = await server.get_yield_curve()
    assert "real_return" in text
    assert text.splitlines()[-1].startswith("252,nominal_return,14.3000,")


def test_analyze_bond_prompt():
    text = server.analyze_bond("NTN-B Principal", 2015)
    assert "get_bond_stats" in text
    assert "NTN-B Principal" in text
