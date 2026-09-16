"""Small argparse presentation layer; synthetic by default, Gmail explicit."""

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
import sys

from dam.audit import render_preview
from dam.auth import AuthError
from dam.config import ConfigurationError, load_config
from dam.gmail import GmailAdapterError
from dam.learning import (
    LearningError, configuration_with_learned_rules, default_learned_rules_path,
    propose_classification_rule, render_candidate, save_classification_rule,
)
from dam.scan import (
    MAX_INITIAL_GMAIL_LIMIT, MAX_SCAN_LIMIT, ScanInputError,
    default_config_directory, load_synthetic_messages, run_gmail_scan, run_synthetic_scan,
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
    scan.add_argument("--use-learned-rules", action="store_true",
                      help="Opt in to privately saved classification rules; grants no action authority.")
    learn = commands.add_parser("learn", help="Preview or explicitly save a synthetic classification rule.",
                                description="Teach a category from packaged synthetic metadata only. Preview is read-only; --save requires the displayed fingerprint. No mailbox action occurs.")
    learn.add_argument("--message-id", required=True, help="Individual synthetic fixture message ID.")
    learn.add_argument("--category", required=True, help="Existing category ID selected by the human.")
    learn.add_argument("--save", action="store_true", help="Explicitly save the reviewed classification-only rule.")
    learn.add_argument("--confirm-fingerprint", metavar="SHA256",
                       help="Required with --save; binds the save to the reviewed candidate.")
    learn.add_argument("--learned-rules-file", metavar="PRIVATE_PATH",
                       help="Private ~/.config/dam/learned-rules.yaml path; mainly for isolated local testing.")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "scan":
        learned = default_learned_rules_path() if args.use_learned_rules else None
        if args.gmail:
            if args.limit is not None and args.limit > MAX_INITIAL_GMAIL_LIMIT:
                print(f"DAM Gmail mode limit must be from 1 to {MAX_INITIAL_GMAIL_LIMIT}; no Gmail access attempted.", file=sys.stderr)
                return 2
            try:
                if learned is None:
                    result = run_gmail_scan(limit=args.limit)
                else:
                    result = run_gmail_scan(limit=args.limit, learned_rules_path=learned)
            except ScanInputError as error:
                print(f"DAM Gmail scan rejected: {error}; no mailbox actions executed.", file=sys.stderr)
                return 2
            except (AuthError, GmailAdapterError, ConfigurationError, LearningError, ValueError, OSError):
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
            if learned is None:
                result = run_synthetic_scan(limit=args.limit)
            else:
                result = run_synthetic_scan(limit=args.limit, learned_rules_path=learned)
        except (ConfigurationError, LearningError, ScanInputError, ValueError, OSError):
            print("DAM scan failed: invalid configuration or synthetic input.", file=sys.stderr)
            return 2
        except Exception:
            print("DAM scan failed: internal error; no mailbox actions were executed.", file=sys.stderr)
            return 1
        print(render_preview(result.preview), end="")
        return 0
    if args.command == "learn":
        if args.save != (args.confirm_fingerprint is not None):
            print("DAM learn: --save requires --confirm-fingerprint, and confirmation requires --save.",
                  file=sys.stderr)
            return 2
        try:
            messages = load_synthetic_messages()
            source = next((item for item in messages if item.message_id == args.message_id), None)
            if source is None:
                raise LearningError("unknown_synthetic_message")
            path = (default_learned_rules_path() if args.learned_rules_file is None
                    else Path(args.learned_rules_file))
            config = configuration_with_learned_rules(load_config(default_config_directory()), path)
            candidate = propose_classification_rule(
                source, args.category, config, as_of=datetime.now(timezone.utc),
                sample=tuple(item for item in messages if item.message_id != source.message_id))
            if args.save:
                outcome = save_classification_rule(
                    candidate, config, path, expected_fingerprint=args.confirm_fingerprint)
                print(render_candidate(candidate, status=outcome.status), end="")
            else:
                print(render_candidate(candidate), end="")
                print("To save, rerun with --save --confirm-fingerprint <candidate fingerprint>.")
            return 0
        except (LearningError, ConfigurationError, ScanInputError, OSError, ValueError):
            print("DAM learn rejected the classification input or private rule file; no mailbox actions executed.",
                  file=sys.stderr)
            return 2
        except Exception:
            print("DAM learn failed internally; no mailbox actions executed.", file=sys.stderr)
            return 1
    return 2
