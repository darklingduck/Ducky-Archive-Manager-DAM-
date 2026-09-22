"""Bounded offline query operations; separate control and display releases.

The caller selects exactly one query. No operation chooses continuation or calls
another application operation. Storage lifetime belongs to this invocation;
TeachingService's existing inspection/configuration helpers remain internal.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dam.categories import CategoryError
from dam.config import ConfigurationError
from dam.learning import LearningError
from dam.models import Settings
from dam.presentation import safe_metadata_text
from dam.storage import Storage, StorageError
from dam.teaching import TeachingError, TeachingService
from dam.teaching_presentation import (
    IncompleteTeachingRow, TeachingPresentationSink, TeachingQueueListing,
    TeachingStatus, TeachingWorkDetail, WorkListingRow,
)


class TeachingQuery(StrEnum):
    LIST = "list"
    SHOW = "show"
    STATUS = "status"


class QueryDisposition(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QueryControlResult:
    """Execution outcome only; queried teaching state is presentation data."""

    disposition: QueryDisposition

    def __post_init__(self) -> None:
        if type(self.disposition) is not QueryDisposition:
            raise TypeError("A fixed query disposition is required")


def query_teaching(query: TeachingQuery, *, settings: Settings,
                   present: TeachingPresentationSink, target_id: str | None = None,
                   learned_rules_path: Path | None = None,
                   category_catalog_path: Path | None = None) -> QueryControlResult:
    """Run one explicit query and release only its authorized display fields.

    Expected failures and internal defects return fixed dispositions, never raw
    exception text/objects. No payload is retained by the operation after return.
    Storage.open retains its existing setup/migration behavior.
    """
    try:
        if type(query) is not TeachingQuery or (
                query is TeachingQuery.LIST and target_id is not None) or (
                query is not TeachingQuery.LIST and (type(target_id) is not str or not target_id)):
            return QueryControlResult(QueryDisposition.REJECTED)
        with Storage.open(settings) as store:
            service = TeachingService(store, learned_rules_path=learned_rules_path,
                                      category_catalog_path=category_catalog_path)
            if query is TeachingQuery.LIST:
                view = TeachingQueueListing(
                    tuple(WorkListingRow(work.work_id, work.state, len(work.members))
                          for work in service.list_work()),
                    tuple(IncompleteTeachingRow(row["teaching_id"], row["status"])
                          for row in store.incomplete_teaching_operations()))
            elif query is TeachingQuery.SHOW:
                work, item, observation = service.inspect(target_id)
                view = TeachingWorkDetail(work.work_id, work.state, item.item_id,
                    observation.run_id, observation.observed_at.isoformat(),
                    safe_metadata_text(observation.metadata.sender,
                        present="sender" in observation.metadata.model_fields_set),
                    safe_metadata_text(observation.metadata.subject,
                        present="subject" in observation.metadata.model_fields_set),
                    tuple(sorted(category.key for category in service.categories())))
            else:
                row = store.teaching_operation(target_id)
                if row is None:
                    raise TeachingError("Unknown teaching operation")
                evaluated, resolved, unresolved = store.teaching_evaluation_counts(target_id)
                view = TeachingStatus(row["teaching_id"], row["status"], evaluated,
                                      resolved, unresolved, row["config_after"])
        # The Writer receives a detached display contract after storage closes.
        present(view)
        return QueryControlResult(QueryDisposition.COMPLETED)
    except (TeachingError, StorageError, LearningError, CategoryError,
            ConfigurationError, ValueError, OSError):
        return QueryControlResult(QueryDisposition.REJECTED)
    except Exception:
        return QueryControlResult(QueryDisposition.FAILED)
