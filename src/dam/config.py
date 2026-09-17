"""Read and validate local YAML; never create state or execute configured rules."""

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent

from dam.models import CategoriesConfig, Configuration, Rule, RulesConfig, Settings


class ConfigurationError(ValueError):
    """A configuration problem suitable for a concise CLI error."""


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML without duplicate keys, aliases, or implicit merge overrides."""

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise ConstructorError(None, None, "YAML aliases are unsupported", event.start_mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConstructorError(None, None, "Mapping keys must be strings", key_node.start_mark)
            if key in mapping:
                raise ConstructorError(None, None, "Duplicate mapping key", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _load_document(path: Path, model):
    try:
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ConfigurationError(f"{path.name}: exceeds the 1 MiB configuration limit")
        document = yaml.load(raw.decode("utf-8"), Loader=UniqueKeySafeLoader)
    except (OSError, UnicodeError):
        raise ConfigurationError(f"{path.name}: cannot read UTF-8 configuration") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        # Do not include raw YAML lines or values in errors.
        problem = "invalid or unsupported YAML"
        if isinstance(error, ConstructorError):
            if error.problem in ("Duplicate mapping key", "Mapping keys must be strings", "YAML aliases are unsupported"):
                problem = error.problem
        raise ConfigurationError(f"{path.name}: {problem}{location}") from None
    except RecursionError:
        raise ConfigurationError(f"{path.name}: YAML nesting is too deep") from None
    if not isinstance(document, dict):
        raise ConfigurationError(f"{path.name}: expected a mapping at the document root")
    if model is RulesConfig and "accepted_classifications" in document:
        raise ConfigurationError("rules.yaml: accepted_classifications requires the private learned-rule loader")
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise _validation_error(path.name, error) from None


def _validation_error(source: str, error: ValidationError) -> ConfigurationError:
    details = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'document'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False, include_context=False)
    )
    return ConfigurationError(f"{source}: {details}")


def load_config(directory: str | Path, *, repository_root: Path | None = None,
                category_catalog_path: Path | None = None) -> Configuration:
    """Load three documents without changing files, expanding credentials, or doing IO beyond reads.

    State paths are checked but not created. Pass repository_root when loading
    configuration outside a checkout so its state paths can also be checked.
    """
    directory = Path(directory).resolve()
    settings = _load_document(directory / "settings.yaml", Settings)
    categories = _load_document(directory / "categories.yaml", CategoriesConfig)
    rules = _load_document(directory / "rules.yaml", RulesConfig)
    try:
        config = Configuration(settings=settings, categories=categories, rules=rules)
    except ValidationError as error:
        raise _validation_error("configuration", error) from None
    if category_catalog_path is not None:
        # Delayed import keeps configuration parsing independent of catalog IO.
        from dam.categories import load_catalog, merge_categories
        merged = merge_categories(categories, load_catalog(category_catalog_path))
        try:
            config = Configuration(settings=settings, categories=merged, rules=rules)
        except ValidationError as error:
            raise _validation_error("configuration", error) from None
    roots = [ancestor for ancestor in (directory, *directory.parents) if (ancestor / ".git").exists()]
    if repository_root is not None:
        roots.append(repository_root.resolve())
    for field in ("database_path", "report_directory"):
        destination = Path(getattr(settings.state, field)).expanduser().resolve()
        if any(destination.is_relative_to(root) for root in roots) or any(
            (ancestor / ".git").exists() for ancestor in (destination, *destination.parents)
        ):
            raise ConfigurationError(f"settings.yaml: state.{field} must be outside Git repositories")
    return config


def _fingerprint(value: dict) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_rule(rule: Rule, *, semantic: bool) -> dict:
    value = rule.model_dump(mode="json")
    if semantic:
        value.pop("notes")
    value["category_ids"] = sorted(value["category_ids"])
    if value["retention"] is not None:
        value["retention"]["protected_types"] = sorted(value["retention"]["protected_types"])
    for name in ("match", "exclude"):
        if value[name] is not None:
            for key, condition in value[name].items():
                if isinstance(condition, list):
                    if key in ("subject_contains_any", "body_contains_any"):
                        condition = [item.casefold() for item in condition]
                    value[name][key] = sorted(condition)
    return value


def configuration_fingerprint(config: Configuration, *, semantic: bool = False) -> str:
    """Hash validated contents, independent of YAML formatting and ordering.

    The semantic hash additionally ignores rule notes. Versions, approval
    references, protections, scope, actions, and policy settings remain bound.
    This is reproducibility evidence, not approval or live preview authorization.
    """
    value = config.model_dump(mode="json")
    value["categories"]["categories"] = sorted(value["categories"]["categories"], key=lambda item: item["id"])
    value["rules"]["rules"] = [
        _canonical_rule(rule, semantic=semantic)
        for rule in sorted(config.rules.rules, key=lambda rule: (rule.id, rule.version))
    ]
    return _fingerprint(value)


def rule_scope_fingerprint(rule: Rule) -> str:
    """Scope hash excludes notes, identity/version, and external approval reference.

    Those references must be bound separately; this hash cannot grant authority.
    """
    value = _canonical_rule(rule, semantic=True)
    for field in ("id", "version", "approval_ref"):
        value.pop(field)
    return _fingerprint(value)
