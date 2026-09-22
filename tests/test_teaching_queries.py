"""Query/control/display isolation using synthetic metadata and private temp state."""

from dataclasses import FrozenInstanceError, fields, is_dataclass
import gc
from types import SimpleNamespace
import weakref

import pytest

from dam.cli import main
from dam.config import load_config
from dam.identifiers import new_object_id
from dam.models import Settings
from dam.scan import default_config_directory
from dam.storage import Storage, StorageError
from dam.teaching import TeachingService
from dam.teaching_presentation import (
    IncompleteTeachingRow, TeachingQueueListing, TeachingStatus, TeachingWorkDetail,
    WorkListingRow, render_teaching,
)
from dam.teaching_queries import QueryControlResult, QueryDisposition, TeachingQuery, query_teaching
from test_durable_gmail import FakeGmail, NOW, message, run

SAFE_FAILURE = "DAM teach failed or remains pending; inspect the teaching operation and retry safely.\n"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected terminal/source capability")


@pytest.fixture
def context(tmp_path, monkeypatch):
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    learned = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    with Storage.open(settings) as store:
        result = run(monkeypatch, store, FakeGmail([
            message("native-A", sender="private-A@example.invalid", subject="SUBJECT-A-sentinel"),
            message("native-B", sender="private-B@example.invalid", subject="SUBJECT-B-sentinel"),
            message("native-C", sender="private-C@example.invalid", subject="SUBJECT-C-sentinel"),
        ]))
        work = {store.item(row.representative_item_id).source_item_id: row
                for row in store.classification_work_list()}
        service = TeachingService(store, learned_rules_path=learned)
        preview = service.preview(work["native-C"].work_id, "finance")
        completed = service.confirm(work["native-C"].work_id, "finance",
                                    confirm_fingerprint=preview.fingerprint, as_of=NOW)
        store.defer_classification_work(work["native-B"].work_id, occurred_at=NOW)
        preview = service.preview(work["native-A"].work_id, "finance")
        store.save_configuration(service._config())
        pending = new_object_id("TEACH")
        store.create_teaching_intent(teaching_id=pending, work_id=preview.work_id,
            item_id=preview.item_id, observation_run_id=preview.observation_run_id,
            observation_message_id=preview.metadata.message_id,
            category_permanent_id=preview.category_permanent_id,
            candidate_fingerprint=preview.candidate.fingerprint,
            preview_fingerprint=preview.fingerprint, config_before=preview.candidate.config_fingerprint,
            rule_id=preview.candidate.rule.id, rule_version=preview.candidate.rule.version, occurred_at=NOW)
        base = load_config(default_config_directory()).model_copy(update={"settings": settings})
        monkeypatch.setattr("dam.cli.load_config", lambda *_args, **_kwargs: base)
        # No query can use any of these capabilities, including imported aliases.
        for module, names in {
            "dam.auth": ("authenticate", "build_gmail_service"),
            "dam.gmail": ("read_authenticated_profile", "read_inbox", "read_message"),
            "dam.durable_gmail": ("authenticate", "build_gmail_service", "read_authenticated_profile", "read_inbox"),
            "dam.scan": ("authenticate", "build_gmail_service", "read_inbox"),
            "dam.review": ("authenticate", "build_gmail_service", "read_message"),
        }.items():
            for name in names:
                monkeypatch.setattr(f"{module}.{name}", forbidden)
        monkeypatch.setattr("dam.source_binding.SourceBindingService.bind_gmail_profile", forbidden)
        yield SimpleNamespace(store=store, settings=settings, learned=learned, work=work,
            completed=completed.teaching_id, pending=pending, result=result, service=service)


def invoke(ctx, kind, target=None, sink=None):
    return query_teaching(kind, settings=ctx.settings, target_id=target,
                          learned_rules_path=ctx.learned, present=sink or (lambda view: None))


def cli(ctx, *args):
    return main(["teach", "--learned-rules-file", str(ctx.learned), *args])


