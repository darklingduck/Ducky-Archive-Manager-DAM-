"""Synthetic, offline authentication boundary using temporary private paths."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import webbrowser

import pytest

from dam.auth import (
    ALLOWED_SCOPES, AuthError, AuthPaths, authenticate, build_gmail_service,
    load_client_config, validate_exact_scopes,
)
from dam.gmail import GMAIL_READONLY_SCOPE

CLIENT_SECRET = "synthetic-client-secret-never-real"
ACCESS_TOKEN = "synthetic-access-token-never-real"
REFRESH_TOKEN = "synthetic-refresh-token-never-real"


@dataclass
class FakeCredential:
    scopes: object = ALLOWED_SCOPES
    granted_scopes: object = ALLOWED_SCOPES
    valid: bool = True
    expired: bool = False
    refresh_token: str | None = None
    access_token: str = ACCESS_TOKEN


class FakeBackend:
    def __init__(self):
        self.decode_result = None
        self.refresh_result = None
        self.authorize_result = None
        self.decode_calls = 0
        self.refresh_calls = 0
        self.authorize_calls = 0
        self.encode_calls = 0
        self.requested_scopes = None
        self.client_seen = None
        self.encoded_result = None

    def decode_token(self, document):
        self.decode_calls += 1
        if isinstance(self.decode_result, Exception):
            raise self.decode_result
        return self.decode_result or FakeCredential(
            scopes=tuple(document["scopes"]), granted_scopes=tuple(document.get("granted_scopes", document["scopes"])),
            valid=document.get("valid", True), expired=document.get("expired", False),
            refresh_token=document.get("refresh_token"))

    def refresh(self, credential):
        self.refresh_calls += 1
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        return self.refresh_result

    def authorize(self, client_config, scopes):
        self.authorize_calls += 1
        self.client_seen = client_config
        self.requested_scopes = scopes
        if isinstance(self.authorize_result, Exception):
            raise self.authorize_result
        return self.authorize_result

    def encode_token(self, credential):
        self.encode_calls += 1
        if self.encoded_result is not None:
            return self.encoded_result
        return json.dumps({"scopes": list(credential.scopes),
                           "granted_scopes": list(credential.granted_scopes),
                           "access_token": credential.access_token,
                           "refresh_token": credential.refresh_token,
                           "valid": credential.valid, "expired": credential.expired})


def write_private(path: Path, document) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def paths(tmp_path):
    paths = AuthPaths.for_home(tmp_path)
    paths.dam_directory.mkdir(parents=True, mode=0o700)
    paths.oauth_directory.mkdir(mode=0o700)
    paths.dam_directory.chmod(0o700)
    paths.oauth_directory.chmod(0o700)
    write_private(paths.client_secret, {"installed": {
        "client_id": "synthetic-client-id", "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.example.invalid/auth",
        "token_uri": "https://accounts.example.invalid/token",
        "redirect_uris": ["http://localhost:0/synthetic"]}})
    return paths


def token(paths, **changes):
    document = {"scopes": [GMAIL_READONLY_SCOPE], "granted_scopes": [GMAIL_READONLY_SCOPE],
                "access_token": ACCESS_TOKEN, "valid": True, "expired": False}
    write_private(paths.token, document | changes)


def test_import_does_not_open_files_network_browser_sqlite_or_service(tmp_path):
    code = """
