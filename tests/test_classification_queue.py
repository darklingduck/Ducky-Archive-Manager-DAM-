"""Step 13 source-neutral identity and durable work, using synthetic email only."""

from datetime import datetime, timedelta, timezone
import sqlite3
import subprocess
import sys

import pytest
from pydantic import TypeAdapter, ValidationError

from dam.actions import propose_action
from dam.classification_queue import ClassificationQueueError, ClassificationQueueService
from dam.config import configuration_fingerprint, load_config
from dam.identifiers import new_object_id
from dam.items import ClassificationWorkID, ClassificationWorkItem, DamItem, EmailItemObservation, MemberDecision, SourceInstance
from dam.learning import configuration_with_learned_rules, propose_classification_rule, save_classification_rule
from dam.models import Configuration, MatchSpec, MessageMetadata, ProposedAction, Rule, RulesConfig, Settings
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, default_config_directory, run_synthetic_scan
from dam.storage import Storage, StorageError, _SCHEMA, _TABLES_V1
import dam.storage as storage_module

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path):
    base = load_config(default_config_directory())
    config = Configuration(
        settings=Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}}),
        categories=base.categories, rules=base.rules)
    with Storage.open(config.settings) as store:
        service = ClassificationQueueService(store)
        source = service.register_synthetic_email_source("synthetic-account")
        yield store, service, source, config


def message(message_id: str, sender: str = "unknown@example.invalid", *, account_id: str = "synthetic-account"):
    return MessageMetadata(account_id=account_id, message_id=message_id, sender=sender,
                           subject="Synthetic unknown document", received_at=NOW,
                           label_ids=("INBOX",))


def with_sender_rule(config: Configuration, sender: str, rule_id: str) -> Configuration:
    rule = Rule(id=rule_id, version=1, match=MatchSpec(sender_emails_any=(sender,)),
                category_ids=("promotions",), proposed_action=ProposedAction.NO_ACTION)
    return Configuration(settings=config.settings, categories=config.categories,
                         rules=RulesConfig(rules=(*config.rules.rules, rule)))


def test_typed_identity_validation_and_source_kind_distinction():
    source_id = new_object_id("SRC")
    item_id = new_object_id("ITEM")
    work_id = new_object_id("CWQ")
    assert len({source_id, item_id, work_id}) == 3
    source = SourceInstance(source_instance_id=source_id, source_identity="synthetic-account")
    item = DamItem(item_id=item_id, source_instance_id=source.source_instance_id,
                   source_item_id="native-1")
    assert source.provider == "synthetic" and item.item_kind == "email"
    for data in ({"source_instance_id": item_id, "source_identity": "synthetic-account"},
                 {"source_instance_id": source_id, "source_identity": "synthetic-account", "provider": "gmail"}):
        with pytest.raises(ValidationError):
            SourceInstance.model_validate(data)
    with pytest.raises(ValidationError):
        DamItem(item_id=source_id, source_instance_id=source_id, source_item_id="native-1")
    with pytest.raises(ValidationError):
        DamItem(item_id=item_id, source_instance_id=source_id, source_item_id="native-1", item_kind="file")
    with pytest.raises(ValidationError):
        SourceInstance(source_instance_id=source_id, source_identity="gmail-account-unverified")
    with pytest.raises(ValidationError):
        TypeAdapter(ClassificationWorkID).validate_python(item_id)
    with pytest.raises(ValidationError):
        SourceInstance.model_validate({"source_instance_id": source_id,
            "source_identity": "synthetic-account", "access_token": "synthetic-secret"})


def test_native_identity_is_scoped_and_repeated_observation_reuses_item(context):
    store, service, first_source, _ = context
    first = service.register_email_item(first_source, message("same-native"))
    repeated = service.register_email_item(first_source, message("same-native"))
    assert repeated.item.item_id == first.item.item_id
    assert service.register_synthetic_email_source("synthetic-account") == first_source
    second_source = service.register_synthetic_email_source("second-account")
    second = service.register_email_item(second_source, message("same-native", account_id="second-account"))
    assert second.item.item_id != first.item.item_id
    assert store.item_by_native_identity(first_source.source_instance_id, "same-native") == first.item
    assert service.inspect_item(first.item.item_id) == first.item
    with pytest.raises(ClassificationQueueError):
        service.register_email_item(first_source, message("wrong-account", account_id="second-account"))
    with pytest.raises(ClassificationQueueError):
        service.register_synthetic_email_source("gmail-account-unverified")
    with pytest.raises(StorageError, match="Unknown source instance"):
        store.register_item(DamItem(item_id=new_object_id("ITEM"),
            source_instance_id=new_object_id("SRC"), source_item_id="orphan-native"))


