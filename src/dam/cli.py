"""Small argparse presentation layer for the synthetic read-only scan."""

import argparse
from collections.abc import Sequence
import sys

from dam.audit import render_preview
from dam.config import ConfigurationError
from dam.scan import MAX_SCAN_LIMIT, ScanInputError, run_synthetic_scan


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
        prog="dam", description="Ducky Archive Manager (DAM): synthetic, read-only Milestone 1 demo.")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Preview a synthetic Inbox scan; no Gmail access or writes.",
                               description="Scan packaged synthetic messages in read-only dry-run mode. No Gmail actions are executed.")
    scan.add_argument("--limit", type=_limit, default=None, metavar="N",
                      help=f"Maximum individual messages to process (default from settings; range 1–{MAX_SCAN_LIMIT}).")
    scan.add_argument("--dry-run", action="store_true",
                      help="Explicitly request dry-run output; scanning is read-only even without this flag.")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "scan":
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
