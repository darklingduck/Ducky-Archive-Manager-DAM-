"""Interface-independent synthetic email Classification Queue service.

Real Gmail durable intake uses verified binding in the separate application
service. Neither service grants mailbox action authority.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from pydantic import ValidationError

from dam.classifier import ClassificationResult, classify
from dam.config import configuration_fingerprint
from dam.identifiers import new_object_id
from dam.items import (
    ClassificationWorkEvent, ClassificationWorkItem, DamItem, EmailItemObservation,
    MemberDecision, SourceInstance,
)
from dam.models import Configuration, MessageMetadata
from dam.storage import Storage, StorageError


class ClassificationQueueError(ValueError):
    """A safe workflow failure; never includes source metadata or content."""


@dataclass(frozen=True, repr=False)
class ClassificationIntake:
    item: DamItem
    classification: ClassificationResult
    work: ClassificationWorkItem | None


@dataclass(frozen=True, repr=False)
class ClassificationReevaluation:
    work: ClassificationWorkItem
    decisions: tuple[tuple[str, ClassificationResult], ...]


class ClassificationQueueService:
    """Coordinate pure email classification and durable source-neutral work."""

    def __init__(self, store: Storage):
        if type(store) is not Storage:
            raise ClassificationQueueError("A DAM Storage instance is required")
        self._store = store

    def register_synthetic_email_source(self, source_identity: str) -> SourceInstance:
        """Resolve one synthetic account to one durable source instance."""
        if not isinstance(source_identity, str) or not source_identity.strip() or source_identity == "gmail-account-unverified":
            raise ClassificationQueueError("A distinct synthetic source identity is required")
        existing = self._store.source_instance_by_identity("synthetic", source_identity)
        if existing is not None:
            return existing
        for _ in range(16):
            source = SourceInstance(source_instance_id=new_object_id("SRC"),
                                    source_identity=source_identity)
            try:
                return self._store.register_source_instance(source)
            except StorageError as error:
                if str(error) != "Source instance identity already has different provenance":
                    raise
        raise ClassificationQueueError("Cannot allocate a source instance identity")

    def register_email_item(self, source: SourceInstance, metadata: MessageMetadata) -> EmailItemObservation:
        if type(source) is not SourceInstance or type(metadata) is not MessageMetadata:
            raise ClassificationQueueError("Validated synthetic email inputs are required")
        try:
            source = SourceInstance.model_validate(source.model_dump(mode="python", exclude_unset=True))
            metadata = MessageMetadata.model_validate(metadata.model_dump(mode="python", exclude_unset=True))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise ClassificationQueueError("Invalid synthetic email observation") from None
        registered = self._store.source_instance(source.source_instance_id)
        if registered != source or source.provider != "synthetic" or metadata.account_id != source.source_identity:
            raise ClassificationQueueError("Email observation does not belong to the registered source")
        existing = self._store.item_by_native_identity(source.source_instance_id, metadata.message_id)
        if existing is not None:
            return EmailItemObservation(item=existing, metadata=metadata)
        for _ in range(16):
            item = DamItem(item_id=new_object_id("ITEM"),
                           source_instance_id=source.source_instance_id,
                           source_item_id=metadata.message_id)
            try:
                return EmailItemObservation(item=self._store.register_item(item), metadata=metadata)
            except StorageError as error:
                if str(error) != "DAM Item ID collision":
                    raise
        raise ClassificationQueueError("Cannot allocate a DAM Item identity")

    def _validated_observation(self, observation: EmailItemObservation) -> EmailItemObservation:
        if type(observation) is not EmailItemObservation:
            raise ClassificationQueueError("A validated email item observation is required")
        try:
            observation = EmailItemObservation.model_validate(
                observation.model_dump(mode="python", exclude_unset=True))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise ClassificationQueueError("Invalid email item observation") from None
        item = self._store.item(observation.item.item_id)
        source = self._store.source_instance(observation.item.source_instance_id)
        if (item != observation.item or source is None or source.provider != "synthetic" or
                source.source_identity != observation.metadata.account_id):
            raise ClassificationQueueError("Observation does not match registered DAM identity")
        return observation

    @staticmethod
    def _decision(item_id: str, classification: ClassificationResult, config: Configuration) -> MemberDecision:
        if classification.category_ids and not classification.category_permanent_ids:
            raise ClassificationQueueError("Classification has no permanent category identity")
        return MemberDecision(item_id=item_id,
                              category_permanent_ids=classification.category_permanent_ids or (),
                              teaching_required=classification.category_teaching_required,
                              config_fingerprint=configuration_fingerprint(config))

    def record_email_observation(self, observation: EmailItemObservation, config: Configuration,
                                 *, as_of: datetime) -> ClassificationIntake:
        """Create work only for unresolved teaching, never generic Review."""
        observation = self._validated_observation(observation)
        classification = classify(observation.metadata, config.rules, as_of=as_of,
                                  settings=config.settings, category_config=config.categories)
        if classification.category_teaching_required is not True or classification.category_ids:
            return ClassificationIntake(observation.item, classification, None)
        self._store.save_configuration(config)
        existing = self._store.active_classification_work_for_item(observation.item.item_id)
        if existing is not None:
            return ClassificationIntake(observation.item, classification, existing)
        decision = self._decision(observation.item.item_id, classification, config)
        for _ in range(16):
            try:
                work = self._store.create_classification_work(new_object_id("CWQ"), decision,
                                                              occurred_at=as_of)
                return ClassificationIntake(observation.item, classification, work)
            except StorageError as error:
                if str(error) == "Classification work ID collision":
                    continue
                if str(error) == "Item already belongs to unresolved classification work":
                    existing = self._store.active_classification_work_for_item(observation.item.item_id)
                    if existing is not None:
                        return ClassificationIntake(observation.item, classification, existing)
                raise
        raise ClassificationQueueError("Cannot allocate classification work identity")

    def add_related_email_item(self, work_id: str, observation: EmailItemObservation,
                               config: Configuration, *, as_of: datetime) -> ClassificationWorkItem:
        """Explicit association only; no sender, subject or provider grouping."""
        observation = self._validated_observation(observation)
        classification = classify(observation.metadata, config.rules, as_of=as_of,
                                  settings=config.settings, category_config=config.categories)
        if classification.category_teaching_required is not True or classification.category_ids:
            raise ClassificationQueueError("Related item does not need category teaching")
        self._store.save_configuration(config)
        return self._store.add_classification_member(
            work_id, self._decision(observation.item.item_id, classification, config), occurred_at=as_of)

    def list_work(self, *, state: str | None = None) -> tuple[ClassificationWorkItem, ...]:
        return self._store.classification_work_list(state=state)

    def inspect_work(self, work_id: str) -> ClassificationWorkItem | None:
        return self._store.classification_work(work_id)

    def inspect_item(self, item_id: str) -> DamItem | None:
        """Resolve a queue member without making callers parse Gmail IDs."""
        return self._store.item(item_id)

    def history(self, work_id: str) -> tuple[ClassificationWorkEvent, ...]:
        return self._store.classification_work_events(work_id)

    def defer_work(self, work_id: str, *, as_of: datetime) -> ClassificationWorkItem:
        return self._store.defer_classification_work(work_id, occurred_at=as_of)

    def reevaluate(self, work_id: str, observations: Iterable[EmailItemObservation],
                   config: Configuration, *, as_of: datetime) -> ClassificationReevaluation:
        """Evaluate every exact member; group membership supplies no evidence."""
        work = self.inspect_work(work_id)
        if work is None or work.state == "resolved":
            raise ClassificationQueueError("Unknown or resolved classification work")
        supplied = tuple(self._validated_observation(entry) for entry in observations)
        if len(supplied) != len(work.members) or {entry.item.item_id for entry in supplied} != {
            member.item_id for member in work.members}:
            raise ClassificationQueueError("Every exact member must have one inspected observation")
        if len({entry.item.item_id for entry in supplied}) != len(supplied):
            raise ClassificationQueueError("Duplicate member observation")
        decisions = []
        for entry in sorted(supplied, key=lambda item: item.item.item_id):
            result = classify(entry.metadata, config.rules, as_of=as_of,
                              settings=config.settings, category_config=config.categories)
            decisions.append((entry.item.item_id, result))
        self._store.save_configuration(config)
        updated = self._store.apply_classification_reevaluation(work_id, tuple(
            self._decision(item_id, result, config) for item_id, result in decisions),
            occurred_at=as_of)
        return ClassificationReevaluation(updated, tuple(decisions))
