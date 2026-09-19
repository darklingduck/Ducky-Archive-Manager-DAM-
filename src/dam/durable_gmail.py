"""Explicit, read-only Gmail durable intake through shared DAM services.

The authenticated Gmail adapter mints the mailbox profile. This service never
accepts a caller-supplied mailbox address or SourceInstance as proof of identity.
It holds no SQLite transaction while Gmail performs a network request.
"""

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dam.actions import propose_action
from dam.audit import ScanPreview, preview_from_storage
from dam.auth import AuthPaths, GoogleAuthBackend, authenticate, build_gmail_service, google_service_factory
from dam.classifier import classify
from dam.config import configuration_fingerprint, load_config
from dam.gmail import GmailReadResult, read_authenticated_profile, read_inbox
from dam.learning import configuration_with_learned_rules
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, ScanInputError, default_config_directory
from dam.source_binding import SourceBindingService
from dam.storage import InventoryCounts, ScanFinish, ScanStart, Storage, StorageError


@dataclass(frozen=True, repr=False)
class DurableGmailScanResult:
    run_id: str
    source_instance_id: str
    requested_limit: int
    status: str
    committed_item_ids: tuple[str, ...]
    read_result: GmailReadResult | None
    out_of_scope_ids: tuple[str, ...]
    preview: ScanPreview | None
    failure: str | None = None
    executed_gmail_actions: int = 0


def run_durable_gmail_scan(*, limit: int | None = None, paths: AuthPaths | None = None,
                           backend: Any = None, service_factory: Any = None,
                           config_directory: Path | None = None,
                           learned_rules_path: Path | None = None,
                           category_catalog_path: Path | None = None,
                           store: Storage | None = None, run_id: str | None = None,
                           as_of: datetime | None = None) -> DurableGmailScanResult:
    """Admit at most ten verified Inbox messages; persist each complete local decision.

    Read failures and Inbox list/get races do not become observed ITEMs. A local
    processing failure stops the batch and leaves earlier committed messages
    intact. No interactive teaching or mid-run configuration change occurs.
    """
    effective_limit = MAX_INITIAL_GMAIL_LIMIT if limit is None else limit
    if type(effective_limit) is not int or not 1 <= effective_limit <= MAX_INITIAL_GMAIL_LIMIT:
        raise ScanInputError(f"Gmail mode limit must be from 1 to {MAX_INITIAL_GMAIL_LIMIT}")
    current = datetime.now(timezone.utc) if as_of is None else as_of
    if current.tzinfo is None or current.utcoffset() is None:
        raise ScanInputError("Scan time must include a timezone")
    current = current.astimezone(timezone.utc)
    identity = run_id if run_id is not None else f"gmail-durable-{uuid4().hex}"
    if not isinstance(identity, str) or not identity.strip():
        raise ScanInputError("Run ID must be nonblank")
    config = load_config(config_directory or default_config_directory(),
                         category_catalog_path=category_catalog_path)
    if learned_rules_path is not None:
        config = configuration_with_learned_rules(config, learned_rules_path)
    if config.settings.scan.label_ids != ("INBOX",) or config.settings.scan.include_spam_trash:
        raise ScanInputError("Gmail mode requires Inbox-only scope")
    session = authenticate(paths if paths is not None else AuthPaths.for_home(),
                           backend if backend is not None else GoogleAuthBackend(),
                           allow_authorization=True)
    gmail_service = build_gmail_service(session, service_factory if service_factory is not None
                                        else google_service_factory)
    profile = read_authenticated_profile(gmail_service)
    if store is not None and type(store) is not Storage:
        raise StorageError("A DAM Storage instance is required")
    storage_context = nullcontext(store) if store is not None else Storage.open(config.settings)
    with storage_context as active_store:
        source = SourceBindingService(active_store).bind_gmail_profile(profile)
        active_store.save_configuration(config)
        active_store.start_scan(ScanStart(run_id=identity, account_id=source.source_instance_id,
            config_fingerprint=configuration_fingerprint(config),
            started_at=current, as_of=current, scope_label_ids=("INBOX",), limit=effective_limit))
        try:
            read = read_inbox(gmail_service, account_id=source.source_instance_id, limit=effective_limit)
        except Exception:
            active_store.finish_scan(identity, ScanFinish(ended_at=current, status="failed",
                inventory=InventoryCounts(completeness="partial")))
            return DurableGmailScanResult(identity, source.source_instance_id, effective_limit,
                "failed", (), None, (), None, "gmail_read_failed")
        committed: list[str] = []
        out_of_scope: list[str] = []
        failure: str | None = None
        for message in read.messages:
            if "label_ids" not in message.model_fields_set or "INBOX" not in message.label_ids:
                out_of_scope.append(message.message_id)
                continue
            try:
                classification = classify(message, config.rules, as_of=current,
                    settings=config.settings, category_config=config.categories)
                proposal = propose_action(message, classification, config.rules,
                    as_of=current, settings=config.settings)
                observed_at = current if as_of is not None else datetime.now(timezone.utc)
                item, _ = active_store.record_verified_gmail_intake(
                    identity, message, classification, proposal, observed_at=observed_at)
                committed.append(item.item_id)
            except Exception:
                failure = "local_intake_failed"
                break
        completed_at = current if as_of is not None else datetime.now(timezone.utc)
        estimate_discrepancy = (read.listing_complete and read.result_size_estimate is not None and
                                read.result_size_estimate != read.listed_count)
        complete = (failure is None and read.coverage == "complete" and not out_of_scope and
                    not estimate_discrepancy)
        active_store.finish_scan(identity, ScanFinish(ended_at=completed_at,
            status="completed" if failure is None else "failed",
            inventory=InventoryCounts(estimated_total=read.result_size_estimate,
                pages_read=read.pages_read, pagination_limited=read.stopped_at_limit,
                completeness="complete" if complete else "partial",
                discrepancy="unresolved" if estimate_discrepancy else
                            "none" if read.listing_complete and read.result_size_estimate is not None else
                            "not_checked")))
        preview = preview_from_storage(active_store, identity, generated_at=completed_at) if failure is None else None
        return DurableGmailScanResult(identity, source.source_instance_id, effective_limit,
            "completed" if failure is None else "failed", tuple(committed), read,
            tuple(out_of_scope), preview, failure)
