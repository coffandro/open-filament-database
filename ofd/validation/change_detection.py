"""
Change detection utilities for the OFD validation system.

This module provides functions to detect changed files in different contexts:
- Git-based change detection for PR validation
- Timestamp-based change detection for incremental validation
- Pattern-based filtering for CLI scope narrowing
"""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .types import ValidationError, ValidationLevel


def get_changed_files_in_pr(
    base_branch: str = "main",
    project_root: Optional[Path] = None
) -> set[Path]:
    """
    Get files changed in a PR using git diff.

    Args:
        base_branch: Base branch to compare against (default: "main")
        project_root: Project root directory (default: current working directory)

    Returns:
        Set of Path objects for changed files, relative to project root

    Raises:
        RuntimeError: If git command fails or not in a git repository
    """
    if project_root is None:
        project_root = Path.cwd()

    try:
        # First, try to fetch the base branch to ensure we have latest
        # Suppress errors if remote doesn't exist (e.g., local-only repo)
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=project_root,
            capture_output=True,
            timeout=30
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        # Continue even if fetch fails - we'll use local branch
        pass

    try:
        # Get list of changed files compared to base branch
        # Use ... (three dots) to get changes in current branch only
        result = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )

        # Fallback: if origin/base_branch doesn't exist, try without origin/
        if result.returncode != 0 or not result.stdout.strip():
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )

    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to get changed files from git: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Git command timed out after 30 seconds") from e

    # Parse output and filter for relevant files
    changed_files = set()
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue

        file_path = project_root / line.strip()

        # Only include files in data/, stores/, or schemas/ directories
        relative_path = line.strip()
        if (relative_path.startswith('data/') or
            relative_path.startswith('stores/') or
            relative_path.startswith('schemas/')):
            changed_files.add(file_path)

    return changed_files


