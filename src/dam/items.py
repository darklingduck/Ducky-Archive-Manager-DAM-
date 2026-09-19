"""Source-neutral identity and classification-work records.

Source-neutral ITEM/CWQ identity remains separate from email metadata.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from dam.models import CategoryPermanentID, ConfigModel, MessageMetadata


SourceInstanceID = Annotated[str, StringConstraints(strict=True, pattern=r"^SRC-[A-Z2-7]{26}$")]
ItemID = Annotated[str, StringConstraints(strict=True, pattern=r"^ITEM-[A-Z2-7]{26}$")]
ClassificationWorkID = Annotated[str, StringConstraints(strict=True, pattern=r"^CWQ-[A-Z2-7]{26}$")]
ClassificationEvaluationID = Annotated[str, StringConstraints(strict=True, pattern=r"^EVAL-[A-Z2-7]{26}$")]
TeachingOperationID = Annotated[str, StringConstraints(strict=True, pattern=r"^TEACH-[A-Z2-7]{26}$")]
SourceItemID = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512,
                                                pattern=r"^[^\x00-\x1f\x7f]+$")]
SourceIdentity = Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                                  min_length=1, max_length=256,
                                                  pattern=r"^[^\x00-\x1f\x7f]+$")]
Fingerprint = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
WorkState = Literal["pending", "deferred", "resolved"]


class SourceInstance(ConfigModel):
    source_instance_id: SourceInstanceID
    provider: Literal["synthetic", "gmail"] = "synthetic"
    identity_status: Literal["synthetic", "verified"] = "synthetic"
    source_identity: SourceIdentity = Field(repr=False)

    @model_validator(mode="after")
    def provider_status_pair(self):
        if (self.provider, self.identity_status) not in (("synthetic", "synthetic"), ("gmail", "verified")):
            raise ValueError("Source provider and verification status disagree")
        if self.provider == "gmail":
            from dam.gmail import GmailAdapterError, normalize_profile_address
            try:
                normalized = normalize_profile_address(self.source_identity)
            except GmailAdapterError:
                raise ValueError("Gmail source identity is malformed") from None
            if normalized != self.source_identity:
                raise ValueError("Gmail source identity must be normalized")
        return self

    @field_validator("source_identity")
    @classmethod
    def reject_unverified_gmail_placeholder(cls, value: str) -> str:
        if value == "gmail-account-unverified":
            raise ValueError("Unverified Gmail identity cannot be persisted")
        return value


class DamItem(ConfigModel):
    item_id: ItemID
    source_instance_id: SourceInstanceID
    item_kind: Literal["email"] = "email"
    source_item_id: SourceItemID = Field(repr=False)


class EmailItemObservation(ConfigModel):
    """A transient envelope; email metadata is never put in queue tables."""

    item: DamItem
    metadata: MessageMetadata = Field(repr=False)

    @model_validator(mode="after")
    def same_native_item(self):
        if self.item.item_kind != "email" or self.item.source_item_id != self.metadata.message_id:
            raise ValueError("Email observation does not match DAM Item identity")
        return self


class ClassificationWorkMember(ConfigModel):
    item_id: ItemID
    state: WorkState
    added_at: AwareDatetime


class ClassificationWorkItem(ConfigModel):
    work_id: ClassificationWorkID
    representative_item_id: ItemID
    state: WorkState
    created_at: AwareDatetime
    updated_at: AwareDatetime
    members: tuple[ClassificationWorkMember, ...]

    @model_validator(mode="after")
    def representative_is_member(self):
        if self.representative_item_id not in {member.item_id for member in self.members}:
            raise ValueError("Representative must be an exact member")
        if self.state == "resolved" and any(member.state != "resolved" for member in self.members):
            raise ValueError("Resolved work cannot contain unresolved members")
        return self


class ClassificationWorkEvent(ConfigModel):
    event_id: int = Field(strict=True, ge=1)
    work_id: ClassificationWorkID
    item_id: ItemID | None
    event_type: Literal["created", "member_added", "deferred", "member_deferred", "reevaluated", "resolved"]
    occurred_at: AwareDatetime
    prior_state: WorkState | None
    new_state: WorkState
    config_fingerprint: Fingerprint | None = None
    category_permanent_ids: tuple[CategoryPermanentID, ...] = ()
    teaching_required: bool | None = None


class MemberDecision(ConfigModel):
    item_id: ItemID
    category_permanent_ids: tuple[CategoryPermanentID, ...]
    teaching_required: bool | None
    config_fingerprint: Fingerprint
