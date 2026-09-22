"""Explicit one-message Gmail review; metadata only, with no mailbox actions."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dam.actions import ActionProposal, propose_action
from dam.auth import AuthPaths, GoogleAuthBackend, authenticate, build_gmail_service, google_service_factory
from dam.classifier import ClassificationResult, classify
from dam.categories import CategoryError, resolve_category
from dam.config import load_config
from dam.gmail import read_message
from dam.learning import configuration_with_learned_rules
from dam.models import Configuration, MessageMetadata
from dam.presentation import classification_basis, review_reasons, safe_metadata_text as _safe_text
from dam.scan import GMAIL_ACCOUNT_ID, default_config_directory


class ReviewScopeError(ValueError):
    """The requested message cannot be reviewed within the current Inbox scope."""


@dataclass(frozen=True, repr=False)
class GmailReviewResult:
    message: MessageMetadata
    classification: ClassificationResult
    proposal: ActionProposal
    config: Configuration
    as_of: datetime
    auth_source: str
    authority_established: bool = False
    executable: bool = False
    executed_gmail_actions: int = 0

    def __repr__(self) -> str:
        return "GmailReviewResult(metadata=<redacted>, authority_established=False, executable=False)"


def review_gmail_message(
    message_id: str, *, category_id: str | None = None,
    paths: AuthPaths | None = None, backend: Any = None, service_factory: Any = None,
    config_directory: Path | None = None, learned_rules_path: Path | None = None,
    as_of: datetime | None = None, category_catalog_path: Path | None = None,
) -> GmailReviewResult:
    """Authenticate, get only the named message, and evaluate it without writes.

    An explicit save command repeats this one-message metadata read so the
    preview fingerprint is checked against current Inbox membership and
    metadata. No snapshot containing personal metadata is persisted for the
    preview. There is no list, thread expansion, or live freshness claim.
    """
    if not isinstance(message_id, str) or not message_id.strip():
        raise ReviewScopeError("message_id_required")
    current = datetime.now(timezone.utc) if as_of is None else as_of
    if current.tzinfo is None or current.utcoffset() is None:
        raise ReviewScopeError("invalid_time")
    current = current.astimezone(timezone.utc)
    config = load_config(config_directory or default_config_directory(),
                         category_catalog_path=category_catalog_path)
    if learned_rules_path is not None:
        config = configuration_with_learned_rules(config, learned_rules_path)
    if category_id is not None:
        try:
            resolve_category(category_id, config.categories)
        except CategoryError:
            raise ReviewScopeError("unknown_category") from None
    if config.settings.scan.label_ids != ("INBOX",) or config.settings.scan.include_spam_trash:
        raise ReviewScopeError("inbox_only_required")
    session = authenticate(paths if paths is not None else AuthPaths.for_home(),
                           backend if backend is not None else GoogleAuthBackend(),
                           allow_authorization=True)
    service = build_gmail_service(session, service_factory if service_factory is not None
                                  else google_service_factory)
    message = read_message(service, account_id=GMAIL_ACCOUNT_ID, message_id=message_id)
    if "label_ids" not in message.model_fields_set or "INBOX" not in message.label_ids:
        raise ReviewScopeError("message_outside_inbox")
    classification = classify(message, config.rules, as_of=current, settings=config.settings,
                              category_config=config.categories)
    proposal = propose_action(message, classification, config.rules,
                              as_of=current, settings=config.settings)
    return GmailReviewResult(message=message, classification=classification,
                             proposal=proposal, config=config, as_of=current,
                             auth_source=session.summary.source)


def render_review(result: GmailReviewResult) -> str:
    """Display only normalized metadata in this explicit human-review context."""
    message = result.message
    classification = result.classification
    proposal = result.proposal
    labels = ", ".join(_safe_text(label, present=True) for label in message.label_ids) or "<none>"
    lines = [
        "DAM explicit Gmail Inbox review (read-only, one individual message)",
        f"Message ID: {message.message_id}",
        f"From: {_safe_text(message.sender, present='sender' in message.model_fields_set)}",
        f"Subject: {_safe_text(message.subject, present='subject' in message.model_fields_set)}",
        f"Observed: {message.received_at.isoformat()}",
        f"Labels: {labels}",
        f"Current DAM classification: {', '.join(classification.category_ids) or 'unclassified'}",
        f"Classification confidence: {classification.classification_confidence:.2f}",
        "Classification basis: " + classification_basis(None if classification.classification_sources is None else
            (item.basis for item in classification.classification_sources)),
        "Category teaching: " + ("required" if classification.category_teaching_required is True else
            "satisfied" if classification.category_teaching_required is False else "undetermined"),
        f"Review required: {'yes' if classification.requires_review else 'no'}",
        "Classification Review reasons: " + review_reasons(classification.review_reasons),
        "Action Review reasons: " + review_reasons(proposal.review_reason_codes, action=True),
        f"Proposed action: {proposal.proposed_action.value} (non-executable)",
        f"Approval: {proposal.approval_type}/{proposal.approval_status}",
        "Authority established: false; executable: false; executed Gmail actions: 0",
        ("No mailbox change occurred. A human must separately choose a category to teach DAM."
         if classification.category_teaching_required is True else
         "No mailbox change occurred. Resolve the classification conflict before teaching DAM."
         if classification.category_teaching_required is None else
         "No mailbox change occurred. Category teaching is already satisfied; other Review reasons may remain."),
    ]
    return "\n".join(lines) + "\n"
