"""Explicit one-message Review boundary; no durable intake or action authority."""

from dataclasses import dataclass
from pathlib import Path

from dam.auth import AuthError
from dam.categories import default_catalog_path
from dam.config import ConfigurationError
from dam.gmail import GmailAdapterError
from dam.learning import LearningError, default_learned_rules_path
from dam.presentation import safe_metadata_text
from dam.review import ReviewScopeError, review_gmail_message
from dam.review_presentation import (
    ReviewDiagnostic, ReviewDiagnosticCode, ReviewDisplay, ReviewPresentationSink,
)
from dam.teaching_queries import QueryControlResult, QueryDisposition


@dataclass(frozen=True, slots=True, repr=False)
class ReviewRequest:
    message_id: str
    gmail: bool = False
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if (type(self.message_id) is not str or type(self.gmail) is not bool or
                any(value is not None and type(value) is not str for value in (
                    self.learned_rules_file, self.category_catalog_file))):
            raise TypeError("Explicit scalar Review inputs required")


def _failure(code: ReviewDiagnosticCode, disposition: QueryDisposition,
             present: ReviewPresentationSink) -> QueryControlResult:
    try:
        present(ReviewDiagnostic(code))
    except Exception:
        # Failed diagnostic delivery neither exposes the exception nor repeats
        # authentication, acquisition, classification, or any other Operation.
        return QueryControlResult(QueryDisposition.FAILED)
    return QueryControlResult(disposition)


def review_message(request: ReviewRequest, *, present: ReviewPresentationSink) -> QueryControlResult:
    """Review exactly the named message once, then release only display fields.

    As with Teaching queries, the disposition includes display delivery; no
    presentation data or retry instruction enters control. Failure does not imply
    rollback of existing OAuth token persistence. Source-client lifetime and all
    classification/proposal behavior remain owned by the unchanged Review service.
    """
    try:
        if type(request) is not ReviewRequest:
            return _failure(ReviewDiagnosticCode.SCOPE_REJECTED, QueryDisposition.REJECTED, present)
        if not request.gmail:
            return _failure(ReviewDiagnosticCode.GMAIL_REQUIRED, QueryDisposition.REJECTED, present)
        # Preserve the service's blank-ID rejection before configuration/auth,
        # without trimming or otherwise rewriting a supplied opaque identifier.
        if not request.message_id.strip():
            raise ReviewScopeError("message_id_required")
        learned = (default_learned_rules_path() if request.learned_rules_file is None
                   else Path(request.learned_rules_file))
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        result = review_gmail_message(request.message_id, learned_rules_path=learned,
                                     **({"category_catalog_path": catalog} if catalog is not None else {}))
        message, classification, proposal = result.message, result.classification, result.proposal
        view = ReviewDisplay(
            message.message_id,
            safe_metadata_text(message.sender, present="sender" in message.model_fields_set),
            safe_metadata_text(message.subject, present="subject" in message.model_fields_set),
            message.received_at.isoformat(),
            tuple(safe_metadata_text(label, present=True) for label in message.label_ids),
            tuple(classification.category_ids), classification.classification_confidence,
            None if classification.classification_sources is None else
                tuple(str(item.basis) for item in classification.classification_sources),
            classification.category_teaching_required, classification.requires_review,
            None if classification.review_reasons is None else tuple(str(code) for code in classification.review_reasons),
            None if proposal.review_reason_codes is None else tuple(str(code) for code in proposal.review_reason_codes),
            proposal.proposed_action.value, str(proposal.approval_type), str(proposal.approval_status))
        # Release broad private results before display delivery. The Writer gets
        # no source client, configuration, evidence, or domain-query capability.
        del result, message, classification, proposal
        present(view)
        return QueryControlResult(QueryDisposition.COMPLETED)
    except ReviewScopeError as error:
        code = (ReviewDiagnosticCode.OUTSIDE_INBOX if str(error) == "message_outside_inbox"
                else ReviewDiagnosticCode.SCOPE_REJECTED)
        return _failure(code, QueryDisposition.REJECTED, present)
    except (LearningError, AuthError, GmailAdapterError, ConfigurationError, OSError, ValueError):
        return _failure(ReviewDiagnosticCode.SAFE_FAILURE, QueryDisposition.REJECTED, present)
    except Exception:
        return _failure(ReviewDiagnosticCode.INTERNAL, QueryDisposition.FAILED, present)