def get_changed_files_since(
    timestamp: str,
    project_root: Optional[Path] = None,
    use_git: bool = True
) -> set[Path]:
    """
    Get files changed since a specific timestamp.

    Args:
        timestamp: Timestamp string (e.g., "2 hours ago", "2024-01-01 10:00")
        project_root: Project root directory (default: current working directory)
        use_git: Use git to detect changes (default: True). If False, uses filesystem mtime.

    Returns:
        Set of Path objects for changed files, relative to project root

    Raises:
        RuntimeError: If git command fails or invalid timestamp format
    """
    if project_root is None:
        project_root = Path.cwd()

    if use_git:
        try:
            # Use git log to find files changed since timestamp
            result = subprocess.run(
                ["git", "diff", "--name-only", f"@{{{timestamp}}}..HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to get changed files from git: {e.stderr}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Git command timed out after 30 seconds") from e

        # Parse output
        changed_files = set()
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue

            file_path = project_root / line.strip()
            relative_path = line.strip()

            if (relative_path.startswith('data/') or
                relative_path.startswith('stores/') or
                relative_path.startswith('schemas/')):
                changed_files.add(file_path)

        return changed_files

    else:
        # Filesystem-based detection using mtime
        # Parse timestamp into datetime
        cutoff_time = _parse_timestamp(timestamp)

        changed_files = set()
        data_dir = project_root / "data"
        stores_dir = project_root / "stores"

        # Scan data and stores directories
        for directory in [data_dir, stores_dir]:
            if not directory.exists():
                continue

            for file_path in directory.rglob('*'):
                if file_path.is_file():
                    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if mtime >= cutoff_time:
                        changed_files.add(file_path)

        return changed_files


def get_files_matching_patterns(
    brands: Optional[list[str]] = None,
    materials: Optional[list[str]] = None,
    patterns: Optional[list[str]] = None,
    project_root: Optional[Path] = None
) -> set[Path]:
    """
    Get files matching filter patterns for CLI scope narrowing.

    Args:
        brands: List of brand names to filter (e.g., ["prusa", "bambulab"])
        materials: List of material types to filter (e.g., ["PLA", "PETG"])
        patterns: List of glob patterns to match (e.g., ["data/prusa/**/*.json"])
        project_root: Project root directory (default: current working directory)

    Returns:
        Set of Path objects for matching files
    """
    if project_root is None:
        project_root = Path.cwd()

    matching_files = set()
    data_dir = project_root / "data"
    stores_dir = project_root / "stores"

    # Apply glob patterns
    if patterns:
        for pattern in patterns:
            # Support both absolute and relative patterns
            if Path(pattern).is_absolute():
                pattern_path = Path(pattern)
            else:
                pattern_path = project_root / pattern

            # Use glob to find matching files
            if '*' in pattern or '?' in pattern:
                # Pattern contains wildcards - use glob
                parent = pattern_path.parent
                while '*' in parent.name or '?' in parent.name:
                    parent = parent.parent

                if parent.exists():
                    for match in parent.glob(str(pattern_path.relative_to(parent))):
                        if match.is_file():
                            matching_files.add(match)
            else:
                # No wildcards - direct file path
                if pattern_path.exists() and pattern_path.is_file():
                    matching_files.add(pattern_path)

    # Apply brand filters
    if brands:
        for brand in brands:
            brand_dir = data_dir / brand
            if brand_dir.exists() and brand_dir.is_dir():
                # Add all files in this brand directory
                for file_path in brand_dir.rglob('*'):
                    if file_path.is_file():
                        matching_files.add(file_path)

    # Apply material filters
    if materials:
        if not data_dir.exists():
            return matching_files

        # Iterate through all brands
        for brand_dir in data_dir.iterdir():
            if not brand_dir.is_dir():
                continue

            # Check each material directory in this brand
            for material in materials:
                material_dir = brand_dir / material
                if material_dir.exists() and material_dir.is_dir():
                    # Add all files in this material directory
                    for file_path in material_dir.rglob('*'):
                        if file_path.is_file():
                            matching_files.add(file_path)

    # If no filters specified, return empty set (don't validate everything)
    if not patterns and not brands and not materials:
        return set()

    return matching_files


def expand_changed_files_with_dependencies(
    changed_files: set[Path],
    project_root: Optional[Path] = None
) -> set[Path]:
    """
    Expand changed files to include dependent files that should also be validated.

    Rules:
    - If brand.json changes -> include brand logo
    - If filament.json changes -> include variant files
    - If schema changes -> include all files using that schema
    - If sizes.json changes -> may trigger store ID validation

    Args:
        changed_files: Set of changed file paths
        project_root: Project root directory (default: current working directory)

    Returns:
        Expanded set of file paths including dependencies
    """
    if project_root is None:
        project_root = Path.cwd()

    expanded_files = changed_files.copy()

    for file_path in changed_files:
        try:
            relative_path = file_path.relative_to(project_root)
        except ValueError:
            # File is not relative to project root
            continue

        # Rule: brand.json -> include logo files
        if file_path.name == 'brand.json':
            brand_dir = file_path.parent
            for logo_name in ['logo.png', 'logo.jpg', 'logo.svg']:
                logo_path = brand_dir / logo_name
                if logo_path.exists():
                    expanded_files.add(logo_path)

        # Rule: filament.json -> include variant directories
        elif file_path.name == 'filament.json':
            filament_dir = file_path.parent
            if filament_dir.exists():
                for variant_dir in filament_dir.iterdir():
                    if variant_dir.is_dir():
                        variant_json = variant_dir / 'variant.json'
                        if variant_json.exists():
                            expanded_files.add(variant_json)

        # Rule: schema changes -> we'll need to validate all files using that schema
        # This is handled at a higher level in the orchestrator

        # Rule: store.json -> include store logo
        elif file_path.name == 'store.json':
            store_dir = file_path.parent
            for logo_name in ['logo.png', 'logo.jpg', 'logo.svg']:
                logo_path = store_dir / logo_name
                if logo_path.exists():
                    expanded_files.add(logo_path)

    return expanded_files


def _parse_timestamp(timestamp: str) -> datetime:
    """
    Parse timestamp string into datetime object.

    Supports formats:
    - Relative: "2 hours ago", "30 minutes ago", "1 day ago"
    - Absolute: "2024-01-01", "2024-01-01 10:00"
    - ISO 8601: "2024-01-01T10:00:00"

    Args:
        timestamp: Timestamp string to parse

    Returns:
        datetime object

    Raises:
        ValueError: If timestamp format is invalid
    """
    timestamp = timestamp.strip().lower()

    # Handle relative timestamps
    if 'ago' in timestamp:
        parts = timestamp.replace('ago', '').strip().split()
        if len(parts) != 2:
            raise ValueError(f"Invalid relative timestamp format: {timestamp}")

        try:
            value = int(parts[0])
            unit = parts[1].rstrip('s')  # Remove plural 's'

            now = datetime.now()
            if unit in ['hour', 'hr', 'h']:
                return now - timedelta(hours=value)
            elif unit in ['minute', 'min', 'm']:
                return now - timedelta(minutes=value)
            elif unit in ['day', 'd']:
                return now - timedelta(days=value)
            elif unit in ['week', 'wk', 'w']:
                return now - timedelta(weeks=value)
            else:
                raise ValueError(f"Unknown time unit: {unit}")

        except ValueError as e:
            raise ValueError(f"Invalid relative timestamp: {timestamp}") from e

    # Handle absolute timestamps
    else:
        # Try various datetime formats
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(timestamp, fmt)
            except ValueError:
                continue

        raise ValueError(f"Invalid timestamp format: {timestamp}")
