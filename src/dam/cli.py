"""Small argparse presentation layer; synthetic by default, Gmail explicit."""

import argparse
from collections.abc import Sequence
import sys

from dam.audit import render_preview
from dam.auth import AuthError
from dam.config import ConfigurationError
from dam.gmail import GmailAdapterError
from dam.scan import (
    MAX_INITIAL_GMAIL_LIMIT, MAX_SCAN_LIMIT, ScanInputError,
    run_gmail_scan, run_synthetic_scan,
)


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from None
    if number < 1 or number > MAX_SCAN_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be from 1 to {MAX_SCAN_LIMIT}")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="dam", description="Ducky Archive Manager (DAM): dry-run scans with no Gmail mailbox writes.")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Preview synthetic Inbox data (default) or explicitly read Gmail.",
                               description="Synthetic demo by default. --gmail explicitly selects a real, read-only Gmail Inbox scan. Neither mode executes mailbox actions.")
    scan.add_argument("--limit", type=_limit, default=None, metavar="N",
                      help=f"Maximum individual messages (synthetic: settings default, up to {MAX_SCAN_LIMIT}; Gmail: default {MAX_INITIAL_GMAIL_LIMIT}, maximum {MAX_INITIAL_GMAIL_LIMIT}).")
    scan.add_argument("--gmail", action="store_true",
                      help="Explicitly authenticate and read only Gmail Inbox metadata; may open OAuth authorization if no token exists.")
    scan.add_argument("--dry-run", action="store_true",
                      help="Explicit dry-run flag; both modes remain non-executing without it.")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "scan":
        if args.gmail:
            if args.limit is not None and args.limit > MAX_INITIAL_GMAIL_LIMIT:
                print(f"DAM Gmail mode limit must be from 1 to {MAX_INITIAL_GMAIL_LIMIT}; no Gmail access attempted.", file=sys.stderr)
                return 2
            try:
                result = run_gmail_scan(limit=args.limit)
            except ScanInputError as error:
                print(f"DAM Gmail scan rejected: {error}; no mailbox actions executed.", file=sys.stderr)
                return 2
            except (AuthError, GmailAdapterError, ConfigurationError, ValueError, OSError):
                print("DAM Gmail read-only scan failed; no mailbox actions executed.", file=sys.stderr)
                return 2
            except Exception:
                print("DAM Gmail scan failed internally; no mailbox actions executed.", file=sys.stderr)
                return 1
            print("DAM real Gmail read-only Inbox scan; no mailbox actions executed.")
            print(render_preview(result.preview), end="")
            read = result.read_result
            print(f"Gmail listing: {read.listed_count} IDs; normalized: {read.observed_count}; "
                  f"read failures: {len(read.failures)}; coverage: {read.coverage}.")
            for failure in read.failures:
                print(f"Uninspected message {failure.message_id}: {failure.reason}; Review required.")
            return 0
        try:
            result = run_synthetic_scan(limit=args.limit)
        except (ConfigurationError, ScanInputError, ValueError, OSError):
            print("DAM scan failed: invalid configuration or synthetic input.", file=sys.stderr)
            return 2
        except Exception:
            print("DAM scan failed: internal error; no mailbox actions were executed.", file=sys.stderr)
            return 1
        print(render_preview(result.preview), end="")
        return 0
    return 2
