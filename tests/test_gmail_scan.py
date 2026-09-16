"""Step 11 integration uses temporary synthetic OAuth files and fake Gmail only."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from types import ModuleType
import webbrowser

import pytest

from dam.auth import AuthError, AuthPaths, GoogleAuthBackend, google_service_factory
from dam.cli import main
from dam.config import load_config
from dam.gmail import GMAIL_READONLY_SCOPE
from dam.models import Configuration, ProposedAction, Rule, RulesConfig
from dam.scan import (
    GMAIL_ACCOUNT_ID, MAX_INITIAL_GMAIL_LIMIT, ScanInputError,
    default_config_directory, run_gmail_scan,
)

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
MILLISECONDS = "1789560000000"


@pytest.fixture(autouse=True)
def forbid_real_oauth_paths(monkeypatch):
    """Guard both reads and replacement of the user's actual OAuth files."""
    real = AuthPaths.for_home()
    protected = {real.client_secret, real.token}
    original_lstat = Path.lstat
    original_os_open = os.open
    original_replace = os.replace

    def guarded_lstat(path, *args, **kwargs):
        if path in protected:
            raise AssertionError("real OAuth file access")
        return original_lstat(path, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        if Path(path) in protected:
            raise AssertionError("real OAuth file access")
        return original_os_open(path, *args, **kwargs)

    def guarded_replace(source, destination, *args, **kwargs):
        if Path(source) in protected or Path(destination) in protected:
            raise AssertionError("real OAuth file replacement")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(os, "replace", guarded_replace)


@dataclass
class Credential:
    scopes: object = (GMAIL_READONLY_SCOPE,)
    granted_scopes: object = (GMAIL_READONLY_SCOPE,)
    valid: bool = True
    expired: bool = False
    refresh_token: str | None = None


class Backend:
    def __init__(self):
        self.decode_calls = 0
        self.refresh_calls = 0
        self.authorize_calls = 0
        self.refresh_result = Credential()
        self.authorize_result = Credential()

    def decode_token(self, document):
        self.decode_calls += 1
        return Credential(tuple(document["scopes"]), tuple(document["granted_scopes"]),
                          document.get("valid", True), document.get("expired", False),
                          document.get("refresh_token"))

    def refresh(self, credential):
        self.refresh_calls += 1
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        return self.refresh_result

    def authorize(self, client_config, scopes):
        self.authorize_calls += 1
        assert scopes == (GMAIL_READONLY_SCOPE,)
        if isinstance(self.authorize_result, Exception):
            raise self.authorize_result
        return self.authorize_result

    def encode_token(self, credential):
        return json.dumps({"scopes": list(credential.scopes),
                           "granted_scopes": list(credential.granted_scopes),
                           "valid": credential.valid, "expired": credential.expired,
                           "refresh_token": credential.refresh_token,
                           "access_token": "synthetic-token"})


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Messages:
    def __init__(self, *, count=10, fail_id=None, trash_id=None):
        self.count = count
        self.fail_id = fail_id
        self.trash_id = trash_id
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        start = 0 if "pageToken" not in kwargs else int(kwargs["pageToken"].removeprefix("offset-"))
        stop = min(start + min(5, kwargs["maxResults"]), self.count)
        result = {"messages": [
            {"id": f"synthetic-gmail-{i:02d}", "threadId": "shared-thread" if i < 2 else f"thread-{i}"}
            for i in range(start, stop)], "resultSizeEstimate": self.count}
        if stop < self.count:
            result["nextPageToken"] = f"offset-{stop}"
        return Request(result)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        message_id = kwargs["id"]
        if message_id == self.fail_id:
            return Request(RuntimeError("synthetic private API failure"))
        number = int(message_id.rsplit("-", 1)[1])
        subject = ("Synthetic receipt" if number == 0 else
                   "Synthetic job alert" if number == 1 else
                   "Synthetic security notice" if number == 2 else
                   "Synthetic trash offer" if message_id == self.trash_id else
                   "Synthetic unknown note")
        return Request({"id": message_id,
                        "threadId": "shared-thread" if number < 2 else f"thread-{number}",
                        "internalDate": MILLISECONDS, "labelIds": ["INBOX"],
                        "payload": {"headers": [
                            {"name": "From", "value": "Synthetic Sender <sender@example.invalid>"},
                            {"name": "Subject", "value": subject}]}})


class Service:
    def __init__(self, messages):
        self.messages_api = messages

    def users(self):
        return self

    def messages(self):
        return self.messages_api


def private_file(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def paths(tmp_path):
    result = AuthPaths.for_home(tmp_path)
    result.dam_directory.mkdir(parents=True, mode=0o700)
    result.oauth_directory.mkdir(mode=0o700)
    result.dam_directory.chmod(0o700)
    result.oauth_directory.chmod(0o700)
    private_file(result.client_secret, {"installed": {
        "client_id": "synthetic-client-id", "client_secret": "synthetic-secret",
        "auth_uri": "https://accounts.example.invalid/auth",
        "token_uri": "https://accounts.example.invalid/token",
        "redirect_uris": ["http://localhost:0/synthetic"]}})
    return result


def token(paths, *, scopes=(GMAIL_READONLY_SCOPE,), grants=(GMAIL_READONLY_SCOPE,),
          valid=True, expired=False, refresh_token=None):
    private_file(paths.token, {"scopes": list(scopes), "granted_scopes": list(grants),
                               "access_token": "synthetic-token", "valid": valid,
                               "expired": expired, "refresh_token": refresh_token})


def scan(paths, *, backend=None, messages=None, limit=10, config_directory=None):
    backend = backend if backend is not None else Backend()
    messages = messages if messages is not None else Messages()
    factory_calls = []
    def factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return Service(messages)
    result = run_gmail_scan(limit=limit, paths=paths, backend=backend,
                            service_factory=factory, config_directory=config_directory,
                            as_of=NOW, run_id="synthetic-gmail-run")
    return result, backend, messages, factory_calls


def test_mocked_ten_message_pipeline_uses_exact_inbox_metadata(paths, monkeypatch):
    token(paths)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("real network, browser or SQLite access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    result, backend, messages, factories = scan(paths)
    assert result.source == "real_gmail_read_only" and result.persistence == "in_memory_only"
    assert result.effective_limit == MAX_INITIAL_GMAIL_LIMIT == 10
    assert result.auth_source == "existing" and backend.decode_calls == 1
    assert len(factories) == 1 and factories[0][0] == ("gmail", "v1")
    assert len(messages.list_calls) == 2 and len(messages.get_calls) == 10
    assert all(call["labelIds"] == ["INBOX"] and call["includeSpamTrash"] is False for call in messages.list_calls)
    assert all(call["format"] == "metadata" and call["metadataHeaders"] == ["From", "Subject"]
               for call in messages.get_calls)
    assert all("raw" not in call["fields"] and "body" not in call["fields"] and
               "snippet" not in call["fields"] for call in messages.get_calls)
    for name in ("modify", "batchModify", "trash", "untrash", "delete", "batchDelete", "send"):
        assert not hasattr(messages, name)
    assert result.read_result.account_id == GMAIL_ACCOUNT_ID
    assert result.read_result.observed_count == result.preview.statistics.total_messages == 10
    assert result.read_result.messages[0].received_at == NOW
    assert result.read_result.messages[0].thread_id == result.read_result.messages[1].thread_id
    assert result.read_result.messages[0].message_id != result.read_result.messages[1].message_id
    entries = {entry.message_id: entry for entry in result.preview.entries}
    assert entries["synthetic-gmail-00"].category_ids == ("finance",)
    assert entries["synthetic-gmail-00"].proposed_action == ProposedAction.NO_ACTION
    assert "protected" in entries["synthetic-gmail-00"].protection_signals
    assert entries["synthetic-gmail-01"].category_ids == ("employment_inactive",)
    assert entries["synthetic-gmail-01"].proposed_action == ProposedAction.ARCHIVE
    assert entries["synthetic-gmail-02"].requires_review
    assert all(not entry.authority_established and not entry.executable for entry in entries.values())
    assert result.preview.statistics.executed_gmail_actions == 0


def test_limit_above_ten_fails_before_auth_and_default_is_ten(paths):
    backend = Backend()
    with pytest.raises(ScanInputError, match="1 to 10"):
        run_gmail_scan(limit=11, paths=paths, backend=backend)
    assert backend.decode_calls == backend.authorize_calls == 0
    token(paths)
    result, _, _, _ = scan(paths, limit=None)
    assert result.effective_limit == 10


def test_read_failure_is_uninspected_and_coverage_partial(paths):
    token(paths)
    failed_id = "synthetic-gmail-04"
    result, _, _, _ = scan(paths, messages=Messages(fail_id=failed_id))
    assert result.read_result.observed_count == 9
    assert result.read_result.failures[0].message_id == failed_id
    assert result.read_result.coverage == "read_failures"
    assert result.preview.statistics.inventory.completeness == "partial"
    assert result.preview.statistics.total_messages == 9
    assert failed_id not in result.preview.exact_message_ids
    assert result.preview.statistics.executed_gmail_actions == 0


def test_inventory_estimate_discrepancy_remains_partial(paths):
    token(paths)
    class DiscrepantMessages(Messages):
        def list(self, **kwargs):
            request = super().list(**kwargs)
            request.value["resultSizeEstimate"] = 99
            return request
    result, _, _, _ = scan(paths, messages=DiscrepantMessages(count=2), limit=10)
    assert result.read_result.listing_complete
    assert result.preview.statistics.inventory.completeness == "partial"
    assert result.preview.statistics.inventory.discrepancy == "unresolved"


def test_no_token_mock_authorization_and_mock_refresh(paths):
    backend = Backend()
    result, backend, _, _ = scan(paths, backend=backend, limit=2)
    assert result.auth_source == "new_authorization"
    assert backend.authorize_calls == 1 and paths.token.exists()
    token(paths, valid=False, expired=True, refresh_token="synthetic-refresh-token")
    backend = Backend()
    result, backend, _, _ = scan(paths, backend=backend, limit=2)
    assert result.auth_source == "refreshed" and backend.refresh_calls == 1


def test_wrong_scope_and_failed_authorization_stop_before_service_or_gmail(paths):
    token(paths, grants=("https://www.googleapis.com/auth/gmail.modify",))
    backend = Backend()
    factory_calls = []
    def factory(*args, **kwargs):
        factory_calls.append(True)
        return Service(Messages())
    with pytest.raises(AuthError) as error:
        run_gmail_scan(paths=paths, backend=backend, service_factory=factory, as_of=NOW)
    assert error.value.category == "scope_mismatch" and factory_calls == []
    paths.token.unlink()
    backend.authorize_result = RuntimeError("synthetic denied authorization")
    with pytest.raises(AuthError) as error:
        run_gmail_scan(paths=paths, backend=backend, service_factory=factory, as_of=NOW)
    assert error.value.category == "authorization_failure" and factory_calls == []


def test_trash_proposal_from_synthetic_rule_is_still_unauthorized(paths, monkeypatch):
    token(paths)
    base = load_config(default_config_directory())
    trash_rule = Rule.model_validate({"id": "synthetic_trash_demo", "version": 1,
        "match": {"sender_domains_any": ["example.invalid"],
                  "subject_contains_any": ["trash offer"]},
        "category_ids": ["promotions"], "proposed_action": "trash",
        "approval_ref": "synthetic-unverified-reference"})
    config = Configuration(settings=base.settings, categories=base.categories,
                           rules=RulesConfig(rules=(*base.rules.rules, trash_rule)))
    monkeypatch.setattr("dam.scan.load_config", lambda *_args: config)
    result, _, _, _ = scan(paths, messages=Messages(trash_id="synthetic-gmail-09"))
    target = next(entry for entry in result.preview.entries if entry.message_id == "synthetic-gmail-09")
    assert target.proposed_action == ProposedAction.TRASH
    assert target.approval_required and target.approval_type == "destructive"
    assert not target.authority_established and not target.executable
    assert result.preview.destructive_candidate_ids == ("synthetic-gmail-09",)
    assert result.preview.statistics.executed_trash == 0


def test_cli_gmail_flag_is_explicit_and_limit_guard_precedes_access(monkeypatch, capsys, paths):
    token(paths)
    result, _, _, _ = scan(paths, limit=2)
    calls = []
    def fake_gmail(*, limit):
        calls.append(limit)
        return result
    monkeypatch.setattr("dam.cli.run_gmail_scan", fake_gmail)
    assert main(["scan", "--gmail", "--limit", "2"]) == 0
    output = capsys.readouterr().out
    assert "real Gmail read-only Inbox scan" in output
    assert "Executed Gmail actions: 0" in output
    assert "authority=false; executable=false" in output
    assert calls == [2]
    assert main(["scan", "--gmail", "--limit", "11"]) == 2
    assert calls == [2]
    assert "no Gmail access attempted" in capsys.readouterr().err


def test_cli_reports_uninspected_message_separately(monkeypatch, capsys, paths):
    token(paths)
    result, _, _, _ = scan(paths, messages=Messages(fail_id="synthetic-gmail-04"))
    monkeypatch.setattr("dam.cli.run_gmail_scan", lambda *, limit: result)
    assert main(["scan", "--gmail", "--limit", "10", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "read failures: 1; coverage: read_failures" in output
    assert "Uninspected message synthetic-gmail-04: api_error; Review required." in output
    assert "Observed: 9" in output


def test_synthetic_mode_never_calls_auth_or_gmail(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("synthetic mode reached Gmail")
    monkeypatch.setattr("dam.cli.run_gmail_scan", forbidden)
    monkeypatch.setattr("dam.scan.authenticate", forbidden)
    monkeypatch.setattr("dam.scan.read_inbox", forbidden)
    assert main(["scan", "--limit", "2", "--dry-run"]) == 0
    assert "synthetic-account" in capsys.readouterr().out


def test_help_and_import_do_not_touch_auth_or_network(tmp_path):
    code = """
import sys
def guard(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.__new__', 'os.mkdir'):
        raise AssertionError(event)
    if event == 'open' and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError('file write')
sys.addaudithook(guard)
import dam.scan
import dam.cli
from dam.cli import main
main(['scan', '--help'])
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--gmail" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_cli_expected_auth_error_is_nonzero_without_secret_leak(monkeypatch, capsys):
    def fail(*, limit):
        raise AuthError("invalid_client")
    monkeypatch.setattr("dam.cli.run_gmail_scan", fail)
    assert main(["scan", "--gmail", "--limit", "1"]) == 2
    error = capsys.readouterr().err
    assert "read-only scan failed" in error
    assert "client-secret" not in error and "token" not in error


def test_lazy_google_backend_contract_uses_only_injected_fake_modules(monkeypatch):
    """Exercise future library call shapes without importing Google packages."""
    calls = []

    class GoogleCredential:
        def __init__(self):
            self.scopes = (GMAIL_READONLY_SCOPE,)
            self.granted_scopes = (GMAIL_READONLY_SCOPE,)
            self.valid = True
            self.expired = False
            self.refresh_token = "synthetic-refresh-token"

        @classmethod
        def from_authorized_user_info(cls, document):
            calls.append("decode")
            return cls()

        def refresh(self, request):
            calls.append("refresh")

        def to_json(self):
            calls.append("encode")
            return json.dumps({"scopes": [GMAIL_READONLY_SCOPE],
                               "access_token": "synthetic-token"})

    class Flow:
        @classmethod
        def from_client_config(cls, client_config, scopes):
            calls.append(("flow", scopes))
            return cls()

        def run_local_server(self, **kwargs):
            calls.append(("mock_local_server", kwargs))
            return GoogleCredential()

    modules = {}
    for name in ("google", "google.oauth2", "google.oauth2.credentials", "google.auth",
                 "google.auth.transport", "google.auth.transport.requests",
                 "google_auth_oauthlib", "google_auth_oauthlib.flow",
                 "googleapiclient", "googleapiclient.discovery"):
        modules[name] = ModuleType(name)
    modules["google.oauth2.credentials"].Credentials = GoogleCredential
    modules["google.auth.transport.requests"].Request = object
    modules["google_auth_oauthlib.flow"].InstalledAppFlow = Flow
    marker = object()
    def fake_build(api, version, **kwargs):
        calls.append(("build", api, version, kwargs["static_discovery"]))
        return marker
    modules["googleapiclient.discovery"].build = fake_build
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    backend = GoogleAuthBackend()
    envelope = backend.decode_token({"granted_scopes": [GMAIL_READONLY_SCOPE]})
    assert envelope.valid and envelope.granted_scopes == (GMAIL_READONLY_SCOPE,)
    refreshed = backend.refresh(envelope)
    assert refreshed.valid and "refresh" in calls
    authorized = backend.authorize({"installed": {}}, (GMAIL_READONLY_SCOPE,))
    assert authorized.valid
    assert ("mock_local_server", {"port": 0, "open_browser": True}) in calls
    assert json.loads(backend.encode_token(authorized))["granted_scopes"] == [GMAIL_READONLY_SCOPE]
    assert google_service_factory("gmail", "v1", credentials=authorized,
                                  cache_discovery=False) is marker
    assert ("build", "gmail", "v1", True) in calls
