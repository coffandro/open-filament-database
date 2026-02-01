"""
Validate command - Validates data files against schemas.

This command provides comprehensive validation for the Open Filament Database,
including JSON schema validation, logo validation, folder name checks, and more.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

from ofd.validation import (
    ValidationOrchestrator,
    ValidationResult,
    ValidationError,
)


# Project root for resolving relative paths
project_root = Path(__file__).parent.parent.parent


def register_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register the validate subcommand."""
    parser = subparsers.add_parser(
        'validate',
        help='Validate data files against schemas',
        description='Validate all data files (brands, materials, filaments, variants, sizes, stores) against their JSON schemas.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ofd validate                 Run all validations
  ofd validate --logos         Only validate logo files
  ofd validate --json-files    Only validate JSON schema compliance
  ofd validate --json          Output results as JSON
  ofd validate --progress      Emit progress events for SSE
        """
    )

    # Validation scope options
    scope_group = parser.add_argument_group('validation scope')
    scope_group.add_argument(
        '--json-files',
        action='store_true',
        help='Validate JSON files against schemas'
    )
    scope_group.add_argument(
        '--logos', '--logo-files',
        action='store_true',
        dest='logos',
        help='Validate logo files (dimensions, naming, format)'
    )
    scope_group.add_argument(
        '--folder-names',
        action='store_true',
        help='Validate folder names match JSON content'
    )
    scope_group.add_argument(
        '--store-ids',
        action='store_true',
        help='Validate store IDs in purchase links'
    )
    scope_group.add_argument(
        '--gtin',
        action='store_true',
        help='Validate GTIN/EAN fields'
    )

    # Output options
    output_group = parser.add_argument_group('output options')
    output_group.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )
    output_group.add_argument(
        '--progress',
        action='store_true',
        help='Emit progress events (for SSE streaming)'
    )

    # Directory options
    dir_group = parser.add_argument_group('directory options')
    dir_group.add_argument(
        '--data-dir',
        default='data',
        help='Data directory (default: data)'
    )
    dir_group.add_argument(
        '--stores-dir',
        default='stores',
        help='Stores directory (default: stores)'
    )

    # Change detection options
    change_group = parser.add_argument_group('change detection')
    change_group.add_argument(
        '--changed-only',
        action='store_true',
        help='Only validate files changed since base branch (for PR validation)'
    )
    change_group.add_argument(
        '--base-branch',
        default='main',
        help='Base branch for change detection (default: main)'
    )
    change_group.add_argument(
        '--since',
        help='Validate files changed since timestamp (e.g., "2 hours ago", "2024-01-01")'
    )

    # Filtering options
    filter_group = parser.add_argument_group('filtering')
    filter_group.add_argument(
        '--brands',
        nargs='+',
        help='Only validate specific brands (e.g., prusa bambulab)'
    )
    filter_group.add_argument(
        '--materials',
        nargs='+',
        help='Only validate specific materials (e.g., PLA PETG ABS)'
    )
    filter_group.add_argument(
        '--pattern',
        nargs='+',
        help='Only validate files matching glob pattern (e.g., "data/prusa/**/*.json")'
    )

    # Performance options
    perf_group = parser.add_argument_group('performance')
    perf_group.add_argument(
        '--workers',
        type=int,
        help='Number of parallel workers (default: CPU count - 2, min 1)'
    )
    perf_group.add_argument(
        '--use-processes',
        action='store_true',
        help='Use process pool instead of thread pool (threads recommended for I/O-bound validation)'
    )

    parser.set_defaults(func=run_validate)


def run_validate(args: argparse.Namespace) -> int:
    """
    Execute the validate command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, 1 for validation errors)
    """
    # Resolve directories
    data_dir = project_root / args.data_dir
    stores_dir = project_root / args.stores_dir

    # Check directories exist
    if not data_dir.exists():
        print(f"Error: Data directory '{data_dir}' does not exist", file=sys.stderr)
        return 1
    if not stores_dir.exists():
        print(f"Error: Stores directory '{stores_dir}' does not exist", file=sys.stderr)
        return 1

    # Determine changed files for filtering
    changed_files = None
    if args.changed_only:
        if not args.json and not args.progress:
            print(f"Detecting changed files since '{args.base_branch}'...")
        try:
            from ofd.validation.change_detection import get_changed_files_in_pr
            changed_files = get_changed_files_in_pr(args.base_branch, project_root)
            if not args.json and not args.progress:
                print(f"Found {len(changed_files)} changed files")
        except RuntimeError as e:
            print(f"Error detecting changed files: {e}", file=sys.stderr)
            return 1
    elif args.since:
        if not args.json and not args.progress:
            print(f"Detecting changed files since '{args.since}'...")
        try:
            from ofd.validation.change_detection import get_changed_files_since
            changed_files = get_changed_files_since(args.since, project_root)
            if not args.json and not args.progress:
                print(f"Found {len(changed_files)} changed files")
        except (RuntimeError, ValueError) as e:
            print(f"Error detecting changed files: {e}", file=sys.stderr)
            return 1
    elif args.brands or args.materials or args.pattern:
        if not args.json and not args.progress:
            print("Filtering files by pattern...")
        try:
            from ofd.validation.change_detection import get_files_matching_patterns
            changed_files = get_files_matching_patterns(
                brands=args.brands,
                materials=args.materials,
                patterns=args.pattern,
                project_root=project_root
            )
            if not args.json and not args.progress:
                print(f"Found {len(changed_files)} matching files")
        except Exception as e:
            print(f"Error filtering files: {e}", file=sys.stderr)
            return 1

    # Create orchestrator
    # Default to using all cores except 2 (leave for desktop/system processes)
    cpu_count = os.cpu_count() or 4
    default_workers = max(1, cpu_count - 2)
    num_workers = args.workers or default_workers

    if not args.json and not args.progress:
        executor_type = "processes" if args.use_processes else "threads"
        print(f"Using {num_workers} parallel workers ({executor_type}) on {cpu_count} CPU cores")

    orchestrator = ValidationOrchestrator(
        data_dir=data_dir,
        stores_dir=stores_dir,
        max_workers=num_workers,
        progress_mode=args.progress,
        use_processes=args.use_processes,
        changed_files=changed_files,
        project_root=project_root
    )

    result = ValidationResult()

    # Determine what to validate
    specific_validations = any([
        args.json_files,
        args.logos,
        args.folder_names,
        args.store_ids,
        args.gtin,
    ])

    if not specific_validations:
        # Run all validations
        if not args.json and not args.progress:
            print("Running all validations...")
        result = orchestrator.validate_all()
    else:
        # Run specific validations
        if args.json_files:
            result.merge(orchestrator.validate_json_files())
        if args.logos:
            result.merge(orchestrator.validate_logo_files())
        if args.folder_names:
            result.merge(orchestrator.validate_folder_names())
        if args.store_ids:
            result.merge(orchestrator.validate_store_ids())
        if args.gtin:
            result.merge(orchestrator.validate_gtin())

    # Output results
    if args.json:
        output = result.to_dict()
        if args.progress:
            print(json.dumps(output))
        else:
            print(json.dumps(output, indent=2))
        return 0 if result.is_valid else 1

    # Text output mode
    if result.errors:
        # Group errors by category
        errors_by_category: Dict[str, List[ValidationError]] = {}
        for error in result.errors:
            if error.category not in errors_by_category:
                errors_by_category[error.category] = []
            errors_by_category[error.category].append(error)

        # Print errors grouped by category
        for category, errors in sorted(errors_by_category.items()):
            print(f"\n{category} ({len(errors)}):")
            print("-" * 80)
            for error in errors:
                print(f"  {error}")

        print(f"\nValidation failed: {result.error_count} errors, {result.warning_count} warnings")
        return 1
    else:
        print("All validations passed!")
        return 0
