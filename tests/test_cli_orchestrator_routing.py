"""Every application CLI variant enters the same direct controller once."""

import ast
from dataclasses import fields
import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest

import dam.cli as cli
import dam.orchestrator as controller
from dam.invocation import InvocationFailure, InvocationMode, InvocationRequest, OperationSelector, PresentationPorts
from dam.scan_operations import ScanControlResult
from dam.teaching_queries import QueryControlResult, QueryDisposition


ROUTES = [
    (["scan"], OperationSelector.SCAN),
    (["scan", "--dry-run", "--limit", "10000"], OperationSelector.SCAN),
    (["scan", "--gmail"], OperationSelector.SCAN),
    (["scan", "--gmail", "--limit", "10", "--dry-run", "--use-learned-rules"], OperationSelector.SCAN),
    (["scan", "--gmail", "--record-locally"], OperationSelector.SCAN),
    (["scan", "--gmail", "--record-locally", "--dry-run", "--category-catalog-file", "original path"], OperationSelector.SCAN),
    (["review", "--message-id", "PRIVATE"], OperationSelector.REVIEW),
    (["review", "--gmail", "--message-id", "PRIVATE"], OperationSelector.REVIEW),
    (["teach", "list"], OperationSelector.TEACH_QUERY),
    (["teach", "show", "CWQ-private"], OperationSelector.TEACH_QUERY),
    (["teach", "status", "TEACH-private"], OperationSelector.TEACH_QUERY),
    (["teach", "preview", "CWQ-private", "--category", "finance"], OperationSelector.TEACH_PREVIEW),
    (["teach", "preview", "CWQ-private", "--category", "finance", "--item-id", "ITEM-private"], OperationSelector.TEACH_PREVIEW),
    (["teach", "save", "CWQ-private", "--category", "finance", "--confirm-fingerprint", "PRIVATE"], OperationSelector.TEACH_SAVE),
    (["teach", "save", "CWQ-private", "--category", "finance", "--item-id", "ITEM-private", "--confirm-fingerprint", "PRIVATE"], OperationSelector.TEACH_SAVE),
    (["teach", "resume", "TEACH-private"], OperationSelector.TEACH_RESUME),
    (["categories"], OperationSelector.CATEGORIES),
    (["categories", "show", "finance"], OperationSelector.CATEGORIES),
]
for gmail in (False, True):
    for save in (False, True):
        ROUTES.append((["learn", "--message-id", "PRIVATE", "--category", "finance",
                        *(["--gmail"] if gmail else []),
                        *(["--save", "--confirm-fingerprint", "PRIVATE"] if save else [])], OperationSelector.LEARN))
for operation, operands in (("add", ["--key", "private", "--name", "Original ' Name"]),
                            ("rename", ["private", "--name", "Original Name"]),
                            ("move", ["private", "--parent", "finance"]), ("retire", ["private"])):
    for save in (False, True):
        extra = (["--save", "--confirm-fingerprint", "PRIVATE", *(["--confirm-id", "CAT-private"] if operation == "add" else
                 ["--confirm-retire", "CAT-private"] if operation == "retire" else [])] if save else [])
        ROUTES.append((["categories", operation, *operands, *extra], OperationSelector.CATEGORIES))


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected initialization or execution")


@pytest.mark.parametrize("arguments,selector", ROUTES)
def test_every_command_variant_routes_once_to_controller(monkeypatch, arguments, selector):
    calls = []
    def routed(request, *, presentation):
        assert type(request) is InvocationRequest and request.mode is InvocationMode.DIRECT
        assert request.operation is selector and type(presentation) is PresentationPorts
        calls.append(request)
        return QueryControlResult(QueryDisposition.COMPLETED)
    monkeypatch.setattr(cli, "execute", routed)
    for target in ("dam.config.load_config", "dam.storage.Storage.open", "dam.auth.authenticate"):
        monkeypatch.setattr(target, forbidden)
    assert cli.main(arguments) == 0 and len(calls) == 1
    if selector is OperationSelector.SCAN:
        assert calls[0].inputs.gmail is ("--gmail" in arguments)
        assert calls[0].inputs.record_locally is ("--record-locally" in arguments)
        assert calls[0].inputs.dry_run is ("--dry-run" in arguments)
    elif selector is OperationSelector.LEARN:
        assert calls[0].inputs.gmail is ("--gmail" in arguments)
        assert calls[0].inputs.save is ("--save" in arguments)


