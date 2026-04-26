"""Tests for HostedEnzymeClient against a local search service."""

import os
import pytest
import httpx
from enzyme_sdk.hosted import HostedEnzymeClient, HostedVaultStatus


# Skip all tests if the local search service isn't running
SEARCH_URL = os.environ.get("ENZYME_SEARCH_URL", "http://localhost:8766")
VAULT_SLUG = os.environ.get("ENZYME_TEST_SLUG", "f1882674-test-vault")


def _search_is_available() -> bool:
    try:
        resp = httpx.get(f"{SEARCH_URL}/health", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


skip_no_server = pytest.mark.skipif(
    not _search_is_available(),
    reason="Local search service not running",
)


@skip_no_server
class TestHostedClient:
    def setup_method(self):
        self.client = HostedEnzymeClient(
            api_key="test-key",  # no auth on local search service
            vault_slug=VAULT_SLUG,
            base_url=SEARCH_URL,
        )

    def teardown_method(self):
        self.client.close()

    def test_status(self):
        status = self.client.status()
        assert isinstance(status, HostedVaultStatus)
        assert status.docs >= 0
        assert status.entities >= 0

    def test_petri(self):
        entities = self.client.petri(top=5)
        assert isinstance(entities, list)
        # Each entity should have name, type, frequency
        for e in entities:
            assert e.name
            assert e.type

    def test_catalyze(self):
        results = self.client.catalyze("design")
        assert isinstance(results, list)

    def test_context_manager(self):
        with HostedEnzymeClient(
            api_key="test",
            vault_slug=VAULT_SLUG,
            base_url=SEARCH_URL,
        ) as client:
            status = client.status()
            assert status.docs >= 0
