"""Presentation-only checks using synthetic metadata and private temporary paths."""

from datetime import datetime, timezone
import re
import shlex

from dam.actions import propose_action
from dam.classifier import classify
from dam.cli import _category_listing, main
from dam.config import load_config
from dam.learning import load_learned_rules
from dam.models import CategoriesConfig, Category
from dam.presentation import classification_basis, review_reasons
from dam.review import GmailReviewResult
from dam.scan import MAX_INITIAL_GMAIL_LIMIT, default_config_directory, load_synthetic_messages, run_synthetic_scan


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _save_arguments(output: str) -> list[str]:
    lines = output.splitlines()
    command = shlex.split(lines[lines.index("Save command:") + 1])
    assert command[0] == "dam"
    return command[1:]


def test_category_listing_has_deterministic_multi_level_hierarchy():
    items = (
        Category(id="gamma", name="Gamma", parent_id="beta"),
        Category(id="zeta", name="Zeta"),
        Category(id="beta", name="Beta", parent_id="alpha"),
        Category(id="alpha", name="Alpha"),
    )
    first = _category_listing(CategoriesConfig(categories=items))
    second = _category_listing(CategoriesConfig(categories=tuple(reversed(items))))
    assert first == second
    lines = first.splitlines()
    assert lines[0].startswith("Alpha") and lines[0].endswith("alpha")
    assert lines[1].startswith("  |-- Beta") and lines[1].endswith("beta")
    assert lines[2].startswith("    |-- Gamma") and lines[2].endswith("gamma")
    assert lines[3].startswith("Zeta") and lines[3].endswith("zeta")
    assert lines[0].index("alpha") == lines[3].index("zeta")


def test_category_previews_give_copyable_confirmed_commands(tmp_path, capsys):
    catalog = tmp_path / ".config" / "dam" / "categories.yaml"
    learned = catalog.with_name("learned-rules.yaml")
    base = ["categories", "--catalog-file", str(catalog), "--learned-rules-file", str(learned)]
    operations = (
        ([*base, "add", "--key", "calendar", "--name", "Calendar & Scheduling"], "--confirm-id"),
        ([*base, "rename", "calendar", "--name", "Calendar Events"], None),
        ([*base, "move", "calendar", "--parent", "finance"], None),
        ([*base, "retire", "calendar"], "--confirm-retire"),
    )
    permanent_id = None
    for preview_args, extra_confirmation in operations:
        previous = catalog.read_bytes() if catalog.exists() else None
        assert main(preview_args) == 0
        shown = capsys.readouterr().out
        assert "preview only" in shown and "Confirmation required to save:" in shown
        assert "Mailbox actions executed: 0" in shown
        identity = re.search(r"Category ID: (CAT-[A-Z2-7]{26})", shown).group(1)
        fingerprint = re.search(r"Fingerprint: ([0-9a-f]{64})", shown.split("Confirmation required to save:", 1)[1]).group(1)
        assert identity in shown and fingerprint in shown
        assert permanent_id is None or identity == permanent_id
        permanent_id = identity
        assert (catalog.read_bytes() if catalog.exists() else None) == previous
        command = _save_arguments(shown)
        assert "--save" in command and "--confirm-fingerprint" in command
        assert command[command.index("--confirm-fingerprint") + 1] == fingerprint
        assert command[command.index("--catalog-file") + 1] == str(catalog)
        assert command[command.index("--learned-rules-file") + 1] == str(learned)
        if extra_confirmation:
            assert command[command.index(extra_confirmation) + 1] == identity
        assert main(command) == 0
        assert "saved" in capsys.readouterr().out
        assert catalog.exists()
    assert main([*base, "show", "calendar"]) == 0
    details = capsys.readouterr().out
    assert f"Permanent ID: {permanent_id}" in details
    assert "Status: retired" in details
    assert not learned.exists()


