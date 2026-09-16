"""Argparse entry points remain synthetic and read-only with or without flag."""

from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dam.cli import main
from dam.scan import ScanInputError


def command(*args, home=None, module=True):
    env = os.environ.copy()
    if home is not None:
        env["HOME"] = str(home)
    launch = [sys.executable, "-m", "dam"] if module else [str(Path(sys.executable).parent / "dam")]
    return subprocess.run([*launch, *args], capture_output=True, text=True, env=env)


def test_console_entry_point_metadata():
    entries = distribution("ducky-archive-manager").entry_points
    assert any(entry.name == "dam" and entry.value == "dam.cli:main" for entry in entries)


def test_module_and_console_help():
    for module in (True, False):
        result = command("--help", module=module)
        assert result.returncode == 0
        assert "Ducky Archive Manager" in result.stdout
        assert "scan" in result.stdout
        detail = command("scan", "--help", module=module)
        assert detail.returncode == 0
        assert "synthetic" in detail.stdout.lower()
        assert "read-only" in detail.stdout.lower()
        assert "--dry-run" in detail.stdout


def test_flag_and_omission_are_both_read_only():
    for arguments in (("scan", "--limit", "2"), ("scan", "--limit", "2", "--dry-run")):
        result = command(*arguments)
        assert result.returncode == 0, result.stderr
        assert "Observed: 2" in result.stdout
        assert "Proposals: archive=1, no_action=1" in result.stdout
        assert "Executed Gmail actions: 0" in result.stdout
        assert "actual Inbox after: not observed" in result.stdout
        assert "authority=false; executable=false" in result.stdout
        assert "applied" not in result.stdout.lower()


def test_default_cli_limit_comes_from_settings():
    result = command("scan")
    assert result.returncode == 0, result.stderr
    assert "Scope: INBOX; limit 100" in result.stdout
    assert "Executed Gmail actions: 0" in result.stdout


@pytest.mark.parametrize("arguments", [
    ("scan", "--limit", "0"), ("scan", "--limit", "-1"),
    ("scan", "--limit", "abc"), ("scan", "--limit", "10001"),
    ("scan", "--execute"), ("scan", "--apply"), ("scan", "--write"),
    ("scan", "--delete"), ("scan", "--trash"), ("scan", "--live"),
])
def test_invalid_or_unsupported_arguments_fail(arguments):
    result = command(*arguments)
    assert result.returncode != 0
    assert "usage:" in result.stderr
    assert "Executed Gmail actions" not in result.stdout


def test_expected_and_internal_failures_are_nonzero_without_traceback(monkeypatch, capsys):
    def invalid(**_):
        raise ScanInputError("private synthetic detail")
    monkeypatch.setattr("dam.cli.run_synthetic_scan", invalid)
    assert main(["scan"]) == 2
    captured = capsys.readouterr()
    assert "private synthetic detail" not in captured.err
    assert "Traceback" not in captured.err
    def broken(**_):
        raise RuntimeError("private internal detail")
    monkeypatch.setattr("dam.cli.run_synthetic_scan", broken)
    assert main(["scan"]) == 1
    captured = capsys.readouterr()
    assert "private internal detail" not in captured.err
    assert "Traceback" not in captured.err
