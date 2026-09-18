"""Small argparse presentation layer; synthetic by default, Gmail explicit."""

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
import shlex
import sys

from dam.audit import render_preview
from dam.auth import AuthError
from dam.categories import (
    CategoryChange, CategoryError, default_catalog_path, load_catalog, merge_categories,
    new_category_id, propose_change, render_change, resolve_category, save_change,
)
from dam.config import ConfigurationError, load_config
from dam.gmail import GmailAdapterError
from dam.learning import (
    CandidateRule, LearningError, configuration_with_learned_rules, default_learned_rules_path,
    propose_classification_rule, render_candidate, save_classification_rule,
)
from dam.review import ReviewScopeError, render_review, review_gmail_message
from dam.models import CategoriesConfig
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


def _unknown_category_message(value: str) -> str:
    return (f"Unknown DAM category {value!r}. Run 'dam categories' to list valid categories. "
            "No rule saved.")


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
    return root


def _catalog_path(override: str | None) -> Path | None:
    path = Path(override) if override else default_catalog_path()
    return path if override or path.exists() else None


def _category_listing(categories: CategoriesConfig) -> str:
    """Display the validated taxonomy as a deterministic, indented tree."""
    categories = CategoriesConfig.model_validate(categories.model_dump(mode="python"))
    children = {item.id: [] for item in categories.categories}
    roots = []
    for item in categories.categories:
        (children[item.parent_id] if item.parent_id else roots).append(item)
    ordered = lambda items: sorted(items, key=lambda item: (item.name.casefold(), item.id))
    rows = []
    def visit(item, depth):
        label = ("  " * depth + ("|-- " if depth else "") + item.name)
        suffix = " (retired; unavailable for learning)" if item.status == "retired" else ""
        rows.append((label, item.key or item.id, suffix))
        for child in ordered(children[item.id]):
            visit(child, depth + 1)
    for root in ordered(roots):
        visit(root, 0)
    width = max((len(label) for label, _, _ in rows), default=0)
    return "\n".join(f"{label:<{width}}  {key}{suffix}" for label, key, suffix in rows)


def _category_save_command(args: argparse.Namespace, change: CategoryChange) -> str:
    parts = ["dam", "categories"]
    if args.catalog_file:
        parts.extend(("--catalog-file", args.catalog_file))
    if args.learned_rules_file:
        parts.extend(("--learned-rules-file", args.learned_rules_file))
    parts.append(args.category_command)
    if args.category_command == "add":
        parts.extend(("--key", args.key, "--name", args.name))
    else:
        parts.append(args.category)
        if args.category_command == "rename":
            parts.extend(("--name", args.name))
    if args.category_command in ("add", "move") and args.parent is not None:
        parts.extend(("--parent", args.parent))
    parts.append("--save")
    if args.category_command == "add":
        parts.extend(("--confirm-id", change.after.permanent_id))
    elif args.category_command == "retire":
        parts.extend(("--confirm-retire", change.after.permanent_id))
    parts.extend(("--confirm-fingerprint", change.fingerprint))
    return shlex.join(parts)


