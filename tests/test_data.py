import shutil
from datetime import date
from pathlib import Path

import pytest

from tesourodireto_mcp import data

FIXTURE = Path(__file__).parent / "fixtures" / "sample.csv"


def test_is_valid_csv_bytes():
    assert data.is_valid_csv_bytes(FIXTURE.read_bytes())
    assert not data.is_valid_csv_bytes(b"<html>rate limited</html>")
    assert not data.is_valid_csv_bytes(b"")


def test_parse_csv():
    records = data.parse_csv(FIXTURE)
    assert len(records) == 15
    assert all(record.price_bid > 0 for record in records)
    assert all(0 < record.yield_bid < 1 for record in records)

    first = next(
        record
        for record in records
        if record.asset_code == "LTN 010125" and record.ref_date == date(2024, 1, 2)
    )
    assert first.matur_date == date(2025, 1, 1)
    assert first.yield_bid == pytest.approx(0.105)
    assert first.price_bid == pytest.approx(900.10)


def test_parse_csv_keeps_all_bond_families():
    codes = {record.asset_code for record in data.parse_csv(FIXTURE)}
    assert any(code.startswith("LTN ") for code in codes)
    assert any(code.startswith("LFT ") for code in codes)
    assert any(code.startswith("NTN-B ") for code in codes)
    assert any(code.startswith("NTN-B Principal ") for code in codes)
    assert any(code.startswith("NTN-B1 ") for code in codes)


def test_parse_csv_rejects_invalid_file(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("<html>error</html>", encoding="utf-8")
    with pytest.raises(data.DataError):
        data.parse_csv(bad)


def test_number_parsing():
    assert data._parse_number("10,50") == pytest.approx(10.5)
    assert data._parse_number("1.234,56") == pytest.approx(1234.56)
    assert data._parse_number("") is None
    assert data._parse_number("abc") is None


def test_ensure_csv_from_local_file(tmp_path, monkeypatch):
    source = tmp_path / "source.csv"
    shutil.copy(FIXTURE, source)
    cache = tmp_path / "cache"

    monkeypatch.setenv("TESOURO_DIRETO_CSV_URL", str(source))
    monkeypatch.setenv("TESOURO_DIRETO_CACHE_DIR", str(cache))
    data.reset_cache()

    path, stale = data.ensure_csv()
    assert path.exists()
    assert not stale
    # A second call reuses the cached file without touching the source.
    source.unlink()
    path2, stale2 = data.ensure_csv()
    assert path2 == path
    assert not stale2


def test_load_dataset_falls_back_to_stale_cache(tmp_path, monkeypatch):
    source = tmp_path / "source.csv"
    shutil.copy(FIXTURE, source)
    cache = tmp_path / "cache"

    monkeypatch.setenv("TESOURO_DIRETO_CSV_URL", str(source))
    monkeypatch.setenv("TESOURO_DIRETO_CACHE_DIR", str(cache))
    data.reset_cache()

    dataset = data.load_dataset()
    assert len(dataset.records) == 15
    assert dataset.latest_ref_date == date(2024, 6, 28)

    # Force a refresh with an unreachable source: the old cache must survive.
    source.unlink()
    monkeypatch.setenv("TESOURO_DIRETO_CSV_URL", str(tmp_path / "missing.csv"))
    dataset2 = data.load_dataset(force_download=True)
    assert dataset2.stale is True
    assert len(dataset2.records) == 15


def test_select_records():
    records = data.parse_csv(FIXTURE)
    selected = data.select_records(
        records,
        groups=["prefixado"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 29),
    )
    assert {record.asset_code for record in selected} == {"LTN 010125", "LTN 010127"}
    assert len(selected) == 5

    one = data.select_records(
        records,
        groups=None,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        asset_code="150835",
    )
    assert {record.asset_code for record in one} == {"NTN-B Principal 150835"}


def test_aggregate_monthly():
    records = data.parse_csv(FIXTURE)
    selected = data.select_records(
        records,
        groups=["prefixado"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        asset_code="010125",
    )
    monthly = data.aggregate_records(selected, "monthly")
    by_period = {row.period: row for row in monthly}

    assert by_period["2024-01"].n_obs == 2
    assert by_period["2024-01"].yield_bid_mean == pytest.approx((0.105 + 0.104) / 2)
    assert by_period["2024-01"].price_bid_mean == pytest.approx((900.10 + 910.20) / 2)
    assert by_period["2024-02"].n_obs == 1
    assert by_period["2024-06"].first_date == date(2024, 6, 28)


def test_summarize():
    records = data.parse_csv(FIXTURE)
    stats = data.summarize(records)
    ltn = next(item for item in stats if item["asset_code"] == "LTN 010125")
    assert ltn["n_obs"] == 4
    assert ltn["last_date"] == "2024-06-28"
    assert ltn["last_yield_pct"] == pytest.approx(11.0)
    assert ltn["max_yield_pct"] == pytest.approx(11.0)
    assert ltn["min_yield_pct"] == pytest.approx(10.4)
