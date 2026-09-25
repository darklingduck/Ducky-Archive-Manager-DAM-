"""Mutation/recovery boundary tests: synthetic evidence and temporary state only."""

from dataclasses import FrozenInstanceError, fields, replace
import gc
import json
import sqlite3
from types import SimpleNamespace
import weakref

import pytest

from dam.cli import main
from dam.config import load_config
from dam.learning import LearningError, load_learned_rules
from dam.models import Settings
from dam.scan import default_config_directory
from dam.storage import Storage, StorageError
from dam.teaching import TeachingError, TeachingService
from dam.teaching_presentation import TeachingSaveDisplay, render_teaching
from dam.teaching_queries import QueryDisposition, TeachingQuery, query_teaching, preview_teaching
from dam.teaching_save import SaveTeaching, TeachingSaveControlResult, save_teaching
from test_durable_gmail import FakeGmail, NOW, message, run
from test_teaching_queries import forbidden, leaves, SAFE_FAILURE

AUTHORITY_LINE = "Classification learning only; authority=false; executable=false; Gmail actions executed=0.\n"
DISPLAY_FAILURE = "DAM teaching outcome could not be displayed; inspect teaching status before retrying.\n"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    learned = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    with Storage.open(settings) as store:
        run(monkeypatch, store, FakeGmail([
            message("A", sender="private-a@example.invalid", subject="SUBJECT-A-SENTINEL"),
            message("A-peer", sender="private-a@example.invalid", subject="SUBJECT-PEER-SENTINEL"),
            message("B", sender="private-b@example.invalid", subject="SUBJECT-B-SENTINEL"),
        ]))
        work = {store.item(row.representative_item_id).source_item_id: row
                for row in store.classification_work_list()}
        store.defer_classification_work(work["B"].work_id, occurred_at=NOW)
        service = TeachingService(store, learned_rules_path=learned)
        preview = service.preview(work["A"].work_id, "finance")
        request = SaveTeaching(preview.work_id, "finance", preview.fingerprint,
                               learned_rules_file=str(learned))
        base = load_config(default_config_directory()).model_copy(update={"settings": settings})
        monkeypatch.setattr("dam.cli.load_config", lambda *_args, **_kwargs: base)
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
                              service=service, preview=preview, request=request)


def save(ctx, request=None, sink=None):
    return save_teaching(request or ctx.request, settings=ctx.settings, present=sink or (lambda _: None))


def cli_save(ctx, request=None):
    request = request or ctx.request
    args = ["teach", "--learned-rules-file", str(ctx.learned), "save", request.work_id,
            "--category", request.category_selector, "--confirm-fingerprint", request.confirm_fingerprint]
    if request.item_id is not None:
        args.extend(("--item-id", request.item_id))
    return main(args)


def operation(ctx):
    return ctx.store.teaching_by_preview(ctx.request.confirm_fingerprint)


def histories(ctx):
    return {row.representative_item_id: ctx.store.evaluation_history(row.representative_item_id)
            for row in ctx.work.values()}


def assert_completed(ctx):
    row = operation(ctx)
    assert row["status"] == "completed"
    assert len(load_learned_rules(ctx.learned).records) == 1
    assert [event["event_type"] for event in ctx.store.teaching_events(row["teaching_id"])] == [
        "confirmed", "rule_saved", "reevaluation_pending", "completed"]
    assert all(len(chain) == 2 for chain in histories(ctx).values())
    assert ctx.store.classification_work(ctx.work["A"].work_id).state == "resolved"
    assert ctx.store.classification_work(ctx.work["A-peer"].work_id).state == "resolved"
    assert ctx.store.classification_work(ctx.work["B"].work_id).state == "deferred"
    return row


@pytest.mark.parametrize("explicit_item", [False, True])
def test_save_cli_exact_output_and_no_authority(ctx, capsys, explicit_item):
    request = replace(ctx.request, item_id=ctx.preview.item_id) if explicit_item else ctx.request
    assert cli_save(ctx, request) == 0
    row = assert_completed(ctx)
    assert capsys.readouterr() == (
        f"Teaching: {row['teaching_id']}; status: completed; reevaluated: 3; resolved: 2; "
        "unresolved: 1; insufficient evidence: 0.\n" + AUTHORITY_LINE, "")
    rule = load_learned_rules(ctx.learned).records[0].rule
    assert rule.proposed_action.value == "no_action" and rule.approval_ref is None
    assert not ctx.store.approvals(ctx.preview.metadata.account_id)
    assert ctx.store.audit_events(ctx.preview.observation_run_id) == ()


