"""Human-directed, metadata-only classification learning.

Preview is pure. Saving writes only a private local YAML classification rule;
neither operation chooses a mailbox action or establishes execution authority.
Scans use learned rules only when explicitly supplied a learned-rules path.
"""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, ValidationError, field_validator
import yaml

from dam.classifier import classify
from dam.config import UniqueKeySafeLoader, configuration_fingerprint
from dam.models import (
    AcceptedClassificationRule, CategoryPermanentID, ConfigModel, Configuration, EvidenceOutcome, MatchSpec, MessageMetadata,
    NonBlankText, ProposedAction, Rule, RuleKind, RulesConfig, match_scope_fingerprint,
)
from dam.rules import _sender_address, evaluate_match

MAX_LEARNED_FILE_BYTES = 1_048_576
MAX_IMPACT_MESSAGES = 100
GMAIL_CATEGORY_LABELS = frozenset({
    "CATEGORY_PERSONAL", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
})


class LearningError(ValueError):
    """A safe learning failure category; never includes raw YAML or metadata."""


class RuleLearningEvidence(ConfigModel):
    field: Literal["sender_email", "sender_domain", "subject", "label_ids"]
    value: str | None = Field(default=None, repr=False)
    selected: bool
    reason: str


class ClassificationImpact(ConfigModel):
    message_id: NonBlankText
    matched_candidate: bool
    before_category_ids: tuple[str, ...]
    after_category_ids: tuple[str, ...]
    after_confidence: float
    after_requires_review: bool


class CandidateRule(ConfigModel):
    source_message_id: NonBlankText
    human_selected_category: NonBlankText
    rule: Rule
    evidence: tuple[RuleLearningEvidence, ...]
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["exact_sender"] = "exact_sender"
    equivalent_rule_id: str | None = None
    conflicts: tuple[str, ...] = ()
    impact: tuple[ClassificationImpact, ...]
    saved: Literal[False] = False
    authority_established: Literal[False] = False
    executable: Literal[False] = False
    executed_gmail_actions: Literal[0] = 0


class LearnedRuleRecord(ConfigModel):
    rule: Rule
    source_message_id: NonBlankText
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    saved_at: AwareDatetime
    source: Literal["human_explicit_save"] = "human_explicit_save"
    category_permanent_id: CategoryPermanentID | None = None