def test_list_exact_output_order_and_disclosure(context, capsys):
    ctx = context
    expected = "".join(f"{row.work_id}  {row.state}  {len(row.members)} item(s)\n"
                       for row in ctx.store.classification_work_list() if row.state != "resolved")
    expected += f"Teaching {ctx.pending}  intent  resume: dam teach resume {ctx.pending}\n"
    assert cli(ctx, "list") == 0
    captured = capsys.readouterr()
    assert captured.out == expected and captured.err == ""
    assert "deferred" in captured.out and "pending" in captured.out
    assert ctx.work["native-C"].work_id not in captured.out
    assert "private-" not in captured.out and "SUBJECT-" not in captured.out


def test_empty_listing(tmp_path, monkeypatch, capsys):
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    base = load_config(default_config_directory()).model_copy(update={"settings": settings})
    monkeypatch.setattr("dam.cli.load_config", lambda *_: base)
    assert main(["teach", "list"]) == 0
    assert capsys.readouterr() == ("", "")


def test_show_exact_output_and_selected_representative(context, capsys):
    ctx = context
    work = ctx.work["native-A"]
    categories = ", ".join(sorted(category.key for category in ctx.service.categories()))
    assert cli(ctx, "show", work.work_id) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        f"Work: {work.work_id} (pending); ITEM: {work.representative_item_id}\n"
        f"Stored observation: {ctx.result.run_id} at {NOW.isoformat()}\n"
        "From: private-A@example.invalid\nSubject: SUBJECT-A-sentinel\n"
        f"Active categories: {categories}\n"
        "Stored evidence is historical; no Gmail freshness or action authority is claimed.\n")
    assert captured.err == ""
    assert "private-B" not in captured.out and "SUBJECT-C" not in captured.out


@pytest.mark.parametrize("which", ["pending", "completed"])
def test_status_exact_counts_and_success(context, capsys, which):
    ctx = context
    target = getattr(ctx, which)
    row = ctx.store.teaching_operation(target)
    evaluated, resolved, unresolved = ctx.store.teaching_evaluation_counts(target)
    assert cli(ctx, "status", target) == 0
    out, err = capsys.readouterr()
    assert out == (f"Teaching: {target}; status: {row['status']}; reevaluated: {evaluated}; "
                   f"resolved: {resolved}; still unresolved: {unresolved}; "
                   f"configuration: {row['config_after'] or '<pending>'}\n")
    assert not err and "private-" not in out and "SUBJECT-" not in out


@pytest.mark.parametrize("case", ["unknown", "resolved", "missing_observation", "unknown_teach", "config"])
def test_expected_failures_are_safe(context, monkeypatch, capsys, case):
    ctx = context
    args = ("show", ctx.work["native-A"].work_id)
    if case == "unknown":
        args = ("show", "CWQ-unknown-private-sentinel")
    elif case == "resolved":
        args = ("show", ctx.work["native-C"].work_id)
    elif case == "missing_observation":
        monkeypatch.setattr(Storage, "latest_email_observation", lambda *_: None)
    elif case == "unknown_teach":
        args = ("status", "TEACH-unknown-private-sentinel")
    else:
        def fail(*args, **kwargs):
            raise ValueError("configuration-private-sentinel")
        monkeypatch.setattr(TeachingService, "_config", fail)
    assert cli(ctx, *args) == 2
    assert capsys.readouterr() == ("", SAFE_FAILURE)


