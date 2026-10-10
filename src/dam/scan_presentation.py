"""Private, requested Scan display releases; no source/repository capabilities.

Sinks must not retain, cache, log, or replay payloads. Only existing terminal fields
are released; this does not support temporary source evidence or action authority.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dam.presentation import classification_basis, review_reasons


def _strings(*values: str) -> None:
    if any(type(value) is not str for value in values):
        raise TypeError("Scalar Scan display strings required")


def _strings_tuple(value: tuple[str, ...] | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TypeError("Immutable Scan display strings required")


def _counts(*values: int) -> None:
    if any(type(value) is not int or value < 0 for value in values):
        raise TypeError("Scalar Scan display counts required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanCountDisplay:
    key: str
    count: int

    def __post_init__(self) -> None:
        _strings(self.key)
        _counts(self.count)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanRuleDisplay:
    rule_id: str
    version: int

    def __post_init__(self) -> None:
        _strings(self.rule_id)
        _counts(self.version)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanMessageDisplay:
    message_id: str
    thread_id: str | None
    received_at: str
    labels: tuple[str, ...]
    sender_present: bool
    subject_present: bool
    categories: tuple[str, ...]
    classification_confidence: float
    classification_band: str
    basis_codes: tuple[str, ...] | None
    rules: tuple[ScanRuleDisplay, ...] | None
    category_permanent_ids: tuple[str, ...] | None
    teaching_required: bool | None
    proposed_action: str
    action_confidence: float
    approval_type: str
    approval_status: str
    requires_review: bool
    protection_signals: tuple[str, ...]
    classification_review_codes: tuple[str, ...] | None
    action_review_codes: tuple[str, ...] | None
    legacy_classification_notes: tuple[str, ...]
    legacy_action_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        _strings(self.message_id, self.received_at, self.classification_band,
                 self.proposed_action, self.approval_type, self.approval_status)
        if self.thread_id is not None and type(self.thread_id) is not str:
            raise TypeError("An optional Scan thread display string is required")
        for value in (self.labels, self.categories, self.protection_signals,
                      self.legacy_classification_notes, self.legacy_action_notes):
            _strings_tuple(value)
        for value in (self.basis_codes, self.category_permanent_ids, self.classification_review_codes, self.action_review_codes):
            _strings_tuple(value, optional=True)
        if self.rules is not None and (type(self.rules) is not tuple or any(type(item) is not ScanRuleDisplay for item in self.rules)):
            raise TypeError("Immutable Scan rule display rows required")
        if any(type(value) is not bool for value in (self.sender_present, self.subject_present, self.requires_review)) or (
                self.teaching_required is not None and type(self.teaching_required) is not bool):
            raise TypeError("Scalar Scan decision display flags required")
        if any(type(value) is not float or not 0 <= value <= 1 for value in (
                self.classification_confidence, self.action_confidence)):
            raise TypeError("Scalar Scan confidence display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanAggregateDisplay:
    observed: int
    classified: int
    unclassified: int
    review: int
    actions: tuple[ScanCountDisplay, ...]
    trash_candidates: int
    approvals_required: int
    destructive_approvals: int
    executable: int

    def __post_init__(self) -> None:
        _counts(self.observed, self.classified, self.unclassified, self.review,
                self.trash_candidates, self.approvals_required, self.destructive_approvals, self.executable)
        if type(self.actions) is not tuple or any(type(item) is not ScanCountDisplay for item in self.actions):
            raise TypeError("Immutable Scan action counts required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanPreviewDisplay:
    run_id: str
    account_id: str
    fingerprint: str
    scope_labels: tuple[str, ...]
    limit: int
    coverage: str
    aggregate: ScanAggregateDisplay
    messages: tuple[ScanMessageDisplay, ...]

    def __post_init__(self) -> None:
        _strings(self.run_id, self.account_id, self.fingerprint, self.coverage)
        _strings_tuple(self.scope_labels)
        _counts(self.limit)
        if (type(self.aggregate) is not ScanAggregateDisplay or type(self.messages) is not tuple or
                any(type(item) is not ScanMessageDisplay for item in self.messages)):
            raise TypeError("Immutable Scan preview display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanReadFailureDisplay:
    message_id: str
    reason: str

    def __post_init__(self) -> None:
        _strings(self.message_id, self.reason)
        if self.reason not in ("api_error", "malformed_response"):
            raise TypeError("A fixed Scan read-failure reason is required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanCoverageDisplay:
    listed: int
    normalized: int
    failures: tuple[ScanReadFailureDisplay, ...]
    coverage: str
    outside_inbox: int | None = None

    def __post_init__(self) -> None:
        _counts(self.listed, self.normalized)
        _strings(self.coverage)
        if self.outside_inbox is not None:
            _counts(self.outside_inbox)
        if type(self.failures) is not tuple or any(type(item) is not ScanReadFailureDisplay for item in self.failures):
            raise TypeError("Immutable Scan read-failure display rows required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanIntakeDisplay:
    run_id: str
    source_id: str
    committed_count: int
    status: str

    def __post_init__(self) -> None:
        _strings(self.run_id, self.source_id, self.status)
        _counts(self.committed_count)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanDisplay:
    source: str
    preview: ScanPreviewDisplay | None
    coverage: ScanCoverageDisplay | None = None
    intake: ScanIntakeDisplay | None = None

    def __post_init__(self) -> None:
        _strings(self.source)
        if ((self.preview is not None and type(self.preview) is not ScanPreviewDisplay) or
                (self.coverage is not None and type(self.coverage) is not ScanCoverageDisplay) or
                (self.intake is not None and type(self.intake) is not ScanIntakeDisplay)):
            raise TypeError("Detached Scan display values required")
        if (self.source not in ("synthetic", "gmail", "durable") or
                (self.source != "durable" and (self.preview is None or self.intake is not None)) or
                (self.source == "durable" and self.intake is None) or
                (self.source == "synthetic" and self.coverage is not None) or
                (self.source == "gmail" and (self.coverage is None or self.coverage.outside_inbox is not None)) or
                (self.source == "durable" and self.coverage is not None and self.coverage.outside_inbox is None)):
            raise TypeError("Consistent Scan display variants required")


class ScanDiagnosticCode(StrEnum):
    RECORD_REQUIRES_GMAIL = "record_requires_gmail"
    GMAIL_LIMIT = "gmail_limit"
    GMAIL_INPUT = "gmail_input"
    GMAIL_SAFE = "gmail_safe"
    GMAIL_INTERNAL = "gmail_internal"
    SYNTHETIC_INVALID = "synthetic_invalid"
    SYNTHETIC_INTERNAL = "synthetic_internal"
    DURABLE_STOP = "durable_stop"
    OUTPUT = "output"


class ScanInputReason(StrEnum):
    LIMIT = "Gmail mode limit must be from 1 to 10"
    TIME = "Scan time must include a timezone"
    RUN_ID = "Run ID must be nonblank"
    SCOPE = "Gmail mode requires Inbox-only scope"
    INVALID = "Invalid Gmail scan input"


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ScanDiagnostic:
    code: ScanDiagnosticCode
    input_reason: ScanInputReason | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not ScanDiagnosticCode:
            raise TypeError("A fixed Scan diagnostic code is required")
        if self.input_reason is not None and type(self.input_reason) is not ScanInputReason:
            raise TypeError("A fixed Scan input reason is required")
        if (self.code is ScanDiagnosticCode.GMAIL_INPUT) != (self.input_reason is not None):
            raise TypeError("Only Gmail input rejection releases a reason")


ScanPresentation = ScanDisplay | ScanDiagnostic


class ScanPresentationSink(Protocol):
    def __call__(self, view: ScanPresentation, /) -> None: ...


def _preview(view: ScanPreviewDisplay) -> str:
    s = view.aggregate
    lines = [f"DAM dry-run preview {view.run_id} ({view.account_id})",
             f"Fingerprint: {view.fingerprint}",
             "Freshness: not checked against live Gmail state; no execution authority",
             f"Scope: {','.join(view.scope_labels)}; limit {view.limit}; coverage {view.coverage}",
             f"Observed: {s.observed}; classified: {s.classified}; unclassified: {s.unclassified}; Review: {s.review}",
             f"Proposals: {', '.join(f'{x.key}={x.count}' for x in s.actions) or 'none'}; Trash candidates: {s.trash_candidates}",
             f"Approvals required: {s.approvals_required}; destructive: {s.destructive_approvals}; executable: {s.executable}",
             "Executed Gmail actions: 0; actual Inbox after: not observed", ""]
    for entry in view.messages:
        bases = classification_basis(entry.basis_codes)
        sources = ("not recorded" if entry.rules is None else
                   ", ".join(f"{source.rule_id}/v{source.version}" for source in entry.rules) or "none")
        cat_ids = ("not recorded" if entry.category_permanent_ids is None else ",".join(entry.category_permanent_ids) or "unavailable")
        teaching = ("not recorded" if entry.rules is None else "required" if entry.teaching_required is True else
                    "satisfied" if entry.teaching_required is False else "undetermined")
        lines.extend([f"Message {entry.message_id} (thread {entry.thread_id or 'unknown'})",
            f"  Observed: {entry.received_at}; labels={','.join(entry.labels) or 'none'}; sender={'present' if entry.sender_present else 'missing'}; subject={'present' if entry.subject_present else 'missing'}",
            f"  Classification: {','.join(entry.categories) or 'withheld'} ({entry.classification_confidence:.2f}, {entry.classification_band})",
            f"  Classification basis: {bases}; rules={sources}; CAT IDs={cat_ids}",
            f"  Category teaching: {teaching}",
            f"  Proposal: {entry.proposed_action} ({entry.action_confidence:.2f}); approval={entry.approval_type}/{entry.approval_status}; authority=false; executable=false",
            f"  Review: {'required' if entry.requires_review else 'no'}; protection={','.join(entry.protection_signals) or 'none'}",
            f"  Classification Review reasons: {review_reasons(entry.classification_review_codes)}",
            f"  Action Review reasons: {review_reasons(entry.action_review_codes, action=True)}"])
        if entry.classification_review_codes is None and entry.legacy_classification_notes:
            lines.append(f"  Legacy classification notes: {'; '.join(entry.legacy_classification_notes)}")
        if entry.action_review_codes is None and entry.legacy_action_notes:
            lines.append(f"  Legacy action Review text: {'; '.join(entry.legacy_action_notes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_scan(view: ScanPresentation) -> str:
    """Stateless Writer; only intentionally released immutable display fields."""
    if type(view) is ScanDiagnostic:
        if view.code is ScanDiagnosticCode.RECORD_REQUIRES_GMAIL:
            return "DAM local recording requires explicit --gmail; no source access attempted.\n"
        if view.code is ScanDiagnosticCode.GMAIL_LIMIT:
            return "DAM Gmail mode limit must be from 1 to 10; no Gmail access attempted.\n"
        if view.code is ScanDiagnosticCode.GMAIL_INPUT:
            return f"DAM Gmail scan rejected: {view.input_reason.value}; no mailbox actions executed.\n"
        if view.code is ScanDiagnosticCode.GMAIL_SAFE:
            return "DAM Gmail read-only scan failed; no mailbox actions executed.\n"
        if view.code is ScanDiagnosticCode.GMAIL_INTERNAL:
            return "DAM Gmail scan failed internally; no mailbox actions executed.\n"
        if view.code is ScanDiagnosticCode.SYNTHETIC_INVALID:
            return "DAM scan failed: invalid configuration or synthetic input.\n"
        if view.code is ScanDiagnosticCode.SYNTHETIC_INTERNAL:
            return "DAM scan failed: internal error; no mailbox actions were executed.\n"
        if view.code is ScanDiagnosticCode.OUTPUT:
            return "DAM scan outcome could not be displayed; inspect any durable intake before retrying.\n"
        return "DAM durable Gmail intake stopped after a local/read failure; earlier committed items remain recorded.\n"
    if type(view) is not ScanDisplay:
        raise TypeError("Unsupported Scan presentation contract")
    output = ""
    if view.source == "gmail":
        output = "DAM real Gmail read-only Inbox scan; no mailbox actions executed.\n"
    elif view.source == "durable":
        output = "DAM real Gmail read-only Inbox scan with explicit private local recording; no mailbox actions executed.\n"
        intake = view.intake
        output += (f"Run: {intake.run_id}; verified source: {intake.source_id}; "
                   f"durably admitted: {intake.committed_count}; status: {intake.status}.\n")
    if view.preview is not None:
        output += _preview(view.preview)
    if view.coverage is not None:
        read = view.coverage
        outside = f"outside current Inbox: {read.outside_inbox}; " if read.outside_inbox is not None else ""
        output += (f"Gmail listing: {read.listed} IDs; normalized: {read.normalized}; "
                   f"read failures: {len(read.failures)}; {outside}coverage: {read.coverage}.\n")
        for failure in read.failures:
            output += f"Uninspected message {failure.message_id}: {failure.reason}; Review required.\n"
    return output