class LearnedRulesFile(ConfigModel):
    schema_version: Literal[1, 2] = 1
    records: tuple[LearnedRuleRecord, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("Only learned-rule schema versions 1 and 2 are supported")
        return value


class RuleLearningResult(ConfigModel):
    candidate: CandidateRule
    status: Literal["saved", "equivalent_exists"]
    path: str
    authority_established: Literal[False] = False
    executable: Literal[False] = False
    executed_gmail_actions: Literal[0] = 0


def default_learned_rules_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".config" / "dam" / "learned-rules.yaml"


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _equivalent(left: Rule, right: Rule) -> bool:
    a, b = left.model_dump(mode="json"), right.model_dump(mode="json")
    for item in (a, b):
        for name in ("id", "version", "notes"):
            item.pop(name)
    return a == b


def _validate_learned_rule(rule: Rule) -> None:
    match = rule.match
    if len(match.sender_emails_any) != 1 or len(rule.category_ids) != 1:
        raise LearningError("unsafe_learned_rule")
    expected_id = "learned_" + _digest({
        "category": rule.category_ids[0], "match": match.model_dump(mode="json")})[:20]
    if (rule.id != expected_id or rule.kind != RuleKind.CLASSIFICATION or
            rule.proposed_action != ProposedAction.NO_ACTION or rule.approval_ref is not None or
            rule.protect or rule.retention is not None or rule.priority_state is not None or
            rule.exclude is not None or rule.version != 1 or not rule.enabled or
            rule.priority != 0 or rule.notes or
            match != MatchSpec(sender_emails_any=match.sender_emails_any)):
        raise LearningError("unsafe_learned_rule")


def propose_classification_rule(
    source: MessageMetadata, category_id: str, config: Configuration, *,
    as_of: datetime, sample: tuple[MessageMetadata, ...] = (),
) -> CandidateRule:
    """Derive an exact-sender classification rule and preview its impact.

    The parsed sender is reused from the rule evaluator. Domain, subject and
    labels are reported as unselected context; no generalization is inferred.
    A sender header alone cannot authenticate the sender or prove message type.
    """
    known = {category.id for category in config.categories.categories if category.status == "active"}
    if category_id not in known:
        raise LearningError("unknown_category")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise LearningError("invalid_time")
    sender = _sender_address(source.sender)
    if sender is None:
        raise LearningError("sender_missing_or_ambiguous")
    if len(sample) > MAX_IMPACT_MESSAGES or any(type(item) is not MessageMetadata for item in sample):
        raise LearningError("invalid_impact_sample")
    messages = (source, *sample)
    identities = [(item.account_id, item.message_id) for item in messages]
    if len(identities) != len(set(identities)):
        raise LearningError("duplicate_message")
    if any(item.account_id != source.account_id for item in messages):
        raise LearningError("mixed_accounts")
    match = MatchSpec(sender_emails_any=(sender,))
    rule_id = "learned_" + _digest({"category": category_id, "match": match.model_dump(mode="json")})[:20]
    rule = Rule(id=rule_id, version=1, kind=RuleKind.CLASSIFICATION,
                match=match, category_ids=(category_id,),
                proposed_action=ProposedAction.NO_ACTION)
    config_hash = configuration_fingerprint(config)
    gmail_categories = tuple(sorted(GMAIL_CATEGORY_LABELS.intersection(source.label_ids)))
    evidence = (
        RuleLearningEvidence(field="sender_email", value=sender, selected=True,
                             reason="One unambiguous sender address; exact match avoids domain broadening."),
        RuleLearningEvidence(field="sender_domain", value=sender.rsplit("@", 1)[1], selected=False,
                             reason="Domain would include other senders and is not inferred."),
        RuleLearningEvidence(field="subject", selected=False,
                             reason="Whole subject and automatic keywords may be volatile or misleading."),
        RuleLearningEvidence(field="label_ids",
                             value=", ".join(gmail_categories) if gmail_categories else None,
                             selected=False,
                             reason="Gmail category labels are supporting context, not a human classification or selected match condition."),
    )
    equivalent = next((existing.id for existing in config.rules.rules
                       if existing.enabled and _equivalent(existing, rule)), None)
    collisions = [existing.id for existing in config.rules.rules
                  if existing.id == rule.id and not _equivalent(existing, rule)]
    combined = RulesConfig(rules=(*config.rules.rules, rule)) if not any(
        existing.id == rule.id for existing in config.rules.rules
    ) else None
    impacts = []
    conflicts = set(collisions)
    for message in sorted(messages, key=lambda item: item.message_id):
        before = classify(message, config.rules, as_of=as_of, settings=config.settings,
                          category_config=config.categories)
        matched = evaluate_match(message, match, as_of=as_of).outcome == EvidenceOutcome.MATCHED
        after = classify(message, combined, as_of=as_of, settings=config.settings,
                         category_config=config.categories) if combined else before
        impacts.append(ClassificationImpact(
            message_id=message.message_id, matched_candidate=matched,
            before_category_ids=before.category_ids, after_category_ids=after.category_ids,
            after_confidence=after.classification_confidence,
            after_requires_review=after.requires_review))
        if matched and before.category_ids and before.category_ids != (category_id,):
            conflicts.update(rule_id for rule_id, _ in before.selected_rules)
        if matched and after.category_ids != (category_id,):
            conflicts.add("projected_classification_withheld_or_different")
    fingerprint = _digest({"source_message_id": source.message_id,
                           "account_id": source.account_id,
                           "category": category_id, "rule": rule.model_dump(mode="json"),
                           "config_fingerprint": config_hash,
                           "evidence": [item.model_dump(mode="json") for item in evidence],
                           "impact": [item.model_dump(mode="json") for item in impacts],
                           "conflicts": sorted(conflicts),
                           "equivalent_rule_id": equivalent})
    return CandidateRule(source_message_id=source.message_id, human_selected_category=category_id,
                         rule=rule, evidence=evidence, config_fingerprint=config_hash,
                         fingerprint=fingerprint, equivalent_rule_id=equivalent,
                         conflicts=tuple(sorted(conflicts)), impact=tuple(impacts))


def _validate_private_path(path: Path) -> None:
    if not path.is_absolute() or path.name != "learned-rules.yaml" or path.parent.name != "dam" or path.parent.parent.name != ".config":
        raise LearningError("invalid_learned_rules_path")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise LearningError("unsafe_learned_rules_path")
    repository = Path(__file__).resolve().parents[2]
    if path.resolve(strict=False).is_relative_to(repository):
        raise LearningError("learned_rules_must_be_outside_repository")


def _private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
            info.st_uid != os.getuid() or info.st_nlink != 1):
        raise LearningError("unsafe_learned_rules_permissions")


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or
            info.st_uid != os.getuid()):
        raise LearningError("unsafe_learned_rules_permissions")