def test_dam_item_id_collision_retries_without_reassigning_identity(context, monkeypatch):
    _, service, source, _ = context
    first = service.register_email_item(source, message("native-first"))
    fresh = new_object_id("ITEM")
    generated = iter((first.item.item_id, fresh))
    monkeypatch.setattr("dam.classification_queue.new_object_id", lambda prefix: next(generated))
    second = service.register_email_item(source, message("native-second"))
    assert second.item.item_id == fresh
    assert service.register_email_item(source, message("native-first")).item.item_id == first.item.item_id


def test_only_teaching_need_creates_durable_work_and_no_automatic_grouping(context):
    store, service, source, config = context
    first = service.register_email_item(source, message("native-a"))
    second = service.register_email_item(source, message("native-b"))
    intake_a = service.record_email_observation(first, config, as_of=NOW)
    intake_b = service.record_email_observation(second, config, as_of=NOW)
    assert intake_a.work and intake_b.work and intake_a.work.work_id != intake_b.work.work_id
    assert intake_a.work.representative_item_id == first.item.item_id
    assert intake_a.work.members[0].item_id == first.item.item_id
    assert intake_a.work.members[0].item_id != first.metadata.message_id
    assert len(service.list_work()) == 2
    assert service.record_email_observation(first, config, as_of=NOW).work.work_id == intake_a.work.work_id
    assert len(service.history(intake_a.work.work_id)) == 1
    matching = with_sender_rule(config, "unknown@example.invalid", "synthetic_learned")
    classified = service.register_email_item(source, message("native-c"))
    result = service.record_email_observation(classified, matching, as_of=NOW)
    assert result.classification.requires_review  # sender-only evidence is 0.90
    assert result.classification.category_teaching_required is False
    assert result.work is None


def test_representative_must_be_an_exact_member(context):
    _, service, source, config = context
    item = service.register_email_item(source, message("native-representative"))
    work = service.record_email_observation(item, config, as_of=NOW).work
    with pytest.raises(ValidationError):
        ClassificationWorkItem(work_id=work.work_id,
            representative_item_id=new_object_id("ITEM"), state=work.state,
            created_at=work.created_at, updated_at=work.updated_at, members=work.members)


def test_service_has_no_prompt_print_or_gmail_call(context, monkeypatch):
    _, service, source, config = context
    def forbidden(*_args, **_kwargs):
        raise AssertionError("interface or Gmail call from queue service")
    monkeypatch.setattr("builtins.print", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr("dam.gmail.read_inbox", forbidden)
    monkeypatch.setattr("dam.auth.authenticate", forbidden)
    item = service.register_email_item(source, message("native-no-interface"))
    work = service.record_email_observation(item, config, as_of=NOW).work
    assert service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=1)).state == "deferred"


def test_human_accepted_sender_rule_still_reviewed_but_not_queued(context, tmp_path):
    _, service, source, config = context
    teaching_source = message("native-teaching")
    candidate = propose_classification_rule(teaching_source, "promotions", config, as_of=NOW)
    learned_path = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    save_classification_rule(candidate, config, learned_path,
                             expected_fingerprint=candidate.fingerprint, saved_at=NOW)
    learned = configuration_with_learned_rules(config, learned_path)
    later = service.register_email_item(source, message("native-later"))
    result = service.record_email_observation(later, learned, as_of=NOW)
    assert result.classification.classification_confidence == .90
    assert result.classification.requires_review
    assert result.classification.category_teaching_required is False
    assert result.work is None and service.list_work() == ()
    proposal = propose_action(later.metadata, result.classification, learned.rules,
                              as_of=NOW, settings=learned.settings)
    assert not proposal.authority_established and not proposal.executable


