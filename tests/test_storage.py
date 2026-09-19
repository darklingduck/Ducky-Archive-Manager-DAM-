"""Synthetic Step 6 data in temporary, private SQLite databases only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys

import pytest
from pydantic import ValidationError

from dam.actions import propose_action
from dam.classifier import classify
from dam.config import configuration_fingerprint, rule_scope_fingerprint
from dam.models import Configuration, MessageMetadata, Settings
from dam.storage import (
    ApprovalDescription, AuditEvent, InventoryCounts, ScanFinish, ScanStart,
    Storage, StorageError,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
TABLES = {"accounts", "labels", "config_snapshots", "rule_versions", "approvals",
          "scan_runs", "messages", "message_observations", "proposals", "audit_events",
          "source_instances", "items", "classification_work_items",
          "classification_work_members", "classification_work_events",
          "classification_evaluations", "teaching_operations", "teaching_events"}


def configuration(tmp_path):
    return Configuration.model_validate({
        "settings": {"state": {"database_path": str(tmp_path / "state" / "dam.db"),
                               "report_directory": str(tmp_path / "reports")}},
        "categories": {"categories": [{"id": "promotions", "name": "Synthetic Promotions"}]},
        "rules": {"rules": [{"id": "synthetic_offers", "version": 1,
                              "match": {"sender_domains_any": ["example.invalid"],
                                        "subject_contains_any": ["offers"]},
                              "category_ids": ["promotions"], "proposed_action": "trash",
                              "approval_ref": "synthetic_reference",
                              "notes": "Synthetic omitted configuration note."}]},
    })


def message(**changes):
    return MessageMetadata(**{
        "account_id": "synthetic_account", "message_id": "synthetic_message_a",
        "thread_id": "synthetic_thread", "sender": "news@example.invalid",
        "subject": "Synthetic weekly offers", "received_at": NOW - timedelta(days=30),
        "label_ids": ("INBOX",), **changes,
    })


def start(config, run_id="synthetic_run_a", **changes):
    return ScanStart(**{
        "run_id": run_id, "account_id": "synthetic_account",
        "config_fingerprint": configuration_fingerprint(config),
        "started_at": NOW, "as_of": NOW, "limit": 100, **changes,
    })


def results(config, metadata):
    classification = classify(metadata, config.rules, as_of=NOW, settings=config.settings)
    proposal = propose_action(metadata, classification, config.rules, as_of=NOW, settings=config.settings)
    return classification, proposal


def observe(store, config, metadata=None, run_id="synthetic_run_a", observed_at=NOW):
    metadata = metadata if metadata is not None else message()
    classification, proposal = results(config, metadata)
    store.record_observation(run_id, metadata, classification, proposal, observed_at=observed_at)
    return proposal


@pytest.fixture
def db(tmp_path):
    config = configuration(tmp_path)
    with Storage.open(config.settings) as store:
        store.save_configuration(config)
        store.start_scan(start(config))
        yield store, config


def sql_rows(path, sql, args=()):
    """Independent test inspection only; never an application API."""
    with sqlite3.connect(path) as connection:
        return connection.execute(sql, args).fetchall()


def test_import_has_no_filesystem_database_or_network_side_effect(tmp_path):
    code = '''
import sys
# Imports may read module source, but may not write, connect or create state.
def guard(event, args):
    if event in ("sqlite3.connect", "socket.connect", "socket.__new__", "os.mkdir", "os.remove", "os.rename"):
        raise AssertionError(event)
    if event == "open" and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError("file write")
sys.addaudithook(guard)
import dam
import dam.storage
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_explicit_initialization_permissions_schema_and_reopen(tmp_path):
    config = configuration(tmp_path)
    path = Path(config.settings.state.database_path)
    assert not path.exists()
    with Storage.open(config.settings) as store:
        assert store.path == path
        info = store.schema_info()
        assert info["version"] == 5 and info["foreign_keys"]
        assert set(info["tables"]) == TABLES
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sql_rows(path, "PRAGMA user_version") == [(5,)]
        assert sql_rows(path, "PRAGMA journal_mode") == [("delete",)]
    with Storage.open(config.settings) as reopened:
        assert reopened.schema_info() == info


def test_default_location_is_configured_but_not_opened():
    assert Settings().state.database_path == "~/.local/share/dam/dam.db"


@pytest.mark.parametrize("target,mode", [("directory", 0o755), ("database", 0o644)])
def test_existing_shared_permissions_rejected_without_chmod(tmp_path, target, mode):
    config = configuration(tmp_path)
    path = Path(config.settings.state.database_path)
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    selected = path.parent if target == "directory" else path
    selected.chmod(mode)
    with pytest.raises(StorageError, match="private"):
        Storage.open(config.settings)
    assert stat.S_IMODE(selected.stat().st_mode) == mode


def test_stricter_existing_mode_is_not_weakened(tmp_path):
    config = configuration(tmp_path)
    with Storage.open(config.settings) as store:
        path = store.path
    path.chmod(0o400)
    try:
        try:
            with Storage.open(config.settings):
                pass
        except StorageError:
            pass  # Read-only mode may prevent opening; never add write permission.
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    finally:
        path.chmod(0o600)


def test_unsupported_permission_semantics_fail_before_creation(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(StorageError, match="POSIX"):
        Storage.open(config.settings)
    assert not Path(config.settings.state.database_path).exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_database_cannot_be_inside_git_even_through_alias(tmp_path, symlink):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: synthetic-worktree-marker")
    location = repo
    if symlink:
        location = tmp_path / "alias"
        location.symlink_to(repo, target_is_directory=True)
    settings = Settings(state={"database_path": str(location / "private" / "dam.db")})
    with pytest.raises(StorageError, match="outside"):
        Storage.open(settings)
    assert not (repo / "private").exists()


def test_explicit_repository_root_without_git_marker(tmp_path):
    root = tmp_path / "checkout"
    settings = Settings(state={"database_path": str(root / "private" / "dam.db")})
    with pytest.raises(StorageError, match="outside"):
        Storage.open(settings, repository_root=root)
    assert not root.exists()


def test_symlink_and_hardlink_files_rejected(tmp_path):
    config = configuration(tmp_path)
    path = Path(config.settings.state.database_path)
    path.parent.mkdir(mode=0o700)
    target = tmp_path / "synthetic_target"
    target.touch(mode=0o600)
    path.symlink_to(target)
    with pytest.raises(StorageError, match="symlink"):
        Storage.open(config.settings)
    path.unlink()
    os.link(target, path)
    with pytest.raises(StorageError, match="hard links"):
        Storage.open(config.settings)
    assert target.read_bytes() == b""


def test_unknown_schema_and_unversioned_existing_tables_are_not_replaced(tmp_path):
    config = configuration(tmp_path)
    path = Path(config.settings.state.database_path)
    with Storage.open(config.settings):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(StorageError, match="Unsupported"):
        Storage.open(config.settings)
    assert sql_rows(path, "PRAGMA user_version") == [(99,)]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 0")
    with pytest.raises(StorageError, match="unversioned"):
        Storage.open(config.settings)


def test_account_configuration_and_rule_provenance_idempotency(db):
    store, config = db
    for _ in range(2):
        store.ensure_account("synthetic_account")
        fingerprint = store.save_configuration(config)
    assert store.accounts() == ("synthetic_account",)
    assert sql_rows(store.path, "SELECT count(*) FROM config_snapshots") == [(1,)]
    assert sql_rows(store.path, "SELECT count(*) FROM rule_versions") == [(1,)]
    assert store.configuration(fingerprint)["semantic_fingerprint"] == configuration_fingerprint(config, semantic=True)
    rule = store.rule_versions(fingerprint)[0]
    assert rule["rule_id"] == "synthetic_offers" and rule["version"] == 1
    assert rule["scope_fingerprint"] == rule_scope_fingerprint(config.rules.rules[0])
    assert "notes" not in rule["provenance"]
    assert "match" not in rule["provenance"]


def test_changed_configuration_and_rule_versions_preserve_history(db):
    store, config = db
    raw = config.model_dump()
    raw["rules"]["rules"][0]["version"] = 2
    newer = Configuration.model_validate(raw)
    original_id = store.save_configuration(config)
    new_id = store.save_configuration(newer)
    assert original_id != new_id
    assert store.rule_versions(original_id)[0]["version"] == 1
    assert store.rule_versions(new_id)[0]["version"] == 2
    assert store.scan("synthetic_run_a").start.config_fingerprint == original_id


def test_two_messages_same_thread_and_same_message_two_scans(db):
    store, config = db
    observe(store, config)
    observe(store, config, message(message_id="synthetic_message_b"))
    store.start_scan(start(config, "synthetic_run_b", started_at=NOW + timedelta(days=1)))
    observe(store, config, message(label_ids=(), subject="Changed synthetic offers"),
            run_id="synthetic_run_b", observed_at=NOW + timedelta(days=1))
    identities = store.messages("synthetic_account")
    assert len(identities) == 2
    assert {m["thread_id"] for m in identities} == {"synthetic_thread"}
    observations = store.observations("synthetic_account", "synthetic_message_a")
    assert [o.run_id for o in observations] == ["synthetic_run_a", "synthetic_run_b"]
    assert observations[0].metadata.label_ids == ("INBOX",)
    assert observations[1].metadata.label_ids == ()
    assert observations[0].metadata.subject != observations[1].metadata.subject
    assert store.scan("synthetic_run_a").observed_unique_messages == 2
    assert store.scan("synthetic_run_b").observed_unique_messages == 1
    assert store.labels("synthetic_account") == ("INBOX",)


def test_account_scoping_preserves_identical_message_ids(db):
    store, config = db
    observe(store, config)
    store.start_scan(start(config, "other_run", account_id="other_synthetic_account"))
    observe(store, config, message(account_id="other_synthetic_account"), run_id="other_run")
    assert len(store.messages("synthetic_account")) == 1
    assert len(store.messages("other_synthetic_account")) == 1


def test_observation_proposal_round_trip_retries_and_immutability(db):
    store, config = db
    expected = observe(store, config)
    observe(store, config)
    loaded, = store.proposals("synthetic_run_a")
    assert loaded == expected
    assert loaded.proposed_action == "trash"
    assert loaded.approval_required and loaded.approval_type == "destructive"
    assert loaded.approval_status == "reference_unverified"
    assert not loaded.authority_established and not loaded.executable
    assert len(store.observations("synthetic_account", "synthetic_message_a")) == 1
    assert store.observations("synthetic_account", "synthetic_message_a")[0].classification == results(config, message())[0]
    with pytest.raises(ValidationError):
        loaded.executable = True
    with pytest.raises(StorageError, match="history"):
        observe(store, config, message(subject="Different synthetic offers"))
    assert store.proposals("synthetic_run_a") == (expected,)


def test_missing_labels_stay_unknown_after_round_trip(db):
    store, config = db
    raw = message().model_dump()
    del raw["label_ids"]
    observe(store, config, MessageMetadata(**raw))
    stored = store.observations("synthetic_account", "synthetic_message_a")[0].metadata
    assert "label_ids" not in stored.model_fields_set
    assert stored.label_ids == ()


def test_scan_finish_counts_and_history(db):
    store, config = db
    store.start_scan(start(config))
    observe(store, config)
    finish = ScanFinish(ended_at=NOW + timedelta(seconds=10), status="completed",
                        inventory=InventoryCounts(label_total=20, estimated_total=18, pages_read=1,
                                                  pagination_limited=True, completeness="partial",
                                                  discrepancy="unresolved"))
    store.finish_scan("synthetic_run_a", finish)
    store.finish_scan("synthetic_run_a", finish)
    observe(store, config)  # Exact retry remains idempotent after completion.
    scan = store.scan("synthetic_run_a")
    assert scan.finish == finish
    assert scan.observed_unique_messages == 1
    assert scan.count_provenance == "stored_unique_observations"
    assert scan.start.limit == 100 and scan.start.mode == "dry_run"
    with pytest.raises(StorageError, match="finished"):
        observe(store, config, message(message_id="new_synthetic_message"))
    with pytest.raises(StorageError, match="history"):
        store.finish_scan("synthetic_run_a", finish.model_copy(update={"status": "failed"}))
    with pytest.raises(StorageError, match="provenance"):
        store.start_scan(start(config, limit=50))


def test_approval_records_are_append_only_unverified_descriptions(db):
    store, config = db
    original = observe(store, config)
    description = ApprovalDescription(record_id="synthetic_description_1",
        approval_ref="synthetic_reference", account_id="synthetic_account",
        config_fingerprint=configuration_fingerprint(config), recorded_at=NOW,
        source_id="synthetic_user", reported_status="reported_approved")
    store.record_approval(description)
    store.record_approval(description)
    loaded, = store.approvals("synthetic_account")
    assert loaded == description
    assert not loaded.authority_established and loaded.validation_status == "unverified"
    assert store.proposals("synthetic_run_a") == (original,)
    assert not store.proposals("synthetic_run_a")[0].executable
    with pytest.raises(StorageError, match="history"):
        store.record_approval(description.model_copy(update={"reported_status": "reported_revoked"}))
    store.record_approval(description.model_copy(update={"record_id": "synthetic_description_2",
                                                        "reported_status": "reported_revoked"}))
    assert len(store.approvals("synthetic_account")) == 2


def test_append_audit_preview_without_fabricating_completed_actions(db):
    store, config = db
    observe(store, config)
    assert store.audit_events("synthetic_run_a") == ()
    event = AuditEvent(event_id="synthetic_event_1", run_id="synthetic_run_a",
                       message_id="synthetic_message_a", recorded_at=NOW,
                       event_type="proposal", state="proposed")
    store.append_audit_event(event)
    store.append_audit_event(event)
    store.append_audit_event(event.model_copy(update={"event_id": "synthetic_event_2", "event_type": "preview"}))
    assert len(store.audit_events("synthetic_run_a")) == 2
    assert store.audit_events("synthetic_run_a")[0] == event
    for loaded in store.audit_events("synthetic_run_a"):
        assert loaded.state == "proposed" and loaded.mode == "dry_run"
        assert not loaded.mailbox_modified and not loaded.subscription_changed
    with pytest.raises(StorageError, match="history"):
        store.append_audit_event(event.model_copy(update={"recorded_at": NOW + timedelta(seconds=1)}))


@pytest.mark.parametrize("change", [
    {"state": "applied"}, {"event_type": "gmail_action_completed"}, {"mode": "live"},
    {"mailbox_modified": True}, {"subscription_changed": True},
])
def test_completed_or_effectful_audit_records_rejected(db, change):
    store, _ = db
    raw = dict(event_id="synthetic_event", run_id="synthetic_run_a", recorded_at=NOW,
               event_type="preview", state="proposed")
    with pytest.raises(ValidationError):
        AuditEvent(**{**raw, **change})
    forged = AuditEvent(**raw).model_copy(update=change)
    with pytest.raises(StorageError, match="Invalid"):
        store.append_audit_event(forged)
    assert store.audit_events("synthetic_run_a") == ()


@pytest.mark.parametrize("field", [
    "oauth_token", "oauth_client_secret", "password", "api_key", "body", "snippet",
    "attachments", "raw_gmail_response", "remote_content", "unsubscribe_url", "recipients",
])
def test_prohibited_content_fields_have_no_storage_path(db, field):
    store, config = db
    raw = message().model_dump()
    with pytest.raises(ValidationError):
        MessageMetadata(**{**raw, field: "synthetic_secret_or_content"})
    with pytest.raises(StorageError, match="typed record"):
        store.record_observation("synthetic_run_a", {**raw, field: "synthetic_secret_or_content"},
                                 *results(config, message()), observed_at=NOW)
    with pytest.raises(ValidationError):
        AuditEvent(event_id="synthetic_event", run_id="synthetic_run_a", recorded_at=NOW,
                   event_type="preview", state="proposed", **{field: "synthetic_secret_or_content"})
    assert b"synthetic_secret_or_content" not in store.path.read_bytes()


def test_notes_and_paths_omitted_and_no_arbitrary_objects(db):
    store, config = db
    assert b"Synthetic omitted configuration note" not in store.path.read_bytes()
    assert str(store.path).encode() not in store.path.read_bytes()
    with pytest.raises(StorageError, match="typed record"):
        store.save_configuration(object())
    with pytest.raises(StorageError, match="typed record"):
        store.append_audit_event({"payload": object()})


def test_forged_executable_proposal_rejected(db):
    store, config = db
    classification, proposal = results(config, message())
    for field in ("authority_established", "executable"):
        forged = proposal.model_copy(update={field: True})
        with pytest.raises(StorageError, match="Invalid"):
            store.record_observation("synthetic_run_a", message(), classification, forged, observed_at=NOW)
    assert store.messages("synthetic_account") == ()


def test_sql_like_metadata_is_parameterized_not_executed(db):
    store, config = db
    sqlish = "synthetic'); DROP TABLE messages; --"
    observe(store, config, message(message_id=sqlish, subject=sqlish + " offers"))
    loaded, = store.observations("synthetic_account", sqlish)
    assert loaded.metadata.message_id == sqlish
    assert loaded.metadata.subject == sqlish + " offers"
    assert set(store.schema_info()["tables"]) == TABLES
    assert store.observations(sqlish, sqlish) == ()


def test_transaction_failure_rolls_back_identity_labels_observation_and_proposal(db):
    store, config = db
    # Inject a late SQLite failure after identity, label, and observation inserts.
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON proposals
                              BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    with pytest.raises(StorageError, match="no logical write"):
        observe(store, config)
    assert store.messages("synthetic_account") == ()
    assert store.labels("synthetic_account") == ()
    assert store.observations("synthetic_account", "synthetic_message_a") == ()
    assert store.proposals("synthetic_run_a") == ()
    assert store.scan("synthetic_run_a").observed_unique_messages == 0
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER synthetic_failure")
    observe(store, config)  # Connection is usable after rollback.


def test_foreign_key_failure_rolls_back_implicit_account(db):
    store, config = db
    invalid = start(config, "synthetic_bad_run", account_id="synthetic_new_account", config_fingerprint="0" * 64)
    with pytest.raises(StorageError, match="transaction failed"):
        store.start_scan(invalid)
    assert store.accounts() == ("synthetic_account",)
    assert store.scan("synthetic_bad_run") is None


def test_configuration_transaction_is_atomic(db):
    store, config = db
    raw = config.model_dump()
    raw["settings"]["policy_version"] = 2
    config2 = Configuration.model_validate(raw)
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON rule_versions
                              BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    with pytest.raises(StorageError):
        store.save_configuration(config2)
    assert store.configuration(configuration_fingerprint(config2)) is None
    assert store.rule_versions(configuration_fingerprint(config2)) == ()


def test_deterministic_json_and_config_order(db):
    store, config = db
    observe(store, config)
    serialized, = sql_rows(store.path, "SELECT proposal_json FROM proposals")[0]
    assert serialized == json.dumps(json.loads(serialized), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = config.model_dump()
    raw["rules"]["rules"] = tuple(reversed(raw["rules"]["rules"]))
    assert store.save_configuration(Configuration.model_validate(raw)) == configuration_fingerprint(config)
    assert sql_rows(store.path, "SELECT count(*) FROM config_snapshots") == [(1,)]
    shifted = NOW.astimezone(timezone(timedelta(hours=-4)))
    store.start_scan(start(config, started_at=shifted, as_of=shifted))  # same UTC instant


def test_no_pickle_network_or_action_execution(db, monkeypatch):
    import pickle
    store, config = db
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external behavior")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(pickle, "dumps", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    observe(store, config)
    assert not store.proposals("synthetic_run_a")[0].executable


def test_observation_provenance_mismatch_does_not_write(db):
    store, config = db
    metadata = message()
    classification, proposal = results(config, metadata)
    for changed_classification, changed_proposal in (
        (replace(classification, account_id="different_synthetic_account"), proposal),
        (replace(classification, policy_version=99), proposal),
        (replace(classification, assessments=()), proposal),
        (classification, proposal.model_copy(update={"supporting_rules": (("unknown_rule", 1),)})),
    ):
        with pytest.raises(StorageError):
            store.record_observation("synthetic_run_a", metadata, changed_classification,
                                     changed_proposal, observed_at=NOW)
    assert store.messages("synthetic_account") == ()


def test_retrieval_survives_reopen_and_returns_no_rows_for_unknowns(db):
    store, config = db
    expected = observe(store, config)
    with Storage.open(config.settings) as reopened:
        assert reopened.proposals("synthetic_run_a", "synthetic_message_a") == (expected,)
        assert reopened.scan("absent") is None
        assert reopened.configuration("0" * 64) is None
        assert reopened.proposals("absent") == ()
        assert reopened.audit_events("absent") == ()
        assert reopened.observations("absent", "absent") == ()


def test_unknown_existing_schema_one_is_not_trusted(tmp_path):
    config = configuration(tmp_path)
    with Storage.open(config.settings) as store:
        path = store.path
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE approvals")
    with pytest.raises(StorageError, match="tables"):
        Storage.open(config.settings)


def test_private_sqlite_companions_required_before_open(tmp_path):
    config = configuration(tmp_path)
    with Storage.open(config.settings) as store:
        path = store.path
    journal = Path(str(path) + "-journal")
    journal.touch(mode=0o644)
    journal.chmod(0o644)
    with pytest.raises(StorageError, match="private"):
        Storage.open(config.settings)
    assert stat.S_IMODE(journal.stat().st_mode) == 0o644


def test_storage_fails_cleanly_after_close(tmp_path):
    store = Storage.open(configuration(tmp_path).settings)
    store.close()
    with pytest.raises(StorageError, match="read"):
        store.accounts()
    with pytest.raises(StorageError, match="unavailable"):
        store.ensure_account("synthetic_account")


def test_scan_time_and_limit_boundaries(db):
    store, config = db
    with pytest.raises(StorageError, match="timezone-aware"):
        observe(store, config, observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(StorageError, match="precedes"):
        observe(store, config, observed_at=NOW - timedelta(seconds=1))
    observe(store, config, observed_at=NOW + timedelta(seconds=2))
    with pytest.raises(StorageError, match="precedes"):
        store.finish_scan("synthetic_run_a", ScanFinish(ended_at=NOW, status="completed"))
    assert store.scan("synthetic_run_a").finish is None
    store.start_scan(start(config, "limited_run", limit=1))
    observe(store, config, run_id="limited_run")
    with pytest.raises(StorageError, match="limit"):
        observe(store, config, message(message_id="synthetic_message_b"), run_id="limited_run")
    assert len(store.messages("synthetic_account")) == 1


def test_retrieval_snapshots_cannot_modify_database(db):
    store, config = db
    fingerprint = configuration_fingerprint(config)
    fetched = store.configuration(fingerprint)
    fetched["provenance"]["policy_version"] = 99
    assert store.configuration(fingerprint)["provenance"]["policy_version"] == 1
    observe(store, config)
    identities = store.messages("synthetic_account")
    identities[0]["thread_id"] = "changed"
    assert store.messages("synthetic_account")[0]["thread_id"] == "synthetic_thread"


def test_actual_git_directory_blocks_storage(tmp_path):
    root = tmp_path / "synthetic_checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    settings = Settings(state={"database_path": str(root / "state" / "dam.db")})
    with pytest.raises(StorageError, match="outside"):
        Storage.open(settings)
    assert not (root / "state").exists()


def test_retention_constraints_survive_round_trip(tmp_path):
    raw = configuration(tmp_path).model_dump()
    raw["rules"]["rules"][0]["retention"] = {"duration_days": None}
    config = Configuration.model_validate(raw)
    with Storage.open(config.settings) as store:
        store.save_configuration(config)
        store.start_scan(start(config))
        assert store.scan("synthetic_run_a").status == "running"
        expected = observe(store, config)
        loaded, = store.proposals("synthetic_run_a")
        assert loaded == expected
        assert loaded.proposed_action == "mark_review"
        assert loaded.retention_constraints[0].state == "indefinite"
        assert loaded.retention_constraints[0].duration_days is None
        assert loaded.protection_signals and loaded.review_reasons
        store.finish_scan("synthetic_run_a", ScanFinish(ended_at=NOW, status="completed"))
        assert store.scan("synthetic_run_a").status == "completed"
