"""Explicit local M1 persistence, never execution or approval authority.

Open with Storage.open(settings); imports do no IO. POSIX ownership/mode checks
fail closed on unsupported systems, symlinks, shared state directories, and
non-private existing files. Existing permissions are never changed. SQLite uses
DELETE journals inside the private state directory, FULL synchronous writes,
foreign keys, explicit transactions, and schema version 1 (PRAGMA user_version).

Only known typed records cross the write API. JSON contains metadata and evidence
summaries, never arbitrary payloads. Callers must not put secrets or copied body
content into allowed header/summary/identifier fields: text semantics cannot be
verified by a storage schema. Configuration provenance deliberately omits notes,
paths and match literals; fingerprints identify the separately maintained source
configuration, so the database is not a full configuration backup.

One observation/proposal per individual message per run; identical retries are
idempotent, differing retries fail. New runs retain new observations. Events and
approval descriptions use caller-supplied stable IDs: retrying an ID is idempotent,
new IDs retain history. Approval status is reported evidence only, never checked
or applied to proposals. No completion of a mailbox action can be recorded here.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from dam.actions import ActionProposal
from dam.classifier import ClassificationResult
from dam.config import configuration_fingerprint, rule_scope_fingerprint
from dam.models import (
    ConfigModel, Configuration, MessageMetadata, NonBlankText, NonNegativeInt,
    PositiveInt, Settings,
)

SCHEMA_VERSION = 1
_TABLES = frozenset({"accounts", "labels", "config_snapshots", "rule_versions", "approvals",
                     "scan_runs", "messages", "message_observations", "proposals", "audit_events"})
Fingerprint = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class StorageError(RuntimeError):
    """A failed local operation, with no private input echoed in its message."""


class ScanStart(ConfigModel):
    run_id: NonBlankText
    account_id: NonBlankText
    config_fingerprint: Fingerprint
    started_at: AwareDatetime
    as_of: AwareDatetime
    scope_label_ids: tuple[Literal["INBOX"], ...] = ("INBOX",)
    limit: PositiveInt
    mode: Literal["dry_run"] = "dry_run"

    @model_validator(mode="after")
    def inbox_scope(self):
        if self.scope_label_ids != ("INBOX",):
            raise ValueError("M1 storage scan scope must be INBOX")
        return self


class InventoryCounts(ConfigModel):
    """Reported inventory evidence, separate from stored unique observations.

    None means not supplied, never zero. Completeness is explicitly reported by
    the caller, not inferred from label totals, estimates or a completed run.
    """

    label_total: NonNegativeInt | None = None
    estimated_total: NonNegativeInt | None = None
    pages_read: NonNegativeInt | None = None
    pagination_limited: bool = False
    completeness: Literal["unknown", "partial", "complete"] = "unknown"
    discrepancy: Literal["not_checked", "none", "unresolved"] = "not_checked"

    @model_validator(mode="after")
    def limited_is_not_complete(self):
        if self.pagination_limited and self.completeness == "complete":
            raise ValueError("Limited pagination cannot establish a complete inventory")
        return self


class ScanFinish(ConfigModel):
    ended_at: AwareDatetime
    status: Literal["completed", "failed", "interrupted"]
    inventory: InventoryCounts = Field(default_factory=InventoryCounts)


class ScanRecord(ConfigModel):
    start: ScanStart
    finish: ScanFinish | None
    observed_unique_messages: NonNegativeInt
    count_provenance: Literal["stored_unique_observations"] = "stored_unique_observations"

    @property
    def status(self) -> Literal["running", "completed", "failed", "interrupted"]:
        return self.finish.status if self.finish else "running"


class ApprovalDescription(ConfigModel):
    """Immutable reported provenance; even reported_approved is unverified."""

    record_id: NonBlankText
    approval_ref: NonBlankText
    account_id: NonBlankText
    config_fingerprint: Fingerprint
    recorded_at: AwareDatetime
    source_id: NonBlankText
    reported_status: Literal["referenced", "reported_approved", "reported_revoked"] = "referenced"
    authority_established: Literal[False] = False
    validation_status: Literal["unverified"] = "unverified"


class AuditEvent(ConfigModel):
    event_id: NonBlankText
    run_id: NonBlankText
    message_id: NonBlankText | None = None
    recorded_at: AwareDatetime
    event_type: Literal["scan_metadata", "observation", "proposal", "preview"]
    state: Literal["informational", "observed", "proposed"]
    mode: Literal["dry_run"] = "dry_run"
    mailbox_modified: Literal[False] = False
    subscription_changed: Literal[False] = False

    @model_validator(mode="after")
    def descriptive_state(self):
        expected = {"scan_metadata": "informational", "observation": "observed",
                    "proposal": "proposed", "preview": "proposed"}
        if self.state != expected[self.event_type]:
            raise ValueError("Audit event type and descriptive state disagree")
        if self.event_type in ("observation", "proposal") and self.message_id is None:
            raise ValueError("Message observation/proposal events require a message ID")
        return self


class ObservationRecord(ConfigModel):
    run_id: str
    observed_at: AwareDatetime
    metadata: MessageMetadata
    classification: ClassificationResult


# Static DDL only. No application values are interpolated into SQL.
_SCHEMA = (
    "CREATE TABLE accounts (account_id TEXT PRIMARY KEY NOT NULL)",
    """CREATE TABLE labels (
        account_id TEXT NOT NULL REFERENCES accounts(account_id), label_id TEXT NOT NULL,
        PRIMARY KEY (account_id, label_id))""",
    """CREATE TABLE config_snapshots (
        fingerprint TEXT PRIMARY KEY NOT NULL, semantic_fingerprint TEXT NOT NULL,
        policy_version INTEGER NOT NULL, provenance_json TEXT NOT NULL)""",
    """CREATE TABLE rule_versions (
        config_fingerprint TEXT NOT NULL REFERENCES config_snapshots(fingerprint),
        rule_id TEXT NOT NULL, version INTEGER NOT NULL, scope_fingerprint TEXT NOT NULL,
        provenance_json TEXT NOT NULL, PRIMARY KEY (config_fingerprint, rule_id, version))""",
    """CREATE TABLE approvals (
        record_id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        config_fingerprint TEXT NOT NULL REFERENCES config_snapshots(fingerprint),
        description_json TEXT NOT NULL,
        authority_established INTEGER NOT NULL DEFAULT 0 CHECK (authority_established = 0))""",
    """CREATE TABLE scan_runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        config_fingerprint TEXT NOT NULL REFERENCES config_snapshots(fingerprint),
        start_json TEXT NOT NULL, finish_json TEXT,
        UNIQUE (run_id, account_id))""",
    """CREATE TABLE messages (
        account_id TEXT NOT NULL REFERENCES accounts(account_id), message_id TEXT NOT NULL,
        thread_id TEXT, PRIMARY KEY (account_id, message_id))""",
    """CREATE TABLE message_observations (
        run_id TEXT NOT NULL, account_id TEXT NOT NULL, message_id TEXT NOT NULL,
        observed_at TEXT NOT NULL, metadata_json TEXT NOT NULL, classification_json TEXT NOT NULL,
        PRIMARY KEY (run_id, message_id),
        FOREIGN KEY (run_id, account_id) REFERENCES scan_runs(run_id, account_id),
        FOREIGN KEY (account_id, message_id) REFERENCES messages(account_id, message_id))""",
    """CREATE TABLE proposals (
        run_id TEXT NOT NULL, message_id TEXT NOT NULL, proposal_json TEXT NOT NULL,
        authority_established INTEGER NOT NULL DEFAULT 0 CHECK (authority_established = 0),
        executable INTEGER NOT NULL DEFAULT 0 CHECK (executable = 0),
        PRIMARY KEY (run_id, message_id),
        FOREIGN KEY (run_id, message_id) REFERENCES message_observations(run_id, message_id))""",
    """CREATE TABLE audit_events (
        event_id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL REFERENCES scan_runs(run_id),
        message_id TEXT, event_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('informational', 'observed', 'proposed')),
        mailbox_modified INTEGER NOT NULL DEFAULT 0 CHECK (mailbox_modified = 0),
        subscription_changed INTEGER NOT NULL DEFAULT 0 CHECK (subscription_changed = 0),
        FOREIGN KEY (run_id, message_id) REFERENCES message_observations(run_id, message_id))""",
)
_CLASSIFICATION = TypeAdapter(ClassificationResult)


def _json(value: dict | list) -> str:
    # No default=str, object hooks, pickle, or arbitrary-object fallback.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _validated(value, model):
    if type(value) is not model:
        raise StorageError("Storage requires the documented typed record")
    try:
        # Revalidate even frozen model_copy/model_construct values; serialize only
        # declared schema fields, never __dict__ or arbitrary Python attributes.
        return model.model_validate(value.model_dump(mode="python", exclude_unset=True))
    except (ValidationError, ValueError, TypeError):
        raise StorageError("Invalid storage record") from None


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("Storage timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _record_json(value: ConfigModel) -> str:
    data = value.model_dump(mode="json")
    for name in ("started_at", "as_of", "ended_at", "recorded_at"):
        if name in data:
            data[name] = _time(getattr(value, name))
    return _json(data)


def _private_path(settings: Settings, repository_root: Path | None) -> Path:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise StorageError("Private POSIX ownership and permission semantics are required")
    path = Path(settings.state.database_path).expanduser()
    if not path.is_absolute():
        raise StorageError("Database path must be absolute")
    # Check before creating anything, including both lexical and resolved paths.
    resolved = path.resolve()
    roots = [Path(__file__).resolve().parents[2], Path.cwd()]
    if repository_root is not None:
        roots.append(repository_root.resolve())
        if resolved.is_relative_to(repository_root.resolve()):
            raise StorageError("Database must remain outside the repository")
    for candidate in (path, resolved, *roots):
        for parent in (candidate, *candidate.parents):
            marker = parent / ".git"
            # A worktree gitfile or repository HEAD identifies a checkout. An
            # empty .git placeholder (e.g. sandbox mount metadata) is not Git.
            if (marker.is_file() or (marker / "HEAD").exists()) and resolved.is_relative_to(parent.resolve()):
                raise StorageError("Database must remain outside Git repositories")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise StorageError("State paths must not contain symlinks")
    # mkdir one component at a time: all newly created directories are private;
    # existing ancestors (e.g. /tmp or ~/.local) are left untouched.
    missing = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _check_private(directory, directory=True)
    _check_private(path.parent, directory=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _check_private(path, directory=False)
    # Refuse unsafe pre-existing SQLite companions before SQLite can open them.
    for suffix in ("-journal", "-wal", "-shm"):
        companion = Path(str(path) + suffix)
        if companion.exists() or companion.is_symlink():
            _check_private(companion, directory=False)
    return path


def _check_private(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    proper_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not proper_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise StorageError("State directory/database must be user-owned and private")
    if not directory and info.st_nlink != 1:
        raise StorageError("Database files must not have multiple hard links")


class Storage:
    """Small typed API. No public connection, SQL executor, or generic blob writer."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self._connection = connection
        self.path = path

    @classmethod
    def open(cls, settings: Settings, *, repository_root: Path | None = None) -> "Storage":
        settings = _validated(settings, Settings)
        connection = None
        try:
            path = _private_path(settings, repository_root)
            connection = sqlite3.connect(path, isolation_level=None, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise StorageError("Unsupported database schema version; no migration performed")
            if version == 0 and connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchone():
                raise StorageError("Refusing to initialize an unversioned nonempty database")
            if version == SCHEMA_VERSION:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != _TABLES:
                    raise StorageError("Database tables do not match the M1 schema")
            connection.execute("PRAGMA journal_mode = DELETE")
            storage = cls(connection, path)
            with storage._transaction():
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute("PRAGMA user_version = 1")
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise StorageError("Database foreign-key integrity check failed")
            return storage
        except (OSError, sqlite3.Error, StorageError) as error:
            if connection is not None:
                connection.close()
            if isinstance(error, StorageError):
                raise
            raise StorageError("Cannot open private DAM storage") from None

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._connection.execute("COMMIT")
        except BaseException as error:
            try:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                raise StorageError("Storage is unavailable; close and reopen before retrying") from None
            if isinstance(error, sqlite3.Error):
                raise StorageError("Storage transaction failed; no logical write was committed") from None
            raise

    def _rows(self, statement: str, parameters=()) -> list[dict]:
        try:
            return [dict(row) for row in self._connection.execute(statement, parameters)]
        except sqlite3.Error:
            raise StorageError("Cannot read DAM storage") from None

    def schema_info(self) -> dict:
        return {
            "version": self._rows("PRAGMA user_version")[0]["user_version"],
            "foreign_keys": self._rows("PRAGMA foreign_keys")[0]["foreign_keys"] == 1,
            "tables": tuple(row["name"] for row in self._rows(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")),
        }

    def ensure_account(self, account_id: str) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise StorageError("Account identity must be nonblank text")
        with self._transaction():
            self._connection.execute("INSERT INTO accounts VALUES (?) ON CONFLICT DO NOTHING", (account_id,))

    def accounts(self) -> tuple[str, ...]:
        return tuple(row["account_id"] for row in self._rows("SELECT account_id FROM accounts ORDER BY account_id"))

    def labels(self, account_id: str) -> tuple[str, ...]:
        return tuple(row["label_id"] for row in self._rows(
            "SELECT label_id FROM labels WHERE account_id=? ORDER BY label_id", (account_id,)))

    def save_configuration(self, config: Configuration) -> str:
        config = _validated(config, Configuration)
        fingerprint = configuration_fingerprint(config)
        semantic = configuration_fingerprint(config, semantic=True)
        by_id = {item.id: item for item in config.categories.categories}
        def category_path(item):
            chain = [item.key or item.id]
            current = item
            while current.parent_id:
                current = by_id[current.parent_id]
                chain.append(current.key or current.id)
            return list(reversed(chain))
        category_snapshot = [
            {"permanent_id": item.permanent_id, "legacy_id": item.id,
             "key": item.key or item.id, "name": item.name,
             "aliases": list(item.aliases),
             "parent_permanent_id": by_id[item.parent_id].permanent_id if item.parent_id else None,
             "path_at_scan": category_path(item), "status": item.status}
            for item in sorted(config.categories.categories, key=lambda item: item.id)
        ]
        category_revision = hashlib.sha256(_json(category_snapshot).encode("utf-8")).hexdigest()
        # Only decision settings and hashes; no rule notes, match text or state paths.
        provenance = _json({"schema_version": config.settings.schema_version,
                            "policy_version": config.settings.policy_version,
                            "confidence": config.settings.confidence.model_dump(mode="json"),
                            "scan": config.settings.scan.model_dump(mode="json"),
                            "category_ids": sorted(c.id for c in config.categories.categories),
                            "category_snapshot_version": 1,
                            "category_catalog_revision": category_revision,
                            "category_snapshot": category_snapshot,
                            "classification_rule_acceptance": [item.model_dump(mode="json") for item in
                                sorted(config.rules.accepted_classifications,
                                       key=lambda item: (item.rule_id, item.rule_version))]})
        with self._transaction():
            self._connection.execute(
                "INSERT INTO config_snapshots VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (fingerprint, semantic, config.settings.policy_version, provenance))
            for rule in sorted(config.rules.rules, key=lambda r: (r.id, r.version)):
                detail = {key: getattr(rule, key) for key in (
                    "enabled", "kind", "priority", "proposed_action", "priority_state", "protect", "approval_ref")}
                detail["category_ids"] = sorted(rule.category_ids)
                detail["retention"] = rule.retention.model_dump(mode="json") if rule.retention else None
                if detail["retention"]:
                    detail["retention"]["protected_types"].sort()
                self._connection.execute(
                    "INSERT INTO rule_versions VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (fingerprint, rule.id, rule.version, rule_scope_fingerprint(rule), _json(detail)))
        return fingerprint

    def configuration(self, fingerprint: str) -> dict | None:
        rows = self._rows("SELECT * FROM config_snapshots WHERE fingerprint=?", (fingerprint,))
        if not rows:
            return None
        result = rows[0]
        result["provenance"] = json.loads(result.pop("provenance_json"))
        return result

    def rule_versions(self, fingerprint: str) -> tuple[dict, ...]:
        rows = self._rows(
            "SELECT * FROM rule_versions WHERE config_fingerprint=? ORDER BY rule_id, version", (fingerprint,))
        for row in rows:
            row["provenance"] = json.loads(row.pop("provenance_json"))
        return tuple(rows)

    def start_scan(self, start: ScanStart) -> None:
        start = _validated(start, ScanStart)
        serialized = _record_json(start)
        with self._transaction():
            existing = self._connection.execute("SELECT start_json FROM scan_runs WHERE run_id=?", (start.run_id,)).fetchone()
            if existing:
                if existing[0] != serialized:
                    raise StorageError("Scan identity already has different provenance")
                return
            self._connection.execute("INSERT INTO accounts VALUES (?) ON CONFLICT DO NOTHING", (start.account_id,))
            self._connection.execute("INSERT INTO scan_runs VALUES (?, ?, ?, ?, NULL)",
                                     (start.run_id, start.account_id, start.config_fingerprint, serialized))

    def finish_scan(self, run_id: str, finish: ScanFinish) -> None:
        finish = _validated(finish, ScanFinish)
        serialized = _record_json(finish)
        with self._transaction():
            run = self.scan(run_id)
            if run is None:
                raise StorageError("Unknown scan run")
            if finish.ended_at < run.start.started_at:
                raise StorageError("Scan end precedes scan start")
            latest = self._connection.execute(
                "SELECT max(observed_at) FROM message_observations WHERE run_id=?", (run_id,)).fetchone()[0]
            if latest and finish.ended_at < datetime.fromisoformat(latest):
                raise StorageError("Scan end precedes a stored observation")
            if run.finish is not None and _record_json(run.finish) != serialized:
                raise StorageError("Completed scan history cannot be replaced")
            self._connection.execute("UPDATE scan_runs SET finish_json=? WHERE run_id=?", (serialized, run_id))

    def scan(self, run_id: str) -> ScanRecord | None:
        rows = self._rows("""SELECT start_json, finish_json,
            (SELECT count(*) FROM message_observations WHERE run_id=scan_runs.run_id) AS observed
            FROM scan_runs WHERE run_id=?""", (run_id,))
        if not rows:
            return None
        row = rows[0]
        return ScanRecord(start=ScanStart.model_validate_json(row["start_json"]),
                          finish=ScanFinish.model_validate_json(row["finish_json"]) if row["finish_json"] else None,
                          observed_unique_messages=row["observed"])

    def record_observation(
        self, run_id: str, message: MessageMetadata, classification: ClassificationResult,
        proposal: ActionProposal, *, observed_at: datetime,
    ) -> None:
        """Atomically store identity, labels, observation and immutable proposal.

        No audit event is fabricated; the upcoming audit layer may append a typed
        descriptive event explicitly. Classification is schema-validated, not
        recomputed; provenance consistency checks never establish action authority.
        """
        message = _validated(message, MessageMetadata)
        proposal = _validated(proposal, ActionProposal)
        if type(classification) is not ClassificationResult:
            raise StorageError("Storage requires a ClassificationResult")
        try:
            classification_json = _json(_CLASSIFICATION.dump_python(classification, mode="json"))
            classification = _CLASSIFICATION.validate_json(classification_json)
        except (ValidationError, ValueError, TypeError):
            raise StorageError("Invalid classification record") from None
        if (message.account_id, message.message_id) != (classification.account_id, classification.message_id) or (
            message.account_id, message.message_id) != (proposal.account_id, proposal.message_id):
            raise StorageError("Observation identities do not agree")
        if proposal.category_ids != classification.category_ids or proposal.classification_confidence != classification.classification_confidence:
            raise StorageError("Proposal and classification do not agree")
        timestamp = _time(observed_at)
        metadata_json = _json(message.model_dump(mode="json", exclude_unset=True))
        proposal_json = _record_json(proposal)
        with self._transaction():
            run = self.scan(run_id)
            if run is None or run.start.account_id != message.account_id:
                raise StorageError("Observation does not belong to the scan account")
            config = self.configuration(run.start.config_fingerprint)
            if proposal.policy_version != config["policy_version"] or classification.policy_version != config["policy_version"]:
                raise StorageError("Observation policy does not match scan provenance")
            known = {(row["rule_id"], row["version"]) for row in self.rule_versions(run.start.config_fingerprint)}
            assessed = {(a.rule_id, a.rule_version) for a in classification.assessments}
            references = (set(proposal.supporting_rules) | set(proposal.protection_rules)
                          | {constraint.rule for constraint in proposal.retention_constraints}
                          | set(classification.matched_rules) | set(classification.selected_rules)
                          | set(classification.protection_rules))
            if assessed != known or not references <= known:
                raise StorageError("Observation rule references do not match scan provenance")
            existing = self._connection.execute(
                """SELECT o.observed_at, o.metadata_json, o.classification_json, p.proposal_json
                   FROM message_observations o JOIN proposals p USING (run_id, message_id)
                   WHERE o.run_id=? AND o.message_id=?""", (run_id, message.message_id)).fetchone()
            if existing:
                if tuple(existing) != (timestamp, metadata_json, classification_json, proposal_json):
                    raise StorageError("Observation history cannot be replaced; use a new scan run")
                return
            if run.finish is not None:
                raise StorageError("Cannot add observations to a finished scan")
            if observed_at < run.start.started_at:
                raise StorageError("Observation precedes scan start")
            if run.observed_unique_messages >= run.start.limit:
                raise StorageError("Observation exceeds the recorded scan limit")
            self._connection.execute("INSERT INTO messages VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                                     (message.account_id, message.message_id, message.thread_id))
            for label in sorted(message.label_ids):
                self._connection.execute("INSERT INTO labels VALUES (?, ?) ON CONFLICT DO NOTHING", (message.account_id, label))
            self._connection.execute("INSERT INTO message_observations VALUES (?, ?, ?, ?, ?, ?)",
                                     (run_id, message.account_id, message.message_id, timestamp, metadata_json, classification_json))
            self._connection.execute("INSERT INTO proposals (run_id, message_id, proposal_json) VALUES (?, ?, ?)",
                                     (run_id, message.message_id, proposal_json))

    def messages(self, account_id: str) -> tuple[dict, ...]:
        """Identity records; thread_id is first-observed, never a deduplication key."""
        return tuple(self._rows("SELECT * FROM messages WHERE account_id=? ORDER BY message_id", (account_id,)))

    def observations(self, account_id: str, message_id: str) -> tuple[ObservationRecord, ...]:
        rows = self._rows("""SELECT * FROM message_observations WHERE account_id=? AND message_id=?
                             ORDER BY observed_at, run_id""", (account_id, message_id))
        return tuple(ObservationRecord(
            run_id=row["run_id"], observed_at=row["observed_at"],
            metadata=MessageMetadata.model_validate_json(row["metadata_json"]),
            classification=_CLASSIFICATION.validate_json(row["classification_json"])) for row in rows)

    def scan_observations(self, run_id: str) -> tuple[ObservationRecord, ...]:
        """Validated individual observations for one scan, ordered by message ID."""
        rows = self._rows("""SELECT * FROM message_observations WHERE run_id=?
                             ORDER BY message_id""", (run_id,))
        return tuple(ObservationRecord(
            run_id=row["run_id"], observed_at=row["observed_at"],
            metadata=MessageMetadata.model_validate_json(row["metadata_json"]),
            classification=_CLASSIFICATION.validate_json(row["classification_json"])) for row in rows)

    def proposals(self, run_id: str, message_id: str | None = None) -> tuple[ActionProposal, ...]:
        rows = self._rows("""SELECT proposal_json FROM proposals WHERE run_id=?
            AND (? IS NULL OR message_id=?) ORDER BY message_id""", (run_id, message_id, message_id))
        return tuple(ActionProposal.model_validate_json(row["proposal_json"]) for row in rows)

    def record_approval(self, description: ApprovalDescription) -> None:
        description = _validated(description, ApprovalDescription)
        serialized = _record_json(description)
        with self._transaction():
            existing = self._connection.execute("SELECT description_json FROM approvals WHERE record_id=?", (description.record_id,)).fetchone()
            if existing:
                if existing[0] != serialized:
                    raise StorageError("Approval description history cannot be replaced")
                return
            self._connection.execute("""INSERT INTO approvals
                (record_id, account_id, config_fingerprint, description_json) VALUES (?, ?, ?, ?)""",
                (description.record_id, description.account_id, description.config_fingerprint, serialized))

    def approvals(self, account_id: str) -> tuple[ApprovalDescription, ...]:
        return tuple(ApprovalDescription.model_validate_json(row["description_json"]) for row in self._rows(
            "SELECT description_json FROM approvals WHERE account_id=? ORDER BY record_id", (account_id,)))

    def append_audit_event(self, event: AuditEvent) -> None:
        event = _validated(event, AuditEvent)
        serialized = _record_json(event)
        with self._transaction():
            if event.event_type == "proposal" and not self.proposals(event.run_id, event.message_id):
                raise StorageError("Proposal event requires a stored proposal")
            existing = self._connection.execute("SELECT event_json FROM audit_events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing[0] != serialized:
                    raise StorageError("Audit event history cannot be replaced")
                return
            self._connection.execute("""INSERT INTO audit_events
                (event_id, run_id, message_id, event_json, state) VALUES (?, ?, ?, ?, ?)""",
                (event.event_id, event.run_id, event.message_id, serialized, event.state))

    def audit_events(self, run_id: str) -> tuple[AuditEvent, ...]:
        return tuple(AuditEvent.model_validate_json(row["event_json"]) for row in self._rows(
            "SELECT event_json FROM audit_events WHERE run_id=? ORDER BY event_id", (run_id,)))
