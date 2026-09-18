"""Private, preview-first DAM category catalog. No Gmail or action authority.

IDs use CAT- plus 26 RFC 4648 base32 characters encoding 128 UUIDv4 bits.
The same generator accepts future type prefixes; this module issues CAT only.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError, field_validator, model_validator
import yaml

from dam.config import UniqueKeySafeLoader, configuration_fingerprint
from dam.identifiers import new_object_id as _new_object_id
from dam.models import CategoriesConfig, Category, CategoryKey, CategoryPermanentID, ConfigModel, Configuration, NonBlankText

MAX_CATALOG_BYTES = 1_048_576


class CategoryError(ValueError):
    """Safe category-catalog error, without private values."""


def new_object_id(prefix: str, *, random_bytes: bytes | None = None) -> str:
    """Compatibility entrypoint for the shared typed-ID generator."""
    try:
        return _new_object_id(prefix, random_bytes=random_bytes)
    except ValueError as error:
        raise CategoryError(str(error)) from None


def new_category_id(categories: CategoriesConfig, *, factory: Callable[[], str] | None = None) -> str:
    """Retry an accidental collision; catalog save checks uniqueness again."""
    issued = {item.permanent_id for item in categories.categories}
    generate = factory if factory is not None else lambda: new_object_id("CAT")
    for _ in range(16):
        candidate = generate()
        if not isinstance(candidate, str) or re.fullmatch(r"CAT-[A-Z2-7]{26}", candidate) is None:
            raise CategoryError("invalid_random_identity")
        if candidate not in issued:
            return candidate
    raise CategoryError("category_identity_collision")


def default_catalog_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".config" / "dam" / "categories.yaml"


class ManagedCategory(ConfigModel):
    permanent_id: CategoryPermanentID
    key: CategoryKey
    name: NonBlankText
    aliases: tuple[CategoryKey, ...] = ()
    parent_permanent_id: CategoryPermanentID | None = None
    status: Literal["active", "retired"] = "active"
    origin: Literal["base_override", "user"]


class CategoryCatalog(ConfigModel):
    schema_version: Literal[1] = 1
    records: tuple[ManagedCategory, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Only category catalog schema version 1 is supported")
        return value

    @model_validator(mode="after")
    def unique_records(self):
        ids = [item.permanent_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate permanent category identity")
        return self


class CategoryChange(ConfigModel):
    operation: Literal["add", "rename", "move", "retire"]
    before: ManagedCategory | None
    after: ManagedCategory
    config_fingerprint: str
    catalog_fingerprint: str
    fingerprint: str
    referenced_rules: tuple[str, ...] = ()
    saved: Literal[False] = False


def _hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _catalog_hash(catalog: CategoryCatalog) -> str:
    return _hash({"schema_version": catalog.schema_version,
                  "records": [item.model_dump(mode="json") for item in
                              sorted(catalog.records, key=lambda item: item.permanent_id)]})


def _validate_path(path: Path) -> None:
    if not path.is_absolute() or path.name != "categories.yaml" or path.parent.name != "dam" or path.parent.parent.name != ".config":
        raise CategoryError("invalid_category_catalog_path")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise CategoryError("unsafe_category_catalog_path")
    repository = Path(__file__).resolve().parents[2]
    if path.resolve(strict=False).is_relative_to(repository):
        raise CategoryError("category_catalog_must_be_outside_repository")


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise CategoryError("unsafe_category_catalog_permissions")


def _private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
            info.st_uid != os.getuid() or info.st_nlink != 1):
        raise CategoryError("unsafe_category_catalog_permissions")


def load_catalog(path: Path) -> CategoryCatalog:
    """Read bounded private YAML. Missing means no user changes; malformed fails."""
    path = Path(path)
    _validate_path(path)
    if path.parent.exists():
        _private_directory(path.parent)
    try:
        _private_file(path)
    except FileNotFoundError:
        return CategoryCatalog()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_uid != os.getuid() or info.st_nlink != 1):
                raise CategoryError("unsafe_category_catalog_permissions")
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise CategoryError("category_catalog_too_large")
        return CategoryCatalog.model_validate(yaml.load(raw.decode("utf-8"), Loader=UniqueKeySafeLoader))
    except CategoryError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, RecursionError):
        raise CategoryError("invalid_category_catalog") from None


def _base_records(base: CategoriesConfig) -> tuple[ManagedCategory, ...]:
    ids = {item.id: item.permanent_id for item in base.categories}
    if any(value is None for value in ids.values()):
        raise CategoryError("base_category_identity_missing")
    return tuple(ManagedCategory(permanent_id=item.permanent_id, key=item.key or item.id,
                                 name=item.name, aliases=item.aliases,
                                 parent_permanent_id=ids[item.parent_id] if item.parent_id else None,
                                 origin="base_override") for item in base.categories)


def merge_categories(base: CategoriesConfig, catalog: CategoryCatalog) -> CategoriesConfig:
    baseline = _base_records(base)
    base_by_id = {item.permanent_id: item for item in baseline}
    records = dict(base_by_id)
    for item in catalog.records:
        if item.origin == "base_override" and item.permanent_id not in base_by_id:
            raise CategoryError("unknown_base_category")
        if item.origin == "user" and item.permanent_id in base_by_id:
            raise CategoryError("base_category_identity_conflict")
        if item.origin == "base_override" and item.key != base_by_id[item.permanent_id].key:
            raise CategoryError("base_key_change_unsupported")
        records[item.permanent_id] = item
    keys = [key for item in records.values() for key in (item.key, *item.aliases)]
    if len(keys) != len(set(keys)):
        raise CategoryError("duplicate_category_key")
    for item in records.values():
        parent = records.get(item.parent_permanent_id)
        if item.parent_permanent_id and parent is None:
            raise CategoryError("unknown_category_parent")
        if item.status == "active" and parent and parent.status != "active":
            raise CategoryError("retired_category_parent")
    by_key = {item.permanent_id: item.key for item in records.values()}
    try:
        return CategoriesConfig(categories=tuple(Category(
            id=item.key, key=item.key, permanent_id=item.permanent_id, name=item.name,
            parent_id=by_key[item.parent_permanent_id] if item.parent_permanent_id else None,
            status=item.status, aliases=item.aliases) for item in records.values()))
    except ValidationError:
        raise CategoryError("invalid_category_tree") from None


def _resolve(selector: str, categories: CategoriesConfig) -> Category:
    matches = [item for item in categories.categories if selector in (item.id, item.permanent_id, *item.aliases)]
    if len(matches) != 1:
        raise CategoryError("unknown_or_ambiguous_category")
    return matches[0]


def resolve_category(selector: str, categories: CategoriesConfig, *, active: bool = True) -> Category:
    item = _resolve(selector, categories)
    if active and item.status != "active":
        raise CategoryError("retired_category")
    return item


def _managed(item: Category, origin: Literal["base_override", "user"], categories: CategoriesConfig) -> ManagedCategory:
    by_key = {entry.id: entry for entry in categories.categories}
    return ManagedCategory(permanent_id=item.permanent_id, key=item.key or item.id, name=item.name,
                           aliases=item.aliases,
                           parent_permanent_id=by_key[item.parent_id].permanent_id if item.parent_id else None,
                           status=item.status, origin=origin)


def propose_change(config: Configuration, catalog: CategoryCatalog, operation: str, *,
                   selector: str | None = None, key: str | None = None,
                   name: str | None = None, parent: str | None = None,
                   generated_id: str | None = None,
                   learned_rules_path: Path | None = None) -> CategoryChange:
    """Pure proposal. A new ID is supplied only by the caller's ID generator."""
    base = config.categories
    if operation == "retire" and learned_rules_path is None:
        raise CategoryError("learned_rule_inventory_required")
    effective = merge_categories(base, catalog)
    current = _resolve(selector, effective) if selector else None
    if operation == "add":
        if selector is not None or not key or not name or not generated_id:
            raise CategoryError("invalid_category_change")
        if any(item.id == key or item.permanent_id == generated_id for item in effective.categories):
            raise CategoryError("duplicate_category_identity_or_key")
        parent_id = resolve_category(parent, effective).permanent_id if parent else None
        after = ManagedCategory(permanent_id=generated_id, key=key, name=name,
                                parent_permanent_id=parent_id, origin="user")
    else:
        if current is None or operation not in ("rename", "move", "retire"):
            raise CategoryError("invalid_category_change")
        origin = "base_override" if any(item.id == current.id for item in base.categories) else "user"
        before = _managed(current, origin, effective)
        if operation == "rename":
            if not name:
                raise CategoryError("category_name_required")
            after = before.model_copy(update={"name": name})
        elif operation == "move":
            parent_id = resolve_category(parent, effective).permanent_id if parent else None
            after = before.model_copy(update={"parent_permanent_id": parent_id})
        else:
            after = before.model_copy(update={"status": "retired"})
    before = None if current is None else _managed(current, after.origin, effective)
    if before == after:
        raise CategoryError("category_unchanged")
    active_children = sorted(item.id for item in effective.categories
                             if operation == "retire" and item.parent_id == current.id and item.status == "active")
    if active_children:
        raise CategoryError("category_has_active_children:" + ",".join(active_children))
    records = tuple(item for item in catalog.records if item.permanent_id != after.permanent_id) + (after,)
    proposed = CategoryCatalog(records=records)
    merged = merge_categories(base, proposed)
    references = {rule.id for rule in config.rules.rules if current and current.id in rule.category_ids and rule.enabled}
    if operation == "retire" and learned_rules_path is not None and current is not None:
        from dam.learning import load_learned_rules
        for record in load_learned_rules(learned_rules_path).records:
            target = record.category_permanent_id or record.rule.category_ids[0]
            if target in (current.permanent_id, current.id) and record.rule.enabled:
                references.add(record.rule.id)
    references = tuple(sorted(references))
    if operation == "retire" and references:
        raise CategoryError("category_has_active_rule_references:" + ",".join(references))
    # Check no enabled rule target is retired in the resulting configuration.
    Configuration(settings=config.settings, categories=merged, rules=config.rules)
    base_hash = configuration_fingerprint(config)
    catalog_hash = _catalog_hash(catalog)
    semantic = {"operation": operation, "before": before.model_dump(mode="json") if before else None,
                "after": after.model_dump(mode="json"), "config": base_hash,
                "catalog": catalog_hash, "referenced_rules": references}
    return CategoryChange(operation=operation, before=before, after=after,
                          config_fingerprint=base_hash, catalog_fingerprint=catalog_hash,
                          fingerprint=_hash(semantic), referenced_rules=references)