def test_explicit_membership_defer_individual_reevaluation_and_history(context):
    store, service, source, config = context
    first = service.register_email_item(source, message("native-primary"))
    other = service.register_email_item(source, message("native-related", "other@example.invalid"))
    work = service.record_email_observation(first, config, as_of=NOW).work
    work = service.add_related_email_item(work.work_id, other, config, as_of=NOW)
    assert {member.item_id for member in work.members} == {first.item.item_id, other.item.item_id}
    assert work.representative_item_id in {member.item_id for member in work.members}
    with pytest.raises(StorageError, match="already a member"):
        service.add_related_email_item(work.work_id, other, config, as_of=NOW)
    deferred = service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=1))
    assert deferred.state == "deferred" and {m.state for m in deferred.members} == {"deferred"}
    assert len(service.list_work(state="deferred")) == 1
    before = service.history(work.work_id)
    rule_a = with_sender_rule(config, "unknown@example.invalid", "synthetic_a")
    reevaluated = service.reevaluate(work.work_id, (other, first), rule_a,
                                     as_of=NOW + timedelta(minutes=2))
    by_id = {member.item_id: member.state for member in reevaluated.work.members}
    assert by_id[first.item.item_id] == "resolved"
    assert by_id[other.item.item_id] == "deferred"
    assert reevaluated.work.state == "deferred"
    assert service.history(work.work_id)[:len(before)] == before
    assert [event.event_type for event in service.history(work.work_id)][-2:] == ["reevaluated", "reevaluated"]
    rule_b = Rule(id="synthetic_b", version=1, match=MatchSpec(sender_emails_any=("other@example.invalid",)),
                  category_ids=("promotions",), proposed_action=ProposedAction.NO_ACTION)
    both = Configuration(settings=config.settings, categories=config.categories,
                         rules=RulesConfig(rules=(*rule_a.rules.rules, rule_b)))
    resolved = service.reevaluate(work.work_id, (first, other), both,
                                  as_of=NOW + timedelta(minutes=3)).work
    assert resolved.state == "resolved" and {m.state for m in resolved.members} == {"resolved"}
    assert service.history(work.work_id)[-1].event_type == "resolved"
    assert service.list_work(state="deferred") == ()
    with pytest.raises(ClassificationQueueError, match="resolved"):
        service.reevaluate(work.work_id, (first, other), both, as_of=NOW + timedelta(minutes=4))
    with pytest.raises(StorageError, match="resolved"):
        service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=4))


def test_related_members_cannot_cross_source_instance(context):
    _, service, source, config = context
    first = service.register_email_item(source, message("native-primary"))
    work = service.record_email_observation(first, config, as_of=NOW).work
    second_source = service.register_synthetic_email_source("other-account")
    other = service.register_email_item(second_source, message("native-related", account_id="other-account"))
    with pytest.raises((ClassificationQueueError, StorageError)):
        service.add_related_email_item(work.work_id, other, config, as_of=NOW)
    assert {m.item_id for m in service.inspect_work(work.work_id).members} == {first.item.item_id}


def test_reevaluation_requires_exact_observed_members_and_read_failure_cannot_enter(context):
    _, service, source, config = context
    first = service.register_email_item(source, message("native-a"))
    other = service.register_email_item(source, message("native-b"))
    work = service.record_email_observation(first, config, as_of=NOW).work
    service.add_related_email_item(work.work_id, other, config, as_of=NOW)
    with pytest.raises(ClassificationQueueError):
        service.reevaluate(work.work_id, (first,), config, as_of=NOW)
    with pytest.raises(ClassificationQueueError):
        service.reevaluate(work.work_id, (first, first), config, as_of=NOW)
    with pytest.raises(ClassificationQueueError):
        service.record_email_observation(object(), config, as_of=NOW)
    with pytest.raises(ValidationError):
        EmailItemObservation(item=first.item, metadata=message("different-native"))
    forged = first.model_copy(update={"metadata": message("different-native")})
    with pytest.raises(ClassificationQueueError):
        service.record_email_observation(forged, config, as_of=NOW)
    with pytest.raises(ClassificationQueueError):
        service.reevaluate(work.work_id, (first, object()), config, as_of=NOW)


def test_queue_identity_and_history_survive_reopen(context):
    store, service, source, config = context
    item = service.register_email_item(source, message("native-durable"))
    work = service.record_email_observation(item, config, as_of=NOW).work
    service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=1))
    before = service.history(work.work_id)
    with Storage.open(config.settings) as reopened:
        again = ClassificationQueueService(reopened)
        assert again.register_synthetic_email_source("synthetic-account") == source
        assert again.register_email_item(source, message("native-durable")).item == item.item
        assert again.inspect_work(work.work_id).state == "deferred"
        assert again.inspect_work(work.work_id).members[0].item_id == item.item.item_id
        assert again.history(work.work_id) == before


