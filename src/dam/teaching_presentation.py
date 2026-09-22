"""Explicit display releases for teaching queries; no domain/query capabilities.

All views contain private persisted metadata (including local identifiers). They
are for the requested display only: sinks must not log, cache, or retain them as
history. Temporary source evidence is not supported by this contract. A future
such contract needs its own enforced interaction lifetime before use.
"""

from dataclasses import dataclass
from typing import Protocol
import shlex


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class WorkListingRow:
    work_id: str
    state: str
    item_count: int

    def __post_init__(self) -> None:
        if (type(self.work_id) is not str or type(self.state) is not str or
                type(self.item_count) is not int):
            raise TypeError("Scalar work display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class IncompleteTeachingRow:
    teaching_id: str
    status: str

    def __post_init__(self) -> None:
        if type(self.teaching_id) is not str or type(self.status) is not str:
            raise TypeError("Scalar teaching display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class TeachingQueueListing:
    work: tuple[WorkListingRow, ...]
    incomplete: tuple[IncompleteTeachingRow, ...]

    def __post_init__(self) -> None:
        if (type(self.work) is not tuple or type(self.incomplete) is not tuple or
                any(type(row) is not WorkListingRow for row in self.work) or
                any(type(row) is not IncompleteTeachingRow for row in self.incomplete)):
            raise TypeError("Immutable teaching listing rows required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class TeachingWorkDetail:
    work_id: str
    state: str
    item_id: str
    observation_run_id: str
    observed_at: str
    sender_display: str
    subject_display: str
    active_categories: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (
                self.work_id, self.state, self.item_id, self.observation_run_id,
                self.observed_at, self.sender_display, self.subject_display)):
            raise TypeError("Scalar detail display values required")
        if type(self.active_categories) is not tuple or any(
                type(key) is not str for key in self.active_categories):
            raise TypeError("Immutable category display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class TeachingStatus:
    teaching_id: str
    status: str
    reevaluated: int
    resolved: int
    unresolved: int
    configuration: str | None

    def __post_init__(self) -> None:
        if (type(self.teaching_id) is not str or type(self.status) is not str or
                any(type(value) is not int for value in (self.reevaluated, self.resolved, self.unresolved)) or
                (self.configuration is not None and type(self.configuration) is not str)):
            raise TypeError("Scalar status display values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class TeachingPreviewDisplay:
    """Released preview and confirmation inputs, never confirmation authority.

    Original selectors/explicit file overrides preserve the existing save command.
    GUI callers can use these values without consuming shell syntax. A subsequent
    save must independently validate them and the fingerprint through Teaching.
    """

    work_id: str
    item_id: str
    observation_run_id: str
    observed_at: str
    category_name: str
    category_permanent_id: str
    sender_display: str
    fingerprint: str
    category_selector: str
    requested_item_id: str | None
    learned_rules_file: str | None
    category_catalog_file: str | None

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (
                self.work_id, self.item_id, self.observation_run_id, self.observed_at,
                self.category_name, self.category_permanent_id, self.sender_display,
                self.fingerprint, self.category_selector)) or any(
                value is not None and type(value) is not str for value in (
                    self.requested_item_id, self.learned_rules_file, self.category_catalog_file)):
            raise TypeError("Scalar preview display values required")


TeachingPresentation = TeachingQueueListing | TeachingWorkDetail | TeachingStatus | TeachingPreviewDisplay


class TeachingPresentationSink(Protocol):
    """Consume one explicitly released view without retaining payload/history."""

    def __call__(self, view: TeachingPresentation, /) -> None: ...


def render_teaching(view: TeachingPresentation) -> str:
    """Stateless terminal formatting; unsupported objects are never inspected."""
    if type(view) is TeachingQueueListing:
        lines = [f"{row.work_id}  {row.state}  {row.item_count} item(s)" for row in view.work]
        lines.extend(f"Teaching {row.teaching_id}  {row.status}  "
                     f"resume: dam teach resume {row.teaching_id}" for row in view.incomplete)
        return "".join(line + "\n" for line in lines)
    if type(view) is TeachingWorkDetail:
        return (f"Work: {view.work_id} ({view.state}); ITEM: {view.item_id}\n"
                f"Stored observation: {view.observation_run_id} at {view.observed_at}\n"
                f"From: {view.sender_display}\nSubject: {view.subject_display}\n"
                f"Active categories: {', '.join(view.active_categories)}\n"
                "Stored evidence is historical; no Gmail freshness or action authority is claimed.\n")
    if type(view) is TeachingStatus:
        return (f"Teaching: {view.teaching_id}; status: {view.status}; "
                f"reevaluated: {view.reevaluated}; resolved: {view.resolved}; "
                f"still unresolved: {view.unresolved}; configuration: {view.configuration or '<pending>'}\n")
    if type(view) is TeachingPreviewDisplay:
        parts = ["dam", "teach"]
        if view.learned_rules_file:
            parts.extend(("--learned-rules-file", view.learned_rules_file))
        if view.category_catalog_file:
            parts.extend(("--category-catalog-file", view.category_catalog_file))
        parts.extend(("save", view.work_id, "--category", view.category_selector))
        if view.requested_item_id:
            parts.extend(("--item-id", view.requested_item_id))
        parts.extend(("--confirm-fingerprint", view.fingerprint))
        return (f"Teaching preview only; work: {view.work_id}; ITEM: {view.item_id}\n"
                f"Stored observation: {view.observation_run_id} at {view.observed_at}\n"
                f"Category: {view.category_name} ({view.category_permanent_id})\n"
                f"Exact sender: {view.sender_display}\n"
                f"Fingerprint: {view.fingerprint}\n"
                "Confirmation required; save command:\n" + shlex.join(parts) + "\n"
                "No Gmail read or mailbox action occurred.\n")
    raise TypeError("Unsupported teaching presentation contract")
