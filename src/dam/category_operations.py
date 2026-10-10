"""Bounded local Categories Operation; persistence remains in category primitives."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re

from dam.categories import (
    CategoryError, default_catalog_path, load_catalog, merge_categories,
    new_category_id, propose_change, resolve_category, save_change,
)
from dam.category_presentation import (
    CategoryChangeDisplay, CategoryConfirmation, CategoryDetail, CategoryDiagnostic,
    CategoryDiagnosticCode, CategoryListing, CategoryListingRow, CategoryPresentationSink,
)
from dam.config import ConfigurationError, load_config
from dam.learning import LearningError, default_learned_rules_path
from dam.models import CategoriesConfig
from dam.scan import default_config_directory
from dam.teaching_queries import QueryControlResult, QueryDisposition


class CategoryOperation(StrEnum):
    LIST = "list"
    SHOW = "show"
    ADD = "add"
    RENAME = "rename"
    MOVE = "move"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True, repr=False)
class CategoriesRequest:
    operation: CategoryOperation
    selector: str | None = None
    key: str | None = None
    name: str | None = None
    parent: str | None = None
    save: bool = False
    confirm_fingerprint: str | None = None
    confirm_id: str | None = None
    confirm_retire: str | None = None
    catalog_file: str | None = None
    learned_rules_file: str | None = None

    def __post_init__(self) -> None:
        if (type(self.operation) is not CategoryOperation or type(self.save) is not bool or
                any(value is not None and type(value) is not str for value in (
                    self.selector, self.key, self.name, self.parent, self.confirm_fingerprint,
                    self.confirm_id, self.confirm_retire, self.catalog_file, self.learned_rules_file))):
            raise TypeError("Explicit scalar category inputs required")
        if self.operation in (CategoryOperation.LIST, CategoryOperation.SHOW):
            if (self.save or any(value is not None for value in (
                    self.key, self.name, self.parent, self.confirm_fingerprint, self.confirm_id, self.confirm_retire)) or
                    (self.operation is CategoryOperation.LIST and self.selector is not None) or
                    (self.operation is CategoryOperation.SHOW and self.selector is None)):
                raise ValueError("Invalid category query inputs")
        elif ((self.operation is CategoryOperation.ADD and
               (self.selector is not None or self.key is None or self.name is None)) or
              (self.operation is not CategoryOperation.ADD and (self.selector is None or self.key is not None)) or
              (self.operation is CategoryOperation.RENAME and self.name is None) or
              (self.operation in (CategoryOperation.MOVE, CategoryOperation.RETIRE) and self.name is not None) or
              (self.operation in (CategoryOperation.RENAME, CategoryOperation.RETIRE) and self.parent is not None) or
              (self.operation is not CategoryOperation.ADD and self.confirm_id is not None) or
              (self.operation is not CategoryOperation.RETIRE and self.confirm_retire is not None)):
            raise ValueError("Invalid category change inputs")


@dataclass(frozen=True, slots=True)
class CategoriesControlResult(QueryControlResult):
    """Only execution disposition and delivery failure; neither authorizes retry."""

    presentation_failed: bool = False

    def __post_init__(self) -> None:
        QueryControlResult.__post_init__(self)
        if type(self.presentation_failed) is not bool:
            raise TypeError("A category presentation failure flag is required")


def project_category_listing(categories: CategoriesConfig) -> CategoryListing:
    """Detach the validated, ordered hierarchy; no domain object reaches Writer."""
    categories = CategoriesConfig.model_validate(categories.model_dump(mode="python"))
    children = {item.id: [] for item in categories.categories}
    roots = []
    for item in categories.categories:
        (children[item.parent_id] if item.parent_id else roots).append(item)
    rows = []
    def visit(item, depth):
        rows.append(CategoryListingRow(item.name, item.key or item.id, depth, item.status == "retired"))
        for child in sorted(children[item.id], key=lambda item: (item.name.casefold(), item.id)):
            visit(child, depth + 1)
    for root in sorted(roots, key=lambda item: (item.name.casefold(), item.id)):
        visit(root, 0)
    return CategoryListing(tuple(rows))


def _deliver_failure(code: CategoryDiagnosticCode, disposition: QueryDisposition,
                     present: CategoryPresentationSink, references: tuple[str, ...] = ()) -> CategoriesControlResult:
    try:
        present(CategoryDiagnostic(code, references))
    except Exception:
        return CategoriesControlResult(disposition, presentation_failed=True)
    return CategoriesControlResult(disposition)


def operate_categories(request: CategoriesRequest, *, present: CategoryPresentationSink) -> CategoriesControlResult:
    """Run once; release display after all repository handles/locks have closed.

    Returned completion is distinct from projection/delivery. Exceptions do not
    assert rollback: atomic replacement can precede a persistence/reporting error.
    No automatic save, retry, or sibling Operation selection occurs here.
    """
    try:
        if type(request) is not CategoriesRequest:
            return _deliver_failure(CategoryDiagnosticCode.INVALID_CHANGE, QueryDisposition.REJECTED, present)
        path = Path(request.catalog_file) if request.catalog_file else default_catalog_path()
        base = load_config(default_config_directory())
        catalog = load_catalog(path)
        effective = merge_categories(base.categories, catalog)
        if request.operation is CategoryOperation.SHOW:
            item = resolve_category(request.selector, effective, active=False)
        elif request.operation is not CategoryOperation.LIST:
            if request.save != (request.confirm_fingerprint is not None):
                raise CategoryError("save_requires_preview_fingerprint")
            if request.operation is CategoryOperation.ADD and request.save and not request.confirm_id:
                raise CategoryError("save_requires_generated_id")
            if request.operation is CategoryOperation.RETIRE and request.save and not request.confirm_retire:
                raise CategoryError("retire_requires_identity_confirmation")
            learned = ((Path(request.learned_rules_file) if request.learned_rules_file else default_learned_rules_path())
                       if request.operation is CategoryOperation.RETIRE else None)
            generated = ((request.confirm_id if request.save else new_category_id(effective))
                         if request.operation is CategoryOperation.ADD else None)
            change = propose_change(base, catalog, request.operation.value, selector=request.selector,
                key=request.key, name=request.name, parent=request.parent, generated_id=generated,
                learned_rules_path=learned)
            if (request.operation is CategoryOperation.RETIRE and request.save and
                    request.confirm_retire != change.after.permanent_id):
                raise CategoryError("retirement_identity_mismatch")
            if request.save:
                save_change(path, base, change, expected_fingerprint=request.confirm_fingerprint,
                            learned_rules_path=learned)
    except CategoryError as error:
        # Only the two existing protection diagnostics intentionally release IDs;
        # all other exception contents are discarded. Reject malformed ID text.
        reason = str(error)
        prefix, _, detail = reason.partition(":")
        references = tuple(detail.split(",")) if detail else ()
        if references and all(re.fullmatch(r"(?:[a-z][a-z0-9_]*|CAT-[A-Z2-7]{26})", value) for value in references):
            if prefix == "category_has_active_children":
                return _deliver_failure(CategoryDiagnosticCode.CHILDREN, QueryDisposition.REJECTED, present, references)
            if prefix == "category_has_active_rule_references":
                return _deliver_failure(CategoryDiagnosticCode.REFERENCES, QueryDisposition.REJECTED, present, references)
        return _deliver_failure(CategoryDiagnosticCode.INVALID_CHANGE, QueryDisposition.REJECTED, present)
    except (ConfigurationError, LearningError, OSError, ValueError):
        return _deliver_failure(CategoryDiagnosticCode.INVALID_CONFIG, QueryDisposition.REJECTED, present)
    except Exception:
        return _deliver_failure(CategoryDiagnosticCode.INTERNAL, QueryDisposition.FAILED, present)

    # Business completion is established. Projection/rendering cannot undo it or
    # cause another mutation. The released values are private display-only data.
    try:
        if request.operation is CategoryOperation.LIST:
            view = project_category_listing(effective)
        elif request.operation is CategoryOperation.SHOW:
            view = CategoryDetail(item.permanent_id, item.key or item.id, item.name, item.parent_id, item.status)
        else:
            def detail(item):
                return CategoryDetail(item.permanent_id, item.key, item.name, item.parent_permanent_id, item.status)
            confirmation = None if request.save else CategoryConfirmation(request.operation.value,
                change.after.permanent_id, change.fingerprint, request.selector, request.key, request.name,
                request.parent, request.catalog_file, request.learned_rules_file)
            view = CategoryChangeDisplay(request.operation.value, request.save,
                detail(change.before) if change.before else None, detail(change.after),
                change.referenced_rules, change.fingerprint, confirmation)
        present(view)
    except Exception:
        return CategoriesControlResult(QueryDisposition.COMPLETED, presentation_failed=True)
    return CategoriesControlResult(QueryDisposition.COMPLETED)