def test_save_operates_without_terminal_and_returns_minimal_control(ctx, monkeypatch):
    views = []
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result = save(ctx, sink=views.append)
    assert tuple(leaves(result)) == (QueryDisposition.COMPLETED, False)
    assert [field.name for field in fields(result)] == ["disposition", "presentation_failed"]
    assert len(views) == 1 and type(views[0]) is TeachingSaveDisplay
    assert all(type(value) in (str, int) for value in leaves(views[0]))
    output = render_teaching(views[0])
    for private in ("private-a", "SUBJECT-A", "finance", ctx.preview.fingerprint,
                    ctx.preview.item_id, ctx.preview.candidate.rule.id):
        assert private not in output + repr(result)


def test_pending_save_output_and_control_preserve_recovery(ctx, monkeypatch, capsys):
    evaluate = TeachingService.evaluate_exact_item
    def insufficient(service, item_id, *args, **kwargs):
        if item_id == ctx.work["B"].representative_item_id:
            raise TeachingError("PRIVATE-MISSING-EVIDENCE")
        return evaluate(service, item_id, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(TeachingService, "evaluate_exact_item", insufficient)
        views = []
        result = save(ctx, sink=views.append)
    assert result == TeachingSaveControlResult(QueryDisposition.INCOMPLETE)
    row = operation(ctx)
    assert row["status"] == "pending_reevaluation"
    assert render_teaching(views[0]) == (
        f"Teaching: {row['teaching_id']}; status: pending_reevaluation; reevaluated: 2; "
        "resolved: 2; unresolved: 0; insufficient evidence: 1.\n" + AUTHORITY_LINE)
    # The unmigrated public resume still recovers this exact domain checkpoint.
    assert main(["teach", "--learned-rules-file", str(ctx.learned), "resume", row["teaching_id"]]) == 0
    assert "status: completed" in capsys.readouterr().out
    assert_completed(ctx)


def test_pending_save_cli_exit_and_output(ctx, monkeypatch, capsys):
    def insufficient(*args, **kwargs):
        raise TeachingError("PRIVATE")
    monkeypatch.setattr(TeachingService, "evaluate_exact_item", insufficient)
    assert cli_save(ctx) == 2
    row = operation(ctx)
    assert capsys.readouterr() == (
        f"Teaching: {row['teaching_id']}; status: pending_reevaluation; reevaluated: 0; "
        "resolved: 0; unresolved: 0; insufficient evidence: 3.\n" + AUTHORITY_LINE, "")


@pytest.mark.parametrize("change", ["fingerprint", "category", "item", "work", "configuration"])
def test_scope_and_stale_confirmation_fail_before_intent(ctx, monkeypatch, capsys, change):
    request = ctx.request
    if change == "fingerprint":
        request = replace(request, confirm_fingerprint="0" * 64)
    elif change == "category":
        request = replace(request, category_selector="promotions")
    elif change == "item":
        request = replace(request, item_id=ctx.work["B"].representative_item_id)
    elif change == "work":
        request = replace(request, work_id=ctx.work["B"].work_id)
    else:
        config = ctx.service._config()
        changed = config.model_copy(update={"settings": config.settings.model_copy(update={
            "confidence": config.settings.confidence.model_copy(update={"auto_threshold": .96})})})
        monkeypatch.setattr(TeachingService, "_config", lambda _: changed)
    before = tuple(ctx.store._connection.iterdump())
    assert cli_save(ctx, request) == 2
    assert capsys.readouterr() == ("", SAFE_FAILURE)
    assert tuple(ctx.store._connection.iterdump()) == before
    assert not ctx.learned.exists()


def test_history_identity_provenance_and_duplicate_confirmation(ctx):
    original = histories(ctx)
    assert save(ctx).disposition is QueryDisposition.COMPLETED
    row = assert_completed(ctx)
    for item_id, chain in histories(ctx).items():
        assert chain[0] == original[item_id][0]
        assert chain[1]["predecessor_id"] == chain[0]["evaluation_id"]
        assert chain[1]["item_id"] == item_id
        assert chain[1]["teaching_id"] == row["teaching_id"]
        assert chain[1]["config_fingerprint"] == row["config_after"]
        assert chain[1]["observation_run_id"] == ctx.preview.observation_run_id
        assert chain[1]["observation_message_id"] == ctx.store.item(item_id).source_item_id
        assert ctx.store.current_evaluation(item_id) == chain[1]
    current = ctx.store.current_evaluation(ctx.preview.item_id)
    result = json.loads(current["classification_json"])
    assert result["classification_confidence"] == .90 and result["requires_review"]
    assert result["category_teaching_required"] is False
    assert result["classification_sources"][0]["rule_id"] == row["rule_id"]
    assert result["classification_sources"][0]["rule_version"] == row["rule_version"]
    before = tuple(ctx.store._connection.iterdump())
    rules = ctx.learned.read_bytes()
    assert save(ctx).disposition is QueryDisposition.COMPLETED
    assert tuple(ctx.store._connection.iterdump()) == before and ctx.learned.read_bytes() == rules
    with pytest.raises(sqlite3.IntegrityError):
        ctx.store._connection.execute("DELETE FROM classification_evaluations WHERE evaluation_id=?",
                                      (current["evaluation_id"],))


@pytest.mark.parametrize("boundary,expected_state,saved,eval_count", [
    ("before_intent", None, False, 0),
    ("before_yaml", "intent", False, 0),
    ("after_yaml", "intent", True, 0),
    ("reload_config", "rule_saved", True, 0),
    ("partial_evaluation", "pending_reevaluation", True, 1),
    ("after_completion", "completed", True, 3),
])
def test_failures_preserve_existing_checkpoints_and_resume(ctx, monkeypatch, capsys,
                                                         boundary, expected_state, saved, eval_count):
    views = []
    with monkeypatch.context() as patch:
        if boundary == "before_intent":
            def fail(*args, **kwargs):
                raise StorageError("PRIVATE-BEFORE-INTENT")
            patch.setattr(Storage, "create_teaching_intent", fail)
        elif boundary == "before_yaml":
            def fail(*args, **kwargs):
                assert operation(ctx)["status"] == "intent"
                raise LearningError("PRIVATE-BEFORE-YAML")
            patch.setattr("dam.teaching.save_classification_rule", fail)
        elif boundary == "after_yaml":
            advance = Storage.advance_teaching
            def fail(store, teaching_id, status, **kwargs):
                if status == "rule_saved":
                    assert len(load_learned_rules(ctx.learned).records) == 1
                    raise StorageError("PRIVATE-AFTER-YAML")
                return advance(store, teaching_id, status, **kwargs)
            patch.setattr(Storage, "advance_teaching", fail)
        elif boundary == "reload_config":
            config = TeachingService._config
            def fail(service):
                if ctx.learned.exists():
                    raise LearningError("PRIVATE-RELOAD")
                return config(service)
            patch.setattr(TeachingService, "_config", fail)
        elif boundary == "partial_evaluation":
            record = Storage.record_teaching_work_reevaluation
            calls = 0
            def fail(store, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise StorageError("PRIVATE-PARTIAL-EVAL")
                return record(store, *args, **kwargs)
            patch.setattr(Storage, "record_teaching_work_reevaluation", fail)
        else:
            def fail(*args, **kwargs):
                raise StorageError("PRIVATE-AFTER-COMPLETION")
            patch.setattr(Storage, "teaching_evaluation_counts", fail)
        result = save(ctx, sink=views.append)
    assert result == TeachingSaveControlResult(QueryDisposition.REJECTED)
    assert views == [] and "PRIVATE" not in repr(result)
    row = operation(ctx)
    assert (row["status"] if row else None) == expected_state
    assert ctx.learned.exists() is saved
    assert sum(len(chain) - 1 for chain in histories(ctx).values()) == eval_count
    existing_ids = {entry["evaluation_id"] for chain in histories(ctx).values() for entry in chain}
    if row is None:
        assert save(ctx).disposition is QueryDisposition.COMPLETED
    else:
        assert main(["teach", "--learned-rules-file", str(ctx.learned), "resume", row["teaching_id"]]) == 0
        assert "status: completed" in capsys.readouterr().out
    assert_completed(ctx)
    after_ids = {entry["evaluation_id"] for chain in histories(ctx).values() for entry in chain}
    assert existing_ids <= after_ids
    # Completed explicit recovery is also a no-op on durable history/YAML.
    before = tuple(ctx.store._connection.iterdump())
    ctx.service.resume(operation(ctx)["teaching_id"])
    assert tuple(ctx.store._connection.iterdump()) == before


def test_retry_after_intent_reuses_teaching_identity(ctx, monkeypatch):
    def fail(*args, **kwargs):
        raise LearningError("PRIVATE")
    with monkeypatch.context() as patch:
        patch.setattr("dam.teaching.save_classification_rule", fail)
        assert save(ctx).disposition is QueryDisposition.REJECTED
    teaching_id = operation(ctx)["teaching_id"]
    assert save(ctx).disposition is QueryDisposition.COMPLETED
    assert assert_completed(ctx)["teaching_id"] == teaching_id


def test_already_saved_matching_rule_reconciles_without_rewriting(ctx, monkeypatch):
    import dam.teaching as teaching
    save_rule = teaching.save_classification_rule
    def fail(*args, **kwargs):
        raise LearningError("PRIVATE")
    with monkeypatch.context() as patch:
        patch.setattr(teaching, "save_classification_rule", fail)
        assert save(ctx).disposition is QueryDisposition.REJECTED
    save_rule(ctx.preview.candidate, ctx.service._config(), ctx.learned,
              expected_fingerprint=ctx.preview.candidate.fingerprint)
    original = ctx.learned.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(teaching, "save_classification_rule", forbidden)
        ctx.service.resume(operation(ctx)["teaching_id"])
    assert_completed(ctx)
    assert ctx.learned.read_bytes() == original


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError])
def test_presentation_failure_keeps_completion_and_never_reexecutes(ctx, monkeypatch, error):
    confirm = TeachingService.confirm
    calls = []
    refs = []
    def confirming(*args, **kwargs):
        calls.append(True)
        return confirm(*args, **kwargs)
    monkeypatch.setattr(TeachingService, "confirm", confirming)
    def sink(view):
        refs.append(weakref.ref(view))
        assert operation(ctx)["status"] == "completed"
        raise error("PRIVATE-WRITER-FAILURE")
    result = save(ctx, sink=sink)
    assert result == TeachingSaveControlResult(QueryDisposition.COMPLETED, presentation_failed=True)
    assert calls == [True]
    assert_completed(ctx)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert "PRIVATE" not in repr(result)


