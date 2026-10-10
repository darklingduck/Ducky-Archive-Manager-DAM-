"""Sole application controller: one permitted direct Operation, then termination.

No session/domain state, configuration/source access, presentation interception,
retries, or continuation live here. Continuous mode is reserved but unsupported.
"""

from dam.category_operations import CategoriesControlResult, operate_categories
from dam.invocation import InvocationFailure, InvocationMode, InvocationRequest, OperationSelector, PresentationPorts
from dam.learn_operations import LearnControlResult, learn
from dam.operation_bindings import teaching_query, teaching_preview, teaching_save, teaching_resume
from dam.review_operations import review_message
from dam.scan_operations import ScanControlResult, scan
from dam.teaching_queries import QueryControlResult, QueryDisposition
from dam.teaching_save import TeachingSaveControlResult


def execute(request: InvocationRequest, *, presentation: PresentationPorts) -> QueryControlResult:
    """Validate intent, pass only the selected display capabilities, invoke once."""
    try:
        if type(request) is not InvocationRequest or type(presentation) is not PresentationPorts:
            return InvocationFailure(QueryDisposition.REJECTED)
        request.__post_init__()
        presentation.__post_init__()
        if request.mode is not InvocationMode.DIRECT:
            return InvocationFailure(QueryDisposition.REJECTED)
    except Exception:
        return InvocationFailure(QueryDisposition.REJECTED)
    try:
        match request.operation:
            case OperationSelector.SCAN:
                result = scan(request.inputs, present=presentation.scan)
            case OperationSelector.REVIEW:
                result = review_message(request.inputs, present=presentation.review)
            case OperationSelector.LEARN:
                result = learn(request.inputs, present=presentation.learn)
            case OperationSelector.CATEGORIES:
                result = operate_categories(request.inputs, present=presentation.categories)
            case OperationSelector.TEACH_QUERY:
                result = teaching_query(request.inputs, present=presentation.teaching, diagnostic=presentation.teaching_diagnostics)
            case OperationSelector.TEACH_PREVIEW:
                result = teaching_preview(request.inputs, present=presentation.teaching, diagnostic=presentation.teaching_diagnostics)
            case OperationSelector.TEACH_SAVE:
                result = teaching_save(request.inputs, present=presentation.teaching, diagnostic=presentation.teaching_diagnostics)
            case OperationSelector.TEACH_RESUME:
                result = teaching_resume(request.inputs, present=presentation.teaching, diagnostic=presentation.teaching_diagnostics)
        # Reject broad or extended objects without inspecting their contents.
        if type(result) not in (QueryControlResult, CategoriesControlResult, LearnControlResult,
                                ScanControlResult, TeachingSaveControlResult):
            return InvocationFailure(QueryDisposition.FAILED)
        result.__post_init__()
        return result  # Direct mode always stops; INCOMPLETE never requests resume.
    except Exception:
        return InvocationFailure(QueryDisposition.FAILED)
