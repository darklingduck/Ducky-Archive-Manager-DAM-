"""Synthetic proposal-only Step 5 acceptance cases."""

from datetime import datetime, timedelta, timezone
from itertools import permutations
import builtins
import socket

import pytest
from pydantic import ValidationError

from dam.actions import propose_action
from dam.classifier import classify
from dam.models import (
    ConfidenceSettings, MatchSpec, MessageMetadata, ProposedAction, Rule,
    RulesConfig, Settings,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def message(**changes):
    return MessageMetadata(**{
        "account_id": "synthetic_account", "message_id": "synthetic_message",
        "sender": "news@example.invalid", "subject": "Weekly offers",
        "received_at": NOW - timedelta(days=30), "label_ids": ("INBOX",), **changes,
    })


def rule(id="offers", **changes):
    return Rule(**{
        "id": id, "version": 1,
        "match": MatchSpec(sender_domains_any=("example.invalid",),
                           subject_contains_any=("offers",)),
        "category_ids": ("promotions",), "proposed_action": "archive", **changes,
    })


def run(*configured, metadata=None, settings=None, as_of=NOW):
    metadata = metadata if metadata is not None else message()
    rules = RulesConfig(rules=configured)
    result = classify(metadata, rules, as_of=as_of, settings=settings)
    return propose_action(metadata, result, rules, as_of=as_of, settings=settings)


@pytest.mark.parametrize("action", list(ProposedAction))
def test_clear_proposals_and_approval_distinctions(action):
    result = run(rule(proposed_action=action))
    assert result.proposed_action == action
    assert result.classification_confidence == .95
    assert result.action_confidence == .95
    assert result.supporting_rules == (("offers", 1),)
    assert result.category_ids == ("promotions",)
    assert not result.authority_established and not result.executable
    expected = ("destructive" if action == "trash" else "mailbox_mutation"
                if action in ("archive", "label") else "none")
    assert result.approval_type == expected
    assert result.approval_required == (expected != "none")
    assert result.approval_status == ("missing" if expected != "none" else "not_required")
    assert result.evidence[0].match_type == "action_rubric_v1"


@pytest.mark.parametrize("state", ["Critical", "Priority", "Review"])
@pytest.mark.parametrize("cleanup", ["archive", "trash"])
def test_actionable_states_block_cleanup(state, cleanup):
    result = run(rule(proposed_action=cleanup), rule("signal", proposed_action="archive",
                                                  priority_state=state))
    assert result.proposed_action == "mark_review"
    assert state in result.protection_signals
    assert not result.executable


def test_approval_reference_never_establishes_authority():
    result = run(rule(proposed_action="trash", approval_ref="synthetic_approval"))
    assert result.proposed_action == "trash"
    assert result.approval_required and result.approval_type == "destructive"
    assert result.approval_status == "reference_unverified"
    assert result.approval_references == ("synthetic_approval",)
    assert not result.authority_established and not result.executable
    assert any("authority is not established" in r for r in result.reasons)
    with pytest.raises(ValidationError):
        type(result)(**{**result.model_dump(), "executable": True})


def test_low_classification_confidence_blocks_trash_independently():
    # The action rule has complete evidence; a higher-ranked category-only rule
    # supplies lower classification confidence. Its no_action also preserves.
    result = run(rule(proposed_action="trash", category_ids=()),
                 rule("category", proposed_action="trash", priority=1,
                      match=MatchSpec(sender_domains_any=("example.invalid",))))
    assert result.classification_confidence == .9
    assert result.proposed_action == "mark_review"
    assert any("Classification confidence" in r for r in result.review_reasons)


def test_low_action_confidence_with_high_classification_confidence():
    raw = message().model_dump()
    del raw["label_ids"]
    result = run(rule(proposed_action="trash"), metadata=MessageMetadata(**raw))
    assert result.classification_confidence == .95
    assert result.action_confidence == .75
    assert result.proposed_action == "mark_review"
    assert any("labels were not inspected" in r for r in result.review_reasons)


@pytest.mark.parametrize("changes", [
    {"exclude": MatchSpec(subject_contains_any=("offers",))},
    {"exclude": MatchSpec(body_contains_any=("security",))},
    {"match": MatchSpec(body_contains_any=("offers",))},
    {"enabled": False},
    {"match": MatchSpec(subject_contains_any=("absent",))},
])
def test_ineligible_rules_cannot_propose_trash(changes):
    result = run(rule(proposed_action="trash", **changes))
    assert result.proposed_action == "no_action"
    assert not result.supporting_rules
    assert result.review_reasons


def test_unknown_other_rule_blocks_definitive_cleanup():
    result = run(rule(proposed_action="trash"), rule("uncertain", protect=True,
                 match=MatchSpec(body_contains_any=("security",))))
    assert result.proposed_action == "mark_review"
    assert result.action_confidence <= .5


@pytest.mark.parametrize("duration,state", [(31, "unexpired"), (None, "indefinite"),
                                           (30, "expired"), (1, "expired")])
@pytest.mark.parametrize("cleanup", ["archive", "trash"])
def test_retention_preserves_even_after_expiration(duration, state, cleanup):
    result = run(rule(proposed_action=cleanup), rule("retention", kind="retention",
                 proposed_action="no_action", retention={"duration_days": duration}))
    assert result.proposed_action == "no_action"
    assert result.retention_constraints[0].state == state
    assert result.retention_constraints[0].duration_days == duration
    assert "protected" in result.protection_signals
    assert not result.authority_established


def test_retention_rule_itself_cannot_turn_expiration_into_cleanup():
    result = run(rule(kind="retention", retention={"duration_days": 1}, proposed_action="trash"))
    assert result.retention_constraints[0].state == "expired"
    assert result.proposed_action == "mark_review"


def test_unknown_retention_preserves_and_huge_duration_does_not_overflow():
    future = run(rule(retention={"duration_days": 1}), metadata=message(received_at=NOW + timedelta(days=1)))
    assert future.retention_constraints[0].state == "unknown"
    assert future.proposed_action == "mark_review"
    result = run(rule(retention={"duration_days": 10**20}))
    assert result.retention_constraints[0].state == "unexpired"
    result = run(rule(proposed_action="trash"), rule("uncertain", retention={"duration_days": None},
                 match=MatchSpec(body_contains_any=("record",))))
    assert result.retention_constraints[0].state == "unknown"
    assert result.proposed_action == "mark_review"


def test_retention_boundary_utc_and_protected_types():
    result = run(rule(retention={"duration_days": 30, "protected_types": ("security", "billing")}),
                 as_of=NOW - timedelta(microseconds=1))
    assert result.retention_constraints[0].state == "unexpired"
    assert result.retention_constraints[0].protected_types == ("billing", "security")
    shifted = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert run(rule(retention={"duration_days": 30}), as_of=shifted) == run(rule(retention={"duration_days": 30}))


def test_archive_trash_conflict_and_no_action_conflict_preserve():
    for action in ("archive", "no_action"):
        result = run(rule("first", proposed_action=action), rule("second", proposed_action="trash"))
        assert result.proposed_action == "mark_review"
        assert result.action_confidence == .5
        assert "trash" in result.considered_actions
        assert any("conflict" in r for r in result.review_reasons)


def test_protection_beats_high_priority_cleanup():
    result = run(rule(proposed_action="trash", priority=9999),
                 rule("protected", protect=True, proposed_action="no_action"))
    assert result.proposed_action == "no_action"
    assert result.protection_rules == (("protected", 1),)


def test_action_rank_reuses_classifier_rank_without_category_requirement():
    result = run(rule("category", proposed_action="no_action"),
                 rule("action", proposed_action="archive", category_ids=(), priority=1))
    assert result.proposed_action == "archive"
    assert result.supporting_rules == (("action", 1),)
    assert result.category_ids == ("promotions",)


def test_empty_rules_no_action_retains_review_explanation():
    result = run()
    assert result.proposed_action == "no_action"
    assert result.review_reasons
    assert not result.approval_required


def test_determinism_immutability_and_no_input_mutation():
    configured = (rule("archive"), rule("trash", proposed_action="trash"),
                  rule("retention", retention={"duration_days": None}))
    metadata = message()
    before = metadata.model_dump_json(), tuple(r.model_dump_json() for r in configured)
    expected = run(*configured, metadata=metadata)
    for ordering in permutations(configured):
        assert run(*ordering, metadata=metadata) == expected
    assert before == (metadata.model_dump_json(), tuple(r.model_dump_json() for r in configured))
    for obj, name, value in [(expected, "executable", True),
                              (expected.evidence[0], "confidence", 1.0),
                              (expected.retention_constraints[0], "state", "expired")]:
        with pytest.raises(ValidationError):
            setattr(obj, name, value)


def test_no_io_or_execution_side_effects(tmp_path, monkeypatch):
    metadata = message()
    rules = RulesConfig(rules=(rule(proposed_action="trash"),))
    classification = classify(metadata, rules, as_of=NOW)
    monkeypatch.chdir(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected IO")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr("dam.rules.evaluate_match", forbidden)
        patch.setattr("dam.classifier.classify", forbidden)
        result = propose_action(metadata, classification, rules, as_of=NOW)
    assert result.proposed_action == "trash"
    assert list(tmp_path.iterdir()) == []


def test_permanent_deletion_not_representable():
    assert set(ProposedAction) == {"no_action", "classify", "label", "archive", "mark_priority",
                                   "mark_review", "mark_unsubscribe_candidate", "trash"}
    with pytest.raises(ValidationError):
        rule(proposed_action="permanent_delete")


def test_threshold_configuration_and_input_mismatch():
    settings = Settings(confidence=ConfidenceSettings(destructive_threshold=.99))
    result = run(rule(proposed_action="trash"), settings=settings)
    assert result.proposed_action == "mark_review"
    assert result.classification_confidence == .95
    with pytest.raises(ValidationError):
        ConfidenceSettings(destructive_threshold=.5)
    rules = RulesConfig(rules=(rule(),))
    classification = classify(message(), rules, as_of=NOW)
    with pytest.raises(ValueError, match="identity or policy"):
        propose_action(message(message_id="other"), classification, rules, as_of=NOW)
    with pytest.raises(ValueError, match="inventory"):
        propose_action(message(), classification, RulesConfig(rules=()), as_of=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        propose_action(message(), classification, rules, as_of=NOW.replace(tzinfo=None))