def test_initial_config_failure_uses_existing_cli_semantics(context, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("private-configuration")
    monkeypatch.setattr("dam.cli.load_config", fail)
    assert cli(context, "list") == 2
    assert capsys.readouterr() == ("", SAFE_FAILURE)


@pytest.mark.parametrize("value,present,expected", [
    (None, False, "<absent>"), (None, True, "<empty>"), ("", True, "<empty>"),
    ("text https://secret.invalid/token more", True, "text [URL redacted] more"),
    ("a\x1bb\nc\t", True, "a�b�c�"), ("x" * 301, True, "x" * 300 + "…"),
])
def test_show_safe_display_projection(context, monkeypatch, value, present, expected):
    # Inject only at the existing inspection primitive; neither raw value nor
    # broad observation is handed to the renderer.
    work, item, observation = context.service.inspect(context.work["native-A"].work_id)
    metadata = SimpleNamespace(sender=value, subject=value,
                               model_fields_set={"sender", "subject"} if present else set())
    observation = SimpleNamespace(run_id=observation.run_id, observed_at=observation.observed_at,
                                  metadata=metadata)
    monkeypatch.setattr(TeachingService, "inspect", lambda *_: (work, item, observation))
    views = []
    assert invoke(context, TeachingQuery.SHOW, work.work_id, views.append).disposition is QueryDisposition.COMPLETED
    assert views[0].sender_display == views[0].subject_display == expected


def leaves(value):
    if is_dataclass(value):
        for field in fields(value):
            yield from leaves(getattr(value, field.name))
    elif type(value) is tuple:
        for entry in value:
            yield from leaves(entry)
    else:
        yield value


def test_operations_without_terminal_and_minimal_control(context, monkeypatch):
    views = []
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        for kind, target in [(TeachingQuery.LIST, None),
                             (TeachingQuery.SHOW, context.work["native-A"].work_id),
                             (TeachingQuery.STATUS, context.pending)]:
            result = invoke(context, kind, target, views.append)
            assert tuple(leaves(result)) == (QueryDisposition.COMPLETED,)
            assert [field.name for field in fields(result)] == ["disposition"]
    assert len(views) == 3
    for view in views:
        assert all(type(value) in (str, int, type(None)) for value in leaves(view))


@pytest.mark.parametrize("exception,disposition,code,text", [
    (StorageError, QueryDisposition.REJECTED, 2, SAFE_FAILURE),
    (RuntimeError, QueryDisposition.FAILED, 1, "DAM teach failed internally.\n"),
])
def test_exception_privacy(context, monkeypatch, capsys, caplog, exception, disposition, code, text):
    def fail(*args, **kwargs):
        raise exception("PRIVATE-EXCEPTION-SENTINEL")
    monkeypatch.setattr(Storage, "classification_work_list", fail)
    views = []
    result = invoke(context, TeachingQuery.LIST, sink=views.append)
    assert result == QueryControlResult(disposition) and not views
    assert "PRIVATE" not in repr(result)
    assert cli(context, "list") == code
    assert capsys.readouterr() == ("", text)
    assert "PRIVATE" not in caplog.text


def test_queries_do_not_mutate_current_storage_or_learning(context):
    before = tuple(context.store._connection.iterdump())
    learned = context.learned.read_bytes()
    for kind, target in [(TeachingQuery.LIST, None),
                         (TeachingQuery.SHOW, context.work["native-A"].work_id),
                         (TeachingQuery.STATUS, context.pending),
                         (TeachingQuery.STATUS, context.completed)]:
        assert invoke(context, kind, target).disposition is QueryDisposition.COMPLETED
    assert tuple(context.store._connection.iterdump()) == before
    assert context.learned.read_bytes() == learned


def test_writer_stateless_across_items_operations_and_scopes(context):
    # Only this synthetic test captures output; the shared writer retains none.
    outputs = []
    references = []
    def sink(view):
        references.append(weakref.ref(view))
        outputs.append(render_teaching(view))
    for _scope in ("scope-one", "scope-two"):
        for kind, target in [(TeachingQuery.SHOW, context.work["native-A"].work_id),
                             (TeachingQuery.LIST, None),
                             (TeachingQuery.STATUS, context.pending),
                             (TeachingQuery.SHOW, context.work["native-B"].work_id)]:
            assert invoke(context, kind, target, sink).disposition is QueryDisposition.COMPLETED
    gc.collect()
    assert all(reference() is None for reference in references)
    for index, output in enumerate(outputs):
        assert ("SUBJECT-A-sentinel" in output) == (index % 4 == 0)
        assert ("SUBJECT-B-sentinel" in output) == (index % 4 == 3)
        assert "SUBJECT-C-sentinel" not in output
    assert render_teaching.__dict__ == {} and render_teaching.__closure__ is None


def test_writer_rejects_unknown_objects_without_inspecting_them():
    class Hostile:
        def __getattribute__(self, name):
            raise AssertionError("Object introspected")
        def __str__(self):
            raise AssertionError("Object rendered")
    for value in (Hostile(), {}, object()):
        with pytest.raises(TypeError, match="Unsupported teaching presentation contract"):
            render_teaching(value)


def test_contracts_are_frozen_and_nested_values_immutable():
    row = WorkListingRow("CWQ", "pending", 1)
    incomplete = IncompleteTeachingRow("TEACH", "intent")
    listing = TeachingQueueListing((row,), (incomplete,))
    detail = TeachingWorkDetail("CWQ", "pending", "ITEM", "run", "time", "from", "subject", ("a",))
    status = TeachingStatus("TEACH", "intent", 0, 0, 0, None)
    for value in (row, incomplete, listing, detail, status, QueryControlResult(QueryDisposition.COMPLETED)):
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "replacement")
        assert not hasattr(value, "__dict__")
    with pytest.raises(TypeError):
        listing.work[0] = row
    with pytest.raises(TypeError):
        TeachingQueueListing([row], ())
    with pytest.raises(TypeError):
        TeachingWorkDetail("CWQ", "pending", "ITEM", "run", "time", "from", "subject", ["a"])
    assert "subject" not in repr(detail)


