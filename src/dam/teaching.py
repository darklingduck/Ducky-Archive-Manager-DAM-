"""Offline classification teaching from durable DAM work; no Gmail or CLI access."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from dam.categories import resolve_category
from dam.classifier import ClassificationResult, ClassificationReviewReason, classify
from dam.config import load_config
from dam.identifiers import new_object_id
from dam.learning import (
    CandidateRule, configuration_with_learned_rules, default_learned_rules_path,
    load_learned_rules, propose_classification_rule, save_classification_rule,
)
from dam.models import Configuration, MessageMetadata
from dam.scan import default_config_directory
from dam.storage import ObservationRecord, Storage, StorageError


class TeachingError(ValueError):
    """Safe application failure without source text or credentials."""


@dataclass(frozen=True, repr=False)
class TeachingPreview:
    work_id: str
    item_id: str
    observation_run_id: str
    observed_at: datetime
    category_permanent_id: str
    category_name: str
    candidate: CandidateRule
    fingerprint: str
    metadata: MessageMetadata


@dataclass(frozen=True)
class TeachingOutcome:
    teaching_id: str
    status: str
    reevaluated: int
    resolved: int
    unresolved: int
    insufficient: int
    config_fingerprint: str | None
    authority_established: bool = False
    executable: bool = False
    executed_gmail_actions: int = 0


class TeachingService:
    """Share queue teaching with CLI or GUI; source evidence stays type-specific."""

    def __init__(self, store: Storage, *, config_directory: Path | None = None,
                 learned_rules_path: Path | None = None,
                 category_catalog_path: Path | None = None):
        if type(store) is not Storage:
            raise TeachingError("A DAM Storage instance is required")
        self.store = store
        self.config_directory = config_directory or default_config_directory()
        self.learned_rules_path = learned_rules_path or default_learned_rules_path()
        self.category_catalog_path = category_catalog_path

    def _config(self) -> Configuration:
        base = load_config(self.config_directory, category_catalog_path=self.category_catalog_path)
        return configuration_with_learned_rules(base, self.learned_rules_path)

    def categories(self):
        return tuple(category for category in self._config().categories.categories if category.status == "active")

    def list_work(self):
        return tuple(work for work in self.store.classification_work_list() if work.state != "resolved")

    def inspect(self, work_id: str, item_id: str | None = None) -> tuple[object, object, ObservationRecord]:
        work = self.store.classification_work(work_id)
        if work is None or work.state == "resolved":
            raise TeachingError("Unknown or resolved classification work")
        selected = item_id or work.representative_item_id
        if selected not in {member.item_id for member in work.members}:
            raise TeachingError("ITEM is not an exact member of this work")
        item = self.store.item(selected)
        observation = self.store.latest_email_observation(selected)
        if item is None or observation is None or observation.metadata.message_id != item.source_item_id or observation.metadata.account_id != item.source_instance_id:
            raise TeachingError("Suitable persisted email evidence is unavailable")
        return work, item, observation

    def preview(self, work_id: str, category_selector: str, *, item_id: str | None = None,
                as_of: datetime | None = None) -> TeachingPreview:
        work, item, observation = self.inspect(work_id, item_id)
        config = self._config()
        category = resolve_category(category_selector, config.categories)
        # A preview is reproducible from the pinned observation after restart.
        current = observation.observed_at
        candidate = propose_classification_rule(observation.metadata, category.id, config, as_of=current)
        material = {"work_id": work_id, "item_id": item.item_id,
            "observation_run_id": observation.run_id,
            "observed_at": observation.observed_at.isoformat(),
            "metadata": observation.metadata.model_dump(mode="json", exclude_unset=True),
            "category_permanent_id": category.permanent_id,
            "candidate_fingerprint": candidate.fingerprint}
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        return TeachingPreview(work_id, item.item_id, observation.run_id,
            observation.observed_at, category.permanent_id, category.name,
            candidate, fingerprint, observation.metadata)

    def confirm(self, work_id: str, category_selector: str, *, confirm_fingerprint: str,
                item_id: str | None = None, as_of: datetime | None = None) -> TeachingOutcome:
        previous = self.store.teaching_by_preview(confirm_fingerprint)
        if previous is not None and previous["work_id"] == work_id and previous["status"] == "completed":
            category = resolve_category(category_selector, self._config().categories)
            if (category.permanent_id != previous["category_permanent_id"] or
                    (item_id is not None and item_id != previous["item_id"])):
                raise TeachingError("Confirmation does not match the completed teaching")
            return self.resume(previous["teaching_id"], as_of=as_of)
        preview = self.preview(work_id, category_selector, item_id=item_id)
        if preview.fingerprint != confirm_fingerprint:
            raise TeachingError("Stale or unconfirmed teaching preview")
        config = self._config()
        self.store.save_configuration(config)
        existing = self.store.teaching_for_preview(work_id, preview.item_id, preview.fingerprint)
        teaching_id = existing["teaching_id"] if existing else new_object_id("TEACH")
        self.store.create_teaching_intent(teaching_id=teaching_id, work_id=work_id,
            item_id=preview.item_id, observation_run_id=preview.observation_run_id,
            observation_message_id=preview.metadata.message_id,
            category_permanent_id=preview.category_permanent_id,
            candidate_fingerprint=preview.candidate.fingerprint,
            preview_fingerprint=preview.fingerprint,
            config_before=preview.candidate.config_fingerprint,
            rule_id=preview.candidate.rule.id, rule_version=preview.candidate.rule.version,
            occurred_at=as_of or datetime.now(timezone.utc))
        return self.resume(teaching_id, as_of=as_of)

    def resume(self, teaching_id: str, *, as_of: datetime | None = None) -> TeachingOutcome:
        operation = self.store.teaching_operation(teaching_id)
        if operation is None:
            raise TeachingError("Unknown teaching operation")
        if operation["status"] == "completed":
            reevaluated, resolved, unresolved = self.store.teaching_evaluation_counts(teaching_id)
            return TeachingOutcome(teaching_id, "completed", reevaluated, resolved,
                unresolved, 0, operation["config_after"])
        now = as_of or datetime.now(timezone.utc)
        records = load_learned_rules(self.learned_rules_path).records
        # Rule IDs are derived from the validated exact-sender match and selected
        # category. Another confirmed work item may have saved the same rule.
        matching = [record for record in records if record.rule.id == operation["rule_id"]
            and record.rule.version == operation["rule_version"]
            and record.category_permanent_id == operation["category_permanent_id"]]
        if not matching:
            if operation["status"] != "intent":
                raise TeachingError("Persisted learned rule is unavailable")
            preview = self.preview(operation["work_id"], operation["category_permanent_id"],
                item_id=operation["item_id"])
            if preview.fingerprint != operation["preview_fingerprint"]:
                raise TeachingError("Teaching inputs changed; preview again")
            save_classification_rule(preview.candidate, self._config(), self.learned_rules_path,
                expected_fingerprint=preview.candidate.fingerprint, saved_at=now)
            records = load_learned_rules(self.learned_rules_path).records
            matching = [record for record in records if record.rule.id == operation["rule_id"]
                and record.rule.version == operation["rule_version"]
                and record.category_permanent_id == operation["category_permanent_id"]]
            if not matching:
                raise TeachingError("Learned-rule save could not be validated")
        if operation["status"] == "intent":
            operation = self.store.advance_teaching(teaching_id, "rule_saved", occurred_at=now)
        config = self._config()
        fingerprint = self.store.save_configuration(config)
        if operation["status"] == "rule_saved":
            operation = self.store.advance_teaching(teaching_id, "pending_reevaluation",
                occurred_at=now, config_after=fingerprint)
        elif operation["config_after"] != fingerprint:
            raise TeachingError("Effective configuration changed during teaching recovery")
        insufficient = 0
        cursor = ""
        while True:
            work_ids = self.store.active_work_ids_after(cursor, limit=32)
            if not work_ids:
                break
            for work_id in work_ids:
                cursor = work_id
                if self.store.work_evaluated_for_teaching(work_id, teaching_id):
                    continue
                work = self.store.classification_work(work_id)
                decisions = []
                for member in work.members:
                    try:
                        observation, result = self.evaluate_exact_item(member.item_id, config, as_of=now)
                    except TeachingError:
                        insufficient += 1
                        decisions = []
                        break
                    decisions.append((member.item_id, observation.run_id,
                                      observation.metadata.message_id, result))
                if not decisions:
                    continue
                self.store.record_teaching_work_reevaluation(work_id, tuple(decisions),
                    config_fingerprint=fingerprint, teaching_id=teaching_id, occurred_at=now)
        if insufficient == 0:
            operation = self.store.advance_teaching(teaching_id, "completed", occurred_at=now)
        total_evaluated, total_resolved, total_unresolved = self.store.teaching_evaluation_counts(teaching_id)
        return TeachingOutcome(teaching_id, operation["status"], total_evaluated, total_resolved,
            total_unresolved, insufficient, fingerprint)

    def evaluate_exact_item(self, item_id: str, config: Configuration, *,
                            as_of: datetime) -> tuple[ObservationRecord, ClassificationResult]:
        """Evaluate one ITEM from its own latest suitable persisted email evidence."""
        item = self.store.item(item_id)
        try:
            observation = self.store.latest_email_observation(item_id)
        except ValidationError:
            raise TeachingError("Persisted email evidence cannot be validated") from None
        if item is None or observation is None or item.source_item_id != observation.metadata.message_id:
            raise TeachingError("Suitable persisted evidence is unavailable")
        result = classify(observation.metadata, config.rules, as_of=as_of,
            settings=config.settings, category_config=config.categories)
        if result.review_reasons and ClassificationReviewReason.UNKNOWN_ELIGIBILITY in result.review_reasons:
            raise TeachingError("Insufficient persisted evidence for local reevaluation")
        return observation, result

    def reevaluate_exact_item(self, item_id: str, config: Configuration, *,
                              as_of: datetime) -> dict:
        """Reusable exact-ITEM reevaluation; append history without changing queue work."""
        observation, result = self.evaluate_exact_item(item_id, config, as_of=as_of)
        fingerprint = self.store.save_configuration(config)
        return self.store.append_item_evaluation(item_id, observation.run_id, result,
            config_fingerprint=fingerprint, evaluated_at=as_of)
