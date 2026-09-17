"""Step 12.2 category discovery is local and uses configured category IDs."""

import os
from pathlib import Path
import socket
import webbrowser

import pytest

from dam.auth import AuthPaths
from dam.cli import main
from dam.config import load_config
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, default_config_directory


@pytest.fixture
def learned_path(tmp_path):
    return tmp_path / ".config" / "dam" / "learned-rules.yaml"


def test_categories_in_top_level_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for command in ("scan", "review", "learn", "categories"):
        assert command in output


def test_categories_list_exact_configured_ids_names_and_parent_order(capsys):
    config = load_config(default_config_directory())
    assert main(["categories"]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()[1:]
    expected = [
        f"{item.id}\t{item.name}" + (f" (parent: {item.parent_id})" if item.parent_id else "")
        for item in config.categories.categories
    ]
    assert lines == expected
    assert len(lines) == len({item.id for item in config.categories.categories})
    assert main(["categories"]) == 0
    assert capsys.readouterr().out == output


def test_every_displayed_id_is_accepted_by_learning_preview(learned_path, capsys):
    assert main(["categories"]) == 0
    lines = capsys.readouterr().out.splitlines()[1:]
    for line in lines:
        category_id = line.split("\t", 1)[0]
        assert main(["learn", "--message-id", "synthetic-004-unknown",
                     "--category", category_id,
                     "--learned-rules-file", str(learned_path)]) == 0
        shown = capsys.readouterr().out
        assert f"Human selected category: {category_id}" in shown
        assert "Status: proposed" in shown
    assert not learned_path.exists()


def test_unknown_synthetic_category_has_guidance_and_no_save(learned_path, capsys):
    result = main(["learn", "--message-id", "synthetic-004-unknown",
                   "--category", "DOES_NOT_EXIST",
                   "--learned-rules-file", str(learned_path)])
    assert result == 2
    error = capsys.readouterr().err
    assert "Unknown DAM category 'DOES_NOT_EXIST'." in error
    assert "dam categories" in error and "No rule saved" in error
    assert not learned_path.exists()


def test_unknown_real_category_rejected_before_oauth_or_gmail(learned_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("OAuth or Gmail access attempted")
    monkeypatch.setattr("dam.review.authenticate", forbidden)
    monkeypatch.setattr("dam.review.read_message", forbidden)
    result = main(["learn", "--gmail", "--message-id", "synthetic-gmail-message",
                   "--category", "DOES_NOT_EXIST",
                   "--learned-rules-file", str(learned_path)])
    assert result == 2
    assert "Run 'dam categories'" in capsys.readouterr().err
    assert not learned_path.exists()


def test_categories_never_accesses_oauth_gmail_network_or_private_files(
    learned_path, monkeypatch, capsys,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("external or private file access attempted")
    real = AuthPaths.for_home()
    protected = {real.client_secret, real.token}
    original_path_open = Path.open
    original_os_open = os.open
    def guarded_path_open(path, *args, **kwargs):
        if path in protected:
            forbidden()
        return original_path_open(path, *args, **kwargs)
    def guarded_os_open(path, *args, **kwargs):
        if Path(path) in protected:
            forbidden()
        return original_os_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    monkeypatch.setattr("dam.auth.authenticate", forbidden)
    monkeypatch.setattr("dam.cli.run_gmail_scan", forbidden)
    monkeypatch.setattr("dam.cli.review_gmail_message", forbidden)
    assert main(["categories"]) == 0
    assert "DAM categories" in capsys.readouterr().out
    assert not learned_path.exists()


def test_existing_synthetic_commands_and_gmail_limit_unchanged(capsys):
    assert main(["scan", "--limit", "2"]) == 0
    assert "Executed Gmail actions: 0" in capsys.readouterr().out
    assert MAX_INITIAL_GMAIL_LIMIT == 10
    assert main(["scan", "--gmail", "--limit", "11"]) == 2
    assert "no Gmail access attempted" in capsys.readouterr().err
