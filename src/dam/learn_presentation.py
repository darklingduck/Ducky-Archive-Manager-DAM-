"""Private, requested Learn display releases; never persistence/action authority.

Sinks must not log, cache, retain, or replay payloads. No temporary expanded source
content is supported. Structured confirmation values work independently of shell
formatting and must be revalidated by a separate, explicitly requested save.
"""

from dataclasses import dataclass
from enum import StrEnum
import shlex
from typing import Protocol


def _strings(*values: str) -> None:
    if any(type(value) is not str for value in values):
        raise TypeError("Scalar Learn display strings required")


def _optional_strings(*values: str | None) -> None:
    if any(value is not None and type(value) is not str for value in values):
        raise TypeError("Optional Learn display strings required")


def _string_tuple(value: tuple[str, ...]) -> None:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TypeError("Immutable Learn display strings required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class LearnEvidenceDisplay:
    field: str
    value: str | None
    selected: bool
    reason: str

    def __post_init__(self) -> None:
        _strings(self.field, self.reason)
        _optional_strings(self.value)
        if type(self.selected) is not bool:
            raise TypeError("Scalar Learn evidence selection required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class LearnImpactDisplay:
    message_id: str
    matched: bool
    before_categories: tuple[str, ...]
    after_categories: tuple[str, ...]
    confidence: float
    requires_review: bool

    def __post_init__(self) -> None:
        _strings(self.message_id)
        _string_tuple(self.before_categories)
        _string_tuple(self.after_categories)
        if (type(self.matched) is not bool or type(self.requires_review) is not bool or
                type(self.confidence) is not float or not 0 <= self.confidence <= 1):
            raise TypeError("Scalar Learn impact display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class LearnConfirmation:
    message_id: str
    category_selector: str
    gmail: bool
    fingerprint: str
    learned_rules_file: str | None
    category_catalog_file: str | None

    def __post_init__(self) -> None:
        _strings(self.message_id, self.category_selector, self.fingerprint)
        _optional_strings(self.learned_rules_file, self.category_catalog_file)
        if type(self.gmail) is not bool:
            raise TypeError("Explicit Learn confirmation source required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class LearnDisplay:
    source_message_id: str
    selected_category: str
    rule_id: str
    rule_version: int
    exact_sender: str
    equivalent_rule_id: str | None
    conflicts: tuple[str, ...]
    fingerprint: str
    status: str
    evidence: tuple[LearnEvidenceDisplay, ...]
    impact: tuple[LearnImpactDisplay, ...]
    confirmation: LearnConfirmation | None

    def __post_init__(self) -> None:
        _strings(self.source_message_id, self.selected_category, self.rule_id,
                 self.exact_sender, self.fingerprint, self.status)
        _optional_strings(self.equivalent_rule_id)
        _string_tuple(self.conflicts)
        if (type(self.rule_version) is not int or type(self.evidence) is not tuple or
                any(type(item) is not LearnEvidenceDisplay for item in self.evidence) or
                type(self.impact) is not tuple or any(type(item) is not LearnImpactDisplay for item in self.impact) or
                (self.confirmation is not None and type(self.confirmation) is not LearnConfirmation)):
            raise TypeError("Immutable Learn display values required")
        if self.status not in ("proposed", "saved", "equivalent_exists") or (
                (self.status == "proposed") != (self.confirmation is not None)):
            raise TypeError("Preview-only Learn confirmation required")


class LearnDiagnosticCode(StrEnum):
    CONFIRMATION = "confirmation"
    OUTSIDE_INBOX = "outside_inbox"
    UNKNOWN_CATEGORY = "unknown_category"
    SCOPE = "scope"
    INPUT = "input"
    CATALOG = "catalog"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class LearnDiagnostic:
    code: LearnDiagnosticCode
    category_selector: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not LearnDiagnosticCode:
            raise TypeError("A fixed Learn diagnostic code is required")
        _optional_strings(self.category_selector)
        if (self.code is LearnDiagnosticCode.UNKNOWN_CATEGORY) != (self.category_selector is not None):
            raise TypeError("Only category guidance releases the requested selector")


LearnPresentation = LearnDisplay | LearnDiagnostic


class LearnPresentationSink(Protocol):
    def __call__(self, view: LearnPresentation, /) -> None: ...


def _save_command(view: LearnConfirmation) -> str:
    parts = ["dam", "learn"]
    if view.gmail:
        parts.append("--gmail")
    parts.extend(("--message-id", view.message_id, "--category", view.category_selector))
    if view.learned_rules_file:
        parts.extend(("--learned-rules-file", view.learned_rules_file))
    if view.category_catalog_file:
        parts.extend(("--category-catalog-file", view.category_catalog_file))
    parts.extend(("--save", "--confirm-fingerprint", view.fingerprint))
    return shlex.join(parts)


def render_learn(view: LearnPresentation) -> str:
    """Stateless formatting; no domain queries, fingerprint computation, or save."""
    if type(view) is LearnDiagnostic:
        if view.code is LearnDiagnosticCode.CONFIRMATION:
            return "DAM learn: --save requires --confirm-fingerprint, and confirmation requires --save.\n"
        if view.code is LearnDiagnosticCode.OUTSIDE_INBOX:
            return "DAM learn: message is outside the current Inbox review scope; no rule saved.\n"
        if view.code is LearnDiagnosticCode.UNKNOWN_CATEGORY:
            return (f"Unknown DAM category {view.category_selector!r}. Run 'dam categories' to list valid categories. "
                    "No rule saved.\n")
        if view.code is LearnDiagnosticCode.SCOPE:
            return "DAM learn rejected the category or message scope; no rule saved.\n"
        if view.code is LearnDiagnosticCode.INPUT:
            return "DAM learn rejected the classification input or private rule file; no mailbox actions executed.\n"
        if view.code is LearnDiagnosticCode.CATALOG:
            return "DAM learn rejected the private category catalog; no rule saved.\n"
        return "DAM learn failed internally; no mailbox actions executed.\n"
    if type(view) is not LearnDisplay:
        raise TypeError("Unsupported Learn presentation contract")
    lines = [
        f"Source message: {view.source_message_id}",
        f"Human selected category: {view.selected_category}",
        f"Candidate rule: {view.rule_id} (version {view.rule_version})",
        f"Exact sender match: {view.exact_sender}",
        "Scope: exact sender only; sender domain is broader and was not selected.",
        "Evidence grade: sender-only 0.90; sender identity and message meaning remain unverified.",
        f"Equivalent rule: {view.equivalent_rule_id or 'none'}",
        f"Conflicts: {', '.join(view.conflicts) if view.conflicts else 'none'}",
        f"Candidate fingerprint: {view.fingerprint}",
        f"Status: {view.status}",
        "Mailbox actions executed: 0; authority=false; executable=false.",
    ]
    for item in view.evidence:
        detail = f"; observed={item.value}" if item.value is not None else ""
        lines.append(f"Evidence {item.field}: {'selected' if item.selected else 'not selected'}{detail}; {item.reason}")
    for item in view.impact:
        lines.append(f"Impact {item.message_id}: match={str(item.matched).lower()}; "
                     f"before={','.join(item.before_categories) or 'unclassified'}; "
                     f"after={','.join(item.after_categories) or 'unclassified'}; "
                     f"confidence={item.confidence:.2f}; Review={str(item.requires_review).lower()}.")
    output = "\n".join(lines) + "\n"
    if view.confirmation is not None:
        output += ("\nConfirmation required to save:\n"
                   f"Fingerprint: {view.confirmation.fingerprint}\nSave command:\n" +
                   _save_command(view.confirmation) + "\n")
    return output
