"""Resume boundary; Teaching alone owns recovery, reconciliation, and checkpoints."""

from dataclasses import dataclass
from pathlib import Path

from dam.categories import CategoryError, default_catalog_path
from dam.config import ConfigurationError
from dam.learning import LearningError
from dam.models import Settings
from dam.storage import Storage, StorageError
from dam.teaching import TeachingError, TeachingService
from dam.teaching_presentation import TeachingPresentationSink, TeachingSaveDisplay
from dam.teaching_queries import QueryDisposition
from dam.teaching_save import TeachingSaveControlResult


@dataclass(frozen=True, slots=True, repr=False)
class ResumeTeaching:
    teaching_id: str
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if type(self.teaching_id) is not str or any(
                value is not None and type(value) is not str for value in (
                    self.learned_rules_file, self.category_catalog_file)):
            raise TypeError("Explicit scalar teaching resume inputs required")


def resume_teaching(request: ResumeTeaching, *, settings: Settings,
                    present: TeachingPresentationSink) -> TeachingSaveControlResult:
    """Resume once and release the existing aggregate outcome after storage closes.

    The save control/display contracts also describe resume's identical output.
    An exception does not assert rollback. An incomplete result is a fact, never
    a continuation instruction. Failed presentation never retries recovery.
    """
    try:
        if type(request) is not ResumeTeaching:
            return TeachingSaveControlResult(QueryDisposition.REJECTED)
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        with Storage.open(settings) as store:
            service = TeachingService(store,
                learned_rules_path=Path(request.learned_rules_file) if request.learned_rules_file else None,
                category_catalog_path=catalog)
            outcome = service.resume(request.teaching_id)
    except (TeachingError, StorageError, LearningError, CategoryError,
            ConfigurationError, ValueError, OSError):
        return TeachingSaveControlResult(QueryDisposition.REJECTED)
    except Exception:
        return TeachingSaveControlResult(QueryDisposition.FAILED)

    disposition = (QueryDisposition.COMPLETED if outcome.status == "completed"
                   else QueryDisposition.INCOMPLETE)
    try:
        present(TeachingSaveDisplay(outcome.teaching_id, outcome.status, outcome.reevaluated,
                                   outcome.resolved, outcome.unresolved, outcome.insufficient))
    except Exception:
        return TeachingSaveControlResult(disposition, presentation_failed=True)
    return TeachingSaveControlResult(disposition)
