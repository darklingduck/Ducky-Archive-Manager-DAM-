"""Synthetic, metadata-only matching tests; no external services."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest
from pydantic import ValidationError

from dam.models import EvidenceOutcome, MatchSpec, MessageMetadata, Rule
from dam.rules import MatchResult, evaluate_match


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def message(**changes):
    return MessageMetadata.model_validate({
        "account_id": "synthetic_account", "message_id": "synthetic_message",
        "sender": "alerts@example.invalid", "subject": "Weekly JOB alert",
        "received_at": NOW - timedelta(days=10), "label_ids": ["INBOX", "Label_1"],
        **changes,
    })


def evaluate(spec, metadata=None):
    return evaluate_match(message() if metadata is None else metadata, MatchSpec(**spec), as_of=NOW)


@pytest.mark.parametrize("sender, expected", [
    ("ALERTS@EXAMPLE.INVALID", "matched"),
    ('"Synthetic, Sender" <ALERTS@EXAMPLE.INVALID>', "matched"),
    ("alerts+extra@example.invalid", "not_matched"),
    ("other@example.invalid", "not_matched"),
    ("alerts@sub.example.invalid", "not_matched"),
    (None, "unknown"), ("not an address", "unknown"),
    ("alerts@example.invalid, other@example.invalid", "unknown"),
    ("Synthetic <alerts@example.invalid", "unknown"),
    ("Group: alerts@example.invalid;", "unknown"),
    ("alerts@example.invalid\nBcc: other@example.invalid", "unknown"),
])
def test_exact_email_and_conservative_standard_library_parsing(sender, expected):
    result = evaluate({"sender_emails_any": ["missing@example.invalid", "ALERTS@example.invalid"]}, message(sender=sender))
    assert result.outcome == expected
    assert result.evidence[0].field == "sender"
    assert result.evidence[0].match_type == "exact_email_any"
    assert result.evidence[0].limitations


@pytest.mark.parametrize("domain, include, expected", [
    ("example.invalid", False, "matched"),
    ("EXAMPLE.INVALID", True, "matched"),
    ("mail.example.invalid", False, "not_matched"),
    ("mail.example.invalid", True, "matched"),
    ("deep.mail.example.invalid", True, "matched"),
    ("notexample.invalid", True, "not_matched"),
    ("example.invalid.other.invalid", True, "not_matched"),
])
def test_domain_boundaries(domain, include, expected):
    assert evaluate({"sender_domains_any": ["example.invalid"], "include_subdomains": include}, message(sender=f"sender@{domain}")).outcome == expected


@pytest.mark.parametrize("field, keywords, subject, expected", [
    ("subject_contains_any", ["missing", "JOB"], "Weekly job alert", "matched"),
    ("subject_contains_all", ["weekly", "ALERT"], "Weekly job alert", "matched"),
    ("subject_contains_all", ["weekly", "receipt"], "Weekly job alert", "not_matched"),
    ("subject_contains_any", ["job"], "", "not_matched"),
    ("subject_contains_any", ["job"], None, "unknown"),
    ("subject_contains_all", ["job"], None, "unknown"),
    ("subject_contains_any", ["STRASSE"], "Straße", "matched"),
    ("subject_contains_any", ["a.*t"], "alert", "not_matched"),
    ("subject_contains_any", ["job"], "jobs", "matched"),
])
def test_subject_substrings(field, keywords, subject, expected):
    assert evaluate({field: keywords}, message(subject=subject)).outcome == expected


def test_subject_all_validation_and_normalization():
    assert MatchSpec(subject_contains_all=[" ALERT "]).subject_contains_all == ("alert",)
    for values in ([""], ["Alert", "ALERT"]):
        with pytest.raises(ValidationError):
            MatchSpec(subject_contains_all=values)


@pytest.mark.parametrize("field, ids, expected", [
    ("label_ids_any", ["missing", "INBOX"], "matched"),
    ("label_ids_all", ["INBOX", "Label_1"], "matched"),
    ("label_ids_all", ["INBOX", "missing"], "not_matched"),
    ("label_ids_any", ["inbox"], "not_matched"),
    ("label_ids_any", ["Display name"], "not_matched"),
])
def test_labels_are_exact_ids(field, ids, expected):
    assert evaluate({field: ids}).outcome == expected


@pytest.mark.parametrize("field", ["label_ids_any", "label_ids_all"])
def test_omitted_labels_unknown_and_explicit_empty_labels_known(field):
    data = message().model_dump()
    del data["label_ids"]
    metadata = MessageMetadata(**data)
    assert evaluate({field: ["INBOX"]}, metadata).outcome == "unknown"
    assert evaluate({field: ["INBOX"]}, message(label_ids=[])).outcome == "not_matched"
    restored = MessageMetadata.model_validate_json(metadata.model_dump_json(exclude_unset=True))
    assert evaluate({field: ["INBOX"]}, restored).outcome == "unknown"


@pytest.mark.parametrize("field, offset_us, expected", [
    ("min_age_days", -1, "not_matched"), ("min_age_days", 0, "matched"),
    ("min_age_days", 1, "matched"), ("max_age_days", -1, "matched"),
    ("max_age_days", 0, "matched"), ("max_age_days", 1, "not_matched"),
])
def test_inclusive_age_boundaries_without_day_rounding(field, offset_us, expected):
    metadata = message(received_at=NOW - timedelta(days=10, microseconds=offset_us))
    assert evaluate({field: 10}, metadata).outcome == expected


def test_age_utc_zero_future_and_large_bounds():
    spec = MatchSpec(min_age_days=10, max_age_days=10)
    metadata = message(header_date=NOW)
    assert evaluate_match(metadata, spec, as_of=NOW).outcome == "matched"
    shifted = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert evaluate_match(metadata, spec, as_of=shifted) == evaluate_match(metadata, spec, as_of=NOW)
    assert evaluate({"min_age_days": 0, "max_age_days": 0}, message(received_at=NOW)).outcome == "matched"
    assert evaluate({"min_age_days": 0}, message(received_at=NOW + timedelta(microseconds=1))).outcome == "unknown"
    assert evaluate({"max_age_days": 10**20}).outcome == "matched"
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_match(metadata, spec, as_of=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("positive, exclusion", [
    ("job alert", "receipt"), ("job alert", "job alert"),
])
def test_exclusions_are_independent_observations(positive, exclusion):
    rule = Rule(id="synthetic_rule", version=1,
                match=MatchSpec(subject_contains_any=[positive]),
                exclude=MatchSpec(subject_contains_any=[exclusion]))
    assert evaluate_match(message(), rule.match, as_of=NOW).outcome == "matched"
    result = evaluate_match(message(), rule.exclude, as_of=NOW)
    assert result.outcome == ("matched" if exclusion == positive else "not_matched")
    assert evaluate_match(message(subject=None), rule.exclude, as_of=NOW).outcome == "unknown"


@pytest.mark.parametrize("left,right", product(EvidenceOutcome, repeat=2))
def test_three_valued_and_truth_table_and_no_short_circuit(left, right):
    metadata = message(
        sender={"matched": "alerts@example.invalid", "not_matched": "other@other.invalid", "unknown": None}[left],
        subject={"matched": "job", "not_matched": "receipt", "unknown": None}[right],
    )
    result = evaluate({"sender_domains_any": ["example.invalid"], "subject_contains_any": ["job"]}, metadata)
    assert [item.outcome for item in result.evidence] == [left, right]
    expected = "not_matched" if "not_matched" in (left, right) else "unknown" if "unknown" in (left, right) else "matched"
    assert result.outcome == expected


def test_unavailable_data_and_mixed_outcomes():
    result = evaluate({"sender_domains_any": ["example.invalid"], "subject_contains_any": ["receipt"],
                       "body_contains_any": ["job"], "relationship_status_any": ["Unknown"]})
    assert [item.outcome for item in result.evidence] == ["matched", "not_matched", "unknown", "unknown"]
    assert result.outcome == "not_matched"
    assert all(item.limitations for item in result.evidence[2:])


def test_empty_spec_has_no_evidence_and_unconditional_match():
    assert evaluate({}) == MatchResult(EvidenceOutcome.MATCHED, ())


def test_deterministic_evidence_provenance_privacy_and_no_input_mutation():
    metadata = message()
    spec = MatchSpec(sender_domains_any=["example.invalid"], subject_contains_all=["job", "weekly"])
    before = metadata.model_dump_json(), spec.model_dump_json(), metadata.model_fields_set.copy()
    kwargs = dict(as_of=NOW, rule_id="synthetic_rule", rule_version=2, policy_version=1)
    result = evaluate_match(metadata, spec, **kwargs)
    assert result == evaluate_match(metadata, spec, **kwargs)
    assert [item.model_dump_json() for item in result.evidence] == [item.model_dump_json() for item in evaluate_match(metadata, spec, **kwargs).evidence]
    assert before == (metadata.model_dump_json(), spec.model_dump_json(), metadata.model_fields_set)
    for item in result.evidence:
        assert (item.rule_id, item.rule_version, item.policy_version) == ("synthetic_rule", 2, 1)
        assert item.confidence is None
        assert metadata.sender not in item.model_dump_json()
        assert metadata.subject not in item.model_dump_json()
    with pytest.raises(FrozenInstanceError):
        result.outcome = EvidenceOutcome.UNKNOWN
    with pytest.raises(ValidationError):
        result.evidence[0].outcome = EvidenceOutcome.UNKNOWN


def test_matching_has_no_action_or_authority_output_or_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rule = Rule(id="synthetic_rule", version=1, match=MatchSpec(sender_domains_any=["example.invalid"]),
                proposed_action="trash", approval_ref="synthetic_reference_only")
    before = rule.model_dump_json()
    result = evaluate_match(message(), rule.match, as_of=NOW, rule_id=rule.id, rule_version=rule.version)
    assert result.outcome == "matched"
    assert {field.name for field in fields(result)} == {"outcome", "evidence"}
    for evidence in result.evidence:
        assert not {"approved", "action", "proposed_action", "approval_ref", "classification"} & evidence.model_dump().keys()
    assert rule.model_dump_json() == before
    assert list(tmp_path.iterdir()) == []
