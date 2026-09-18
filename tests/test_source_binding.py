"""Step 14A verified Gmail source binding, entirely mocked and local."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dam.auth import ALLOWED_SCOPES
from dam.classification_queue import ClassificationQueueService
from dam.gmail import (
    GMAIL_READONLY_SCOPE, GmailAdapterError, GmailProfile, GmailReadResult,
    normalize_profile_address, read_authenticated_profile,
)
from dam.identifiers import new_object_id
from dam.items import SourceInstance
from dam.models import MessageMetadata, Settings
from dam.scan import GMAIL_ACCOUNT_ID, MAX_INITIAL_GMAIL_LIMIT, default_config_directory, run_gmail_scan
from dam.review import review_gmail_message
from dam.source_binding import SourceBindingError, SourceBindingService
from dam.storage import Storage, StorageError, _SCHEMA, _SCHEMA_V2
import dam.storage as storage_module


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ProfileOnlyUsers:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def getProfile(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.response)


class ProfileOnlyService:
    def __init__(self, response):
        self.api = ProfileOnlyUsers(response)

    def users(self):
        return self.api


@pytest.fixture
def state(tmp_path):
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    with Storage.open(settings) as store:
        yield store


def profile(address="person@example.invalid"):
    service = ProfileOnlyService({"emailAddress": address})
    return read_authenticated_profile(service)


def test_profile_lookup_uses_authenticated_me_read_only_and_discards_other_fields():
    service = ProfileOnlyService({"emailAddress": "Person@EXAMPLE.invalid"})
    result = read_authenticated_profile(service)
    assert result.email_address == "Person@example.invalid"
    assert service.api.calls == [{"userId": "me", "fields": "emailAddress"}]
    assert not hasattr(service.api, "messages")
    assert not hasattr(service.api, "modify")
    assert "Person" not in repr(result)
    assert ALLOWED_SCOPES == (GMAIL_READONLY_SCOPE,) == (
        "https://www.googleapis.com/auth/gmail.readonly",)


@pytest.mark.parametrize("response", [
    None, [], "person@example.invalid", {}, {"emailAddress": None},
    {"emailAddress": ""}, {"emailAddress": "   "},
    {"emailAddress": "missing-at-sign"}, {"emailAddress": "person@@example.invalid"},
    {"emailAddress": "person@example.invalid "},
    {"emailAddress": "person@example.invalid", "historyId": "123"},
    {"historyId": "123"},
])
def test_bad_profile_data_fails_without_source_binding(state, response):
    service = ProfileOnlyService(response)
    with pytest.raises((GmailAdapterError, ValidationError)):
        read_authenticated_profile(service)
    assert state._rows("SELECT * FROM source_instances") == []


def test_failed_profile_api_call_has_safe_error_and_no_source(state):
    service = ProfileOnlyService(RuntimeError("synthetic-token-secret"))
    with pytest.raises(GmailAdapterError) as error:
        read_authenticated_profile(service)
    assert "synthetic-token-secret" not in str(error.value)
    assert state._rows("SELECT * FROM source_instances") == []


def test_hostile_mapping_response_fails_as_safe_profile_error(state):
    class HostileDict(dict):
        def __iter__(self):
            raise RuntimeError("synthetic-secret-from-response")

    service = ProfileOnlyService(HostileDict(emailAddress="person@example.invalid"))
    with pytest.raises(GmailAdapterError) as error:
        read_authenticated_profile(service)
    assert "synthetic-secret-from-response" not in str(error.value)
    assert state._rows("SELECT * FROM source_instances") == []


def test_normalization_preserves_local_part_dots_plus_and_distinguishes_address_change():
    assert normalize_profile_address("First.Last+tag@EXAMPLE.invalid") == "First.Last+tag@example.invalid"
    assert normalize_profile_address("First.Last@example.invalid") != normalize_profile_address(
        "FirstLast@example.invalid")
    assert normalize_profile_address("first+tag@example.invalid") != normalize_profile_address(
        "first@example.invalid")
    assert normalize_profile_address("First@example.invalid") != normalize_profile_address(
        "first@example.invalid")


def test_same_mailbox_reuses_src_different_mailbox_does_not(state, monkeypatch):
    binding = SourceBindingService(state)
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: pytest.fail("binding printed"))
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: pytest.fail("binding prompted"))
    first = binding.bind_gmail_profile(profile("person@EXAMPLE.invalid"))
    assert first.provider == "gmail" and first.identity_status == "verified"
    assert first.source_identity == "person@example.invalid"
    assert binding.bind_gmail_profile(profile("person@example.invalid")) == first
    other = binding.bind_gmail_profile(profile("other@example.invalid"))
    assert other.source_instance_id != first.source_instance_id
    assert state.source_instance_by_identity("gmail", "person@example.invalid") == first
    assert state._rows("SELECT count(*) AS n FROM source_instances")[0]["n"] == 2
    # A changed primary profile address is a new native identity, never a merge.
    renamed = binding.bind_gmail_profile(profile("renamed@example.invalid"))
    assert renamed.source_instance_id not in (first.source_instance_id, other.source_instance_id)
    assert state.source_instance(first.source_instance_id) == first


def test_token_client_or_asserted_address_cannot_define_binding(state):
    binding = SourceBindingService(state)
    verified = profile("person@example.invalid")
    first = binding.bind_gmail_profile(verified)
    # The operation has no token/client parameters. Rebinding the same API
    # profile after either changes still resolves the original source.
    assert binding.bind_gmail_profile(profile("person@example.invalid")) == first
    for claimed in ("person@example.invalid", {"emailAddress": "person@example.invalid"}, object()):
        with pytest.raises(SourceBindingError):
            binding.bind_gmail_profile(claimed)
    with pytest.raises(SourceBindingError):
        binding.bind_gmail_profile(GmailProfile(email_address="configured@example.invalid"))
    with pytest.raises(SourceBindingError):
        binding.bind_gmail_profile(verified.model_copy(update={"email_address": "other@example.invalid"}))
    altered = verified.model_copy(update={"email_address": "other@example.invalid"})
    object.__setattr__(altered, "_adapter_address", altered.email_address)
    with pytest.raises(SourceBindingError):
        binding.bind_gmail_profile(altered)
    with pytest.raises(SourceBindingError):
        binding.bind_gmail_profile(verified.model_copy(update={"email_address": "other@bad domain"}))
    assert state._rows("SELECT count(*) AS n FROM source_instances")[0]["n"] == 1


def test_prohibited_placeholder_status_and_secret_fields(state):
    with pytest.raises(ValidationError):
        SourceInstance(source_instance_id=new_object_id("SRC"), provider="gmail",
                       identity_status="synthetic", source_identity="person@example.invalid")
    with pytest.raises(ValidationError):
        SourceInstance(source_instance_id=new_object_id("SRC"), provider="gmail",
                       identity_status="verified", source_identity=GMAIL_ACCOUNT_ID)
    with pytest.raises(ValidationError):
        SourceInstance.model_validate({"source_instance_id": new_object_id("SRC"),
            "provider": "gmail", "identity_status": "verified",
            "source_identity": "person@example.invalid", "refresh_token": "synthetic-secret"})
    asserted = SourceInstance(source_instance_id=new_object_id("SRC"), provider="gmail",
                              identity_status="verified", source_identity="person@example.invalid")
    with pytest.raises(StorageError, match="Verified Gmail profile"):
        state.register_source_instance(asserted)
    with pytest.raises(StorageError, match="Verified Gmail profile"):
        state.register_source_instance(asserted,
            verified_profile=GmailProfile(email_address="person@example.invalid"))
    with pytest.raises(sqlite3.IntegrityError):
        state._connection.execute("INSERT INTO source_instances VALUES (?, 'gmail', 'synthetic', ?)",
                                  (new_object_id("SRC"), "person@example.invalid"))
    assert state._rows("SELECT * FROM source_instances") == []


def test_binding_persists_only_source_record_and_leaves_queue_empty(state):
    source = SourceBindingService(state).bind_gmail_profile(profile())
    assert "person@example.invalid" not in repr(source)
    assert state.source_instance(source.source_instance_id) == source
    assert state._rows("SELECT * FROM items") == []
    for table in ("classification_work_items", "classification_work_members", "classification_work_events"):
        assert state._rows(f"SELECT * FROM {table}") == []
    assert ClassificationQueueService(state).list_work() == ()
    assert GMAIL_ACCOUNT_ID == "gmail-account-unverified"
    assert MAX_INITIAL_GMAIL_LIMIT == 10
    with pytest.raises(ValidationError):
        SourceInstance(source_instance_id=new_object_id("SRC"), source_identity=GMAIL_ACCOUNT_ID)


def test_source_persistence_failure_creates_no_binding_or_work(state, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Storage, "register_source_instance",
                      lambda *_a, **_k: (_ for _ in ()).throw(StorageError("synthetic failure")))
        with pytest.raises(SourceBindingError, match="Cannot persist verified Gmail source"):
            SourceBindingService(state).bind_gmail_profile(profile())
    assert state._rows("SELECT * FROM source_instances") == []
    assert state._rows("SELECT * FROM items") == []
    assert state._rows("SELECT * FROM classification_work_items") == []


def test_current_gmail_scan_and_review_do_not_bind_or_persist_items(state, monkeypatch, tmp_path):
    catalog_path = tmp_path / ".config" / "dam" / "categories.yaml"
    session = SimpleNamespace(summary=SimpleNamespace(source="existing"))
    service = object()
    empty = GmailReadResult(account_id=GMAIL_ACCOUNT_ID, requested_limit=1,
                            messages=(), failures=(), listed_message_ids=(),
                            pages_read=1, listing_complete=True, stopped_at_limit=False,
                            coverage="complete")
    monkeypatch.setattr("dam.scan.authenticate", lambda *_a, **_k: session)
    monkeypatch.setattr("dam.scan.build_gmail_service", lambda *_a, **_k: service)
    monkeypatch.setattr("dam.scan.read_inbox", lambda actual, **kwargs: empty
                        if actual is service and kwargs == {"account_id": GMAIL_ACCOUNT_ID, "limit": 1}
                        else pytest.fail("unexpected scan read"))
    monkeypatch.setattr("dam.review.authenticate", lambda *_a, **_k: session)
    monkeypatch.setattr("dam.review.build_gmail_service", lambda *_a, **_k: service)
    message = MessageMetadata(account_id=GMAIL_ACCOUNT_ID, message_id="synthetic-id",
                              received_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
                              label_ids=("INBOX",))
    monkeypatch.setattr("dam.review.read_message", lambda actual, **kwargs: message
                        if actual is service and kwargs == {"account_id": GMAIL_ACCOUNT_ID,
                                                          "message_id": "synthetic-id"}
                        else pytest.fail("unexpected review read"))
    with monkeypatch.context() as patch:
        patch.setattr(Storage, "open", lambda *_a, **_k: pytest.fail("unexpected durable storage"))
        scan = run_gmail_scan(limit=1, config_directory=default_config_directory(),
                              category_catalog_path=catalog_path)
        review = review_gmail_message("synthetic-id", config_directory=default_config_directory(),
                                       category_catalog_path=catalog_path)
    assert scan.persistence == "in_memory_only" and review.message.message_id == "synthetic-id"
    assert state._rows("SELECT * FROM source_instances") == []
    assert state._rows("SELECT * FROM items") == []
    assert state._rows("SELECT * FROM classification_work_items") == []


def test_concurrent_binding_same_mailbox_is_idempotent(tmp_path):
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    with Storage.open(settings):
        pass

    def bind(_):
        with Storage.open(settings) as store:
            return SourceBindingService(store).bind_gmail_profile(profile()).source_instance_id

    with ThreadPoolExecutor(max_workers=4) as workers:
        ids = list(workers.map(bind, range(8)))
    assert len(set(ids)) == 1
    with Storage.open(settings) as store:
        assert store._rows("SELECT count(*) AS n FROM source_instances")[0]["n"] == 1


def _populate_v2(path):
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    src, item, work = new_object_id("SRC"), new_object_id("ITEM"), new_object_id("CWQ")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in (*_SCHEMA, *_SCHEMA_V2):
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.execute("INSERT INTO accounts VALUES ('synthetic-legacy-account')")
        connection.execute("INSERT INTO messages VALUES ('synthetic-legacy-account', 'old-message', NULL)")
        connection.execute("INSERT INTO source_instances VALUES (?, 'synthetic', 'synthetic', 'synthetic-account')", (src,))
        connection.execute("INSERT INTO items VALUES (?, ?, 'email', 'native-message')", (item, src))
        timestamp = datetime(2026, 9, 18, tzinfo=timezone.utc).isoformat()
        connection.execute("INSERT INTO classification_work_items VALUES (?, ?, 'pending', ?, ?)",
                           (work, item, timestamp, timestamp))
        connection.execute("INSERT INTO classification_work_members VALUES (?, ?, 'pending', ?)",
                           (work, item, timestamp))
        connection.execute("""INSERT INTO classification_work_events
            (work_id, item_id, event_type, occurred_at, new_state)
            VALUES (?, ?, 'created', ?, 'pending')""", (work, item, timestamp))
    return src, item, work


def test_v2_to_v3_preserves_existing_history_and_is_idempotent(tmp_path):
    path = tmp_path / "state" / "dam.db"
    src, item, work = _populate_v2(path)
    tables = ("source_instances", "items", "classification_work_items",
              "classification_work_members", "classification_work_events", "messages")
    with sqlite3.connect(path) as connection:
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    settings = Settings.model_validate({"state": {"database_path": str(path)}})
    with Storage.open(settings) as store:
        assert store.schema_info()["version"] == 3
        assert store.schema_info()["foreign_keys"] is True
        assert store.source_instance(src).source_identity == "synthetic-account"
        assert store.item(item).source_instance_id == src
        assert store.classification_work(work).representative_item_id == item
        assert len(store.classification_work_events(work)) == 1
        assert store.messages("synthetic-legacy-account")[0]["message_id"] == "old-message"
        assert store._rows("SELECT count(*) AS n FROM source_instances WHERE provider='gmail'")[0]["n"] == 0
        with sqlite3.connect(path) as connection:
            assert {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                    for table in tables} == before
        assert SourceBindingService(store).bind_gmail_profile(profile()).provider == "gmail"
    with Storage.open(settings) as reopened:
        assert reopened.schema_info()["version"] == 3
        assert reopened.source_instance(src).source_identity == "synthetic-account"
        assert reopened.item(item).source_instance_id == src
        assert len(reopened.classification_work_events(work)) == 1
        assert reopened._rows("SELECT count(*) AS n FROM source_instances WHERE provider='gmail'")[0]["n"] == 1


def test_v3_migration_preserves_relational_constraints_and_rejects_empty_identity(tmp_path):
    path = tmp_path / "state" / "dam.db"
    src, item, work = _populate_v2(path)
    settings = Settings.model_validate({"state": {"database_path": str(path)}})
    with Storage.open(settings):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_list(items)").fetchone()[2] == "source_instances"
        assert {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")} == {
                "classification_work_events_no_update", "classification_work_events_no_delete"}
        assert any(row[2] for row in connection.execute("PRAGMA index_list(source_instances)"))
        assert "one_open_classification_work_per_item" in {row[1] for row in
            connection.execute("PRAGMA index_list(classification_work_members)")}
        for provider, status, identity in (
            ("synthetic", "verified", "synthetic-account"),
            ("gmail", "synthetic", "person@example.invalid"),
            ("gmail", "unverified", "person@example.invalid"),
            ("outlook", "verified", "person@example.invalid"),
            ("gmail", "verified", ""),
            ("gmail", "verified", "   "),
            ("gmail", "verified", GMAIL_ACCOUNT_ID),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO source_instances VALUES (?, ?, ?, ?)",
                                   (new_object_id("SRC"), provider, status, identity))
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO source_instances VALUES (?, 'synthetic', 'synthetic', ?)",
                               (new_object_id("SRC"), "synthetic-account"))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO items VALUES (?, ?, 'email', 'orphan')",
                               (new_object_id("ITEM"), new_object_id("SRC")))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE classification_work_events SET new_state='resolved' WHERE work_id=?",
                               (work,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM classification_work_events WHERE work_id=?", (work,))
        connection.rollback()
        connection.execute("BEGIN")
        connection.execute("DELETE FROM classification_work_members WHERE work_id=? AND item_id=?", (work, item))
        with pytest.raises(sqlite3.IntegrityError):
            connection.commit()
        connection.rollback()
        assert connection.execute("SELECT source_instance_id FROM source_instances").fetchone()[0] == src


def test_failed_v3_migration_rolls_back_without_claiming_v3(tmp_path, monkeypatch):
    path = tmp_path / "state" / "dam.db"
    src, item, work = _populate_v2(path)
    settings = Settings.model_validate({"state": {"database_path": str(path)}})
    with monkeypatch.context() as patch:
        patch.setattr(storage_module, "_SCHEMA_V3", (*storage_module._SCHEMA_V3[:2], "INVALID SQL"))
        with pytest.raises(StorageError):
            Storage.open(settings)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT source_instance_id FROM source_instances").fetchone()[0] == src
        assert connection.execute("SELECT item_id FROM items").fetchone()[0] == item
        assert connection.execute("SELECT work_id FROM classification_work_items").fetchone()[0] == work
        assert "source_instances_v3" not in {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    with Storage.open(settings) as recovered:
        assert recovered.schema_info()["version"] == 3
