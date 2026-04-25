import json
import subprocess

import pytest

from enzyme_sdk.client import EnzymeClient, EnzymeError


def test_embed_entries_sends_json_payload(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"model": "ese", "dimension": 512, "items": []}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = EnzymeClient(enzyme_bin="enzyme-test", timeout=12)
    result = client.embed_entries([{"id": "a", "title": "Black rice"}])

    assert result["model"] == "ese"
    assert calls[0][0] == ["enzyme-test", "embed-entries"]
    assert calls[0][1]["timeout"] == 12
    assert json.loads(calls[0][1]["input"]) == {
        "entries": [{"id": "a", "title": "Black rice"}]
    }


def test_embed_entries_accepts_single_entry(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout='{"items":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = EnzymeClient()
    assert client.embed_entries(entry={"id": "a"}) == {"items": []}


def test_embed_entries_requires_exactly_one_payload():
    client = EnzymeClient()

    with pytest.raises(ValueError, match="exactly one"):
        client.embed_entries()

    with pytest.raises(ValueError, match="exactly one"):
        client.embed_entries([], entry={})


def test_embed_entries_raises_enzyme_error_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="bad input")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(EnzymeError, match="bad input"):
        EnzymeClient().embed_entries([])
