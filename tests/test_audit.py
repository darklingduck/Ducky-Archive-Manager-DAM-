"""Step 7 uses synthetic messages only; no Gmail or external requests."""

from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

from dam.actions import propose_action
from dam.audit import build_preview, preview_from_storage, record_preview_event, render_preview
from dam.classifier import classify
from dam.config import configuration_fingerprint, rule_scope_fingerprint
from dam.models import Configuration, MessageMetadata
from dam.storage import InventoryCounts, ObservationRecord, ScanFinish, ScanRecord, ScanStart, Storage

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def config(tmp_path, *, version=1):
    return Configuration.model_validate({
        "settings": {"state": {"database_path": str(tmp_path / "state" / "dam.db")}},
        "categories": {"categories": [
            {"id": "finance", "name": "Finance"}, {"id": "promotions", "name": "Promotions"}]},
        "rules": {"rules": [
            {"id": "financial", "version": version, "kind": "safety", "protect": True,
             "match": {"sender_domains_any": ["bank.example.invalid"], "subject_contains_any": ["statement"]},
             "category_ids": ["finance"], "proposed_action": "no_action"},
            {"id": "archive_offer", "version": 1,
             "match": {"sender_domains_any": ["offers.example.invalid"], "subject_contains_any": ["offer"]},
             "category_ids": ["promotions"], "proposed_action": "archive"},
            {"id": "trash_offer", "version": 1,
             "match": {"sender_domains_any": ["trash.example.invalid"], "subject_contains_any": ["offer"]},
             "category_ids": ["promotions"], "proposed_action": "trash", "approval_ref": "synthetic_ref"},
        ]},
    })


def records(cfg, specs):
    observations, proposals = [], []
    for message_id, sender, subject, days, thread in specs:
        m = MessageMetadata(account_id="synthetic_account", message_id=message_id,
            thread_id=thread, sender=sender, subject=subject,
            received_at=NOW-timedelta(days=days), label_ids=("INBOX",))
        c = classify(m, cfg.rules, as_of=NOW, settings=cfg.settings)
        p = propose_action(m, c, cfg.rules, as_of=NOW, settings=cfg.settings)
        observations.append(ObservationRecord(run_id="run_1", observed_at=NOW, metadata=m, classification=c))
        proposals.append(p)
    return observations, proposals


SPECS = [
    ("bank", "records@bank.example.invalid", "Statement", 30, "thread_a"),
    ("archive", "news@offers.example.invalid", "Weekly offer", 20, "shared_thread"),
    ("trash", "news@trash.example.invalid", "Weekly offer", 10, "shared_thread"),
    ("unknown", "person@unknown.example.invalid", "A question", 1, "thread_b"),
]


def preview(cfg, specs=SPECS, *, observations=None, proposals=None, generated_at=NOW,
            fingerprint=None, versions=None):
    observations, proposals = (observations, proposals) if observations is not None else records(cfg, specs)
    scan = ScanRecord(start=ScanStart(run_id="run_1", account_id="synthetic_account",
        config_fingerprint=fingerprint or configuration_fingerprint(cfg), started_at=NOW,
        as_of=NOW, limit=10), finish=ScanFinish(ended_at=NOW, status="completed",
        inventory=InventoryCounts(label_total=25, pages_read=1, pagination_limited=True,
                                  completeness="partial", discrepancy="unresolved")),
        observed_unique_messages=len(observations))
    refs = versions if versions is not None else tuple((r.id, r.version, rule_scope_fingerprint(r)) for r in cfg.rules.rules)
    return build_preview(scan, observations, proposals, rule_versions=refs, generated_at=generated_at)


def test_one_message_preview_and_destructive_authority(tmp_path):
    p = preview(config(tmp_path), [SPECS[2]])
    assert p.exact_message_ids == ("trash",)
    assert p.destructive_candidate_ids == ("trash",)
    assert p.entries[0].approval_type == "destructive"
    assert p.entries[0].approval_status == "reference_unverified"
    assert not p.entries[0].authority_established and not p.entries[0].executable
    assert p.statistics.executed_gmail_actions == 0


