"""Configuration tests use synthetic documents and temporary directories only."""

from copy import deepcopy
from pathlib import Path
import traceback

import pytest
import yaml
from pydantic import ValidationError

from dam.config import (
    ConfigurationError,
    configuration_fingerprint,
    load_config,
    rule_scope_fingerprint,
)
from dam.models import (
    CategoriesConfig,
    Configuration,
    ProposedAction,
)


@pytest.fixture
def documents(tmp_path):
    return {
        "settings": {
            "schema_version": 1,
            "state": {
                "database_path": f"/var/lib/dam-synthetic-{tmp_path.name}/dam.db",
                "report_directory": f"/var/lib/dam-synthetic-{tmp_path.name}/previews",
            },
        },
        "categories": {
            "schema_version": 1,
            "categories": [
                {"id": "synthetic_parent", "name": "Synthetic Parent"},
                {"id": "synthetic_child", "name": "Synthetic Child", "parent_id": "synthetic_parent"},
            ],
        },
        "rules": {
            "schema_version": 1,
            "rules": [{
                "id": "synthetic_rule",
                "version": 1,
                "priority": 100,
                "match": {
                    "sender_domains_any": ["example.invalid"],
                    "subject_contains_any": ["job alert", "newsletter"],
                },
                "exclude": {"subject_contains_any": ["receipt"]},
                "category_ids": ["synthetic_child"],
                "proposed_action": "archive",
                "notes": "Synthetic configuration only.",
            }],
        },
    }


