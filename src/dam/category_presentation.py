"""Private, requested-display-only category releases and stateless formatting.

Sinks must not retain, log, cache, or replay payloads. No temporary source evidence
is supported. These contracts carry display values, never repositories or authority.
"""

from dataclasses import dataclass
from enum import StrEnum
import shlex
from typing import Protocol


def _strings(*values: str) -> None:
    if any(type(value) is not str for value in values):
        raise TypeError("Scalar category display strings required")


def _optional_strings(*values: str | None) -> None:
    if any(value is not None and type(value) is not str for value in values):
        raise TypeError("Optional category display strings required")


def _string_tuple(value: tuple[str, ...]) -> None:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TypeError("Immutable category display strings required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryListingRow:
    name: str
    key: str
    depth: int
    retired: bool

    def __post_init__(self) -> None:
        _strings(self.name, self.key)
        if type(self.depth) is not int or self.depth < 0 or type(self.retired) is not bool:
            raise TypeError("Scalar category hierarchy values required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryListing:
    rows: tuple[CategoryListingRow, ...]

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or any(type(row) is not CategoryListingRow for row in self.rows):
            raise TypeError("Immutable category listing rows required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryDetail:
    permanent_id: str
    key: str
    name: str
    parent: str | None
    status: str

    def __post_init__(self) -> None:
        _strings(self.permanent_id, self.key, self.name, self.status)
        _optional_strings(self.parent)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryConfirmation:
    """Semantic save inputs for terminal or GUI; fingerprint grants no authority."""

    operation: str
    permanent_id: str
    fingerprint: str
    selector: str | None
    key: str | None
    name: str | None
    parent: str | None
    catalog_file: str | None
    learned_rules_file: str | None

    def __post_init__(self) -> None:
        _strings(self.operation, self.permanent_id, self.fingerprint)
        _optional_strings(self.selector, self.key, self.name, self.parent,
                          self.catalog_file, self.learned_rules_file)
        if self.operation not in ("add", "rename", "move", "retire"):
            raise TypeError("A category change operation is required")
        if ((self.operation == "add" and (self.key is None or self.name is None)) or
                (self.operation != "add" and self.selector is None) or
                (self.operation == "rename" and self.name is None)):
            raise TypeError("Complete category confirmation inputs required")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryChangeDisplay:
    operation: str
    saved: bool
    before: CategoryDetail | None
    after: CategoryDetail
    referenced_rules: tuple[str, ...]
    fingerprint: str
    confirmation: CategoryConfirmation | None

    def __post_init__(self) -> None:
        _strings(self.operation, self.fingerprint)
        _string_tuple(self.referenced_rules)
        if (type(self.saved) is not bool or
                (self.before is not None and type(self.before) is not CategoryDetail) or
                type(self.after) is not CategoryDetail or
                (self.confirmation is not None and type(self.confirmation) is not CategoryConfirmation)):
            raise TypeError("Immutable category change display values required")
        if self.saved != (self.confirmation is None):
            raise TypeError("Preview-only category confirmation required")


class CategoryDiagnosticCode(StrEnum):
    CHILDREN = "active_children"
    REFERENCES = "active_rule_references"
    INVALID_CHANGE = "invalid_change"
    INVALID_CONFIG = "invalid_config"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class CategoryDiagnostic:
    code: CategoryDiagnosticCode
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.code) is not CategoryDiagnosticCode:
            raise TypeError("A fixed category diagnostic code is required")
        _string_tuple(self.references)
        if self.references and self.code not in (CategoryDiagnosticCode.CHILDREN, CategoryDiagnosticCode.REFERENCES):
            raise TypeError("References require a category protection diagnostic")


CategoryPresentation = CategoryListing | CategoryDetail | CategoryChangeDisplay | CategoryDiagnostic


class CategoryPresentationSink(Protocol):
    def __call__(self, view: CategoryPresentation, /) -> None: ...


def _save_command(view: CategoryConfirmation) -> str:
    parts = ["dam", "categories"]
    if view.catalog_file:
        parts.extend(("--catalog-file", view.catalog_file))
    if view.learned_rules_file:
        parts.extend(("--learned-rules-file", view.learned_rules_file))
    parts.append(view.operation)
    if view.operation == "add":
        parts.extend(("--key", view.key, "--name", view.name))
    else:
        parts.append(view.selector)
        if view.operation == "rename":
            parts.extend(("--name", view.name))
    if view.operation in ("add", "move") and view.parent is not None:
        parts.extend(("--parent", view.parent))
    parts.append("--save")
    if view.operation == "add":
        parts.extend(("--confirm-id", view.permanent_id))
    elif view.operation == "retire":
        parts.extend(("--confirm-retire", view.permanent_id))
    parts.extend(("--confirm-fingerprint", view.fingerprint))
    return shlex.join(parts)


def render_categories(view: CategoryPresentation) -> str:
    """Render only detached display values; never inspect unsupported objects."""
    if type(view) is CategoryListing:
        rows = [("  " * row.depth + ("|-- " if row.depth else "") + row.name,
                 row.key, " (retired; unavailable for learning)" if row.retired else "")
                for row in view.rows]
        width = max((len(label) for label, _, _ in rows), default=0)
        return ("DAM categories (use a key with dam learn; show <key> displays its permanent ID):\n" +
                "\n".join(f"{label:<{width}}  {key}{suffix}" for label, key, suffix in rows) + "\n")
    if type(view) is CategoryDetail:
        return (f"Permanent ID: {view.permanent_id}\nKey: {view.key}\nName: {view.name}\n"
                f"Parent: {view.parent or '<root>'}\nStatus: {view.status}\n")
    if type(view) is CategoryChangeDisplay:
        def show(item: CategoryDetail | None) -> str:
            if item is None:
                return "<absent>"
            return (f"{item.permanent_id}  {item.key}  {item.name}  "
                    f"parent={item.parent or '<root>'}  status={item.status}")
        output = (f"Category {view.operation}: {'saved' if view.saved else 'preview only'}\n"
                  f"Before: {show(view.before)}\nAfter: {show(view.after)}\n"
                  f"Active rule references: {', '.join(view.referenced_rules) or 'none'}\n"
                  f"Preview fingerprint: {view.fingerprint}\nMailbox actions executed: 0\n")
        if view.confirmation is not None:
            confirmation = view.confirmation
            output += ("\nConfirmation required to save:\n"
                       f"Category ID: {confirmation.permanent_id}\nFingerprint: {confirmation.fingerprint}\n"
                       "Save command:\n" + _save_command(confirmation) + "\n")
        return output
    if type(view) is CategoryDiagnostic:
        if view.code is CategoryDiagnosticCode.CHILDREN:
            return ("DAM categories: move active children before retirement: " +
                    ",".join(view.references) + ". No change saved.\n")
        if view.code is CategoryDiagnosticCode.REFERENCES:
            return ("DAM categories: resolve active rule references before retirement: " +
                    ",".join(view.references) + ". No change saved.\n")
        if view.code is CategoryDiagnosticCode.INVALID_CHANGE:
            return "DAM categories failed: invalid local category change or catalog; no change saved.\n"
        if view.code is CategoryDiagnosticCode.INVALID_CONFIG:
            return "DAM categories failed: invalid local configuration.\n"
        return "DAM categories failed internally.\n"
    raise TypeError("Unsupported category presentation contract")
