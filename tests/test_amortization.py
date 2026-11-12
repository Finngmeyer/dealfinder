from __future__ import annotations

import pytest

from dealfinder.underwrite import balance_after, monthly_payment


def test_payment_matches_the_standard_formula():
    assert monthly_payment(360_000, 0.07, 360) == pytest.approx(2_395.09, abs=0.005)
    assert monthly_payment(471_306, 0.065, 360) == pytest.approx(2_978.97, abs=0.005)


def test_zero_rate_is_straight_line():
    assert monthly_payment(120_000, 0.0, 360) == pytest.approx(333.3333, abs=1e-4)
    assert balance_after(120_000, 0.0, 360, 90) == pytest.approx(90_000)


def test_no_principal_means_no_payment():
    assert monthly_payment(0, 0.07, 360) == 0.0
    assert balance_after(0, 0.07, 360, 12) == 0.0


def test_balance_runs_from_principal_to_zero():
    assert balance_after(360_000, 0.07, 360, 0) == pytest.approx(360_000)
    assert balance_after(360_000, 0.07, 360, 60) == pytest.approx(338_873.72, abs=0.01)
    assert balance_after(360_000, 0.07, 360, 360) == pytest.approx(0, abs=0.01)
    assert balance_after(360_000, 0.07, 360, 999) == pytest.approx(0, abs=0.01)


def test_first_year_principal():
    assert 360_000 - balance_after(360_000, 0.07, 360, 12) == pytest.approx(3_656.92, abs=0.01)


@pytest.mark.parametrize("months", [0, -12])
def test_term_must_be_positive(months):
    with pytest.raises(ValueError):
        monthly_payment(100_000, 0.07, months)
    with pytest.raises(ValueError):
        balance_after(100_000, 0.07, months, 1)