def write_documents(directory, documents):
    directory.mkdir(exist_ok=True)
    for name, document in documents.items():
        (directory / f"{name}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return directory


def validated(documents):
    return Configuration.model_validate(documents)


def test_shipped_defaults_load_without_creating_state():
    config = load_config(Path(__file__).parents[1] / "config")
    assert config.settings.scan.default_limit == 100
    assert config.settings.scan.label_ids == ("INBOX",)
    assert config.settings.scan.include_spam_trash is False
    assert config.settings.inspection.snippet_when_needed is True
    assert config.settings.inspection.body_when_needed is False
    assert config.settings.state.database_path == "~/.local/share/dam/dam.db"
    assert config.settings.state.report_directory == "~/.local/share/dam/previews"
    assert config.settings.confidence.auto_threshold == 0.95
    assert config.settings.confidence.review_threshold == 0.75
    assert all(rule.approval_ref is None for rule in config.rules.rules)


def test_loader_does_not_create_state_paths(tmp_path, documents):
    state_directory = Path(documents["settings"]["state"]["database_path"]).parent
    assert not state_directory.exists()
    load_config(write_documents(tmp_path / "config", documents))
    assert not state_directory.exists()


@pytest.mark.parametrize("content, phrase", [
    ("scan: [\n", "invalid or unsupported YAML"),
    ("schema_version: 1\nschema_version: 1\n", "Duplicate mapping key"),
    ("scan:\n  default_limit: 10\n  default_limit: 100\n", "Duplicate mapping key"),
    ("!!python/object/apply:os.system ['echo synthetic']", "invalid or unsupported YAML"),
    ("state: &s {}\nscan: *s\n", "aliases are unsupported"),
    ("scan: {<<: {default_limit: 100}}", "invalid or unsupported YAML"),
    ("", "expected a mapping"),
    ("- synthetic\n", "expected a mapping"),
    ("1: synthetic\n", "Mapping keys must be strings"),
    ("{}\n---\n{}\n", "invalid or unsupported YAML"),
])
def test_bad_yaml_is_understandable_and_safe(tmp_path, documents, content, phrase):
    directory = write_documents(tmp_path / "config", documents)
    (directory / "settings.yaml").write_text(content)
    with pytest.raises(ConfigurationError, match=phrase) as error:
        load_config(directory)
    assert "settings.yaml" in str(error.value)
    assert "echo synthetic" not in str(error.value)


def test_missing_invalid_utf8_and_oversized_documents(tmp_path, documents):
    directory = write_documents(tmp_path / "config", documents)
    path = directory / "settings.yaml"
    for contents, phrase in [(b"\xff", "UTF-8"), (b" " * 1_048_577, "1 MiB")]:
        path.write_bytes(contents)
        with pytest.raises(ConfigurationError, match=phrase):
            load_config(directory)
    path.unlink()
    with pytest.raises(ConfigurationError, match="settings.yaml"):
        load_config(directory)


@pytest.mark.parametrize("section, field", [
    ("settings", "gmail_writes"), ("settings", "unsubscribe_execution"),
    ("settings", "oauth_scopes"), ("settings", "live"),
    ("categories", "unknown"), ("rules", "approvals"),
])
def test_unknown_root_fields_rejected(documents, section, field):
    documents[section][field] = True
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("section", ["scan", "state", "inspection", "reads", "confidence"])
def test_unknown_nested_settings_fields_rejected(documents, section):
    documents["settings"].setdefault(section, {})["live"] = True
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("field", ["approved", "approval", "execute", "unknown"])
def test_rule_fields_cannot_create_authority(documents, field):
    documents["rules"]["rules"][0][field] = True
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("section, field", [
    ("scan", "include_spam_trash"), ("inspection", "body_when_needed"),
])
@pytest.mark.parametrize("value", [True, 0, "false"])
def test_initial_inspection_and_scope_cannot_be_broadened(documents, section, field, value):
    documents["settings"].setdefault(section, {})[field] = value
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("labels", [[], ["INBOX", "INBOX"], ["SENT"], ["SPAM"], ["TRASH"]])
def test_inbox_only_scope(documents, labels):
    documents["settings"]["scan"] = {"label_ids": labels}
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("section, field", [
    ("scan", "default_limit"), ("inspection", "max_content_messages"),
    ("inspection", "max_text_characters"), ("reads", "max_attempts"),
    ("reads", "timeout_seconds"),
])
@pytest.mark.parametrize("value", [0, -1, True, "100"])
def test_positive_setting_numbers_are_strict(documents, section, field, value):
    documents["settings"].setdefault(section, {})[field] = value
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("section", ["settings", "categories", "rules"])
@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1"])
def test_schema_versions(documents, section, version):
    documents[section]["schema_version"] = version
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("thresholds", [
    {"auto_threshold": 0.5, "review_threshold": 0.75},
    {"auto_threshold": 0.75, "review_threshold": 0.75},
    {"auto_threshold": 1.1}, {"review_threshold": -0.1},
    {"auto_threshold": "0.95"}, {"auto_threshold": True},
])
def test_confidence_thresholds(documents, thresholds):
    documents["settings"]["confidence"] = thresholds
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("field", ["database_path", "report_directory"])
def test_paths_must_be_explicit_and_outside_git(tmp_path, documents, field):
    documents["settings"]["state"][field] = "relative/private"
    with pytest.raises(ValidationError):
        validated(documents)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    documents["settings"]["state"][field] = str(root / "private")
    with pytest.raises(ConfigurationError, match="outside Git"):
        load_config(write_documents(root / "config", documents))


def test_explicit_root_and_symlinks_are_checked(tmp_path, documents):
    root = tmp_path / "checkout"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    documents["settings"]["state"]["database_path"] = str(link / "dam.db")
    with pytest.raises(ConfigurationError, match="outside Git"):
        load_config(write_documents(tmp_path / "config", documents), repository_root=root)


@pytest.mark.parametrize("problem", ["duplicate", "missing_parent", "self_cycle", "long_cycle", "unknown_field"])
def test_category_tree_validation(documents, problem):
    categories = documents["categories"]["categories"]
    if problem == "duplicate":
        categories.append(deepcopy(categories[0]))
    elif problem == "missing_parent":
        categories[1]["parent_id"] = "missing"
    elif problem == "self_cycle":
        categories[0]["parent_id"] = categories[0]["id"]
    elif problem == "long_cycle":
        categories[0]["parent_id"] = categories[1]["id"]
    else:
        categories[0]["unknown"] = True
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("field, value", [
    ("version", 0), ("version", True), ("version", "1"),
    ("priority", -1), ("priority", True), ("priority", "100"),
    ("enabled", "true"), ("protect", 1), ("notes", 123),
    ("category_ids", ["missing"]), ("category_ids", ["synthetic_child", "synthetic_child"]),
    ("proposed_action", "permanent_delete"), ("proposed_action", "unsubscribe"),
    ("proposed_action", "delete"), ("kind", "unknown"),
])
def test_rule_validation(documents, field, value):
    documents["rules"]["rules"][0][field] = value
    with pytest.raises(ValidationError):
        validated(documents)


def test_rule_history_has_unique_versions_and_one_enabled_version(documents):
    rules = documents["rules"]["rules"]
    rules.append(deepcopy(rules[0]))
    with pytest.raises(ValidationError, match="combinations must be unique"):
        validated(documents)
    rules[1]["version"] = 2
    with pytest.raises(ValidationError, match="one version"):
        validated(documents)
    rules[0]["enabled"] = False
    assert len(validated(documents).rules.rules) == 2


@pytest.mark.parametrize("match", [
    {}, {"include_subdomains": True}, {"subject_contains_any": [""]},
    {"subject_contains_any": ["Alert", "alert"]}, {"subject_contains": "alert"},
    {"min_age_days": 10, "max_age_days": 1}, {"min_age_days": -1},
    {"sender_domains_any": ["https://example.invalid"]},
    {"sender_domains_any": ["example..invalid"]},
    {"sender_emails_any": ["Synthetic <sender@example.invalid>"]},
])
def test_explicit_match_schema(documents, match):
    documents["rules"]["rules"][0]["match"] = match
    with pytest.raises(ValidationError):
        validated(documents)


def test_zero_age_is_a_condition_and_fallback_can_be_unconditional(documents):
    documents["rules"]["rules"][0]["match"] = {"min_age_days": 0}
    assert validated(documents).rules.rules[0].match.has_conditions()
    documents["rules"]["rules"][0].update(kind="fallback", match={})
    assert not validated(documents).rules.rules[0].match.has_conditions()


def test_exclusion_remains_separate_and_cannot_be_empty(documents):
    rule = validated(documents).rules.rules[0]
    assert "receipt" not in rule.match.subject_contains_any
    assert rule.exclude.subject_contains_any == ("receipt",)
    documents["rules"]["rules"][0]["exclude"] = {}
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("field", ["match", "exclude", "retention"])
def test_unknown_fields_rejected_in_all_rule_sections(documents, field):
    rule = documents["rules"]["rules"][0]
    rule.setdefault(field, {})["execute"] = True
    with pytest.raises(ValidationError):
        validated(documents)


def test_sender_normalization_and_rule_order_are_deterministic(documents):
    second_rule = deepcopy(documents["rules"]["rules"][0])
    second_rule["id"] = "synthetic_second_rule"
    documents["rules"]["rules"].append(second_rule)
    before = configuration_fingerprint(validated(documents), semantic=True)
    documents["rules"]["rules"].reverse()
    documents["rules"]["rules"][0]["match"]["sender_domains_any"] = ["EXAMPLE.INVALID"]
    assert configuration_fingerprint(validated(documents), semantic=True) == before


def test_rule_scope_binds_protection_but_versions_are_bound_separately(documents):
    original = validated(documents).rules.rules[0]
    documents["rules"]["rules"][0]["version"] = 2
    new_version = validated(documents).rules.rules[0]
    assert rule_scope_fingerprint(original) == rule_scope_fingerprint(new_version)
    documents["rules"]["rules"][0]["protect"] = True
    assert rule_scope_fingerprint(validated(documents).rules.rules[0]) != rule_scope_fingerprint(original)


def test_null_retention_is_indefinite_and_not_authority(documents):
    rule = documents["rules"]["rules"][0]
    rule.update(kind="retention", protect=True, proposed_action="no_action", retention={"duration_days": None})
    parsed = validated(documents).rules.rules[0]
    assert parsed.retention.duration_days is None
    assert parsed.approval_ref is None
    rule["retention"]["duration_days"] = 0
    with pytest.raises(ValidationError):
        validated(documents)
    rule.pop("retention")
    with pytest.raises(ValidationError):
        validated(documents)


@pytest.mark.parametrize("protection", [{"protect": True}, {"priority_state": "Critical"}, {"priority_state": "Priority"}, {"priority_state": "Review"}])
def test_protected_rule_cannot_propose_trash(documents, protection):
    documents["rules"]["rules"][0].update(proposed_action="trash", **protection)
    with pytest.raises(ValidationError):
        validated(documents)


def test_trash_and_approval_reference_are_descriptions_only(documents):
    documents["rules"]["rules"][0].update(proposed_action="trash", approval_ref="synthetic-unverified-reference")
    rule = validated(documents).rules.rules[0]
    assert rule.proposed_action is ProposedAction.TRASH
    assert not hasattr(rule, "approved")


def test_fingerprints_ignore_format_order_and_notes_semantically(tmp_path, documents):
    first = validated(documents)
    original = configuration_fingerprint(first)
    semantic = configuration_fingerprint(first, semantic=True)
    documents["categories"]["categories"].reverse()
    documents["rules"]["rules"][0]["match"]["subject_contains_any"].reverse()
    directory = write_documents(tmp_path / "config", documents)
    (directory / "settings.yaml").write_text("# synthetic comment\n" + (directory / "settings.yaml").read_text())
    assert configuration_fingerprint(load_config(directory)) == original
    documents["rules"]["rules"][0]["notes"] = "Changed explanation only."
    changed = validated(documents)
    assert configuration_fingerprint(changed) != original
    assert configuration_fingerprint(changed, semantic=True) == semantic
    assert rule_scope_fingerprint(changed.rules.rules[0]) == rule_scope_fingerprint(first.rules.rules[0])


@pytest.mark.parametrize("change", ["action", "exclusion", "sender", "policy", "version", "approval", "retention", "priority"])
def test_material_changes_affect_semantic_fingerprint(documents, change):
    before = configuration_fingerprint(validated(documents), semantic=True)
    rule = documents["rules"]["rules"][0]
    if change == "action":
        rule["proposed_action"] = "trash"
    elif change == "exclusion":
        rule["exclude"] = None
    elif change == "sender":
        rule["match"]["sender_domains_any"].append("another.example.invalid")
    elif change == "policy":
        documents["settings"]["policy_version"] = 2
    elif change == "version":
        rule["version"] = 2
    elif change == "approval":
        rule["approval_ref"] = "synthetic-reference"
    elif change == "retention":
        rule["retention"] = {"duration_days": 30}
    else:
        rule["priority"] = 101
    assert configuration_fingerprint(validated(documents), semantic=True) != before


def test_validation_errors_locate_fields_without_echoing_values(tmp_path, documents):
    documents["settings"]["scan"] = {"default_limit": "synthetic-sensitive-value"}
    with pytest.raises(ConfigurationError) as error:
        load_config(write_documents(tmp_path / "config", documents))
    assert "scan.default_limit" in str(error.value)
    assert "synthetic-sensitive-value" not in str(error.value)
    assert "synthetic-sensitive-value" not in "".join(traceback.format_exception(error.value))


def test_models_are_frozen_and_json_round_trip(documents):
    config = validated(documents)
    assert Configuration.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValidationError):
        config.settings.policy_version = 2
