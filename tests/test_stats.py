"""Proposal counts are distinct from applied mailbox changes."""

from datetime import datetime, timezone

import pytest

from dam.models import ProposedAction
from dam.stats import StatisticsInput, calculate_statistics
from dam.storage import InventoryCounts, ScanFinish, ScanRecord, ScanStart

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def scan(count):
    return ScanRecord(start=ScanStart(run_id="synthetic", account_id="account",
        config_fingerprint="0" * 64, started_at=NOW, as_of=NOW, limit=10),
        finish=ScanFinish(ended_at=NOW, status="completed", inventory=InventoryCounts(
            label_total=100, pages_read=1, pagination_limited=True,
            completeness="partial", discrepancy="unresolved")),
        observed_unique_messages=count)


def entry(message_id, day, category=(), action=ProposedAction.NO_ACTION, **changes):
    from datetime import timedelta
    data = dict(message_id=message_id, received_at=NOW-timedelta(days=day),
        category_ids=category, proposed_action=action, requires_review=False,
        protection_signals=(), approval_required=False, approval_type="none",
        authority_established=False, executable=False, classification_band="high",
        action_band="high")
    return StatisticsInput(**(data | changes))


def test_statistics_counts_coverage_and_no_actual_after():
    entries = [
        entry("a", 30, ("finance",), protection_signals=("protected",)),
        entry("b", 20, ("promotions",), ProposedAction.ARCHIVE,
              approval_required=True, approval_type="mailbox_mutation"),
        entry("c", 10, ("promotions",), ProposedAction.TRASH,
              approval_required=True, approval_type="destructive"),
        entry("d", 1, requires_review=True, classification_band="insufficient",
              action_band="insufficient"),
    ]
    stats = calculate_statistics(reversed(entries), scan(4))
    assert (stats.total_messages, stats.classified, stats.unclassified, stats.requiring_review) == (4, 3, 1, 1)
    assert stats.with_protection_signals == 1
    assert [(x.key, x.count) for x in stats.by_category] == [("finance", 1), ("promotions", 2)]
    assert [(x.key, x.count) for x in stats.by_proposed_action] == [("archive", 1), ("no_action", 2), ("trash", 1)]
    assert (stats.trash_candidates, stats.requiring_approval, stats.requiring_destructive_approval) == (1, 2, 1)
    assert stats.authority_established == stats.executable == stats.executed_gmail_actions == 0
    assert stats.executed_archives == stats.executed_trash == 0
    assert stats.actual_inbox_after is None
    assert stats.oldest_received_at == entries[0].received_at
    assert stats.newest_received_at == entries[-1].received_at
    assert stats.scan_limit == 10 and stats.scope_label_ids == ("INBOX",)
    assert stats.inventory.completeness == "partial" and stats.inventory.label_total == 100
    assert [(x.key, x.count) for x in stats.by_classification_band] == [("high", 3), ("insufficient", 1)]


def test_statistics_rejects_duplicate_or_incomplete_input():
    with pytest.raises(ValueError, match="Duplicate"):
        calculate_statistics([entry("a", 1), entry("a", 2)], scan(2))
    with pytest.raises(ValueError, match="cover"):
        calculate_statistics([entry("a", 1)], scan(2))