def test_writer_runs_after_storage_closes(context, monkeypatch):
    closed = []
    close = Storage.close
    def closing(store):
        closed.append(True)
        close(store)
    monkeypatch.setattr(Storage, "close", closing)
    def sink(view):
        assert closed == [True]
        assert render_teaching(view)
    assert invoke(context, TeachingQuery.LIST, sink=sink).disposition is QueryDisposition.COMPLETED


def test_writer_failure_does_not_retain_payload_or_exception(context):
    refs = []
    def sink(view):
        refs.append(weakref.ref(view))
        raise RuntimeError("PRIVATE-WRITER-FAILURE")
    result = invoke(context, TeachingQuery.SHOW, context.work["native-A"].work_id, sink)
    gc.collect()
    assert result.disposition is QueryDisposition.FAILED
    assert all(ref() is None for ref in refs)
    assert "PRIVATE" not in repr(result)


def test_contracts_reject_domain_objects_and_mutable_leaves():
    forbidden_payload = {"subject": "PRIVATE"}
    constructors = (
        lambda: WorkListingRow(forbidden_payload, "pending", 1),
        lambda: IncompleteTeachingRow("TEACH", forbidden_payload),
        lambda: TeachingWorkDetail("CWQ", "pending", "ITEM", "run", "time", forbidden_payload, "subject", ()),
        lambda: TeachingStatus("TEACH", "intent", 0, 0, 0, forbidden_payload),
        lambda: QueryControlResult(forbidden_payload),
    )
    for construct in constructors:
        with pytest.raises(TypeError) as error:
            construct()
        assert "PRIVATE" not in str(error.value)


def test_writer_has_no_repository_or_source_dependencies(monkeypatch):
    import ast
    import inspect
    import dam.teaching_presentation as presentation
    monkeypatch.setattr(Storage, "open", forbidden)
    monkeypatch.setattr("dam.auth.authenticate", forbidden)
    monkeypatch.setattr("dam.config.load_config", forbidden)
    assert render_teaching(TeachingQueueListing((), ())) == ""
    assert "<pending>" in render_teaching(TeachingStatus("TEACH", "intent", 0, 0, 0, None))
    tree = ast.parse(inspect.getsource(presentation))
    assert {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} == {"dataclasses", "typing"}


def test_unexpected_cli_setup_failure_is_sanitized(context, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-SETUP-DEFECT")
    monkeypatch.setattr("dam.cli.load_config", fail)
    assert cli(context, "list") == 1
    assert capsys.readouterr() == ("", "DAM teach failed internally.\n")


def test_interruption_closes_storage_without_retaining_view(context, monkeypatch):
    closed = []
    references = []
    close = Storage.close
    def closing(store):
        closed.append(True)
        close(store)
    monkeypatch.setattr(Storage, "close", closing)
    def sink(view):
        references.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        invoke(context, TeachingQuery.SHOW, context.work["native-A"].work_id, sink)
    gc.collect()
    assert closed == [True]
    assert all(ref() is None for ref in references)


def test_invalid_request_never_opens_storage(context, monkeypatch):
    monkeypatch.setattr(Storage, "open", forbidden)
    assert invoke(context, "list").disposition is QueryDisposition.REJECTED
    assert invoke(context, TeachingQuery.SHOW).disposition is QueryDisposition.REJECTED
    assert invoke(context, TeachingQuery.LIST, "unexpected").disposition is QueryDisposition.REJECTED
