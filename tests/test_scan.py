"""Synthetic end-to-end scan; no persistent state or Gmail dependency."""

from datetime import datetime, timezone
import socket
import sqlite3
import subprocess
import sys

import pytest

from dam.models import ProposedAction
from dam.scan import ScanInputError, load_synthetic_messages, run_synthetic_scan

FIXED = datetime(2026, 9, 16, tzinfo=timezone.utc)


def run(**changes):
    return run_synthetic_scan(as_of=FIXED, run_id="synthetic-fixed-run", **changes)


def test_full_pipeline_is_deterministic_and_read_only(monkeypatch):
    def forbid_storage(*_args, **_kwargs):
        raise AssertionError("Synthetic scan must not open SQLite")
    monkeypatch.setattr("dam.storage.Storage.open", forbid_storage)
    first = run()
    second = run()
    assert first == second
    assert first.effective_limit == 100
    assert first.source == "package_synthetic_fixture"
    assert first.persistence == "in_memory_only"
    p = first.preview
    assert p.fingerprint == second.preview.fingerprint
    assert p.exact_message_ids == (
        "synthetic-001-receipt", "synthetic-002-job-alert",
        "synthetic-003-security", "synthetic-004-unknown")
    assert p.statistics.total_messages == 4
    assert p.statistics.classified == 3 and p.statistics.unclassified == 1
    assert p.statistics.requiring_review == 2
    assert p.statistics.executed_gmail_actions == 0
    assert p.statistics.actual_inbox_after is None
    assert not p.authority_established and not p.executable


def test_individual_messages_same_thread_and_limit():
    result = run(limit=2)
    p = result.preview
    assert result.effective_limit == 2
    assert p.statistics.total_messages == 2
    assert p.statistics.scan_limit == 2
    assert p.statistics.inventory.completeness == "partial"
    assert p.statistics.inventory.pagination_limited is False
    assert p.entries[0].thread_id == p.entries[1].thread_id
    assert p.entries[0].message_id != p.entries[1].message_id
    assert p.entries[0].proposed_action == ProposedAction.NO_ACTION
    assert p.entries[1].proposed_action == ProposedAction.ARCHIVE
    assert not p.entries[0].executable and not p.entries[1].executable


def test_protection_archive_and_review_flow():
    entries = {entry.message_id: entry for entry in run().preview.entries}
    receipt = entries["synthetic-001-receipt"]
    assert receipt.category_ids == ("finance",)
    assert receipt.proposed_action == ProposedAction.NO_ACTION
    assert "protected" in receipt.protection_signals
    archive = entries["synthetic-002-job-alert"]
    assert archive.category_ids == ("employment_inactive",)
    assert archive.proposed_action == ProposedAction.ARCHIVE
    assert archive.approval_required and not archive.authority_established
    assert entries["synthetic-003-security"].requires_review
    assert entries["synthetic-004-unknown"].requires_review
    assert entries["synthetic-004-unknown"].category_ids == ()
    assert all(not entry.executable for entry in entries.values())


@pytest.mark.parametrize("limit", [0, -1, "2", 10_001, True, 1.5])
def test_invalid_programmatic_limit_is_rejected(limit):
    with pytest.raises(ScanInputError, match="Limit"):
        run(limit=limit)


def test_synthetic_fixture_has_only_synthetic_identities():
    messages = load_synthetic_messages()
    assert all(message.account_id == "synthetic-account" for message in messages)
    assert all("example.invalid" in message.sender for message in messages)
    assert all("INBOX" in message.label_ids for message in messages)


def test_scan_has_no_sqlite_or_network_path(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External connection or SQLite access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert run(limit=2).preview.statistics.executed_gmail_actions == 0


def test_imports_do_not_scan_create_state_or_connect(tmp_path):
    code = """
import sys
def guard(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.__new__', 'os.mkdir'):
        raise AssertionError(event)
    if event == 'open' and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError('file write')
sys.addaudithook(guard)
import dam
import dam.cli
import dam.scan
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