def test_partly_resolved_member_cannot_implicitly_reopen_when_rules_change(context):
    store, service, source, config = context
    first = service.register_email_item(source, message("native-first"))
    other = service.register_email_item(source, message("native-other", "other@example.invalid"))
    work = service.record_email_observation(first, config, as_of=NOW).work
    service.add_related_email_item(work.work_id, other, config, as_of=NOW)
    rule = with_sender_rule(config, "unknown@example.invalid", "synthetic_initial")
    first_pass = service.reevaluate(work.work_id, (first, other), rule,
                                    as_of=NOW + timedelta(minutes=1)).work
    assert next(m for m in first_pass.members if m.item_id == first.item.item_id).state == "resolved"
    assert service.record_email_observation(first, config, as_of=NOW + timedelta(minutes=2)).work.work_id == work.work_id
    assert len(service.list_work()) == 1
    with pytest.raises(StorageError, match="already belongs"):
        store.create_classification_work(new_object_id("CWQ"), MemberDecision(
            item_id=first.item.item_id, category_permanent_ids=(), teaching_required=True,
            config_fingerprint=configuration_fingerprint(config)), occurred_at=NOW + timedelta(minutes=2))
    history = service.history(work.work_id)
    with pytest.raises(StorageError, match="reopen"):
        service.reevaluate(work.work_id, (first, other), config,
                           as_of=NOW + timedelta(minutes=2))
    assert service.inspect_work(work.work_id) == first_pass
    assert service.history(work.work_id) == history


def test_repeated_defer_is_idempotent_and_does_not_duplicate_history(context):
    _, service, source, config = context
    item = service.register_email_item(source, message("native-defer"))
    work = service.record_email_observation(item, config, as_of=NOW).work
    first = service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=1))
    history = service.history(work.work_id)
    second = service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=2))
    assert second == first and service.history(work.work_id) == history


def test_queue_rows_do_not_store_email_headers_and_existing_scan_is_unconnected(context):
    store, service, source, config = context
    secret_subject = "Synthetic subject confidential marker"
    metadata = message("native-a").model_copy(update={"subject": secret_subject})
    item = service.register_email_item(source, metadata)
    service.record_email_observation(item, config, as_of=NOW)
    with sqlite3.connect(store.path) as connection:
        rows = []
        for table in ("source_instances", "items", "classification_work_items",
                      "classification_work_members", "classification_work_events"):
            rows.extend(connection.execute(f"SELECT * FROM {table}").fetchall())
    assert secret_subject not in repr(rows)
    assert "unknown@example.invalid" not in repr(rows)
    assert run_synthetic_scan(limit=1, run_id="synthetic-unconnected", as_of=NOW).persistence == "in_memory_only"
    assert MAX_INITIAL_GMAIL_LIMIT == 10
    assert service.list_work()  # only the explicit service call created work


def test_v1_database_migrates_without_rewriting_old_rows(tmp_path):
    path = tmp_path / "state" / "dam.db"
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("INSERT INTO accounts VALUES ('synthetic-legacy-account')")
        connection.execute("INSERT INTO messages VALUES ('synthetic-legacy-account', 'legacy-native', NULL)")
        connection.execute("INSERT INTO config_snapshots VALUES (?, ?, 1, ?)",
                           ("0" * 64, "1" * 64, '{"legacy":"unchanged"}'))
        connection.execute("INSERT INTO scan_runs VALUES (?, ?, ?, ?, NULL)",
                           ("legacy-run", "synthetic-legacy-account", "0" * 64, '{"legacy":"run"}'))
        connection.execute("""INSERT INTO audit_events
            (event_id, run_id, message_id, event_json, state) VALUES (?, ?, NULL, ?, 'informational')""",
            ("legacy-event", "legacy-run", '{"legacy":"audit"}'))
    settings = Settings.model_validate({"state": {"database_path": str(path)}})
    with Storage.open(settings) as store:
        assert store.schema_info()["version"] == 4
        assert _TABLES_V1 <= set(store.schema_info()["tables"])
        assert store.messages("synthetic-legacy-account")[0]["message_id"] == "legacy-native"
        assert store.classification_work_list() == ()
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT count(*) FROM source_instances").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0
            assert connection.execute("SELECT provenance_json FROM config_snapshots").fetchone()[0] == '{"legacy":"unchanged"}'
            assert connection.execute("SELECT start_json FROM scan_runs").fetchone()[0] == '{"legacy":"run"}'
            assert connection.execute("SELECT event_json FROM audit_events").fetchone()[0] == '{"legacy":"audit"}'
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO messages VALUES ('absent-account', 'orphan', NULL)")
            connection.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO messages VALUES ('synthetic-legacy-account', 'legacy-native', NULL)")
            connection.rollback()
    with Storage.open(settings) as reopened:
        assert reopened.schema_info()["version"] == 4
        assert reopened.messages("synthetic-legacy-account")[0]["message_id"] == "legacy-native"


