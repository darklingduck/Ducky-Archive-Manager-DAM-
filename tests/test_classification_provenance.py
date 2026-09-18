"""Synthetic classification acceptance remains distinct from evidence and action authority."""

from datetime import datetime, timezone
import json
import sqlite3

import yaml
import pytest
from pydantic import TypeAdapter

from dam.actions import ActionProposal, propose_action
from dam.audit import preview_from_storage, render_preview
from dam.classifier import ClassificationResult, ClassificationReviewReason, classify
from dam.config import ConfigurationError, load_config
from dam.learning import configuration_with_learned_rules, propose_classification_rule, save_classification_rule
from dam.models import Configuration, MessageMetadata, Rule, RulesConfig, Settings
from dam.review import GmailReviewResult, render_review
from dam.scan import default_config_directory, load_synthetic_messages, run_synthetic_scan
from dam.storage import ScanFinish, ScanStart, Storage


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _context(tmp_path):
    config = load_config(default_config_directory())
    source = load_synthetic_messages()[-1]
    path = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    candidate = propose_classification_rule(source, "promotions", config, as_of=NOW)
    save_classification_rule(candidate, config, path, expected_fingerprint=candidate.fingerprint,
                             saved_at=NOW)
    return config, source, path, candidate


def test_unclassified_message_needs_teaching_and_structured_review():
    config = load_config(default_config_directory())
    source = load_synthetic_messages()[-1]
    result = classify(source, config.rules, as_of=NOW, settings=config.settings,
                      category_config=config.categories)
    assert not result.category_ids and result.category_teaching_required
    assert result.requires_review
    assert ClassificationReviewReason.CATEGORY_UNRESOLVED in result.review_reasons
    assert result.classification_sources == ()


def test_human_accepted_sender_rule_classifies_later_message_without_action_authority(tmp_path):
    config, source, path, candidate = _context(tmp_path)
    before = path.read_bytes()
    learned = configuration_with_learned_rules(config, path)
    assert path.read_bytes() == before
    later = source.model_copy(update={"message_id": "synthetic-later"})
    result = classify(later, learned.rules, as_of=NOW, settings=learned.settings,
                      category_config=learned.categories)
    expected_cat = next(item.permanent_id for item in config.categories.categories
                        if item.id == "promotions")
    assert result.category_ids == ("promotions",)
    assert result.category_permanent_ids == (expected_cat,)
    assert result.classification_confidence == .90
    assert result.requires_review and not result.category_teaching_required
    assert result.review_reasons == (ClassificationReviewReason.EVIDENCE_BELOW_HIGH_THRESHOLD,)
    assert len(result.classification_sources) == 1
    provenance = result.classification_sources[0]
    assert (provenance.rule_id, provenance.rule_version) == (candidate.rule.id, 1)
    assert provenance.category_permanent_id == expected_cat
    assert provenance.basis == "human_accepted_learned_rule"
    assert provenance.match_scope == "exact_sender"
    assert provenance.candidate_fingerprint == candidate.fingerprint
    assert provenance.scope_fingerprint and len(provenance.scope_fingerprint) == 64
    assert provenance.accepted_at == NOW
    assert provenance.learned_record_schema_version == 2
    proposal = propose_action(later, result, learned.rules, as_of=NOW, settings=learned.settings)
    assert proposal.proposed_action == "no_action"
    assert not proposal.authority_established and not proposal.executable
    assert not proposal.approval_required
    shown = render_review(GmailReviewResult(message=later, classification=result, proposal=proposal,
                                            config=learned, as_of=NOW, auth_source="mock"))
    assert "Category teaching: satisfied" in shown
    assert "Evidence is below the current high-confidence threshold." in shown
    assert "must separately choose a category" not in shown


def test_preview_carries_separate_review_reasons_and_remains_deterministic(tmp_path):
    _, source, path, candidate = _context(tmp_path)
    matching = source.model_copy(update={"message_id": "synthetic-later"})
    unrelated = source.model_copy(update={"message_id": "synthetic-unrelated",
                                          "sender": "other@example.invalid"})
    args = dict(messages=(matching, unrelated), limit=2, learned_rules_path=path,
                run_id="synthetic-provenance", as_of=NOW)
    first = run_synthetic_scan(**args).preview
    second = run_synthetic_scan(**{**args, "messages": (unrelated, matching)}).preview
    assert first.to_json() == second.to_json()
    assert first.fingerprint == second.fingerprint
    entry = next(item for item in first.entries if item.message_id == "synthetic-later")
    assert not entry.category_teaching_required and entry.requires_review
    assert entry.classification_review_reasons == ("evidence_below_high_threshold",)
    assert entry.classification_sources[0].candidate_fingerprint == candidate.fingerprint
    assert entry.review_reasons  # Action-stage preservation reason remains separate.
    assert entry.action_review_reason_codes == ("classification_requires_review",)
    assert first.statistics.executed_gmail_actions == 0
    assert not first.authority_established and not first.executable
    other = next(item for item in first.entries if item.message_id == "synthetic-unrelated")
    assert other.category_teaching_required and not other.classification_sources
    from dam.audit import render_preview
    text = render_preview(first)
    assert "Classification basis: Human-taught rule" in text
    assert "Category teaching: satisfied" in text
    assert "Classification Review reasons: Evidence is below the current high-confidence threshold." in text
    assert "Action Review reasons: Classification still requires Review." in text
    assert "Category teaching: required" in text
    assert "unknown@unknown.example.invalid" not in text


