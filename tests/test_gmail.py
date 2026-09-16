"""Purpose-built read-only Gmail fakes; no credentials, network or files."""

from datetime import datetime, timezone
import socket
import sqlite3
import subprocess
import sys

import pytest

from dam.gmail import (
    GET_FIELDS, GMAIL_READONLY_SCOPE, GmailAdapterError, LIST_FIELDS,
    normalize_message, read_inbox,
)


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeMessages:
    """Only the two read operations exist; mutation names are absent."""

    def __init__(self, pages, detail):
        self.pages = pages
        self.detail = detail
        self.list_calls = []
        self.get_calls = []

    def list(self, **arguments):
        self.list_calls.append(arguments)
        return FakeRequest(self.pages[arguments.get("pageToken")])

    def get(self, **arguments):
        self.get_calls.append(arguments)
        return FakeRequest(self.detail[arguments["id"]])


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, pages, detail):
        self.messages_api = FakeMessages(pages, detail)

    def users(self):
        return FakeUsers(self.messages_api)


def reference(message_id, thread="shared-thread"):
    return {"id": message_id, "threadId": thread}


def detail(message_id, *, thread="shared-thread", timestamp="1789550400123",
           headers=None, labels=None):
    return {"id": message_id, "threadId": thread, "internalDate": timestamp,
            "payload": {"headers": headers if headers is not None else [
                {"name": "From", "value": "Example <news@example.invalid>"},
                {"name": "Subject", "value": "Synthetic notice"}]},
            "labelIds": labels if labels is not None else ["INBOX", "UNREAD"]}


def service_for(*ids):
    return FakeService({None: {"messages": [reference(mid) for mid in ids]}},
                       {mid: detail(mid) for mid in ids})


