"""One bounded Scan Operation family; existing services own scan/intake behavior."""

from dataclasses import dataclass
from pathlib import Path

from dam.audit import ScanPreview
from dam.auth import AuthError
from dam.config import ConfigurationError
from dam.durable_gmail import run_durable_gmail_scan
from dam.gmail import GmailAdapterError, GmailReadResult
from dam.learning import LearningError, default_learned_rules_path
from dam.categories import default_catalog_path
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, MAX_SCAN_LIMIT, ScanInputError, run_gmail_scan, run_synthetic_scan
from dam.scan_presentation import (
    ScanAggregateDisplay, ScanCountDisplay, ScanCoverageDisplay, ScanDiagnostic, ScanDiagnosticCode,
    ScanDisplay, ScanInputReason, ScanIntakeDisplay, ScanMessageDisplay, ScanPresentationSink,
    ScanPreviewDisplay, ScanReadFailureDisplay, ScanRuleDisplay,
)
from dam.source_binding import SourceBindingError
from dam.storage import StorageError
from dam.teaching_queries import QueryControlResult, QueryDisposition


@dataclass(frozen=True, slots=True, repr=False)
class ScanRequest:
    gmail: bool = False
    limit: int | None = None
    record_locally: bool = False
    dry_run: bool = False
    use_learned_rules: bool = False
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if (any(type(value) is not bool for value in (
                self.gmail, self.record_locally, self.dry_run, self.use_learned_rules)) or
                (self.limit is not None and type(self.limit) is not int) or
                (self.category_catalog_file is not None and type(self.category_catalog_file) is not str)):
            raise TypeError("Explicit scalar Scan inputs required")


@dataclass(frozen=True, slots=True)
class ScanControlResult(QueryControlResult):
    """Disposition and failed display delivery only; no private data or retries.

    Ordinary partial coverage is completed execution. A returned durable stop is
    incomplete execution, not a rollback or a continuation instruction. Exceptions
    do not establish whether earlier local writes occurred.
    """

    presentation_failed: bool = False

    def __post_init__(self) -> None:
        QueryControlResult.__post_init__(self)
        if type(self.presentation_failed) is not bool:
            raise TypeError("A Scan presentation failure flag is required")


def _project_preview(preview: ScanPreview) -> ScanPreviewDisplay:
    s = preview.statistics
    aggregate = ScanAggregateDisplay(s.total_messages, s.classified, s.unclassified, s.requiring_review,
        tuple(ScanCountDisplay(item.key, item.count) for item in s.by_proposed_action),
        s.trash_candidates, s.requiring_approval, s.requiring_destructive_approval, s.executable)
    entries = tuple(ScanMessageDisplay(entry.message_id, entry.thread_id, entry.received_at.isoformat(),
        entry.label_ids, entry.sender_present, entry.subject_present, entry.category_ids,
        entry.classification_confidence, entry.classification_band,
        None if entry.classification_sources is None else tuple(str(source.basis) for source in entry.classification_sources),
        None if entry.classification_sources is None else tuple(ScanRuleDisplay(source.rule_id, source.rule_version)
                                                              for source in entry.classification_sources),
        entry.category_permanent_ids, entry.category_teaching_required, entry.proposed_action.value,
        entry.action_confidence, entry.approval_type, entry.approval_status, entry.requires_review,
        entry.protection_signals, entry.classification_review_reasons, entry.action_review_reason_codes,
        entry.legacy_classification_notes if entry.classification_review_reasons is None else (),
        entry.review_reasons if entry.action_review_reason_codes is None else ()) for entry in preview.entries)
    return ScanPreviewDisplay(preview.run_id, preview.account_id, preview.fingerprint, preview.scope_label_ids,
        preview.scan_limit, s.inventory.completeness if s.inventory else "unknown", aggregate, entries)


def _project_coverage(read: GmailReadResult, *, outside: int | None = None,
                      force_partial: bool = False) -> ScanCoverageDisplay:
    return ScanCoverageDisplay(read.listed_count, read.observed_count,
        tuple(ScanReadFailureDisplay(item.message_id, item.reason) for item in read.failures),
        "partial" if force_partial else read.coverage, outside)