def test_conflict_unknown_and_safety_reasons_remain_independent(tmp_path):
    config, source, path, candidate = _context(tmp_path)
    learned = configuration_with_learned_rules(config, path)
    conflicting = Rule(id="synthetic_conflict", version=1, match=candidate.rule.match,
                       category_ids=("finance",))
    conflict_rules = RulesConfig(rules=(*learned.rules.rules, conflicting),
                                 accepted_classifications=learned.rules.accepted_classifications)
    conflict = classify(source, conflict_rules, as_of=NOW, settings=config.settings,
                        category_config=config.categories)
    assert not conflict.category_ids and conflict.requires_review
    assert conflict.category_teaching_required is None
    assert ClassificationReviewReason.CLASSIFICATION_CONFLICT in conflict.review_reasons
    assert conflict.classification_sources == ()
    missing = MessageMetadata(account_id=source.account_id, message_id="synthetic-missing",
                              received_at=NOW, label_ids=("INBOX",))
    unknown = classify(missing, learned.rules, as_of=NOW, settings=config.settings,
                       category_config=config.categories)
    assert ClassificationReviewReason.UNKNOWN_ELIGIBILITY in unknown.review_reasons
    assert unknown.category_teaching_required and unknown.requires_review
    safety = Rule(id="synthetic_review_signal", version=1, kind="safety",
                  match=candidate.rule.match, priority_state="Review")
    safety_rules = RulesConfig(rules=(*learned.rules.rules, safety),
                               accepted_classifications=learned.rules.accepted_classifications)
    guarded = classify(source, safety_rules, as_of=NOW, settings=config.settings,
                       category_config=config.categories)
    assert guarded.category_ids == ("promotions",)
    assert not guarded.category_teaching_required
    assert ClassificationReviewReason.EXPLICIT_REVIEW_SIGNAL in guarded.review_reasons
    assert guarded.requires_review
    proposal = propose_action(source, guarded, safety_rules, as_of=NOW, settings=config.settings)
    assert not proposal.authority_established and not proposal.executable