import sys
def guard(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.__new__', 'os.mkdir'):
        raise AssertionError(event)
    if event == 'open' and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError('file write')
sys.addaudithook(guard)
import dam.auth
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_exact_scope_is_central_and_accepted():
    assert ALLOWED_SCOPES == (GMAIL_READONLY_SCOPE,)
    validate_exact_scopes([GMAIL_READONLY_SCOPE], [GMAIL_READONLY_SCOPE])


@pytest.mark.parametrize("requested,granted", [
    ([], []), (None, None), ("https://www.googleapis.com/auth/gmail.readonly", [GMAIL_READONLY_SCOPE]),
    (["https://www.googleapis.com/auth/gmail.modify"], [GMAIL_READONLY_SCOPE]),
    ([GMAIL_READONLY_SCOPE], ["https://mail.google.com/"]),
    ([GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"], [GMAIL_READONLY_SCOPE]),
    ([GMAIL_READONLY_SCOPE], [GMAIL_READONLY_SCOPE, "unexpected"]),
    ([GMAIL_READONLY_SCOPE, GMAIL_READONLY_SCOPE], [GMAIL_READONLY_SCOPE]),
    ([123], [GMAIL_READONLY_SCOPE]), ([GMAIL_READONLY_SCOPE], None),
])
def test_unverifiable_broad_or_malformed_scopes_rejected(requested, granted):
    with pytest.raises(AuthError) as error:
        validate_exact_scopes(requested, granted)
    assert error.value.category == "scope_mismatch"


def test_default_paths_are_private_and_outside_repo():
    paths = AuthPaths.for_home()
    assert paths.client_secret == Path.home() / ".config/dam/oauth/client-secret.json"
    assert paths.token == Path.home() / ".config/dam/oauth/token.json"
    assert not paths.client_secret.is_relative_to(Path.cwd())


def test_private_client_config_and_permissions(paths):
    assert stat.S_IMODE(paths.dam_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.oauth_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.client_secret.stat().st_mode) == 0o600
    assert load_client_config(paths)["installed"]["client_secret"] == CLIENT_SECRET


@pytest.mark.parametrize("target", ["dam_directory", "oauth_directory", "client_secret", "token"])
def test_unsafe_existing_permissions_rejected(paths, target):
    if target == "token":
        token(paths)
    selected = getattr(paths, target)
    selected.chmod(0o755 if selected.is_dir() else 0o644)
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "unsafe_permissions"
    assert CLIENT_SECRET not in str(error.value)


def test_missing_and_malformed_client_file(paths):
    paths.client_secret.unlink()
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "missing_client"
    write_private(paths.client_secret, {"installed": {"client_secret": CLIENT_SECRET}})
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "invalid_client"
    assert CLIENT_SECRET not in str(error.value)


def test_no_token_does_not_implicitly_authorize(paths):
    backend = FakeBackend()
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "token_missing"
    assert backend.authorize_calls == backend.refresh_calls == 0


def test_valid_existing_credential_is_scope_checked_and_not_persisted(paths):
    token(paths)
    backend = FakeBackend()
    session = authenticate(paths, backend)
    assert session.summary.source == "existing"
    assert session.summary.scope_verified and not session.summary.token_persisted
    assert not session.summary.mailbox_access_performed and not session.summary.authority_established
    assert backend.decode_calls == 1 and backend.refresh_calls == backend.authorize_calls == backend.encode_calls == 0
    assert ACCESS_TOKEN not in repr(session) and REFRESH_TOKEN not in repr(session)
    assert ACCESS_TOKEN not in session.summary.model_dump_json()
    with pytest.raises(AttributeError):
        session._credential = FakeCredential()


def test_valid_flag_cannot_bypass_unknown_or_wrong_granted_scopes(paths):
    token(paths)
    backend = FakeBackend()
    backend.decode_result = FakeCredential(granted_scopes=None, valid=True)
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "scope_mismatch"
    backend.decode_result = FakeCredential(granted_scopes=("https://www.googleapis.com/auth/gmail.modify",))
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "scope_mismatch"


def test_expired_credential_refreshes_only_through_mock_and_persists(paths):
    token(paths, valid=False, expired=True, refresh_token=REFRESH_TOKEN)
    backend = FakeBackend()
    backend.refresh_result = FakeCredential()
    session = authenticate(paths, backend)
    assert session.summary.source == "refreshed" and session.summary.token_persisted
    assert backend.refresh_calls == backend.encode_calls == 1
    assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600
    assert json.loads(paths.token.read_text())["scopes"] == [GMAIL_READONLY_SCOPE]
    assert list(paths.oauth_directory.glob("*.tmp")) == []


def test_refresh_scope_revalidated_and_failure_never_falls_back_to_flow(paths):
    token(paths, valid=False, expired=True, refresh_token=REFRESH_TOKEN)
    backend = FakeBackend()
    backend.refresh_result = FakeCredential(granted_scopes=("https://mail.google.com/",))
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "scope_mismatch"
    assert backend.authorize_calls == backend.encode_calls == 0
    backend.refresh_result = RuntimeError("private refresh response")
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "refresh_failure"
    assert "private refresh response" not in str(error.value)
    assert backend.authorize_calls == 0


def test_expired_without_refresh_and_invalid_or_malformed_token(paths):
    token(paths, valid=False, expired=True)
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "invalid_token"
    token(paths, valid=False, expired=False)
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "invalid_token"
    paths.token.write_text("{malformed private token", encoding="utf-8")
    paths.token.chmod(0o600)
    with pytest.raises(AuthError) as error:
        authenticate(paths, FakeBackend())
    assert error.value.category == "invalid_token"
    assert "malformed private token" not in str(error.value)


def test_token_document_scope_mismatch_rejected_before_decode(paths):
    token(paths, scopes=["https://www.googleapis.com/auth/gmail.modify"])
    backend = FakeBackend()
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "scope_mismatch"
    assert backend.decode_calls == 0
    token(paths, granted_scopes=["https://www.googleapis.com/auth/gmail.modify"])
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "scope_mismatch"
    assert backend.decode_calls == 0


def test_mocked_authorization_requires_explicit_opt_in_and_exact_scopes(paths):
    backend = FakeBackend()
    backend.authorize_result = FakeCredential()
    session = authenticate(paths, backend, allow_authorization=True)
    assert backend.requested_scopes == (GMAIL_READONLY_SCOPE,)
    assert backend.client_seen["installed"]["client_secret"] == CLIENT_SECRET
    assert session.summary.source == "new_authorization" and session.summary.token_persisted
    assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600
    assert not session.summary.mailbox_access_performed


def test_authorization_denial_wrong_scope_and_invalid_result(paths):
    backend = FakeBackend()
    backend.authorize_result = RuntimeError("private authorization code")
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "authorization_failure"
    assert "private authorization code" not in str(error.value)
    backend.authorize_result = FakeCredential(scopes=("https://www.googleapis.com/auth/gmail.send",))
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "scope_mismatch"
    backend.authorize_result = FakeCredential(valid=False)
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "invalid_token"
    assert not paths.token.exists()


def test_failed_atomic_replace_leaves_old_private_token_and_no_temp(paths, monkeypatch):
    token(paths, valid=False, expired=True, refresh_token=REFRESH_TOKEN)
    old_bytes = paths.token.read_bytes()
    backend = FakeBackend()
    backend.refresh_result = FakeCredential()
    def fail_replace(*_args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("dam.auth.os.replace", fail_replace)
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend)
    assert error.value.category == "token_persistence_failure"
    assert paths.token.read_bytes() == old_bytes
    assert list(paths.oauth_directory.glob("*.tmp")) == []


def test_serialized_token_scope_is_checked_before_persistence(paths):
    backend = FakeBackend()
    backend.authorize_result = FakeCredential()
    backend.encoded_result = json.dumps({
        "scopes": [GMAIL_READONLY_SCOPE],
        "granted_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "access_token": ACCESS_TOKEN})
    with pytest.raises(AuthError) as error:
        authenticate(paths, backend, allow_authorization=True)
    assert error.value.category == "scope_mismatch"
    assert not paths.token.exists()


def test_invalid_paths_inside_repo_rejected_without_reads():
    root = Path.cwd()
    paths = AuthPaths(root / ".config/dam/oauth/client-secret.json",
                      root / ".config/dam/oauth/token.json")
    with pytest.raises(AuthError) as error:
        load_client_config(paths)
    assert error.value.category == "invalid_paths"
    parent_spelling = root.parent / "dam" / ".." / "dam" / ".config/dam/oauth"
    with pytest.raises(AuthError) as error:
        load_client_config(AuthPaths(parent_spelling / "client-secret.json",
                                     parent_spelling / "token.json"))
    assert error.value.category == "invalid_paths"


def test_service_factory_requires_validated_session_and_makes_no_mailbox_read(paths):
    token(paths)
    session = authenticate(paths, FakeBackend())
    calls = []
    sentinel = object()
    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel
    assert build_gmail_service(session, factory) is sentinel
    assert len(calls) == 1 and calls[0][0] == ("gmail", "v1")
    assert calls[0][1]["cache_discovery"] is False
    assert not hasattr(sentinel, "users")
    with pytest.raises(AuthError):
        build_gmail_service(object(), factory)


def test_authentication_does_not_use_network_browser_sqlite_or_gmail(paths, monkeypatch):
    token(paths)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network, browser, SQLite or Gmail access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    session = authenticate(paths, FakeBackend())
    assert session.summary.source == "existing"
    assert not session.summary.mailbox_access_performed


def test_step8_cli_remains_synthetic():
    result = subprocess.run([sys.executable, "-m", "dam", "scan", "--limit", "2", "--dry-run"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "synthetic-account" in result.stdout
    assert "Executed Gmail actions: 0" in result.stdout
