"""Shared test fixtures: the fixtures directory and readers for its recorded device responses."""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    """Path to tests/fixtures/ (recorded AiSEG2 responses)."""
    return FIXTURES


@pytest.fixture(scope="session")
def read_text(fixtures_dir: pathlib.Path) -> Callable[[str], str]:
    """Return a reader that loads a fixture file as UTF-8 text."""

    def _read(name: str) -> str:
        return (fixtures_dir / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture(scope="session")
def read_bytes(fixtures_dir: pathlib.Path) -> Callable[[str], bytes]:
    """Return a reader that loads a fixture file as raw bytes (for BOM-sensitive CSVs)."""

    def _read(name: str) -> bytes:
        return (fixtures_dir / name).read_bytes()

    return _read
