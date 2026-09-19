"""Step 12 classification teaching uses only packaged synthetic metadata."""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

import pytest
import yaml

from dam.cli import main
from dam.config import load_config
from dam.learning import (
    LearningError, configuration_with_learned_rules, default_learned_rules_path,
    load_learned_rules, propose_classification_rule, render_candidate,
    save_classification_rule,
)
from dam.models import MessageMetadata, ProposedAction
from dam.scan import default_config_directory, load_synthetic_messages, run_synthetic_scan

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


@pytest.fixture
def rules_path(tmp_path):
    return tmp_path / ".config" / "dam" / "learned-rules.yaml"


@pytest.fixture
def context():
    config = load_config(default_config_directory())
    messages = load_synthetic_messages()
    return config, messages[-1], messages


def candidate(context, *, category="promotions", sample=()):
    config, source, _ = context
    return propose_classification_rule(source, category, config, as_of=NOW, sample=sample)


def test_concurrent_learned_rule_saves_do_not_overwrite_one_another(context, rules_path):
    config, source, _ = context
    first = propose_classification_rule(source, "promotions", config, as_of=NOW)
    other = source.model_copy(update={"message_id": "another-native-id",
                                      "sender": "another@example.invalid"})
    second = propose_classification_rule(other, "finance", config, as_of=NOW)

    def save(proposal):
        try:
            save_classification_rule(proposal, config, rules_path,
                                     expected_fingerprint=proposal.fingerprint, saved_at=NOW)
            return "saved"
        except LearningError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(save, (first, second)))
    assert sorted(outcomes) == ["saved", "stale_or_unconfirmed_candidate"]
    assert len(load_learned_rules(rules_path).records) == 1


def test_preview_is_pure_exact_sender_and_classification_only(context, rules_path):
    config, source, messages = context
    result = candidate(context, sample=messages[:-1])
    assert result.human_selected_category == "promotions"
    assert result.source_message_id == source.message_id
    assert result.scope == "exact_sender"
    assert result.rule.match.sender_emails_any == ("unknown@unknown.example.invalid",)
    assert result.rule.match.sender_domains_any == ()
    assert result.rule.match.subject_contains_any == ()
    assert result.rule.match.min_age_days is None
    assert result.rule.match.max_age_days is None
    assert result.rule.match.label_ids_any == ()
    assert result.rule.match.label_ids_all == ()
    assert source.message_id not in result.rule.id
    assert source.received_at.isoformat() not in result.rule.model_dump_json()
    assert result.rule.proposed_action == ProposedAction.NO_ACTION
    assert result.rule.approval_ref is None
    assert not result.saved and not result.authority_established and not result.executable
    assert result.executed_gmail_actions == 0
    assert [item.field for item in result.evidence if item.selected] == ["sender_email"]
    assert all(not item.selected for item in result.evidence if item.field != "sender_email")
    assert result.impact[-1].after_category_ids == ("promotions",)
    assert result.impact[-1].after_requires_review
    assert result.impact[-1].after_confidence == .90
    assert result.impact[0].after_category_ids == ("finance",)
    assert not rules_path.exists()
    assert not rules_path.parent.exists()
    assert result.config_fingerprint and len(result.fingerprint) == 64


def test_unknown_category_and_missing_sender_rejected(context):
    config, source, _ = context
    with pytest.raises(LearningError, match="unknown_category"):
        candidate(context, category="promotons")
    missing = source.model_copy(update={"sender": None})
    with pytest.raises(LearningError, match="sender_missing_or_ambiguous"):
        propose_classification_rule(missing, "promotions", config, as_of=NOW)
    ambiguous = source.model_copy(update={"sender": "a@example.invalid, b@example.invalid"})
    with pytest.raises(LearningError, match="sender_missing_or_ambiguous"):
        propose_classification_rule(ambiguous, "promotions", config, as_of=NOW)


def test_gmail_category_is_supporting_evidence_only(context):
    config, source, _ = context
    labeled = source.model_copy(update={"label_ids": ("INBOX", "CATEGORY_UPDATES")})
    result = propose_classification_rule(labeled, "promotions", config, as_of=NOW)
    label_evidence = next(item for item in result.evidence if item.field == "label_ids")
    assert label_evidence.value == "CATEGORY_UPDATES"
    assert not label_evidence.selected
    assert result.human_selected_category == "promotions"
    assert result.rule.match.label_ids_any == ()
    assert result.rule.match.label_ids_all == ()
    assert "Evidence label_ids: not selected; observed=CATEGORY_UPDATES" in render_candidate(result)


