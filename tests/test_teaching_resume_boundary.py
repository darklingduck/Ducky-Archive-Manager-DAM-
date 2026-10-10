"""Offline resume boundary and existing durable recovery checkpoints."""

from dataclasses import FrozenInstanceError, fields, replace
import gc
import json
import weakref

import pytest

from test_durable_gmail import NOW
from dam.cli import main
from dam.learning import LearningError, load_learned_rules
from dam.storage import Storage, StorageError
from dam.teaching import TeachingError, TeachingService
from dam.teaching_presentation import TeachingSaveDisplay, render_teaching
from dam.teaching_queries import QueryDisposition, TeachingQuery, query_teaching
from dam.teaching_resume import ResumeTeaching, resume_teaching
from dam.teaching_save import TeachingSaveControlResult
from test_teaching_save_boundary import (
    ctx, save, operation, histories, assert_completed, AUTHORITY_LINE, DISPLAY_FAILURE,
)
from test_teaching_queries import forbidden, leaves, SAFE_FAILURE


def resume(ctx, sink=lambda _: None, teaching_id=None):
    return resume_teaching(ResumeTeaching(teaching_id or operation(ctx)["teaching_id"],
        learned_rules_file=str(ctx.learned)), settings=ctx.settings, present=sink)


def cli_resume(ctx, teaching_id=None):
    return main(["teach", "--learned-rules-file", str(ctx.learned), "resume",
                 teaching_id or operation(ctx)["teaching_id"]])


def pending(ctx, monkeypatch):
    def fail(*args, **kwargs):
        raise LearningError("PRIVATE-CHECKPOINT")
    with monkeypatch.context() as patch:
        patch.setattr("dam.teaching.save_classification_rule", fail)
        assert save(ctx).disposition is QueryDisposition.REJECTED
    assert operation(ctx)["status"] == "intent"


