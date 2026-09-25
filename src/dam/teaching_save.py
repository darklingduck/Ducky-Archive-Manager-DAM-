"""Save boundary around Teaching's existing recoverable transaction.

No retries, rollback, checkpoint reconstruction, or workflow transitions live here.
A failure disposition does not imply no writes: Teaching's durable status remains
truth after an exception. Only a returned Teaching outcome establishes completion
or known incomplete work. Presentation failure is separate from that outcome.
"""

from dataclasses import dataclass
from pathlib import Path

from dam.categories import CategoryError, default_catalog_path
from dam.config import ConfigurationError
from dam.learning import LearningError
from dam.models import Settings
from dam.storage import Storage, StorageError
from dam.teaching import TeachingError, TeachingService
from dam.teaching_presentation import TeachingPresentationSink, TeachingSaveDisplay
from dam.teaching_queries import QueryControlResult, QueryDisposition


@dataclass(frozen=True, slots=True, repr=False)
class SaveTeaching:
    work_id: str
    category_selector: str
    confirm_fingerprint: str
    item_id: str | None = None
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (
                self.work_id, self.category_selector, self.confirm_fingerprint)) or any(
                value is not None and type(value) is not str for value in (
                    self.item_id, self.learned_rules_file, self.category_catalog_file)):
            raise TypeError("Explicit scalar teaching save inputs required")


@dataclass(frozen=True, slots=True)
class TeachingSaveControlResult(QueryControlResult):
    """Known execution disposition plus display-delivery failure; no private data.

    Unlike a query, failed output must not turn a completed mutation into a failed
    mutation. Neither field authorizes retry. Exceptions during Teaching remain
    REJECTED/FAILED without asserting rollback or inventing a recovery state.
    """

    presentation_failed: bool = False

    def __post_init__(self) -> None:
        QueryControlResult.__post_init__(self)
        if type(self.presentation_failed) is not bool:
            raise TypeError("A presentation failure flag is required")


def save_teaching(request: SaveTeaching, *, settings: Settings,
                  present: TeachingPresentationSink) -> TeachingSaveControlResult:
    """Confirm once, close storage, then release only aggregate display fields."""
    try:
        if type(request) is not SaveTeaching:
            return TeachingSaveControlResult(QueryDisposition.REJECTED)
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        with Storage.open(settings) as store:
            service = TeachingService(store,
                learned_rules_path=Path(request.learned_rules_file) if request.learned_rules_file else None,
                category_catalog_path=catalog)
            outcome = service.confirm(request.work_id, request.category_selector,
                confirm_fingerprint=request.confirm_fingerprint, item_id=request.item_id)
    except (TeachingError, StorageError, LearningError, CategoryError,
            ConfigurationError, ValueError, OSError):
        return TeachingSaveControlResult(QueryDisposition.REJECTED)
    except Exception:
        return TeachingSaveControlResult(QueryDisposition.FAILED)

    disposition = (QueryDisposition.COMPLETED if outcome.status == "completed"
                   else QueryDisposition.INCOMPLETE)
    # Projection/rendering cannot roll back or repeat confirm(). Never retain a
    # raw exception or infer that an already committed transaction was undone.
    try:
        view = TeachingSaveDisplay(outcome.teaching_id, outcome.status, outcome.reevaluated,
                                   outcome.resolved, outcome.unresolved, outcome.insufficient)
        present(view)
    except Exception:
        return TeachingSaveControlResult(disposition, presentation_failed=True)
    return TeachingSaveControlResult(disposition)
