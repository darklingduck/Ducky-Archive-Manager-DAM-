"""Bounded Learn preview/save; learning primitives retain confirmation/persistence."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dam.auth import AuthError
from dam.categories import CategoryError, default_catalog_path, resolve_category
from dam.config import ConfigurationError, load_config
from dam.gmail import GmailAdapterError
from dam.learning import (
    LearningError, configuration_with_learned_rules, default_learned_rules_path,
    propose_classification_rule, save_classification_rule,
)
from dam.learn_presentation import (
    LearnConfirmation, LearnDiagnostic, LearnDiagnosticCode, LearnDisplay,
    LearnEvidenceDisplay, LearnImpactDisplay, LearnPresentationSink,
)
from dam.review import ReviewScopeError, review_gmail_message
from dam.scan import ScanInputError, default_config_directory, load_synthetic_messages
from dam.teaching_queries import QueryControlResult, QueryDisposition


@dataclass(frozen=True, slots=True, repr=False)
class LearnRequest:
    message_id: str
    category_selector: str
    gmail: bool = False
    save: bool = False
    confirm_fingerprint: str | None = None
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if (type(self.message_id) is not str or type(self.category_selector) is not str or
                type(self.gmail) is not bool or type(self.save) is not bool or
                any(value is not None and type(value) is not str for value in (
                    self.confirm_fingerprint, self.learned_rules_file, self.category_catalog_file))):
            raise TypeError("Explicit scalar Learn inputs required")


@dataclass(frozen=True, slots=True)
class LearnControlResult(QueryControlResult):
    """Execution and separate display failure only; neither instructs a retry.

    A fixed failure disposition instead of a boolean preserves legacy expected
    versus unexpected Writer exit codes while keeping known save completion.
    Exceptions during save never imply rollback or absence of earlier writes.
    """

    presentation_failure: QueryDisposition | None = None

    def __post_init__(self) -> None:
        QueryControlResult.__post_init__(self)
        if self.presentation_failure is not None and (
                type(self.presentation_failure) is not QueryDisposition or
                self.presentation_failure not in (QueryDisposition.REJECTED, QueryDisposition.FAILED)):
            raise TypeError("A fixed Learn presentation failure disposition is required")


def _diagnostic(error: Exception, selector: str) -> tuple[LearnDiagnostic, QueryDisposition]:
    """Whitelist existing useful failure codes; never release exception contents."""
    if isinstance(error, ReviewScopeError):
        code = (LearnDiagnosticCode.OUTSIDE_INBOX if str(error) == "message_outside_inbox" else
                LearnDiagnosticCode.UNKNOWN_CATEGORY if str(error) == "unknown_category" else LearnDiagnosticCode.SCOPE)
    elif isinstance(error, LearningError):
        code = LearnDiagnosticCode.UNKNOWN_CATEGORY if str(error) == "unknown_category" else LearnDiagnosticCode.INPUT
    elif isinstance(error, CategoryError):
        code = (LearnDiagnosticCode.UNKNOWN_CATEGORY if str(error) in ("unknown_or_ambiguous_category", "retired_category")
                else LearnDiagnosticCode.CATALOG)
    elif isinstance(error, (AuthError, GmailAdapterError, ConfigurationError, ScanInputError, OSError, ValueError)):
        code = LearnDiagnosticCode.INPUT
    else:
        code = LearnDiagnosticCode.INTERNAL
    return (LearnDiagnostic(code, selector if code is LearnDiagnosticCode.UNKNOWN_CATEGORY else None),
            QueryDisposition.FAILED if code is LearnDiagnosticCode.INTERNAL else QueryDisposition.REJECTED)


def _deliver_diagnostic(view: LearnDiagnostic, disposition: QueryDisposition, present: LearnPresentationSink,
                        *, presentation_failure: QueryDisposition | None = None) -> LearnControlResult:
    try:
        present(view)
    except Exception:
        return LearnControlResult(disposition, presentation_failure=QueryDisposition.FAILED)
    return LearnControlResult(disposition, presentation_failure=presentation_failure)


def learn(request: LearnRequest, *, present: LearnPresentationSink) -> LearnControlResult:
    """Run one explicit preview or confirmed save, then detach display values.

    Gmail acquisition/evaluation temporarily reuses the existing Review service,
    not the Review Operation or its presentation. Save always reacquires evidence
    and reconstructs/validates the candidate; preview never calls persistence.
    Locks, atomic writes, client lifetime, and fingerprints stay in existing code.
    """
    try:
        if type(request) is not LearnRequest:
            return _deliver_diagnostic(LearnDiagnostic(LearnDiagnosticCode.INPUT), QueryDisposition.REJECTED, present)
        if request.save != (request.confirm_fingerprint is not None):
            return _deliver_diagnostic(LearnDiagnostic(LearnDiagnosticCode.CONFIRMATION), QueryDisposition.REJECTED, present)
        path = default_learned_rules_path() if request.learned_rules_file is None else Path(request.learned_rules_file)
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        if request.gmail:
            reviewed = review_gmail_message(request.message_id, category_id=request.category_selector,
                learned_rules_path=path, **({"category_catalog_path": catalog} if catalog is not None else {}))
            selected = resolve_category(request.category_selector, reviewed.config.categories).id
            candidate = propose_classification_rule(reviewed.message, selected, reviewed.config, as_of=reviewed.as_of)
            config = reviewed.config
            del reviewed
        else:
            messages = load_synthetic_messages()
            source = next((item for item in messages if item.message_id == request.message_id), None)
            if source is None:
                raise LearningError("unknown_synthetic_message")
            config = configuration_with_learned_rules(load_config(default_config_directory(), category_catalog_path=catalog), path)
            selected = resolve_category(request.category_selector, config.categories).id
            candidate = propose_classification_rule(source, selected, config, as_of=datetime.now(timezone.utc),
                sample=tuple(item for item in messages if item.message_id != source.message_id))
            del source, messages
        status = (save_classification_rule(candidate, config, path,
                  expected_fingerprint=request.confirm_fingerprint).status if request.save else "proposed")
    except Exception as error:
        view, disposition = _diagnostic(error, request.category_selector)
        return _deliver_diagnostic(view, disposition, present)

    # Returned persistence establishes completion. Display cannot undo it, repeat
    # it, or infer another Operation. Only deliberately displayed values escape.
    try:
        confirmation = None if request.save else LearnConfirmation(request.message_id, request.category_selector,
            request.gmail, candidate.fingerprint, request.learned_rules_file, request.category_catalog_file)
        view = LearnDisplay(candidate.source_message_id, candidate.human_selected_category,
            candidate.rule.id, candidate.rule.version, candidate.rule.match.sender_emails_any[0],
            candidate.equivalent_rule_id, candidate.conflicts, candidate.fingerprint, status,
            tuple(LearnEvidenceDisplay(item.field, item.value, item.selected, item.reason) for item in candidate.evidence),
            tuple(LearnImpactDisplay(item.message_id, item.matched_candidate, item.before_category_ids,
                item.after_category_ids, item.after_confidence, item.after_requires_review) for item in candidate.impact),
            confirmation)
        del candidate, config
        present(view)
    except Exception as error:
        diagnostic, failure = _diagnostic(error, request.category_selector)
        return _deliver_diagnostic(diagnostic, QueryDisposition.COMPLETED, present, presentation_failure=failure)
    return LearnControlResult(QueryDisposition.COMPLETED)
