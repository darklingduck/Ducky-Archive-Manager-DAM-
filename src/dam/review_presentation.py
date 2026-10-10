"""Private Review display releases; no source, configuration, or domain capabilities.

Sinks consume only the requested display and must not log, cache, retain, or replay
payloads. This does not implement temporary expanded evidence or screen clearing.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dam.presentation import classification_basis, review_reasons


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ReviewDisplay:
    message_id: str
    sender_display: str
    subject_display: str
    received_at: str
    labels: tuple[str, ...]
    category_ids: tuple[str, ...]
    classification_confidence: float
    classification_basis_codes: tuple[str, ...] | None
    category_teaching_required: bool | None
    requires_review: bool
    classification_review_codes: tuple[str, ...] | None
    action_review_codes: tuple[str, ...] | None
    proposed_action: str
    approval_type: str
    approval_status: str

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (
                self.message_id, self.sender_display, self.subject_display, self.received_at,
                self.proposed_action, self.approval_type, self.approval_status)):
            raise TypeError("Scalar Review display strings required")
        for value in (self.labels, self.category_ids):
            if type(value) is not tuple or any(type(item) is not str for item in value):
                raise TypeError("Immutable Review display strings required")
        for value in (self.classification_basis_codes, self.classification_review_codes, self.action_review_codes):
            if value is not None and (type(value) is not tuple or any(type(item) is not str for item in value)):
                raise TypeError("Optional immutable Review display strings required")
        if (type(self.classification_confidence) is not float or not 0 <= self.classification_confidence <= 1 or
                type(self.requires_review) is not bool or
                (self.category_teaching_required is not None and type(self.category_teaching_required) is not bool)):
            raise TypeError("Scalar Review decision display values required")


class ReviewDiagnosticCode(StrEnum):
    GMAIL_REQUIRED = "gmail_required"
    OUTSIDE_INBOX = "outside_inbox"
    SCOPE_REJECTED = "scope_rejected"
    SAFE_FAILURE = "safe_failure"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ReviewDiagnostic:
    code: ReviewDiagnosticCode

    def __post_init__(self) -> None:
        if type(self.code) is not ReviewDiagnosticCode:
            raise TypeError("A fixed Review diagnostic code is required")


ReviewPresentation = ReviewDisplay | ReviewDiagnostic


class ReviewPresentationSink(Protocol):
    def __call__(self, view: ReviewPresentation, /) -> None: ...


def render_review_display(view: ReviewPresentation) -> str:
    """Stateless formatting of deliberately released values only."""
    if type(view) is ReviewDiagnostic:
        if view.code is ReviewDiagnosticCode.GMAIL_REQUIRED:
            return "DAM review requires explicit --gmail; no Gmail access attempted.\n"
        if view.code is ReviewDiagnosticCode.OUTSIDE_INBOX:
            return "DAM review: message is outside the current Inbox review scope.\n"
        if view.code is ReviewDiagnosticCode.SCOPE_REJECTED:
            return "DAM review rejected the message scope.\n"
        if view.code is ReviewDiagnosticCode.SAFE_FAILURE:
            return "DAM review failed safely; no mailbox actions executed.\n"
        return "DAM review failed internally; no mailbox actions executed.\n"
    if type(view) is not ReviewDisplay:
        raise TypeError("Unsupported Review presentation contract")
    lines = [
        "DAM explicit Gmail Inbox review (read-only, one individual message)",
        f"Message ID: {view.message_id}",
        f"From: {view.sender_display}",
        f"Subject: {view.subject_display}",
        f"Observed: {view.received_at}",
        f"Labels: {', '.join(view.labels) or '<none>'}",
        f"Current DAM classification: {', '.join(view.category_ids) or 'unclassified'}",
        f"Classification confidence: {view.classification_confidence:.2f}",
        "Classification basis: " + classification_basis(view.classification_basis_codes),
        "Category teaching: " + ("required" if view.category_teaching_required is True else
            "satisfied" if view.category_teaching_required is False else "undetermined"),
        f"Review required: {'yes' if view.requires_review else 'no'}",
        "Classification Review reasons: " + review_reasons(view.classification_review_codes),
        "Action Review reasons: " + review_reasons(view.action_review_codes, action=True),
        f"Proposed action: {view.proposed_action} (non-executable)",
        f"Approval: {view.approval_type}/{view.approval_status}",
        "Authority established: false; executable: false; executed Gmail actions: 0",
        ("No mailbox change occurred. A human must separately choose a category to teach DAM."
         if view.category_teaching_required is True else
         "No mailbox change occurred. Resolve the classification conflict before teaching DAM."
         if view.category_teaching_required is None else
         "No mailbox change occurred. Category teaching is already satisfied; other Review reasons may remain."),
    ]
    return "\n".join(lines) + "\n"