def test_cli_presentation_failure_status_and_explicit_duplicate(ctx, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("PRIVATE-WRITER")
    with monkeypatch.context() as patch:
        patch.setattr("dam.cli._write_teaching", fail)
        assert cli_save(ctx) == 1
    assert capsys.readouterr() == ("", DISPLAY_FAILURE)
    row = assert_completed(ctx)
    before = tuple(ctx.store._connection.iterdump())
    assert main(["teach", "--learned-rules-file", str(ctx.learned), "status", row["teaching_id"]]) == 0
    assert "status: completed" in capsys.readouterr().out
    assert cli_save(ctx) == 0
    assert tuple(ctx.store._connection.iterdump()) == before


def test_writer_isolated_and_contracts_immutable(monkeypatch):
    for target in ("dam.storage.Storage.open", "dam.teaching.TeachingService.confirm",
                   "dam.teaching.TeachingService.preview", "dam.config.load_config",
                   "dam.auth.authenticate", "dam.gmail.read_message"):
        monkeypatch.setattr(target, forbidden)
    view = TeachingSaveDisplay("TEACH-example", "completed", 3, 2, 1, 0)
    assert render_teaching(view).endswith(AUTHORITY_LINE)
    with pytest.raises(TypeError, match="Unsupported"):
        render_teaching(SimpleNamespace(teaching_id="PRIVATE"))
    for value in (view, SaveTeaching("CWQ", "category", "fingerprint"),
                  TeachingSaveControlResult(QueryDisposition.COMPLETED)):
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        assert not hasattr(value, "__dict__")
    for field in fields(view):
        with pytest.raises(TypeError):
            replace(view, **{field.name: {"PRIVATE": "record"}})


def test_cross_operation_display_has_no_retained_private_state(ctx):
    outputs, refs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_teaching(view))
    assert save(ctx, sink=sink).disposition is QueryDisposition.COMPLETED
    first_id = operation(ctx)["teaching_id"]
    assert query_teaching(TeachingQuery.SHOW, settings=ctx.settings,
        target_id=ctx.work["B"].work_id, learned_rules_path=ctx.learned, present=sink).disposition is QueryDisposition.COMPLETED
    assert preview_teaching(settings=ctx.settings, work_id=ctx.work["B"].work_id,
        category_selector="promotions", learned_rules_file=str(ctx.learned), present=sink).disposition is QueryDisposition.COMPLETED
    preview = ctx.service.preview(ctx.work["B"].work_id, "promotions")
    request = SaveTeaching(preview.work_id, "promotions", preview.fingerprint,
                           learned_rules_file=str(ctx.learned))
    assert save(ctx, request, sink).disposition is QueryDisposition.COMPLETED
    assert all("private-a" not in output and "SUBJECT-A" not in output for output in outputs)
    assert all(first_id not in output for output in outputs[1:])
    assert "private-b" not in outputs[-1] and "SUBJECT-B" not in outputs[-1]
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert render_teaching.__dict__ == {} and render_teaching.__closure__ is None


