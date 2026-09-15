"""Pure metadata classification, without action selection or approval authority.

Precedence (highest first): safety/actionable signals, retention/protection,
service-specific rules with sender conditions, other constrained classifications,
label/age-only general rules, fallback. M1 cannot validate approvals, so neither
approval_ref nor proposed_action affects classification. Higher numeric priority
wins within a tier, then specificity: more populated AND fields, narrower sender
scope (email > exact domain > subdomains), then fewer OR alternatives. This is a
structural heuristic, not a claim of logical implication between arbitrary rules.

Confidence rubric v1: sender plus subject evidence .95, sender alone .90,
subject alone .80, labels alone .75, age-only .50, unconditional fallback .0.
These are conservative evidence grades, not probabilities or permission to act.
Unknown evidence never earns credit. Conflict/unresolved eligibility caps the
score at .50. Configured thresholds determine Review and confidence band.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from dam.models import (
    Evidence, EvidenceOutcome, MessageMetadata, PriorityState, Rule, RuleKind,
    RulesConfig, Settings,
)
from dam.rules import MatchResult, evaluate_match


SAFETY_STATES = (PriorityState.CRITICAL, PriorityState.PRIORITY, PriorityState.REVIEW)
STATE_ORDER = (*SAFETY_STATES, PriorityState.ROUTINE, PriorityState.ARCHIVED)


@dataclass(frozen=True)
class RuleAssessment:
    rule_id: str
    rule_version: int
    status: Literal["disabled", "rejected", "excluded", "unresolved", "applicable"]
    positive: MatchResult | None
    exclusion: MatchResult | None
    rank: tuple[int, int, int, int, int]
    category_ids: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class ClassificationResult:
    account_id: str
    message_id: str
    category_ids: tuple[str, ...]
    matched_rules: tuple[tuple[str, int], ...]
    selected_rules: tuple[tuple[str, int], ...]
    assessments: tuple[RuleAssessment, ...]
    protected: bool
    protection_rules: tuple[tuple[str, int], ...]
    priority_states: tuple[PriorityState, ...]
    priority_state: PriorityState | None
    classification_confidence: float
    confidence_band: Literal["high", "review", "insufficient"]
    requires_review: bool
    evidence: tuple[Evidence, ...]
    limitations: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: int
    classifier_version: int = 1


def _rank(rule: Rule) -> tuple[int, int, int, int, int]:
    spec = rule.match
    sender = 3 if spec.sender_emails_any else (
        1 if spec.include_subdomains else 2
    ) if spec.sender_domains_any else 0
    fields = [value for name, value in spec.model_dump().items()
              if name != "include_subdomains" and value is not None and value != ()]
    alternatives = sum(len(value) - 1 for name, value in spec.model_dump().items()
                       if name.endswith("_any") and value)
    if rule.kind == RuleKind.SAFETY or rule.priority_state in SAFETY_STATES:
        tier = 5
    elif rule.protect or rule.retention is not None or rule.kind == RuleKind.RETENTION:
        tier = 4
    elif rule.kind == RuleKind.FALLBACK:
        tier = 0
    elif rule.kind == RuleKind.SERVICE_SPECIFIC and sender:
        tier = 3
    elif sender or spec.subject_contains_any or spec.subject_contains_all or spec.body_contains_any:
        tier = 2
    else:
        tier = 1
    return tier, rule.priority, len(fields), sender, -alternatives


def _protects(rule: Rule) -> bool:
    return (rule.protect or rule.retention is not None or rule.kind == RuleKind.SAFETY
            or rule.priority_state in SAFETY_STATES)


def _score(assessment: RuleAssessment) -> float:
    assert assessment.positive is not None
    fields = {item.field for item in assessment.positive.evidence
              if item.outcome == EvidenceOutcome.MATCHED}
    if {"sender", "subject"} <= fields:
        return .95
    if "sender" in fields:
        return .90
    if "subject" in fields:
        return .80
    if "label_ids" in fields:
        return .75
    return .50 if fields else 0.0


def classify(
    message: MessageMetadata, rules: RulesConfig, *, as_of: datetime,
    settings: Settings | None = None,
) -> ClassificationResult:
    """Classify validated inputs at an explicit time without IO or mutation.

    Categories are selected independently of accumulated protection signals.
    Equally ranked, different category sets are withheld (candidates remain in
    assessments); protection is never removed by a lower-protection rule.
    Unknown eligibility always requires Review, even if another rule classifies.
    Protection-only rules do not erase a compatible ordinary category.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    settings = settings if settings is not None else Settings()
    assessments = []
    applicable: list[tuple[Rule, RuleAssessment]] = []
    for rule in sorted(rules.rules, key=lambda item: (item.id, item.version)):
        positive = exclusion = None
        status = "disabled"
        explanation = "Disabled rule was not evaluated."
        if rule.enabled:
            refs = dict(as_of=as_of, rule_id=rule.id, rule_version=rule.version,
                        policy_version=settings.policy_version)
            positive = evaluate_match(message, rule.match, **refs)
            exclusion = evaluate_match(message, rule.exclude, **refs) if rule.exclude else None
            if positive.outcome == EvidenceOutcome.NOT_MATCHED:
                status, explanation = "rejected", "Positive conditions were disproved."
            elif exclusion and exclusion.outcome == EvidenceOutcome.MATCHED:
                status, explanation = "excluded", "Exclusion conditions matched; rule cannot classify."
            elif positive.outcome == EvidenceOutcome.UNKNOWN or (
                exclusion and exclusion.outcome == EvidenceOutcome.UNKNOWN
            ):
                status, explanation = "unresolved", "Eligibility is unknown; rule cannot classify."
            else:
                status, explanation = "applicable", "Positive match established and no exclusion matched."
        assessment = RuleAssessment(rule.id, rule.version, status, positive, exclusion,
                                    _rank(rule), tuple(sorted(rule.category_ids)), explanation)
        assessments.append(assessment)
        if status == "applicable":
            applicable.append((rule, assessment))

    candidates = [a for _, a in applicable if a.category_ids]
    best = max((a.rank for a in candidates), default=None)
    selected = [a for a in candidates if a.rank == best]
    category_conflict = len({a.category_ids for a in selected}) > 1
    categories = selected[0].category_ids if selected and not category_conflict else ()
    # Check each comparable authority group, including protection-only rules.
    protection_groups: dict[tuple[int, ...], set[tuple[bool, PriorityState | None]]] = {}
    for rule, assessment in applicable:
        protection_groups.setdefault(assessment.rank, set()).add((_protects(rule), rule.priority_state))
    protection_conflict = any(len(group) > 1 for group in protection_groups.values())
    unresolved = any(a.status == "unresolved" for a in assessments)
    protections = tuple((r.id, r.version) for r, _ in applicable if _protects(r))
    states = {r.priority_state for r, _ in applicable if r.priority_state is not None}
    score = min((_score(a) for a in selected), default=0.0) if categories else 0.0
    if unresolved or protection_conflict:
        score = min(score, .50)
    band = ("high" if score >= settings.confidence.auto_threshold else
            "review" if score >= settings.confidence.review_threshold else "insufficient")
    review = (not categories or band != "high" or unresolved or category_conflict
              or protection_conflict or PriorityState.REVIEW in states)
    if review:
        states.add(PriorityState.REVIEW)
    ordered_states = tuple(state for state in STATE_ORDER if state in states)
    reasons = ["Only definitive positive matches with cleared exclusions participate."]
    if selected:
        reasons.append(f"Highest category rank {best}; tied identical category sets agree.")
    else:
        reasons.append("No eligible rule supplied a category.")
    if category_conflict:
        reasons.append("Equal-authority category conflict: candidates withheld; Review required.")
    if protection_conflict:
        reasons.append("Equal-authority protection conflict: all protective signals retained; Review required.")
    if unresolved:
        reasons.append("Unresolved rule eligibility requires Review; no category inferred from unknown evidence.")
    observations = tuple(item for a in assessments for result in (a.positive, a.exclusion)
                         if result is not None for item in result.evidence)
    limitations = tuple(sorted({limitation for item in observations for limitation in item.limitations} | {
        "Metadata evidence does not establish message meaning or authenticate sender identity.",
        "Classification confidence grants no action or approval authority.",
        "Specificity is structural; arbitrary condition sets are not proven logical subsets.",
    }))
    confidence_evidence = Evidence(
        field="classification", match_type="metadata_rubric_v1",
        outcome=EvidenceOutcome.MATCHED if categories else EvidenceOutcome.UNKNOWN,
        explanation=(f"Rubric v1 evidence grade {score:.2f}; weakest selected rule sets the grade; "
                     f"unresolved eligibility/protection conflict caps it at 0.50. Band: {band}."),
        confidence=score, policy_version=settings.policy_version, limitations=limitations,
    )
    return ClassificationResult(
        account_id=message.account_id, message_id=message.message_id, category_ids=categories,
        matched_rules=tuple((r.id, r.version) for r, _ in applicable),
        selected_rules=tuple((a.rule_id, a.rule_version) for a in selected),
        assessments=tuple(assessments), protected=bool(protections), protection_rules=protections,
        priority_states=ordered_states, priority_state=ordered_states[0] if ordered_states else None,
        classification_confidence=score, confidence_band=band, requires_review=review,
        evidence=(*observations, confidence_evidence), limitations=limitations,
        reasons=tuple(reasons), policy_version=settings.policy_version,
    )