def load_learned_rules(path: Path) -> LearnedRulesFile:
    """Read only a private, bounded, strictly validated local YAML file."""
    path = Path(path)
    _validate_private_path(path)
    if path.parent.exists():
        _private_directory(path.parent)
    try:
        _private_file(path)
    except FileNotFoundError:
        return LearnedRulesFile()
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise LearningError("unsafe_learned_rules_permissions")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_uid != os.getuid() or info.st_nlink != 1):
                raise LearningError("unsafe_learned_rules_permissions")
            raw = stream.read(MAX_LEARNED_FILE_BYTES + 1)
        if len(raw) > MAX_LEARNED_FILE_BYTES:
            raise LearningError("learned_rules_too_large")
        data = yaml.load(raw.decode("utf-8"), Loader=UniqueKeySafeLoader)
        result = LearnedRulesFile.model_validate(data)
    except LearningError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, RecursionError):
        raise LearningError("invalid_learned_rules_file") from None
    identities = [(item.rule.id, item.rule.version) for item in result.records]
    if len(identities) != len(set(identities)):
        raise LearningError("duplicate_learned_rule")
    for item in result.records:
        _validate_learned_rule(item.rule)
    return result


def configuration_with_learned_rules(config: Configuration, path: Path) -> Configuration:
    """Opt-in, validated merge for a later scan; no action authority is added."""
    learned = load_learned_rules(path)
    effective = tuple(_effective_learned_rule(item, config) for item in learned.records)
    by_id = {item.id: item for item in config.categories.categories}
    accepted = tuple(AcceptedClassificationRule(
        rule_id=rule.id, rule_version=rule.version,
        category_permanent_id=by_id[rule.category_ids[0]].permanent_id,
        candidate_fingerprint=record.candidate_fingerprint,
        scope_fingerprint=match_scope_fingerprint(rule.match),
        saved_at=record.saved_at, record_schema_version=learned.schema_version,
    ) for record, rule in zip(learned.records, effective, strict=True))
    try:
        rules = RulesConfig(rules=(*config.rules.rules, *effective),
                            accepted_classifications=(*config.rules.accepted_classifications, *accepted))
        return Configuration(settings=config.settings, categories=config.categories, rules=rules)
    except ValidationError:
        raise LearningError("learned_rule_conflict") from None


def _effective_learned_rule(record: LearnedRuleRecord, config: Configuration) -> Rule:
    """Resolve a v2 permanent target or a valid legacy key without changing disk."""
    target = record.rule.category_ids[0]
    if record.category_permanent_id is not None:
        matches = [item for item in config.categories.categories
                   if item.permanent_id == record.category_permanent_id and
                   target in (item.id, item.key, *item.aliases)]
    else:
        matches = [item for item in config.categories.categories
                   if target in (item.id, item.key, *item.aliases)]
    if len(matches) != 1 or matches[0].status != "active":
        raise LearningError("unknown_learned_category")
    return record.rule.model_copy(update={"category_ids": (matches[0].id,)})


def save_classification_rule(candidate: CandidateRule, config: Configuration, path: Path, *,
                             expected_fingerprint: str, saved_at: datetime | None = None) -> RuleLearningResult:
    """Serialize the complete read/check/write cycle within the private directory."""
    path = Path(path)
    _validate_private_path(path)
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _private_directory(path.parent)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return _save_classification_rule_locked(candidate, config, path,
                expected_fingerprint=expected_fingerprint, saved_at=saved_at)
        finally:
            os.close(descriptor)
    except LearningError:
        raise
    except OSError:
        raise LearningError("learned_rules_persistence_failure") from None