def _diagnostic(view: ScanDiagnostic, disposition: QueryDisposition,
                present: ScanPresentationSink) -> ScanControlResult:
    try:
        present(view)
    except Exception:
        return ScanControlResult(disposition, presentation_failed=True)
    return ScanControlResult(disposition)


def scan(request: ScanRequest, *, present: ScanPresentationSink) -> ScanControlResult:
    """Select one existing implementation, execute once, then detach output.

    No workflow continuation, intake retry, shared configuration cache, or source
    client enters this boundary. Dry-run's existing non-executing meaning applies
    with or without the flag; explicit local recording still retains metadata.
    """
    if type(request) is not ScanRequest:
        return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.SYNTHETIC_INVALID), QueryDisposition.REJECTED, present)
    if request.record_locally and not request.gmail:
        return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.RECORD_REQUIRES_GMAIL), QueryDisposition.REJECTED, present)
    if request.gmail and request.limit is not None and not 1 <= request.limit <= MAX_INITIAL_GMAIL_LIMIT:
        return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.GMAIL_LIMIT), QueryDisposition.REJECTED, present)
    if not request.gmail and request.limit is not None and not 1 <= request.limit <= MAX_SCAN_LIMIT:
        return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.SYNTHETIC_INVALID), QueryDisposition.REJECTED, present)
    try:
        learned = default_learned_rules_path() if request.use_learned_rules else None
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        options = {"category_catalog_path": catalog} if catalog is not None else {}
        if request.record_locally:
            result = run_durable_gmail_scan(limit=request.limit, learned_rules_path=learned, **options)
        else:
            if learned is not None:
                options["learned_rules_path"] = learned
            result = (run_gmail_scan(limit=request.limit, **options) if request.gmail
                      else run_synthetic_scan(limit=request.limit, **options))
        stopped = bool(result.failure) if request.record_locally else False
    except ScanInputError as error:
        if request.gmail:
            try:
                reason = ScanInputReason(str(error))
            except ValueError:
                reason = ScanInputReason.INVALID
            return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.GMAIL_INPUT, reason), QueryDisposition.REJECTED, present)
        return _diagnostic(ScanDiagnostic(ScanDiagnosticCode.SYNTHETIC_INVALID), QueryDisposition.REJECTED, present)
    except (AuthError, GmailAdapterError, ConfigurationError, LearningError, SourceBindingError, StorageError, ValueError, OSError):
        code = ScanDiagnosticCode.GMAIL_SAFE if request.gmail else ScanDiagnosticCode.SYNTHETIC_INVALID
        return _diagnostic(ScanDiagnostic(code), QueryDisposition.REJECTED, present)
    except Exception:
        code = ScanDiagnosticCode.GMAIL_INTERNAL if request.gmail else ScanDiagnosticCode.SYNTHETIC_INTERNAL
        return _diagnostic(ScanDiagnostic(code), QueryDisposition.FAILED, present)

    disposition = QueryDisposition.INCOMPLETE if stopped else QueryDisposition.COMPLETED
    try:
        if request.record_locally:
            view = ScanDisplay("durable", _project_preview(result.preview) if result.preview is not None else None,
                _project_coverage(result.read_result, outside=len(result.out_of_scope_ids),
                    force_partial=result.status != "completed" or bool(result.out_of_scope_ids))
                    if result.read_result is not None else None,
                ScanIntakeDisplay(result.run_id, result.source_instance_id, len(result.committed_item_ids), result.status))
        elif request.gmail:
            view = ScanDisplay("gmail", _project_preview(result.preview), _project_coverage(result.read_result))
        else:
            view = ScanDisplay("synthetic", _project_preview(result.preview))
        del result
        present(view)
        if stopped:
            present(ScanDiagnostic(ScanDiagnosticCode.DURABLE_STOP))
    except Exception:
        # Never turn a known completion/stop into a failed intake or retry it.
        try:
            present(ScanDiagnostic(ScanDiagnosticCode.OUTPUT))
        except Exception:
            pass
        return ScanControlResult(disposition, presentation_failed=True)
    return ScanControlResult(disposition)