def test_mixed_preview_order_fingerprint_json_and_text_are_deterministic(tmp_path):
    cfg = config(tmp_path)
    obs, props = records(cfg, SPECS)
    first = preview(cfg, observations=obs, proposals=props)
    reordered = preview(cfg, observations=list(reversed(obs)), proposals=list(reversed(props)))
    assert first.to_json() == reordered.to_json()
    assert render_preview(first) == render_preview(reordered)
    assert first.fingerprint == reordered.fingerprint
    assert first.exact_message_ids == ("archive", "bank", "trash", "unknown")
    assert first.destructive_candidate_ids == ("trash",)
    assert first.entries[0].thread_id == first.entries[2].thread_id == "shared_thread"
    assert len(first.entries) == 4
    assert json.loads(first.to_json())["entries"][2]["outcome"] == "proposed"
    assert "actual Inbox after: not observed" in render_preview(first)
    assert "Executed Gmail actions: 0" in render_preview(first)
    assert "authority=false; executable=false" in render_preview(first)


def test_fingerprint_tracks_message_set_provenance_protection_and_proposal(tmp_path):
    cfg = config(tmp_path)
    base = preview(cfg)
    assert preview(cfg, SPECS[:-1]).fingerprint != base.fingerprint
    assert preview(cfg, fingerprint="a" * 64).fingerprint != base.fingerprint
    refs = tuple((r.id, r.version, rule_scope_fingerprint(r)) for r in cfg.rules.rules)
    assert preview(cfg, versions=tuple((a, b, "b" * 64) for a, b, _ in refs)).fingerprint != base.fingerprint
    obs, props = records(cfg, SPECS)
    changed = list(props)
    changed[2] = changed[2].model_copy(update={"proposed_action": "mark_review", "approval_required": False,
                                       "approval_type": "none", "approval_status": "not_required"})
    assert preview(cfg, observations=obs, proposals=changed).fingerprint != base.fingerprint
    changed = list(props)
    changed[0] = changed[0].model_copy(update={"protection_signals": ("Critical",)})
    assert preview(cfg, observations=obs, proposals=changed).fingerprint != base.fingerprint
    assert preview(cfg, generated_at=NOW+timedelta(hours=1)).fingerprint == base.fingerprint


def test_no_sensitive_payload_or_unexpected_side_effects(tmp_path):
    cfg = config(tmp_path)
    p = preview(cfg, [SPECS[2]])
    output = p.to_json() + render_preview(p)
    assert "Weekly offer" not in output
    assert "news@trash.example.invalid" not in output
    assert "synthetic_ref" not in output
    code = """
import sys
def guard(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.__new__', 'os.mkdir'):
        raise AssertionError(event)
    if event == 'open' and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError('file write')
sys.addaudithook(guard)
import dam.audit
import dam.stats
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_persisted_preview_event_is_proposal_only(tmp_path):
    cfg = config(tmp_path)
    observations, proposals = records(cfg, [SPECS[2]])
    with Storage.open(cfg.settings) as store:
        fingerprint = store.save_configuration(cfg)
        store.start_scan(ScanStart(run_id="run_1", account_id="synthetic_account",
            config_fingerprint=fingerprint, started_at=NOW, as_of=NOW, limit=10))
        store.record_observation("run_1", observations[0].metadata, observations[0].classification,
                                 proposals[0], observed_at=NOW)
        store.finish_scan("run_1", ScanFinish(ended_at=NOW, status="completed"))
        p = preview_from_storage(store, "run_1", generated_at=NOW)
        record_preview_event(store, p)
        record_preview_event(store, p)
        events = store.audit_events("run_1")
    assert len(events) == 1
    assert events[0].state == "proposed" and not events[0].mailbox_modified
    assert not events[0].subscription_changed
    assert p.destructive_candidate_ids == ("trash",)
