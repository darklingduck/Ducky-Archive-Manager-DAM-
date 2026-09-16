"""Explicit synthetic, read-only Step 8 scan orchestration.

The demo uses an in-memory ScanRecord and does not open SQLite or retain scan
history. Configuration and package-owned fixture are read only on invocation.
Future source adapters and persistence can supply the same validated records.
"""

from datetime import datetime, timezone
from importlib.resources import files
import json
from pathlib import Path
import sys
from typing import Iterable
from uuid import uuid4

from pydantic import ValidationError

from dam.actions import propose_action
from dam.audit import ScanPreview, build_preview
from dam.classifier import classify
from dam.config import load_config, configuration_fingerprint, rule_scope_fingerprint
from dam.models import ConfigModel, MessageMetadata
from dam.storage import InventoryCounts, ObservationRecord, ScanFinish, ScanRecord, ScanStart

MAX_SCAN_LIMIT = 10_000


class ScanInputError(ValueError):
    """Invalid synthetic scan input; never echoes message contents."""


class ScanResult(ConfigModel):
    preview: ScanPreview
    source_message_count: int
    effective_limit: int
    source: str = "package_synthetic_fixture"
    persistence: str = "in_memory_only"


def default_config_directory() -> Path:
    """Use source config in a checkout, packaged config in an installed wheel."""
    checkout = Path(__file__).resolve().parents[2] / "config"
    if (checkout / "settings.yaml").is_file():
        return checkout
    return Path(sys.prefix) / "share" / "dam" / "config"


def load_synthetic_messages() -> tuple[MessageMetadata, ...]:
    """Load the package's bounded metadata-only fixture, without side effects."""
    try:
        raw = files("dam").joinpath("data/demo_messages.json").read_bytes()
        if len(raw) > 1_048_576:
            raise ScanInputError("Synthetic fixture exceeds the size limit")
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"description", "messages"} or not isinstance(document["messages"], list):
            raise ScanInputError("Invalid synthetic fixture structure")
        messages = tuple(MessageMetadata.model_validate(item) for item in document["messages"])
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError):
        raise ScanInputError("Cannot load validated synthetic message metadata") from None
    if len({(m.account_id, m.message_id) for m in messages}) != len(messages):
        raise ScanInputError("Synthetic fixture contains duplicate message IDs")
    if len({m.account_id for m in messages}) != 1:
        raise ScanInputError("Synthetic fixture must describe one account")
    return messages


def run_synthetic_scan(*, limit: int | None = None, config_directory: Path | None = None,
                       messages: Iterable[MessageMetadata] | None = None,
                       run_id: str | None = None, as_of: datetime | None = None) -> ScanResult:
    """Run existing rule, classifier, proposal and preview layers on exact messages.

    No persistence, Gmail, approval lookup or mailbox execution occurs. Inject
    run_id/as_of and messages for reproducible synthetic tests.
    """
    config = load_config(config_directory or default_config_directory())
    effective_limit = config.settings.scan.default_limit if limit is None else limit
    if type(effective_limit) is not int or not 1 <= effective_limit <= MAX_SCAN_LIMIT:
        raise ScanInputError(f"Limit must be an integer from 1 to {MAX_SCAN_LIMIT}")
    current = as_of if as_of is not None else datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ScanInputError("Scan time must include a timezone")
    current = current.astimezone(timezone.utc)
    identity = run_id if run_id is not None else f"synthetic-{uuid4().hex}"
    if not isinstance(identity, str) or not identity.strip():
        raise ScanInputError("Run ID must be nonblank")
    source = tuple(messages) if messages is not None else load_synthetic_messages()
    if not source or any(type(message) is not MessageMetadata for message in source):
        raise ScanInputError("Scan requires validated synthetic messages")
    if len({(m.account_id, m.message_id) for m in source}) != len(source):
        raise ScanInputError("Scan input contains duplicate message IDs")
    if len({m.account_id for m in source}) != 1:
        raise ScanInputError("Scan input must describe one account")
    # The M1 scan scope is INBOX; labels must be explicitly inspected.
    if any("label_ids" not in m.model_fields_set for m in source):
        raise ScanInputError("Scan input requires inspected labels")
    inbox = tuple(m for m in source if "INBOX" in m.label_ids)
    selected = inbox[:effective_limit]
    start = ScanStart(run_id=identity, account_id=source[0].account_id,
                      config_fingerprint=configuration_fingerprint(config),
                      started_at=current, as_of=current,
                      scope_label_ids=config.settings.scan.label_ids, limit=effective_limit)
    observations = []
    proposals = []
    for message in selected:
        classification = classify(message, config.rules, as_of=current, settings=config.settings)
        proposal = propose_action(message, classification, config.rules,
                                  as_of=current, settings=config.settings)
        observations.append(ObservationRecord(run_id=identity, observed_at=current,
                                              metadata=message, classification=classification))
        proposals.append(proposal)
    complete = len(selected) == len(inbox)
    finish = ScanFinish(ended_at=current, status="completed", inventory=InventoryCounts(
        label_total=len(inbox), pages_read=None, pagination_limited=False,
        completeness="complete" if complete else "partial",
        discrepancy="none" if complete else "not_checked"))
    record = ScanRecord(start=start, finish=finish, observed_unique_messages=len(selected))
    versions = tuple((rule.id, rule.version, rule_scope_fingerprint(rule)) for rule in config.rules.rules)
    preview = build_preview(record, observations, proposals, rule_versions=versions,
                            generated_at=current)
    return ScanResult(preview=preview, source_message_count=len(inbox),
                      effective_limit=effective_limit)
