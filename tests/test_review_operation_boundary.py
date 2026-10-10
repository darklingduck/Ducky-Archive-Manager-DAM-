"""Review boundary: synthetic Gmail metadata and temporary private state only."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import gc
import json
import shutil
from types import SimpleNamespace
import weakref

import pytest

from dam.category_presentation import CategoryDetail, render_categories
from dam.cli import main
from dam.config import ConfigurationError, load_config
from dam.gmail import GMAIL_READONLY_SCOPE
from dam.learning import LearningError, propose_classification_rule, save_classification_rule
from dam.review import ReviewScopeError, render_review, review_gmail_message
from dam.review_operations import ReviewRequest, review_message
from dam.review_presentation import ReviewDiagnostic, ReviewDiagnosticCode, ReviewDisplay, render_review_display
from dam.scan import default_config_directory, MAX_INITIAL_GMAIL_LIMIT
from dam.teaching_presentation import TeachingStatus, render_teaching
from dam.teaching_queries import QueryControlResult, QueryDisposition
from test_review import (
    Backend, Credential, Messages, Service, NOW, TARGET, OTHER, private, forbid_real_oauth_and_network,
)

GMAIL_REQUIRED = "DAM review requires explicit --gmail; no Gmail access attempted.\n"
OUTSIDE = "DAM review: message is outside the current Inbox review scope.\n"
SCOPE_ERROR = "DAM review rejected the message scope.\n"
SAFE_ERROR = "DAM review failed safely; no mailbox actions executed.\n"
INTERNAL_ERROR = "DAM review failed internally; no mailbox actions executed.\n"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected domain, source, or terminal capability")


def leaves(value):
    if is_dataclass(value):
        for field in fields(value):
            yield from leaves(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            yield from leaves(item)
    else:
        yield value


@pytest.fixture
def ctx(private, tmp_path, monkeypatch):
    paths, learned = private
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("categories.yaml", "rules.yaml", "settings.yaml"):
        shutil.copyfile(default_config_directory() / name, directory / name)
    catalog = learned.with_name("categories.yaml")
    messages, backend = Messages(), Backend()
    calls, expected, domain_refs = [], [], []
    classifications, proposals = [], []
    def injected(message_id, **kwargs):
        calls.append((message_id, kwargs))
        result = review_gmail_message(message_id, paths=paths, backend=backend,
            service_factory=lambda *a, **k: Service(messages), config_directory=directory,
            as_of=NOW, **kwargs)
        expected.append(render_review(result))
        classifications.append(result.classification)
        proposals.append(result.proposal)
        domain_refs.extend((weakref.ref(result), weakref.ref(result.message), weakref.ref(result.config)))
        return result
    monkeypatch.setattr("dam.review_operations.review_gmail_message", injected)
    monkeypatch.setattr("dam.review_operations.default_learned_rules_path", lambda: learned)
    monkeypatch.setattr("dam.review_operations.default_catalog_path", lambda: catalog)
    for target in (
        "dam.storage.Storage.open", "dam.storage.Storage.record_approval",
        "dam.gmail.read_inbox", "dam.gmail.read_authenticated_profile",
        "dam.scan.read_inbox", "dam.durable_gmail.read_inbox",
        "dam.teaching.TeachingService.confirm", "dam.teaching.TeachingService.resume",
    ):
        monkeypatch.setattr(target, forbidden)
    return SimpleNamespace(paths=paths, learned=learned, directory=directory, catalog=catalog,
        messages=messages, backend=backend, calls=calls, expected=expected, domain_refs=domain_refs,
        classifications=classifications, proposals=proposals)


def invoke(ctx, message_id=TARGET, **values):
    views = []
    result = review_message(ReviewRequest(message_id, **{"gmail": True, **values}), present=views.append)
    return result, views


def cli_review(message_id=TARGET, *args):
    return main(["review", "--gmail", "--message-id", message_id, *args])


def test_one_metadata_read_exact_output_no_authority_or_sqlite(ctx, capsys):
    files = {path: path.read_bytes() for path in ctx.paths.oauth_directory.iterdir()}
    result, views = invoke(ctx)
    assert result == QueryControlResult(QueryDisposition.COMPLETED)
    assert render_review_display(views[0]) == ctx.expected[-1]
    assert cli_review() == 0
    assert capsys.readouterr() == (ctx.expected[-1], "")
    assert len(ctx.messages.get_calls) == 2 and ctx.backend.decode_calls == 2
    assert not ctx.messages.list_calls
    for call in ctx.messages.get_calls:
        assert call["id"] == TARGET and call["userId"] == "me" and call["format"] == "metadata"
        assert call["metadataHeaders"] == ["From", "Subject"]
        assert not any(word in call["fields"] for word in ("body", "snippet", "raw", "attachment"))
    assert not ctx.learned.exists() and not ctx.catalog.exists()
    assert {path: path.read_bytes() for path in ctx.paths.oauth_directory.iterdir()} == files
    assert all(not proposal.executable and not proposal.authority_established for proposal in ctx.proposals)
    assert "Authority established: false; executable: false; executed Gmail actions: 0" in ctx.expected[-1]
    assert all(not hasattr(ctx.messages, name) for name in (
        "modify", "batchModify", "trash", "delete", "batchDelete", "send", "attachments"))
    assert MAX_INITIAL_GMAIL_LIMIT == 10


def test_explicit_gmail_required_before_any_paths_or_source_access(ctx, monkeypatch, capsys):
    monkeypatch.setattr("dam.review_operations.review_gmail_message", forbidden)
    monkeypatch.setattr("dam.review_operations.default_catalog_path", forbidden)
    monkeypatch.setattr("dam.review_operations.default_learned_rules_path", forbidden)
    result, views = invoke(ctx, gmail=False)
    assert result == QueryControlResult(QueryDisposition.REJECTED)
    assert render_review_display(views[0]) == GMAIL_REQUIRED
    assert main(["review", "--message-id", TARGET]) == 2
    assert capsys.readouterr() == ("", GMAIL_REQUIRED)
    assert ctx.calls == [] and ctx.messages.get_calls == []


@pytest.mark.parametrize("message_id", ["", " ", "\t"])
def test_blank_message_id_rejected_before_config_or_auth(ctx, capsys, message_id):
    result, views = invoke(ctx, message_id)
    assert result == QueryControlResult(QueryDisposition.REJECTED)
    assert render_review_display(views[0]) == SCOPE_ERROR
    assert cli_review(message_id) == 2
    assert capsys.readouterr() == ("", SCOPE_ERROR)
    assert ctx.calls == [] and ctx.backend.decode_calls == 0


@pytest.mark.parametrize("message_id", ["PRIVATE ID", " PRIVATE", "PRIVATE\x00ID"])
def test_malformed_id_preserves_adapter_rejection_no_metadata_read(ctx, capsys, message_id):
    result, views = invoke(ctx, message_id)
    assert result.disposition is QueryDisposition.REJECTED
    assert render_review_display(views[0]) == SAFE_ERROR
    assert ctx.calls[0][0] == message_id  # No trimming/rewriting an opaque ID.
    assert ctx.backend.decode_calls == 1 and ctx.messages.get_calls == []
    assert cli_review(message_id) == 2
    assert capsys.readouterr() == ("", SAFE_ERROR)


def test_missing_id_is_parser_only_and_help_has_no_source_access(ctx, capsys):
    for arguments, code in ((["review", "--gmail"], 2), (["review", "--help"], 0)):
        with pytest.raises(SystemExit) as raised:
            main(arguments)
        assert raised.value.code == code
        capsys.readouterr()
    assert ctx.calls == [] and ctx.backend.decode_calls == 0


def test_request_and_control_immutable_minimal_and_no_terminal_use(ctx, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", forbidden)
        patch.setattr("builtins.input", forbidden)
        result, views = invoke(ctx)
    assert [field.name for field in fields(result)] == ["disposition"]
    assert list(leaves(result)) == [QueryDisposition.COMPLETED]
    request = ReviewRequest(TARGET, gmail=True)
    assert [field.name for field in fields(request)] == ["message_id", "gmail", "learned_rules_file", "category_catalog_file"]
    for value in (request, result):
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        assert not hasattr(value, "__dict__")
    for field in fields(request):
        with pytest.raises(TypeError) as raised:
            replace(request, **{field.name: {"PRIVATE": "object"}})
        assert "PRIVATE" not in str(raised.value)
    monkeypatch.setattr("dam.review_operations.review_gmail_message", forbidden)
    views = []
    assert review_message({"PRIVATE": "namespace"}, present=views.append).disposition is QueryDisposition.REJECTED
    assert render_review_display(views[0]) == SCOPE_ERROR


def test_limited_display_fields_exclude_configuration_evidence_and_thread(ctx):
    result, views = invoke(ctx)
    view = views[0]
    assert type(view) is ReviewDisplay
    assert set(field.name for field in fields(view)) == {
        "message_id", "sender_display", "subject_display", "received_at", "labels", "category_ids",
        "classification_confidence", "classification_basis_codes", "category_teaching_required", "requires_review",
        "classification_review_codes", "action_review_codes", "proposed_action", "approval_type", "approval_status",
    }
    assert all(type(value) in (str, float, bool, type(None)) for value in leaves(view))
    output = render_review_display(view)
    assert "Example Alerts" in output and "Synthetic unexplained update" in output
    for private in ("synthetic-sensitive-snippet", "synthetic-sensitive-body", "synthetic-sensitive-raw",
                    "synthetic-sensitive-attachment", "synthetic-shared-thread", "synthetic-token", "synthetic-secret",
                    "gmail-account-unverified", str(ctx.paths.token)):
        assert private not in output + repr(view) + repr(result)
    assert TARGET not in repr(result)


@pytest.mark.parametrize("sender,subject,expected_sender,expected_subject", [
    (None, None, "<empty>", "<empty>"),
    ("", "", "<empty>", "<empty>"),
    ("alerts@example.invalid\x1b", "See https://example.invalid/?token=PRIVATE\n", "alerts@example.invalid�", "See [URL redacted]�"),
    ("a" * 301, "s" * 301, "a" * 300 + "…", "s" * 300 + "…"),
])
def test_projection_preserves_sanitized_display(ctx, monkeypatch, sender, subject, expected_sender, expected_subject):
    original = review_gmail_message
    def altered(message_id, **kwargs):
        result = original(message_id, paths=ctx.paths, backend=ctx.backend,
            service_factory=lambda *a, **k: Service(ctx.messages), config_directory=ctx.directory, as_of=NOW, **kwargs)
        return replace(result, message=result.message.model_copy(update={"sender": sender, "subject": subject}))
    monkeypatch.setattr("dam.review_operations.review_gmail_message", altered)
    _, views = invoke(ctx)
    assert views[0].sender_display == expected_sender
    assert views[0].subject_display == expected_subject


def test_absent_headers_preserve_absent_distinction(ctx, monkeypatch):
    original = review_gmail_message
    def altered(message_id, **kwargs):
        result = original(message_id, paths=ctx.paths, backend=ctx.backend,
            service_factory=lambda *a, **k: Service(ctx.messages), config_directory=ctx.directory, as_of=NOW, **kwargs)
        values = result.message.model_dump(exclude={"sender", "subject"})
        from dam.models import MessageMetadata
        return replace(result, message=MessageMetadata.model_validate(values))
    monkeypatch.setattr("dam.review_operations.review_gmail_message", altered)
    _, views = invoke(ctx)
    assert views[0].sender_display == views[0].subject_display == "<absent>"


def test_writer_is_stateless_immutable_and_cannot_retrieve_domain_data(ctx, monkeypatch):
    _, views = invoke(ctx)
    for target in (
        "dam.review_operations.review_gmail_message", "dam.review.review_gmail_message", "dam.review.read_message",
        "dam.review.authenticate", "dam.review.load_config", "dam.config.load_config",
        "dam.learning.load_learned_rules", "dam.review.classify", "dam.review.propose_action",
    ):
        monkeypatch.setattr(target, forbidden)
    assert render_review_display(views[0]) == ctx.expected[0]
    for value in (views[0], ReviewDiagnostic(ReviewDiagnosticCode.INTERNAL)):
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        assert not hasattr(value, "__dict__")
        for field in fields(value):
            with pytest.raises(TypeError):
                replace(value, **{field.name: {"PRIVATE": "domain"}})
    for broad in (SimpleNamespace(message="PRIVATE"), ctx.classifications[0], ctx.proposals[0]):
        with pytest.raises(TypeError, match="Unsupported"):
            render_review_display(broad)
    assert render_review_display.__dict__ == {} and render_review_display.__closure__ is None


@pytest.mark.parametrize("error,code,diagnostic", [
    (RuntimeError, 1, INTERNAL_ERROR), (ValueError, 2, SAFE_ERROR), (OSError, 2, SAFE_ERROR),
])
def test_writer_failure_preserves_exit_codes_and_never_retries(ctx, monkeypatch, capsys, error, code, diagnostic):
    refs = []
    def writer(view):
        refs.append(weakref.ref(view))
        if type(view) is ReviewDisplay:
            raise error("PRIVATE-WRITER")
        # A fixed safe diagnostic is a separate release, never another read.
        assert render_review_display(view) == diagnostic
    result = review_message(ReviewRequest(TARGET, gmail=True), present=writer)
    assert result.disposition is (QueryDisposition.FAILED if code == 1 else QueryDisposition.REJECTED)
    assert len(ctx.calls) == len(ctx.messages.get_calls) == ctx.backend.decode_calls == 1
    original_writer = __import__("dam.cli", fromlist=["_write_review"])._write_review
    def terminal_writer(view):
        if type(view) is ReviewDisplay:
            raise error("PRIVATE-WRITER")
        original_writer(view)
    monkeypatch.setattr("dam.cli._write_review", terminal_writer)
    assert cli_review() == code
    assert capsys.readouterr() == ("", diagnostic)
    assert len(ctx.calls) == len(ctx.messages.get_calls) == ctx.backend.decode_calls == 2
    assert ctx.classifications[0] == ctx.classifications[1]
    assert ctx.proposals[0] == ctx.proposals[1]
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_failed_diagnostic_delivery_stops_without_retry(ctx):
    def writer(view):
        raise RuntimeError("PRIVATE-ALL-OUTPUT")
    result = review_message(ReviewRequest(TARGET, gmail=True), present=writer)
    assert result.disposition is QueryDisposition.FAILED
    assert len(ctx.messages.get_calls) == ctx.backend.decode_calls == 1


@pytest.mark.parametrize("error,diagnostic,disposition", [
    (ReviewScopeError, SCOPE_ERROR, QueryDisposition.REJECTED),
    (LearningError, SAFE_ERROR, QueryDisposition.REJECTED),
    (ConfigurationError, SAFE_ERROR, QueryDisposition.REJECTED),
    (ValueError, SAFE_ERROR, QueryDisposition.REJECTED),
    (RuntimeError, INTERNAL_ERROR, QueryDisposition.FAILED),
])
def test_expected_and_unexpected_failures_sanitized(ctx, monkeypatch, capsys, caplog, error, diagnostic, disposition):
    def fail(*args, **kwargs):
        raise error("PRIVATE-SOURCE-ERROR")
    monkeypatch.setattr("dam.review_operations.review_gmail_message", fail)
    result, views = invoke(ctx)
    assert result == QueryControlResult(disposition)
    assert render_review_display(views[0]) == diagnostic
    assert cli_review() == (1 if disposition is QueryDisposition.FAILED else 2)
    assert capsys.readouterr() == ("", diagnostic)
    assert "PRIVATE" not in repr(result) + repr(views) + caplog.text


def test_non_inbox_and_failed_get_keep_useful_safe_errors(ctx, capsys):
    ctx.messages.inbox = False
    assert cli_review() == 2
    assert capsys.readouterr() == ("", OUTSIDE)
    assert len(ctx.messages.get_calls) == 1
    ctx.messages.inbox = True
    def fail(**kwargs):
        raise RuntimeError("PRIVATE-RAW-RESPONSE")
    ctx.messages.get = fail
    assert cli_review() == 2
    assert capsys.readouterr() == ("", SAFE_ERROR)


def test_wrong_oauth_scope_rejected_before_get(ctx, capsys):
    token = json.loads(ctx.paths.token.read_text())
    token["granted_scopes"] = ["https://www.googleapis.com/auth/gmail.modify"]
    ctx.paths.token.write_text(json.dumps(token))
    ctx.paths.token.chmod(0o600)
    assert cli_review() == 2
    assert capsys.readouterr() == ("", SAFE_ERROR)
    assert ctx.messages.get_calls == [] and ctx.backend.decode_calls == 0


def test_learned_rule_selection_and_classification_match_unchanged_primitives(ctx):
    _, baseline = invoke(ctx)
    assert baseline[0].category_ids == ()
    from dam.models import MessageMetadata
    source = MessageMetadata.model_validate({"account_id": "gmail-account-unverified", "message_id": TARGET,
        "sender": "Example Alerts <alerts@updates.example.invalid>", "subject": "Synthetic unexplained update",
        "received_at": NOW, "label_ids": ("INBOX", "CATEGORY_UPDATES")})
    config = load_config(ctx.directory)
    candidate = propose_classification_rule(source, "promotions", config, as_of=NOW)
    save_classification_rule(candidate, config, ctx.learned, expected_fingerprint=candidate.fingerprint)
    before = ctx.learned.read_bytes()
    _, learned = invoke(ctx, learned_rules_file=str(ctx.learned))
    assert learned[0].category_ids == ("promotions",)
    assert learned[0].classification_basis_codes == ("human_accepted_learned_rule",)
    assert learned[0].classification_confidence == .90 and learned[0].category_teaching_required is False
    assert render_review_display(learned[0]) == ctx.expected[-1]
    _, default = invoke(ctx)
    assert default[0] == learned[0]  # CLI historically selects the default learned-rule file too.
    absent = ctx.learned.parent.parent.parent / "other" / ".config" / "dam" / "learned-rules.yaml"
    _, unlearned = invoke(ctx, learned_rules_file=str(absent))
    assert unlearned[0].category_ids == ()
    assert ctx.learned.read_bytes() == before
    assert all(not proposal.executable and not proposal.authority_established for proposal in ctx.proposals)


def test_original_overrides_and_ids_reach_service_without_cli_domain_access(ctx, monkeypatch, capsys):
    monkeypatch.setattr("dam.cli.review_gmail_message", forbidden)  # Still used only by Learn.
    monkeypatch.setattr("dam.cli.load_config", forbidden)
    monkeypatch.setattr("dam.cli._catalog_path", forbidden)
    assert cli_review(OTHER, "--learned-rules-file", str(ctx.learned),
                      "--category-catalog-file", str(ctx.catalog)) == 0
    assert capsys.readouterr() == (ctx.expected[-1], "")
    assert ctx.calls == [(OTHER, {"learned_rules_path": ctx.learned, "category_catalog_path": ctx.catalog})]
    assert len(ctx.messages.get_calls) == 1 and ctx.messages.get_calls[0]["id"] == OTHER


def test_cross_message_and_operation_isolation_and_domain_release(ctx):
    refs, outputs = [], []
    def sink(view):
        refs.append(weakref.ref(view))
        outputs.append(render_review_display(view))
    assert review_message(ReviewRequest(TARGET, gmail=True), present=sink).disposition is QueryDisposition.COMPLETED
    assert review_message(ReviewRequest(OTHER, gmail=True), present=sink).disposition is QueryDisposition.COMPLETED
    assert "Synthetic unexplained update" in outputs[0] and "Synthetic unexplained update" not in outputs[1]
    assert "alerts@updates.example.invalid" not in outputs[1]
    assert "other@other.example.invalid" not in outputs[0]
    category = render_categories(CategoryDetail("CAT-other", "private", "CATEGORY-SENTINEL", None, "active"))
    teaching = render_teaching(TeachingStatus("TEACH-SENTINEL", "completed", 0, 0, 0, "CONFIG-SENTINEL"))
    assert TARGET not in category + teaching and OTHER not in category + teaching
    assert all("CATEGORY-SENTINEL" not in out and "TEACH-SENTINEL" not in out for out in outputs)
    gc.collect()
    assert all(ref() is None for ref in refs + ctx.domain_refs)


def test_writer_interruption_releases_payload_and_does_not_retry(ctx):
    refs = []
    def sink(view):
        refs.append(weakref.ref(view))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        review_message(ReviewRequest(TARGET, gmail=True), present=sink)
    assert len(ctx.messages.get_calls) == ctx.backend.decode_calls == 1
    gc.collect()
    assert all(ref() is None for ref in refs + ctx.domain_refs)


@pytest.mark.parametrize("state", [True, False, None])
def test_historical_optional_provenance_and_teaching_wording(ctx, monkeypatch, state):
    expected = []
    def historical(message_id, **kwargs):
        result = review_gmail_message(message_id, paths=ctx.paths, backend=ctx.backend,
            service_factory=lambda *a, **k: Service(ctx.messages), config_directory=ctx.directory, as_of=NOW, **kwargs)
        result = replace(result, classification=replace(result.classification,
            classification_sources=None, review_reasons=None, category_teaching_required=state),
            proposal=result.proposal.model_copy(update={"review_reason_codes": None}))
        expected.append(render_review(result))
        return result
    monkeypatch.setattr("dam.review_operations.review_gmail_message", historical)
    result, views = invoke(ctx)
    assert result.disposition is QueryDisposition.COMPLETED
    assert render_review_display(views[0]) == expected[0]
    assert views[0].classification_basis_codes is None
    assert views[0].classification_review_codes is None and views[0].action_review_codes is None


@pytest.mark.parametrize("state", ["new_authorization", "refreshed"])
def test_existing_oauth_token_persistence_remains_readonly(ctx, state):
    attempts = []
    if state == "new_authorization":
        ctx.paths.token.unlink()
        def authorize(client, scopes):
            attempts.append(("authorize", scopes))
            return Credential()
        ctx.backend.authorize = authorize
    else:
        def decode(document):
            ctx.backend.decode_calls += 1
            return Credential(expired=True, valid=False, refresh_token="synthetic-refresh")
        def refresh(credential):
            attempts.append(("refresh", credential.scopes))
            return Credential()
        ctx.backend.decode_token = decode
        ctx.backend.refresh = refresh
    def encode(credential):
        return json.dumps({"access_token": "synthetic-new-token", "scopes": list(credential.scopes),
                           "granted_scopes": list(credential.granted_scopes)})
    ctx.backend.encode_token = encode
    result, views = invoke(ctx)
    assert result.disposition is QueryDisposition.COMPLETED
    assert attempts == [("authorize" if state == "new_authorization" else "refresh", (GMAIL_READONLY_SCOPE,))]
    token = json.loads(ctx.paths.token.read_text())
    assert token["access_token"] == "synthetic-new-token"
    assert token["scopes"] == token["granted_scopes"] == [GMAIL_READONLY_SCOPE]
    assert len(ctx.messages.get_calls) == 1 and not ctx.messages.list_calls
    assert "synthetic-new-token" not in render_review_display(views[0]) + repr(result)
    assert not ctx.learned.exists()