def _learn_save_command(args: argparse.Namespace, candidate: CandidateRule) -> str:
    parts = ["dam", "learn"]
    if args.gmail:
        parts.append("--gmail")
    parts.extend(("--message-id", args.message_id, "--category", args.category))
    if args.learned_rules_file:
        parts.extend(("--learned-rules-file", args.learned_rules_file))
    if args.category_catalog_file:
        parts.extend(("--category-catalog-file", args.category_catalog_file))
    parts.extend(("--save", "--confirm-fingerprint", candidate.fingerprint))
    return shlex.join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "categories":
        try:
            path = Path(args.catalog_file) if args.catalog_file else default_catalog_path()
            base = load_config(default_config_directory())
            catalog = load_catalog(path)
            effective = merge_categories(base.categories, catalog)
            if args.category_command == "show":
                item = resolve_category(args.category, effective, active=False)
                print(f"Permanent ID: {item.permanent_id}\nKey: {item.key or item.id}\n"
                      f"Name: {item.name}\nParent: {item.parent_id or '<root>'}\nStatus: {item.status}")
                return 0
            if args.category_command in ("add", "rename", "move", "retire"):
                if args.save != (args.confirm_fingerprint is not None):
                    raise CategoryError("save_requires_preview_fingerprint")
                if args.category_command == "add" and args.save and not args.confirm_id:
                    raise CategoryError("save_requires_generated_id")
                if args.category_command == "retire" and args.save and not args.confirm_retire:
                    raise CategoryError("retire_requires_identity_confirmation")
                generated = ((args.confirm_id if args.save else new_category_id(effective))
                             if args.category_command == "add" else None)
                change = propose_change(base, catalog, args.category_command,
                    selector=getattr(args, "category", None), key=getattr(args, "key", None),
                    name=getattr(args, "name", None), parent=getattr(args, "parent", None),
                    generated_id=generated,
                    learned_rules_path=(Path(args.learned_rules_file) if args.learned_rules_file
                                        else default_learned_rules_path()) if args.category_command == "retire" else None)
                if args.category_command == "retire" and args.save and args.confirm_retire != change.after.permanent_id:
                    raise CategoryError("retirement_identity_mismatch")
                if args.save:
                    save_change(path, base, change, expected_fingerprint=args.confirm_fingerprint,
                                learned_rules_path=(Path(args.learned_rules_file) if args.learned_rules_file
                                                    else default_learned_rules_path())
                                if args.category_command == "retire" else None)
                print(render_change(change, saved=args.save), end="")
                if not args.save:
                    print("\nConfirmation required to save:")
                    print(f"Category ID: {change.after.permanent_id}")
                    print(f"Fingerprint: {change.fingerprint}")
                    print("Save command:")
                    print(_category_save_command(args, change))
                return 0
        except CategoryError as error:
            reason = str(error)
            if reason.startswith("category_has_active_children:"):
                print("DAM categories: move active children before retirement: " +
                      reason.partition(":")[2] + ". No change saved.", file=sys.stderr)
            elif reason.startswith("category_has_active_rule_references:"):
                print("DAM categories: resolve active rule references before retirement: " +
                      reason.partition(":")[2] + ". No change saved.", file=sys.stderr)
            else:
                print("DAM categories failed: invalid local category change or catalog; no change saved.",
                      file=sys.stderr)
            return 2
        except (ConfigurationError, LearningError, OSError, ValueError):
            print("DAM categories failed: invalid local configuration.", file=sys.stderr)
            return 2
        except Exception:
            print("DAM categories failed internally.", file=sys.stderr)
            return 1
        print("DAM categories (use a key with dam learn; show <key> displays its permanent ID):")
        print(_category_listing(effective))
        return 0
    if args.command == "scan":
        learned = default_learned_rules_path() if args.use_learned_rules else None
        category_path = _catalog_path(args.category_catalog_file)
        if args.gmail:
            if args.limit is not None and args.limit > MAX_INITIAL_GMAIL_LIMIT:
                print(f"DAM Gmail mode limit must be from 1 to {MAX_INITIAL_GMAIL_LIMIT}; no Gmail access attempted.", file=sys.stderr)
                return 2
            try:
                options = {"category_catalog_path": category_path} if category_path is not None else {}
                if learned is None:
                    result = run_gmail_scan(limit=args.limit, **options)
                else:
                    result = run_gmail_scan(limit=args.limit, learned_rules_path=learned, **options)
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
            options = {"category_catalog_path": category_path} if category_path is not None else {}
            if learned is None:
                result = run_synthetic_scan(limit=args.limit, **options)
            else:
                result = run_synthetic_scan(limit=args.limit, learned_rules_path=learned, **options)
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
            path = (default_learned_rules_path() if args.learned_rules_file is None
                    else Path(args.learned_rules_file))
            category_path = _catalog_path(args.category_catalog_file)
            if args.gmail:
                reviewed = review_gmail_message(args.message_id, category_id=args.category,
                                                learned_rules_path=path,
                                                **({"category_catalog_path": category_path} if category_path else {}))
                selected_category = resolve_category(args.category, reviewed.config.categories).id
                candidate = propose_classification_rule(
                    reviewed.message, selected_category, reviewed.config, as_of=reviewed.as_of)
                config = reviewed.config
            else:
                messages = load_synthetic_messages()
                source = next((item for item in messages if item.message_id == args.message_id), None)
                if source is None:
                    raise LearningError("unknown_synthetic_message")
                config = configuration_with_learned_rules(load_config(
                    default_config_directory(), category_catalog_path=category_path), path)
                selected_category = resolve_category(args.category, config.categories).id
                candidate = propose_classification_rule(
                    source, selected_category, config, as_of=datetime.now(timezone.utc),
                    sample=tuple(item for item in messages if item.message_id != source.message_id))
            if args.save:
                outcome = save_classification_rule(
                    candidate, config, path, expected_fingerprint=args.confirm_fingerprint)
                print(render_candidate(candidate, status=outcome.status), end="")
            else:
                print(render_candidate(candidate), end="")
                print("\nConfirmation required to save:")
                print(f"Fingerprint: {candidate.fingerprint}")
                print("Save command:")
                print(_learn_save_command(args, candidate))
            return 0
        except ReviewScopeError as error:
            if str(error) == "message_outside_inbox":
                print("DAM learn: message is outside the current Inbox review scope; no rule saved.",
                      file=sys.stderr)
            elif str(error) == "unknown_category":
                print(_unknown_category_message(args.category), file=sys.stderr)
            else:
                print("DAM learn rejected the category or message scope; no rule saved.",
                      file=sys.stderr)
            return 2
        except LearningError as error:
            if str(error) == "unknown_category":
                print(_unknown_category_message(args.category), file=sys.stderr)
            else:
                print("DAM learn rejected the classification input or private rule file; no mailbox actions executed.",
                      file=sys.stderr)
            return 2
        except CategoryError as error:
            if str(error) in ("unknown_or_ambiguous_category", "retired_category"):
                print(_unknown_category_message(args.category), file=sys.stderr)
            else:
                print("DAM learn rejected the private category catalog; no rule saved.", file=sys.stderr)
            return 2
        except (AuthError, GmailAdapterError, ConfigurationError,
                ScanInputError, OSError, ValueError):
            print("DAM learn rejected the classification input or private rule file; no mailbox actions executed.",
                  file=sys.stderr)
            return 2
        except Exception:
            print("DAM learn failed internally; no mailbox actions executed.", file=sys.stderr)
            return 1
    if args.command == "review":
        if not args.gmail:
            print("DAM review requires explicit --gmail; no Gmail access attempted.", file=sys.stderr)
            return 2
        try:
            path = (default_learned_rules_path() if args.learned_rules_file is None
                    else Path(args.learned_rules_file))
            category_path = _catalog_path(args.category_catalog_file)
            result = review_gmail_message(args.message_id, learned_rules_path=path,
                                          **({"category_catalog_path": category_path} if category_path else {}))
            print(render_review(result), end="")
            return 0
        except ReviewScopeError as error:
            if str(error) == "message_outside_inbox":
                print("DAM review: message is outside the current Inbox review scope.",
                      file=sys.stderr)
            else:
                print("DAM review rejected the message scope.", file=sys.stderr)
            return 2
        except (LearningError, AuthError, GmailAdapterError, ConfigurationError,
                OSError, ValueError):
            print("DAM review failed safely; no mailbox actions executed.", file=sys.stderr)
            return 2
        except Exception:
            print("DAM review failed internally; no mailbox actions executed.", file=sys.stderr)
            return 1
    return 2