def test_synthetic_learn_preview_prints_actual_copyable_save_command(tmp_path, capsys):
    learned = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    catalog = learned.with_name("categories.yaml")
    preview_args = ["learn", "--message-id", "synthetic-004-unknown", "--category", "promotions",
                    "--learned-rules-file", str(learned), "--category-catalog-file", str(catalog)]
    assert main(preview_args) == 0
    shown = capsys.readouterr().out
    assert not learned.exists() and "Status: proposed" in shown
    fingerprint = re.search(r"Candidate fingerprint: ([0-9a-f]{64})", shown).group(1)
    assert f"Confirmation required to save:\nFingerprint: {fingerprint}" in shown
    assert "<candidate fingerprint>" not in shown
    command = _save_arguments(shown)
    assert "--gmail" not in command
    assert command[command.index("--message-id") + 1] == "synthetic-004-unknown"
    assert command[command.index("--category-catalog-file") + 1] == str(catalog)
    assert command[command.index("--confirm-fingerprint") + 1] == fingerprint
    assert main(command) == 0
    assert "Status: saved" in capsys.readouterr().out
    assert len(load_learned_rules(learned).records) == 1


def test_mocked_gmail_learn_command_preserves_mode_and_requires_fresh_read(tmp_path, monkeypatch, capsys):
    learned = tmp_path / ".config" / "dam" / "learned-rules.yaml"
    config = load_config(default_config_directory())
    source = load_synthetic_messages()[-1]
    classification = classify(source, config.rules, as_of=NOW, settings=config.settings,
                              category_config=config.categories)
    proposal = propose_action(source, classification, config.rules, as_of=NOW, settings=config.settings)
    reviewed = GmailReviewResult(message=source, classification=classification, proposal=proposal,
                                 config=config, as_of=NOW, auth_source="mock")
    reads = []
    def mocked_review(message_id, **_kwargs):
        reads.append(message_id)
        return reviewed
    monkeypatch.setattr("dam.cli.review_gmail_message", mocked_review)
    preview_args = ["learn", "--gmail", "--message-id", source.message_id,
                    "--category", "promotions", "--learned-rules-file", str(learned)]
    assert main(preview_args) == 0
    shown = capsys.readouterr().out
    assert not learned.exists()
    command = _save_arguments(shown)
    assert "--gmail" in command and command[command.index("--message-id") + 1] == source.message_id
    assert command[command.index("--learned-rules-file") + 1] == str(learned)
    assert command[command.index("--confirm-fingerprint") + 1] in shown
    assert main(command) == 0
    assert "Status: saved" in capsys.readouterr().out
    assert reads == [source.message_id, source.message_id]
    assert len(load_learned_rules(learned).records) == 1


def test_presentation_labels_are_pure_and_unknown_codes_fall_back():
    assert classification_basis(("human_accepted_learned_rule",)) == "Human-taught rule"
    assert classification_basis(("configured_rule",)) == "Configured rule"
    assert classification_basis(("future_basis",)) == "future_basis"
    assert classification_basis(None) == "not recorded"
    assert classification_basis(()) == "unresolved"
    assert review_reasons(("evidence_below_high_threshold",)) == "Evidence is below the current high-confidence threshold."
    assert review_reasons(("classification_requires_review",), action=True) == "Classification still requires Review."
    assert review_reasons(("future_classification_reason",)) == "future_classification_reason"
    assert review_reasons(("future_action_reason",), action=True) == "future_action_reason"
    assert review_reasons(None) == "not recorded"
    assert review_reasons(()) == "none"


def test_scan_privacy_confidence_and_authority_are_unchanged():
    result = run_synthetic_scan(limit=100, as_of=NOW, run_id="synthetic-ux")
    from dam.audit import render_preview
    shown = render_preview(result.preview)
    assert "sender=present; subject=present" in shown
    assert "unknown@unknown.example.invalid" not in shown
    assert "Executed Gmail actions: 0" in shown
    assert result.preview.statistics.executed_gmail_actions == 0
    assert all(not item.authority_established and not item.executable for item in result.preview.entries)
    assert MAX_INITIAL_GMAIL_LIMIT == 10
