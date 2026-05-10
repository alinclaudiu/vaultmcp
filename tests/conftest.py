"""Pytest configuration shared across tests."""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "e2e: end-to-end test requiring a running Postgres"
    )
