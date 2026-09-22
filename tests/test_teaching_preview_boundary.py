"""Offline preview disclosure, confirmation compatibility, and lifetime tests."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta
import gc
import hashlib
import json
import shlex
import weakref

import pytest

from dam.categories import CategoryCatalog
from dam.cli import main, parser
from dam.presentation import safe_metadata_text
from dam.storage import Storage, StorageError
from dam.teaching import TeachingService
from dam.teaching_presentation import TeachingPreviewDisplay, render_teaching
from dam.teaching_queries import QueryControlResult, QueryDisposition, preview_teaching
from test_teaching_queries import context, cli, forbidden, leaves, SAFE_FAILURE


def preview(ctx, *, native="native-A", category="finance", present=None, **options):
    return preview_teaching(settings=ctx.settings, work_id=ctx.work[native].work_id,
        category_selector=category, learned_rules_file=str(ctx.learned),
        present=present or (lambda view: None), **options)


def capture(ctx, **options):
    views = []
    assert preview(ctx, present=views.append, **options).disposition is QueryDisposition.COMPLETED
    assert len(views) == 1
    assert type(views[0]) is TeachingPreviewDisplay
    return views[0]


@pytest.mark.parametrize("explicit_item", [False, True])
@pytest.mark.parametrize("permanent_selector", [False, True])
def test_exact_legacy_preview_output(context, capsys, explicit_item, permanent_selector):
    ctx = context
    work = ctx.work["native-A"]
    original = ctx.service.preview(work.work_id, "finance")
    category = original.category_permanent_id if permanent_selector else "finance"
    extra = ["--item-id", original.item_id] if explicit_item else []
    command = ["dam", "teach", "--learned-rules-file", str(ctx.learned),
               "save", work.work_id, "--category", category, *extra,
               "--confirm-fingerprint", original.fingerprint]
    assert cli(ctx, "preview", work.work_id, "--category", category, *extra) == 0
    out, err = capsys.readouterr()
    assert out == (
        f"Teaching preview only; work: {work.work_id}; ITEM: {original.item_id}\n"
        f"Stored observation: {original.observation_run_id} at {original.observed_at.isoformat()}\n"
        f"Category: {original.category_name} ({original.category_permanent_id})\n"
        f"Exact sender: {safe_metadata_text(original.candidate.rule.match.sender_emails_any[0], present=True)}\n"
        f"Fingerprint: {original.fingerprint}\n"
        "Confirmation required; save command:\n" + shlex.join(command) + "\n"
        "No Gmail read or mailbox action occurred.\n")
    assert err == ""
    assert "SUBJECT-A" not in out and "private-b" not in out
    parsed = parser().parse_args(shlex.split(out.splitlines()[-2])[1:])
    assert parsed.teach_command == "save" and parsed.confirm_fingerprint == original.fingerprint
    assert parsed.category == category and parsed.item_id == (original.item_id if explicit_item else None)


def test_fingerprint_stays_in_existing_teaching_primitive(context):
    ctx = context
    work = ctx.work["native-A"]
    original = ctx.service.preview(work.work_id, "finance")
    # Pin the existing binding independently of rendering/operation projection.
    material = {"work_id": work.work_id, "item_id": original.item_id,
        "observation_run_id": original.observation_run_id,
        "observed_at": original.observed_at.isoformat(),
        "metadata": original.metadata.model_dump(mode="json", exclude_unset=True),
        "category_permanent_id": original.category_permanent_id,
        "candidate_fingerprint": original.candidate.fingerprint}
    expected = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False).encode("utf-8")).hexdigest()
    first = capture(ctx)
    assert first.fingerprint == capture(ctx).fingerprint == original.fingerprint == expected
    assert capture(ctx, category=original.category_permanent_id).fingerprint == expected
    assert capture(ctx, item_id=original.item_id).fingerprint == expected
    assert capture(ctx, category="promotions").fingerprint != expected
    assert capture(ctx, native="native-B").fingerprint != expected


@pytest.mark.parametrize("change", ["subject", "observation_time", "configuration"])
def test_relevant_evidence_or_configuration_changes_fingerprint(context, monkeypatch, change):
    before = capture(context)
    if change == "configuration":
        config = context.service._config()
        changed = config.model_copy(update={"settings": config.settings.model_copy(update={
            "confidence": config.settings.confidence.model_copy(update={"auto_threshold": 0.96})})})
        monkeypatch.setattr(TeachingService, "_config", lambda _: changed)
    else:
        work, item, observation = context.service.inspect(context.work["native-A"].work_id)
        if change == "subject":
            observation = observation.model_copy(update={"metadata": observation.metadata.model_copy(
                update={"subject": "changed-private-subject"})})
        else:
            observation = observation.model_copy(update={"observed_at": observation.observed_at + timedelta(seconds=1)})
        monkeypatch.setattr(TeachingService, "inspect", lambda *_args, **_kwargs: (work, item, observation))
    assert capture(context).fingerprint != before.fingerprint


def test_quoted_command_preserves_original_paths_and_scope(context, tmp_path, capsys):
    directory = tmp_path / "space ' quote $value `literal` ;" / ".config" / "dam"
    directory.mkdir(parents=True, mode=0o700)
    learned = directory / "learned-rules.yaml"
    learned.write_bytes(context.learned.read_bytes())
    learned.chmod(0o600)
    catalog = directory / "categories.yaml"
    catalog.write_text(CategoryCatalog().model_dump_json())
    catalog.chmod(0o600)
    # Repeated separators are intentionally preserved in displayed arguments.
    learned_arg = str(directory) + "//learned-rules.yaml"
    catalog_arg = str(directory) + "//categories.yaml"
    work = context.work["native-A"]
    args = ["teach", "--learned-rules-file", learned_arg, "--category-catalog-file", catalog_arg,
            "preview", work.work_id, "--category", "finance", "--item-id", work.representative_item_id]
    assert main(args) == 0
    out, err = capsys.readouterr()
    assert err == ""
    command = shlex.split(out.splitlines()[-2])
    fingerprint = next(line.removeprefix("Fingerprint: ") for line in out.splitlines() if line.startswith("Fingerprint: "))
    assert command == ["dam", "teach", "--learned-rules-file", learned_arg,
        "--category-catalog-file", catalog_arg, "save", work.work_id, "--category", "finance",
        "--item-id", work.representative_item_id, "--confirm-fingerprint", fingerprint]


def test_default_catalog_used_but_not_added_as_explicit_override(context, tmp_path, monkeypatch):
    catalog = tmp_path / ".config" / "dam" / "categories.yaml"
    catalog.write_text(CategoryCatalog().model_dump_json())
    catalog.chmod(0o600)
    monkeypatch.setattr("dam.teaching_queries.default_catalog_path", lambda: catalog)
    view = capture(context)
    assert view.category_catalog_file is None
    assert "--category-catalog-file" not in render_teaching(view)


def test_preview_no_mutation_no_source_no_automatic_save(context, monkeypatch):
    before = tuple(context.store._connection.iterdump())
    learned = context.learned.read_bytes()
    for name in ("confirm", "resume", "reevaluate_exact_item"):
        monkeypatch.setattr(TeachingService, name, forbidden)
    monkeypatch.setattr("dam.teaching.save_classification_rule", forbidden)
    # Context fixture rejects all authentication/read/source-binding calls.
    result = preview(context)
    assert result == QueryControlResult(QueryDisposition.COMPLETED)
    assert tuple(context.store._connection.iterdump()) == before
    assert context.learned.read_bytes() == learned


def test_absent_learned_file_not_created(context, tmp_path):
    learned = tmp_path / "another" / ".config" / "dam" / "learned-rules.yaml"
    before = tuple(context.store._connection.iterdump())
    assert preview_teaching(settings=context.settings, work_id=context.work["native-A"].work_id,
        category_selector="finance", learned_rules_file=str(learned),
        present=lambda _: None).disposition is QueryDisposition.COMPLETED
    assert not learned.parent.exists()
    assert tuple(context.store._connection.iterdump()) == before


def test_control_contains_only_execution_disposition_without_terminal(context, monkeypatch):
    views = []
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result = preview(context, present=views.append)
    assert tuple(leaves(result)) == (QueryDisposition.COMPLETED,)
    assert [field.name for field in fields(result)] == ["disposition"]
    assert all(type(value) in (str, type(None)) for value in leaves(views[0]))
    assert not any(word in repr(result) for word in ("sender", "subject", "finance", "fingerprint", "save"))


def test_writer_needs_only_detached_contract_and_never_computes_fingerprint(context, monkeypatch):
    view = capture(context)
    original_fingerprint = view.fingerprint
    for target in ("dam.storage.Storage.open", "dam.teaching.TeachingService.preview",
                   "dam.teaching.TeachingService._config", "dam.config.load_config", "hashlib.sha256"):
        monkeypatch.setattr(target, forbidden)
    output = render_teaching(view)
    assert output.count(original_fingerprint) == 2
    assert view.fingerprint == original_fingerprint
    with pytest.raises(FrozenInstanceError):
        view.fingerprint = "different"
    with pytest.raises(TypeError, match="Unsupported teaching presentation contract"):
        render_teaching({"fingerprint": original_fingerprint})


def test_cross_item_preview_lifetime_and_disclosure(context):
    outputs = []
    fingerprints = []
    refs = []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_teaching(view))
        fingerprints.append(view.fingerprint)
    assert preview(context, present=sink).disposition is QueryDisposition.COMPLETED
    assert preview(context, native="native-B", category="promotions", present=sink).disposition is QueryDisposition.COMPLETED
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert "private-a@example.invalid" in outputs[0]
    assert "private-a@example.invalid" not in outputs[1]
    assert "private-b@example.invalid" in outputs[1]
    assert fingerprints[0] not in outputs[1]
    assert context.work["native-A"].work_id not in outputs[1]
    assert "Category: Finance " not in outputs[1]
    assert render_teaching.__dict__ == {} and render_teaching.__closure__ is None


@pytest.mark.parametrize("exception,disposition,code,error_text", [
    (StorageError, QueryDisposition.REJECTED, 2, SAFE_FAILURE),
    (RuntimeError, QueryDisposition.FAILED, 1, "DAM teach failed internally.\n"),
])
def test_preview_exception_privacy(context, monkeypatch, capsys, caplog, exception, disposition, code, error_text):
    def fail(*_args, **_kwargs):
        raise exception("PRIVATE-PREVIEW-FAILURE")
    monkeypatch.setattr(TeachingService, "preview", fail)
    views = []
    result = preview(context, present=views.append)
    assert result == QueryControlResult(disposition) and views == []
    assert cli(context, "preview", context.work["native-A"].work_id, "--category", "finance") == code
    assert capsys.readouterr() == ("", error_text)
    assert "PRIVATE" not in repr(result) + caplog.text


@pytest.mark.parametrize("case", ["unknown_work", "resolved", "unknown_category", "foreign_item", "missing_evidence", "invalid_config"])
def test_preview_expected_failures(context, monkeypatch, capsys, case):
    work = context.work["native-A"].work_id
    category = "finance"
    extra = []
    if case == "unknown_work":
        work = "CWQ-private-unknown"
    elif case == "resolved":
        work = context.work["native-C"].work_id
    elif case == "unknown_category":
        category = "private-unknown-category"
    elif case == "foreign_item":
        extra = ["--item-id", context.work["native-B"].representative_item_id]
    elif case == "missing_evidence":
        monkeypatch.setattr(Storage, "latest_email_observation", lambda *_: None)
    else:
        def fail(*_args):
            raise ValueError("PRIVATE-CONFIG")
        monkeypatch.setattr(TeachingService, "_config", fail)
    assert cli(context, "preview", work, "--category", category, *extra) == 2
    assert capsys.readouterr() == ("", SAFE_FAILURE)


def test_preview_contract_rejects_broad_objects_and_hides_repr(context):
    view = capture(context)
    for name in (field.name for field in fields(view)):
        with pytest.raises(TypeError, match="Scalar preview display values required"):
            replace(view, **{name: {"private": "domain payload"}})
    assert "private-a" not in repr(view)
    assert view.fingerprint not in repr(view)
    assert not hasattr(view, "__dict__")


@pytest.mark.parametrize("interrupt", [False, True])
def test_writer_failure_releases_preview_after_storage_closed(context, monkeypatch, interrupt):
    refs = []
    closed = []
    close = Storage.close
    def closing(store):
        close(store)
        closed.append(True)
    monkeypatch.setattr(Storage, "close", closing)
    def sink(view):
        assert closed == [True]
        refs.append(weakref.ref(view))
        if interrupt:
            raise KeyboardInterrupt
        raise RuntimeError("PRIVATE-WRITER-PREVIEW")
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            preview(context, present=sink)
    else:
        assert preview(context, present=sink).disposition is QueryDisposition.FAILED
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_preview_fingerprint_does_not_bypass_save_validation(context):
    from dam.teaching import TeachingError
    view = capture(context)
    before = tuple(context.store._connection.iterdump())
    learned = context.learned.read_bytes()
    # The released token is useful only with the exact proposal; it cannot
    # authorize a different category or another ITEM's teaching decision.
    with pytest.raises(TeachingError, match="Stale or unconfirmed"):
        context.service.confirm(view.work_id, "promotions", confirm_fingerprint=view.fingerprint)
    with pytest.raises(TeachingError, match="Stale or unconfirmed"):
        context.service.confirm(context.work["native-B"].work_id, "finance",
                                confirm_fingerprint=view.fingerprint)
    assert tuple(context.store._connection.iterdump()) == before
    assert context.learned.read_bytes() == learned


def test_default_file_arguments_are_not_inferred_into_save_command(context):
    views = []
    assert preview_teaching(settings=context.settings, work_id=context.work["native-A"].work_id,
        category_selector="finance", present=views.append).disposition is QueryDisposition.COMPLETED
    view = views[0]
    command = shlex.split(render_teaching(view).splitlines()[-2])
    assert command == ["dam", "teach", "save", view.work_id, "--category", "finance",
                       "--confirm-fingerprint", view.fingerprint]


@pytest.mark.parametrize("sender,expected", [
    ("https://private.invalid/token", "[URL redacted]"),
    ("a\x1bb\nc", "a�b�c"),
    ("x" * 301, "x" * 300 + "…"),
])
def test_preview_releases_only_safe_sender_display(context, monkeypatch, sender, expected):
    from types import SimpleNamespace
    original = context.service.preview(context.work["native-A"].work_id, "finance")
    candidate = SimpleNamespace(rule=SimpleNamespace(match=SimpleNamespace(sender_emails_any=(sender,))))
    injected = replace(original, candidate=candidate)
    monkeypatch.setattr(TeachingService, "preview", lambda *_args, **_kwargs: injected)
    view = capture(context)
    assert view.sender_display == expected
    assert "Exact sender: " + expected + "\n" in render_teaching(view)
    assert all(type(value) in (str, type(None)) for value in leaves(view))
