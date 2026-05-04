from __future__ import annotations

import pytest

from uxv_extract.normalize import normalize_country, normalize_us_state


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("United States", "United States"),
        ("united states", "United States"),
        ("UNITED STATES", "United States"),
        ("US", "United States"),
        ("USA", "United States"),
        ("U.S.A.", "United States"),
        ("U.S.", "United States"),
        ("America", "United States"),
        ("United States of America", "United States"),
        ("  united  states  of  america  ", "United States"),
        ("UK", "United Kingdom"),
        ("U.K.", "United Kingdom"),
        ("United Kingdom", "United Kingdom"),
        ("Germany", "Germany"),
        ("Deutschland", "Germany"),
        ("Australia", "Australia"),
        ("Canada", "Canada"),
        ("Japan", "Japan"),
        ("Republic of Korea", "South Korea"),
        ("UAE", "United Arab Emirates"),
        ("South Africa", "South Africa"),
    ],
)
def test_normalize_country_known(raw: str, expected: str) -> None:
    assert normalize_country(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Bayern",
        "Pirate Island",
        "12345",
    ],
)
def test_normalize_country_unknown_passes_through(raw: str) -> None:
    """Unknown values pass through stripped — better than overwriting."""
    assert normalize_country(raw) == raw.strip()


def test_normalize_country_empty() -> None:
    assert normalize_country("") == ""
    assert normalize_country(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("VA", "Virginia"),
        ("va", "Virginia"),
        ("Va.", "Virginia"),
        ("Virginia", "Virginia"),
        ("VIRGINIA", "Virginia"),
        ("FL", "Florida"),
        ("CA", "California"),
        ("CALIFORNIA", "California"),
        ("DC", "District of Columbia"),
        ("NY", "New York"),
        ("New York", "New York"),
        ("new york", "New York"),
    ],
)
def test_normalize_us_state_known(raw: str, expected: str) -> None:
    assert normalize_us_state(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "BC",  # Canadian province — leave alone (Butler/Honeywell aren't going to be Canadian)
        "Ontario",
        "Bayern",
        "NSW",  # Australian state
        "Queensland",
    ],
)
def test_normalize_us_state_non_us_passes_through(raw: str) -> None:
    assert normalize_us_state(raw) == raw


def test_normalize_us_state_none() -> None:
    assert normalize_us_state(None) is None
    assert normalize_us_state("") == ""
