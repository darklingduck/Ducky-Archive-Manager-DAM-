"""Pure Step 5 recommendations. No proposal grants executable authority.

Consume Step 4 eligibility/ranks; never rematch or reinterpret precedence.
Callers must supply the same message, rules, settings and as_of used to classify.
The consistency checks below detect identity/version mismatches, but are not
preview validation: Step 4 does not bind content or configuration fingerprints.

Action rubric v1 grades the weakest winning action rule: definitive eligibility
starts at .95; missing sender OR subject evidence caps cleanup at .75, missing
inspected labels caps mutations at .75, conflicts/unknown eligibility cap at .50.
Protection/retention blocks cleanup regardless of score. Missing approval caps
mutation confidence at .95 (recommendation only), never establishes authority.
Classification confidence is a separate gate, not an input to this rubric.
Classify and mark_* are informational annotations here, not mailbox label writes.
"""

from datetime import datetime, timezone
from typing import Literal

from dam.classifier import ClassificationResult, SAFETY_STATES
from dam.models import (
    ConfigModel, Confidence, Evidence, EvidenceOutcome, MessageMetadata,
    ProposedAction, RulesConfig, Settings,
)

RuleReference = tuple[str, int]
CLEANUP = (ProposedAction.ARCHIVE, ProposedAction.TRASH)
MUTATIONS = (ProposedAction.LABEL, *CLEANUP)


class RetentionConstraint(ConfigModel):
    rule: RuleReference
    state: Literal["indefinite", "unexpired", "expired", "unknown"]
    duration_days: int | None
    protected_types: tuple[str, ...]
    reason: str


class ActionProposal(ConfigModel):
    account_id: str
    message_id: str
    proposed_action: ProposedAction
    considered_actions: tuple[ProposedAction, ...]
    supporting_rules: tuple[RuleReference, ...]
    category_ids: tuple[str, ...]
    classification_confidence: Confidence
    action_confidence: Confidence
    evidence: tuple[Evidence, ...]
    protection_rules: tuple[RuleReference, ...]
    protection_signals: tuple[str, ...]
    retention_constraints: tuple[RetentionConstraint, ...]
    approval_required: bool
    approval_type: Literal["none", "mailbox_mutation", "destructive"]
    approval_status: Literal["not_required", "missing", "reference_unverified"]
    approval_references: tuple[str, ...]
    authority_established: Literal[False] = False
    executable: Literal[False] = False
    reasons: tuple[str, ...]
    review_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    policy_version: int
    action_rubric_version: Literal[1] = 1


