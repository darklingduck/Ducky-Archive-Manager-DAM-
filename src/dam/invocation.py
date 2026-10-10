"""Closed immutable invocation intent and narrow, invocation-local display ports."""

from dataclasses import dataclass
from enum import StrEnum

from dam.category_operations import CategoriesRequest
from dam.category_presentation import CategoryPresentationSink
from dam.learn_operations import LearnRequest
from dam.learn_presentation import LearnPresentationSink
from dam.review_operations import ReviewRequest
from dam.review_presentation import ReviewPresentationSink
from dam.scan_operations import ScanRequest
from dam.scan_presentation import ScanPresentationSink
from dam.teaching_presentation import TeachingPresentationSink, TeachingDiagnosticSink
from dam.teaching_queries import QueryControlResult, QueryDisposition, TeachingQuery
from dam.teaching_resume import ResumeTeaching
from dam.teaching_save import SaveTeaching


class InvocationMode(StrEnum):
    DIRECT = "direct"
    CONTINUOUS = "continuous"  # Unsupported; no initialization or execution.


class OperationSelector(StrEnum):
    SCAN = "scan"
    REVIEW = "review"
    LEARN = "learn"
    CATEGORIES = "categories"
    TEACH_QUERY = "teach_query"
    TEACH_PREVIEW = "teach_preview"
    TEACH_SAVE = "teach_save"
    TEACH_RESUME = "teach_resume"


@dataclass(frozen=True, slots=True, repr=False)
class TeachingQueryRequest:
    query: TeachingQuery
    target_id: str | None = None
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if type(self.query) is not TeachingQuery or any(
                value is not None and type(value) is not str for value in (
                    self.target_id, self.learned_rules_file, self.category_catalog_file)):
            raise TypeError("Explicit scalar Teaching query inputs required")


@dataclass(frozen=True, slots=True, repr=False)
class TeachingPreviewRequest:
    work_id: str
    category_selector: str
    item_id: str | None = None
    learned_rules_file: str | None = None
    category_catalog_file: str | None = None

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (self.work_id, self.category_selector)) or any(
                value is not None and type(value) is not str for value in (
                    self.item_id, self.learned_rules_file, self.category_catalog_file)):
            raise TypeError("Explicit scalar Teaching preview inputs required")


OperationInput = (ScanRequest | ReviewRequest | LearnRequest | CategoriesRequest |
                  TeachingQueryRequest | TeachingPreviewRequest | SaveTeaching | ResumeTeaching)


def _input_type(operation: OperationSelector) -> type:
    # Fixed metadata only, created per call; no mutable registry or live services.
    return {OperationSelector.SCAN: ScanRequest, OperationSelector.REVIEW: ReviewRequest,
            OperationSelector.LEARN: LearnRequest, OperationSelector.CATEGORIES: CategoriesRequest,
            OperationSelector.TEACH_QUERY: TeachingQueryRequest, OperationSelector.TEACH_PREVIEW: TeachingPreviewRequest,
            OperationSelector.TEACH_SAVE: SaveTeaching, OperationSelector.TEACH_RESUME: ResumeTeaching}[operation]


@dataclass(frozen=True, slots=True, repr=False)
class InvocationRequest:
    mode: InvocationMode
    operation: OperationSelector
    inputs: OperationInput

    def __post_init__(self) -> None:
        if type(self.mode) is not InvocationMode or type(self.operation) is not OperationSelector:
            raise TypeError("Fixed invocation mode and Operation selector required")
        if type(self.inputs) is not _input_type(self.operation):
            raise TypeError("Matching immutable Operation input required")
        # Defensive revalidation is local and structural; no domain/source reads.
        self.inputs.__post_init__()


@dataclass(frozen=True, slots=True, repr=False)
class PresentationPorts:
    scan: ScanPresentationSink
    review: ReviewPresentationSink
    learn: LearnPresentationSink
    categories: CategoryPresentationSink
    teaching: TeachingPresentationSink
    teaching_diagnostics: TeachingDiagnosticSink

    def __post_init__(self) -> None:
        if any(not callable(port) for port in (self.scan, self.review, self.learn,
                self.categories, self.teaching, self.teaching_diagnostics)):
            raise TypeError("Narrow callable presentation ports required")


@dataclass(frozen=True, slots=True)
class InvocationFailure(QueryControlResult):
    """Controller rejection/defect only; interfaces may render a fixed diagnostic.

    A failure never asserts rollback of a selected Operation or authorizes retry.
    No private request, exception, or presentation data accompanies this result.
    """

    def __post_init__(self) -> None:
        QueryControlResult.__post_init__(self)
        if self.disposition not in (QueryDisposition.REJECTED, QueryDisposition.FAILED):
            raise TypeError("An invocation failure disposition is required")
