"""Synthetic/local Step 12.3 category identity and catalog behavior."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from multiprocessing import get_context
import os
from pathlib import Path
import re
import socket
import stat
import webbrowser

import pytest
from pydantic import ValidationError

from dam.auth import AuthPaths
from dam.categories import (
    CategoryCatalog, CategoryError, ManagedCategory, default_catalog_path, load_catalog, merge_categories, new_category_id, new_object_id,
    propose_change, resolve_category, save_change,
)
from dam.cli import main
from dam.config import load_config
from dam.learning import (
    configuration_with_learned_rules, load_learned_rules, propose_classification_rule,
    save_classification_rule,
)
from dam.models import Category, CategoriesConfig, Configuration, MessageMetadata
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, default_config_directory, run_synthetic_scan
from dam.storage import Storage

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
IDS = {
    "accounts_security": "CAT-VRY3X2CKQVB4RJPR6SZO42AGOI",
    "finance": "CAT-HPTOWGW2T5FS3PW7HWZCNBG5JQ",
    "it_development": "CAT-VRQQPNZERFC67OJE3MDN6MIFQY",
    "employment": "CAT-2MWDXHEBGFFJXH6HDURUI3U5UI",
    "employment_inactive": "CAT-UTPEZ4A6DVA7DMDYDJR5A2TRWU",
    "promotions": "CAT-XMJSBWIPE5CTLOCM2PXADCXWRY",
}


@pytest.fixture
def context(tmp_path):
    return load_config(default_config_directory()), default_catalog_path(tmp_path)


def _preview(context, operation, **changes):
    config, path = context
    if operation == "retire" and "learned_rules_path" not in changes:
        changes["learned_rules_path"] = path.with_name("learned-rules.yaml")
    return propose_change(config, load_catalog(path), operation, **changes)


def _save(context, change):
    config, path = context
    return save_change(path, config, change, expected_fingerprint=change.fingerprint,
                       learned_rules_path=path.with_name("learned-rules.yaml") if change.operation == "retire" else None)


def _message(sender="alerts@example.invalid", message_id="synthetic-calendar"):
    return MessageMetadata(account_id="synthetic-account", message_id=message_id,
                           sender=sender, subject="Synthetic calendar notification",
                           received_at=NOW, label_ids=("INBOX",))


def _process_save(path, config, change, queue):
    try:
        save_change(path, config, change, expected_fingerprint=change.fingerprint)
        queue.put("saved")
    except CategoryError as error:
        queue.put(str(error))


def test_typed_random_ids_and_stable_bootstrap(context):
    config, _ = context
    assert {item.id: item.permanent_id for item in config.categories.categories} == IDS
    first, second = new_object_id("CAT"), new_object_id("CAT")
    assert first != second
    assert re.fullmatch(r"CAT-[A-Z2-7]{26}", first)
    assert new_object_id("SUB", random_bytes=b"\x00" * 16).startswith("SUB-")
    with pytest.raises(CategoryError, match="invalid_identity_type"):
        new_object_id("cat")
    with pytest.raises(ValidationError):
        Category(id="sample", name="Sample", permanent_id="CAT-TOO_SHORT")
    fresh = new_object_id("CAT")
    values = iter((IDS["finance"], fresh))
    assert new_category_id(config.categories, factory=lambda: next(values)) == fresh


def test_create_merge_permissions_and_no_automatic_learning(context):
    config, path = context
    change = _preview(context, "add", key="calendar", name="Calendar",
                      generated_id=new_object_id("CAT"))
    assert not path.exists()
    assert change.saved is False
    _save(context, change)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.with_name("categories.lock").exists()
    effective = merge_categories(config.categories, load_catalog(path))
    item = resolve_category("calendar", effective)
    assert item.permanent_id == change.after.permanent_id
    assert resolve_category(item.permanent_id, effective) == item
    assert len(effective.categories) == 7
    assert not path.with_name("learned-rules.yaml").exists()


def test_collision_and_retired_id_or_key_never_reused(context):
    config, path = context
    created = _preview(context, "add", key="calendar", name="Calendar", generated_id=new_object_id("CAT"))
    _save(context, created)
    with pytest.raises(CategoryError, match="duplicate_category_identity_or_key"):
        _preview(context, "add", key="shopping", name="Shopping", generated_id=created.after.permanent_id)
    retired = _preview(context, "retire", selector="calendar")
    _save(context, retired)
    with pytest.raises(CategoryError, match="duplicate_category_identity_or_key"):
        _preview(context, "add", key="calendar", name="New Calendar", generated_id=new_object_id("CAT"))
    with pytest.raises(CategoryError, match="retired_category"):
        resolve_category("calendar", merge_categories(config.categories, load_catalog(path)))


def test_rename_move_keep_identity_and_validate_parents(context):
    config, path = context
    personal = _preview(context, "add", key="personal", name="Personal", generated_id=new_object_id("CAT"))
    _save(context, personal)
    calendar = _preview(context, "add", key="calendar", name="Calendar", generated_id=new_object_id("CAT"))
    _save(context, calendar)
    renamed = _preview(context, "rename", selector="calendar", name="Calendar & Scheduling")
    _save(context, renamed)
    moved = _preview(context, "move", selector="calendar", parent="personal")
    _save(context, moved)
    assert renamed.after.permanent_id == moved.after.permanent_id == calendar.after.permanent_id
    effective = merge_categories(config.categories, load_catalog(path))
    item = resolve_category("calendar", effective)
    assert item.name == "Calendar & Scheduling" and item.parent_id == "personal"
    with pytest.raises(CategoryError, match="unknown_or_ambiguous_category"):
        _preview(context, "move", selector="calendar", parent="missing")
    with pytest.raises(CategoryError, match="invalid_category_tree"):
        _preview(context, "move", selector="calendar", parent="calendar")
    with pytest.raises(CategoryError, match="invalid_category_tree"):
        _preview(context, "move", selector="personal", parent="calendar")
    with pytest.raises(CategoryError, match="category_has_active_children"):
        _preview(context, "retire", selector="personal")


def test_rule_references_block_retirement_and_no_action_inheritance(context):
    config, path = context
    with pytest.raises(CategoryError, match="category_has_active_rule_references"):
        _preview(context, "retire", selector="finance")
    changed = _preview(context, "move", selector="finance", parent="promotions")
    _save(context, changed)
    before = run_synthetic_scan(messages=(_message("receipt@example.invalid"),), as_of=NOW, run_id="before")
    after = run_synthetic_scan(messages=(_message("receipt@example.invalid"),), as_of=NOW,
                               run_id="after", category_catalog_path=path)
    assert before.preview.entries[0].proposed_action == after.preview.entries[0].proposed_action
    assert after.preview.entries[0].authority_established is False
    assert after.preview.entries[0].executable is False
    assert after.preview.statistics.executed_gmail_actions == 0


def test_stale_preview_collision_and_atomic_failure(context, monkeypatch):
    config, path = context
    first = _preview(context, "add", key="calendar", name="Calendar", generated_id=new_object_id("CAT"))
    second = _preview(context, "add", key="personal", name="Personal", generated_id=new_object_id("CAT"))
    _save(context, first)
    with pytest.raises(CategoryError, match="stale_category_catalog"):
        _save(context, second)
    original = path.read_bytes()
    change = _preview(context, "rename", selector="calendar", name="Changed")
    def fail_replace(*_args):
        raise OSError("synthetic interrupted replacement")
    monkeypatch.setattr("dam.categories.os.replace", fail_replace)
    with pytest.raises(CategoryError, match="persistence_failure"):
        _save(context, change)
    assert path.read_bytes() == original
    assert not tuple(path.parent.glob(".categories-*.tmp"))


def test_two_concurrent_saves_have_one_winner(context):
    config, path = context
    changes = [_preview(context, "add", key=key, name=key.title(), generated_id=new_object_id("CAT"))
               for key in ("calendar", "personal")]
    def save(change):
        try:
            _save(context, change)
            return "saved"
        except CategoryError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, changes))
    assert sorted(outcomes) == ["saved", "stale_category_catalog"]
    assert len(load_catalog(path).records) == 1


def test_two_processes_cannot_publish_stale_creations(context):
    config, path = context
    changes = [_preview(context, "add", key=key, name=key.title(), generated_id=new_object_id("CAT"))
               for key in ("calendar", "personal")]
    runtime = get_context("fork")
    queue = runtime.Queue()
    processes = [runtime.Process(target=_process_save, args=(path, config, change, queue))
                 for change in changes]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in processes) == ["saved", "stale_category_catalog"]
    assert len(load_catalog(path).records) == 1


def test_malformed_catalog_and_unsafe_permissions_rejected(context):
    config, path = context
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("schema_version: 1\nrecords: [broken", encoding="utf-8")
    path.chmod(0o600)
    original = path.read_bytes()
    with pytest.raises(CategoryError, match="invalid_category_catalog"):
        load_catalog(path)
    assert path.read_bytes() == original
    path.chmod(0o644)
    with pytest.raises(CategoryError, match="unsafe_category_catalog_permissions"):
        load_catalog(path)


def test_effective_merge_rejects_duplicate_identity_alias_and_retired_parent(context):
    config, _ = context
    duplicate = CategoryCatalog(records=(ManagedCategory(
        permanent_id=IDS["finance"], key="other", name="Other", origin="user"),))
    with pytest.raises(CategoryError, match="base_category_identity_conflict"):
        merge_categories(config.categories, duplicate)
    parent_id, child_id = new_object_id("CAT"), new_object_id("CAT")
    retired_parent = ManagedCategory(permanent_id=parent_id, key="personal", name="Personal",
                                     status="retired", origin="user")
    child = ManagedCategory(permanent_id=child_id, key="calendar", name="Calendar",
                            parent_permanent_id=parent_id, origin="user")
    with pytest.raises(CategoryError, match="retired_category_parent"):
        merge_categories(config.categories, CategoryCatalog(records=(retired_parent, child)))
    alias_conflict = child.model_copy(update={"parent_permanent_id": None, "aliases": ("finance",)})
    with pytest.raises(CategoryError, match="duplicate_category_key"):
        merge_categories(config.categories, CategoryCatalog(records=(alias_conflict,)))
    sibling_name = child.model_copy(update={"parent_permanent_id": None, "name": "Finance"})
    with pytest.raises(CategoryError, match="invalid_category_tree"):
        merge_categories(config.categories, CategoryCatalog(records=(sibling_name,)))


def test_legacy_learned_rule_id_preserved_after_rename_and_move(context):
    config, path = context
    rules = path.with_name("learned-rules.yaml")
    source = _message()
    candidate = propose_classification_rule(source, "promotions", config, as_of=NOW)
    saved = save_classification_rule(candidate, config, rules, expected_fingerprint=candidate.fingerprint,
                                     saved_at=NOW)
    original_id = saved.candidate.rule.id
    assert load_learned_rules(rules).schema_version == 2
    assert load_learned_rules(rules).records[0].category_permanent_id == IDS["promotions"]
    parent = _preview(context, "add", key="personal", name="Personal", generated_id=new_object_id("CAT"))
    _save(context, parent)
    _save(context, _preview(context, "rename", selector="promotions", name="Offers"))
    _save(context, _preview(context, "move", selector="promotions", parent="personal"))
    changed = load_config(default_config_directory(), category_catalog_path=path)
    effective = configuration_with_learned_rules(changed, rules)
    assert next(rule for rule in effective.rules.rules if rule.id == original_id).category_ids == ("promotions",)
    scan = run_synthetic_scan(messages=(source, _message("other@example.invalid", "unrelated")),
                              as_of=NOW, run_id="learned", learned_rules_path=rules,
                              category_catalog_path=path)
    entries = {item.message_id: item for item in scan.preview.entries}
    assert entries[source.message_id].category_ids == ("promotions",)
    assert entries["unrelated"].category_ids == ()
    assert scan.preview.statistics.executed_gmail_actions == 0


def test_legacy_v1_record_load_does_not_rewrite(context):
    config, path = context
    rules = path.with_name("learned-rules.yaml")
    candidate = propose_classification_rule(_message(), "promotions", config, as_of=NOW)
    save_classification_rule(candidate, config, rules, expected_fingerprint=candidate.fingerprint, saved_at=NOW)
    raw = rules.read_text()
    raw = raw.replace("schema_version: 2", "schema_version: 1")
    raw = "\n".join(line for line in raw.splitlines() if "category_permanent_id:" not in line) + "\n"
    rules.write_text(raw)
    rules.chmod(0o600)
    before = rules.read_bytes()
    effective = configuration_with_learned_rules(config, rules)
    assert candidate.rule.id in {rule.id for rule in effective.rules.rules}
    assert rules.read_bytes() == before
    changed_categories = CategoriesConfig(categories=tuple(
        item.model_copy(update={"id": "offers", "key": "offers", "aliases": ("promotions",)})
        if item.id == "promotions" else item for item in config.categories.categories))
    changed = Configuration(settings=config.settings, categories=changed_categories, rules=config.rules)
    migrated = configuration_with_learned_rules(changed, rules)
    assert next(rule for rule in migrated.rules.rules if rule.id == candidate.rule.id).category_ids == ("offers",)
    assert rules.read_bytes() == before


def test_saved_learned_rule_blocks_retirement(context):
    config, path = context
    rules = path.with_name("learned-rules.yaml")
    created = _preview(context, "add", key="calendar", name="Calendar", generated_id=new_object_id("CAT"))
    _save(context, created)
    effective = load_config(default_config_directory(), category_catalog_path=path)
    candidate = propose_classification_rule(_message(), "calendar", effective, as_of=NOW)
    save_classification_rule(candidate, effective, rules, expected_fingerprint=candidate.fingerprint,
                             saved_at=NOW)
    saved = load_learned_rules(rules)
    assert saved.schema_version == 2
    assert saved.records[0].category_permanent_id == created.after.permanent_id
    with pytest.raises(CategoryError, match="category_has_active_rule_references"):
        propose_change(config, load_catalog(path), "retire", selector="calendar",
                       learned_rules_path=rules)


def test_historical_category_snapshot_uses_then_current_taxonomy(context, tmp_path):
    config, path = context
    settings = config.settings.model_copy(update={"state": config.settings.state.model_copy(update={
        "database_path": str(tmp_path / "state" / "dam.db"),
        "report_directory": str(tmp_path / "reports")})})
    before = config.model_copy(update={"settings": settings})
    with Storage.open(settings) as store:
        fingerprint = store.save_configuration(before)
        prior = store.configuration(fingerprint)["provenance"]["category_snapshot"]
        finance = next(item for item in prior if item["key"] == "finance")
        assert finance["permanent_id"] == IDS["finance"]
        assert finance["name"] == "Finance" and finance["path_at_scan"] == ["finance"]
        assert len(store.configuration(fingerprint)["provenance"]["category_catalog_revision"]) == 64
        _save(context, _preview(context, "rename", selector="finance", name="Money"))
        changed = load_config(default_config_directory(), category_catalog_path=path)
        changed = changed.model_copy(update={"settings": settings})
        new_fingerprint = store.save_configuration(changed)
        newer = store.configuration(new_fingerprint)["provenance"]["category_snapshot"]
        assert next(item for item in newer if item["key"] == "finance")["name"] == "Money"
        assert store.configuration(fingerprint)["provenance"]["category_snapshot"] == prior


def test_cli_preview_save_show_move_and_retirement_confirmation(context, capsys):
    _, path = context
    base = ["categories", "--catalog-file", str(path)]
    assert main(base) == 0
    assert "finance" in capsys.readouterr().out
    add = [*base, "add", "--key", "calendar", "--name", "Calendar"]
    assert main(add) == 0
    preview = capsys.readouterr().out
    assert "preview only" in preview and not path.exists()
    pid = re.search(r"CAT-[A-Z2-7]{26}", preview).group()
    fingerprint = re.search(r"Preview fingerprint: ([0-9a-f]{64})", preview).group(1)
    assert main([*add, "--save", "--confirm-id", pid, "--confirm-fingerprint", fingerprint]) == 0
    assert "saved" in capsys.readouterr().out
    assert main([*base, "show", "calendar"]) == 0
    assert pid in capsys.readouterr().out
    assert main(["learn", "--message-id", "synthetic-004-unknown", "--category", pid,
                 "--category-catalog-file", str(path),
                 "--learned-rules-file", str(path.with_name("learned-rules.yaml"))]) == 0
    assert "Human selected category: calendar" in capsys.readouterr().out
    assert not path.with_name("learned-rules.yaml").exists()
    rename = [*base, "rename", "calendar", "--name", "Calendar & Scheduling"]
    assert main(rename) == 0
    fingerprint = re.search(r"Preview fingerprint: ([0-9a-f]{64})", capsys.readouterr().out).group(1)
    assert main([*rename, "--save", "--confirm-fingerprint", fingerprint]) == 0
    capsys.readouterr()
    move = [*base, "move", "calendar", "--parent", "finance"]
    assert main(move) == 0
    fingerprint = re.search(r"Preview fingerprint: ([0-9a-f]{64})", capsys.readouterr().out).group(1)
    assert main([*move, "--save", "--confirm-fingerprint", fingerprint]) == 0
    capsys.readouterr()
    retire = [*base, "retire", "calendar"]
    assert main(retire) == 0
    fingerprint = re.search(r"Preview fingerprint: ([0-9a-f]{64})", capsys.readouterr().out).group(1)
    assert main([*retire, "--save", "--confirm-fingerprint", fingerprint]) == 2
    capsys.readouterr()
    assert main([*retire, "--save", "--confirm-retire", pid,
                 "--confirm-fingerprint", fingerprint]) == 0
    assert "status=retired" in capsys.readouterr().out
    assert main(base) == 0
    listing = capsys.readouterr().out
    assert "calendar\tCalendar & Scheduling" in listing
    assert "retired; unavailable for learning" in listing


def test_category_management_no_external_access_and_scan_limit_unchanged(context, monkeypatch):
    _, path = context
    def forbidden(*_args, **_kwargs):
        raise AssertionError("external access attempted")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    monkeypatch.setattr("dam.auth.authenticate", forbidden)
    monkeypatch.setattr("dam.cli.run_gmail_scan", forbidden)
    protected = {AuthPaths.for_home().client_secret, AuthPaths.for_home().token}
    original_open = Path.open
    original_os_open = os.open
    def guarded_open(self, *args, **kwargs):
        if self in protected:
            forbidden()
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    def guarded_os_open(path, *args, **kwargs):
        if Path(path) in protected:
            forbidden()
        return original_os_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded_os_open)
    assert main(["categories", "--catalog-file", str(path)]) == 0
    assert main(["categories", "--catalog-file", str(path), "add", "--key", "calendar",
                 "--name", "Calendar"]) == 0
    assert MAX_INITIAL_GMAIL_LIMIT == 10
