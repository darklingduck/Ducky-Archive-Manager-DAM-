"""Synthetic tests for initial validation boundaries, not classification logic."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from dam.models import Evidence, EvidenceOutcome, MessageMetadata


@pytest.fixture
def fixture_data():
    return json.loads((Path(__file__).parent / "fixtures/messages.json").read_text())


@pytest.fixture
def message_data(fixture_data):
    return fixture_data["messages"][0].copy()


@pytest.fixture
def evidence_data(fixture_data):
    return fixture_data["evidence"][0].copy()


def test_synthetic_fixtures_and_individual_message_identity(fixture_data):
    messages = [MessageMetadata.model_validate(item) for item in fixture_data["messages"]]
    assert messages[0].thread_id == messages[1].thread_id
    assert messages[0].message_id != messages[1].message_id
    assert messages[2].sender is None
    assert messages[2].header_date is None
    assert messages[2].subject == ""
    evidence = [Evidence.model_validate(item) for item in fixture_data["evidence"]]
    assert evidence[1].outcome is EvidenceOutcome.UNKNOWN
    assert evidence[1].confidence is None


def test_dates_normalize_to_utc(message_data):
    message = MessageMetadata.model_validate(message_data)
    assert message.received_at == datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
    assert message.header_date.tzinfo is timezone.utc


@pytest.mark.parametrize("field", ["received_at", "header_date"])
@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-09-12T10:00:00", 1789200000000, "1789200000", True],
)
def test_invalid_or_ambiguous_dates_rejected(message_data, field, value):
    message_data[field] = value
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("field", ["account_id", "message_id", "thread_id", "history_id"])
@pytest.mark.parametrize("value", ["", "   ", 123, True])
def test_identifiers_are_nonblank_strings(message_data, field, value):
    message_data[field] = value
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


def test_missing_subject_is_not_an_empty_subject(message_data):
    message_data.pop("subject")
    assert MessageMetadata.model_validate(message_data).subject is None


@pytest.mark.parametrize("field", ["account_id", "message_id", "received_at"])
def test_required_message_fields_cannot_be_missing(message_data, field):
    message_data.pop(field)
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("value", ["", "   ", 123])
def test_present_sender_must_be_nonblank_text(message_data, value):
    message_data["sender"] = value
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("value", [123, True, ["subject"]])
def test_subject_is_not_silently_coerced(message_data, value):
    message_data["subject"] = value
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("labels", [["INBOX", "INBOX"], [""], [123], "INBOX"])
def test_invalid_labels_rejected(message_data, labels):
    message_data["label_ids"] = labels
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("field", ["body", "snippet", "attachments", "unsubscribe_url", "action"])
def test_content_and_action_fields_rejected(message_data, field):
    message_data[field] = "synthetic-only"
    with pytest.raises(ValidationError):
        MessageMetadata.model_validate(message_data)


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), "0.95", True])
def test_invalid_confidence_rejected(evidence_data, value):
    evidence_data["confidence"] = value
    with pytest.raises(ValidationError):
        Evidence.model_validate(evidence_data)


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_confidence_boundaries_accepted(evidence_data, value):
    evidence_data["confidence"] = value
    assert Evidence.model_validate(evidence_data).confidence == value


@pytest.mark.parametrize("field", ["rule_version", "policy_version"])
@pytest.mark.parametrize("value", [0, -1, "1", True])
def test_versions_are_positive_integers(evidence_data, field, value):
    evidence_data[field] = value
    with pytest.raises(ValidationError):
        Evidence.model_validate(evidence_data)


@pytest.mark.parametrize("field", ["field", "match_type", "explanation"])
def test_evidence_requires_explanation_and_identifiers(evidence_data, field):
    evidence_data[field] = "   "
    with pytest.raises(ValidationError):
        Evidence.model_validate(evidence_data)


def test_evidence_does_not_accept_approval_or_unknown_outcomes(evidence_data):
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence_data, "approved": True})
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence_data, "outcome": "confirmed"})


def test_metadata_and_evidence_json_round_trip(message_data, evidence_data):
    for model, data in [(MessageMetadata, message_data), (Evidence, evidence_data)]:
        original = model.model_validate(data)
        assert model.model_validate_json(original.model_dump_json()) == original


def test_models_are_immutable_and_reprs_minimize_private_text(message_data, evidence_data):
    message = MessageMetadata.model_validate(message_data)
    evidence = Evidence.model_validate(evidence_data)
    assert isinstance(message.label_ids, tuple)
    for model, field, value in [(message, "subject", "changed"), (evidence, "confidence", 0.5)]:
        with pytest.raises(ValidationError):
            setattr(model, field, value)
    assert message.sender not in repr(message)
    assert message.subject not in repr(message)
    assert evidence.explanation not in repr(evidence)
