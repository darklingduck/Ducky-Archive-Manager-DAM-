"""Teaching entry bindings: existing bootstrap and safe diagnostics, no recovery.

Each binding is part of its selected Operation's entry boundary, adapting explicit
inputs to its unchanged implementation. It does not select another Operation,
inspect CLI arguments, or coordinate Teaching's internal durable transaction.
"""

from pathlib import Path

from dam.categories import CategoryError, default_catalog_path
from dam.config import ConfigurationError, load_config
from dam.invocation import TeachingQueryRequest, TeachingPreviewRequest
from dam.learning import LearningError
from dam.models import Settings
from dam.scan import default_config_directory
from dam.storage import StorageError
from dam.teaching import TeachingError
from dam.teaching_presentation import TeachingDiagnostic, TeachingDiagnosticSink, TeachingPresentationSink
from dam.teaching_queries import QueryControlResult, QueryDisposition, query_teaching, preview_teaching
from dam.teaching_resume import ResumeTeaching, resume_teaching
from dam.teaching_save import SaveTeaching, TeachingSaveControlResult, save_teaching


def _settings() -> Settings:
    return load_config(default_config_directory()).settings


def _report(result: QueryControlResult, diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    if type(result) not in (QueryControlResult, TeachingSaveControlResult):
        raise TypeError("Minimal Teaching control result required")
    code = None
    if type(result) is TeachingSaveControlResult and result.presentation_failed:
        code = "output"
    elif result.disposition is QueryDisposition.FAILED:
        code = "mutation_internal" if type(result) is TeachingSaveControlResult else "internal"
    elif result.disposition is QueryDisposition.REJECTED:
        code = "rejected"
    if code is not None:
        try:
            diagnostic(TeachingDiagnostic(code))
        except Exception:
            if type(result) is TeachingSaveControlResult:
                return TeachingSaveControlResult(result.disposition, presentation_failed=True)
            return QueryControlResult(QueryDisposition.FAILED)
    return result


def _setup_failure(error: Exception, diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    expected = isinstance(error, (TeachingError, StorageError, LearningError, CategoryError,
                                 ConfigurationError, ValueError, OSError))
    return _report(QueryControlResult(QueryDisposition.REJECTED if expected else QueryDisposition.FAILED), diagnostic)


def teaching_query(request: TeachingQueryRequest, *, present: TeachingPresentationSink,
                   diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    try:
        settings = _settings()
        catalog = Path(request.category_catalog_file) if request.category_catalog_file else default_catalog_path()
        catalog = catalog if request.category_catalog_file or catalog.exists() else None
        result = query_teaching(request.query, settings=settings, target_id=request.target_id,
            learned_rules_path=Path(request.learned_rules_file) if request.learned_rules_file else None,
            category_catalog_path=catalog, present=present)
    except Exception as error:
        return _setup_failure(error, diagnostic)
    return _report(result, diagnostic)


def teaching_preview(request: TeachingPreviewRequest, *, present: TeachingPresentationSink,
                     diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    try:
        result = preview_teaching(settings=_settings(), work_id=request.work_id,
            category_selector=request.category_selector, item_id=request.item_id,
            learned_rules_file=request.learned_rules_file, category_catalog_file=request.category_catalog_file,
            present=present)
    except Exception as error:
        return _setup_failure(error, diagnostic)
    return _report(result, diagnostic)


def teaching_save(request: SaveTeaching, *, present: TeachingPresentationSink,
                  diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    try:
        result = save_teaching(request, settings=_settings(), present=present)
    except Exception as error:
        return _setup_failure(error, diagnostic)
    return _report(result, diagnostic)


def teaching_resume(request: ResumeTeaching, *, present: TeachingPresentationSink,
                    diagnostic: TeachingDiagnosticSink) -> QueryControlResult:
    try:
        result = resume_teaching(request, settings=_settings(), present=present)
    except Exception as error:
        return _setup_failure(error, diagnostic)
    return _report(result, diagnostic)