@pytest.mark.parametrize("arguments,code", [
    (["--help"], 0), (["scan", "--help"], 0), (["teach", "--help"], 0),
    ([], 2), (["review", "--gmail"], 2), (["teach", "save", "CWQ", "--category", "finance"], 2),
    (["scan", "--limit", "0"], 2), (["scan", "--limit", "10001"], 2),
    (["scan", "--limit", "bad"], 2), (["unknown"], 2),
])
def test_help_and_syntax_errors_never_enter_controller_or_initialize(monkeypatch, capsys, arguments, code):
    monkeypatch.setattr(cli, "execute", forbidden)
    for target in ("dam.config.load_config", "dam.storage.Storage.open", "dam.auth.authenticate"):
        monkeypatch.setattr(target, forbidden)
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)
    assert raised.value.code == code
    assert "Unexpected initialization" not in capsys.readouterr().err


@pytest.mark.parametrize("arguments,selector,text", [
    (["scan", "--record-locally"], OperationSelector.SCAN, "requires explicit --gmail"),
    (["scan", "--gmail", "--limit", "11"], OperationSelector.SCAN, "limit must be from 1 to 10"),
    (["scan", "--gmail", "--record-locally", "--limit", "11"], OperationSelector.SCAN, "limit must be from 1 to 10"),
    (["review", "--message-id", "PRIVATE"], OperationSelector.REVIEW, "requires explicit --gmail"),
    (["learn", "--message-id", "PRIVATE", "--category", "finance", "--save"], OperationSelector.LEARN, "--save requires --confirm-fingerprint"),
])
def test_semantic_rejections_enter_real_controller_selected_operation_once(monkeypatch, capsys, arguments, selector, text):
    real_execute = controller.execute
    calls, operations = [], []
    name = {OperationSelector.SCAN: "scan", OperationSelector.REVIEW: "review_message", OperationSelector.LEARN: "learn"}[selector]
    operation = getattr(controller, name)
    def selected(*args, **kwargs):
        operations.append(True)
        return operation(*args, **kwargs)
    def routed(request, **kwargs):
        calls.append(request.operation)
        return real_execute(request, **kwargs)
    monkeypatch.setattr(controller, name, selected)
    monkeypatch.setattr(cli, "execute", routed)
    for target in ("dam.auth.authenticate", "dam.scan.authenticate", "dam.durable_gmail.authenticate",
                   "dam.review.authenticate", "dam.storage.Storage.open", "dam.config.load_config"):
        monkeypatch.setattr(target, forbidden)
    assert cli.main(arguments) == 2
    assert calls == [selector] and operations == [True]
    assert text in capsys.readouterr().err


def test_offline_scan_categories_and_teaching_route_without_authentication(tmp_path, monkeypatch, capsys):
    from dam.config import load_config
    from dam.models import Settings
    from dam.scan import default_config_directory
    base = load_config(default_config_directory())
    settings = Settings.model_validate({"state": {"database_path": str(tmp_path / "state" / "dam.db")}})
    monkeypatch.setattr("dam.operation_bindings.load_config", lambda *_args: base.model_copy(update={"settings": settings}))
    catalog = tmp_path / ".config" / "dam" / "categories.yaml"
    monkeypatch.setattr("dam.category_operations.default_catalog_path", lambda: catalog)
    monkeypatch.setattr("dam.scan_operations.default_catalog_path", lambda: catalog)
    for target in ("dam.auth.authenticate", "dam.scan.authenticate", "dam.durable_gmail.authenticate", "dam.review.authenticate"):
        monkeypatch.setattr(target, forbidden)
    calls = []
    def execute(request, **kwargs):
        calls.append(request.operation)
        return controller.execute(request, **kwargs)
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.main(["scan", "--limit", "1"]) == 0
    assert "Observed: 1" in capsys.readouterr().out
    assert cli.main(["categories"]) == 0
    assert "Finance" in capsys.readouterr().out
    assert cli.main(["teach", "list"]) == 0
    assert capsys.readouterr() == ("", "")
    assert calls == [OperationSelector.SCAN, OperationSelector.CATEGORIES, OperationSelector.TEACH_QUERY]


