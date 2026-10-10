"""Scan boundary: synthetic metadata, fake Gmail/OAuth, and temporary SQLite."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import gc
import shutil
from types import SimpleNamespace
import weakref

import pytest
import yaml

from dam.audit import render_preview
from dam.category_presentation import CategoryDetail, render_categories
from dam.cli import main
from dam.config import ConfigurationError, load_config
from dam.durable_gmail import run_durable_gmail_scan
from dam.learning import LearningError, propose_classification_rule, save_classification_rule
from dam.scan import (
    MAX_INITIAL_GMAIL_LIMIT, MAX_SCAN_LIMIT, ScanInputError,
    default_config_directory, load_synthetic_messages, run_gmail_scan, run_synthetic_scan,
)
from dam.scan_operations import ScanControlResult, ScanRequest, scan
from dam.scan_presentation import (
    ScanDiagnostic, ScanDiagnosticCode, ScanDisplay, render_scan,
)
from dam.storage import SCHEMA_VERSION, Storage, StorageError
from dam.teaching_presentation import TeachingStatus, render_teaching
from dam.teaching_queries import QueryDisposition
from test_durable_gmail import FakeGmail, NOW, message
from test_review import Backend, private, forbid_real_oauth_and_network

OUTPUT_ERROR = "DAM scan outcome could not be displayed; inspect any durable intake before retrying.\n"
STOP_ERROR = "DAM durable Gmail intake stopped after a local/read failure; earlier committed items remain recorded.\n"
GMAIL_LIMIT = "DAM Gmail mode limit must be from 1 to 10; no Gmail access attempted.\n"
RECORD_ERROR = "DAM local recording requires explicit --gmail; no source access attempted.\n"
SYNTHETIC_ERROR = "DAM scan failed: invalid configuration or synthetic input.\n"
GMAIL_ERROR = "DAM Gmail read-only scan failed; no mailbox actions executed.\n"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected source, repository, or Operation capability")


def leaves(value):
    if is_dataclass(value):
        for field in fields(value):
            yield from leaves(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            yield from leaves(item)
    else:
        yield value


def contracts(value):
    if is_dataclass(value):
        yield value
        for field in fields(value):
            yield from contracts(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            yield from contracts(item)


def old_output(kind, result):
    """Compatibility oracle: existing preview formatter and former CLI fields."""
    output = ""
    if kind == "gmail":
        output = "DAM real Gmail read-only Inbox scan; no mailbox actions executed.\n"
    elif kind == "durable":
        output = "DAM real Gmail read-only Inbox scan with explicit private local recording; no mailbox actions executed.\n"
        output += (f"Run: {result.run_id}; verified source: {result.source_instance_id}; "
                   f"durably admitted: {len(result.committed_item_ids)}; status: {result.status}.\n")
    if result.preview is not None:
        output += render_preview(result.preview)
    read = result.read_result if kind != "synthetic" else None
    if read is not None:
        outside = f"outside current Inbox: {len(result.out_of_scope_ids)}; " if kind == "durable" else ""
        coverage = ("partial" if kind == "durable" and (result.status != "completed" or result.out_of_scope_ids)
                    else read.coverage)
        output += (f"Gmail listing: {read.listed_count} IDs; normalized: {read.observed_count}; "
                   f"read failures: {len(read.failures)}; {outside}coverage: {coverage}.\n")
        output += "".join(f"Uninspected message {failure.message_id}: {failure.reason}; Review required.\n"
                          for failure in read.failures)
    return output


@pytest.fixture
def ctx(private, tmp_path, monkeypatch):
    paths, learned = private
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("categories.yaml", "rules.yaml", "settings.yaml"):
        shutil.copyfile(default_config_directory() / name, directory / name)
    document = yaml.safe_load((directory / "settings.yaml").read_text())
    database = tmp_path / "state" / "dam.db"
    # Parse temporary config normally, then inject validated test Settings.
    # config.load_config treats the sandbox's empty /tmp/.git mount marker as
    # a checkout; Storage already distinguishes it. Do not change that loader
    # in this boundary refactor (the existing durable tests inject Settings too).
    from dam.models import Settings
    document["state"]["database_path"] = str(database)
    settings = Settings.model_validate(document)
    def isolated_config(*args, **kwargs):
        base = load_config(*args, **kwargs)
        return base.model_copy(update={"settings": settings})
    monkeypatch.setattr("dam.scan.load_config", isolated_config)
    monkeypatch.setattr("dam.durable_gmail.load_config", isolated_config)
    gmail = FakeGmail([
        message("msg-A", sender="PRIVATE-A@example.invalid", subject="SUBJECT-A-SENTINEL"),
        message("msg-B", sender="PRIVATE-B@example.invalid", subject="SUBJECT-B-SENTINEL"),
        message("msg-C", sender="receipts@example.invalid", subject="Synthetic receipt"),
    ])
    backend = Backend()
    calls, expected, refs, counts = [], [], [], {"synthetic": 0, "gmail": 0, "durable": 0}
    def capture(kind, result):
        expected.append(old_output(kind, result))
        refs.append(weakref.ref(result))
        return result
    def synthetic(**kwargs):
        calls.append(("synthetic", kwargs))
        counts["synthetic"] += 1
        return capture("synthetic", run_synthetic_scan(config_directory=directory,
            run_id=f"synthetic-run-{counts['synthetic']}", as_of=NOW, **kwargs))
    def ordinary(**kwargs):
        calls.append(("gmail", kwargs))
        counts["gmail"] += 1
        return capture("gmail", run_gmail_scan(config_directory=directory, paths=paths, backend=backend,
            service_factory=lambda *a, **k: gmail, run_id=f"ordinary-run-{counts['gmail']}", as_of=NOW, **kwargs))
    def durable(**kwargs):
        calls.append(("durable", kwargs))
        counts["durable"] += 1
        return capture("durable", run_durable_gmail_scan(config_directory=directory, paths=paths, backend=backend,
            service_factory=lambda *a, **k: gmail, run_id=f"durable-run-{counts['durable']}", as_of=NOW, **kwargs))
    monkeypatch.setattr("dam.scan_operations.run_synthetic_scan", synthetic)
    monkeypatch.setattr("dam.scan_operations.run_gmail_scan", ordinary)
    monkeypatch.setattr("dam.scan_operations.run_durable_gmail_scan", durable)
    catalog = learned.with_name("categories.yaml")
    monkeypatch.setattr("dam.scan_operations.default_catalog_path", lambda: catalog)
    monkeypatch.setattr("dam.scan_operations.default_learned_rules_path", lambda: learned)
    for target in ("dam.storage.Storage.record_approval", "dam.teaching.TeachingService.confirm",
                   "dam.learn_operations.learn", "dam.review_operations.review_message"):
        monkeypatch.setattr(target, forbidden)
    return SimpleNamespace(paths=paths, learned=learned, catalog=catalog, directory=directory, settings=settings,
        database=database, gmail=gmail, backend=backend, calls=calls, expected=expected, refs=refs, counts=counts)


def invoke(request=ScanRequest(), sink=None):
    views = []
    return scan(request, present=sink or views.append), views


def arguments(request):
    result = ["scan"]
    for flag, enabled in (("--gmail", request.gmail), ("--record-locally", request.record_locally),
                          ("--dry-run", request.dry_run), ("--use-learned-rules", request.use_learned_rules)):
        if enabled:
            result.append(flag)
    if request.limit is not None:
        result.extend(("--limit", str(request.limit)))
    if request.category_catalog_file is not None:
        result.extend(("--category-catalog-file", request.category_catalog_file))
    return result


@pytest.mark.parametrize("kind", ["synthetic", "gmail", "durable"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_three_paths_exact_output_defaults_and_nonexecution(ctx, capsys, kind, dry_run):
    request = ScanRequest(gmail=kind != "synthetic", record_locally=kind == "durable", dry_run=dry_run)
    result, views = invoke(request)
    assert result == ScanControlResult(QueryDisposition.COMPLETED)
    assert render_scan(views[0]) == ctx.expected[-1]
    assert main(arguments(request)) == 0
    assert capsys.readouterr() == (ctx.expected[-1], "")
    assert [call[0] for call in ctx.calls] == [kind, kind]
    assert "Executed Gmail actions: 0" in render_scan(views[0])
    assert views[0].preview.limit == (100 if kind == "synthetic" else 10)
    assert ctx.database.exists() is (kind == "durable")
    assert len(ctx.gmail.get_calls) == (0 if kind == "synthetic" else 6)
    assert len(ctx.gmail.profile_calls) == (2 if kind == "durable" else 0)
    assert ctx.backend.decode_calls == (0 if kind == "synthetic" else 2)
    for call in ctx.gmail.get_calls:
        assert call["userId"] == "me" and call["format"] == "metadata"
        assert call["metadataHeaders"] == ["From", "Subject"]
        assert all(word not in call["fields"] for word in ("body", "raw", "snippet", "attachment"))
    for call in ctx.gmail.list_calls:
        assert call["labelIds"] == ["INBOX"] and call["includeSpamTrash"] is False
    assert all(not hasattr(ctx.gmail, name) for name in ("modify", "batchModify", "trash", "delete", "send", "attachments"))
    for private in ("PRIVATE-A@example.invalid", "SUBJECT-A-SENTINEL", "synthetic forbidden body", "synthetic-token"):
        assert private not in render_scan(views[0]) + repr(result)


@pytest.mark.parametrize("kind", ["synthetic", "gmail"])
def test_non_durable_paths_never_open_sqlite(ctx, monkeypatch, kind):
    monkeypatch.setattr(Storage, "open", forbidden)
    if kind == "synthetic":
        monkeypatch.setattr("dam.scan.authenticate", forbidden)
    assert invoke(ScanRequest(gmail=kind == "gmail"))[0].disposition is QueryDisposition.COMPLETED
    assert not ctx.database.exists()


@pytest.mark.parametrize("limit", [1, 10_000])
def test_synthetic_limit_preserved(ctx, limit):
    result, views = invoke(ScanRequest(limit=limit))
    assert result.disposition is QueryDisposition.COMPLETED
    assert views[0].preview.limit == limit
    assert ctx.backend.decode_calls == 0 and ctx.gmail.list_calls == []
    assert MAX_SCAN_LIMIT == 10_000


@pytest.mark.parametrize("limit", [0, -1, 10_001])
def test_invalid_direct_synthetic_limits_reject_without_execution(ctx, limit):
    result, views = invoke(ScanRequest(limit=limit))
    assert result.disposition is QueryDisposition.REJECTED and render_scan(views[0]) == SYNTHETIC_ERROR
    assert ctx.calls == [] and not ctx.database.exists()


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("limit", [0, 11])
def test_gmail_limit_before_paths_authentication_or_source(ctx, monkeypatch, capsys, durable, limit):
    monkeypatch.setattr("dam.scan_operations.default_catalog_path", forbidden)
    monkeypatch.setattr("dam.scan_operations.default_learned_rules_path", forbidden)
    result, views = invoke(ScanRequest(gmail=True, limit=limit, record_locally=durable, use_learned_rules=True))
    assert result == ScanControlResult(QueryDisposition.REJECTED)
    assert render_scan(views[0]) == GMAIL_LIMIT
    if limit == 11:
        assert main(arguments(ScanRequest(gmail=True, limit=limit, record_locally=durable))) == 2
        assert capsys.readouterr() == ("", GMAIL_LIMIT)
    assert ctx.calls == [] and ctx.backend.decode_calls == 0
    assert ctx.gmail.list_calls == ctx.gmail.get_calls == ctx.gmail.profile_calls == []
    assert not ctx.database.exists() and MAX_INITIAL_GMAIL_LIMIT == 10


def test_local_recording_requires_explicit_gmail(ctx, capsys):
    result, views = invoke(ScanRequest(record_locally=True))
    assert result == ScanControlResult(QueryDisposition.REJECTED)
    assert render_scan(views[0]) == RECORD_ERROR
    assert main(["scan", "--record-locally"]) == 2
    assert capsys.readouterr() == ("", RECORD_ERROR)
    assert ctx.calls == [] and not ctx.database.exists()


@pytest.mark.parametrize("durable", [False, True])
def test_partial_get_failures_complete_invocation_and_keep_coverage(ctx, capsys, durable):
    ctx.gmail.fail_get = {"msg-B"}
    request = ScanRequest(gmail=True, record_locally=durable)
    result, views = invoke(request)
    view = views[0]
    assert result.disposition is QueryDisposition.COMPLETED
    assert view.coverage.normalized == 2 and view.coverage.coverage == "read_failures"
    assert view.coverage.failures[0].message_id == "msg-B" and view.coverage.failures[0].reason == "api_error"
    assert render_scan(view) == ctx.expected[-1]
    assert main(arguments(request)) == 0
    assert capsys.readouterr() == (ctx.expected[-1], "")
    if durable:
        with Storage.open(ctx.settings) as store:
            assert len(store._rows("SELECT * FROM items")) == 2
            assert store.scan("durable-run-1").finish.status == "completed"
            assert store.item_by_native_identity(view.intake.source_id, "msg-B") is None


def test_durable_identity_evaluations_queue_provenance_and_idempotency(ctx):
    result, views = invoke(ScanRequest(gmail=True, record_locally=True))
    assert result.disposition is QueryDisposition.COMPLETED
    source_id = views[0].intake.source_id
    with Storage.open(ctx.settings) as store:
        source = store.source_instance(source_id)
        assert source.provider == "gmail"
        items = {message_id: store.item_by_native_identity(source_id, message_id) for message_id in ("msg-A", "msg-B", "msg-C")}
        assert all(item is not None for item in items.values())
        first_evals = {}
        for message_id, item in items.items():
            observations = store.email_observations_for_item(item.item_id)
            assert len(observations) == 1 and observations[0].metadata.message_id == message_id
            current = store.current_evaluation(item.item_id)
            first_evals[message_id] = current
            assert current["item_id"] == item.item_id and current["predecessor_id"] is None
            assert current["observation_run_id"] == "durable-run-1"
            assert current["config_fingerprint"] == store.scan("durable-run-1").start.config_fingerprint
            assert store.configuration(current["config_fingerprint"]) is not None
            assert store.proposals("durable-run-1", message_id)[0].executable is False
        pending = store.active_classification_work_for_item(items["msg-A"].item_id)
        store.defer_classification_work(pending.work_id, occurred_at=NOW)
        assert store.active_classification_work_for_item(items["msg-C"].item_id) is None
        assert store.approvals(source_id) == ()
        before_schema = store.schema_info()
    repeated, _ = invoke(ScanRequest(gmail=True, record_locally=True))
    assert repeated.disposition is QueryDisposition.COMPLETED
    with Storage.open(ctx.settings) as store:
        assert store.schema_info() == before_schema
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        assert len(store._rows("SELECT * FROM source_instances")) == 1
        assert len(store._rows("SELECT * FROM items")) == 3
        for message_id, item in items.items():
            assert store.item_by_native_identity(source_id, message_id).item_id == item.item_id
            assert len(store.email_observations_for_item(item.item_id)) == 2
            chain = store.evaluation_history(item.item_id)
            assert len(chain) == 2 and chain[0] == first_evals[message_id]
            assert chain[1]["predecessor_id"] == chain[0]["evaluation_id"]
        assert store.classification_work(pending.work_id).state == "deferred"
        assert len(store.classification_work_list()) == 2


@pytest.mark.parametrize("failure", ["local", "listing"])
def test_durable_stop_keeps_commits_output_and_exit_two(ctx, monkeypatch, capsys, failure):
    if failure == "listing":
        ctx.gmail.list_error = RuntimeError("PRIVATE-LISTING")
    else:
        record = Storage.record_verified_gmail_intake
        def fail(store, run_id, message, *args, **kwargs):
            if message.message_id == "msg-B":
                raise StorageError("PRIVATE-LOCAL-FAILURE")
            return record(store, run_id, message, *args, **kwargs)
        monkeypatch.setattr(Storage, "record_verified_gmail_intake", fail)
    request = ScanRequest(gmail=True, record_locally=True)
    result, views = invoke(request)
    assert result == ScanControlResult(QueryDisposition.INCOMPLETE)
    assert render_scan(views[0]) == ctx.expected[-1]
    assert render_scan(views[1]) == STOP_ERROR
    assert views[0].intake.committed_count == (1 if failure == "local" else 0)
    assert main(arguments(request)) == 2
    assert capsys.readouterr() == (ctx.expected[-1], STOP_ERROR)
    with Storage.open(ctx.settings) as store:
        assert len(store._rows("SELECT * FROM items")) == (1 if failure == "local" else 0)
        assert store.scan("durable-run-1").finish.status == "failed"
        assert store.scan("durable-run-2").finish.status == "failed"
    assert "rolled back" not in STOP_ERROR and "PRIVATE" not in repr(result) + repr(views)


def test_durable_inbox_list_get_race_is_not_admitted(ctx):
    ctx.gmail.records[0]["labelIds"] = ["CATEGORY_UPDATES"]
    result, views = invoke(ScanRequest(gmail=True, record_locally=True))
    assert result.disposition is QueryDisposition.COMPLETED
    assert views[0].intake.committed_count == 2 and views[0].coverage.outside_inbox == 1
    assert views[0].coverage.coverage == "partial"
    with Storage.open(ctx.settings) as store:
        assert store.item_by_native_identity(views[0].intake.source_id, "msg-A") is None


@pytest.mark.parametrize("kind", ["synthetic", "gmail", "durable"])
def test_learned_rules_are_opt_in_and_category_override_preserved(ctx, kind):
    source = load_synthetic_messages()[-1]
    config = load_config(ctx.directory)
    proposal = propose_classification_rule(source, "promotions", config, as_of=NOW)
    save_classification_rule(proposal, config, ctx.learned, expected_fingerprint=proposal.fingerprint)
    request = ScanRequest(gmail=kind != "synthetic", record_locally=kind == "durable",
                          category_catalog_file=str(ctx.catalog))
    _, baseline = invoke(request)
    assert "learned_rules_path" not in ctx.calls[-1][1] or ctx.calls[-1][1]["learned_rules_path"] is None
    _, taught = invoke(replace(request, use_learned_rules=True))
    assert ctx.calls[-1][1]["learned_rules_path"] == ctx.learned
    assert ctx.calls[-1][1]["category_catalog_path"] == ctx.catalog
    if kind == "synthetic":
        assert baseline[0].preview.messages[-1].categories == ()
        assert taught[0].preview.messages[-1].categories == ("promotions",)
    assert render_scan(taught[0]) == ctx.expected[-1]


@pytest.mark.parametrize("kind", ["synthetic", "gmail", "durable"])
def test_writer_failure_never_reexecutes_or_undoes_intake(ctx, monkeypatch, capsys, kind):
    request = ScanRequest(gmail=kind != "synthetic", record_locally=kind == "durable")
    refs = []
    def sink(view):
        refs.append(weakref.ref(view))
        if type(view) is ScanDisplay:
            raise RuntimeError("PRIVATE-WRITER")
        assert render_scan(view) == OUTPUT_ERROR
    result, _ = invoke(request, sink)
    assert result == ScanControlResult(QueryDisposition.COMPLETED, presentation_failed=True)
    assert len(ctx.calls) == 1
    assert ctx.backend.decode_calls == (0 if kind == "synthetic" else 1)
    if kind == "durable":
        with Storage.open(ctx.settings) as store:
            assert store.scan("durable-run-1").finish.status == "completed"
            assert len(store._rows("SELECT * FROM items")) == 3
    from dam.cli import _write_scan
    def terminal(view):
        if type(view) is ScanDisplay:
            raise ValueError("PRIVATE-WRITER")
        _write_scan(view)
    monkeypatch.setattr("dam.cli._write_scan", terminal)
    assert main(arguments(request)) == 1
    assert capsys.readouterr() == ("", OUTPUT_ERROR)
    assert len(ctx.calls) == 2
    gc.collect()
    assert all(ref() is None for ref in refs + ctx.refs)


def test_incomplete_intake_and_failed_presentation_are_independent(ctx):
    ctx.gmail.list_error = RuntimeError("PRIVATE-LIST")
    def fail(view):
        raise RuntimeError("PRIVATE-DISPLAY")
    result, _ = invoke(ScanRequest(gmail=True, record_locally=True), fail)
    assert result == ScanControlResult(QueryDisposition.INCOMPLETE, presentation_failed=True)
    assert len(ctx.calls) == ctx.backend.decode_calls == 1


def test_projection_failure_and_interrupt_preserve_durable_work(ctx, monkeypatch):
    def fail(*args):
        raise ValueError("PRIVATE-PROJECTION")
    with monkeypatch.context() as patch:
        patch.setattr("dam.scan_operations._project_preview", fail)
        result, _ = invoke(ScanRequest(gmail=True, record_locally=True))
    assert result == ScanControlResult(QueryDisposition.COMPLETED, True)
    refs = []
    def interrupt(view):
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        invoke(ScanRequest(gmail=True, record_locally=True), interrupt)
    with Storage.open(ctx.settings) as store:
        assert len(store._rows("SELECT * FROM items")) == 3
        assert store.scan("durable-run-1").finish.status == store.scan("durable-run-2").finish.status == "completed"
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_control_input_and_all_display_contracts_are_minimal_and_immutable(ctx, monkeypatch):
    request = ScanRequest(gmail=True, record_locally=True)
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result, views = invoke(request)
    assert [field.name for field in fields(result)] == ["disposition", "presentation_failed"]
    assert list(leaves(result)) == [QueryDisposition.COMPLETED, False]
    assert all(type(value) in (str, int, float, bool, type(None)) for value in leaves(views[0]))
    assert "msg-A" not in repr(result)
    assert [field.name for field in fields(views[0])] == ["source", "preview", "coverage", "intake"]
    assert [field.name for field in fields(views[0].intake)] == ["run_id", "source_id", "committed_count", "status"]
    assert [field.name for field in fields(views[0].preview)] == [
        "run_id", "account_id", "fingerprint", "scope_labels", "limit", "coverage", "aggregate", "messages"]
    for value in (request, result, *contracts(views[0]), ScanDiagnostic(ScanDiagnosticCode.GMAIL_SAFE)):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        for field in fields(value):
            with pytest.raises(TypeError):
                replace(value, **{field.name: {"PRIVATE": "object"}})
    for value in (True, "1", object()):
        with pytest.raises(TypeError):
            ScanRequest(limit=value)
    monkeypatch.setattr("dam.scan_operations.default_catalog_path", forbidden)
    assert scan({"PRIVATE": "namespace"}, present=lambda _: None).disposition is QueryDisposition.REJECTED


def test_writer_no_source_repository_or_domain_access(ctx, monkeypatch):
    _, views = invoke(ScanRequest(gmail=True, record_locally=True))
    for target in ("dam.scan_operations.run_synthetic_scan", "dam.scan_operations.run_gmail_scan",
                   "dam.scan_operations.run_durable_gmail_scan", "dam.config.load_config",
                   "dam.storage.Storage.open", "dam.gmail.read_inbox", "dam.scan.classify", "dam.scan.propose_action"):
        monkeypatch.setattr(target, forbidden)
    assert render_scan(views[0]) == ctx.expected[-1]
    for value in (SimpleNamespace(preview="PRIVATE"), ctx.gmail, load_config(ctx.directory)):
        with pytest.raises(TypeError, match="Unsupported"):
            render_scan(value)
    assert render_scan.__dict__ == {} and render_scan.__closure__ is None


@pytest.mark.parametrize("kind", ["synthetic", "gmail", "durable"])
@pytest.mark.parametrize("error,disposition", [(ConfigurationError, QueryDisposition.REJECTED), (RuntimeError, QueryDisposition.FAILED)])
def test_sanitized_source_errors_and_exit_compatibility(ctx, monkeypatch, capsys, caplog, kind, error, disposition):
    def fail(**kwargs):
        raise error("PRIVATE-SOURCE-CREDENTIAL-SENTINEL")
    monkeypatch.setattr("dam.scan_operations." + {"synthetic": "run_synthetic_scan", "gmail": "run_gmail_scan", "durable": "run_durable_gmail_scan"}[kind], fail)
    request = ScanRequest(gmail=kind != "synthetic", record_locally=kind == "durable")
    result, views = invoke(request)
    assert result == ScanControlResult(disposition)
    assert main(arguments(request)) == (1 if disposition is QueryDisposition.FAILED else 2)
    output, diagnostic = capsys.readouterr()
    assert output == "" and diagnostic == render_scan(views[0])
    assert "PRIVATE" not in repr(result) + repr(views) + diagnostic + caplog.text


@pytest.mark.parametrize("reason", ["Gmail mode requires Inbox-only scope", "PRIVATE-INVALID-INPUT"])
def test_gmail_input_errors_whitelist_safe_messages(ctx, monkeypatch, capsys, reason):
    def fail(**kwargs):
        raise ScanInputError(reason)
    monkeypatch.setattr("dam.scan_operations.run_gmail_scan", fail)
    result, views = invoke(ScanRequest(gmail=True))
    assert result.disposition is QueryDisposition.REJECTED
    safe = reason if reason.startswith("Gmail mode") else "Invalid Gmail scan input"
    assert render_scan(views[0]) == f"DAM Gmail scan rejected: {safe}; no mailbox actions executed.\n"
    assert main(["scan", "--gmail"]) == 2
    assert capsys.readouterr() == ("", render_scan(views[0]))


def test_cross_message_source_and_operation_presentation_release(ctx):
    refs, outputs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_scan(view))
    invoke(ScanRequest(gmail=True, limit=1), sink)
    ctx.gmail.records = [ctx.gmail.records[1]]
    invoke(ScanRequest(gmail=True, limit=1), sink)
    invoke(ScanRequest(limit=1), sink)
    assert "msg-A" in outputs[0] and "msg-A" not in outputs[1]
    assert "msg-B" not in outputs[0] and "msg-B" in outputs[1]
    assert "msg-A" not in outputs[2] and "msg-B" not in outputs[2]
    category = render_categories(CategoryDetail("CAT-example", "private", "CATEGORY-SENTINEL", None, "active"))
    teaching = render_teaching(TeachingStatus("TEACH-SENTINEL", "completed", 0, 0, 0, "CONFIG-SENTINEL"))
    assert "msg-A" not in category + teaching and "msg-B" not in category + teaching
    assert all("CATEGORY-SENTINEL" not in output and "TEACH-SENTINEL" not in output for output in outputs)
    gc.collect()
    assert all(ref() is None for ref in refs + ctx.refs)


def test_durable_storage_is_closed_before_delivery(ctx, monkeypatch):
    close = Storage.close
    closed = []
    def closing(store):
        close(store)
        closed.append(store)
    monkeypatch.setattr(Storage, "close", closing)
    def sink(view):
        assert len(closed) == 1
        import sqlite3
        with pytest.raises(sqlite3.ProgrammingError):
            closed[0]._connection.execute("SELECT 1")
    assert invoke(ScanRequest(gmail=True, record_locally=True), sink)[0].disposition is QueryDisposition.COMPLETED


@pytest.mark.parametrize("durable", [False, True])
def test_hard_ten_message_ceiling_and_pagination_coverage(ctx, durable):
    ctx.gmail.records = [message(f"bounded-{index}") for index in range(15)]
    listing = ctx.gmail.list
    def more(**kwargs):
        response = listing(**kwargs)
        response.value["nextPageToken"] = "synthetic-next-page"
        return response
    ctx.gmail.list = more
    result, views = invoke(ScanRequest(gmail=True, record_locally=durable))
    assert result.disposition is QueryDisposition.COMPLETED
    assert len(ctx.gmail.get_calls) == 10 and len(ctx.gmail.list_calls) == 1
    assert views[0].coverage.coverage == "limit_reached"
    assert views[0].preview.coverage == "partial" and views[0].preview.aggregate.observed == 10
    assert render_scan(views[0]) == ctx.expected[-1]
    if durable:
        with Storage.open(ctx.settings) as store:
            assert len(store._rows("SELECT * FROM items")) == 10
            assert store.scan("durable-run-1").finish.inventory.pagination_limited is True


def test_changed_verified_mailbox_keeps_source_item_identity_separate(ctx):
    _, first = invoke(ScanRequest(gmail=True, record_locally=True))
    first_source = first[0].intake.source_id
    ctx.gmail.address = "other-account@example.invalid"
    _, second = invoke(ScanRequest(gmail=True, record_locally=True))
    second_source = second[0].intake.source_id
    assert first_source != second_source
    assert "other-account@example.invalid" not in render_scan(second[0])
    with Storage.open(ctx.settings) as store:
        assert len(store._rows("SELECT * FROM source_instances")) == 2
        assert len(store._rows("SELECT * FROM items")) == 6
        assert store.item_by_native_identity(first_source, "msg-A").item_id != store.item_by_native_identity(second_source, "msg-A").item_id


@pytest.mark.parametrize("durable", [False, True])
def test_empty_gmail_mailbox_output_compatibility(ctx, durable):
    ctx.gmail.records = []
    result, views = invoke(ScanRequest(gmail=True, record_locally=durable))
    assert result == ScanControlResult(QueryDisposition.COMPLETED)
    assert views[0].preview.aggregate.observed == 0 and views[0].preview.messages == ()
    assert views[0].coverage.listed == views[0].coverage.normalized == 0
    assert render_scan(views[0]) == ctx.expected[-1]
    assert ctx.gmail.get_calls == []
