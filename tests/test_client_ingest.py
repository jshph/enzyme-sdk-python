import json
import subprocess
from datetime import date, datetime, timezone

from enzyme_sdk.client import EnzymeClient


def test_ingest_serializes_datetime_created_at(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"status": "ok", "documents_ingested": 1}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = EnzymeClient(enzyme_bin="enzyme-test")
    result = client.ingest(
        collection="user-123",
        entry={
            "title": "Session",
            "created_at": datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc),
        },
    )

    assert result["documents_ingested"] == 1
    assert calls[0][0] == ["enzyme-test", "--collection", "user-123", "ingest", "--quiet"]
    assert json.loads(calls[0][1]["input"]) == {
        "entry": {
            "title": "Session",
            "created_at": "2026-04-25T14:30:00+00:00",
        }
    }


def test_ingest_serializes_naive_datetime_as_utc(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ok"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    EnzymeClient().ingest(
        collection="user-123",
        entry={
            "title": "Session",
            "created_at": datetime(2026, 4, 25, 14, 30),
        },
    )

    assert json.loads(calls[0][1]["input"])["entry"]["created_at"] == (
        "2026-04-25T14:30:00+00:00"
    )


def test_ingest_serializes_date_created_at(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ok"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    EnzymeClient().ingest(
        collection="user-123",
        entry={
            "title": "Session",
            "created_at": date(2026, 4, 25),
        },
    )

    assert json.loads(calls[0][1]["input"])["entry"]["created_at"] == "2026-04-25"


def test_ingest_passes_epoch_millis_through(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ok"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    EnzymeClient().ingest(
        collection="user-123",
        entry={
            "title": "Session",
            "created_at": 1777127400000,
        },
    )

    assert json.loads(calls[0][1]["input"])["entry"]["created_at"] == 1777127400000
