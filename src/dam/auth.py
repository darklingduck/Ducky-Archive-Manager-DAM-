"""Offline-testable, injected Gmail read-only authentication boundary.

No Google library, OAuth flow, browser, callback, network transport, Gmail
adapter or SQLite object is imported or constructed here. An explicit caller
must supply a backend for token decoding, refresh and authorization. The only
allowed scope is centralized in dam.gmail. Credential.valid never substitutes
for exact requested AND granted scope evidence.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import Field

from dam.gmail import GMAIL_READONLY_SCOPE
from dam.models import ConfigModel

ALLOWED_SCOPES = (GMAIL_READONLY_SCOPE,)
MAX_SECRET_FILE_BYTES = 1_048_576


class AuthError(RuntimeError):
    """A safe category and file reference, never a secret or backend exception."""

    def __init__(self, category: Literal[
        "missing_client", "invalid_client", "unsafe_permissions", "invalid_token",
        "token_missing", "scope_mismatch", "refresh_failure", "authorization_failure",
        "token_persistence_failure", "invalid_paths", "invalid_service",
    ], *, path: Path | None = None):
        self.category = category
        self.path = path
        location = f" ({path})" if path is not None else ""
        super().__init__(f"DAM authentication {category}{location}")


@dataclass(frozen=True)
class AuthPaths:
    client_secret: Path
    token: Path

    @classmethod
    def for_home(cls, home: Path | None = None) -> "AuthPaths":
        base = Path.home() if home is None else Path(home).expanduser()
        oauth = base / ".config" / "dam" / "oauth"
        return cls(oauth / "client-secret.json", oauth / "token.json")

    @property
    def dam_directory(self) -> Path:
        return self.client_secret.parent.parent

    @property
    def oauth_directory(self) -> Path:
        return self.client_secret.parent


class AuthBackend(Protocol):
    """Injected adapter; Step 10 supplies no implementation with IO/network."""

    def decode_token(self, document: Mapping[str, Any]) -> Any: ...
    def refresh(self, credential: Any) -> Any: ...
    def authorize(self, client_config: Mapping[str, Any], scopes: tuple[str, ...]) -> Any: ...
    def encode_token(self, credential: Any) -> str: ...


class AuthSummary(ConfigModel):
    ready: Literal[True] = True
    source: Literal["existing", "refreshed", "new_authorization"]
    scope_verified: Literal[True] = True
    token_persisted: bool
    token_path: str
    mailbox_access_performed: Literal[False] = False
    authority_established: Literal[False] = False


_SESSION_SEAL = object()


class AuthSession:
    """Opaque credential lease; only the non-secret summary is serializable."""

    __slots__ = ("summary", "_credential")

    def __init__(self, summary: AuthSummary, credential: Any, *, _seal: object):
        if _seal is not _SESSION_SEAL:
            raise TypeError("AuthSession is created only by DAM authentication")
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "_credential", credential)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("AuthSession is immutable")

    def __repr__(self) -> str:
        return f"AuthSession(summary={self.summary!r}, credential=<redacted>)"


def _scope_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        raise AuthError("scope_mismatch")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
        raise AuthError("scope_mismatch")
    scopes = tuple(value)
    if len(scopes) != len(set(scopes)) or set(scopes) != set(ALLOWED_SCOPES):
        raise AuthError("scope_mismatch")
    return scopes


def validate_exact_scopes(requested: Any, granted: Any) -> None:
    """Both scope declarations must prove exactly gmail.readonly, nothing else."""
    _scope_tuple(requested)
    _scope_tuple(granted)


def _validate_credential(credential: Any) -> None:
    try:
        requested = credential.scopes
        granted = credential.granted_scopes
        valid = credential.valid
        expired = credential.expired
    except Exception:
        raise AuthError("invalid_token") from None
    validate_exact_scopes(requested, granted)
    if type(valid) is not bool or type(expired) is not bool or not valid or expired:
        raise AuthError("invalid_token")


def _validate_paths(paths: AuthPaths) -> None:
    if (type(paths) is not AuthPaths or paths.client_secret.name != "client-secret.json" or
            paths.token.name != "token.json" or paths.client_secret.parent != paths.token.parent or
            paths.oauth_directory.name != "oauth" or paths.dam_directory.name != "dam" or
            paths.dam_directory.parent.name != ".config"):
        raise AuthError("invalid_paths")
    repository = Path(__file__).resolve().parents[2]
    checkout = Path.cwd().resolve()
    roots = (repository, checkout) if (checkout / ".git" / "HEAD").exists() else (repository,)
    for path in (paths.client_secret, paths.token):
        if not path.is_absolute():
            raise AuthError("invalid_paths", path=path)
        for part in (path, *path.parents):
            if part.is_symlink():
                raise AuthError("invalid_paths", path=path)
        resolved = path.resolve(strict=False)
        if any(resolved.is_relative_to(root) for root in roots):
            raise AuthError("invalid_paths", path=path)


def _private_mode(path: Path, *, directory: bool, missing_category: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise AuthError(missing_category, path=path) from None
    except OSError:
        raise AuthError("unsafe_permissions", path=path) from None
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    required = 0o700 if directory else 0o600
    if (not correct_type or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) != required or (not directory and info.st_nlink != 1)):
        raise AuthError("unsafe_permissions", path=path)


def _private_directories(paths: AuthPaths) -> None:
    _private_mode(paths.dam_directory, directory=True, missing_category="missing_client")
    _private_mode(paths.oauth_directory, directory=True, missing_category="missing_client")


def _read_private_json(path: Path, *, missing_category: Literal["missing_client", "token_missing"],
                       invalid_category: Literal["invalid_client", "invalid_token"]) -> dict[str, Any]:
    _private_mode(path, directory=False, missing_category=missing_category)
    if not hasattr(os, "O_NOFOLLOW"):
        raise AuthError("unsafe_permissions", path=path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise AuthError("unsafe_permissions", path=path)
            raw = stream.read(MAX_SECRET_FILE_BYTES + 1)
    except AuthError:
        raise
    except OSError:
        raise AuthError("unsafe_permissions", path=path) from None
    if len(raw) > MAX_SECRET_FILE_BYTES:
        raise AuthError(invalid_category, path=path)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AuthError(invalid_category, path=path) from None
    if not isinstance(document, dict):
        raise AuthError(invalid_category, path=path)
    return document


def load_client_config(paths: AuthPaths) -> dict[str, Any]:
    """Read only an installed-app client file; return data solely to injected backend."""
    _validate_paths(paths)
    _private_directories(paths)
    document = _read_private_json(paths.client_secret, missing_category="missing_client",
                                  invalid_category="invalid_client")
    installed = document.get("installed")
    if not isinstance(installed, dict) or "web" in document:
        raise AuthError("invalid_client", path=paths.client_secret)
    for field_name in ("client_id", "client_secret", "auth_uri", "token_uri"):
        value = installed.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise AuthError("invalid_client", path=paths.client_secret)
    if (not installed["auth_uri"].startswith("https://") or
            not installed["token_uri"].startswith("https://")):
        raise AuthError("invalid_client", path=paths.client_secret)
    redirects = installed.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or any(
        not isinstance(uri, str) or not uri.strip() for uri in redirects
    ):
        raise AuthError("invalid_client", path=paths.client_secret)
    return document


def _token_document(raw: str, path: Path) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SECRET_FILE_BYTES:
        raise AuthError("token_persistence_failure", path=path)
    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise AuthError("token_persistence_failure", path=path) from None
    if not isinstance(document, dict):
        raise AuthError("token_persistence_failure", path=path)
    validate_exact_scopes(document.get("scopes"), document.get("granted_scopes"))
    return document


def _persist_token(paths: AuthPaths, encoded: str) -> None:
    _validate_paths(paths)
    _private_directories(paths)
    if paths.token.exists() or paths.token.is_symlink():
        _private_mode(paths.token, directory=False, missing_category="token_persistence_failure")
    temporary = paths.oauth_directory / f"token.json.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _private_mode(temporary, directory=False, missing_category="token_persistence_failure")
        os.replace(temporary, paths.token)
        directory_fd = os.open(paths.oauth_directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, AuthError):
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AuthError("token_persistence_failure", path=paths.token) from None


def authenticate(paths: AuthPaths, backend: AuthBackend, *, allow_authorization: bool = False) -> AuthSession:
    """Offline-capable orchestration; no implicit flow or refresh implementation."""
    client = load_client_config(paths)
    try:
        token = _read_private_json(paths.token, missing_category="token_missing",
                                   invalid_category="invalid_token")
    except AuthError as error:
        if error.category != "token_missing":
            raise
        if not allow_authorization:
            raise
        try:
            credential = backend.authorize(client, ALLOWED_SCOPES)
        except Exception:
            raise AuthError("authorization_failure") from None
        _validate_credential(credential)
        source: Literal["existing", "refreshed", "new_authorization"] = "new_authorization"
    else:
        validate_exact_scopes(token.get("scopes"), token.get("granted_scopes"))
        try:
            credential = backend.decode_token(token)
        except Exception:
            raise AuthError("invalid_token", path=paths.token) from None
        # Scope evidence is checked before any mocked refresh operation.
        try:
            validate_exact_scopes(credential.scopes, credential.granted_scopes)
            expired = credential.expired
            valid = credential.valid
        except AuthError:
            raise
        except Exception:
            raise AuthError("invalid_token", path=paths.token) from None
        if type(expired) is not bool or type(valid) is not bool:
            raise AuthError("invalid_token", path=paths.token)
        if expired:
            if not getattr(credential, "refresh_token", None):
                raise AuthError("invalid_token", path=paths.token)
            try:
                credential = backend.refresh(credential)
            except Exception:
                raise AuthError("refresh_failure", path=paths.token) from None
            _validate_credential(credential)
            source = "refreshed"
        elif valid:
            _validate_credential(credential)
            source = "existing"
        else:
            raise AuthError("invalid_token", path=paths.token)
    persisted = source != "existing"
    if persisted:
        try:
            encoded = backend.encode_token(credential)
        except Exception:
            raise AuthError("token_persistence_failure", path=paths.token) from None
        _token_document(encoded, paths.token)
        _persist_token(paths, encoded)
    summary = AuthSummary(source=source, token_persisted=persisted, token_path=str(paths.token))
    return AuthSession(summary, credential, _seal=_SESSION_SEAL)


def build_gmail_service(session: AuthSession, factory: Any) -> Any:
    """Inject a builder after revalidation; construction performs no API read."""
    if type(session) is not AuthSession:
        raise AuthError("invalid_service")
    _validate_credential(session._credential)
    if not session.summary.scope_verified:
        raise AuthError("invalid_service")
    try:
        return factory("gmail", "v1", credentials=session._credential, cache_discovery=False)
    except Exception:
        raise AuthError("invalid_service") from None
