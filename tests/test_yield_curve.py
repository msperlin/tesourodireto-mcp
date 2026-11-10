from datetime import date
from pathlib import Path

import pytest

from tesourodireto_mcp import yield_curve

FIXTURE = Path(__file__).parent / "fixtures" / "anbima.html"


def test_holidays_are_bundled():
    holidays = yield_curve.anbima_holidays()
    assert date(2026, 11, 2) in holidays  # Finados
    assert date(2026, 12, 25) in holidays
    assert len(holidays) > 900


def test_add_business_days_matches_anbima_calendar():
    start = date(2026, 10, 30)
    assert yield_curve.add_business_days(start, 1) == date(2026, 11, 3)
    assert yield_curve.add_business_days(start, 21) == date(2026, 12, 2)
    assert yield_curve.add_business_days(start, 252) == date(2027, 11, 4)
    assert yield_curve.add_business_days(start, 1260) == date(2031, 11, 12)
    assert yield_curve.add_business_days(start, -21) == date(2026, 9, 30)


def test_parse_yield_curve():
    points = yield_curve.parse_yield_curve(FIXTURE.read_text(encoding="utf-8"))
    assert len(points) == 12
    assert points[0].current_date == date(2026, 10, 30)
    assert points[0].n_biz_days == 252
    assert points[0].type == "real_return"
    assert points[0].value == pytest.approx(6.5334)
    assert points[0].ref_date == date(2027, 11, 4)
    assert {point.type for point in points} == set(yield_curve.CURVE_TYPES)

    nominal = next(point for point in points if point.type == "nominal_return")
    assert nominal.value == pytest.approx(14.3249)

    long_vertex = next(point for point in points if point.n_biz_days == 1008)
    assert long_vertex.type == "real_return"


def test_parse_yield_curve_rejects_wrong_page():
    with pytest.raises(yield_curve.YieldCurveError):
        yield_curve.parse_yield_curve("<html><body>maintenance</body></html>")
