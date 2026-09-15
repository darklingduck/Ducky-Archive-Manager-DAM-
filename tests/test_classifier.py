"""Synthetic Step 4 classification; no services, actions, or approval records."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from itertools import permutations

import pytest
from pydantic import ValidationError

from dam.classifier import classify
from dam.models import (
    ConfidenceSettings, MatchSpec, MessageMetadata, PriorityState, Rule,
    RulesConfig, Settings,
)
from dam.rules import evaluate_match

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def message(**changes):
    return MessageMetadata(**{
        "account_id": "synthetic_account", "message_id": "synthetic_message",
        "sender": "sender@example.invalid", "subject": "Weekly receipt notice",
        "received_at": NOW, "label_ids": ("INBOX",), **changes,
    })


def rule(id="record", **changes):
    return Rule(**{
        "id": id, "version": 1,
        "match": MatchSpec(sender_domains_any=("example.invalid",),
                           subject_contains_any=("receipt",)),
        "category_ids": ("finance",), **changes,
    })


def run(*rules, metadata=None, settings=None):
    return classify(message() if metadata is None else metadata,
                    RulesConfig(rules=rules), as_of=NOW, settings=settings)


def test_clear_match_reuses_step_three_evidence():
    configured = rule()
    result = run(configured)
    assert result.category_ids == ("finance",)
    assert result.matched_rules == result.selected_rules == (("record", 1),)
    assert not result.requires_review
    assert result.classification_confidence == .95
    assert result.confidence_band == "high"
    assert result.assessments[0].positive == evaluate_match(
        message(), configured.match, as_of=NOW, rule_id="record", rule_version=1, policy_version=1)
    assert result.evidence[-1].confidence == .95
    assert result.limitations


def test_no_matching_rule_and_empty_configuration():
    for result in (run(), run(rule(match=MatchSpec(subject_contains_any=("missing",))))):
        assert not result.category_ids
        assert not result.matched_rules
        assert result.requires_review
        assert result.priority_state == PriorityState.REVIEW
        assert result.classification_confidence == 0
    assert run(rule(match=MatchSpec(subject_contains_any=("missing",)))).assessments[0].status == "rejected"


@pytest.mark.parametrize("changes", [
    {"match": MatchSpec(body_contains_any=("receipt",))},
    {"exclude": MatchSpec(body_contains_any=("security",))},
    {"match": MatchSpec(relationship_status_any=("Current",))},
])
def test_unknown_prevents_positive_classification(changes):
    result = run(rule(**changes))
    assert not result.category_ids and not result.matched_rules
    assert result.assessments[0].status == "unresolved"
    assert result.requires_review
    assert any(e.outcome == "unknown" for e in result.evidence)


def test_unknown_competing_protection_requires_review_without_asserting_protection():
    result = run(rule(), rule("possible", protect=True,
                             match=MatchSpec(body_contains_any=("security",))))
    assert result.category_ids == ("finance",)
    assert not result.protected
    assert result.requires_review and result.classification_confidence == .5


def test_exclusion_blocks_and_definitively_false_exclusion_allows():
    blocked = run(rule(exclude=MatchSpec(subject_contains_any=("receipt",))))
    assert blocked.assessments[0].status == "excluded"
    assert not blocked.category_ids
    allowed = run(rule(exclude=MatchSpec(subject_contains_any=("security",))))
    assert allowed.category_ids == ("finance",)
    assert allowed.assessments[0].exclusion.outcome == "not_matched"


def test_disproved_positive_with_unknown_condition_is_rejected():
    result = run(rule(match=MatchSpec(subject_contains_any=("absent",), body_contains_any=("x",))))
    assert result.assessments[0].status == "rejected"


@pytest.mark.parametrize("protection", [
    {"protect": True}, {"kind": "retention", "retention": {"duration_days": None}},
    {"kind": "safety"},
])
def test_protection_category_precedes_ordinary_regardless_of_priority(protection):
    result = run(rule("ordinary", priority=10000, category_ids=("promotions",)),
                 rule("protected", category_ids=("records",), **protection))
    assert result.category_ids == ("records",)
    assert result.protected
    assert result.protection_rules == (("protected", 1),)


@pytest.mark.parametrize("state", ["Critical", "Priority", "Review"])
def test_actionable_states_preserved_and_precede_ordinary(state):
    result = run(rule("ordinary", priority=9999),
                 rule("signal", priority_state=state, category_ids=("security",)))
    assert result.category_ids == ("security",)
    assert result.priority_state == state
    assert state in result.priority_states and result.protected
    assert result.requires_review == (state == "Review")


def test_protection_only_does_not_create_or_erase_category():
    signal = rule("signal", category_ids=(), priority_state="Critical")
    assert run(signal).category_ids == ()
    result = run(rule(), signal)
    assert result.category_ids == ("finance",)
    assert result.priority_state == "Critical" and result.protected


def test_lower_rank_signals_survive_and_review_does_not_erase_critical():
    result = run(rule("critical", priority_state="Critical", priority=10),
                 rule("review", priority_state="Review", priority=1))
    assert result.priority_state == "Critical"
    assert result.priority_states == (PriorityState.CRITICAL, PriorityState.REVIEW)
    assert result.requires_review
    assert len(result.protection_rules) == 2


def test_service_specific_and_specific_vs_general():
    general = rule("general", match=MatchSpec(label_ids_any=("INBOX",)), priority=999)
    specific = rule("specific", category_ids=("records",))
    assert run(general, specific).category_ids == ("records",)
    service = rule("service", kind="service_specific", category_ids=("service",))
    assert run(specific, service).category_ids == ("service",)
    # A service label without sender evidence grants no elevated authority.
    ungrounded = rule("ungrounded", kind="service_specific", category_ids=("other",),
                      match=MatchSpec(label_ids_any=("INBOX",)), priority=9999)
    assert run(specific, ungrounded).category_ids == ("records",)


def test_configured_priority_before_specificity_in_same_tier():
    narrow = rule("narrow", category_ids=("narrow",))
    broad = rule("broad", match=MatchSpec(sender_domains_any=("example.invalid",)),
                 priority=1, category_ids=("broad",))
    assert run(narrow, broad).category_ids == ("broad",)


@pytest.mark.parametrize("broad,narrow", [
    (MatchSpec(sender_domains_any=("example.invalid",)),
     MatchSpec(sender_domains_any=("example.invalid",), subject_contains_any=("receipt",))),
    (MatchSpec(sender_domains_any=("example.invalid",)),
     MatchSpec(sender_emails_any=("sender@example.invalid",))),
    (MatchSpec(sender_domains_any=("example.invalid",), include_subdomains=True),
     MatchSpec(sender_domains_any=("example.invalid",))),
    (MatchSpec(subject_contains_any=("receipt", "weekly")),
     MatchSpec(subject_contains_any=("receipt",))),
])
def test_specificity_ties(broad, narrow):
    result = run(rule("broad", match=broad), rule("narrow", match=narrow, category_ids=("narrow",)))
    assert result.category_ids == ("narrow",)


def test_equal_authority_category_conflict_withholds_candidates_and_requires_review():
    result = run(rule("first"), rule("second", category_ids=("promotions",)))
    assert result.category_ids == ()
    assert result.selected_rules == (("first", 1), ("second", 1))
    assert result.requires_review and result.classification_confidence == 0
    assert {a.category_ids for a in result.assessments} == {("finance",), ("promotions",)}
    assert any("category conflict" in reason for reason in result.reasons)


def test_identical_category_sets_agree_regardless_of_category_order():
    result = run(rule("first", category_ids=("finance", "records")),
                 rule("second", category_ids=("records", "finance")))
    assert result.category_ids == ("finance", "records")
    assert not result.requires_review


def test_equal_authority_protection_conflict_retains_strongest_signals():
    result = run(rule("first", kind="safety", priority_state="Critical"),
                 rule("second", kind="safety", priority_state="Archived"))
    assert result.category_ids == ("finance",)
    assert result.protected and result.requires_review
    assert result.priority_state == "Critical"
    assert "Archived" in result.priority_states
    assert any("protection conflict" in reason for reason in result.reasons)


def test_fallback_only_fills_missing_category_and_unknown_is_not_cured():
    fallback = rule("fallback", kind="fallback", match=MatchSpec(),
                    category_ids=("unknown",), priority=9999)
    assert run(rule(), fallback).category_ids == ("finance",)
    result = run(fallback)
    assert result.category_ids == ("unknown",) and result.requires_review
    assert result.classification_confidence == 0
    result = run(fallback, rule(match=MatchSpec(body_contains_any=("receipt",))))
    assert result.category_ids == ("unknown",) and result.requires_review
    assert result.assessments[1].status == "unresolved"


@pytest.mark.parametrize("spec,score", [
    (MatchSpec(sender_domains_any=("example.invalid",)), .90),
    (MatchSpec(subject_contains_any=("receipt",)), .80),
    (MatchSpec(label_ids_any=("INBOX",)), .75),
    (MatchSpec(min_age_days=0), .50),
])
def test_evidence_confidence_rubric_and_limitations(spec, score):
    result = run(rule(match=spec))
    assert result.classification_confidence == score
    assert result.requires_review
    assert result.evidence[-1].match_type == "metadata_rubric_v1"
    assert "Rubric v1" in result.evidence[-1].explanation
    assert result.evidence[-1].limitations


def test_configured_confidence_thresholds_and_policy_provenance():
    settings = Settings(policy_version=2, confidence=ConfidenceSettings(
        auto_threshold=.99, review_threshold=.96))
    result = run(rule(), settings=settings)
    assert result.classification_confidence == .95
    assert result.confidence_band == "insufficient" and result.requires_review
    assert all(e.policy_version == 2 for e in result.evidence)
    assert result.policy_version == 2


def test_order_independence_no_mutation_and_deep_immutability():
    configured = (rule("first"), rule("second", category_ids=("promotions",)),
                  rule("disabled", enabled=False))
    metadata = message()
    before = tuple(r.model_dump_json() for r in configured), metadata.model_dump_json(), metadata.model_fields_set.copy()
    expected = run(*configured, metadata=metadata)
    for ordering in permutations(configured):
        assert run(*ordering, metadata=metadata) == expected
    assert before == (tuple(r.model_dump_json() for r in configured), metadata.model_dump_json(), metadata.model_fields_set)
    assert expected.assessments[0].status == "disabled"
    assert expected.assessments[0].positive is None
    with pytest.raises(FrozenInstanceError):
        expected.requires_review = False
    with pytest.raises(FrozenInstanceError):
        expected.assessments[0].status = "applicable"
    with pytest.raises(ValidationError):
        expected.evidence[0].confidence = 1.0


def test_no_actions_approval_authority_or_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = rule()
    described = rule(proposed_action="trash", approval_ref="synthetic_reference")
    assert run(original) == run(described)
    result = run(described)
    forbidden = {"action", "proposed_action", "action_confidence", "approved", "approval_ref"}
    assert not forbidden & {f.name for f in fields(result)}
    assert not forbidden & {f.name for f in fields(result.assessments[0])}
    assert list(tmp_path.iterdir()) == []


def test_explicit_aware_time_required_even_without_rules():
    with pytest.raises(ValueError, match="timezone-aware"):
        classify(message(), RulesConfig(rules=()), as_of=NOW.replace(tzinfo=None))
