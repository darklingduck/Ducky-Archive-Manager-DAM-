"""Pure three-valued observations; no classification or action authority.

Call evaluate_match separately for Rule.match and Rule.exclude. An exclusion
result describes whether its conditions hold, not whether a rule may act.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import HeaderParser

from dam.models import Evidence, EvidenceOutcome, MatchSpec, MessageMetadata


@dataclass(frozen=True)
class MatchResult:
    """AND summary plus every populated condition, in schema order.

    Empty specifications match unconditionally (for future fallback use).
    Neither this summary nor its evidence determines an action.
    """

    outcome: EvidenceOutcome
    evidence: tuple[Evidence, ...]


def _sender_address(sender: str | None) -> str | None:
    """Require one defect-free mailbox; never guess from malformed headers."""
    if sender is None or "\r" in sender or "\n" in sender:
        return None
    try:
        header = HeaderParser(policy=policy.default).parsestr(f"From: {sender}\n\n")["From"]
        if header.defects or len(header.addresses) != 1:
            return None
        address = header.addresses[0]
        if not address.username or not address.domain:
            return None
        if any(group.display_name is not None for group in header.groups):
            return None
        return address.addr_spec.lower()
    except (ValueError, IndexError):
        return None


def evaluate_match(
    message: MessageMetadata,
    spec: MatchSpec,
    *,
    as_of: datetime,
    rule_id: str | None = None,
    rule_version: int | None = None,
    policy_version: int | None = None,
) -> MatchResult:
    """Evaluate a validated positive OR exclusion specification without IO.

    Time is supplied explicitly for reproducibility. Days are elapsed 24-hour
    periods in UTC, without rounding; bounds are inclusive. Future receipt
    times yield unknown age evidence. Body and relationship status are absent
    from MessageMetadata and always unknown. Omitted label_ids are unknown;
    explicitly supplied label_ids (including an empty tuple) are inspected.
    Preserve that distinction when serializing with exclude_unset=True.

    References are provenance only. Explanations omit raw message content and
    configured keyword values. Every populated field is evaluated even when
    an earlier field has already disproved the overall AND.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    as_of = as_of.astimezone(timezone.utc)
    age = as_of - message.received_at
    # Integer microseconds avoid rounding at inclusive boundaries and permit
    # validated age bounds larger than datetime/timedelta's representable range.
    age_us = (age.days * 86400 + age.seconds) * 1_000_000 + age.microseconds
    sender = _sender_address(message.sender)
    evidence: list[Evidence] = []
    for name in MatchSpec.model_fields:
        values = getattr(spec, name)
        if name == "include_subdomains" or values is None or values == ():
            continue
        matched: bool | None = None
        limitations: tuple[str, ...] = ()
        if name.startswith("sender_"):
            field = "sender"
            match_type = "exact_email_any" if name == "sender_emails_any" else (
                "domain_or_subdomain_any" if spec.include_subdomains else "exact_domain_any"
            )
            limitations = ("Sender header matching does not authenticate sender identity.",)
            if sender is None:
                explanation = "Sender is missing or is not one unambiguous, valid mailbox."
            else:
                domain = sender.rsplit("@", 1)[1]
                hits = [
                    sender == value if name == "sender_emails_any" else
                    domain == value or (spec.include_subdomains and domain.endswith("." + value))
                    for value in values
                ]
                matched = any(hits)
                explanation = f"{sum(hits)} of {len(values)} configured sender conditions matched."
        elif name.startswith("subject_contains_"):
            field = "subject"
            match_type = name.removeprefix("subject_")
            if message.subject is None:
                explanation = "Subject was not supplied; keyword presence is unknown."
                limitations = ("Missing subject is not an inspected empty subject.",)
            else:
                hits = [value in message.subject.casefold() for value in values]
                matched = all(hits) if name.endswith("_all") else any(hits)
                explanation = f"{sum(hits)} of {len(values)} case-insensitive substrings matched."
        elif name.startswith("label_ids_"):
            field = "label_ids"
            match_type = "exact_" + name.removeprefix("label_ids_")
            if "label_ids" not in message.model_fields_set:
                explanation = "Label IDs were not supplied; label membership is unknown."
                limitations = ("Default empty labels do not establish inspection.",)
            else:
                hits = [value in message.label_ids for value in values]
                matched = all(hits) if name.endswith("_all") else any(hits)
                explanation = f"{sum(hits)} of {len(values)} exact label IDs matched."
        elif name in ("min_age_days", "max_age_days"):
            field = "received_at"
            match_type = name
            limitations = ("Age uses receipt time, not the sender's Date header.",)
            if age_us < 0:
                explanation = "Receipt time is later than the supplied evaluation time."
                limitations += ("Future receipt time prevents reliable age evaluation.",)
            else:
                bound_us = values * 86400 * 1_000_000
                matched = age_us >= bound_us if name == "min_age_days" else age_us <= bound_us
                explanation = (
                    f"Elapsed UTC age {age_us} microseconds compared with inclusive "
                    f"{name}={values} at {as_of.isoformat()}."
                )
        else:
            # Fail closed for data unavailable in this metadata-only layer,
            # including any future schema fields without an evaluator.
            field = {"body_contains_any": "body", "relationship_status_any": "relationship_status"}.get(name, name)
            match_type = name
            explanation = "This condition has no inspected data in the metadata-only evaluator."
            limitations = ("No content or relationship status is retrieved or inferred.",)
        outcome = EvidenceOutcome.UNKNOWN if matched is None else (
            EvidenceOutcome.MATCHED if matched else EvidenceOutcome.NOT_MATCHED
        )
        evidence.append(Evidence(
            field=field, match_type=match_type, outcome=outcome,
            explanation=explanation, limitations=limitations,
            rule_id=rule_id, rule_version=rule_version, policy_version=policy_version,
        ))
    outcomes = {item.outcome for item in evidence}
    overall = (
        EvidenceOutcome.NOT_MATCHED if EvidenceOutcome.NOT_MATCHED in outcomes else
        EvidenceOutcome.UNKNOWN if EvidenceOutcome.UNKNOWN in outcomes else
        EvidenceOutcome.MATCHED
    )
    return MatchResult(overall, tuple(evidence))