def test_database_enforces_representative_and_append_only_history(context):
    store, service, source, config = context
    item = service.register_email_item(source, message("native-db-constraints"))
    work = service.record_email_observation(item, config, as_of=NOW).work
    event = service.history(work.work_id)[0]
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO items VALUES (?, ?, 'email', ?)",
                               (new_object_id("ITEM"), source.source_instance_id, item.item.source_item_id))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE classification_work_events SET new_state='resolved' WHERE event_id=?",
                               (event.event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM classification_work_events WHERE event_id=?", (event.event_id,))
        connection.rollback()
        connection.execute("BEGIN")
        connection.execute("DELETE FROM classification_work_members WHERE work_id=? AND item_id=?",
                           (work.work_id, item.item.item_id))
        with pytest.raises(sqlite3.IntegrityError):
            connection.commit()
        connection.rollback()
    assert service.inspect_work(work.work_id) == work
    assert service.history(work.work_id) == (event,)
    with pytest.raises(StorageError, match="Invalid classification work identity"):
        store.create_classification_work(item.item.item_id, MemberDecision(
            item_id=item.item.item_id, category_permanent_ids=(), teaching_required=True,
            config_fingerprint=configuration_fingerprint(config)), occurred_at=NOW)


def test_state_and_history_rollback_together_when_event_write_fails(context, monkeypatch):
    store, service, source, config = context
    item = service.register_email_item(source, message("native-atomic"))
    def failed_event(*_args, **_kwargs):
        raise StorageError("synthetic event insertion failure")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_work_event", failed_event)
        with pytest.raises(StorageError, match="synthetic event"):
            service.record_email_observation(item, config, as_of=NOW)
    assert service.list_work() == ()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM classification_work_members").fetchone()[0] == 0
    work = service.record_email_observation(item, config, as_of=NOW).work
    creation_history = service.history(work.work_id)
    with monkeypatch.context() as patch:
        patch.setattr(store, "_work_event", failed_event)
        with pytest.raises(StorageError, match="synthetic event"):
            service.defer_work(work.work_id, as_of=NOW + timedelta(minutes=1))
    assert service.inspect_work(work.work_id) == work
    assert service.history(work.work_id) == creation_history
    learned = with_sender_rule(config, "unknown@example.invalid", "synthetic_atomic")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_work_event", failed_event)
        with pytest.raises(StorageError, match="synthetic event"):
            service.reevaluate(work.work_id, (item,), learned, as_of=NOW + timedelta(minutes=2))
    assert service.inspect_work(work.work_id) == work
    assert service.history(work.work_id) == creation_history


def test_multi_member_reevaluation_rolls_back_after_later_event_failure(context, monkeypatch):
    store, service, source, config = context
    first = service.register_email_item(source, message("native-first-atomic"))
    second = service.register_email_item(source, message("native-second-atomic"))
    work = service.record_email_observation(first, config, as_of=NOW).work
    work = service.add_related_email_item(work.work_id, second, config, as_of=NOW)
    old_events = service.history(work.work_id)
    original_event = store._work_event
    count = 0
    def fail_on_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise StorageError("synthetic later event failure")
        return original_event(*args, **kwargs)
    learned = with_sender_rule(config, "unknown@example.invalid", "synthetic_multi_atomic")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_work_event", fail_on_second)
        with pytest.raises(StorageError, match="synthetic later event"):
            service.reevaluate(work.work_id, (first, second), learned,
                               as_of=NOW + timedelta(minutes=1))
    assert service.inspect_work(work.work_id) == work
    assert service.history(work.work_id) == old_events


def test_failed_v1_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "state" / "dam.db"
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("INSERT INTO accounts VALUES ('synthetic-legacy-account')")
    with monkeypatch.context() as patch:
        patch.setattr(storage_module, "_SCHEMA_V2", (storage_module._SCHEMA_V2[0], "INVALID SQL"))
        with pytest.raises(StorageError):
            Storage.open(Settings.model_validate({"state": {"database_path": str(path)}}))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "source_instances" not in tables
    with Storage.open(Settings.model_validate({"state": {"database_path": str(path)}})) as recovered:
        assert recovered.schema_info()["version"] == 4


def test_importing_queue_has_no_database_filesystem_or_network_activity(tmp_path):
    code = '''
import sys
def guard(event, args):
    if event in ("sqlite3.connect", "socket.connect", "socket.__new__", "os.mkdir", "os.rename"):
        raise AssertionError(event)
    if event == "open" and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError("file write")
sys.addaudithook(guard)
import dam.classification_queue
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
