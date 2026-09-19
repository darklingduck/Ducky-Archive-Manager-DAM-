"""Explicit local M1 persistence, never execution or approval authority.

Open with Storage.open(settings); imports do no IO. POSIX ownership/mode checks
fail closed on unsupported systems, symlinks, shared state directories, and
non-private existing files. Existing permissions are never changed. SQLite uses
DELETE journals inside the private state directory, FULL synchronous writes,
foreign keys, explicit transactions, and schema version 5 (PRAGMA user_version).

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
from dam.gmail import GmailProfile, is_adapter_profile
from dam.items import (
    ClassificationWorkEvent, ClassificationWorkID, ClassificationWorkItem, ClassificationWorkMember,
    DamItem, MemberDecision, SourceInstance,
)
from dam.identifiers import new_object_id
from dam.models import (
    ConfigModel, Configuration, MessageMetadata, NonBlankText, NonNegativeInt,
    PositiveInt, Settings,
)

SCHEMA_VERSION = 5
_TABLES_V1 = frozenset({"accounts", "labels", "config_snapshots", "rule_versions", "approvals",
                     "scan_runs", "messages", "message_observations", "proposals", "audit_events"})
_TABLES = _TABLES_V1 | frozenset({"source_instances", "items", "classification_work_items",
                                "classification_work_members", "classification_work_events",
                                "classification_evaluations", "teaching_operations", "teaching_events"})
_TABLES_V4 = _TABLES - {"classification_evaluations", "teaching_operations", "teaching_events"}
_TRIGGERS_V2 = frozenset({"classification_work_events_no_update",
                          "classification_work_events_no_delete"})
_TRIGGERS_V4 = _TRIGGERS_V2 | frozenset({"verified_email_observation_insert",
                                        "verified_email_observation_update"})
_TRIGGERS_V5 = _TRIGGERS_V4 | frozenset({"classification_evaluations_no_update",
    "classification_evaluations_no_delete", "classification_evaluations_validate",
    "teaching_operations_validate", "teaching_operations_identity_no_update",
    "teaching_events_no_update", "teaching_events_no_delete"})
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
_SCHEMA_V2 = (
    """CREATE TABLE source_instances (
        source_instance_id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL,
        identity_status TEXT NOT NULL, source_identity TEXT NOT NULL,
        UNIQUE (provider, source_identity),
        CHECK (provider = 'synthetic' AND identity_status = 'synthetic'))""",
    """CREATE TABLE items (
        item_id TEXT PRIMARY KEY NOT NULL,
        source_instance_id TEXT NOT NULL REFERENCES source_instances(source_instance_id),
        item_kind TEXT NOT NULL CHECK (item_kind = 'email'), source_item_id TEXT NOT NULL,
        UNIQUE (source_instance_id, source_item_id))""",
    """CREATE TABLE classification_work_items (
        work_id TEXT PRIMARY KEY NOT NULL,
        representative_item_id TEXT NOT NULL REFERENCES items(item_id),
        state TEXT NOT NULL CHECK (state IN ('pending', 'deferred', 'resolved')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY (work_id, representative_item_id)
            REFERENCES classification_work_members(work_id, item_id)
            DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE classification_work_members (
        work_id TEXT NOT NULL REFERENCES classification_work_items(work_id),
        item_id TEXT NOT NULL REFERENCES items(item_id),
        state TEXT NOT NULL CHECK (state IN ('pending', 'deferred', 'resolved')),
        added_at TEXT NOT NULL, PRIMARY KEY (work_id, item_id))""",
    """CREATE UNIQUE INDEX one_open_classification_work_per_item
        ON classification_work_members(item_id) WHERE state != 'resolved'""",
    """CREATE TABLE classification_work_events (
        event_id INTEGER PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES classification_work_items(work_id),
        item_id TEXT REFERENCES items(item_id),
        event_type TEXT NOT NULL CHECK (event_type IN
            ('created', 'member_added', 'deferred', 'member_deferred', 'reevaluated', 'resolved')),
        occurred_at TEXT NOT NULL,
        prior_state TEXT CHECK (prior_state IN ('pending', 'deferred', 'resolved')),
        new_state TEXT NOT NULL CHECK (new_state IN ('pending', 'deferred', 'resolved')),
        config_fingerprint TEXT, category_permanent_ids_json TEXT NOT NULL DEFAULT '[]',
        teaching_required INTEGER CHECK (teaching_required IN (0, 1)),
        FOREIGN KEY (config_fingerprint) REFERENCES config_snapshots(fingerprint))""",
    """CREATE TRIGGER classification_work_events_no_update
        BEFORE UPDATE ON classification_work_events
        BEGIN SELECT RAISE(ABORT, 'classification work history is immutable'); END""",
    """CREATE TRIGGER classification_work_events_no_delete
        BEFORE DELETE ON classification_work_events
        BEGIN SELECT RAISE(ABORT, 'classification work history is immutable'); END""",
)
_SCHEMA_V3 = (
    """CREATE TABLE source_instances_v3 (
        source_instance_id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL,
        identity_status TEXT NOT NULL, source_identity TEXT NOT NULL,
        UNIQUE (provider, source_identity),
        CHECK ((provider = 'synthetic' AND identity_status = 'synthetic') OR
               (provider = 'gmail' AND identity_status = 'verified')),
        CHECK (length(trim(source_identity)) > 0),
        CHECK (source_identity != 'gmail-account-unverified'))""",
    """INSERT INTO source_instances_v3
        (source_instance_id, provider, identity_status, source_identity)
        SELECT source_instance_id, provider, identity_status, source_identity FROM source_instances""",
    "DROP TABLE source_instances",
    "ALTER TABLE source_instances_v3 RENAME TO source_instances",
)
_SCHEMA_V4 = (
    "ALTER TABLE message_observations ADD COLUMN item_id TEXT REFERENCES items(item_id)",
    """CREATE UNIQUE INDEX one_item_observation_per_run
        ON message_observations(run_id, item_id) WHERE item_id IS NOT NULL""",
    """CREATE TRIGGER verified_email_observation_insert
        BEFORE INSERT ON message_observations
        WHEN NEW.item_id IS NOT NULL OR EXISTS
            (SELECT 1 FROM source_instances WHERE source_instance_id=NEW.account_id AND provider='gmail')
        BEGIN
            SELECT RAISE(ABORT, 'linked email observation requires matching ITEM')
            WHERE NEW.item_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM items i JOIN source_instances s ON s.source_instance_id=i.source_instance_id
                WHERE i.item_id=NEW.item_id AND i.source_instance_id=NEW.account_id
                  AND i.source_item_id=NEW.message_id AND i.item_kind='email'
                  AND (s.provider!='gmail' OR s.identity_status='verified'));
        END""",
    """CREATE TRIGGER verified_email_observation_update
        BEFORE UPDATE ON message_observations
        WHEN OLD.item_id IS NOT NEW.item_id OR OLD.account_id IS NOT NEW.account_id
             OR OLD.message_id IS NOT NEW.message_id
        BEGIN SELECT RAISE(ABORT, 'verified observation identity is immutable'); END""",
)
_SCHEMA_V5 = (
    """CREATE TABLE classification_evaluations (
        evaluation_id TEXT PRIMARY KEY NOT NULL CHECK (length(evaluation_id)=31 AND
            substr(evaluation_id,1,5)='EVAL-' AND substr(evaluation_id,6) NOT GLOB '*[^A-Z2-7]*'),
        item_id TEXT NOT NULL REFERENCES items(item_id),
        observation_run_id TEXT NOT NULL, observation_message_id TEXT NOT NULL,
        predecessor_id TEXT UNIQUE REFERENCES classification_evaluations(evaluation_id),
        config_fingerprint TEXT NOT NULL REFERENCES config_snapshots(fingerprint),
        classification_json TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        cause TEXT NOT NULL CHECK (cause IN ('original_intake', 'human_teaching', 'explicit_reevaluation')),
        teaching_id TEXT REFERENCES teaching_operations(teaching_id),
        CHECK (predecessor_id IS NULL OR predecessor_id != evaluation_id),
        CHECK ((cause='human_teaching') = (teaching_id IS NOT NULL)),
        FOREIGN KEY (observation_run_id, observation_message_id)
            REFERENCES message_observations(run_id, message_id))""",
    """CREATE UNIQUE INDEX one_evaluation_per_teaching_item
        ON classification_evaluations(teaching_id, item_id) WHERE teaching_id IS NOT NULL""",
    """CREATE TRIGGER classification_evaluations_validate BEFORE INSERT ON classification_evaluations
        BEGIN
            SELECT RAISE(ABORT, 'evaluation observation must match ITEM') WHERE NOT EXISTS (
                SELECT 1 FROM message_observations o WHERE o.run_id=NEW.observation_run_id
                AND o.message_id=NEW.observation_message_id AND o.item_id=NEW.item_id);
            SELECT RAISE(ABORT, 'teaching evaluation has inconsistent provenance') WHERE
                NEW.teaching_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM teaching_operations t WHERE t.teaching_id=NEW.teaching_id
                    AND t.status IN ('pending_reevaluation', 'completed')
                    AND t.config_after=NEW.config_fingerprint);
            SELECT RAISE(ABORT, 'evaluation predecessor must be current for ITEM') WHERE
                (NEW.predecessor_id IS NULL AND EXISTS
                    (SELECT 1 FROM classification_evaluations WHERE item_id=NEW.item_id)) OR
                (NEW.predecessor_id IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM classification_evaluations p WHERE p.evaluation_id=NEW.predecessor_id
                     AND p.item_id=NEW.item_id AND NOT EXISTS
                        (SELECT 1 FROM classification_evaluations n WHERE n.predecessor_id=p.evaluation_id)));
        END""",
    """CREATE TRIGGER classification_evaluations_no_update BEFORE UPDATE ON classification_evaluations
        BEGIN SELECT RAISE(ABORT, 'classification evaluation history is immutable'); END""",
    """CREATE TRIGGER classification_evaluations_no_delete BEFORE DELETE ON classification_evaluations
        BEGIN SELECT RAISE(ABORT, 'classification evaluation history is immutable'); END""",
    """CREATE TABLE teaching_operations (
        teaching_id TEXT PRIMARY KEY NOT NULL CHECK (length(teaching_id)=32 AND
            substr(teaching_id,1,6)='TEACH-' AND substr(teaching_id,7) NOT GLOB '*[^A-Z2-7]*'),
        work_id TEXT NOT NULL REFERENCES classification_work_items(work_id),
        item_id TEXT NOT NULL REFERENCES items(item_id),
        observation_run_id TEXT NOT NULL, observation_message_id TEXT NOT NULL,
        category_permanent_id TEXT NOT NULL, candidate_fingerprint TEXT NOT NULL,
        preview_fingerprint TEXT NOT NULL, config_before TEXT NOT NULL REFERENCES config_snapshots(fingerprint),
        config_after TEXT REFERENCES config_snapshots(fingerprint),
        rule_id TEXT NOT NULL, rule_version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('intent', 'rule_saved', 'pending_reevaluation', 'completed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (work_id, item_id, candidate_fingerprint),
        FOREIGN KEY (work_id, item_id) REFERENCES classification_work_members(work_id, item_id),
        FOREIGN KEY (observation_run_id, observation_message_id)
            REFERENCES message_observations(run_id, message_id))""",
    """CREATE TRIGGER teaching_operations_validate BEFORE INSERT ON teaching_operations
        BEGIN SELECT RAISE(ABORT, 'teaching evidence must belong to exact ITEM')
        WHERE NOT EXISTS (SELECT 1 FROM message_observations o
            WHERE o.run_id=NEW.observation_run_id AND o.message_id=NEW.observation_message_id
              AND o.item_id=NEW.item_id); END""",
    """CREATE TRIGGER teaching_operations_identity_no_update BEFORE UPDATE ON teaching_operations
        WHEN OLD.teaching_id IS NOT NEW.teaching_id OR OLD.work_id IS NOT NEW.work_id
          OR OLD.item_id IS NOT NEW.item_id OR OLD.observation_run_id IS NOT NEW.observation_run_id
          OR OLD.observation_message_id IS NOT NEW.observation_message_id
          OR OLD.category_permanent_id IS NOT NEW.category_permanent_id
          OR OLD.candidate_fingerprint IS NOT NEW.candidate_fingerprint
          OR OLD.preview_fingerprint IS NOT NEW.preview_fingerprint
          OR OLD.config_before IS NOT NEW.config_before OR OLD.rule_id IS NOT NEW.rule_id
          OR OLD.rule_version IS NOT NEW.rule_version
        BEGIN SELECT RAISE(ABORT, 'teaching identity is immutable'); END""",
    """CREATE TABLE teaching_events (
        event_id INTEGER PRIMARY KEY, teaching_id TEXT NOT NULL REFERENCES teaching_operations(teaching_id),
        event_type TEXT NOT NULL CHECK (event_type IN
            ('confirmed', 'rule_saved', 'reevaluation_pending', 'completed')),
        occurred_at TEXT NOT NULL, config_fingerprint TEXT REFERENCES config_snapshots(fingerprint),
        UNIQUE (teaching_id, event_type))""",
    """CREATE TRIGGER teaching_events_no_update BEFORE UPDATE ON teaching_events
        BEGIN SELECT RAISE(ABORT, 'teaching history is immutable'); END""",
    """CREATE TRIGGER teaching_events_no_delete BEFORE DELETE ON teaching_events
        BEGIN SELECT RAISE(ABORT, 'teaching history is immutable'); END""",
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
            if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):
                raise StorageError("Unsupported database schema version; no migration performed")
            if version == 0 and connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchone():
                raise StorageError("Refusing to initialize an unversioned nonempty database")
            if version in (1, 2, 3, 4, SCHEMA_VERSION):
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                expected = _TABLES_V1 if version == 1 else _TABLES if version == SCHEMA_VERSION else _TABLES_V4
                if tables != expected:
                    raise StorageError("Database tables do not match the supported schema")
                if version in (2, 3, 4, SCHEMA_VERSION):
                    triggers = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'")}
                    expected_triggers = (_TRIGGERS_V5 if version == SCHEMA_VERSION else
                                         _TRIGGERS_V4 if version == 4 else _TRIGGERS_V2)
                    if triggers != expected_triggers:
                        raise StorageError("Database history protections do not match the supported schema")
            connection.execute("PRAGMA journal_mode = DELETE")
            storage = cls(connection, path)
            # SQLite cannot widen a CHECK constraint in place. Rebuild only the
            # source table inside one transaction, preserving every SRC and all
            # referencing ITEM/CWQ rows. FK integrity is checked before commit.
            if version < SCHEMA_VERSION:
                connection.execute("PRAGMA foreign_keys = OFF")
            try:
                with storage._transaction():
                    if version == 0:
                        for statement in _SCHEMA:
                            connection.execute(statement)
                    if version in (0, 1):
                        for statement in _SCHEMA_V2:
                            connection.execute(statement)
                    if version in (0, 1, 2):
                        for statement in _SCHEMA_V3:
                            connection.execute(statement)
                        connection.execute("PRAGMA user_version = 3")
                    if version in (0, 1, 2, 3):
                        for statement in _SCHEMA_V4:
                            connection.execute(statement)
                        connection.execute("PRAGMA user_version = 4")
                    if version in (0, 1, 2, 3, 4):
                        for statement in _SCHEMA_V5:
                            connection.execute(statement)
                        connection.execute("PRAGMA user_version = 5")
                    if connection.execute("PRAGMA foreign_key_check").fetchone():
                        raise StorageError("Database foreign-key integrity check failed")
            finally:
                if version < SCHEMA_VERSION:
                    connection.execute("PRAGMA foreign_keys = ON")
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
            self._connection.execute("""INSERT INTO message_observations
                (run_id, account_id, message_id, observed_at, metadata_json, classification_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
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

    def email_observations_for_item(self, item_id: str) -> tuple[ObservationRecord, ...]:
        """Return immutable email evidence for an ITEM, without guessing legacy links."""
        rows = self._rows("""SELECT run_id, observed_at, metadata_json, classification_json
            FROM message_observations WHERE item_id=? ORDER BY observed_at, run_id""", (item_id,))
        return tuple(ObservationRecord(
            run_id=row["run_id"], observed_at=row["observed_at"],
            metadata=MessageMetadata.model_validate_json(row["metadata_json"]),
            classification=_CLASSIFICATION.validate_json(row["classification_json"])) for row in rows)

    def latest_email_observation(self, item_id: str) -> ObservationRecord | None:
        rows = self._rows("""SELECT run_id, observed_at, metadata_json, classification_json
            FROM message_observations WHERE item_id=?
            ORDER BY observed_at DESC, run_id DESC LIMIT 1""", (item_id,))
        if not rows:
            return None
        row = rows[0]
        return ObservationRecord(run_id=row["run_id"], observed_at=row["observed_at"],
            metadata=MessageMetadata.model_validate_json(row["metadata_json"]),
            classification=_CLASSIFICATION.validate_json(row["classification_json"]))

    def evaluation_history(self, item_id: str) -> tuple[dict, ...]:
        """Return the immutable predecessor chain, oldest first."""
        return tuple(self._rows("""SELECT * FROM classification_evaluations WHERE item_id=?
            ORDER BY rowid""", (item_id,)))

    def current_evaluation(self, item_id: str) -> dict | None:
        rows = self._rows("""SELECT e.* FROM classification_evaluations e WHERE e.item_id=?
            AND NOT EXISTS (SELECT 1 FROM classification_evaluations n
                            WHERE n.predecessor_id=e.evaluation_id)""", (item_id,))
        if len(rows) > 1:
            raise StorageError("Contradictory classification evaluation history")
        return rows[0] if rows else None

    def teaching_operation(self, teaching_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM teaching_operations WHERE teaching_id=?", (teaching_id,))
        return rows[0] if rows else None

    def incomplete_teaching_operations(self) -> tuple[dict, ...]:
        return tuple(self._rows("""SELECT * FROM teaching_operations
            WHERE status != 'completed' ORDER BY created_at, teaching_id"""))

    def teaching_for_preview(self, work_id: str, item_id: str, fingerprint: str) -> dict | None:
        rows = self._rows("""SELECT * FROM teaching_operations
            WHERE work_id=? AND item_id=? AND preview_fingerprint=?""",
            (work_id, item_id, fingerprint))
        return rows[0] if rows else None

    def teaching_by_preview(self, fingerprint: str) -> dict | None:
        rows = self._rows("SELECT * FROM teaching_operations WHERE preview_fingerprint=?",
                          (fingerprint,))
        return rows[0] if rows else None

    def teaching_events(self, teaching_id: str) -> tuple[dict, ...]:
        return tuple(self._rows("SELECT * FROM teaching_events WHERE teaching_id=? ORDER BY event_id",
                                (teaching_id,)))

    def teaching_evaluation_counts(self, teaching_id: str) -> tuple[int, int, int]:
        rows = self._rows("""SELECT classification_json FROM classification_evaluations
            WHERE teaching_id=? ORDER BY evaluation_id""", (teaching_id,))
        results = tuple(_CLASSIFICATION.validate_json(row["classification_json"]) for row in rows)
        resolved = sum(result.category_teaching_required is False and
                       bool(result.category_permanent_ids) for result in results)
        return len(results), resolved, len(results) - resolved

    def create_teaching_intent(self, *, teaching_id: str, work_id: str, item_id: str,
                               observation_run_id: str, observation_message_id: str,
                               category_permanent_id: str, candidate_fingerprint: str,
                               preview_fingerprint: str, config_before: str,
                               rule_id: str, rule_version: int, occurred_at: datetime) -> dict:
        timestamp = _time(occurred_at)
        with self._transaction():
            work = self.classification_work(work_id)
            if work is None or work.state == "resolved" or item_id not in {m.item_id for m in work.members}:
                raise StorageError("Teaching requires active exact classification work")
            observation = self._connection.execute("""SELECT 1 FROM message_observations
                WHERE run_id=? AND message_id=? AND item_id=?""",
                (observation_run_id, observation_message_id, item_id)).fetchone()
            if observation is None or self.configuration(config_before) is None:
                raise StorageError("Teaching evidence or configuration is unavailable")
            prior = self._connection.execute("""SELECT * FROM teaching_operations
                WHERE work_id=? AND item_id=? AND candidate_fingerprint=?""",
                (work_id, item_id, candidate_fingerprint)).fetchone()
            if prior:
                if prior["preview_fingerprint"] != preview_fingerprint or prior["category_permanent_id"] != category_permanent_id:
                    raise StorageError("Teaching identity has different provenance")
                return dict(prior)
            self._connection.execute("""INSERT INTO teaching_operations VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'intent', ?, ?)""",
                (teaching_id, work_id, item_id, observation_run_id, observation_message_id,
                 category_permanent_id, candidate_fingerprint, preview_fingerprint,
                 config_before, rule_id, rule_version, timestamp, timestamp))
            self._connection.execute("""INSERT INTO teaching_events
                (teaching_id, event_type, occurred_at) VALUES (?, 'confirmed', ?)""",
                (teaching_id, timestamp))
        return self.teaching_operation(teaching_id)

    def advance_teaching(self, teaching_id: str, status: str, *, occurred_at: datetime,
                         config_after: str | None = None) -> dict:
        stages = {"intent": 0, "rule_saved": 1, "pending_reevaluation": 2, "completed": 3}
        if status not in stages:
            raise StorageError("Invalid teaching progress")
        timestamp = _time(occurred_at)
        with self._transaction():
            row = self._connection.execute("SELECT * FROM teaching_operations WHERE teaching_id=?",
                                           (teaching_id,)).fetchone()
            if row is None or stages[status] > stages[row["status"]] + 1:
                raise StorageError("Invalid teaching transition")
            if stages[status] <= stages[row["status"]]:
                return dict(row)
            if status in ("pending_reevaluation", "completed") and not (config_after or row["config_after"]):
                raise StorageError("Teaching result configuration is unavailable")
            if config_after and self.configuration(config_after) is None:
                raise StorageError("Unknown teaching result configuration")
            self._connection.execute("""UPDATE teaching_operations SET status=?,
                config_after=COALESCE(?, config_after), updated_at=? WHERE teaching_id=?""",
                (status, config_after, timestamp, teaching_id))
            self._connection.execute("""INSERT INTO teaching_events
                (teaching_id, event_type, occurred_at, config_fingerprint) VALUES (?, ?, ?, ?)""",
                (teaching_id, "reevaluation_pending" if status == "pending_reevaluation" else status,
                 timestamp, config_after or row["config_after"]))
        return self.teaching_operation(teaching_id)

    def _append_evaluation(self, item_id: str, observation_run_id: str, message_id: str,
                           classification_json: str, config_fingerprint: str,
                           evaluated_at: datetime, cause: str,
                           teaching_id: str | None = None) -> str:
        current = self.current_evaluation(item_id)
        evaluation_id = new_object_id("EVAL")
        self._connection.execute("""INSERT INTO classification_evaluations
            (evaluation_id, item_id, observation_run_id, observation_message_id,
             predecessor_id, config_fingerprint, classification_json, evaluated_at, cause, teaching_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (evaluation_id, item_id, observation_run_id, message_id,
             current["evaluation_id"] if current else None, config_fingerprint,
             classification_json, _time(evaluated_at), cause, teaching_id))
        return evaluation_id

    def append_item_evaluation(self, item_id: str, observation_run_id: str,
                               result: ClassificationResult, *, config_fingerprint: str,
                               evaluated_at: datetime, cause: str = "explicit_reevaluation") -> dict:
        """General exact-ITEM local evaluation, independent of a teaching operation."""
        if cause != "explicit_reevaluation" or type(result) is not ClassificationResult:
            raise StorageError("Invalid local evaluation cause or result")
        serialized = _json(_CLASSIFICATION.dump_python(result, mode="json"))
        with self._transaction():
            item = self.item(item_id)
            if item is None or item.source_instance_id != result.account_id or item.source_item_id != result.message_id:
                raise StorageError("Evaluation does not match exact ITEM")
            if self.configuration(config_fingerprint) is None:
                raise StorageError("Unknown evaluation configuration")
            evaluation_id = self._append_evaluation(item_id, observation_run_id, result.message_id,
                serialized, config_fingerprint, evaluated_at, cause)
        return self._rows("SELECT * FROM classification_evaluations WHERE evaluation_id=?", (evaluation_id,))[0]

    def work_evaluated_for_teaching(self, work_id: str, teaching_id: str) -> bool:
        row = self._rows("""SELECT count(*) AS missing FROM classification_work_members m
            WHERE m.work_id=? AND NOT EXISTS (SELECT 1 FROM classification_evaluations e
                WHERE e.item_id=m.item_id AND e.teaching_id=?)""", (work_id, teaching_id))
        return bool(row) and row[0]["missing"] == 0

    def record_verified_gmail_intake(
        self, run_id: str, message: MessageMetadata, classification: ClassificationResult,
        proposal: ActionProposal, *, observed_at: datetime,
    ) -> tuple[DamItem, ClassificationWorkItem | None]:
        """Commit one verified Gmail ITEM, observation, proposal and needed work atomically.

        The application service obtains the authenticated profile before this
        trusted storage primitive is called. No Gmail request runs in this transaction.
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
        if ((message.account_id, message.message_id) != (classification.account_id, classification.message_id) or
                (message.account_id, message.message_id) != (proposal.account_id, proposal.message_id) or
                proposal.category_ids != classification.category_ids or
                proposal.classification_confidence != classification.classification_confidence or
                proposal.authority_established or proposal.executable):
            raise StorageError("Durable intake identities or authority disagree")
        timestamp = _time(observed_at)
        metadata_json = _json(message.model_dump(mode="json", exclude_unset=True))
        proposal_json = _record_json(proposal)
        item_id: str | None = None
        work_id: str | None = None
        with self._transaction():
            source = self._connection.execute("""SELECT provider, identity_status FROM source_instances
                WHERE source_instance_id=?""", (message.account_id,)).fetchone()
            if source is None or tuple(source) != ("gmail", "verified"):
                raise StorageError("Verified Gmail source is required for durable intake")
            run = self.scan(run_id)
            if run is None or run.start.account_id != message.account_id or run.finish is not None:
                raise StorageError("Durable intake requires an open scan for the verified source")
            config = self.configuration(run.start.config_fingerprint)
            if (config is None or proposal.policy_version != config["policy_version"] or
                    classification.policy_version != config["policy_version"]):
                raise StorageError("Intake policy does not match scan provenance")
            known = {(row["rule_id"], row["version"]) for row in self.rule_versions(run.start.config_fingerprint)}
            assessed = {(a.rule_id, a.rule_version) for a in classification.assessments}
            references = (set(proposal.supporting_rules) | set(proposal.protection_rules)
                          | {constraint.rule for constraint in proposal.retention_constraints}
                          | set(classification.matched_rules) | set(classification.selected_rules)
                          | set(classification.protection_rules))
            if assessed != known or not references <= known:
                raise StorageError("Intake rule references do not match scan provenance")
            existing = self._connection.execute("""SELECT o.observed_at, o.metadata_json,
                o.classification_json, o.item_id, p.proposal_json FROM message_observations o
                JOIN proposals p USING (run_id, message_id) WHERE o.run_id=? AND o.message_id=?""",
                (run_id, message.message_id)).fetchone()
            if existing is not None:
                if tuple(existing)[:3] != (timestamp, metadata_json, classification_json) or existing[4] != proposal_json or not existing[3]:
                    raise StorageError("Observation history cannot be replaced; use a new scan run")
                item_id = existing[3]
            else:
                if observed_at < run.start.started_at or run.observed_unique_messages >= run.start.limit:
                    raise StorageError("Observation exceeds scan time or limit")
                native = self._connection.execute("""SELECT item_id, item_kind FROM items
                    WHERE source_instance_id=? AND source_item_id=?""",
                    (message.account_id, message.message_id)).fetchone()
                if native is not None:
                    if native["item_kind"] != "email":
                        raise StorageError("Native item identity has a different kind")
                    item_id = native["item_id"]
                else:
                    for _ in range(16):
                        candidate = new_object_id("ITEM")
                        if self._connection.execute("SELECT 1 FROM items WHERE item_id=?", (candidate,)).fetchone() is None:
                            item_id = candidate
                            break
                    if item_id is None:
                        raise StorageError("Cannot allocate a DAM Item identity")
                    self._connection.execute("INSERT INTO items VALUES (?, ?, 'email', ?)",
                                             (item_id, message.account_id, message.message_id))
                self._connection.execute("INSERT INTO messages VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                                         (message.account_id, message.message_id, message.thread_id))
                for label in sorted(message.label_ids):
                    self._connection.execute("INSERT INTO labels VALUES (?, ?) ON CONFLICT DO NOTHING",
                                             (message.account_id, label))
                self._connection.execute("""INSERT INTO message_observations
                    (run_id, account_id, message_id, observed_at, metadata_json, classification_json, item_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, message.account_id, message.message_id, timestamp,
                     metadata_json, classification_json, item_id))
                self._connection.execute("INSERT INTO proposals (run_id, message_id, proposal_json) VALUES (?, ?, ?)",
                                         (run_id, message.message_id, proposal_json))
                self._append_evaluation(item_id, run_id, message.message_id, classification_json,
                                        run.start.config_fingerprint, observed_at, "original_intake")
            if classification.category_teaching_required is True and not classification.category_ids:
                current = self._connection.execute("""SELECT w.work_id FROM classification_work_items w
                    JOIN classification_work_members m ON m.work_id=w.work_id
                    WHERE m.item_id=? AND w.state!='resolved' AND m.state!='resolved'""",
                    (item_id,)).fetchone()
                if current is not None:
                    work_id = current["work_id"]
                else:
                    for _ in range(16):
                        candidate = new_object_id("CWQ")
                        if self._connection.execute("SELECT 1 FROM classification_work_items WHERE work_id=?", (candidate,)).fetchone() is None:
                            work_id = candidate
                            break
                    if work_id is None:
                        raise StorageError("Cannot allocate classification work identity")
                    self._connection.execute("INSERT INTO classification_work_items VALUES (?, ?, 'pending', ?, ?)",
                                             (work_id, item_id, timestamp, timestamp))
                    self._connection.execute("INSERT INTO classification_work_members VALUES (?, ?, 'pending', ?)",
                                             (work_id, item_id, timestamp))
                    decision = MemberDecision(item_id=item_id, category_permanent_ids=(),
                        teaching_required=True, config_fingerprint=run.start.config_fingerprint)
                    self._work_event(work_id, item_id, "created", observed_at, None, "pending", decision)
        return self.item(item_id), self.classification_work(work_id) if work_id else None

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

    # Step 13 identity and workflow storage is separate from legacy Gmail history.
    # No existing message/observation/audit row is reinterpreted or rewritten.
    def register_source_instance(self, source: SourceInstance, *,
                                 verified_profile: GmailProfile | None = None) -> SourceInstance:
        source = _validated(source, SourceInstance)
        if source.provider == "gmail":
            if not is_adapter_profile(verified_profile) or verified_profile.email_address != source.source_identity:
                raise StorageError("Verified Gmail profile is required for source registration")
        elif verified_profile is not None:
            raise StorageError("Synthetic source registration cannot use a Gmail profile")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM source_instances WHERE source_instance_id=?",
                (source.source_instance_id,)).fetchone()
            native = self._connection.execute(
                "SELECT * FROM source_instances WHERE provider=? AND source_identity=?",
                (source.provider, source.source_identity)).fetchone()
            if row is None and native is not None:
                return SourceInstance.model_validate(dict(native))
            if row is None:
                self._connection.execute("INSERT INTO source_instances VALUES (?, ?, ?, ?)",
                    (source.source_instance_id, source.provider, source.identity_status,
                     source.source_identity))
            elif dict(row) != source.model_dump():
                raise StorageError("Source instance identity already has different provenance")
        return source

    def source_instance_by_identity(self, provider: str, source_identity: str) -> SourceInstance | None:
        rows = self._rows("SELECT * FROM source_instances WHERE provider=? AND source_identity=?",
                          (provider, source_identity))
        return SourceInstance.model_validate(rows[0]) if rows else None

    def source_instance(self, source_instance_id: str) -> SourceInstance | None:
        rows = self._rows("SELECT * FROM source_instances WHERE source_instance_id=?", (source_instance_id,))
        return SourceInstance.model_validate(rows[0]) if rows else None

    def register_item(self, item: DamItem) -> DamItem:
        item = _validated(item, DamItem)
        with self._transaction():
            if self._connection.execute("SELECT 1 FROM source_instances WHERE source_instance_id=?",
                                        (item.source_instance_id,)).fetchone() is None:
                raise StorageError("Unknown source instance")
            native = self._connection.execute(
                "SELECT * FROM items WHERE source_instance_id=? AND source_item_id=?",
                (item.source_instance_id, item.source_item_id)).fetchone()
            if native is not None:
                if native["item_kind"] != item.item_kind:
                    raise StorageError("Native item identity has a different kind")
                return DamItem.model_validate(dict(native))
            if self._connection.execute("SELECT 1 FROM items WHERE item_id=?", (item.item_id,)).fetchone():
                raise StorageError("DAM Item ID collision")
            self._connection.execute("INSERT INTO items VALUES (?, ?, ?, ?)",
                (item.item_id, item.source_instance_id, item.item_kind, item.source_item_id))
        return item

    def item(self, item_id: str) -> DamItem | None:
        rows = self._rows("SELECT * FROM items WHERE item_id=?", (item_id,))
        return DamItem.model_validate(rows[0]) if rows else None

    def item_by_native_identity(self, source_instance_id: str, source_item_id: str) -> DamItem | None:
        rows = self._rows("SELECT * FROM items WHERE source_instance_id=? AND source_item_id=?",
                          (source_instance_id, source_item_id))
        return DamItem.model_validate(rows[0]) if rows else None

    def _work_event(self, work_id: str, item_id: str | None, event_type: str,
                    occurred_at: datetime, prior_state: str | None, new_state: str,
                    decision: MemberDecision | None = None) -> None:
        self._connection.execute("""INSERT INTO classification_work_events
            (work_id, item_id, event_type, occurred_at, prior_state, new_state,
             config_fingerprint, category_permanent_ids_json, teaching_required)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (work_id, item_id, event_type, _time(occurred_at), prior_state, new_state,
             decision.config_fingerprint if decision else None,
             _json(list(decision.category_permanent_ids)) if decision else "[]",
             int(decision.teaching_required) if decision and decision.teaching_required is not None else None))

    def active_classification_work_for_item(self, item_id: str) -> ClassificationWorkItem | None:
        rows = self._rows("""SELECT w.work_id FROM classification_work_items w
            JOIN classification_work_members m ON m.work_id=w.work_id
            WHERE m.item_id=? AND w.state!='resolved' ORDER BY w.created_at, w.work_id""", (item_id,))
        return self.classification_work(rows[0]["work_id"]) if rows else None

    def create_classification_work(self, work_id: str, decision: MemberDecision,
                                   *, occurred_at: datetime) -> ClassificationWorkItem:
        decision = _validated(decision, MemberDecision)
        # Validate typed ID without inventing a separate unchecked storage path.
        try:
            TypeAdapter(ClassificationWorkID).validate_python(work_id)
        except ValidationError:
            raise StorageError("Invalid classification work identity") from None
        timestamp = _time(occurred_at)
        if decision.teaching_required is not True or decision.category_permanent_ids:
            raise StorageError("Classification work requires unresolved category teaching")
        with self._transaction():
            if self._connection.execute("SELECT 1 FROM items WHERE item_id=?", (decision.item_id,)).fetchone() is None:
                raise StorageError("Unknown DAM Item")
            if self._connection.execute("SELECT 1 FROM classification_work_items WHERE work_id=?", (work_id,)).fetchone():
                raise StorageError("Classification work ID collision")
            if self._connection.execute("""SELECT 1 FROM classification_work_members m
                JOIN classification_work_items w ON w.work_id=m.work_id
                WHERE m.item_id=? AND w.state!='resolved'""", (decision.item_id,)).fetchone():
                raise StorageError("Item already belongs to unresolved classification work")
            self._connection.execute("INSERT INTO classification_work_items VALUES (?, ?, 'pending', ?, ?)",
                                     (work_id, decision.item_id, timestamp, timestamp))
            self._connection.execute("INSERT INTO classification_work_members VALUES (?, ?, 'pending', ?)",
                                     (work_id, decision.item_id, timestamp))
            self._work_event(work_id, decision.item_id, "created", occurred_at, None, "pending", decision)
        return self.classification_work(work_id)

    def add_classification_member(self, work_id: str, decision: MemberDecision,
                                  *, occurred_at: datetime) -> ClassificationWorkItem:
        decision = _validated(decision, MemberDecision)
        if decision.teaching_required is not True or decision.category_permanent_ids:
            raise StorageError("Related member still requires classification teaching")
        timestamp = _time(occurred_at)
        with self._transaction():
            work = self._connection.execute("""SELECT w.state, w.updated_at, i.source_instance_id
                FROM classification_work_items w
                JOIN items i ON i.item_id=w.representative_item_id
                WHERE w.work_id=?""", (work_id,)).fetchone()
            if work is None or work["state"] == "resolved":
                raise StorageError("Unknown or resolved classification work")
            if timestamp < work["updated_at"]:
                raise StorageError("Work transition precedes its current state")
            member_item = self._connection.execute("SELECT source_instance_id FROM items WHERE item_id=?",
                                                   (decision.item_id,)).fetchone()
            if member_item is None:
                raise StorageError("Unknown DAM Item")
            if member_item["source_instance_id"] != work["source_instance_id"]:
                raise StorageError("Related member belongs to a different source instance")
            if self._connection.execute("SELECT 1 FROM classification_work_members WHERE work_id=? AND item_id=?",
                                        (work_id, decision.item_id)).fetchone():
                raise StorageError("Item is already a member")
            if self._connection.execute("""SELECT 1 FROM classification_work_members m
                JOIN classification_work_items w ON w.work_id=m.work_id
                WHERE m.item_id=? AND w.state!='resolved'""", (decision.item_id,)).fetchone():
                raise StorageError("Item already belongs to unresolved classification work")
            state = work["state"]
            self._connection.execute("INSERT INTO classification_work_members VALUES (?, ?, ?, ?)",
                                     (work_id, decision.item_id, state, timestamp))
            self._connection.execute("UPDATE classification_work_items SET updated_at=? WHERE work_id=?",
                                     (timestamp, work_id))
            self._work_event(work_id, decision.item_id, "member_added", occurred_at, None, state, decision)
        return self.classification_work(work_id)

    def classification_work(self, work_id: str) -> ClassificationWorkItem | None:
        rows = self._rows("SELECT * FROM classification_work_items WHERE work_id=?", (work_id,))
        if not rows:
            return None
        row = rows[0]
        members = tuple(ClassificationWorkMember.model_validate(member) for member in self._rows(
            "SELECT item_id, state, added_at FROM classification_work_members WHERE work_id=? ORDER BY item_id",
            (work_id,)))
        return ClassificationWorkItem(work_id=row["work_id"],
            representative_item_id=row["representative_item_id"], state=row["state"],
            created_at=row["created_at"], updated_at=row["updated_at"], members=members)

    def classification_work_list(self, *, state: str | None = None) -> tuple[ClassificationWorkItem, ...]:
        if state is not None and state not in ("pending", "deferred", "resolved"):
            raise StorageError("Invalid classification work state")
        rows = self._rows("""SELECT work_id FROM classification_work_items
            WHERE (? IS NULL OR state=?) ORDER BY created_at, work_id""", (state, state))
        return tuple(self.classification_work(row["work_id"]) for row in rows)

    def active_work_ids_after(self, cursor: str = "", *, limit: int = 32) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise StorageError("Invalid classification work page size")
        rows = self._rows("""SELECT work_id FROM classification_work_items
            WHERE state!='resolved' AND work_id>? ORDER BY work_id LIMIT ?""", (cursor, limit))
        return tuple(row["work_id"] for row in rows)

    def classification_work_events(self, work_id: str) -> tuple[ClassificationWorkEvent, ...]:
        rows = self._rows("SELECT * FROM classification_work_events WHERE work_id=? ORDER BY event_id", (work_id,))
        return tuple(ClassificationWorkEvent.model_validate({
            "event_id": row["event_id"], "work_id": row["work_id"], "item_id": row["item_id"],
            "event_type": row["event_type"], "occurred_at": row["occurred_at"],
            "prior_state": row["prior_state"], "new_state": row["new_state"],
            "config_fingerprint": row["config_fingerprint"],
            "category_permanent_ids": tuple(json.loads(row["category_permanent_ids_json"])),
            "teaching_required": None if row["teaching_required"] is None else bool(row["teaching_required"]),
        }) for row in rows)

    def defer_classification_work(self, work_id: str, *, occurred_at: datetime) -> ClassificationWorkItem:
        timestamp = _time(occurred_at)
        with self._transaction():
            work = self._connection.execute("SELECT state, updated_at FROM classification_work_items WHERE work_id=?",
                                            (work_id,)).fetchone()
            if work is None or work["state"] == "resolved":
                raise StorageError("Unknown or resolved classification work")
            if timestamp < work["updated_at"]:
                raise StorageError("Work transition precedes its current state")
            if work["state"] == "pending":
                members = self._connection.execute("""SELECT item_id FROM classification_work_members
                    WHERE work_id=? AND state='pending' ORDER BY item_id""", (work_id,)).fetchall()
                self._connection.execute("UPDATE classification_work_items SET state='deferred', updated_at=? WHERE work_id=?",
                                         (timestamp, work_id))
                self._work_event(work_id, None, "deferred", occurred_at, "pending", "deferred")
                for member in members:
                    self._connection.execute("UPDATE classification_work_members SET state='deferred' WHERE work_id=? AND item_id=?",
                                             (work_id, member["item_id"]))
                    self._work_event(work_id, member["item_id"], "member_deferred", occurred_at,
                                     "pending", "deferred")
        return self.classification_work(work_id)

    def apply_classification_reevaluation(self, work_id: str, decisions: tuple[MemberDecision, ...],
                                          *, occurred_at: datetime) -> ClassificationWorkItem:
        decisions = tuple(_validated(item, MemberDecision) for item in decisions)
        timestamp = _time(occurred_at)
        with self._transaction():
            work = self._connection.execute("SELECT state, updated_at FROM classification_work_items WHERE work_id=?",
                                            (work_id,)).fetchone()
            if work is None or work["state"] == "resolved":
                raise StorageError("Unknown or resolved classification work")
            if timestamp < work["updated_at"]:
                raise StorageError("Work transition precedes its current state")
            rows = self._connection.execute("""SELECT item_id, state FROM classification_work_members
                WHERE work_id=? ORDER BY item_id""", (work_id,)).fetchall()
            by_id = {row["item_id"]: row["state"] for row in rows}
            if len(decisions) != len(by_id) or {item.item_id for item in decisions} != set(by_id):
                raise StorageError("Reevaluation must cover every exact member once")
            if len({item.config_fingerprint for item in decisions}) != 1:
                raise StorageError("Reevaluation decisions must use one configuration")
            transitions = []
            for decision in sorted(decisions, key=lambda item: item.item_id):
                prior = by_id[decision.item_id]
                resolved = decision.teaching_required is False and bool(decision.category_permanent_ids)
                if prior == "resolved" and not resolved:
                    raise StorageError("Resolved member cannot reopen without an explicit workflow")
                new_state = "resolved" if resolved else prior
                transitions.append((decision, prior, new_state))
            for decision, prior, new_state in transitions:
                if new_state != prior:
                    self._connection.execute("""UPDATE classification_work_members SET state=?
                        WHERE work_id=? AND item_id=?""", (new_state, work_id, decision.item_id))
                self._work_event(work_id, decision.item_id, "reevaluated", occurred_at,
                                 prior, new_state, decision)
            still_open = self._connection.execute("""SELECT 1 FROM classification_work_members
                WHERE work_id=? AND state!='resolved' LIMIT 1""", (work_id,)).fetchone()
            state = work["state"] if still_open else "resolved"
            self._connection.execute("UPDATE classification_work_items SET state=?, updated_at=? WHERE work_id=?",
                                     (state, timestamp, work_id))
            if state == "resolved":
                self._work_event(work_id, None, "resolved", occurred_at, work["state"], "resolved")
        return self.classification_work(work_id)

    def record_teaching_work_reevaluation(self, work_id: str,
        decisions: tuple[tuple[str, str, str, ClassificationResult], ...], *,
        config_fingerprint: str, teaching_id: str, occurred_at: datetime) -> ClassificationWorkItem:
        """Append exact-item evaluations and queue transitions in one local transaction."""
        timestamp = _time(occurred_at)
        with self._transaction():
            teaching = self._connection.execute("SELECT status, config_after FROM teaching_operations WHERE teaching_id=?",
                                                (teaching_id,)).fetchone()
            if teaching is None or teaching["status"] not in ("pending_reevaluation", "completed") or teaching["config_after"] != config_fingerprint:
                raise StorageError("Teaching is not ready for reevaluation")
            work = self._connection.execute("SELECT state, updated_at FROM classification_work_items WHERE work_id=?",
                                            (work_id,)).fetchone()
            if work is None:
                raise StorageError("Unknown classification work")
            rows = self._connection.execute("SELECT item_id, state FROM classification_work_members WHERE work_id=? ORDER BY item_id",
                                            (work_id,)).fetchall()
            by_id = {row["item_id"]: row["state"] for row in rows}
            if len(decisions) != len(by_id) or {entry[0] for entry in decisions} != set(by_id):
                raise StorageError("Reevaluation must cover each exact work member")
            if work["state"] == "resolved":
                if all(self._connection.execute("""SELECT 1 FROM classification_evaluations
                    WHERE item_id=? AND teaching_id=?""", (item_id, teaching_id)).fetchone()
                       for item_id in by_id):
                    return self.classification_work(work_id)
                raise StorageError("Resolved work cannot be reevaluated")
            if timestamp < work["updated_at"]:
                raise StorageError("Work transition precedes its current state")
            for item_id, run_id, message_id, result in sorted(decisions, key=lambda entry: entry[0]):
                if type(result) is not ClassificationResult or result.message_id != message_id:
                    raise StorageError("Invalid exact-item reevaluation")
                native = self.item(item_id)
                if native is None or native.source_item_id != message_id or result.account_id != native.source_instance_id:
                    raise StorageError("Reevaluation identity disagrees with ITEM")
                serialized = _json(_CLASSIFICATION.dump_python(result, mode="json"))
                existing = self._connection.execute("""SELECT classification_json, config_fingerprint
                    FROM classification_evaluations WHERE item_id=? AND teaching_id=?""",
                    (item_id, teaching_id)).fetchone()
                if existing is not None:
                    if tuple(existing) != (serialized, config_fingerprint):
                        raise StorageError("Teaching reevaluation already has different provenance")
                    continue
                self._append_evaluation(item_id, run_id, message_id, serialized,
                                        config_fingerprint, occurred_at, "human_teaching", teaching_id)
                prior = by_id[item_id]
                resolved = result.category_teaching_required is False and bool(result.category_permanent_ids)
                if prior == "resolved" and not resolved:
                    raise StorageError("Resolved member cannot reopen implicitly")
                new_state = "resolved" if resolved else prior
                if new_state != prior:
                    self._connection.execute("UPDATE classification_work_members SET state=? WHERE work_id=? AND item_id=?",
                                             (new_state, work_id, item_id))
                self._work_event(work_id, item_id, "reevaluated", occurred_at, prior, new_state,
                    MemberDecision(item_id=item_id,
                        category_permanent_ids=result.category_permanent_ids or (),
                        teaching_required=result.category_teaching_required,
                        config_fingerprint=config_fingerprint))
            still_open = self._connection.execute("""SELECT 1 FROM classification_work_members
                WHERE work_id=? AND state!='resolved' LIMIT 1""", (work_id,)).fetchone()
            new_work_state = work["state"] if still_open else "resolved"
            self._connection.execute("UPDATE classification_work_items SET state=?, updated_at=? WHERE work_id=?",
                                     (new_work_state, timestamp, work_id))
            if new_work_state == "resolved":
                self._work_event(work_id, None, "resolved", occurred_at, work["state"], "resolved")
        return self.classification_work(work_id)
