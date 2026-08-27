"""Skips the whole suite (rather than failing) when API_TEST_BASE_URL /
API_TEST_TOKEN aren't set. This suite talks to a real deployment, so
"credentials not configured" is an expected local/CI state, not a
failure. See README.md."""

import pytest

from api_tests.client import client_from_env


@pytest.fixture
def client():
    c = client_from_env()
    if c is None:
        pytest.skip("API_TEST_BASE_URL / API_TEST_TOKEN not set; see api_tests/README.md")
    return c
