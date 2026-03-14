"""Tests for VaR calculator (KP-08)."""

import math

from pmm1.analytics.var_calculator import (
    VaRReporter,
    portfolio_var_95,
    position_var_95,
)


def test_single_var():
    """size=100, price=0.6 -> VaR = 100 * max(0.6, 0.4) = 60."""
    var = position_var_95(100.0, 0.6)
    assert var == 60.0


def test_portfolio_independent():
    """rho=0 -> sqrt(sum(var^2))."""
    positions = [
        {"size": 100.0, "price": 0.6},
        {"size": 50.0, "price": 0.7},
    ]
    var = portfolio_var_95(positions, rho=0.0)
    # Individual VaRs: 100*0.6=60, 50*0.7=35
    expected = math.sqrt(60**2 + 35**2)
    assert abs(var - expected) < 0.001


def test_portfolio_correlated():
    """rho=0.5 -> higher than independent."""
    positions = [
        {"size": 100.0, "price": 0.6},
        {"size": 50.0, "price": 0.7},
    ]
    var_ind = portfolio_var_95(positions, rho=0.0)
    var_corr = portfolio_var_95(positions, rho=0.5)
    assert var_corr > var_ind


def test_empty_portfolio():
    """Empty portfolio -> 0."""
    var = portfolio_var_95([], rho=0.05)
    assert var == 0.0


def test_var_reporter_basic():
    """VaRReporter returns expected structure."""
    reporter = VaRReporter()
    positions = [
        {"size": 100.0, "price": 0.6},
        {"size": 50.0, "price": 0.7},
    ]
    report = reporter.compute_report(positions)
    assert report["position_count"] == 2
    assert report["total_var_95"] > 0
    assert len(report["individual_vars"]) == 2


def test_var_reporter_empty():
    """VaRReporter with no positions."""
    reporter = VaRReporter()
    report = reporter.compute_report([])
    assert report["total_var_95"] == 0.0
    assert report["position_count"] == 0