@pytest.mark.parametrize("checkpoint", ["intent", "yaml", "rule_saved", "partial"])
def test_resume_checkpoint_history_provenance_and_idempotency(ctx, monkeypatch, checkpoint):
    pending(ctx, monkeypatch)
    if checkpoint != "intent":
        advance = Storage.advance_teaching
        record = Storage.record_teaching_work_reevaluation
        calls = 0
        def advance_fail(store, teaching_id, status, **kwargs):
            if status == ("rule_saved" if checkpoint == "yaml" else "pending_reevaluation"):
                raise StorageError("PRIVATE-ADVANCE")
            return advance(store, teaching_id, status, **kwargs)
        def record_fail(store, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise StorageError("PRIVATE-PARTIAL")
            return record(store, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "record_teaching_work_reevaluation" if checkpoint == "partial"
                          else "advance_teaching", record_fail if checkpoint == "partial" else advance_fail)
            assert resume(ctx).disposition is QueryDisposition.REJECTED
        assert operation(ctx)["status"] == {
            "yaml": "intent", "rule_saved": "rule_saved", "partial": "pending_reevaluation"}[checkpoint]
        assert len(load_learned_rules(ctx.learned).records) == 1
    before = histories(ctx)
    assert resume(ctx).disposition is QueryDisposition.COMPLETED
    row = assert_completed(ctx)
    for item_id, chain in histories(ctx).items():
        assert chain[:len(before[item_id])] == before[item_id]
        assert chain[1]["predecessor_id"] == chain[0]["evaluation_id"]
        assert chain[1]["config_fingerprint"] == row["config_after"]
        assert chain[1]["teaching_id"] == row["teaching_id"]
        assert chain[1]["item_id"] == item_id
        assert chain[1]["observation_message_id"] == ctx.store.item(item_id).source_item_id
    result = json.loads(ctx.store.current_evaluation(ctx.preview.item_id)["classification_json"])
    assert result["classification_sources"][0]["rule_id"] == row["rule_id"]
    assert result["classification_sources"][0]["rule_version"] == row["rule_version"]
    assert result["requires_review"] and not result["category_teaching_required"]
    dump, yaml = tuple(ctx.store._connection.iterdump()), ctx.learned.read_bytes()
    assert resume(ctx).disposition is QueryDisposition.COMPLETED
    assert tuple(ctx.store._connection.iterdump()) == dump and ctx.learned.read_bytes() == yaml


def test_resume_exact_cli_output_and_no_authority(ctx, monkeypatch, capsys):
    pending(ctx, monkeypatch)
    assert cli_resume(ctx) == 0
    row = assert_completed(ctx)
    assert capsys.readouterr() == (
        f"Teaching: {row['teaching_id']}; status: completed; reevaluated: 3; resolved: 2; "
        "unresolved: 1; insufficient evidence: 0.\n" + AUTHORITY_LINE, "")
    rule = load_learned_rules(ctx.learned).records[0].rule
    assert rule.proposed_action.value == "no_action" and rule.approval_ref is None
    assert not ctx.store.approvals(ctx.preview.metadata.account_id)
    assert ctx.store.audit_events(ctx.preview.observation_run_id) == ()


def test_incomplete_control_and_output(ctx, monkeypatch, capsys):
    pending(ctx, monkeypatch)
    def fail(*args, **kwargs):
        raise TeachingError("PRIVATE-EVIDENCE")
    with monkeypatch.context() as patch:
        patch.setattr(TeachingService, "evaluate_exact_item", fail)
        views = []
        assert resume(ctx, views.append) == TeachingSaveControlResult(QueryDisposition.INCOMPLETE)
        assert cli_resume(ctx) == 2
    assert capsys.readouterr() == (render_teaching(views[0]), "")
    assert views[0].insufficient == 3 and views[0].reevaluated == 0
    assert operation(ctx)["status"] == "pending_reevaluation"
    assert resume(ctx).disposition is QueryDisposition.COMPLETED
    assert_completed(ctx)


@pytest.mark.parametrize("corruption", ["unknown", "yaml", "missing_rule", "configuration"])
def test_invalid_recovery_sanitized_without_evaluations(ctx, monkeypatch, capsys, corruption):
    pending(ctx, monkeypatch)
    target = None
    if corruption == "unknown":
        target = "TEACH-PRIVATE-UNKNOWN"
    elif corruption == "yaml":
        ctx.learned.parent.mkdir(parents=True, exist_ok=True)
        ctx.learned.write_text("records: [PRIVATE-CORRUPT")
    elif corruption == "missing_rule":
        ctx.store.advance_teaching(operation(ctx)["teaching_id"], "rule_saved", occurred_at=NOW)
    else:
        def fail(*args, **kwargs):
            raise TeachingError("PRIVATE")
        with monkeypatch.context() as patch:
            patch.setattr(TeachingService, "evaluate_exact_item", fail)
            assert resume(ctx).disposition is QueryDisposition.INCOMPLETE
        config = ctx.service._config()
        changed = config.model_copy(update={"settings": config.settings.model_copy(update={
            "confidence": config.settings.confidence.model_copy(update={"auto_threshold": .96})})})
        monkeypatch.setattr(TeachingService, "_config", lambda _: changed)
    before = histories(ctx)
    assert resume(ctx, forbidden, target).disposition is QueryDisposition.REJECTED
    assert cli_resume(ctx, target) == 2
    assert capsys.readouterr() == ("", SAFE_FAILURE)
    assert histories(ctx) == before


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError])
def test_writer_failure_preserves_completion_no_retry(ctx, monkeypatch, capsys, error):
    pending(ctx, monkeypatch)
    original = TeachingService.resume
    calls, refs = [], []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(TeachingService, "resume", counted)
    def sink(view):
        refs.append(weakref.ref(view))
        assert_completed(ctx)
        raise error("PRIVATE-WRITER")
    assert resume(ctx, sink) == TeachingSaveControlResult(QueryDisposition.COMPLETED, True)
    assert calls == [True]
    before = tuple(ctx.store._connection.iterdump())
    monkeypatch.setattr("dam.cli._write_teaching", sink)
    assert cli_resume(ctx) == 1
    assert calls == [True, True]
    assert capsys.readouterr() == ("", DISPLAY_FAILURE)
    assert tuple(ctx.store._connection.iterdump()) == before
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_minimal_immutable_control_input_and_isolated_display(ctx, monkeypatch):
    pending(ctx, monkeypatch)
    refs, outputs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_teaching(view))
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result = resume(ctx, sink)
    assert list(leaves(result)) == [QueryDisposition.COMPLETED, False]
    assert [f.name for f in fields(result)] == ["disposition", "presentation_failed"]
    request = ResumeTeaching("TEACH")
    for value in (request, result):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
    assert query_teaching(TeachingQuery.SHOW, settings=ctx.settings, present=sink,
        target_id=ctx.work["B"].work_id, learned_rules_path=ctx.learned).disposition is QueryDisposition.COMPLETED
    assert resume(ctx, sink).disposition is QueryDisposition.COMPLETED
    assert "private-b" in outputs[1] and "private-b" not in outputs[2]
    for private in ("private-a", "SUBJECT-A", ctx.preview.item_id, ctx.preview.fingerprint):
        assert private not in outputs[0] + repr(result)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert render_teaching.__dict__ == {} and render_teaching.__closure__ is None
    monkeypatch.setattr(Storage, "open", forbidden)
    assert resume_teaching({}, settings=ctx.settings, present=forbidden).disposition is QueryDisposition.REJECTED
    for field in fields(request):
        with pytest.raises(TypeError):
            replace(request, **{field.name: {"PRIVATE": "value"}})
    assert render_teaching(TeachingSaveDisplay("TEACH", "completed", 0, 0, 0, 0)).endswith(AUTHORITY_LINE)


@pytest.mark.parametrize("error,disposition,code", [
    (StorageError, QueryDisposition.REJECTED, 2),
    (RuntimeError, QueryDisposition.FAILED, 1),
])
def test_execution_error_sanitization(ctx, monkeypatch, capsys, caplog, error, disposition, code):
    def fail(*args, **kwargs):
        raise error("PRIVATE-RECOVERY")
    monkeypatch.setattr(TeachingService, "resume", fail)
    result = resume(ctx, forbidden, "TEACH-private")
    assert result == TeachingSaveControlResult(disposition)
    assert cli_resume(ctx, "TEACH-private") == code
    out, err = capsys.readouterr()
    assert out == "" and "PRIVATE" not in err + caplog.text + repr(result)
    assert err == (SAFE_FAILURE if code == 2 else
        "DAM teach failed internally; inspect durable teaching status before retrying.\n")


def test_projection_failure_after_close_and_interrupt(ctx, monkeypatch):
    pending(ctx, monkeypatch)
    close = Storage.close
    closed = []
    def closing(store):
        close(store)
        closed.append(True)
    monkeypatch.setattr(Storage, "close", closing)
    def fail(*args):
        assert closed == [True]
        raise ValueError("PRIVATE-PROJECTION")
    with monkeypatch.context() as patch:
        patch.setattr("dam.teaching_resume.TeachingSaveDisplay", fail)
        assert resume(ctx, forbidden) == TeachingSaveControlResult(QueryDisposition.COMPLETED, True)
    assert_completed(ctx)
    refs = []
    def interrupt(view):
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        resume(ctx, interrupt)
    assert_completed(ctx)
    gc.collect()
    assert all(ref() is None for ref in refs)
