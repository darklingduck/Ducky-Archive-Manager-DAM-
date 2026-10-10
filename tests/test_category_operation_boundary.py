"""Category boundary checks using only temporary configuration and private files."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from datetime import datetime, timezone
import gc
import shlex
import shutil
import socket
from types import SimpleNamespace
import weakref

import pytest

from dam.categories import CategoryError, load_catalog, new_object_id, propose_change, render_change, save_change
from dam.category_operations import CategoryOperation, CategoriesRequest, CategoriesControlResult, operate_categories
from dam.category_presentation import (
    CategoryChangeDisplay, CategoryConfirmation, CategoryDetail, CategoryDiagnostic,
    CategoryDiagnosticCode, CategoryListing, CategoryListingRow, render_categories,
)
from dam.cli import main
from dam.config import ConfigurationError, load_config
from dam.learning import LearningError, propose_classification_rule, save_classification_rule
from dam.models import MessageMetadata
from dam.scan import default_config_directory, MAX_INITIAL_GMAIL_LIMIT
from dam.teaching_presentation import TeachingStatus, render_teaching
from dam.teaching_queries import QueryDisposition

CHANGE_ERROR = "DAM categories failed: invalid local category change or catalog; no change saved.\n"
CONFIG_ERROR = "DAM categories failed: invalid local configuration.\n"
INTERNAL_ERROR = "DAM categories failed internally.\n"
DISPLAY_ERROR = "DAM categories outcome could not be displayed; inspect the category catalog before retrying.\n"
CAT_A = new_object_id("CAT", random_bytes=b"\x01" * 16)
CAT_B = new_object_id("CAT", random_bytes=b"\x02" * 16)


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected source, repository, or terminal capability")


def leaves(value):
    if is_dataclass(value):
        for field in fields(value):
            yield from leaves(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            yield from leaves(item)
    else:
        yield value


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("categories.yaml", "rules.yaml", "settings.yaml"):
        shutil.copyfile(default_config_directory() / name, directory / name)
    catalog = tmp_path / ".config" / "dam" / "categories.yaml"
    learned = catalog.with_name("learned-rules.yaml")
    monkeypatch.setattr("dam.category_operations.default_config_directory", lambda: directory)
    monkeypatch.setattr("dam.category_operations.default_catalog_path", lambda: catalog)
    monkeypatch.setattr("dam.category_operations.default_learned_rules_path", lambda: learned)
    for target in (
        "dam.auth.authenticate", "dam.auth.build_gmail_service", "dam.gmail.read_message",
        "dam.gmail.read_inbox", "dam.gmail.read_authenticated_profile", "dam.scan.authenticate",
        "dam.scan.read_inbox", "dam.review.authenticate", "dam.review.read_message",
        "dam.durable_gmail.authenticate", "dam.durable_gmail.read_inbox",
        "dam.storage.Storage.open", "dam.storage.Storage.record_approval",
    ):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    return SimpleNamespace(directory=directory, catalog=catalog, learned=learned,
                           config=load_config(directory))


def invoke(ctx, operation, sink=None, **values):
    views = []
    result = operate_categories(CategoriesRequest(CategoryOperation(operation),
        catalog_file=str(ctx.catalog), learned_rules_file=str(ctx.learned), **values),
        present=sink or views.append)
    return result, views


def saved(ctx, preview, sink=None):
    confirmation = preview.confirmation
    return invoke(ctx, confirmation.operation, sink=sink, selector=confirmation.selector,
        key=confirmation.key, name=confirmation.name, parent=confirmation.parent, save=True,
        confirm_fingerprint=confirmation.fingerprint,
        confirm_id=confirmation.permanent_id if confirmation.operation == "add" else None,
        confirm_retire=confirmation.permanent_id if confirmation.operation == "retire" else None)


def create(ctx, key="private_a", name="PRIVATE-A-NAME", monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr("dam.category_operations.new_category_id", lambda _: CAT_A if key == "private_a" else CAT_B)
    result, views = invoke(ctx, "add", key=key, name=name)
    assert result == CategoriesControlResult(QueryDisposition.COMPLETED)
    result, _ = saved(ctx, views[0])
    assert result == CategoriesControlResult(QueryDisposition.COMPLETED)
    return views[0].after.permanent_id


def save_command(view):
    output = render_categories(view).splitlines()
    return shlex.split(output[output.index("Save command:") + 1])[1:]


def test_list_show_exact_output_hierarchy_and_retired_state(ctx, monkeypatch, capsys):
    identity = create(ctx, monkeypatch=monkeypatch)
    _, views = invoke(ctx, "list")
    assert main(["categories"]) == 0
    assert capsys.readouterr() == (render_categories(views[0]), "")
    _, views = invoke(ctx, "show", selector=identity)
    assert render_categories(views[0]) == (
        f"Permanent ID: {identity}\nKey: private_a\nName: PRIVATE-A-NAME\nParent: <root>\nStatus: active\n")
    assert main(["categories", "show", "private_a"]) == 0
    assert capsys.readouterr() == (render_categories(views[0]), "")
    _, previews = invoke(ctx, "move", selector=identity, parent="finance")
    saved(ctx, previews[0])
    _, detail = invoke(ctx, "show", selector="private_a")
    assert detail[0].parent == "finance"
    _, previews = invoke(ctx, "retire", selector="private_a")
    saved(ctx, previews[0])
    result, detail = invoke(ctx, "show", selector="private_a")
    assert result.disposition is QueryDisposition.COMPLETED and detail[0].status == "retired"
    _, listing = invoke(ctx, "list")
    assert "  |-- PRIVATE-A-NAME" in render_categories(listing[0])
    assert "private_a (retired; unavailable for learning)" in render_categories(listing[0])
    before = ctx.catalog.read_bytes()
    result, views = invoke(ctx, "rename", selector="private_a", name="Changed")
    # Existing category editing permits rename of retired records; learning
    # eligibility remains retired. This boundary must preserve that behavior.
    assert result.disposition is QueryDisposition.COMPLETED
    assert views[0].after.status == "retired" and ctx.catalog.read_bytes() == before


@pytest.mark.parametrize("operation", ["add", "rename", "move", "retire"])
@pytest.mark.parametrize("selector_kind", ["key", "identity"])
def test_all_previews_saves_output_fingerprint_and_confirmation(ctx, monkeypatch, capsys, operation, selector_kind):
    monkeypatch.setattr("dam.category_operations.new_category_id", lambda _: CAT_A)
    identity = CAT_A if operation == "add" else create(ctx)
    selector = "private_a" if selector_kind == "key" else identity
    values = ({"key": "private_a", "name": "PRIVATE-A-NAME", "parent": "finance"} if operation == "add" else
              {"selector": selector, **({"name": "Renamed & Private"} if operation == "rename" else
                                       {"parent": "finance"} if operation == "move" else {})})
    before = ctx.catalog.read_bytes() if ctx.catalog.exists() else None
    result, views = invoke(ctx, operation, **values)
    view = views[0]
    expected = propose_change(ctx.config, load_catalog(ctx.catalog), operation, **values,
        generated_id=identity if operation == "add" else None,
        learned_rules_path=ctx.learned if operation == "retire" else None)
    assert view.fingerprint == expected.fingerprint
    assert render_categories(view).startswith(render_change(expected))
    again, repeated = invoke(ctx, operation, **values)
    assert result == again == CategoriesControlResult(QueryDisposition.COMPLETED)
    assert repeated[0] == view
    assert (ctx.catalog.read_bytes() if ctx.catalog.exists() else None) == before
    command = save_command(view)
    assert command[command.index("--confirm-fingerprint") + 1] == view.fingerprint
    assert command[command.index("--catalog-file") + 1] == str(ctx.catalog)
    assert main(command) == 0
    assert capsys.readouterr() == (render_change(expected, saved=True), "")
    record = next(row for row in load_catalog(ctx.catalog).records if row.permanent_id == identity)
    assert record.key == "private_a"
    assert not ctx.learned.exists()
    assert MAX_INITIAL_GMAIL_LIMIT == 10


@pytest.mark.parametrize("case", ["wrong_fingerprint", "changed_name", "missing_fingerprint", "fingerprint_without_save",
                                  "missing_id", "wrong_id", "retire_missing", "retire_wrong", "stale_catalog"])
def test_confirmation_rejections_preserve_catalog(ctx, monkeypatch, case):
    identity = create(ctx, monkeypatch=monkeypatch)
    operation = "add" if case in ("missing_id", "wrong_id") else "retire" if case.startswith("retire") else "rename"
    values = {"key": "private_b", "name": "Private B"} if operation == "add" else {
        "selector": "private_a", **({"name": "New name"} if operation == "rename" else {})}
    if operation == "add":
        monkeypatch.setattr("dam.category_operations.new_category_id", lambda _: CAT_B)
    _, views = invoke(ctx, operation, **values)
    view = views[0]
    confirmations = {"save": True, "confirm_fingerprint": view.fingerprint}
    if operation == "add":
        confirmations["confirm_id"] = view.after.permanent_id
    if operation == "retire":
        confirmations["confirm_retire"] = identity
    if case == "wrong_fingerprint":
        confirmations["confirm_fingerprint"] = "0" * 64
    elif case == "changed_name":
        values["name"] = "Different name"
    elif case == "missing_fingerprint":
        confirmations["confirm_fingerprint"] = None
    elif case == "fingerprint_without_save":
        confirmations["save"] = False
    elif case == "missing_id":
        confirmations["confirm_id"] = None
    elif case == "wrong_id":
        confirmations["confirm_id"] = CAT_B if view.after.permanent_id != CAT_B else CAT_A
    elif case == "retire_missing":
        confirmations["confirm_retire"] = None
    elif case == "retire_wrong":
        confirmations["confirm_retire"] = CAT_B
    else:
        _, changed = invoke(ctx, "rename", selector="private_a", name="Intervening")
        saved(ctx, changed[0])
    before = ctx.catalog.read_bytes()
    result, diagnostic = invoke(ctx, operation, **values, **confirmations)
    assert result == CategoriesControlResult(QueryDisposition.REJECTED)
    assert render_categories(diagnostic[0]) == CHANGE_ERROR
    assert ctx.catalog.read_bytes() == before


def test_preview_never_calls_save_or_creates_private_files(ctx, monkeypatch):
    monkeypatch.setattr("dam.category_operations.save_change", forbidden)
    assert invoke(ctx, "add", key="private_a", name="Private A")[0].disposition is QueryDisposition.COMPLETED
    assert not ctx.catalog.parent.exists()


@pytest.mark.parametrize("kind", ["children", "configured_rules", "learned_rules"])
def test_reference_protections_and_exact_diagnostics(ctx, capsys, kind):
    if kind == "children":
        selector = "it_development"
        expected = "DAM categories: move active children before retirement: employment. No change saved.\n"
    elif kind == "configured_rules":
        selector = "accounts_security"
        expected = "DAM categories: resolve active rule references before retirement: synthetic_security_record. No change saved.\n"
    else:
        selector = "private_a"
        create(ctx)
        effective = load_config(ctx.directory, category_catalog_path=ctx.catalog)
        source = MessageMetadata(account_id="synthetic", message_id="sample", sender="a@example.invalid",
            subject="Synthetic", received_at=datetime(2026, 9, 15, tzinfo=timezone.utc), label_ids=("INBOX",))
        candidate = propose_classification_rule(source, selector, effective, as_of=source.received_at)
        save_classification_rule(candidate, effective, ctx.learned, expected_fingerprint=candidate.fingerprint)
        expected = ("DAM categories: resolve active rule references before retirement: " +
                    candidate.rule.id + ". No change saved.\n")
    catalog = ctx.catalog.read_bytes() if ctx.catalog.exists() else None
    learned = ctx.learned.read_bytes() if ctx.learned.exists() else None
    assert main(["categories", "--learned-rules-file", str(ctx.learned), "retire", selector]) == 2
    assert capsys.readouterr() == ("", expected)
    assert (ctx.catalog.read_bytes() if ctx.catalog.exists() else None) == catalog
    assert (ctx.learned.read_bytes() if ctx.learned.exists() else None) == learned


@pytest.mark.parametrize("checkpoint", ["replace", "after_replace"])
def test_atomic_persistence_failure_and_explicit_retry(ctx, monkeypatch, checkpoint):
    create(ctx)
    before = ctx.catalog.read_bytes()
    _, previews = invoke(ctx, "rename", selector="private_a", name="New Name")
    import dam.categories as categories
    replace_file, fsync = categories.os.replace, categories.os.fsync
    def fail_replace(*args):
        raise OSError("PRIVATE-REPLACE")
    calls = 0
    def fail_fsync(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("PRIVATE-AFTER-REPLACE")
        return fsync(*args)
    with monkeypatch.context() as patch:
        patch.setattr(categories.os, "replace" if checkpoint == "replace" else "fsync",
                      fail_replace if checkpoint == "replace" else fail_fsync)
        result, views = saved(ctx, previews[0])
    assert result.disposition is QueryDisposition.REJECTED
    assert render_categories(views[0]) == CHANGE_ERROR
    assert not tuple(ctx.catalog.parent.glob(".categories-*.tmp"))
    if checkpoint == "replace":
        assert ctx.catalog.read_bytes() == before
        assert saved(ctx, previews[0])[0].disposition is QueryDisposition.COMPLETED
    else:
        assert ctx.catalog.read_bytes() != before
        after = ctx.catalog.read_bytes()
        assert saved(ctx, previews[0])[0].disposition is QueryDisposition.REJECTED
        assert ctx.catalog.read_bytes() == after
    assert next(row for row in load_catalog(ctx.catalog).records if row.key == "private_a").name == "New Name"


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError])
def test_writer_failure_keeps_completion_no_repeat_and_releases_payload(ctx, monkeypatch, capsys, error):
    _, previews = invoke(ctx, "add", key="private_a", name="Private A")
    original_save = save_change
    calls, refs = [], []
    def save_once(*args, **kwargs):
        calls.append(True)
        return original_save(*args, **kwargs)
    monkeypatch.setattr("dam.category_operations.save_change", save_once)
    def writer(view):
        refs.append(weakref.ref(view))
        assert len(load_catalog(ctx.catalog).records) == 1
        raise error("PRIVATE-WRITER")
    result, _ = saved(ctx, previews[0], writer)
    assert result == CategoriesControlResult(QueryDisposition.COMPLETED, presentation_failed=True)
    assert calls == [True]
    before = ctx.catalog.read_bytes()
    monkeypatch.setattr("dam.cli._write_categories", writer)
    # Explicit repeat has existing rejection semantics, no second save.
    assert main(save_command(previews[0])) == 1
    assert capsys.readouterr() == ("", DISPLAY_ERROR)
    assert calls == [True] and ctx.catalog.read_bytes() == before
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_cli_writer_failure_on_initial_save(ctx, monkeypatch, capsys):
    _, previews = invoke(ctx, "add", key="private_a", name="Private A")
    calls = []
    def save_once(*args, **kwargs):
        calls.append(True)
        return save_change(*args, **kwargs)
    def writer(view):
        raise RuntimeError("PRIVATE-WRITER")
    monkeypatch.setattr("dam.category_operations.save_change", save_once)
    monkeypatch.setattr("dam.cli._write_categories", writer)
    assert main(save_command(previews[0])) == 1
    assert capsys.readouterr() == ("", DISPLAY_ERROR)
    assert calls == [True] and len(load_catalog(ctx.catalog).records) == 1


@pytest.mark.parametrize("error,diagnostic,disposition", [
    (CategoryError, CHANGE_ERROR, QueryDisposition.REJECTED),
    (LearningError, CONFIG_ERROR, QueryDisposition.REJECTED),
    (ConfigurationError, CONFIG_ERROR, QueryDisposition.REJECTED),
    (OSError, CONFIG_ERROR, QueryDisposition.REJECTED),
    (RuntimeError, INTERNAL_ERROR, QueryDisposition.FAILED),
])
def test_sanitized_errors_control_and_cli_exit(ctx, monkeypatch, capsys, caplog, error, diagnostic, disposition):
    def fail(*args, **kwargs):
        raise error("PRIVATE-ERROR-SENTINEL")
    monkeypatch.setattr("dam.category_operations.load_config", fail)
    result, views = invoke(ctx, "list")
    assert result == CategoriesControlResult(disposition)
    assert render_categories(views[0]) == diagnostic
    assert main(["categories"]) == (1 if disposition is QueryDisposition.FAILED else 2)
    assert capsys.readouterr() == ("", diagnostic)
    assert "PRIVATE" not in repr(result) + repr(views) + caplog.text


def test_control_minimal_input_validation_and_no_terminal_access(ctx, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result, views = invoke(ctx, "list")
    assert [field.name for field in fields(result)] == ["disposition", "presentation_failed"]
    assert list(leaves(result)) == [QueryDisposition.COMPLETED, False]
    assert isinstance(views[0], CategoryListing)
    monkeypatch.setattr("dam.category_operations.load_config", forbidden)
    invalid = operate_categories({"PRIVATE": "namespace"}, present=lambda _: None)
    assert invalid.disposition is QueryDisposition.REJECTED
    request = CategoriesRequest(CategoryOperation.LIST)
    for field in fields(request):
        with pytest.raises(TypeError):
            replace(request, **{field.name: {"PRIVATE": "object"}})
    for values in ({"operation": CategoryOperation.SHOW},
                   {"operation": CategoryOperation.LIST, "save": True},
                   {"operation": CategoryOperation.ADD},
                   {"operation": CategoryOperation.RENAME, "selector": "a"}):
        with pytest.raises(ValueError):
            CategoriesRequest(**values)


def test_writer_has_no_domain_or_repository_capabilities_and_contracts_are_immutable(ctx, monkeypatch):
    create(ctx)
    _, listing = invoke(ctx, "list")
    _, detail = invoke(ctx, "show", selector="private_a")
    _, change = invoke(ctx, "rename", selector="private_a", name="New Name")
    contracts = [listing[0], listing[0].rows[0], detail[0], change[0], change[0].confirmation,
                 CategoryDiagnostic(CategoryDiagnosticCode.INVALID_CONFIG)]
    for target in ("dam.category_operations.load_config", "dam.category_operations.load_catalog",
                   "dam.categories.load_catalog", "dam.learning.load_learned_rules",
                   "dam.category_operations.propose_change", "dam.category_operations.save_change"):
        monkeypatch.setattr(target, forbidden)
    for view in (listing[0], detail[0], change[0]):
        assert render_categories(view)
        assert all(type(value) in (str, int, bool, type(None)) for value in leaves(view))
    for value in (*contracts, CategoriesRequest(CategoryOperation.LIST),
                  CategoriesControlResult(QueryDisposition.COMPLETED)):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        for field in fields(value):
            with pytest.raises(TypeError):
                replace(value, **{field.name: {"PRIVATE": "domain object"}})
    for broad in (ctx.config, load_catalog, SimpleNamespace(rows=[])):
        with pytest.raises(TypeError, match="Unsupported"):
            render_categories(broad)


def test_cross_category_and_teaching_display_isolation(ctx, monkeypatch):
    create(ctx, monkeypatch=monkeypatch)
    create(ctx, key="private_b", name="PRIVATE-B-NAME", monkeypatch=monkeypatch)
    refs, outputs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_categories(view))
    invoke(ctx, "show", sink, selector="private_a")
    invoke(ctx, "show", sink, selector="private_b")
    invoke(ctx, "rename", sink, selector="private_b", name="PRIVATE-B-NEW")
    teaching = TeachingStatus("TEACH-other", "completed", 1, 1, 0, "OTHER-CONFIG")
    assert "PRIVATE" not in render_teaching(teaching)
    assert "PRIVATE-A-NAME" in outputs[0] and all("PRIVATE-A-NAME" not in out for out in outputs[1:])
    assert "TEACH-other" not in "".join(outputs)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert render_categories.__dict__ == {} and render_categories.__closure__ is None


def test_confirmation_is_structured_and_safely_quoted(ctx, tmp_path, monkeypatch):
    quoted_home = tmp_path / "home ' $() ` space"
    catalog = quoted_home / ".config" / "dam" / "categories.yaml"
    learned = catalog.with_name("learned-rules.yaml")
    name = "Private ' name; $(no-command) `literal`"
    views = []
    request = CategoriesRequest(CategoryOperation.ADD, key="private_a", name=name,
        parent="finance", catalog_file=str(catalog), learned_rules_file=str(learned))
    assert operate_categories(request, present=views.append).disposition is QueryDisposition.COMPLETED
    confirmation = views[0].confirmation
    assert type(confirmation) is CategoryConfirmation and confirmation.name == name
    command = save_command(views[0])
    assert command[command.index("--name") + 1] == name
    assert command[command.index("--catalog-file") + 1] == str(catalog)
    assert command[command.index("--learned-rules-file") + 1] == str(learned)
    assert not catalog.parent.exists()
    assert main(command) == 0
    assert load_catalog(catalog).records[0].name == name


def test_post_completion_projection_failure_and_interruption(ctx, monkeypatch):
    _, previews = invoke(ctx, "add", key="private_a", name="Private A")
    def fail(*args):
        raise ValueError("PRIVATE-PROJECTION")
    with monkeypatch.context() as patch:
        patch.setattr("dam.category_operations.CategoryChangeDisplay", fail)
        assert saved(ctx, previews[0], forbidden)[0] == CategoriesControlResult(QueryDisposition.COMPLETED, True)
    assert len(load_catalog(ctx.catalog).records) == 1
    _, previews = invoke(ctx, "rename", selector="private_a", name="Renamed")
    refs = []
    def interrupt(view):
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        saved(ctx, previews[0], interrupt)
    assert load_catalog(ctx.catalog).records[0].name == "Renamed"
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_locked_save_revalidates_intervening_change(ctx, monkeypatch):
    create(ctx)
    _, previews = invoke(ctx, "rename", selector="private_a", name="Requested")
    expected = propose_change(ctx.config, load_catalog(ctx.catalog), "rename",
                              selector="private_a", name="Intervening")
    calls = []
    def concurrent_save(*args, **kwargs):
        calls.append(True)
        save_change(ctx.catalog, ctx.config, expected, expected_fingerprint=expected.fingerprint)
        return save_change(*args, **kwargs)
    monkeypatch.setattr("dam.category_operations.save_change", concurrent_save)
    result, views = saved(ctx, previews[0])
    assert result.disposition is QueryDisposition.REJECTED
    assert calls == [True] and render_categories(views[0]) == CHANGE_ERROR
    assert load_catalog(ctx.catalog).records[0].name == "Intervening"


@pytest.mark.parametrize("case", ["unknown", "corrupt", "duplicate_key"])
def test_query_and_identity_errors_preserve_safe_diagnostics(ctx, capsys, case):
    if case == "corrupt":
        create(ctx)
        ctx.catalog.write_text("records: [PRIVATE-CORRUPT")
        command = ["categories"]
    elif case == "duplicate_key":
        command = ["categories", "add", "--key", "finance", "--name", "Duplicate"]
    else:
        command = ["categories", "show", "PRIVATE-UNKNOWN"]
    before = ctx.catalog.read_bytes() if ctx.catalog.exists() else None
    assert main(command) == 2
    assert capsys.readouterr() == ("", CHANGE_ERROR)
    assert (ctx.catalog.read_bytes() if ctx.catalog.exists() else None) == before


def test_cli_only_supplies_explicit_input_and_terminal_sink(ctx, monkeypatch, capsys):
    requests = []
    operation = operate_categories
    def invoke_once(request, *, present):
        requests.append(request)
        return operation(request, present=present)
    monkeypatch.setattr("dam.cli.operate_categories", invoke_once)
    monkeypatch.setattr("dam.cli.load_config", forbidden)
    monkeypatch.setattr("dam.cli.resolve_category", forbidden)
    assert main(["categories", "add", "--key", "private_a", "--name", "Private A"]) == 0
    assert len(requests) == 1 and type(requests[0]) is CategoriesRequest
    command = shlex.split(capsys.readouterr().out.split("Save command:\n", 1)[1].strip())[1:]
    assert main(command) == 0
    assert len(requests) == 2 and requests[1].save is True
    assert "Category add: saved" in capsys.readouterr().out
    assert len(load_catalog(ctx.catalog).records) == 1
