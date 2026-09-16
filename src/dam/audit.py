"""Read-only Step 7 scan previews. A fingerprint is provenance, never approval.

This module cannot prove freshness against live Gmail state. It performs no
network or mailbox operations; the sole optional write is a proposed-state
event in an already-open Step 6 store. The full manifest is returned to the
caller and is not stored by the Step 6 schema.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable

from pydantic import AwareDatetime, Field, model_validator

from dam.actions import ActionProposal, RetentionConstraint
from dam.classifier import ClassificationResult
from dam.models import ConfigModel, Evidence, MessageMetadata, ProposedAction
from dam.stats import ScanStatistics, StatisticsInput, calculate_statistics
from dam.storage import AuditEvent, ObservationRecord, ScanRecord, Storage


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class PreviewEvidence(ConfigModel):
    field: str
    match_type: str
    outcome: str
    rule_id: str | None
    rule_version: int | None
    policy_version: int | None
    limitations: tuple[str, ...]


class RuleDecision(ConfigModel):
    rule_id: str
    rule_version: int
    status: str
    selected: bool


class MessagePreview(ConfigModel):
    account_id: str
    message_id: str
    thread_id: str | None
    received_at: AwareDatetime
    observed_at: AwareDatetime
    label_ids: tuple[str, ...]
    sender_present: bool
    subject_present: bool
    metadata_fingerprint: str
    category_ids: tuple[str, ...]
    classification_confidence: float
    classification_band: str
    requires_review: bool
    rule_decisions: tuple[RuleDecision, ...]
    evidence: tuple[PreviewEvidence, ...]
    protection_signals: tuple[str, ...]
    protection_rules: tuple[tuple[str, int], ...]
    retention_constraints: tuple[RetentionConstraint, ...]
    proposed_action: ProposedAction
    considered_actions: tuple[ProposedAction, ...]
    supporting_rules: tuple[tuple[str, int], ...]
    action_confidence: float
    action_band: str
    approval_required: bool
    approval_type: str
    approval_status: str
    authority_established: bool
    executable: bool
    review_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    outcome: str = "proposed"
    mailbox_modified: bool = False
    subscription_changed: bool = False

    @model_validator(mode="after")
    def preview_only(self):
        if self.authority_established or self.executable or self.mailbox_modified or self.subscription_changed or self.outcome != "proposed":
            raise ValueError("Step 7 entries are proposals without authority or effects")
        return self


class ScanPreview(ConfigModel):
    run_id: str
    account_id: str
    config_fingerprint: str
    rule_versions: tuple[tuple[str, int, str], ...]
    generated_at: AwareDatetime
    scan_started_at: AwareDatetime
    scan_as_of: AwareDatetime
    scan_ended_at: AwareDatetime
    scan_status: str
    scope_label_ids: tuple[str, ...]
    scan_limit: int
    exact_message_ids: tuple[str, ...]
    destructive_candidate_ids: tuple[str, ...]
    entries: tuple[MessagePreview, ...]
    statistics: ScanStatistics
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: str = "dry_run"
    freshness_status: str = "not_checked_against_live_source"
    authority_established: bool = False
    executable: bool = False

    @model_validator(mode="after")
    def check_scope(self):
        if self.authority_established or self.executable or self.mode != "dry_run":
            raise ValueError("Step 7 previews cannot grant execution authority")
        if self.exact_message_ids != tuple(entry.message_id for entry in self.entries):
            raise ValueError("Exact message IDs do not match preview entries")
        if self.destructive_candidate_ids != tuple(entry.message_id for entry in self.entries if entry.proposed_action == ProposedAction.TRASH):
            raise ValueError("Destructive IDs do not match individual Trash proposals")
        return self

    def to_json(self) -> str:
        return _json(self.model_dump(mode="json"))


def _evidence(item: Evidence) -> PreviewEvidence:
    # Evidence explanations are intentionally omitted: they may contain copied
    # private text in supplied records. Structured outcomes still explain matches.
    return PreviewEvidence(field=item.field, match_type=item.match_type,
                           outcome=item.outcome.value, rule_id=item.rule_id,
                           rule_version=item.rule_version, policy_version=item.policy_version,
                           limitations=tuple(sorted(set(item.limitations))))


def _entry(observation: ObservationRecord, proposal: ActionProposal) -> MessagePreview:
    message: MessageMetadata = observation.metadata
    classification: ClassificationResult = observation.classification
    if ((message.account_id, message.message_id) != (proposal.account_id, proposal.message_id) or
        (classification.account_id, classification.message_id) != (proposal.account_id, proposal.message_id) or
        classification.category_ids != proposal.category_ids or
        classification.classification_confidence != proposal.classification_confidence):
        raise ValueError("Preview observation and proposal disagree")
    if proposal.authority_established or proposal.executable:
        raise ValueError("Step 7 cannot preview an executable proposal")
    selected = set(classification.selected_rules)
    evidence = {_json(item.model_dump(mode="json")): item for item in map(
        _evidence, (*classification.evidence, *proposal.evidence))}
    metadata = message.model_dump(mode="json", exclude_unset=True)
    metadata["label_ids"] = sorted(message.label_ids)
    action_band = "high" if proposal.action_confidence >= .95 else "review" if proposal.action_confidence >= .75 else "insufficient"
    return MessagePreview(
        account_id=message.account_id, message_id=message.message_id, thread_id=message.thread_id,
        received_at=message.received_at, observed_at=observation.observed_at,
        label_ids=tuple(sorted(message.label_ids)), sender_present=message.sender is not None,
        subject_present=message.subject is not None, metadata_fingerprint=_hash(metadata),
        category_ids=tuple(sorted(classification.category_ids)),
        classification_confidence=classification.classification_confidence,
        classification_band=classification.confidence_band, requires_review=classification.requires_review or bool(proposal.review_reasons),
        rule_decisions=tuple(RuleDecision(rule_id=a.rule_id, rule_version=a.rule_version,
            status=a.status, selected=(a.rule_id, a.rule_version) in selected)
            for a in sorted(classification.assessments, key=lambda a: (a.rule_id, a.rule_version))),
        evidence=tuple(evidence[key] for key in sorted(evidence)),
        protection_signals=tuple(sorted(set(proposal.protection_signals))),
        protection_rules=tuple(sorted(proposal.protection_rules)),
        retention_constraints=tuple(sorted(proposal.retention_constraints, key=lambda c: _json(c.model_dump(mode="json")))),
        proposed_action=proposal.proposed_action,
        considered_actions=tuple(sorted(set(proposal.considered_actions))),
        supporting_rules=tuple(sorted(set(proposal.supporting_rules))),
        action_confidence=proposal.action_confidence, action_band=action_band,
        approval_required=proposal.approval_required, approval_type=proposal.approval_type,
        approval_status=proposal.approval_status, authority_established=False, executable=False,
        review_reasons=tuple(sorted(set(proposal.review_reasons))),
        reasons=tuple(sorted(set((*classification.reasons, *proposal.reasons)))),
        limitations=tuple(sorted(set((*classification.limitations, *proposal.limitations)))),
    )


def build_preview(scan: ScanRecord, observations: Iterable[ObservationRecord],
                  proposals: Iterable[ActionProposal], *, rule_versions: Iterable[tuple[str, int, str]],
                  generated_at: datetime) -> ScanPreview:
    """Build a deterministic completed-scan manifest from validated explicit data."""
    if scan.finish is None or scan.finish.status != "completed":
        raise ValueError("Preview requires a completed scan")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone aware")
    observations = tuple(observations)
    proposals = tuple(proposals)
    if len({o.metadata.message_id for o in observations}) != len(observations) or len({p.message_id for p in proposals}) != len(proposals):
        raise ValueError("Duplicate individual message IDs")
    by_id = {p.message_id: p for p in proposals}
    if set(by_id) != {o.metadata.message_id for o in observations} or len(observations) != scan.observed_unique_messages:
        raise ValueError("Preview must cover exactly the stored scan observations and proposals")
    for o in observations:
        if o.run_id != scan.start.run_id or o.metadata.account_id != scan.start.account_id:
            raise ValueError("Observation does not belong to scan")
        if o.classification.policy_version != by_id[o.metadata.message_id].policy_version:
            raise ValueError("Policy version mismatch")
    supplied_versions = tuple(rule_versions)
    versions = tuple(sorted(set(supplied_versions)))
    if len(versions) != len(supplied_versions):
        raise ValueError("Duplicate rule-version provenance")
    assessed = {(a.rule_id, a.rule_version) for o in observations for a in o.classification.assessments}
    if observations and assessed != {(r, v) for r, v, _ in versions}:
        raise ValueError("Rule-version provenance does not match assessments")
    entries = tuple(_entry(o, by_id[o.metadata.message_id]) for o in sorted(observations, key=lambda o: o.metadata.message_id))
    inputs = tuple(StatisticsInput(**{name: getattr(e, name) for name in StatisticsInput.model_fields}) for e in entries)
    stats = calculate_statistics(inputs, scan)
    base = dict(run_id=scan.start.run_id, account_id=scan.start.account_id,
                config_fingerprint=scan.start.config_fingerprint, rule_versions=versions,
                generated_at=generated_at.astimezone(timezone.utc),
                scan_started_at=scan.start.started_at, scan_as_of=scan.start.as_of,
                scan_ended_at=scan.finish.ended_at, scan_status=scan.finish.status,
                scope_label_ids=tuple(sorted(scan.start.scope_label_ids)), scan_limit=scan.start.limit,
                exact_message_ids=tuple(e.message_id for e in entries),
                destructive_candidate_ids=tuple(e.message_id for e in entries if e.proposed_action == ProposedAction.TRASH),
                entries=entries, statistics=stats)
    # Generated time is presentation provenance, not a decision input.
    semantic = ScanPreview(**base, fingerprint="0" * 64).model_dump(
        mode="json", exclude={"generated_at", "fingerprint"})
    return ScanPreview(**base, fingerprint=_hash(semantic))


def preview_from_storage(store: Storage, run_id: str, *, generated_at: datetime) -> ScanPreview:
    """Read through the Step 6 typed API; no SQL or storage write here."""
    scan = store.scan(run_id)
    if scan is None:
        raise ValueError("Unknown scan run")
    versions = tuple((row["rule_id"], row["version"], row["scope_fingerprint"])
                     for row in store.rule_versions(scan.start.config_fingerprint))
    return build_preview(scan, store.scan_observations(run_id), store.proposals(run_id),
                         rule_versions=versions, generated_at=generated_at)


def record_preview_event(store: Storage, preview: ScanPreview) -> None:
    """Append only a proposed-state marker; Step 6 cannot persist the manifest."""
    store.append_audit_event(AuditEvent(
        event_id=f"preview:{preview.run_id}:{preview.fingerprint}", run_id=preview.run_id,
        recorded_at=preview.generated_at, event_type="preview", state="proposed"))


def render_preview(preview: ScanPreview) -> str:
    """Pure, deterministic plain text for human review; no message content."""
    s = preview.statistics
    lines = [f"DAM dry-run preview {preview.run_id} ({preview.account_id})",
             f"Fingerprint: {preview.fingerprint}",
             "Freshness: not checked against live Gmail state; no execution authority",
             f"Scope: {','.join(preview.scope_label_ids)}; limit {preview.scan_limit}; coverage {s.inventory.completeness if s.inventory else 'unknown'}",
             f"Observed: {s.total_messages}; classified: {s.classified}; unclassified: {s.unclassified}; Review: {s.requiring_review}",
             f"Proposals: {', '.join(f'{x.key}={x.count}' for x in s.by_proposed_action) or 'none'}; Trash candidates: {s.trash_candidates}",
             f"Approvals required: {s.requiring_approval}; destructive: {s.requiring_destructive_approval}; executable: {s.executable}",
             "Executed Gmail actions: 0; actual Inbox after: not observed", ""]
    for entry in preview.entries:
        reason = entry.review_reasons[0] if entry.review_reasons else (entry.reasons[0] if entry.reasons else "No additional reason")
        lines.extend([f"Message {entry.message_id} (thread {entry.thread_id or 'unknown'})",
            f"  Observed: {entry.received_at.isoformat()}; labels={','.join(entry.label_ids) or 'none'}; sender={'present' if entry.sender_present else 'missing'}; subject={'present' if entry.subject_present else 'missing'}",
            f"  Classification: {','.join(entry.category_ids) or 'withheld'} ({entry.classification_confidence:.2f}, {entry.classification_band})",
            f"  Proposal: {entry.proposed_action.value} ({entry.action_confidence:.2f}); approval={entry.approval_type}/{entry.approval_status}; authority=false; executable=false",
            f"  Review: {'required' if entry.requires_review else 'no'}; protection={','.join(entry.protection_signals) or 'none'}",
            f"  Reason: {reason}", ""])
    return "\n".join(lines).rstrip() + "\n"
