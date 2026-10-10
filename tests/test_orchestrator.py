"""Direct controller routing, control-only results, and opaque display ports."""

import ast
from dataclasses import FrozenInstanceError, dataclass, fields, replace
import gc
import inspect
from types import SimpleNamespace
import weakref

import pytest

import dam.orchestrator as controller
from dam.category_operations import CategoryOperation, CategoriesControlResult
from dam.invocation import (
    CategoriesRequest, InvocationFailure, InvocationMode, InvocationRequest, LearnRequest,
    OperationSelector, PresentationPorts, ResumeTeaching, ReviewRequest, SaveTeaching,
    ScanRequest, TeachingPreviewRequest, TeachingQuery, TeachingQueryRequest,
)
from dam.learn_operations import LearnControlResult
from dam.operation_bindings import teaching_query, teaching_preview, teaching_save, teaching_resume
from dam.scan_operations import ScanControlResult
from dam.teaching_presentation import TeachingDiagnostic, render_teaching
from dam.teaching_queries import QueryControlResult, QueryDisposition
from dam.teaching_save import TeachingSaveControlResult


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected application or source capability")


def sink(view):
    pass


def ports(**overrides):
    return PresentationPorts(**{**dict(scan=sink, review=sink, learn=sink, categories=sink,
                                      teaching=sink, teaching_diagnostics=sink), **overrides})


CASES = (
    (OperationSelector.SCAN, ScanRequest(), "scan", "scan"),
    (OperationSelector.REVIEW, ReviewRequest("PRIVATE-MSG", True), "review_message", "review"),
    (OperationSelector.LEARN, LearnRequest("PRIVATE-MSG", "finance"), "learn", "learn"),
    (OperationSelector.CATEGORIES, CategoriesRequest(CategoryOperation.LIST), "operate_categories", "categories"),
    (OperationSelector.TEACH_QUERY, TeachingQueryRequest(TeachingQuery.LIST), "teaching_query", "teaching"),
    (OperationSelector.TEACH_PREVIEW, TeachingPreviewRequest("CWQ-PRIVATE", "finance"), "teaching_preview", "teaching"),
    (OperationSelector.TEACH_SAVE, SaveTeaching("CWQ-PRIVATE", "finance", "PRIVATE-FINGERPRINT"), "teaching_save", "teaching"),
    (OperationSelector.TEACH_RESUME, ResumeTeaching("TEACH-PRIVATE"), "teaching_resume", "teaching"),
)


@pytest.fixture(autouse=True)
def no_sources(monkeypatch):
    for _, _, name, _ in CASES:
        monkeypatch.setattr(controller, name, forbidden)
    for target in ("dam.config.load_config", "dam.storage.Storage.open", "dam.auth.authenticate",
                   "dam.scan.authenticate", "dam.durable_gmail.authenticate", "dam.review.authenticate",
                   "dam.gmail.read_message", "dam.gmail.read_inbox"):
        monkeypatch.setattr(target, forbidden)


@pytest.mark.parametrize("operation,inputs,name,port", CASES)
@pytest.mark.parametrize("disposition", list(QueryDisposition))
def test_exactly_one_operation_and_direct_termination(monkeypatch, operation, inputs, name, port, disposition):
    calls = []
    presentation = ports()
    result = QueryControlResult(disposition)
    def selected(request, **kwargs):
        calls.append(request)
        assert request is inputs and kwargs["present"] is getattr(presentation, port)
        if port == "teaching":
            assert kwargs["diagnostic"] is presentation.teaching_diagnostics
        else:
            assert set(kwargs) == {"present"}
        return result
    monkeypatch.setattr(controller, name, selected)
    returned = controller.execute(InvocationRequest(InvocationMode.DIRECT, operation, inputs), presentation=presentation)
    assert returned is result and calls == [inputs]
    assert fields(returned)[0].name == "disposition"
    assert "PRIVATE" not in repr(returned)


@pytest.mark.parametrize("selector,wrong_input", [
    (selector, wrong) for selector, matching, _, _ in CASES for _, wrong, _, _ in CASES
    if type(wrong) is not type(matching)
])
def test_all_selector_input_mismatches_rejected_before_initialization(selector, wrong_input):
    with pytest.raises(TypeError, match="Matching"):
        InvocationRequest(InvocationMode.DIRECT, selector, wrong_input)
    # Revalidate a deliberately forged instance; frozen construction is not a
    # reason to trust a manipulated object at the execution boundary.
    forged = object.__new__(InvocationRequest)
    object.__setattr__(forged, "mode", InvocationMode.DIRECT)
    object.__setattr__(forged, "operation", selector)
    object.__setattr__(forged, "inputs", wrong_input)
    assert controller.execute(forged, presentation=ports()) == InvocationFailure(QueryDisposition.REJECTED)