def test_legacy_learned_rule_retains_identity_and_file_bytes(tmp_path):
    config, source, path, candidate = _context(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["schema_version"] = 1
    document["records"][0].pop("category_permanent_id")
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    before = path.read_bytes()
    learned = configuration_with_learned_rules(config, path)
    assert path.read_bytes() == before
    later = source.model_copy(update={"message_id": "synthetic-legacy-later"})
    result = classify(later, learned.rules, as_of=NOW, settings=config.settings,
                      category_config=config.categories)
    assert result.category_ids == ("promotions",)
    assert result.classification_sources[0].rule_id == candidate.rule.id
    assert result.classification_sources[0].learned_record_schema_version == 1
    assert result.classification_sources[0].basis == "human_accepted_learned_rule"


def test_accepted_provenance_cannot_be_attached_to_changed_match_scope(tmp_path):
    config, _, path, _ = _context(tmp_path)
    learned = configuration_with_learned_rules(config, path)
    old = learned.rules.accepted_classifications[0]
    changed = old.model_copy(update={"scope_fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="active exact-sender classification rule"):
        RulesConfig(rules=learned.rules.rules, accepted_classifications=(changed,))


def test_existing_sqlite_json_preserves_classification_provenance_without_schema_change(tmp_path):
    config, source, path, candidate = _context(tmp_path)
    learned = configuration_with_learned_rules(config, path)
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    effective = Configuration(settings=settings, categories=learned.categories, rules=learned.rules)
    later = source.model_copy(update={"message_id": "synthetic-persisted"})
    classification = classify(later, effective.rules, as_of=NOW, settings=effective.settings,
                              category_config=effective.categories)
    proposal = propose_action(later, classification, effective.rules, as_of=NOW,
                              settings=effective.settings)
    with Storage.open(effective.settings) as store:
        fingerprint = store.save_configuration(effective)
        start = ScanStart(run_id="synthetic-provenance", account_id=later.account_id,
                          config_fingerprint=fingerprint, started_at=NOW, as_of=NOW,
                          limit=1)
        store.start_scan(start)
        store.record_observation(start.run_id, later, classification, proposal, observed_at=NOW)
        store.finish_scan(start.run_id, ScanFinish(ended_at=NOW, status="completed"))
        loaded = store.scan_observations(start.run_id)[0].classification
        snapshot = store.configuration(fingerprint)
    assert loaded.classification_sources == classification.classification_sources
    assert loaded.review_reasons == classification.review_reasons
    assert loaded.category_permanent_ids == classification.category_permanent_ids
    assert snapshot["provenance"]["classification_rule_acceptance"][0]["candidate_fingerprint"] == candidate.fingerprint
    assert snapshot["provenance"]["category_snapshot"]


def test_pre_step_12_4_json_preserves_historical_absence_and_legacy_review_text(tmp_path):
    config, source, path, _ = _context(tmp_path)
    learned = configuration_with_learned_rules(config, path)
    database = tmp_path / "historical" / "dam.db"
    settings = Settings.model_validate({"state": {"database_path": str(database)}})
    effective = Configuration(settings=settings, categories=learned.categories, rules=learned.rules)
    message = source.model_copy(update={"message_id": "synthetic-historical"})
    classification = classify(message, effective.rules, as_of=NOW, settings=settings,
                              category_config=effective.categories)
    proposal = propose_action(message, classification, effective.rules, as_of=NOW, settings=settings)
    with Storage.open(settings) as store:
        fingerprint = store.save_configuration(effective)
        store.start_scan(ScanStart(run_id="synthetic-historical-run", account_id=message.account_id,
                                   config_fingerprint=fingerprint, started_at=NOW, as_of=NOW, limit=1))
        store.record_observation("synthetic-historical-run", message, classification, proposal,
                                 observed_at=NOW)
        store.finish_scan("synthetic-historical-run", ScanFinish(ended_at=NOW, status="completed"))
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT classification_json FROM message_observations").fetchone()
        old_classification = json.loads(row[0])
        for field in ("classification_sources", "category_permanent_ids", "category_teaching_required", "review_reasons"):
            old_classification.pop(field)
        row = connection.execute("SELECT proposal_json FROM proposals").fetchone()
        old_proposal = json.loads(row[0])
        old_proposal.pop("review_reason_codes")
        stored_classification = json.dumps(old_classification, sort_keys=True)
        stored_proposal = json.dumps(old_proposal, sort_keys=True)
        connection.execute("UPDATE message_observations SET classification_json=?", (stored_classification,))
        connection.execute("UPDATE proposals SET proposal_json=?", (stored_proposal,))
    with Storage.open(settings) as store:
        loaded_classification = store.scan_observations("synthetic-historical-run")[0].classification
        loaded_proposal = store.proposals("synthetic-historical-run")[0]
        preview = preview_from_storage(store, "synthetic-historical-run", generated_at=NOW)
        again = preview_from_storage(store, "synthetic-historical-run", generated_at=NOW)
        shown = render_preview(preview)
        assert preview.to_json() == again.to_json()
        assert preview.fingerprint == again.fingerprint
    assert loaded_classification.category_ids == ("promotions",)
    assert loaded_classification.requires_review
    assert loaded_classification.classification_sources is None
    assert loaded_classification.review_reasons is None
    assert loaded_proposal.review_reason_codes is None
    assert "Classification: promotions (0.90" in shown
    assert "Classification basis: not recorded" in shown
    assert "Category teaching: not recorded" in shown
    assert "Classification Review reasons: not recorded" in shown
    assert "Action Review reasons: not recorded" in shown
    assert "Legacy classification notes:" in shown
    assert "Legacy action Review text: Classification requires Review; cleanup is withheld." in shown
    assert "Classification basis: unresolved" not in shown
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT classification_json FROM message_observations").fetchone()[0] == stored_classification
        assert connection.execute("SELECT proposal_json FROM proposals").fetchone()[0] == stored_proposal
    # Missing historical fields differ from an explicitly recorded empty value.
    current = TypeAdapter(ClassificationResult).validate_python({**old_classification,
        "classification_sources": [], "category_permanent_ids": [], "review_reasons": []})
    explicitly_empty_proposal = ActionProposal.model_validate({**old_proposal, "review_reason_codes": []})
    assert current.classification_sources == () and current.review_reasons == ()
    assert explicitly_empty_proposal.review_reason_codes == ()


def test_ordinary_rules_yaml_cannot_claim_human_acceptance(tmp_path):
    config, _, path, _ = _context(tmp_path)
    original_learned_file = path.read_bytes()
    learned = configuration_with_learned_rules(config, path)
    directory = tmp_path / "synthetic-config"
    directory.mkdir()
    for name in ("settings", "categories", "rules"):
        (directory / f"{name}.yaml").write_bytes(
            (default_config_directory() / f"{name}.yaml").read_bytes())
    assert not load_config(directory).rules.accepted_classifications
    before = (directory / "rules.yaml").read_text(encoding="utf-8")
    claimed = learned.rules.accepted_classifications[0].model_dump(mode="json")
    document = yaml.safe_load(before)
    document["accepted_classifications"] = [claimed]
    (directory / "rules.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="rules.yaml: accepted_classifications requires the private learned-rule loader"):
        load_config(directory)
    assert path.read_bytes() == original_learned_file
    reloaded = configuration_with_learned_rules(config, path)
    assert reloaded.rules.accepted_classifications == learned.rules.accepted_classifications
    assert all(rule.proposed_action == "no_action" for rule in reloaded.rules.rules if rule.id.startswith("learned_"))
