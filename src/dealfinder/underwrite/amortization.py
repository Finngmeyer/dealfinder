"""Fixed-rate loan math."""

from __future__ import annotations


def monthly_payment(principal: float, annual_rate: float, months: int) -> float:
    """Principal and interest: L x r / (1 - (1 + r)^-n), with r = annual rate / 12."""
    if months <= 0:
        raise ValueError("months must be positive")
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    if r == 0:
        return principal / months
    return principal * r / (1 - (1 + r) ** -months)


def balance_after(principal: float, annual_rate: float, months: int, payments_made: int) -> float:
    """Loan balance left after ``payments_made`` monthly payments."""
    if months <= 0:
        raise ValueError("months must be positive")
    if principal <= 0:
        return 0.0
    k = max(0, min(payments_made, months))
    r = annual_rate / 12
    if r == 0:
        return principal * (1 - k / months)
    growth = (1 + r) ** k
    payment = monthly_payment(principal, annual_rate, months)
    return max(0.0, principal * growth - payment * (growth - 1) / r)