def test_unsupported_continuous_mode_and_untyped_inputs_do_no_work():
    request = InvocationRequest(InvocationMode.CONTINUOUS, OperationSelector.SCAN, ScanRequest())
    assert controller.execute(request, presentation=ports()) == InvocationFailure(QueryDisposition.REJECTED)
    for value in ({"PRIVATE": "data"}, SimpleNamespace(operation="scan"), lambda: None, object()):
        assert controller.execute(value, presentation=ports()) == InvocationFailure(QueryDisposition.REJECTED)
        with pytest.raises(TypeError):
            InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, value)
    assert controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()),
                              presentation={"PRIVATE": "ports"}) == InvocationFailure(QueryDisposition.REJECTED)


@pytest.mark.parametrize("result", [ScanControlResult(QueryDisposition.INCOMPLETE, True),
    CategoriesControlResult(QueryDisposition.COMPLETED, True),
    LearnControlResult(QueryDisposition.COMPLETED, QueryDisposition.REJECTED),
    TeachingSaveControlResult(QueryDisposition.COMPLETED, True)])
def test_existing_control_facts_return_unchanged_and_never_retry(monkeypatch, result):
    calls = []
    def selected(*args, **kwargs):
        calls.append(True)
        return result
    monkeypatch.setattr(controller, "scan", selected)
    assert controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()),
                              presentation=ports()) is result
    assert calls == [True]


def test_controller_never_inspects_or_retains_display_payload(monkeypatch):
    class OpaqueDisplay:
        def __getattribute__(self, name):
            raise AssertionError("Controller touched private presentation")
        def __repr__(self):
            raise AssertionError("Controller rendered private presentation")
    refs = []
    delivered = []
    def writer(view):
        refs.append(weakref.ref(view))
        delivered.append(True)
    presentation = ports(review=writer)
    def selected(request, *, present):
        assert present is writer
        view = OpaqueDisplay()
        present(view)
        return QueryControlResult(QueryDisposition.COMPLETED)
    monkeypatch.setattr(controller, "review_message", selected)
    returned = controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.REVIEW, ReviewRequest("PRIVATE", True)),
                                  presentation=presentation)
    assert returned == QueryControlResult(QueryDisposition.COMPLETED) and delivered == [True]
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert controller.execute.__dict__ == {} and controller.execute.__closure__ is None


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_escaped_exception_sanitized_no_retry(monkeypatch, caplog, error):
    calls = []
    def selected(*args, **kwargs):
        calls.append(True)
        raise error("PRIVATE-CREDENTIAL-SENTINEL")
    monkeypatch.setattr(controller, "scan", selected)
    result = controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()), presentation=ports())
    assert result == InvocationFailure(QueryDisposition.FAILED) and calls == [True]
    assert "PRIVATE" not in repr(result) + caplog.text


def test_broad_and_extended_results_rejected_without_introspection(monkeypatch):
    class OpaqueDomain:
        def __getattribute__(self, name):
            raise AssertionError("Controller inspected broad domain result")
    @dataclass(frozen=True)
    class BroadControl(QueryControlResult):
        private: str = "PRIVATE-SENTINEL"
    for result in (OpaqueDomain(), BroadControl(QueryDisposition.COMPLETED), {"PRIVATE": "domain"}):
        monkeypatch.setattr(controller, "scan", lambda *_args, **_kwargs: result)
        assert controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()),
                                  presentation=ports()) == InvocationFailure(QueryDisposition.FAILED)


def test_invocation_models_are_immutable_and_do_not_carry_authority():
    request = InvocationRequest(InvocationMode.DIRECT, OperationSelector.TEACH_PREVIEW, TeachingPreviewRequest("CWQ", "finance"))
    assert [field.name for field in fields(request)] == ["mode", "operation", "inputs"]
    assert [field.name for field in fields(InvocationFailure(QueryDisposition.REJECTED))] == ["disposition"]
    for value in (request, request.inputs, TeachingQueryRequest(TeachingQuery.LIST), ports(), InvocationFailure(QueryDisposition.FAILED)):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
    for field in fields(request.inputs):
        with pytest.raises(TypeError):
            replace(request.inputs, **{field.name: {"PRIVATE": "object"}})
    with pytest.raises(TypeError):
        PresentationPorts(None, sink, sink, sink, sink, sink)
    with pytest.raises(TypeError):
        InvocationRequest("direct", OperationSelector.SCAN, ScanRequest())