def test_readonly_scope_and_import_side_effects(tmp_path):
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    code = """
import sys
def guard(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.__new__', 'os.mkdir'):
        raise AssertionError(event)
    if event == 'open' and (args[2] & (64 | 512 | 1 | 2)):
        raise AssertionError('file write')
sys.addaudithook(guard)
import dam.gmail
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_one_page_metadata_request_and_normalization():
    service = service_for("opaque-A")
    result = read_inbox(service, account_id="synthetic-account", limit=100)
    assert result.listed_message_ids == ("opaque-A",)
    assert result.observed_count == 1 and result.listed_count == 1
    assert result.pages_read == 1 and result.listing_complete
    assert not result.stopped_at_limit and result.coverage == "complete"
    assert not result.authority_established and not result.executable
    assert service.messages_api.list_calls == [{
        "userId": "me", "labelIds": ["INBOX"], "includeSpamTrash": False,
        "maxResults": 100, "fields": LIST_FIELDS}]
    assert service.messages_api.get_calls == [{
        "userId": "me", "id": "opaque-A", "format": "metadata",
        "metadataHeaders": ["From", "Subject"], "fields": GET_FIELDS}]
    assert "body" not in GET_FIELDS and "raw" not in GET_FIELDS
    assert "snippet" not in GET_FIELDS and "attachment" not in GET_FIELDS
    assert result.messages[0].received_at == datetime(2026, 9, 16, 9, 20, 0, 123000, tzinfo=timezone.utc)
    assert result.messages[0].label_ids == ("INBOX", "UNREAD")


def test_multiple_pages_same_thread_stays_two_individual_messages():
    service = FakeService({
        None: {"messages": [reference("opaque-A")], "nextPageToken": "page-two", "resultSizeEstimate": 2},
        "page-two": {"messages": [reference("opaque-B")], "resultSizeEstimate": 2},
    }, {"opaque-A": detail("opaque-A"), "opaque-B": detail("opaque-B")})
    result = read_inbox(service, account_id="synthetic-account", limit=10)
    assert result.pages_read == 2 and result.listing_complete
    assert result.result_size_estimate == 2
    assert result.listed_message_ids == ("opaque-A", "opaque-B")
    assert tuple(m.message_id for m in result.messages) == ("opaque-A", "opaque-B")
    assert result.messages[0].thread_id == result.messages[1].thread_id == "shared-thread"
    assert service.messages_api.list_calls[1]["pageToken"] == "page-two"


def test_limit_mid_pagination_stops_before_next_page():
    service = FakeService({None: {"messages": [reference("A"), reference("B")],
                                  "nextPageToken": "unused"}},
                          {"A": detail("A"), "B": detail("B")})
    result = read_inbox(service, account_id="synthetic-account", limit=2)
    assert result.listed_count == 2 and result.pages_read == 1
    assert result.stopped_at_limit and not result.listing_complete
    assert result.coverage == "limit_reached"
    assert len(service.messages_api.list_calls) == 1


def test_page_size_is_capped_at_gmail_maximum():
    service = FakeService({None: {}}, {})
    read_inbox(service, account_id="synthetic-account", limit=600)
    assert service.messages_api.list_calls[0]["maxResults"] == 500


@pytest.mark.parametrize("page", [{}, {"messages": []}])
def test_empty_or_missing_messages_list_is_empty_result(page):
    result = read_inbox(FakeService({None: page}, {}), account_id="synthetic-account", limit=3)
    assert result.messages == () and result.failures == ()
    assert result.listing_complete and result.coverage == "complete"


def test_empty_page_with_new_token_can_continue():
    service = FakeService({None: {"messages": [], "nextPageToken": "next"},
                           "next": {"messages": [reference("A")]}}, {"A": detail("A")})
    result = read_inbox(service, account_id="synthetic-account", limit=2)
    assert result.pages_read == 2 and result.listed_message_ids == ("A",)


@pytest.mark.parametrize("page", [[], {"messages": None}, {"messages": "bad"},
                                        {"messages": [{}]}, {"resultSizeEstimate": "many"}])
def test_malformed_listing_aborts_without_inventing_messages(page):
    with pytest.raises(GmailAdapterError) as error:
        read_inbox(FakeService({None: page}, {}), account_id="synthetic-account", limit=2)
    assert error.value.operation == "list" and error.value.page_number == 1


@pytest.mark.parametrize("token", ["first", "", 123])
def test_repeated_or_invalid_pagination_token_fails_safely(token):
    pages = {None: {"messages": [], "nextPageToken": token}}
    if token == "first":
        pages["first"] = {"messages": [], "nextPageToken": "first"}
    service = FakeService(pages, {})
    with pytest.raises(GmailAdapterError, match="pagination token") as error:
        read_inbox(service, account_id="synthetic-account", limit=2)
    assert error.value.operation == "list"


def test_unique_empty_tokens_are_bounded_by_page_limit():
    service = FakeService({None: {"messages": [], "nextPageToken": "one"},
                           "one": {"messages": [], "nextPageToken": "two"}}, {})
    with pytest.raises(GmailAdapterError, match="page limit exceeded"):
        read_inbox(service, account_id="synthetic-account", limit=2, max_pages=2)


def test_absent_and_empty_from_subject_remain_distinct():
    absent = normalize_message(detail("A", headers=[]), account_id="synthetic-account")
    empty = normalize_message(detail("B", headers=[
        {"name": "fRoM", "value": ""}, {"name": "sUbJeCt", "value": ""}]),
        account_id="synthetic-account")
    assert absent.sender is None and "sender" not in absent.model_fields_set
    assert empty.sender is None and "sender" in empty.model_fields_set
    assert absent.subject is None and "subject" not in absent.model_fields_set
    assert empty.subject == "" and "subject" in empty.model_fields_set


def test_case_insensitive_header_names_and_original_from_value():
    raw = detail("A", headers=[{"name": "fRoM", "value": "Example Person <person@example.invalid>"},
                               {"name": "subject", "value": "Synthetic Case"}])
    message = normalize_message(raw, account_id="synthetic-account")
    assert message.sender == "Example Person <person@example.invalid>"
    assert message.subject == "Synthetic Case"


def test_duplicate_relevant_header_fails_instead_of_concatenating():
    raw = detail("A", headers=[{"name": "From", "value": "a@example.invalid"},
                               {"name": "FROM", "value": "b@example.invalid"}])
    with pytest.raises(GmailAdapterError, match="duplicate relevant header"):
        normalize_message(raw, account_id="synthetic-account")


@pytest.mark.parametrize("timestamp", [None, "bad", "-1", -1, True, 4_102_444_800_001, "9" * 100])
def test_missing_or_malformed_internal_date_fails(timestamp):
    raw = detail("A", timestamp=timestamp)
    with pytest.raises(GmailAdapterError, match="internalDate"):
        normalize_message(raw, account_id="synthetic-account")


def test_integer_milliseconds_and_missing_thread_id():
    raw = detail("opaque-not-uuid", timestamp=1_000, thread=None)
    raw.pop("threadId")
    message = normalize_message(raw, account_id="synthetic-account")
    assert message.message_id == "opaque-not-uuid"
    assert message.received_at == datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    assert message.thread_id is None


@pytest.mark.parametrize("change", [
    {"id": None}, {"id": 123}, {"payload": None}, {"payload": []},
    {"payload": {}}, {"payload": {"headers": {}}},
    {"payload": {"headers": [None]}},
    {"payload": {"headers": [{"name": "From"}]}},
    {"labelIds": "INBOX"}, {"labelIds": ["INBOX", 42]},
    {"labelIds": ["INBOX", "INBOX"]},
])
def test_malformed_get_data_fails_without_raw_payload(change):
    raw = detail("A") | change
    with pytest.raises(GmailAdapterError) as error:
        normalize_message(raw, account_id="synthetic-account")
    assert "news@example.invalid" not in str(error.value)


def test_missing_and_empty_labels_distinguished():
    missing = detail("A")
    missing.pop("labelIds")
    empty = detail("B", labels=[])
    first = normalize_message(missing, account_id="synthetic-account")
    second = normalize_message(empty, account_id="synthetic-account")
    assert first.label_ids == second.label_ids == ()
    assert "label_ids" not in first.model_fields_set
    assert "label_ids" in second.model_fields_set


def test_extra_raw_body_and_snippet_fields_are_discarded():
    raw = detail("A") | {"raw": "private raw", "snippet": "private snippet"}
    raw["payload"]["body"] = {"data": "private body"}
    raw["payload"]["parts"] = [{"body": {"data": "private attachment"}}]
    message = normalize_message(raw, account_id="synthetic-account")
    serialized = message.model_dump_json()
    assert all(secret not in serialized for secret in (
        "private raw", "private snippet", "private body", "private attachment"))


def test_individual_api_and_malformed_get_failures_are_separate_from_observed():
    service = FakeService({None: {"messages": [reference("A"), reference("B"), reference("C")]}},
                          {"A": detail("A"), "B": RuntimeError("secret access token"),
                           "C": {"id": "C", "payload": "bad"}})
    result = read_inbox(service, account_id="synthetic-account", limit=10)
    assert result.listed_count == 3 and result.observed_count == 1
    assert tuple((f.message_id, f.reason) for f in result.failures) == (
        ("B", "api_error"), ("C", "malformed_response"))
    assert result.coverage == "read_failures" and result.listing_complete
    assert "secret access token" not in repr(result)


def test_get_identity_mismatch_becomes_read_failure():
    service = FakeService({None: {"messages": [reference("A")]}},
                          {"A": detail("B")})
    result = read_inbox(service, account_id="synthetic-account", limit=2)
    assert result.messages == ()
    assert result.failures[0].message_id == "A"
    assert result.failures[0].reason == "malformed_response"


def test_limit_and_read_failure_are_both_reported():
    service = FakeService({None: {"messages": [reference("A")], "nextPageToken": "later"}},
                          {"A": RuntimeError("private payload")})
    result = read_inbox(service, account_id="synthetic-account", limit=1)
    assert result.coverage == "limit_and_read_failures"
    assert result.stopped_at_limit and not result.listing_complete


def test_list_failure_aborts_with_safe_page_context():
    service = FakeService({None: RuntimeError("secret authorization header")}, {})
    with pytest.raises(GmailAdapterError) as error:
        read_inbox(service, account_id="synthetic-account", limit=2)
    assert error.value.operation == "list" and error.value.page_number == 1
    assert "secret authorization header" not in str(error.value)


def test_no_network_oauth_or_sqlite_and_no_mutation_surface(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network or SQLite access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    service = service_for("A")
    assert read_inbox(service, account_id="synthetic-account", limit=1).observed_count == 1
    for name in ("modify", "batchModify", "trash", "untrash", "delete", "batchDelete",
                 "send", "insert", "import_", "create"):
        assert not hasattr(service.messages_api, name)


def test_existing_cli_stays_packaged_synthetic_demo():
    result = subprocess.run([sys.executable, "-m", "dam", "scan", "--limit", "2", "--dry-run"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "synthetic-account" in result.stdout
    assert "Observed: 2" in result.stdout
    assert "Executed Gmail actions: 0" in result.stdout
