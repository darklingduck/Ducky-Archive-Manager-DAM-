"""Injected, read-only Gmail response adapter; no OAuth or service construction.

Only messages.list and messages.get are used. A list/pagination failure aborts
the read; individual get failures are returned separately and must never be
classified as observed messages. Duplicate relevant headers fail that message
instead of guessing a sender/subject. An empty From is represented by an
explicitly set sender=None; absent From leaves that field unset, preserving the
distinction in MessageMetadata.model_fields_set without weakening its schema.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from dam.models import ConfigModel, MessageMetadata, NonBlankText, PositiveInt

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MAX_PAGE_SIZE = 500
MAX_PAGES = 1000
MAX_INTERNAL_DATE_MS = 4_102_444_800_000  # 2100-01-01 UTC, conservative ceiling.
LIST_FIELDS = "messages(id,threadId),nextPageToken,resultSizeEstimate"
GET_FIELDS = "id,threadId,internalDate,labelIds,payload/headers"
REQUIRED_HEADERS = ("From", "Subject")


class GmailAdapterError(RuntimeError):
    """Safe read context only; never wraps raw API payloads into messages."""

    def __init__(self, operation: Literal["list", "get", "normalize"], reason: str,
                 *, message_id: str | None = None, page_number: int | None = None):
        self.operation = operation
        self.reason = reason
        self.message_id = message_id
        self.page_number = page_number
        location = (f" message {message_id}" if message_id is not None else
                    f" page {page_number}" if page_number is not None else "")
        super().__init__(f"Gmail {operation}{location}: {reason}")


class MessageReadFailure(ConfigModel):
    message_id: NonBlankText
    reason: Literal["api_error", "malformed_response"]


class GmailReadResult(ConfigModel):
    """Discovery evidence only; never classification, approval, or execution."""

    account_id: NonBlankText
    requested_limit: PositiveInt
    messages: tuple[MessageMetadata, ...]
    failures: tuple[MessageReadFailure, ...]
    listed_message_ids: tuple[NonBlankText, ...]
    pages_read: int = Field(ge=0)
    result_size_estimate: int | None = Field(default=None, ge=0)
    listing_complete: bool
    stopped_at_limit: bool
    coverage: Literal["complete", "limit_reached", "read_failures", "limit_and_read_failures"]
    scope_label_ids: tuple[Literal["INBOX"], ...] = ("INBOX",)
    mode: Literal["read_only"] = "read_only"
    authority_established: Literal[False] = False
    executable: Literal[False] = False

    @property
    def observed_count(self) -> int:
        return len(self.messages)

    @property
    def listed_count(self) -> int:
        return len(self.listed_message_ids)

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.listed_count != self.observed_count + len(self.failures):
            raise ValueError("Listed IDs must resolve to one message or read failure each")
        if set(self.listed_message_ids) != {m.message_id for m in self.messages} | {f.message_id for f in self.failures}:
            raise ValueError("Read result IDs do not match listing")
        return self


@dataclass(frozen=True)
class _ListedMessage:
    message_id: str
    thread_id: str | None


def _opaque_id(value: Any, *, operation: Literal["list", "get", "normalize"],
               name: str, message_id: str | None = None, page_number: int | None = None) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or
            any(character.isspace() or ord(character) < 32 for character in value)):
        raise GmailAdapterError(operation, f"missing or malformed {name}",
                                message_id=message_id, page_number=page_number)
    return value


def _internal_date(value: Any, *, message_id: str) -> datetime:
    if type(value) is int:
        milliseconds = value
    elif isinstance(value, str) and len(value) <= 13 and value.isascii() and value.isdecimal():
        milliseconds = int(value)
    else:
        raise GmailAdapterError("normalize", "missing or malformed internalDate", message_id=message_id)
    if not 0 <= milliseconds <= MAX_INTERNAL_DATE_MS:
        raise GmailAdapterError("normalize", "internalDate outside supported range", message_id=message_id)
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)


def normalize_message(raw: Any, *, account_id: str, expected_id: str | None = None,
                      listed_thread_id: str | None = None) -> MessageMetadata:
    """Normalize one metadata-format get response; discard all other fields.

    This function never reads snippets, bodies, raw MIME, attachments or remote
    content. Caller-supplied response fields outside the allow-list are ignored.
    """
    if not isinstance(raw, dict):
        raise GmailAdapterError("normalize", "message response is not an object", message_id=expected_id)
    message_id = _opaque_id(raw.get("id"), operation="normalize", name="message ID", message_id=expected_id)
    if expected_id is not None and message_id != expected_id:
        raise GmailAdapterError("normalize", "message ID differs from listing", message_id=expected_id)
    thread = raw.get("threadId")
    if thread is not None:
        thread = _opaque_id(thread, operation="normalize", name="thread ID", message_id=message_id)
    if listed_thread_id is not None and thread is not None and thread != listed_thread_id:
        raise GmailAdapterError("normalize", "thread ID differs from listing", message_id=message_id)
    received_at = _internal_date(raw.get("internalDate"), message_id=message_id)
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
        raise GmailAdapterError("normalize", "missing or malformed metadata headers", message_id=message_id)
    relevant: dict[str, str] = {}
    for header in payload["headers"]:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("value"), str):
            raise GmailAdapterError("normalize", "malformed header entry", message_id=message_id)
        name = header["name"]
        value = header["value"]
        if not name or name != name.strip() or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise GmailAdapterError("normalize", "malformed header entry", message_id=message_id)
        canonical = name.casefold()
        if canonical in ("from", "subject"):
            if canonical in relevant:
                raise GmailAdapterError("normalize", "duplicate relevant header", message_id=message_id)
            relevant[canonical] = value
    values: dict[str, Any] = dict(account_id=account_id, message_id=message_id,
                                  received_at=received_at)
    if thread is not None:
        values["thread_id"] = thread
    if "from" in relevant:
        # Existing metadata contract rejects blank sender strings. Explicit
        # sender=None retains header-presence evidence in model_fields_set.
        values["sender"] = relevant["from"] if relevant["from"].strip() else None
    if "subject" in relevant:
        values["subject"] = relevant["subject"]
    if "labelIds" in raw:
        labels = raw["labelIds"]
        if not isinstance(labels, list) or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in labels):
            raise GmailAdapterError("normalize", "malformed labelIds", message_id=message_id)
        if len(labels) != len(set(labels)):
            raise GmailAdapterError("normalize", "duplicate labelIds", message_id=message_id)
        values["label_ids"] = tuple(sorted(labels))
    try:
        return MessageMetadata.model_validate(values)
    except ValidationError:
        raise GmailAdapterError("normalize", "metadata failed validation", message_id=message_id) from None


def read_inbox(service: Any, *, account_id: str, limit: int, max_pages: int = MAX_PAGES) -> GmailReadResult:
    """Read exact individual Inbox IDs through an injected Gmail-like service.

    Listing/pagination failures abort with safe context. Per-message get or
    normalization failures remain explicit; callers must review those IDs.
    No retries, OAuth, service construction or mutation methods exist here.
    """
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be nonblank")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")
    try:
        api = service.users().messages()
    except Exception:
        raise GmailAdapterError("list", "read interface unavailable", page_number=1) from None
    listed: list[_ListedMessage] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    page_number = 0
    listing_complete = False
    result_size_estimate: int | None = None
    while len(listed) < limit:
        page_number += 1
        if page_number > max_pages:
            raise GmailAdapterError("list", "pagination page limit exceeded", page_number=page_number)
        arguments: dict[str, Any] = dict(userId="me", labelIds=["INBOX"],
            includeSpamTrash=False, maxResults=min(MAX_PAGE_SIZE, limit - len(listed)), fields=LIST_FIELDS)
        if page_token is not None:
            arguments["pageToken"] = page_token
        try:
            page = api.list(**arguments).execute()
        except Exception:
            raise GmailAdapterError("list", "read request failed", page_number=page_number) from None
        if not isinstance(page, dict):
            raise GmailAdapterError("list", "page response is not an object", page_number=page_number)
        entries = page.get("messages", [])
        if not isinstance(entries, list) or len(entries) > arguments["maxResults"]:
            raise GmailAdapterError("list", "malformed message list", page_number=page_number)
        estimate = page.get("resultSizeEstimate")
        if estimate is not None:
            if type(estimate) is not int or estimate < 0:
                raise GmailAdapterError("list", "malformed result estimate", page_number=page_number)
            result_size_estimate = estimate
        for entry in entries:
            if not isinstance(entry, dict):
                raise GmailAdapterError("list", "malformed message reference", page_number=page_number)
            message_id = _opaque_id(entry.get("id"), operation="list", name="message ID", page_number=page_number)
            thread = entry.get("threadId")
            if thread is not None:
                thread = _opaque_id(thread, operation="list", name="thread ID", page_number=page_number)
            if message_id in seen_ids:
                raise GmailAdapterError("list", "duplicate message ID across pages", page_number=page_number)
            seen_ids.add(message_id)
            listed.append(_ListedMessage(message_id, thread))
        next_token = page.get("nextPageToken")
        if next_token is None:
            listing_complete = True
            break
        if not isinstance(next_token, str) or not next_token.strip() or next_token in seen_tokens:
            raise GmailAdapterError("list", "invalid or repeated pagination token", page_number=page_number)
        seen_tokens.add(next_token)
        page_token = next_token
    messages: list[MessageMetadata] = []
    failures: list[MessageReadFailure] = []
    for ref in listed:
        try:
            raw = api.get(userId="me", id=ref.message_id, format="metadata",
                          metadataHeaders=list(REQUIRED_HEADERS), fields=GET_FIELDS).execute()
        except Exception:
            failures.append(MessageReadFailure(message_id=ref.message_id, reason="api_error"))
            continue
        try:
            messages.append(normalize_message(raw, account_id=account_id,
                expected_id=ref.message_id, listed_thread_id=ref.thread_id))
        except GmailAdapterError:
            failures.append(MessageReadFailure(message_id=ref.message_id, reason="malformed_response"))
    stopped_at_limit = len(listed) == limit and not listing_complete
    coverage = ("limit_and_read_failures" if stopped_at_limit and failures else
                "limit_reached" if stopped_at_limit else
                "read_failures" if failures else "complete")
    return GmailReadResult(account_id=account_id, requested_limit=limit,
        messages=tuple(messages), failures=tuple(failures),
        listed_message_ids=tuple(ref.message_id for ref in listed),
        pages_read=page_number, result_size_estimate=result_size_estimate,
        listing_complete=listing_complete, stopped_at_limit=stopped_at_limit,
        coverage=coverage)