def render_change(change: CategoryChange, *, saved: bool = False) -> str:
    def show(item: ManagedCategory | None) -> str:
        if item is None:
            return "<absent>"
        return (f"{item.permanent_id}  {item.key}  {item.name}  "
                f"parent={item.parent_permanent_id or '<root>'}  status={item.status}")
    return (f"Category {change.operation}: {'saved' if saved else 'preview only'}\n"
            f"Before: {show(change.before)}\nAfter: {show(change.after)}\n"
            f"Active rule references: {', '.join(change.referenced_rules) or 'none'}\n"
            f"Preview fingerprint: {change.fingerprint}\n"
            "Mailbox actions executed: 0\n")


@contextmanager
def _locked(path: Path):
    _validate_path(path)
    directory = path.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    _private_directory(directory)
    lock = directory / "categories.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise CategoryError("unsafe_category_catalog_permissions")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_change(path: Path, base_config: Configuration, change: CategoryChange, *,
                expected_fingerprint: str,
                learned_rules_path: Path | None = None) -> ManagedCategory:
    """CAS-style save under a local lock; no mail or learned-rule changes."""
    if expected_fingerprint != change.fingerprint or configuration_fingerprint(base_config) != change.config_fingerprint:
        raise CategoryError("stale_or_unconfirmed_category_change")
    if change.operation == "retire" and learned_rules_path is None:
        raise CategoryError("learned_rule_inventory_required")
    path = Path(path)
    with _locked(path):
        latest = load_catalog(path)
        if _catalog_hash(latest) != change.catalog_fingerprint:
            raise CategoryError("stale_category_catalog")
        fresh = propose_change(base_config, latest, change.operation,
            selector=change.before.permanent_id if change.before else None,
            key=change.after.key if change.operation == "add" else None,
            name=change.after.name if change.operation in ("add", "rename") else None,
            parent=change.after.parent_permanent_id if change.operation in ("add", "move") else None,
            generated_id=change.after.permanent_id if change.operation == "add" else None,
            learned_rules_path=learned_rules_path)
        if fresh.fingerprint != change.fingerprint:
            raise CategoryError("stale_or_unconfirmed_category_change")
        effective = merge_categories(base_config.categories, latest)
        if change.operation == "add" and any(item.permanent_id == change.after.permanent_id or item.id == change.after.key for item in effective.categories):
            raise CategoryError("duplicate_category_identity_or_key")
        updated = CategoryCatalog(records=tuple(item for item in latest.records
                                                if item.permanent_id != change.after.permanent_id) + (change.after,))
        merged = merge_categories(base_config.categories, updated)
        Configuration(settings=base_config.settings, categories=merged, rules=base_config.rules)
        document = updated.model_dump(mode="json")
        document["records"] = sorted(document["records"], key=lambda item: item["permanent_id"])
        raw = yaml.safe_dump(document, sort_keys=True, allow_unicode=True).encode("utf-8")
        if len(raw) > MAX_CATALOG_BYTES:
            raise CategoryError("category_catalog_too_large")
        temporary = path.parent / f".categories-{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise CategoryError("category_catalog_persistence_failure") from None
        finally:
            temporary.unlink(missing_ok=True)
    return change.after