def _save_classification_rule_locked(candidate: CandidateRule, config: Configuration, path: Path, *,
                                     expected_fingerprint: str, saved_at: datetime | None) -> RuleLearningResult:
    """Save only a fresh, explicitly fingerprint-confirmed candidate, atomically."""
    path = Path(path)
    _validate_private_path(path)
    if expected_fingerprint != candidate.fingerprint or configuration_fingerprint(config) != candidate.config_fingerprint:
        raise LearningError("stale_or_unconfirmed_candidate")
    _validate_learned_rule(candidate.rule)
    if candidate.human_selected_category != candidate.rule.category_ids[0]:
        raise LearningError("unsafe_learned_rule")
    if candidate.conflicts:
        raise LearningError("candidate_conflict_requires_review")
    existing = load_learned_rules(path)
    expected_existing = {rule.id: rule for rule in config.rules.rules if rule.id.startswith("learned_")}
    actual_existing = {item.rule.id: _effective_learned_rule(item, config) for item in existing.records}
    if expected_existing != actual_existing:
        raise LearningError("stale_or_unconfirmed_candidate")
    if candidate.equivalent_rule_id is not None:
        return RuleLearningResult(candidate=candidate, status="equivalent_exists", path=str(path))
    if any(_equivalent(item.rule, candidate.rule) for item in existing.records):
        return RuleLearningResult(candidate=candidate, status="equivalent_exists", path=str(path))
    if any(item.rule.id == candidate.rule.id for item in existing.records):
        raise LearningError("candidate_rule_id_conflict")
    timestamp = datetime.now(timezone.utc) if saved_at is None else saved_at
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise LearningError("invalid_time")
    record = LearnedRuleRecord(rule=candidate.rule, source_message_id=candidate.source_message_id,
                               candidate_fingerprint=candidate.fingerprint,
                               config_fingerprint=candidate.config_fingerprint,
                               saved_at=timestamp.astimezone(timezone.utc),
                               category_permanent_id=next((item.permanent_id for item in config.categories.categories
                                   if item.id == candidate.human_selected_category), None))
    records = tuple(sorted((*existing.records, record), key=lambda item: item.rule.id))
    document = {
        "schema_version": 2,
        "records": [{
            "rule": {
                "id": item.rule.id, "version": item.rule.version,
                "kind": "classification",
                "match": {"sender_emails_any": list(item.rule.match.sender_emails_any)},
                "category_ids": list(item.rule.category_ids),
                "proposed_action": "no_action",
            },
            "source_message_id": item.source_message_id,
            "candidate_fingerprint": item.candidate_fingerprint,
            "config_fingerprint": item.config_fingerprint,
            "saved_at": item.saved_at.isoformat(),
            "source": item.source,
            **({"category_permanent_id": item.category_permanent_id} if item.category_permanent_id else {}),
        } for item in records],
    }
    raw = yaml.safe_dump(document, sort_keys=True, allow_unicode=True).encode("utf-8")
    if len(raw) > MAX_LEARNED_FILE_BYTES:
        raise LearningError("learned_rules_too_large")
    directory = path.parent
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or
                info.st_uid != os.getuid()):
            raise LearningError("unsafe_learned_rules_permissions")
        temporary = directory / f".learned-rules-{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            folder_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(folder_fd)
            finally:
                os.close(folder_fd)
        finally:
            temporary.unlink(missing_ok=True)
    except LearningError:
        raise
    except OSError:
        raise LearningError("learned_rules_persistence_failure") from None
    return RuleLearningResult(candidate=candidate, status="saved", path=str(path))


def render_candidate(candidate: CandidateRule, *, status: str = "proposed") -> str:
    """Pure, concise human explanation; classification only."""
    lines = [
        f"Source message: {candidate.source_message_id}",
        f"Human selected category: {candidate.human_selected_category}",
        f"Candidate rule: {candidate.rule.id} (version {candidate.rule.version})",
        f"Exact sender match: {candidate.rule.match.sender_emails_any[0]}",
        "Scope: exact sender only; sender domain is broader and was not selected.",
        "Evidence grade: sender-only 0.90; sender identity and message meaning remain unverified.",
        f"Equivalent rule: {candidate.equivalent_rule_id or 'none'}",
        f"Conflicts: {', '.join(candidate.conflicts) if candidate.conflicts else 'none'}",
        f"Candidate fingerprint: {candidate.fingerprint}",
        f"Status: {status}",
        "Mailbox actions executed: 0; authority=false; executable=false.",
    ]
    for item in candidate.evidence:
        detail = f"; observed={item.value}" if item.value is not None else ""
        lines.append(f"Evidence {item.field}: {'selected' if item.selected else 'not selected'}{detail}; {item.reason}")
    for item in candidate.impact:
        lines.append(f"Impact {item.message_id}: match={str(item.matched_candidate).lower()}; "
                     f"before={','.join(item.before_category_ids) or 'unclassified'}; "
                     f"after={','.join(item.after_category_ids) or 'unclassified'}; "
                     f"confidence={item.after_confidence:.2f}; Review={str(item.after_requires_review).lower()}.")
    return "\n".join(lines) + "\n"