def propose_action(
    message: MessageMetadata, classification: ClassificationResult,
    rules: RulesConfig, *, as_of: datetime, settings: Settings | None = None,
) -> ActionProposal:
    """Recommend one action without IO, clocks, approval lookup, or mutation.

    Equal-ranked conflicting actions always yield informational Review. No
    applicable action yields no_action, retaining classification Review reasons.
    Expired retention stays protective because Step 4 protection is authoritative;
    protected_types cannot be resolved from metadata and are never waived here.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    settings = settings if settings is not None else Settings()
    if (message.account_id, message.message_id) != (
        classification.account_id, classification.message_id
    ) or settings.policy_version != classification.policy_version:
        raise ValueError("Classification identity or policy does not match inputs")
    by_ref = {(r.id, r.version): r for r in rules.rules}
    assessments = sorted(classification.assessments, key=lambda a: (a.rule_id, a.rule_version))
    if (len(assessments) != len(by_ref) or
            {(a.rule_id, a.rule_version) for a in assessments} != set(by_ref)):
        raise ValueError("Classification rule inventory does not match inputs")
    applicable = [a for a in assessments if a.status == "applicable"]
    if any(not by_ref[(a.rule_id, a.rule_version)].enabled for a in applicable):
        raise ValueError("Applicable classification rule is disabled")

    retention = []
    age = as_of.astimezone(timezone.utc) - message.received_at
    for a in assessments:
        r = by_ref[(a.rule_id, a.rule_version)]
        if r.retention is None or a.status not in ("applicable", "unresolved"):
            continue
        duration = r.retention.duration_days
        if a.status == "unresolved" or age.total_seconds() < 0:
            state = "unknown"
        elif duration is None:
            state = "indefinite"
        else:
            # Integer day comparison avoids timedelta overflow for large durations.
            state = "expired" if age.days >= duration else "unexpired"
        retention.append(RetentionConstraint(
            rule=(r.id, r.version), state=state, duration_days=duration,
            protected_types=tuple(sorted(r.retention.protected_types)),
            reason="Expiration never authorizes Trash; Step 4 protection remains in force.",
        ))

    best = max((a.rank for a in applicable), default=None)
    winners = [a for a in applicable if a.rank == best]
    refs = tuple((a.rule_id, a.rule_version) for a in winners)
    actions = tuple(sorted({by_ref[ref].proposed_action for ref in refs}))
    action = actions[0] if len(actions) == 1 else ProposedAction.NO_ACTION
    reasons = ["Step 4 definitive eligibility and precedence ranks select supporting rules."]
    review = []
    signals = set(state.value for state in classification.priority_states if state in SAFETY_STATES)
    if classification.protected:
        signals.add("protected")
    if classification.requires_review:
        signals.add("classification_requires_review")
        review.append("Classification requires Review; cleanup is withheld.")
    unknown = any(a.status == "unresolved" for a in assessments)
    if unknown:
        review.append("Unresolved eligibility prevents automatic recommendations.")
    conflict = len(actions) > 1
    score = .95 if winners else 1.0
    if conflict:
        action = ProposedAction.MARK_REVIEW
        review.append("Equal-ranked action conflict; preserve and request Review.")
    if any(candidate in MUTATIONS for candidate in actions) and "label_ids" not in message.model_fields_set:
        score = min(score, .75)
        review.append("Current labels were not inspected; mutation evidence is incomplete.")
    for a in winners:
        if by_ref[(a.rule_id, a.rule_version)].proposed_action in CLEANUP:
            observed = {e.field for e in a.positive.evidence
                        if e.outcome == EvidenceOutcome.MATCHED} if a.positive else set()
            if not {"sender", "subject"} <= observed:
                score = min(score, .75)
                review.append("Cleanup rule lacks combined sender and subject evidence.")
    if conflict or unknown:
        score = min(score, .50)
    if action in CLEANUP:
        threshold = (settings.confidence.destructive_threshold if action == ProposedAction.TRASH
                     else settings.confidence.auto_threshold)
        blocked = bool(signals or any(r.state != "expired" for r in retention))
        if blocked:
            score = min(score, .50)
            review.append("Protection or retention requires preservation; no M1 override authority exists.")
        if classification.classification_confidence < threshold:
            review.append("Classification confidence is below the required action threshold.")
        if score < threshold:
            review.append("Action confidence is below the required action threshold.")
        if blocked or unknown or classification.classification_confidence < threshold or score < threshold:
            action = ProposedAction.MARK_REVIEW
    elif unknown and action != ProposedAction.NO_ACTION:
        action = ProposedAction.MARK_REVIEW
    if not winners:
        reasons.append("No applicable action rule; preserve without action.")
    if retention:
        reasons.append("Retention constraints are retained independently of action rank; expiration grants no cleanup authority.")
    if signals and action == ProposedAction.NO_ACTION:
        reasons.append("Preservation signals remain active; no mailbox change is recommended.")
    approval_type = ("destructive" if action == ProposedAction.TRASH else
                     "mailbox_mutation" if action in MUTATIONS else "none")
    approval_refs = tuple(sorted({by_ref[ref].approval_ref for ref in refs
                                 if by_ref[ref].approval_ref is not None}))
    required = approval_type != "none"
    approval_status = ("reference_unverified" if approval_refs else
                       "missing" if required else "not_required")
    if required:
        reasons.append(f"{approval_type} approval is required; authority is not established.")
    else:
        reasons.append("This result is informational; no mailbox operation is requested.")
    limitations = tuple(sorted(set(classification.limitations) | {
        "M1 is proposal-only; no proposal is executable authority.",
        "Approval references are identifiers only; approval validation is not implemented.",
        "Caller must supply the same classification inputs; no immutable preview binding exists yet.",
        "Metadata cannot verify message meaning, protected content, or sender authenticity.",
        "Confidence grades recommendation evidence, not permission or probability.",
    }))
    evidence = Evidence(
        field="action", match_type="action_rubric_v1", outcome=EvidenceOutcome.MATCHED,
        explanation=f"Action evidence grade {score:.2f}; eligibility, labels, conflicts and preservation gates evaluated.",
        confidence=score, policy_version=settings.policy_version,
        limitations=tuple(sorted(set(review))),
    )
    return ActionProposal(
        account_id=message.account_id, message_id=message.message_id,
        proposed_action=action, considered_actions=actions, supporting_rules=refs,
        category_ids=classification.category_ids,
        classification_confidence=classification.classification_confidence,
        action_confidence=score, evidence=(evidence, *(item for a in winners
            for result in (a.positive, a.exclusion) if result is not None
            for item in result.evidence)),
        protection_rules=tuple(sorted(classification.protection_rules)),
        protection_signals=tuple(sorted(signals)), retention_constraints=tuple(retention),
        approval_required=required, approval_type=approval_type, approval_status=approval_status,
        approval_references=approval_refs, reasons=tuple(reasons),
        review_reasons=tuple(sorted(set(review))), limitations=limitations,
        policy_version=settings.policy_version,
    )