@pytest.mark.parametrize("error,disposition,exit_code", [
    (StorageError, QueryDisposition.REJECTED, 2),
    (LearningError, QueryDisposition.REJECTED, 2),
    (RuntimeError, QueryDisposition.FAILED, 1),
])
def test_mutation_exception_privacy(ctx, monkeypatch, capsys, caplog, error, disposition, exit_code):
    def fail(*args, **kwargs):
        raise error("PRIVATE-MUTATION-FAILURE")
    monkeypatch.setattr(TeachingService, "confirm", fail)
    result = save(ctx)
    assert result == TeachingSaveControlResult(disposition)
    assert cli_save(ctx) == exit_code
    out, err = capsys.readouterr()
    assert out == ""
    assert err == (SAFE_FAILURE if exit_code == 2 else
                   "DAM teach failed internally; inspect durable teaching status before retrying.\n")
    assert "PRIVATE" not in repr(result) + caplog.text


def test_pending_mutation_and_failed_display_are_independent(ctx, monkeypatch):
    def insufficient(*args, **kwargs):
        raise TeachingError("PRIVATE-EVIDENCE")
    def sink(view):
        assert view.status == "pending_reevaluation"
        raise RuntimeError("PRIVATE-DISPLAY")
    monkeypatch.setattr(TeachingService, "evaluate_exact_item", insufficient)
    assert save(ctx, sink=sink) == TeachingSaveControlResult(
        QueryDisposition.INCOMPLETE, presentation_failed=True)
    assert operation(ctx)["status"] == "pending_reevaluation"
    assert all(len(chain) == 1 for chain in histories(ctx).values())
    assert len(load_learned_rules(ctx.learned).records) == 1


