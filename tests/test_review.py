"""Step 12.1 uses synthetic Gmail-shaped responses and temporary OAuth files."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import webbrowser

import pytest

from dam.auth import AuthError, AuthPaths
from dam.cli import main
from dam.gmail import GMAIL_READONLY_SCOPE
from dam.learning import load_learned_rules
from dam.review import ReviewScopeError, render_review, review_gmail_message
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, run_gmail_scan

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
MILLISECONDS = "1789560000000"
TARGET = "synthetic-review-001"
OTHER = "synthetic-review-002"


@pytest.fixture(autouse=True)
def forbid_real_oauth_and_network(monkeypatch):
    real = AuthPaths.for_home()
    protected = {real.client_secret, real.token}
    original_lstat, original_open, original_replace = Path.lstat, os.open, os.replace
    def lstat(path, *args, **kwargs):
        if path in protected:
            raise AssertionError("real OAuth file access")
        return original_lstat(path, *args, **kwargs)
    def open_file(path, *args, **kwargs):
        if Path(path) in protected:
            raise AssertionError("real OAuth file access")
        return original_open(path, *args, **kwargs)
    def replace(source, destination, *args, **kwargs):
        if Path(source) in protected or Path(destination) in protected:
            raise AssertionError("real OAuth file access")
        return original_replace(source, destination, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("network or browser access")
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)


@dataclass
class Credential:
    scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)
    granted_scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)
    valid: bool = True
    expired: bool = False
    refresh_token: str | None = None


class Backend:
    def __init__(self):
        self.decode_calls = 0
    def decode_token(self, document):
        self.decode_calls += 1
        return Credential(tuple(document["scopes"]), tuple(document["granted_scopes"]))
    def authorize(self, *_):
        raise AssertionError("unexpected authorization")
    def refresh(self, *_):
        raise AssertionError("unexpected refresh")
    def encode_token(self, *_):
        raise AssertionError("unexpected token write")


class Request:
    def __init__(self, response):
        self.response = response
    def execute(self):
        return self.response


class Messages:
    def __init__(self, *, inbox=True, allow_list=False):
        self.inbox = inbox
        self.allow_list = allow_list
        self.get_calls = []
        self.list_calls = []
    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        sender = ("Example Alerts <alerts@updates.example.invalid>" if kwargs["id"] == TARGET
                  else "Other <other@other.example.invalid>")
        subject = ("Synthetic unexplained update" if kwargs["id"] == TARGET
                   else "Synthetic unrelated note")
        labels = ["INBOX", "CATEGORY_UPDATES"] if self.inbox else ["CATEGORY_UPDATES"]
        return Request({
            "id": kwargs["id"], "threadId": "synthetic-shared-thread",
            "internalDate": MILLISECONDS, "labelIds": labels,
            "payload": {"headers": [{"name": "From", "value": sender},
                                     {"name": "Subject", "value": subject}]},
            "snippet": "synthetic-sensitive-snippet",
            "body": "synthetic-sensitive-body",
            "raw": "synthetic-sensitive-raw",
            "attachments": ["synthetic-sensitive-attachment"],
        })
    def list(self, **kwargs):
        if not self.allow_list:
            raise AssertionError("review attempted message listing")
        self.list_calls.append(kwargs)
        return Request({"messages": [{"id": TARGET, "threadId": "synthetic-shared-thread"},
                                     {"id": OTHER, "threadId": "synthetic-shared-thread"}],
                        "resultSizeEstimate": 2})


class Service:
    def __init__(self, messages):
        self.messages_api = messages
    def users(self):
        return self
    def messages(self):
        return self.messages_api


@pytest.fixture
def private(tmp_path):
    paths = AuthPaths.for_home(tmp_path)
    paths.dam_directory.mkdir(parents=True, mode=0o700)
    paths.oauth_directory.mkdir(mode=0o700)
    paths.dam_directory.chmod(0o700)
    paths.oauth_directory.chmod(0o700)
    client = {"installed": {"client_id": "synthetic-client",
                            "client_secret": "synthetic-secret",
                            "auth_uri": "https://auth.example.invalid/",
                            "token_uri": "https://token.example.invalid/",
                            "redirect_uris": ["http://localhost:0/synthetic"]}}
    paths.client_secret.write_text(json.dumps(client))
    paths.client_secret.chmod(0o600)
    token = {"scopes": [GMAIL_READONLY_SCOPE], "granted_scopes": [GMAIL_READONLY_SCOPE],
             "access_token": "synthetic-token"}
    paths.token.write_text(json.dumps(token))
    paths.token.chmod(0o600)
    learned = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    return paths, learned


def get_review(private, messages=None, *, category_id=None):
    paths, learned = private
    messages = Messages() if messages is None else messages
    backend = Backend()
    factories = []
    def factory(*args, **kwargs):
        factories.append((args, kwargs))
        return Service(messages)
    result = review_gmail_message(TARGET, category_id=category_id, paths=paths,
                                  backend=backend, service_factory=factory,
                                  learned_rules_path=learned, as_of=NOW)
    return result, messages, backend, factories


def test_one_explicit_metadata_get_and_human_review_display(private):
    result, messages, backend, factories = get_review(private)
    assert len(messages.get_calls) == 1 and not messages.list_calls
    call = messages.get_calls[0]
    assert call["id"] == TARGET and call["userId"] == "me"
    assert call["format"] == "metadata" and call["metadataHeaders"] == ["From", "Subject"]
    assert all(name not in call["fields"] for name in ("body", "raw", "snippet", "attachment"))
    assert backend.decode_calls == 1 and len(factories) == 1
    assert factories[0][0] == ("gmail", "v1")
    assert result.message.thread_id == "synthetic-shared-thread"
    assert result.classification.requires_review
    assert not result.authority_established and not result.executable
    assert result.executed_gmail_actions == 0
    assert "updates.example.invalid" not in repr(result)
    assert "Synthetic unexplained update" not in repr(result)
    shown = render_review(result)
    assert "Example Alerts <alerts@updates.example.invalid>" in shown
    assert "Synthetic unexplained update" in shown
    assert "CATEGORY_UPDATES" in shown and "Review required: yes" in shown
    assert "executed Gmail actions: 0" in shown
    assert all(secret not in shown for secret in (
        "synthetic-sensitive-snippet", "synthetic-sensitive-body", "synthetic-sensitive-raw",
        "synthetic-sensitive-attachment", "synthetic-token", "synthetic-secret"))
    for name in ("modify", "batchModify", "trash", "untrash", "delete", "batchDelete", "send"):
        assert not hasattr(messages, name)


def test_non_inbox_or_missing_labels_rejected_before_learning(private):
    paths, learned = private
    messages = Messages(inbox=False)
    with pytest.raises(ReviewScopeError, match="outside_inbox"):
        get_review(private, messages)
    assert len(messages.get_calls) == 1 and not learned.exists()


def test_review_display_redacts_url_tokens_and_control_characters(private):
    result, _, _, _ = get_review(private)
    altered = replace(result, message=result.message.model_copy(update={
        "subject": "Synthetic notice https://example.invalid/unsubscribe?token=synthetic-secret\x1b[31m"
    }))
    shown = render_review(altered)
    assert "[URL redacted]" in shown
    assert "synthetic-secret" not in shown
    assert "\x1b" not in shown


def test_wrong_scope_and_unknown_category_stop_before_gmail_read(private):
    paths, learned = private
    messages = Messages()
    token = json.loads(paths.token.read_text())
    token["granted_scopes"] = ["https://www.googleapis.com/auth/gmail.modify"]
    paths.token.write_text(json.dumps(token))
    paths.token.chmod(0o600)
    with pytest.raises(AuthError, match="scope_mismatch"):
        get_review(private, messages)
    assert not messages.get_calls
    with pytest.raises(ReviewScopeError, match="unknown_category"):
        get_review(private, messages, category_id="typo")
    assert not messages.get_calls and not learned.exists()


def test_cli_review_requires_gmail_and_id_without_access(private, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Gmail access attempted")
    monkeypatch.setattr("dam.cli.review_gmail_message", forbidden)
    assert main(["review", "--message-id", TARGET]) == 2
    assert "requires explicit --gmail" in capsys.readouterr().err
    with pytest.raises(SystemExit) as missing:
        main(["review", "--gmail"])
    assert missing.value.code == 2
    assert main(["learn", "--message-id", TARGET, "--category", "promotions",
                 "--learned-rules-file", str(private[1])]) == 2
    assert not private[1].exists()


def test_cli_review_learn_preview_save_and_later_mock_scan(private, monkeypatch, capsys):
    paths, learned = private
    messages = Messages()
    backend = Backend()
    def injected(message_id, **kwargs):
        return review_gmail_message(message_id, paths=paths, backend=backend,
                                    service_factory=lambda *a, **k: Service(messages),
                                    as_of=NOW, **kwargs)
    monkeypatch.setattr("dam.cli.review_gmail_message", injected)
    assert main(["review", "--gmail", "--message-id", TARGET,
                 "--learned-rules-file", str(learned)]) == 0
    review_output = capsys.readouterr().out
    assert "Synthetic unexplained update" in review_output
    assert "Review required: yes" in review_output
    assert not learned.exists()
    arguments = ["learn", "--gmail", "--message-id", TARGET, "--category", "promotions",
                 "--learned-rules-file", str(learned)]
    assert main(arguments) == 0
    preview = capsys.readouterr().out
    assert "Status: proposed" in preview and "Exact sender match: alerts@updates.example.invalid" in preview
    assert "Evidence label_ids: not selected; observed=CATEGORY_UPDATES" in preview
    assert not learned.exists()
    fingerprint = next(line.split(": ", 1)[1] for line in preview.splitlines()
                       if line.startswith("Candidate fingerprint:"))
    assert main([*arguments, "--save", "--confirm-fingerprint", fingerprint]) == 0
    assert "Status: saved" in capsys.readouterr().out
    assert len(load_learned_rules(learned).records) == 1
    assert len(messages.get_calls) == 3 and not messages.list_calls
    later_messages = Messages(allow_list=True)
    later = run_gmail_scan(limit=2, paths=paths, backend=Backend(),
                           service_factory=lambda *a, **k: Service(later_messages),
                           learned_rules_path=learned, as_of=NOW, run_id="synthetic-later")
    entries = {entry.message_id: entry for entry in later.preview.entries}
    assert entries[TARGET].category_ids == ("promotions",)
    assert entries[OTHER].category_ids == ()
    assert all(not item.authority_established and not item.executable for item in entries.values())
    assert later.preview.statistics.executed_gmail_actions == 0
    assert len(later_messages.get_calls) == 2


def test_cli_non_inbox_and_unknown_category_never_save(private, monkeypatch, capsys):
    paths, learned = private
    messages = Messages(inbox=False)
    def injected(message_id, **kwargs):
        return review_gmail_message(message_id, paths=paths, backend=Backend(),
                                    service_factory=lambda *a, **k: Service(messages),
                                    as_of=NOW, **kwargs)
    monkeypatch.setattr("dam.cli.review_gmail_message", injected)
    args = ["learn", "--gmail", "--message-id", TARGET, "--category", "promotions",
            "--learned-rules-file", str(learned)]
    assert main(args) == 2
    assert "outside the current Inbox" in capsys.readouterr().err
    assert len(messages.get_calls) == 1 and not learned.exists()
    assert main(["learn", "--gmail", "--message-id", TARGET, "--category", "typo",
                 "--learned-rules-file", str(learned)]) == 2
    assert len(messages.get_calls) == 1 and not learned.exists()


def test_normal_gmail_scan_renderer_still_hides_values(private, monkeypatch, capsys):
    paths, learned = private
    messages = Messages(allow_list=True)
    result = run_gmail_scan(limit=2, paths=paths, backend=Backend(),
                            service_factory=lambda *a, **k: Service(messages),
                            as_of=NOW, run_id="synthetic-privacy")
    monkeypatch.setattr("dam.cli.run_gmail_scan", lambda *, limit: result)
    assert main(["scan", "--gmail", "--limit", "2", "--dry-run"]) == 0
    shown = capsys.readouterr().out
    assert "sender=present; subject=present" in shown
    assert "alerts@updates.example.invalid" not in shown
    assert "Synthetic unexplained update" not in shown
    assert "Executed Gmail actions: 0" in shown
    assert MAX_INITIAL_GMAIL_LIMIT == 10
    assert main(["scan", "--gmail", "--limit", "11"]) == 2
