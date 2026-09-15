"""Validated metadata and evidence; no retrieval, classification, or actions."""

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    StrictBool,
    field_validator,
    model_validator,
)


NonBlankText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]
Confidence = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class EvidenceOutcome(StrEnum):
    """Missing evidence is distinct from evidence that does not match."""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    UNKNOWN = "unknown"


class MessageMetadata(BaseModel):
    """One individual message, scoped to an account, without content payloads.

    Gmail IDs are opaque, not email addresses or numbers. Thread and RFC IDs
    are reference metadata, not action targets or unique message identities.
    Missing sender/subject/date headers remain missing rather than invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    account_id: NonBlankText
    message_id: NonBlankText
    thread_id: NonBlankText | None = None
    rfc_message_id: NonBlankText | None = Field(default=None, repr=False)
    sender: NonBlankText | None = Field(default=None, repr=False)
    subject: Annotated[str, StringConstraints(strict=True)] | None = Field(
        default=None, repr=False
    )
    received_at: AwareDatetime
    header_date: AwareDatetime | None = None
    label_ids: tuple[NonBlankText, ...] = ()
    history_id: NonBlankText | None = None

    @field_validator("received_at", "header_date", mode="before")
    @classmethod
    def reject_numeric_dates(cls, value: object) -> object:
        # Conversion from Gmail's millisecond timestamp belongs in its adapter.
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Dates must be aware datetimes or ISO 8601 strings")
        if isinstance(value, str):
            # Pydantic also accepts numeric timestamp strings; those can confuse
            # seconds with milliseconds, so require explicit calendar dates.
            datetime.fromisoformat(value)
        return value

    @field_validator("received_at", "header_date")
    @classmethod
    def normalize_dates(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    @field_validator("label_ids")
    @classmethod
    def reject_duplicate_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Label IDs must be unique within a message")
        return value


class Evidence(BaseModel):
    """An explainable observation, not classification or action approval.

    Explanations must summarize evidence rather than copy private content.
    Confidence is an optional deterministic score, not a statistical guarantee.
    Rule/policy references and limitations preserve the basis of the observation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    field: NonBlankText
    match_type: NonBlankText
    outcome: EvidenceOutcome
    explanation: NonBlankText = Field(repr=False)
    rule_id: NonBlankText | None = None
    rule_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    policy_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    confidence: Confidence | None = None
    limitations: tuple[NonBlankText, ...] = Field(default=(), repr=False)


# Configuration describes future decisions; it does not evaluate or execute them.
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
ConfigID = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")
]


def _schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("Only integer schema_version 1 is supported")
    return value


