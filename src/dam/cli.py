"""Small argparse presentation layer; synthetic by default, Gmail explicit."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from dam.categories import (
    CategoryError, default_catalog_path,
)
from dam.category_operations import CategoryOperation, CategoriesRequest, operate_categories
from dam.category_presentation import CategoryDiagnostic, CategoryPresentation, render_categories
from dam.config import ConfigurationError, load_config
from dam.learning import LearningError
from dam.learn_operations import LearnRequest, learn
from dam.learn_presentation import LearnDiagnostic, LearnPresentation, render_learn
from dam.review_operations import ReviewRequest, review_message
from dam.review_presentation import ReviewDiagnostic, ReviewPresentation, render_review_display
from dam.scan import (
    MAX_INITIAL_GMAIL_LIMIT, MAX_SCAN_LIMIT, default_config_directory,
)
from dam.scan_operations import ScanRequest, scan
from dam.scan_presentation import ScanDiagnostic, ScanPresentation, render_scan
from dam.storage import StorageError
from dam.teaching import TeachingError
from dam.teaching_queries import QueryDisposition, TeachingQuery, query_teaching, preview_teaching
from dam.teaching_presentation import TeachingPresentation, render_teaching
from dam.teaching_save import SaveTeaching, save_teaching
from dam.teaching_resume import ResumeTeaching, resume_teaching


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
    scan.add_argument("--record-locally", action="store_true",
                      help="With --gmail, explicitly store private read-only ITEM, observation, and Classification Queue state in local SQLite.")
    scan.add_argument("--use-learned-rules", action="store_true",
                      help="Opt in to privately saved classification rules; grants no action authority.")
    scan.add_argument("--category-catalog-file", metavar="PRIVATE_PATH",
                      help="Private category catalog path; mainly for isolated local testing.")
    review = commands.add_parser("review", help="Explicitly inspect one Gmail Inbox message for human review.",
                                 description="With --gmail, fetch exactly one named Inbox message in metadata format. No mailbox action or rule save occurs.")
    review.add_argument("--gmail", action="store_true",
                        help="Required for this command; explicitly permits one read-only Gmail metadata get.")
    review.add_argument("--message-id", required=True, help="Exact individual Gmail message ID to review.")
    review.add_argument("--learned-rules-file", metavar="PRIVATE_PATH",
                        help="Private learned-rule path; mainly for isolated local testing.")
    review.add_argument("--category-catalog-file", metavar="PRIVATE_PATH")
    learn = commands.add_parser("learn", help="Preview or explicitly save a human classification rule.",
                                description="Synthetic metadata by default. --gmail explicitly reads one named real Inbox message. Preview is read-only; --save requires its fingerprint. No mailbox action occurs.")
    learn.add_argument("--gmail", action="store_true",
                       help="Explicitly read one real Gmail Inbox message for classification teaching.")
    learn.add_argument("--message-id", required=True, help="Exact individual synthetic or Gmail message ID.")
    learn.add_argument("--category", required=True, help="Existing category ID selected by the human.")
    learn.add_argument("--save", action="store_true", help="Explicitly save the reviewed classification-only rule.")
    learn.add_argument("--confirm-fingerprint", metavar="SHA256",
                       help="Required with --save; binds the save to the reviewed candidate.")
    learn.add_argument("--learned-rules-file", metavar="PRIVATE_PATH",
                       help="Private ~/.config/dam/learned-rules.yaml path; mainly for isolated local testing.")
    learn.add_argument("--category-catalog-file", metavar="PRIVATE_PATH")
    categories = commands.add_parser("categories", help="List and manage local DAM categories.",
                                    description="Discover categories or preview/save private local taxonomy changes; no Gmail or OAuth access.")
    categories.add_argument("--catalog-file", metavar="PRIVATE_PATH",
                            help="Private catalog path; mainly for isolated local testing.")
    categories.add_argument("--learned-rules-file", metavar="PRIVATE_PATH",
                            help="Private learned-rule path for retirement reference checks.")
    changes = categories.add_subparsers(dest="category_command")
    show = changes.add_parser("show", help="Show category details and permanent identity.")
    show.add_argument("category")
    for operation in ("add", "rename", "move", "retire"):
        item = changes.add_parser(operation, help=f"Preview or explicitly save category {operation}.")
        if operation == "add":
            item.add_argument("--key", required=True)
            item.add_argument("--name", required=True)
            item.add_argument("--parent")
            item.add_argument("--confirm-id", help="DAM-generated ID copied from the add preview; required to save.")
        else:
            item.add_argument("category")
            if operation == "rename":
                item.add_argument("--name", required=True)
            if operation == "move":
                item.add_argument("--parent", help="Parent key or permanent ID; omit to move to root.")
            if operation == "retire":
                item.add_argument("--confirm-retire", help="Permanent ID copied from the retirement preview.")
        item.add_argument("--save", action="store_true")
        item.add_argument("--confirm-fingerprint", metavar="SHA256")
    teach = commands.add_parser("teach", help="Inspect and teach durable Classification Queue work offline.")
    teach.add_argument("--learned-rules-file", metavar="PRIVATE_PATH")
    teach.add_argument("--category-catalog-file", metavar="PRIVATE_PATH")
    teaching = teach.add_subparsers(dest="teach_command", required=True)
    teaching.add_parser("list", help="List unresolved and deferred teaching work.")
    detail = teaching.add_parser("show", help="Inspect persisted evidence for one work item.")
    detail.add_argument("work_id")
    for operation in ("preview", "save"):
        entry = teaching.add_parser(operation, help=f"{operation.title()} offline classification teaching.")
        entry.add_argument("work_id")
        entry.add_argument("--category", required=True)
        entry.add_argument("--item-id")
        if operation == "save":
            entry.add_argument("--confirm-fingerprint", required=True)
    for operation in ("resume", "status"):
        entry = teaching.add_parser(operation, help=f"{operation.title()} a durable teaching operation.")
        entry.add_argument("teaching_id")
    return root


def _catalog_path(override: str | None) -> Path | None:
    path = Path(override) if override else default_catalog_path()
    return path if override or path.exists() else None


def _write_teaching(view: TeachingPresentation) -> None:
    """Terminal adapter: render only the explicitly supplied display contract."""
    print(render_teaching(view), end="")


def _write_categories(view: CategoryPresentation) -> None:
    """Terminal delivery of detached category output and safe diagnostics only."""
    print(render_categories(view), end="", file=sys.stderr if type(view) is CategoryDiagnostic else sys.stdout)


def _write_review(view: ReviewPresentation) -> None:
    """Terminal adapter for explicit Review display and sanitized diagnostics."""
    print(render_review_display(view), end="", file=sys.stderr if type(view) is ReviewDiagnostic else sys.stdout)


def _write_learn(view: LearnPresentation) -> None:
    """Terminal delivery of detached Learn output and fixed safe diagnostics."""
    print(render_learn(view), end="", file=sys.stderr if type(view) is LearnDiagnostic else sys.stdout)


def _write_scan(view: ScanPresentation) -> None:
    """Terminal delivery of detached Scan display and fixed safe diagnostics."""
    print(render_scan(view), end="", file=sys.stderr if type(view) is ScanDiagnostic else sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "teach":
        try:
            base = load_config(default_config_directory())
            if args.teach_command in ("list", "show", "status", "preview"):
                if args.teach_command == "preview":
                    result = preview_teaching(settings=base.settings, work_id=args.work_id,
                        category_selector=args.category, item_id=args.item_id,
                        learned_rules_file=args.learned_rules_file,
                        category_catalog_file=args.category_catalog_file, present=_write_teaching)
                else:
                    result = query_teaching(TeachingQuery(args.teach_command), settings=base.settings,
                        target_id=(args.work_id if args.teach_command == "show" else
                                   args.teaching_id if args.teach_command == "status" else None),
                        learned_rules_path=Path(args.learned_rules_file) if args.learned_rules_file else None,
                        category_catalog_path=Path(args.category_catalog_file) if args.category_catalog_file else _catalog_path(None),
                        present=_write_teaching)
                if result.disposition is QueryDisposition.COMPLETED:
                    return 0
                if result.disposition is QueryDisposition.FAILED:
                    print("DAM teach failed internally.", file=sys.stderr)
                    return 1
                print("DAM teach failed or remains pending; inspect the teaching operation and retry safely.", file=sys.stderr)
                return 2
            if args.teach_command in ("save", "resume"):
                if args.teach_command == "resume":
                    result = resume_teaching(ResumeTeaching(args.teaching_id,
                        args.learned_rules_file, args.category_catalog_file),
                        settings=base.settings, present=_write_teaching)
                else:
                    result = save_teaching(SaveTeaching(args.work_id, args.category,
                        args.confirm_fingerprint, args.item_id, args.learned_rules_file,
                        args.category_catalog_file), settings=base.settings, present=_write_teaching)
                if result.presentation_failed:
                    print("DAM teaching outcome could not be displayed; inspect teaching status before retrying.",
                          file=sys.stderr)
                    return 1
                if result.disposition is QueryDisposition.COMPLETED:
                    return 0
                if result.disposition is QueryDisposition.INCOMPLETE:
                    return 2
                if result.disposition is QueryDisposition.FAILED:
                    print("DAM teach failed internally; inspect durable teaching status before retrying.", file=sys.stderr)
                    return 1
                print("DAM teach failed or remains pending; inspect the teaching operation and retry safely.", file=sys.stderr)
                return 2
        except (TeachingError, StorageError, LearningError, CategoryError, ConfigurationError, ValueError, OSError):
            print("DAM teach failed or remains pending; inspect the teaching operation and retry safely.", file=sys.stderr)
            return 2
        except Exception:
            print("DAM teach failed internally.", file=sys.stderr)
            return 1
    if args.command == "categories":
        result = operate_categories(CategoriesRequest(
            CategoryOperation(args.category_command or "list"),
            selector=getattr(args, "category", None), key=getattr(args, "key", None),
            name=getattr(args, "name", None), parent=getattr(args, "parent", None),
            save=getattr(args, "save", False), confirm_fingerprint=getattr(args, "confirm_fingerprint", None),
            confirm_id=getattr(args, "confirm_id", None), confirm_retire=getattr(args, "confirm_retire", None),
            catalog_file=args.catalog_file, learned_rules_file=args.learned_rules_file), present=_write_categories)
        if result.presentation_failed:
            print("DAM categories outcome could not be displayed; inspect the category catalog before retrying.",
                  file=sys.stderr)
            return 1
        if result.disposition is QueryDisposition.COMPLETED:
            return 0
        return 1 if result.disposition is QueryDisposition.FAILED else 2
    if args.command == "scan":
        result = scan(ScanRequest(args.gmail, args.limit, args.record_locally, args.dry_run,
            args.use_learned_rules, args.category_catalog_file), present=_write_scan)
        if result.presentation_failed:
            return 1
        if result.disposition is QueryDisposition.COMPLETED:
            return 0
        return 1 if result.disposition is QueryDisposition.FAILED else 2
    if args.command == "learn":
        result = learn(LearnRequest(args.message_id, args.category, args.gmail, args.save,
            args.confirm_fingerprint, args.learned_rules_file, args.category_catalog_file), present=_write_learn)
        disposition = result.presentation_failure or result.disposition
        if disposition is QueryDisposition.COMPLETED:
            return 0
        return 1 if disposition is QueryDisposition.FAILED else 2
    if args.command == "review":
        result = review_message(ReviewRequest(args.message_id, args.gmail,
            args.learned_rules_file, args.category_catalog_file), present=_write_review)
        if result.disposition is QueryDisposition.COMPLETED:
            return 0
        return 1 if result.disposition is QueryDisposition.FAILED else 2
    return 2
