"""Human-facing decision labels and safe metadata text; no policy decisions."""

from collections.abc import Iterable
import re


_BASIS = {
    "configured_rule": "Configured rule",
    "human_accepted_learned_rule": "Human-taught rule",
}

_CLASSIFICATION_REVIEW = {
    "category_unresolved": "No category was established.",
    "evidence_below_high_threshold": "Evidence is below the current high-confidence threshold.",
    "classification_conflict": "Competing rules disagree on the category.",
    "unknown_eligibility": "Some rule eligibility is unknown.",
    "protection_conflict": "Protection rules conflict.",
    "explicit_review_signal": "A rule explicitly requires Review.",
}

_ACTION_REVIEW = {
    "classification_requires_review": "Classification still requires Review.",
    "unknown_rule_eligibility": "Some action-rule eligibility is unknown.",
    "action_conflict": "Competing rules disagree on the action.",
    "labels_uninspected": "Current labels were not inspected.",
    "cleanup_evidence_incomplete": "Cleanup lacks combined sender and subject evidence.",
    "protection_or_retention": "Protection or retention prevents cleanup.",
    "classification_below_action_threshold": "Classification evidence is below the action threshold.",
    "action_below_threshold": "Action evidence is below the action threshold.",
}


def classification_basis(codes: Iterable[str] | None) -> str:
    """Keep absent historical provenance distinct from a recorded empty set."""
    if codes is None:
        return "not recorded"
    labels = sorted({_BASIS.get(str(code), str(code)) for code in codes})
    return ", ".join(labels) or "unresolved"


def review_reasons(codes: Iterable[str] | None, *, action: bool = False) -> str:
    """Map known codes for display; retain unknown codes without interpretation."""
    if codes is None:
        return "not recorded"
    labels = _ACTION_REVIEW if action else _CLASSIFICATION_REVIEW
    return " ".join(labels.get(str(code), str(code)) for code in codes) or "none"


def safe_metadata_text(value: str | None, *, present: bool) -> str:
    """Preserve historical review display redaction and presence distinctions."""
    if not present:
        return "<absent>"
    if not value:
        return "<empty>"
    cleaned = re.sub(r"https?://\S+", "[URL redacted]", value, flags=re.IGNORECASE)
    cleaned = "".join(character if character.isprintable() else "�" for character in cleaned)
    return cleaned[:300] + ("…" if len(cleaned) > 300 else "")