def test_display_after_close_and_interruption_does_not_undo_save(ctx, monkeypatch):
    close = Storage.close
    closed, refs = [], []
    def closing(store):
        close(store)
        closed.append(True)
    monkeypatch.setattr(Storage, "close", closing)
    def sink(view):
        assert closed == [True]
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        save(ctx, sink=sink)
    assert_completed(ctx)
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_permanent_category_and_completed_duplicate_scope_checks(ctx):
    request = replace(ctx.request, category_selector=ctx.preview.category_permanent_id)
    assert save(ctx, request).disposition is QueryDisposition.COMPLETED
    assert_completed(ctx)
    before = tuple(ctx.store._connection.iterdump())
    assert save(ctx, replace(request, category_selector="promotions")).disposition is QueryDisposition.REJECTED
    assert save(ctx, replace(request, item_id=ctx.work["B"].representative_item_id)).disposition is QueryDisposition.REJECTED
    assert tuple(ctx.store._connection.iterdump()) == before


def test_invalid_save_input_cannot_open_storage(ctx, monkeypatch):
    monkeypatch.setattr(Storage, "open", forbidden)
    assert save_teaching({"PRIVATE": "namespace"}, settings=ctx.settings,
                         present=forbidden).disposition is QueryDisposition.REJECTED
    with pytest.raises(TypeError) as error:
        SaveTeaching("CWQ", "category", {"PRIVATE": "fingerprint"})
    assert "PRIVATE" not in str(error.value)


def test_projection_failure_keeps_committed_mutation(ctx, monkeypatch):
    def fail(*args):
        raise ValueError("PRIVATE-PROJECTION")
    monkeypatch.setattr("dam.teaching_save.TeachingSaveDisplay", fail)
    assert save(ctx, sink=forbidden) == TeachingSaveControlResult(
        QueryDisposition.COMPLETED, presentation_failed=True)
    assert_completed(ctx)
