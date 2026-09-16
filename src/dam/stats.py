"""Pure, immutable counts of read-only scan proposals, never mailbox effects."""

from collections import Counter
from datetime import datetime
from typing import Iterable

from pydantic import AwareDatetime, Field

from dam.models import ConfigModel, ProposedAction
from dam.storage import InventoryCounts, ScanRecord


class StatisticItem(ConfigModel):
    key: str
    count: int = Field(ge=0)


class StatisticsInput(ConfigModel):
    message_id: str
    received_at: AwareDatetime
    category_ids: tuple[str, ...]
    proposed_action: ProposedAction
    requires_review: bool
    protection_signals: tuple[str, ...]
    approval_required: bool
    approval_type: str
    authority_established: bool
    executable: bool
    classification_band: str
    action_band: str


class ScanStatistics(ConfigModel):
    total_messages: int
    classified: int
    unclassified: int
    requiring_review: int
    with_protection_signals: int
    by_category: tuple[StatisticItem, ...]
    by_proposed_action: tuple[StatisticItem, ...]
    trash_candidates: int
    requiring_approval: int
    requiring_destructive_approval: int
    authority_established: int
    executable: int
    by_classification_band: tuple[StatisticItem, ...]
    by_action_band: tuple[StatisticItem, ...]
    oldest_received_at: AwareDatetime | None
    newest_received_at: AwareDatetime | None
    scan_limit: int
    scope_label_ids: tuple[str, ...]
    observed_unique_messages: int
    inventory: InventoryCounts | None
    executed_gmail_actions: int = Field(default=0, frozen=True)
    executed_archives: int = Field(default=0, frozen=True)
    executed_trash: int = Field(default=0, frozen=True)
    actual_inbox_after: None = None


def _items(counter: Counter[str]) -> tuple[StatisticItem, ...]:
    return tuple(StatisticItem(key=key, count=counter[key]) for key in sorted(counter))


def calculate_statistics(entries: Iterable[StatisticsInput], scan: ScanRecord) -> ScanStatistics:
    """Count supplied exact messages; a completed scan may still have partial coverage."""
    entries = tuple(entries)
    if len({entry.message_id for entry in entries}) != len(entries):
        raise ValueError("Duplicate message IDs in statistics input")
    if len(entries) != scan.observed_unique_messages:
        raise ValueError("Statistics input does not cover stored observations")
    dates: tuple[datetime, ...] = tuple(entry.received_at for entry in entries)
    return ScanStatistics(
        total_messages=len(entries),
        classified=sum(bool(entry.category_ids) for entry in entries),
        unclassified=sum(not entry.category_ids for entry in entries),
        requiring_review=sum(entry.requires_review for entry in entries),
        with_protection_signals=sum(bool(entry.protection_signals) for entry in entries),
        by_category=_items(Counter(category for entry in entries for category in entry.category_ids)),
        by_proposed_action=_items(Counter(entry.proposed_action.value for entry in entries)),
        trash_candidates=sum(entry.proposed_action == ProposedAction.TRASH for entry in entries),
        requiring_approval=sum(entry.approval_required for entry in entries),
        requiring_destructive_approval=sum(entry.approval_type == "destructive" for entry in entries),
        authority_established=sum(entry.authority_established for entry in entries),
        executable=sum(entry.executable for entry in entries),
        by_classification_band=_items(Counter(entry.classification_band for entry in entries)),
        by_action_band=_items(Counter(entry.action_band for entry in entries)),
        oldest_received_at=min(dates) if dates else None,
        newest_received_at=max(dates) if dates else None,
        scan_limit=scan.start.limit,
        scope_label_ids=scan.start.scope_label_ids,
        observed_unique_messages=scan.observed_unique_messages,
        inventory=scan.finish.inventory if scan.finish else None,
    )
