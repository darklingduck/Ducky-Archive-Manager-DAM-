"""Learn boundary using synthetic fixtures, fake Gmail, and temporary private files."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import gc
import json
import shlex
import shutil
import stat
from types import SimpleNamespace
import weakref

import pytest
import yaml

from dam.categories import CategoryError
from dam.category_presentation import CategoryDetail, render_categories
from dam.cli import main
from dam.config import ConfigurationError, load_config
from dam.learn_operations import LearnControlResult, LearnRequest, learn
from dam.learn_presentation import (
    LearnConfirmation, LearnDiagnostic, LearnDiagnosticCode, LearnDisplay,
    render_learn,
)
from dam.learning import (
    LearningError, configuration_with_learned_rules, load_learned_rules,
    propose_classification_rule, render_candidate, save_classification_rule,
)
from dam.review import ReviewScopeError, review_gmail_message
from dam.review_operations import ReviewRequest, review_message
from dam.review_presentation import render_review_display
from dam.scan import default_config_directory, load_synthetic_messages, MAX_INITIAL_GMAIL_LIMIT
from dam.teaching_queries import QueryDisposition
from test_review import Backend, Messages, Service, NOW, TARGET, OTHER, private, forbid_real_oauth_and_network

SYNTHETIC = "synthetic-004-unknown"
INPUT_ERROR = "DAM learn rejected the classification input or private rule file; no mailbox actions executed.\n"
INTERNAL_ERROR = "DAM learn failed internally; no mailbox actions executed.\n"
CONFIRM_ERROR = "DAM learn: --save requires --confirm-fingerprint, and confirmation requires --save.\n"
SCOPE_ERROR = "DAM learn rejected the category or message scope; no rule saved.\n"
OUTSIDE_ERROR = "DAM learn: message is outside the current Inbox review scope; no rule saved.\n"
CATALOG_ERROR = "DAM learn rejected the private category catalog; no rule saved.\n"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected Operation, repository, or source capability")


def leaves(value):
    if is_dataclass(value):
        for field in fields(value):
            yield from leaves(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            yield from leaves(item)
    else:
        yield value


class ChangingMessages(Messages):
    sender = None
    subject = None

    def get(self, **kwargs):
        request = super().get(**kwargs)
        for header in request.response["payload"]["headers"]:
            value = self.sender if header["name"] == "From" else self.subject
            if value is not None:
                header["value"] = value
        return request


@pytest.fixture
def ctx(private, tmp_path, monkeypatch):
    paths, learned = private
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("categories.yaml", "rules.yaml", "settings.yaml"):
        shutil.copyfile(default_config_directory() / name, directory / name)
    synthetic = load_synthetic_messages()
    messages, backend = ChangingMessages(), Backend()
    calls, expected, candidate_refs, domain_refs, saves = [], [], [], [], []
    class Clock:
        @staticmethod
        def now(zone):
            return NOW
    monkeypatch.setattr("dam.learn_operations.datetime", Clock)
    monkeypatch.setattr("dam.learn_operations.default_config_directory", lambda: directory)
    monkeypatch.setattr("dam.learn_operations.default_learned_rules_path", lambda: learned)
    catalog = learned.with_name("categories.yaml")
    monkeypatch.setattr("dam.learn_operations.default_catalog_path", lambda: catalog)
    monkeypatch.setattr("dam.learn_operations.load_synthetic_messages", lambda: synthetic)
    def injected(message_id, **kwargs):
        calls.append((message_id, kwargs))
        result = review_gmail_message(message_id, paths=paths, backend=backend,
            service_factory=lambda *a, **k: Service(messages), config_directory=directory,
            as_of=NOW, **kwargs)
        domain_refs.extend((weakref.ref(result), weakref.ref(result.message), weakref.ref(result.config)))
        return result
    def proposal(*args, **kwargs):
        result = propose_classification_rule(*args, **kwargs)
        expected.append(render_candidate(result))
        candidate_refs.append(weakref.ref(result))
        return result
    def save(*args, **kwargs):
        saves.append(True)
        return save_classification_rule(*args, **kwargs)
    monkeypatch.setattr("dam.learn_operations.review_gmail_message", injected)
    monkeypatch.setattr("dam.learn_operations.propose_classification_rule", proposal)
    monkeypatch.setattr("dam.learn_operations.save_classification_rule", save)
    for target in (
        "dam.review_operations.review_message", "dam.storage.Storage.open", "dam.storage.Storage.record_approval",
        "dam.gmail.read_inbox", "dam.gmail.read_authenticated_profile",
        "dam.scan.read_inbox", "dam.durable_gmail.read_inbox", "dam.teaching.TeachingService.confirm",
    ):
        monkeypatch.setattr(target, forbidden)
    return SimpleNamespace(paths=paths, learned=learned, directory=directory, catalog=catalog, synthetic=synthetic,
        messages=messages, backend=backend, calls=calls, expected=expected,
        candidate_refs=candidate_refs, domain_refs=domain_refs, saves=saves)


def request(ctx, *, gmail=False, message_id=None, **values):
    return LearnRequest(message_id or (TARGET if gmail else SYNTHETIC), "promotions", gmail=gmail,
        learned_rules_file=str(ctx.learned), category_catalog_file=str(ctx.catalog), **values)


def invoke(request, sink=None):
    views = []
    result = learn(request, present=sink or views.append)
    return result, views


def confirmed(request, view):
    return replace(request, save=True, confirm_fingerprint=view.fingerprint)


def cli_arguments(request):
    args = ["learn", "--message-id", request.message_id, "--category", request.category_selector]
    if request.gmail:
        args.append("--gmail")
    if request.save:
        args.append("--save")
    for flag, value in (("--confirm-fingerprint", request.confirm_fingerprint),
                        ("--learned-rules-file", request.learned_rules_file),
                        ("--category-catalog-file", request.category_catalog_file)):
        if value is not None:
            args.extend((flag, value))
    return args


def save_command(view):
    lines = render_learn(view).splitlines()
    return shlex.split(lines[lines.index("Save command:") + 1])[1:]


@pytest.mark.parametrize("gmail", [False, True])
def test_all_four_variants_exact_output_persistence_and_provenance(ctx, capsys, gmail):
    original = request(ctx, gmail=gmail)
    result, views = invoke(original)
    preview = views[0]
    assert result == LearnControlResult(QueryDisposition.COMPLETED)
    assert render_learn(preview).startswith(ctx.expected[-1])
    assert not ctx.learned.exists()
    assert main(cli_arguments(original)) == 0
    assert capsys.readouterr() == (render_learn(preview), "")
    assert main(save_command(preview)) == 0
    assert capsys.readouterr() == (ctx.expected[-1].replace("Status: proposed", "Status: saved"), "")
    assert ctx.saves == [True]
    learned = load_learned_rules(ctx.learned)
    assert learned.schema_version == 2 and len(learned.records) == 1
    record = learned.records[0]
    assert record.source_message_id == original.message_id
    assert record.candidate_fingerprint == preview.fingerprint
    assert record.source == "human_explicit_save" and record.category_permanent_id is not None
    assert record.rule.proposed_action.value == "no_action" and record.rule.approval_ref is None
    assert record.rule.match.sender_emails_any == (preview.exact_sender,)
    assert stat.S_IMODE(ctx.learned.stat().st_mode) == 0o600
    assert stat.S_IMODE(ctx.learned.parent.stat().st_mode) == 0o700
    assert len(ctx.messages.get_calls) == (3 if gmail else 0)
    assert ctx.backend.decode_calls == (3 if gmail else 0)
    assert not ctx.messages.list_calls
    for call in ctx.messages.get_calls:
        assert call["id"] == TARGET and call["format"] == "metadata" and call["userId"] == "me"
        assert call["metadataHeaders"] == ["From", "Subject"]
        assert all(part not in call["fields"] for part in ("body", "snippet", "raw", "attachment"))
    for name in ("modify", "batchModify", "trash", "delete", "batchDelete", "send", "attachments"):
        assert not hasattr(ctx.messages, name)
    assert MAX_INITIAL_GMAIL_LIMIT == 10


def test_synthetic_never_authenticates_or_calls_review_operation(ctx, monkeypatch):
    for target in ("dam.learn_operations.review_gmail_message", "dam.review.authenticate", "dam.auth.authenticate",
                   "dam.review.read_message", "dam.review_operations.review_message"):
        monkeypatch.setattr(target, forbidden)
    original = request(ctx)
    _, views = invoke(original)
    assert invoke(confirmed(original, views[0]))[0].disposition is QueryDisposition.COMPLETED
    assert ctx.calls == [] and ctx.backend.decode_calls == 0 and ctx.messages.get_calls == []


@pytest.mark.parametrize("gmail", [False, True])
def test_preview_no_save_and_deterministic_exact_candidate(ctx, monkeypatch, gmail):
    monkeypatch.setattr("dam.learn_operations.save_classification_rule", forbidden)
    original = request(ctx, gmail=gmail)
    first, views = invoke(original)
    second, again = invoke(original)
    assert first == second == LearnControlResult(QueryDisposition.COMPLETED)
    assert views[0] == again[0] and views[0].fingerprint == again[0].fingerprint
    assert not ctx.learned.exists() and ctx.saves == []
    if not gmail:
        config = configuration_with_learned_rules(load_config(ctx.directory, category_catalog_path=ctx.catalog), ctx.learned)
        source = next(item for item in ctx.synthetic if item.message_id == SYNTHETIC)
        expected = propose_classification_rule(source, "promotions", config, as_of=NOW,
            sample=tuple(item for item in ctx.synthetic if item.message_id != SYNTHETIC))
        assert views[0].fingerprint == expected.fingerprint
        assert render_learn(views[0]).startswith(render_candidate(expected))
    else:
        assert len(ctx.messages.get_calls) == 2


@pytest.mark.parametrize("case", ["without_fingerprint", "without_save"])
@pytest.mark.parametrize("gmail", [False, True])
def test_confirmation_pair_rejected_before_acquisition(ctx, capsys, case, gmail):
    original = request(ctx, gmail=gmail)
    malformed = replace(original, save=case == "without_fingerprint",
                        confirm_fingerprint="0" * 64 if case == "without_save" else None)
    result, views = invoke(malformed)
    assert result == LearnControlResult(QueryDisposition.REJECTED)
    assert render_learn(views[0]) == CONFIRM_ERROR
    assert main(cli_arguments(malformed)) == 2
    assert capsys.readouterr() == ("", CONFIRM_ERROR)
    assert not ctx.learned.exists() and not ctx.saves and not ctx.calls


@pytest.mark.parametrize("change", ["fingerprint", "category", "sender", "configuration", "file"])
def test_stale_or_incorrect_confirmations_never_save(ctx, change):
    original = request(ctx, gmail=True)
    _, views = invoke(original)
    save = confirmed(original, views[0])
    if change == "fingerprint":
        save = replace(save, confirm_fingerprint="0" * 64)
    elif change == "category":
        save = replace(save, category_selector="finance")
    elif change == "sender":
        ctx.messages.sender = "Changed <changed@different.example.invalid>"
    elif change == "configuration":
        document = yaml.safe_load((ctx.directory / "settings.yaml").read_text())
        document["confidence"]["auto_threshold"] = .96
        (ctx.directory / "settings.yaml").write_text(yaml.safe_dump(document))
    else:
        unrelated = next(item for item in ctx.synthetic if item.message_id == SYNTHETIC)
        config = load_config(ctx.directory)
        candidate = propose_classification_rule(unrelated, "finance", config, as_of=NOW)
        save_classification_rule(candidate, config, ctx.learned, expected_fingerprint=candidate.fingerprint)
    before = ctx.learned.read_bytes() if ctx.learned.exists() else None
    result, diagnostics = invoke(save)
    assert result == LearnControlResult(QueryDisposition.REJECTED)
    assert render_learn(diagnostics[0]) == INPUT_ERROR
    assert (ctx.learned.read_bytes() if ctx.learned.exists() else None) == before
    assert len(ctx.messages.get_calls) == 2 and ctx.backend.decode_calls == 2


def test_changed_inbox_scope_during_gmail_save_is_preserved(ctx, capsys):
    original = request(ctx, gmail=True)
    _, views = invoke(original)
    ctx.messages.inbox = False
    assert main(cli_arguments(confirmed(original, views[0]))) == 2
    assert capsys.readouterr() == ("", OUTSIDE_ERROR)
    assert len(ctx.messages.get_calls) == 2 and not ctx.learned.exists() and ctx.saves == []


@pytest.mark.parametrize("gmail", [False, True])
def test_duplicates_and_conflicts_preserve_file_and_authority(ctx, gmail):
    original = request(ctx, gmail=gmail)
    _, previews = invoke(original)
    assert invoke(confirmed(original, previews[0]))[0].disposition is QueryDisposition.COMPLETED
    before = ctx.learned.read_bytes()
    # Old confirmation must be rejected after configuration changes.
    assert invoke(confirmed(original, previews[0]))[0].disposition is QueryDisposition.REJECTED
    _, duplicates = invoke(original)
    assert duplicates[0].equivalent_rule_id == previews[0].rule_id
    result, saved = invoke(confirmed(original, duplicates[0]))
    assert result.disposition is QueryDisposition.COMPLETED and saved[0].status == "equivalent_exists"
    assert ctx.learned.read_bytes() == before and len(load_learned_rules(ctx.learned).records) == 1
    other_category = replace(original, category_selector="finance")
    _, conflicts = invoke(other_category)
    assert conflicts[0].conflicts
    assert invoke(confirmed(other_category, conflicts[0]))[0].disposition is QueryDisposition.REJECTED
    assert ctx.learned.read_bytes() == before


@pytest.mark.parametrize("checkpoint", ["replace", "after_replace"])
def test_atomic_failure_cleanup_and_explicit_recovery(ctx, monkeypatch, checkpoint):
    original = request(ctx)
    _, previews = invoke(original)
    assert invoke(confirmed(original, previews[0]))[0].disposition is QueryDisposition.COMPLETED
    before = ctx.learned.read_bytes()
    other = request(ctx, gmail=True)
    _, previews = invoke(other)
    import dam.learning as learning
    fsync = learning.os.fsync
    calls = 0
    def fail_replace(*args):
        raise OSError("PRIVATE-REPLACE")
    def fail_sync(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("PRIVATE-DIRECTORY-SYNC")
        return fsync(*args)
    with monkeypatch.context() as patch:
        patch.setattr(learning.os, "replace" if checkpoint == "replace" else "fsync",
                      fail_replace if checkpoint == "replace" else fail_sync)
        result, diagnostics = invoke(confirmed(other, previews[0]))
    assert result.disposition is QueryDisposition.REJECTED and render_learn(diagnostics[0]) == INPUT_ERROR
    assert not tuple(ctx.learned.parent.glob(".learned-rules-*.tmp"))
    if checkpoint == "replace":
        assert ctx.learned.read_bytes() == before
        assert invoke(confirmed(other, previews[0]))[0].disposition is QueryDisposition.COMPLETED
    else:
        assert ctx.learned.read_bytes() != before
        assert invoke(confirmed(other, previews[0]))[0].disposition is QueryDisposition.REJECTED
        _, new_preview = invoke(other)
        assert invoke(confirmed(other, new_preview[0]))[1][0].status == "equivalent_exists"
    assert len(load_learned_rules(ctx.learned).records) == 2


def test_save_uses_existing_lock_and_revalidates_intervening_file(ctx, monkeypatch):
    original = request(ctx)
    _, previews = invoke(original)
    other = ctx.synthetic[-1].model_copy(update={"message_id": "other", "sender": "other@different.invalid"})
    config = load_config(ctx.directory)
    candidate = propose_classification_rule(other, "finance", config, as_of=NOW)
    def race(*args, **kwargs):
        save_classification_rule(candidate, config, ctx.learned, expected_fingerprint=candidate.fingerprint)
        return save_classification_rule(*args, **kwargs)
    monkeypatch.setattr("dam.learn_operations.save_classification_rule", race)
    result, diagnostics = invoke(confirmed(original, previews[0]))
    assert result.disposition is QueryDisposition.REJECTED and render_learn(diagnostics[0]) == INPUT_ERROR
    assert len(load_learned_rules(ctx.learned).records) == 1
    assert load_learned_rules(ctx.learned).records[0].rule.id == candidate.rule.id


@pytest.mark.parametrize("gmail", [False, True])
@pytest.mark.parametrize("error,failure,diagnostic,code", [
    (RuntimeError, QueryDisposition.FAILED, INTERNAL_ERROR, 1),
    (ValueError, QueryDisposition.REJECTED, INPUT_ERROR, 2),
    (OSError, QueryDisposition.REJECTED, INPUT_ERROR, 2),
])
def test_writer_failure_retains_completed_save_and_no_retry(ctx, monkeypatch, capsys, gmail, error, failure, diagnostic, code):
    original = request(ctx, gmail=gmail)
    _, previews = invoke(original)
    refs = []
    def sink(view):
        refs.append(weakref.ref(view))
        if type(view) is LearnDisplay:
            assert len(load_learned_rules(ctx.learned).records) == 1
            raise error("PRIVATE-WRITER")
        assert render_learn(view) == diagnostic
    result, _ = invoke(confirmed(original, previews[0]), sink)
    assert result == LearnControlResult(QueryDisposition.COMPLETED, presentation_failure=failure)
    assert ctx.saves == [True]
    assert len(ctx.messages.get_calls) == (2 if gmail else 0)
    before = ctx.learned.read_bytes()
    assert main(cli_arguments(confirmed(original, previews[0]))) == 2  # Explicit stale retry never rewrites.
    assert capsys.readouterr() == ("", INPUT_ERROR)
    assert ctx.learned.read_bytes() == before
    gc.collect()
    assert all(ref() is None for ref in refs)
    # New explicit preview/confirmation tests terminal mapping after known completion.
    _, new_preview = invoke(original)
    from dam.cli import _write_learn
    def terminal(view):
        if type(view) is LearnDisplay:
            raise error("PRIVATE-WRITER")
        _write_learn(view)
    monkeypatch.setattr("dam.cli._write_learn", terminal)
    assert main(cli_arguments(confirmed(original, new_preview[0]))) == code
    assert capsys.readouterr() == ("", diagnostic)
    assert ctx.learned.read_bytes() == before


def test_inputs_controls_minimal_immutable_and_no_terminal(ctx, monkeypatch):
    original = request(ctx)
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result, views = invoke(original)
    assert [field.name for field in fields(result)] == ["disposition", "presentation_failure"]
    assert list(leaves(result)) == [QueryDisposition.COMPLETED, None]
    assert original.category_selector not in repr(result) and original.message_id not in repr(result)
    for value in (original, result):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
    for field in fields(original):
        with pytest.raises(TypeError):
            replace(original, **{field.name: {"PRIVATE": "object"}})
    for value in (True, "failed", QueryDisposition.COMPLETED):
        with pytest.raises(TypeError):
            LearnControlResult(QueryDisposition.COMPLETED, presentation_failure=value)
    monkeypatch.setattr("dam.learn_operations.load_synthetic_messages", forbidden)
    assert learn({"PRIVATE": "namespace"}, present=lambda _: None).disposition is QueryDisposition.REJECTED


def test_minimized_contract_and_stateless_writer_no_domain_access(ctx, monkeypatch):
    _, views = invoke(request(ctx))
    display = views[0]
    assert type(display) is LearnDisplay
    assert [field.name for field in fields(display)] == [
        "source_message_id", "selected_category", "rule_id", "rule_version", "exact_sender", "equivalent_rule_id",
        "conflicts", "fingerprint", "status", "evidence", "impact", "confirmation"]
    assert all(type(value) in (str, int, float, bool, type(None)) for value in leaves(display))
    for target in ("dam.learn_operations.load_config", "dam.learn_operations.review_gmail_message",
                   "dam.learn_operations.load_synthetic_messages", "dam.learn_operations.propose_classification_rule",
                   "dam.learn_operations.save_classification_rule", "dam.learning.load_learned_rules"):
        monkeypatch.setattr(target, forbidden)
    assert render_learn(display).startswith(ctx.expected[0])
    for private in ("A fictional question", "synthetic-unknown-thread", "synthetic-account", str(ctx.paths.token)):
        assert private not in render_learn(display) + repr(display)
    for value in (display, display.evidence[0], display.impact[0], display.confirmation,
                  LearnDiagnostic(LearnDiagnosticCode.INPUT)):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        for field in fields(value):
            with pytest.raises(TypeError):
                replace(value, **{field.name: {"PRIVATE": "domain"}})
    for broad in (SimpleNamespace(rule="PRIVATE"), ctx.synthetic[0], load_config(ctx.directory)):
        with pytest.raises(TypeError, match="Unsupported"):
            render_learn(broad)
    assert render_learn.__dict__ == {} and render_learn.__closure__ is None


def test_structured_confirmation_original_selectors_and_safe_quoting(ctx, tmp_path):
    catalog = tmp_path / "home ' $(literal) ` space" / ".config" / "dam" / "categories.yaml"
    learned = catalog.with_name("learned-rules.yaml")
    category = load_config(ctx.directory).categories.categories[-1].permanent_id
    original = replace(request(ctx), category_selector=category, learned_rules_file=str(learned), category_catalog_file=str(catalog))
    _, views = invoke(original)
    confirmation = views[0].confirmation
    assert type(confirmation) is LearnConfirmation
    assert confirmation.category_selector == category and views[0].selected_category == "promotions"
    assert confirmation.learned_rules_file == str(learned) and confirmation.category_catalog_file == str(catalog)
    args = save_command(views[0])
    assert args[args.index("--category") + 1] == category
    assert args[args.index("--learned-rules-file") + 1] == str(learned)
    assert main(args) == 0
    assert load_learned_rules(learned).records[0].category_permanent_id == category


@pytest.mark.parametrize("error,diagnostic,disposition", [
    (LearningError, INPUT_ERROR, QueryDisposition.REJECTED),
    (ConfigurationError, INPUT_ERROR, QueryDisposition.REJECTED),
    (CategoryError, CATALOG_ERROR, QueryDisposition.REJECTED),
    (ReviewScopeError, SCOPE_ERROR, QueryDisposition.REJECTED),
    (RuntimeError, INTERNAL_ERROR, QueryDisposition.FAILED),
])
def test_sanitized_private_sentinel_errors_and_exit_codes(ctx, monkeypatch, capsys, caplog, error, diagnostic, disposition):
    def fail(*args, **kwargs):
        raise error("PRIVATE-SOURCE-ERROR")
    monkeypatch.setattr("dam.learn_operations.load_synthetic_messages", fail)
    original = request(ctx)
    result, views = invoke(original)
    assert result == LearnControlResult(disposition)
    assert render_learn(views[0]) == diagnostic
    assert main(cli_arguments(original)) == (1 if disposition is QueryDisposition.FAILED else 2)
    assert capsys.readouterr() == ("", diagnostic)
    assert "PRIVATE" not in repr(result) + repr(views) + caplog.text


def test_useful_unknown_category_unknown_message_and_gmail_scope_diagnostics(ctx, capsys):
    bad = replace(request(ctx, gmail=True), category_selector="PRIVATE-UNKNOWN")
    assert main(cli_arguments(bad)) == 2
    assert capsys.readouterr() == ("", "Unknown DAM category 'PRIVATE-UNKNOWN'. Run 'dam categories' to list valid categories. No rule saved.\n")
    assert ctx.messages.get_calls == [] and ctx.backend.decode_calls == 0
    assert main(cli_arguments(replace(request(ctx), message_id="PRIVATE-UNKNOWN"))) == 2
    assert capsys.readouterr() == ("", INPUT_ERROR)
    assert not ctx.learned.exists()


def test_domain_and_display_release_cross_message_and_operation_isolation(ctx, monkeypatch):
    refs, outputs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_learn(view))
    invoke(request(ctx, gmail=True), sink)
    invoke(request(ctx, gmail=True, message_id=OTHER), sink)
    assert "alerts@updates.example.invalid" in outputs[0] and "alerts@updates.example.invalid" not in outputs[1]
    assert "other@other.example.invalid" not in outputs[0]
    # A separate Review Operation uses its own sink and cannot expose Learn state.
    from dam.review_operations import review_gmail_message as review_service
    def injected(message_id, **kwargs):
        return review_service(message_id, paths=ctx.paths, backend=ctx.backend,
            service_factory=lambda *a, **k: Service(ctx.messages), config_directory=ctx.directory, as_of=NOW, **kwargs)
    monkeypatch.setattr("dam.review_operations.review_message", review_message)
    monkeypatch.setattr("dam.review_operations.review_gmail_message", injected)
    review_views = []
    assert review_message(ReviewRequest(OTHER, True, str(ctx.learned), str(ctx.catalog)), present=review_views.append).disposition is QueryDisposition.COMPLETED
    assert "Candidate fingerprint" not in render_review_display(review_views[0])
    category = render_categories(CategoryDetail("CAT-example", "private", "CATEGORY-SENTINEL", None, "active"))
    assert "updates.example.invalid" not in category
    assert all("CATEGORY-SENTINEL" not in output for output in outputs)
    gc.collect()
    assert all(ref() is None for ref in refs + ctx.candidate_refs + ctx.domain_refs)


def test_writer_interruption_and_projection_failure_preserve_saved_rule(ctx, monkeypatch):
    original = request(ctx)
    _, views = invoke(original)
    def fail(*args):
        raise ValueError("PRIVATE-PROJECTION")
    with monkeypatch.context() as patch:
        patch.setattr("dam.learn_operations.LearnDisplay", fail)
        result, _ = invoke(confirmed(original, views[0]))
    assert result == LearnControlResult(QueryDisposition.COMPLETED, QueryDisposition.REJECTED)
    assert len(load_learned_rules(ctx.learned).records) == 1 and ctx.saves == [True]
    _, previews = invoke(original)
    refs = []
    def interrupt(view):
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        invoke(confirmed(original, previews[0]), interrupt)
    assert len(load_learned_rules(ctx.learned).records) == 1
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_failed_diagnostic_delivery_never_retries_or_acquires_again(ctx):
    def fail(view):
        raise RuntimeError("PRIVATE-OUTPUT")
    result, _ = invoke(request(ctx, gmail=True), fail)
    assert result == LearnControlResult(QueryDisposition.COMPLETED, QueryDisposition.FAILED)
    assert len(ctx.messages.get_calls) == ctx.backend.decode_calls == 1
    assert not ctx.learned.exists() and ctx.saves == []


def test_persistence_lock_descriptor_closed_before_display(ctx, monkeypatch):
    import os
    import dam.learning as learning
    original = request(ctx)
    _, previews = invoke(original)
    real_flock = learning.fcntl.flock
    descriptors = []
    def locking(descriptor, mode):
        assert mode == learning.fcntl.LOCK_EX
        descriptors.append(descriptor)
        return real_flock(descriptor, mode)
    monkeypatch.setattr(learning.fcntl, "flock", locking)
    def sink(view):
        assert type(view) is LearnDisplay and view.status == "saved"
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
    result, _ = invoke(confirmed(original, previews[0]), sink)
    assert result == LearnControlResult(QueryDisposition.COMPLETED)
    assert ctx.saves == [True]