@pytest.mark.parametrize("inputs,binding,primitive", [
    (TeachingQueryRequest(TeachingQuery.LIST), teaching_query, "query_teaching"),
    (TeachingPreviewRequest("CWQ", "finance"), teaching_preview, "preview_teaching"),
    (SaveTeaching("CWQ", "finance", "fingerprint"), teaching_save, "save_teaching"),
    (ResumeTeaching("TEACH"), teaching_resume, "resume_teaching"),
])
def test_teaching_bindings_bootstrap_once_invoke_once_without_recovery(monkeypatch, inputs, binding, primitive):
    calls = []
    from dam.models import Settings
    settings = Settings()
    def bootstrap(*args, **kwargs):
        calls.append("settings")
        return SimpleNamespace(settings=settings)
    def selected(*args, **kwargs):
        calls.append(primitive)
        assert kwargs["settings"] is settings
        return QueryControlResult(QueryDisposition.INCOMPLETE)
    monkeypatch.setattr("dam.operation_bindings.load_config", bootstrap)
    monkeypatch.setattr("dam.operation_bindings." + primitive, selected)
    assert binding(inputs, present=forbidden, diagnostic=forbidden) == QueryControlResult(QueryDisposition.INCOMPLETE)
    assert calls == ["settings", primitive]


@pytest.mark.parametrize("error,disposition,text", [
    (ValueError, QueryDisposition.REJECTED, "DAM teach failed or remains pending; inspect the teaching operation and retry safely.\n"),
    (RuntimeError, QueryDisposition.FAILED, "DAM teach failed internally.\n"),
])
def test_teaching_bootstrap_failures_are_fixed_diagnostics(monkeypatch, error, disposition, text):
    def failure(*args):
        raise error("PRIVATE-SETTINGS")
    monkeypatch.setattr("dam.operation_bindings.load_config", failure)
    views = []
    result = teaching_save(SaveTeaching("CWQ", "finance", "fingerprint"), present=forbidden, diagnostic=views.append)
    assert result == QueryControlResult(disposition)
    assert type(views[0]) is TeachingDiagnostic and render_teaching(views[0]) == text
    assert "PRIVATE" not in repr(result) + repr(views)


def test_controller_dependencies_have_no_domain_or_rendering_access():
    tree = ast.parse(inspect.getsource(controller))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not calls & {"load_config", "print", "input", "classify", "propose_action", "authenticate", "render_teaching", "read_inbox"}
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(tree))
    assert {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} <= {
        "dam.category_operations", "dam.invocation", "dam.learn_operations", "dam.operation_bindings",
        "dam.review_operations", "dam.scan_operations", "dam.teaching_queries", "dam.teaching_save",
    }


@pytest.mark.parametrize("fail", [False, True])
def test_selected_operation_owns_cleanup_on_success_or_failure(monkeypatch, fail):
    from contextlib import contextmanager
    lifecycle = []
    @contextmanager
    def resource():
        lifecycle.append("open")
        try:
            yield
        finally:
            lifecycle.append("closed")
    def selected(*args, **kwargs):
        with resource():
            if fail:
                raise RuntimeError("PRIVATE-FAILURE")
            return QueryControlResult(QueryDisposition.COMPLETED)
    monkeypatch.setattr(controller, "scan", selected)
    result = controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()), presentation=ports())
    assert result.disposition is (QueryDisposition.FAILED if fail else QueryDisposition.COMPLETED)
    assert lifecycle == ["open", "closed"]


def test_corrupt_control_fact_rejected_without_leaking_values(monkeypatch):
    result = ScanControlResult(QueryDisposition.COMPLETED)
    object.__setattr__(result, "presentation_failed", "PRIVATE-SENTINEL")
    monkeypatch.setattr(controller, "scan", lambda *_args, **_kwargs: result)
    returned = controller.execute(InvocationRequest(InvocationMode.DIRECT, OperationSelector.SCAN, ScanRequest()), presentation=ports())
    assert returned == InvocationFailure(QueryDisposition.FAILED)
    assert "PRIVATE" not in repr(returned)


def test_teaching_diagnostic_contract_is_immutable_and_fixed():
    diagnostic = TeachingDiagnostic("output")
    assert [field.name for field in fields(diagnostic)] == ["code"]
    with pytest.raises(FrozenInstanceError):
        diagnostic.code = "private"
    for code in ("PRIVATE-SENTINEL", {"PRIVATE": "value"}):
        with pytest.raises(TypeError):
            TeachingDiagnostic(code)
    assert render_teaching(diagnostic) == "DAM teaching outcome could not be displayed; inspect teaching status before retrying.\n"


def test_failed_teaching_diagnostic_cannot_change_known_completion(monkeypatch):
    from dam.models import Settings
    calls = []
    monkeypatch.setattr("dam.operation_bindings.load_config", lambda *_args: SimpleNamespace(settings=Settings()))
    def completed(*args, **kwargs):
        calls.append(True)
        return TeachingSaveControlResult(QueryDisposition.COMPLETED, presentation_failed=True)
    monkeypatch.setattr("dam.operation_bindings.save_teaching", completed)
    result = teaching_save(SaveTeaching("CWQ", "finance", "fingerprint"), present=forbidden, diagnostic=forbidden)
    assert result == TeachingSaveControlResult(QueryDisposition.COMPLETED, True)
    assert calls == [True]