def test_candidate_fingerprint_deterministic_and_provenance_bound(context):
    config, source, messages = context
    first = candidate(context, sample=messages[:-1])
    second = candidate(context, sample=tuple(reversed(messages[:-1])))
    assert first == second
    assert first.fingerprint == second.fingerprint
    changed = source.model_copy(update={"message_id": "synthetic-other-id"})
    third = propose_classification_rule(changed, "promotions", config, as_of=NOW)
    assert third.fingerprint != first.fingerprint
    assert propose_classification_rule(source, "finance", config, as_of=NOW).fingerprint != first.fingerprint
    without_sample = propose_classification_rule(source, "promotions", config, as_of=NOW)
    assert without_sample.fingerprint != first.fingerprint
    assert "unknown@unknown" not in first.rule.id


def test_save_requires_exact_preview_fingerprint_and_private_atomic_file(context, rules_path):
    config, _, _ = context
    proposal = candidate(context)
    with pytest.raises(LearningError, match="stale_or_unconfirmed_candidate"):
        save_classification_rule(proposal, config, rules_path, expected_fingerprint="wrong")
    assert not rules_path.exists()
    saved = save_classification_rule(proposal, config, rules_path,
                                     expected_fingerprint=proposal.fingerprint, saved_at=NOW)
    assert saved.status == "saved"
    assert not saved.authority_established and not saved.executable
    assert saved.executed_gmail_actions == 0
    assert stat.S_IMODE(rules_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(rules_path.stat().st_mode) == 0o600
    assert sorted(path.name for path in rules_path.parent.iterdir()) == ["learned-rules.yaml"]
    document = yaml.safe_load(rules_path.read_text())
    assert document["records"][0]["source"] == "human_explicit_save"
    assert document["records"][0]["rule"]["proposed_action"] == "no_action"
    assert "body" not in json.dumps(document).lower()
    assert "token" not in json.dumps(document).lower()
    assert "unsubscribe" not in json.dumps(document).lower()
    loaded = load_learned_rules(rules_path)
    assert loaded.records[0].rule == proposal.rule


def test_equivalent_rule_is_not_duplicated(context, rules_path):
    config, source, _ = context
    proposal = candidate(context)
    save_classification_rule(proposal, config, rules_path, expected_fingerprint=proposal.fingerprint)
    merged = configuration_with_learned_rules(config, rules_path)
    duplicate = propose_classification_rule(source, "promotions", merged, as_of=NOW)
    assert duplicate.equivalent_rule_id == proposal.rule.id
    again = save_classification_rule(duplicate, merged, rules_path,
                                     expected_fingerprint=duplicate.fingerprint)
    assert again.status == "equivalent_exists"
    assert len(load_learned_rules(rules_path).records) == 1


def test_different_category_for_same_sender_is_conflict(context, rules_path):
    config, source, _ = context
    first = candidate(context)
    save_classification_rule(first, config, rules_path, expected_fingerprint=first.fingerprint)
    merged = configuration_with_learned_rules(config, rules_path)
    second = propose_classification_rule(source, "finance", merged, as_of=NOW)
    assert second.conflicts
    with pytest.raises(LearningError, match="candidate_conflict_requires_review"):
        save_classification_rule(second, merged, rules_path,
                                 expected_fingerprint=second.fingerprint)
    assert len(load_learned_rules(rules_path).records) == 1


def test_multiple_valid_rules_preserved_and_sorted(context, rules_path):
    config, source, _ = context
    first = candidate(context)
    save_classification_rule(first, config, rules_path, expected_fingerprint=first.fingerprint)
    other = MessageMetadata(account_id=source.account_id, message_id="synthetic-second",
                            sender="Other <other@other.example.invalid>", subject="A fictional note",
                            received_at=NOW, label_ids=("INBOX",))
    merged = configuration_with_learned_rules(config, rules_path)
    second = propose_classification_rule(other, "finance", merged, as_of=NOW)
    save_classification_rule(second, merged, rules_path,
                             expected_fingerprint=second.fingerprint)
    loaded = load_learned_rules(rules_path)
    assert len(loaded.records) == 2
    assert [record.rule.id for record in loaded.records] == sorted(record.rule.id for record in loaded.records)
    assert {record.rule.match.sender_emails_any[0] for record in loaded.records} == {
        "unknown@unknown.example.invalid", "other@other.example.invalid"}


def test_existing_category_conflict_blocks_save(context, rules_path):
    config, _, messages = context
    job = messages[1]
    proposed = propose_classification_rule(job, "promotions", config, as_of=NOW)
    assert proposed.conflicts
    assert proposed.impact[0].before_category_ids == ("employment_inactive",)
    assert proposed.impact[0].after_category_ids == ("employment_inactive",)
    with pytest.raises(LearningError, match="candidate_conflict_requires_review"):
        save_classification_rule(proposed, config, rules_path,
                                 expected_fingerprint=proposed.fingerprint)
    assert not rules_path.exists()


def test_opt_in_saved_rule_classifies_later_synthetic_scan_without_action(context, rules_path):
    config, _, _ = context
    proposal = candidate(context)
    save_classification_rule(proposal, config, rules_path, expected_fingerprint=proposal.fingerprint)
    baseline = run_synthetic_scan(run_id="baseline", as_of=NOW)
    learned = run_synthetic_scan(run_id="learned", as_of=NOW, learned_rules_path=rules_path)
    before = next(item for item in baseline.preview.entries if item.message_id == proposal.source_message_id)
    after = next(item for item in learned.preview.entries if item.message_id == proposal.source_message_id)
    assert before.category_ids == ()
    assert after.category_ids == ("promotions",)
    assert after.proposed_action in (ProposedAction.NO_ACTION, ProposedAction.MARK_REVIEW)
    assert after.requires_review
    assert not after.authority_established and not after.executable
    assert learned.preview.statistics.executed_gmail_actions == 0
    assert all(not item.authority_established and not item.executable
               for item in learned.preview.entries)
    assert baseline.preview.config_fingerprint != learned.preview.config_fingerprint


def test_later_same_sender_matches_and_unrelated_message_does_not(context, rules_path):
    config, source, _ = context
    proposed = candidate(context)
    save_classification_rule(proposed, config, rules_path,
                             expected_fingerprint=proposed.fingerprint)
    followup = source.model_copy(update={
        "message_id": "synthetic-followup", "subject": "A different fictional topic",
        "received_at": NOW,
    })
    unrelated = source.model_copy(update={
        "message_id": "synthetic-unrelated", "sender": "Other <other@different.invalid>",
        "subject": "An unrelated fictional note", "received_at": NOW,
    })
    result = run_synthetic_scan(messages=(source, followup, unrelated),
                                learned_rules_path=rules_path, run_id="synthetic-later", as_of=NOW)
    entries = {item.message_id: item for item in result.preview.entries}
    assert entries["synthetic-followup"].category_ids == ("promotions",)
    assert entries["synthetic-unrelated"].category_ids == ()
    assert entries["synthetic-unrelated"].requires_review
    assert all(not item.authority_established and not item.executable for item in entries.values())
    assert result.preview.statistics.executed_gmail_actions == 0


def test_scan_never_creates_learned_rules(context, rules_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scan attempted automatic learning")
    monkeypatch.setattr("dam.learning.save_classification_rule", forbidden)
    result = run_synthetic_scan(learned_rules_path=rules_path,
                                run_id="synthetic-no-learning", as_of=NOW)
    target = next(item for item in result.preview.entries if item.message_id == "synthetic-004-unknown")
    assert target.category_ids == () and target.requires_review
    assert not rules_path.exists()
    assert not rules_path.parent.exists()


def test_invalid_or_unsafe_existing_yaml_is_not_overwritten(context, rules_path):
    config, _, _ = context
    proposal = candidate(context)
    rules_path.parent.mkdir(parents=True, mode=0o700)
    rules_path.write_text("schema_version: 1\nrecords: [\n", encoding="utf-8")
    rules_path.chmod(0o600)
    original = rules_path.read_bytes()
    with pytest.raises(LearningError, match="invalid_learned_rules_file"):
        save_classification_rule(proposal, config, rules_path, expected_fingerprint=proposal.fingerprint)
    assert rules_path.read_bytes() == original
    rules_path.chmod(0o644)
    with pytest.raises(LearningError, match="unsafe_learned_rules_permissions"):
        load_learned_rules(rules_path)


def test_duplicate_yaml_key_and_malformed_schema_rejected(rules_path):
    rules_path.parent.mkdir(parents=True, mode=0o700)
    for raw in ("schema_version: 1\nschema_version: 1\nrecords: []\n",
                "schema_version: true\nrecords: []\n"):
        rules_path.write_text(raw, encoding="utf-8")
        rules_path.chmod(0o600)
        with pytest.raises(LearningError, match="invalid_learned_rules_file"):
            load_learned_rules(rules_path)


def test_stale_configuration_cannot_save(context, rules_path):
    config, source, _ = context
    first = candidate(context)
    second = propose_classification_rule(source, "finance", config, as_of=NOW)
    save_classification_rule(second, config, rules_path,
                             expected_fingerprint=second.fingerprint)
    changed = configuration_with_learned_rules(config, rules_path)
    with pytest.raises(LearningError, match="stale_or_unconfirmed_candidate"):
        save_classification_rule(first, changed, rules_path,
                                 expected_fingerprint=first.fingerprint)


def test_unsafe_directory_and_symlink_rejected(context, rules_path, tmp_path):
    config, _, _ = context
    proposal = candidate(context)
    rules_path.parent.mkdir(parents=True, mode=0o755)
    with pytest.raises(LearningError, match="unsafe_learned_rules_permissions"):
        save_classification_rule(proposal, config, rules_path,
                                 expected_fingerprint=proposal.fingerprint)
    rules_path.parent.chmod(0o700)
    target = tmp_path / "target"
    target.write_text("private")
    rules_path.symlink_to(target)
    with pytest.raises(LearningError, match="unsafe_learned_rules_path"):
        load_learned_rules(rules_path)
    assert target.read_text() == "private"


def test_tampered_learned_action_rejected(context, rules_path):
    config, _, _ = context
    proposal = candidate(context)
    save_classification_rule(proposal, config, rules_path, expected_fingerprint=proposal.fingerprint)
    document = yaml.safe_load(rules_path.read_text())
    document["records"][0]["rule"]["proposed_action"] = "trash"
    rules_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    rules_path.chmod(0o600)
    with pytest.raises(LearningError, match="unsafe_learned_rule"):
        load_learned_rules(rules_path)


def test_failed_atomic_replace_preserves_old_file(context, rules_path, monkeypatch):
    config, source, _ = context
    first = candidate(context)
    save_classification_rule(first, config, rules_path,
                             expected_fingerprint=first.fingerprint)
    original = rules_path.read_bytes()
    merged = configuration_with_learned_rules(config, rules_path)
    second_message = MessageMetadata(account_id=source.account_id, message_id="synthetic-other",
                                     sender="other@other.example.invalid", subject="Fictional",
                                     received_at=NOW, label_ids=("INBOX",))
    second = propose_classification_rule(second_message, "finance", merged, as_of=NOW)
    def fail_replace(*args, **kwargs):
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr("dam.learning.os.replace", fail_replace)
    with pytest.raises(LearningError, match="learned_rules_persistence_failure"):
        save_classification_rule(second, merged, rules_path,
                                 expected_fingerprint=second.fingerprint)
    assert rules_path.read_bytes() == original
    assert sorted(path.name for path in rules_path.parent.iterdir()) == ["learned-rules.yaml"]


def test_save_rejects_forged_action_candidate(context, rules_path):
    config, _, _ = context
    proposal = candidate(context)
    forged_rule = proposal.rule.model_copy(update={"proposed_action": ProposedAction.TRASH})
    forged = proposal.model_copy(update={"rule": forged_rule})
    with pytest.raises(LearningError, match="unsafe_learned_rule"):
        save_classification_rule(forged, config, rules_path,
                                 expected_fingerprint=forged.fingerprint)
    assert not rules_path.exists()


def test_changed_private_file_after_preview_is_stale(context, rules_path):
    config, source, _ = context
    proposal = candidate(context)
    other = MessageMetadata(account_id=source.account_id, message_id="synthetic-other",
                            sender="other@other.example.invalid", subject="Fictional",
                            received_at=NOW, label_ids=("INBOX",))
    added = propose_classification_rule(other, "finance", config, as_of=NOW)
    save_classification_rule(added, config, rules_path,
                             expected_fingerprint=added.fingerprint)
    with pytest.raises(LearningError, match="stale_or_unconfirmed_candidate"):
        save_classification_rule(proposal, config, rules_path,
                                 expected_fingerprint=proposal.fingerprint)


def test_render_is_deterministic_and_clear_about_no_execution(context):
    result = candidate(context)
    text = render_candidate(result)
    assert text == render_candidate(result)
    assert "Human selected category: promotions" in text
    assert "Exact sender match:" in text
    assert "broader and was not selected" in text
    assert "Status: proposed" in text
    assert "Mailbox actions executed: 0; authority=false; executable=false." in text
    assert "synthetic-004-unknown" in text
    assert "A fictional question" not in text


def test_cli_preview_then_separate_confirmed_save_is_synthetic(context, rules_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("learning must not access OAuth or Gmail")
    monkeypatch.setattr("dam.scan.run_gmail_scan", forbidden)
    monkeypatch.setattr("dam.cli.run_gmail_scan", forbidden)
    arguments = ["learn", "--message-id", "synthetic-004-unknown", "--category", "promotions",
                 "--learned-rules-file", str(rules_path)]
    assert main(arguments) == 0
    preview = capsys.readouterr().out
    assert "Status: proposed" in preview
    assert not rules_path.exists()
    fingerprint = next(line.split(": ", 1)[1] for line in preview.splitlines()
                       if line.startswith("Candidate fingerprint:"))
    assert main([*arguments, "--save"]) == 2
    assert not rules_path.exists()
    capsys.readouterr()
    assert main([*arguments, "--save", "--confirm-fingerprint", fingerprint]) == 0
    saved = capsys.readouterr().out
    assert "Status: saved" in saved
    assert len(load_learned_rules(rules_path).records) == 1


def test_cli_invalid_category_and_unknown_message_fail_without_write(rules_path, capsys):
    base = ["learn", "--learned-rules-file", str(rules_path)]
    assert main([*base, "--message-id", "synthetic-004-unknown", "--category", "typo"]) == 2
    assert main([*base, "--message-id", "no-such-synthetic-message", "--category", "promotions"]) == 2
    with pytest.raises(SystemExit) as missing:
        main([*base, "--message-id", "synthetic-004-unknown"])
    assert missing.value.code == 2
    assert not rules_path.exists()
    assert "Traceback" not in capsys.readouterr().err


def test_learning_cli_never_calls_gmail_oauth_or_network(context, rules_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("external access attempted")
    monkeypatch.setattr("dam.auth.authenticate", forbidden)
    monkeypatch.setattr("dam.scan.authenticate", forbidden)
    monkeypatch.setattr("dam.gmail.read_inbox", forbidden)
    monkeypatch.setattr("dam.scan.read_inbox", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    real_private = Path.home() / ".config" / "dam"
    original_path_open = Path.open
    original_os_open = os.open
    def guarded_path_open(path, *args, **kwargs):
        if path.is_relative_to(real_private):
            forbidden()
        return original_path_open(path, *args, **kwargs)
    def guarded_os_open(path, *args, **kwargs):
        if Path(path).is_relative_to(real_private):
            forbidden()
        return original_os_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    arguments = ["learn", "--message-id", "synthetic-004-unknown", "--category", "promotions",
                 "--learned-rules-file", str(rules_path)]
    assert main(arguments) == 0
    preview = capsys.readouterr().out
    fingerprint = next(line.split(": ", 1)[1] for line in preview.splitlines()
                       if line.startswith("Candidate fingerprint:"))
    assert main([*arguments, "--save", "--confirm-fingerprint", fingerprint]) == 0
    assert "Mailbox actions executed: 0" in capsys.readouterr().out


def test_import_and_help_do_not_create_files_or_call_gmail(tmp_path):
    code = (
        "import dam.learning, dam.cli, dam.scan; "
        "from dam.cli import main; "
        "assert main(['learn', '--help']) == 0"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "--save" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_default_path_is_user_local_not_repository():
    path = default_learned_rules_path(Path("/tmp/synthetic-step12-home"))
    assert path == Path("/tmp/synthetic-step12-home/.config/dam/learned-rules.yaml")
    assert not path.is_relative_to(Path(__file__).resolve().parents[1])