def test_original_teaching_inputs_and_paths_are_only_constructed(monkeypatch):
    seen = []
    def execute(request, **kwargs):
        seen.append(request)
        return QueryControlResult(QueryDisposition.COMPLETED)
    monkeypatch.setattr(cli, "execute", execute)
    for command in (("list",), ("show", "CWQ-private"), ("preview", "CWQ-private", "--category", "CAT-original")):
        assert cli.main(["teach", "--learned-rules-file", "original ' path", "--category-catalog-file", "../original spelling", *command]) == 0
    assert all(value.inputs.learned_rules_file == "original ' path" and value.inputs.category_catalog_file == "../original spelling" for value in seen)
    assert seen[-1].inputs.category_selector == "CAT-original"


@pytest.mark.parametrize("disposition,code", [(QueryDisposition.COMPLETED, 0), (QueryDisposition.INCOMPLETE, 2),
                                             (QueryDisposition.REJECTED, 2), (QueryDisposition.FAILED, 1)])
def test_cli_maps_only_control_facts(monkeypatch, disposition, code):
    monkeypatch.setattr(cli, "execute", lambda *_args, **_kwargs: ScanControlResult(disposition))
    assert cli.main(["scan"]) == code


def test_controller_failure_fixed_safe_diagnostic_and_no_private_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "execute", lambda *_args, **_kwargs: InvocationFailure(QueryDisposition.FAILED))
    assert cli.main(["scan"]) == 1
    assert capsys.readouterr() == ("", "DAM invocation failed internally; inspect durable state before retrying.\n")


@pytest.mark.parametrize("module", [True, False])
def test_installed_and_module_launchers_share_controller_route(tmp_path, module):
    # Patch the actual imported controller before either launcher imports CLI.
    # No application initialization is permitted in these subprocess checks.
    (tmp_path / "sitecustomize.py").write_text('''
import sys
import dam.orchestrator as controller
from dam.scan_operations import ScanControlResult
from dam.teaching_queries import QueryDisposition

def routed(request, *, presentation):
    print("CONTROLLER:" + request.operation.value + ":" + request.mode.value)
    return ScanControlResult(QueryDisposition.COMPLETED)
controller.execute = routed

def guard(event, args):
    if event in ("sqlite3.connect", "socket.connect", "socket.__new__", "os.mkdir"):
        raise AssertionError("unexpected application initialization")
sys.addaudithook(guard)
''')
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path) + os.pathsep + str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "dam"] if module else [str(Path(sys.executable).parent / "dam")]
    result = subprocess.run([*command, "scan"], cwd=tmp_path, env=environment, text=True, capture_output=True)
    assert result.returncode == 0 and result.stdout == "CONTROLLER:scan:direct\n" and result.stderr == ""


def test_cli_has_no_application_bypass_or_configuration_composition():
    tree = ast.parse(inspect.getsource(cli))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not imported & {"scan", "learn", "review_message", "operate_categories", "query_teaching", "preview_teaching",
                           "save_teaching", "resume_teaching", "load_config", "Storage", "TeachingService",
                           "run_gmail_scan", "run_durable_gmail_scan", "run_synthetic_scan", "default_catalog_path"}
    main_tree = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = [node.func.id for node in ast.walk(main_tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert calls.count("execute") == 1
    assert set(calls) <= {"parser", "_invocation", "PresentationPorts", "_exit_code", "execute"}
    assert not hasattr(cli, "_catalog_path")