class ConfigModel(BaseModel):
    """Unknown fields fail rather than silently changing a policy's meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class StateSettings(ConfigModel):
    database_path: NonBlankText = "~/.local/share/dam/dam.db"
    report_directory: NonBlankText = "~/.local/share/dam/previews"

    @field_validator("database_path", "report_directory")
    @classmethod
    def require_explicit_local_paths(cls, value: str) -> str:
        if not (Path(value).is_absolute() or value.startswith("~/")):
            raise ValueError("State paths must be absolute or start with ~/")
        return value


class ScanSettings(ConfigModel):
    default_limit: PositiveInt = 100
    label_ids: tuple[Literal["INBOX"], ...] = ("INBOX",)
    include_spam_trash: Literal[False] = False

    @field_validator("label_ids")
    @classmethod
    def require_inbox_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("INBOX",):
            raise ValueError("Milestone 1 supports one INBOX scan scope")
        return value

    @field_validator("include_spam_trash", mode="before")
    @classmethod
    def forbid_spam_trash(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Spam/Trash inclusion must be false in Milestone 1")
        return value


class InspectionSettings(ConfigModel):
    snippet_when_needed: StrictBool = True
    body_when_needed: Literal[False] = False
    max_content_messages: PositiveInt = 10
    max_text_characters: PositiveInt = 4096

    @field_validator("body_when_needed", mode="before")
    @classmethod
    def forbid_body_inspection(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Full-body inspection is disabled initially")
        return value


class ConfidenceSettings(ConfigModel):
    auto_threshold: Confidence = 0.95
    review_threshold: Confidence = 0.75
    destructive_threshold: Annotated[float, Field(strict=True, ge=0.95, le=1, allow_inf_nan=False)] = 0.95

    @model_validator(mode="after")
    def ordered_thresholds(self) -> "ConfidenceSettings":
        if self.review_threshold >= self.auto_threshold:
            raise ValueError("Review threshold must be below auto threshold")
        return self


class ReadSettings(ConfigModel):
    max_attempts: PositiveInt = 3
    timeout_seconds: PositiveInt = 30


class Settings(ConfigModel):
    schema_version: Literal[1] = 1
    policy_version: PositiveInt = 1
    state: StateSettings = Field(default_factory=StateSettings)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    inspection: InspectionSettings = Field(default_factory=InspectionSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    reads: ReadSettings = Field(default_factory=ReadSettings)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        return _schema_version(value)


class Category(ConfigModel):
    id: ConfigID
    name: NonBlankText
    parent_id: ConfigID | None = None


class CategoriesConfig(ConfigModel):
    schema_version: Literal[1] = 1
    categories: tuple[Category, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        return _schema_version(value)

    @model_validator(mode="after")
    def validate_tree(self) -> "CategoriesConfig":
        parents = {category.id: category.parent_id for category in self.categories}
        if len(parents) != len(self.categories):
            raise ValueError("Category IDs must be unique")
        for category_id, parent_id in parents.items():
            if parent_id is not None and parent_id not in parents:
                raise ValueError(f"Category {category_id} has an unknown parent")
        for category_id in parents:
            visited = set()
            current = category_id
            while current is not None:
                if current in visited:
                    raise ValueError(f"Category cycle includes {current}")
                visited.add(current)
                current = parents[current]
        return self


class ProposedAction(StrEnum):
    NO_ACTION = "no_action"
    CLASSIFY = "classify"
    LABEL = "label"
    ARCHIVE = "archive"
    MARK_PRIORITY = "mark_priority"
    MARK_REVIEW = "mark_review"
    MARK_UNSUBSCRIBE_CANDIDATE = "mark_unsubscribe_candidate"
    TRASH = "trash"


class RuleKind(StrEnum):
    SAFETY = "safety"
    RETENTION = "retention"
    SERVICE_SPECIFIC = "service_specific"
    CLASSIFICATION = "classification"
    FALLBACK = "fallback"


class PriorityState(StrEnum):
    CRITICAL = "Critical"
    PRIORITY = "Priority"
    REVIEW = "Review"
    ROUTINE = "Routine"
    ARCHIVED = "Archived"


class RelationshipStatus(StrEnum):
    CURRENT = "Current"
    HISTORICAL = "Historical"
    PROMOTIONAL = "Promotional"
    UNKNOWN = "Unknown"
    SUSPICIOUS = "Suspicious"


class MatchSpec(ConfigModel):
    """AND across populated fields; any/all is explicit within each field.

    Missing or uninspected evidence is unknown in the evaluator.
    Sender domains are exact unless include_subdomains is explicitly true.
    Keywords use case-insensitive substring matching, not regular expressions.
    Age bounds are inclusive days since receipt, measured in UTC.
    """

    sender_emails_any: tuple[NonBlankText, ...] = ()
    sender_domains_any: tuple[NonBlankText, ...] = ()
    include_subdomains: StrictBool = False
    subject_contains_any: tuple[NonBlankText, ...] = ()
    subject_contains_all: tuple[NonBlankText, ...] = ()
    body_contains_any: tuple[NonBlankText, ...] = ()
    label_ids_any: tuple[NonBlankText, ...] = ()
    label_ids_all: tuple[NonBlankText, ...] = ()
    min_age_days: NonNegativeInt | None = None
    max_age_days: NonNegativeInt | None = None
    relationship_status_any: tuple[RelationshipStatus, ...] = ()

    @field_validator("sender_emails_any", "sender_domains_any")
    @classmethod
    def normalize_sender_conditions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.lower() for item in value)

    @field_validator("sender_emails_any")
    @classmethod
    def require_bare_addresses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", item) for item in value):
            raise ValueError("Sender conditions require bare email addresses")
        return value

    @field_validator("sender_domains_any")
    @classmethod
    def require_domain_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        label_pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if any(
            len(item) > 253 or any(not re.fullmatch(label_pattern, label) for label in item.split("."))
            for item in value
        ):
            raise ValueError("Sender domains must be domain names, not URLs")
        return value

    @field_validator("subject_contains_any", "subject_contains_all", "body_contains_any")
    @classmethod
    def normalize_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.casefold() for item in value)

    @model_validator(mode="after")
    def validate_conditions(self) -> "MatchSpec":
        if self.include_subdomains and not self.sender_domains_any:
            raise ValueError("include_subdomains requires sender_domains_any")
        if self.min_age_days is not None and self.max_age_days is not None:
            if self.min_age_days > self.max_age_days:
                raise ValueError("Minimum age cannot exceed maximum age")
        for name, value in self.model_dump().items():
            if isinstance(value, tuple) and len(value) != len(set(value)):
                raise ValueError(f"Duplicate values in {name}")
        return self

    def has_conditions(self) -> bool:
        return any(
            value is not None and value != () and value is not False
            for name, value in self.model_dump().items()
            if name != "include_subdomains"
        )


class RetentionSpec(ConfigModel):
    """Null duration retains protected records indefinitely, not zero days."""

    measured_from: Literal["received_at"] = "received_at"
    duration_days: PositiveInt | None = None
    timezone: Literal["UTC"] = "UTC"
    protected_types: tuple[NonBlankText, ...] = ()


class Rule(ConfigModel):
    """A versioned description, never an approval record or executable action."""

    id: ConfigID
    version: PositiveInt
    enabled: StrictBool = True
    kind: RuleKind = RuleKind.CLASSIFICATION
    priority: NonNegativeInt = 0
    match: MatchSpec
    exclude: MatchSpec | None = None
    category_ids: tuple[ConfigID, ...] = ()
    proposed_action: ProposedAction = ProposedAction.NO_ACTION
    priority_state: PriorityState | None = None
    protect: StrictBool = False
    retention: RetentionSpec | None = None
    approval_ref: NonBlankText | None = None
    notes: Annotated[str, StringConstraints(strict=True)] = ""

    @model_validator(mode="after")
    def validate_description(self) -> "Rule":
        if self.kind != RuleKind.FALLBACK and not self.match.has_conditions():
            raise ValueError("Non-fallback rules require positive match conditions")
        if self.exclude is not None and not self.exclude.has_conditions():
            raise ValueError("Exclusions must contain explicit conditions")
        if len(self.category_ids) != len(set(self.category_ids)):
            raise ValueError("Rule category IDs must be unique")
        if self.kind == RuleKind.RETENTION and self.retention is None:
            raise ValueError("Retention rules require a retention description")
        if self.proposed_action == ProposedAction.TRASH and (
            self.protect or self.priority_state in (
                PriorityState.CRITICAL, PriorityState.PRIORITY, PriorityState.REVIEW
            )
        ):
            raise ValueError("A protected/actionable rule cannot propose Trash")
        return self


class RulesConfig(ConfigModel):
    schema_version: Literal[1] = 1
    rules: tuple[Rule, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        return _schema_version(value)

    @model_validator(mode="after")
    def unique_rule_versions(self) -> "RulesConfig":
        identities = [(rule.id, rule.version) for rule in self.rules]
        if len(identities) != len(set(identities)):
            raise ValueError("Rule ID/version combinations must be unique")
        enabled_ids = [rule.id for rule in self.rules if rule.enabled]
        if len(enabled_ids) != len(set(enabled_ids)):
            raise ValueError("Only one version of each rule may be enabled")
        return self


class Configuration(ConfigModel):
    settings: Settings
    categories: CategoriesConfig
    rules: RulesConfig

    @model_validator(mode="after")
    def valid_category_references(self) -> "Configuration":
        known_ids = {category.id for category in self.categories.categories}
        for rule in self.rules.rules:
            if not set(rule.category_ids).issubset(known_ids):
                raise ValueError(f"Rule {rule.id} references an unknown category")
        return self
