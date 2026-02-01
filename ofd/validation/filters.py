"""
Task filtering utilities for the OFD validation system.

This module provides functions to filter validation tasks based on changed files,
with intelligent dependency resolution to ensure all necessary validations are performed.
"""

from pathlib import Path
from typing import Set, List

from .types import ValidationTask


def filter_tasks_by_files(
    tasks: List[ValidationTask],
    changed_files: Set[Path],
    project_root: Path
) -> List[ValidationTask]:
    """
    Filter validation tasks to only include those relevant to changed files.

    This function implements intelligent dependency resolution:
    - If a JSON file changes, validate that file
    - If a brand.json changes, also validate the brand logo
    - If a schema changes, validate all files using that schema
    - If sizes.json changes, trigger store ID validation
    - If any file in a directory changes, validate folder naming

    Args:
        tasks: List of all validation tasks
        changed_files: Set of changed file paths
        project_root: Project root directory for resolving relative paths

    Returns:
        Filtered list of validation tasks
    """
    if not changed_files:
        # No files changed - return all tasks (full validation)
        return tasks

    filtered_tasks = []
    changed_relative_paths = set()

    # Convert changed files to relative paths for easier comparison
    for file_path in changed_files:
        try:
            relative = file_path.relative_to(project_root)
            changed_relative_paths.add(relative)
        except ValueError:
            # File is not relative to project root - skip
            continue

    # Check if any schema files changed
    schema_changes = any(
        str(rel_path).startswith('schemas/')
        for rel_path in changed_relative_paths
    )

    # Build directory set for folder validation
    changed_directories = set()
    for rel_path in changed_relative_paths:
        changed_directories.add(rel_path.parent)

    # Iterate through tasks and determine which should be included
    for task in tasks:
        should_include = False

        if task.task_type == 'json':
            # JSON validation task
            # Include if:
            # 1. The specific JSON file changed
            # 2. A schema file changed (validates all JSON)

            try:
                task_relative = task.path.relative_to(project_root)
            except ValueError:
                # Task path not relative to project root
                continue

            if schema_changes:
                # Schema changed - validate all JSON files using that schema
                schema_name = task.extra_data.get('schema_name', '') if task.extra_data else ''

                # Check if the relevant schema changed
                for rel_path in changed_relative_paths:
                    if (str(rel_path).startswith('schemas/') and
                        schema_name in str(rel_path)):
                        should_include = True
                        break
            elif task_relative in changed_relative_paths:
                # The specific JSON file changed
                should_include = True

        elif task.task_type == 'logo':
            # Logo validation task
            # Include if:
            # 1. The logo file itself changed
            # 2. The parent directory's JSON file changed (brand.json or store.json)

            try:
                task_relative = task.path.relative_to(project_root)
            except ValueError:
                continue

            if task_relative in changed_relative_paths:
                # Logo file itself changed
                should_include = True
            else:
                # Check if parent directory's JSON file changed
                parent_dir = task.path.parent
                for json_name in ['brand.json', 'store.json']:
                    json_path = parent_dir / json_name
                    try:
                        json_relative = json_path.relative_to(project_root)
                        if json_relative in changed_relative_paths:
                            should_include = True
                            break
                    except ValueError:
                        continue

        elif task.task_type == 'folder':
            # Folder name validation task
            # Include if:
            # 1. Any file in the directory changed
            # 2. The directory's JSON file changed

            try:
                task_dir = task.path
                task_dir_relative = task_dir.relative_to(project_root)
            except ValueError:
                continue

            # Check if any changed file is in this directory
            if task_dir_relative in changed_directories:
                should_include = True
            else:
                # Check subdirectories too
                for changed_dir in changed_directories:
                    try:
                        # Check if changed_dir is a subdirectory of task_dir
                        changed_dir.relative_to(task_dir_relative)
                        should_include = True
                        break
                    except ValueError:
                        continue

        if should_include:
            filtered_tasks.append(task)

    # Special handling for cross-file validators (StoreIdValidator, GTINValidator)
    # If any sizes.json file changed, we need to validate ALL store IDs and GTINs
    # because these validators cross-reference multiple files

    # Check if any sizes.json changed
    sizes_changed = any(
        rel_path.name == 'sizes.json'
        for rel_path in changed_relative_paths
    )

    # Note: Store ID and GTIN validation are handled separately in the orchestrator
    # as they are not task-based. This is documented here for context.

    return filtered_tasks


def get_affected_schema_files(
    changed_files: Set[Path],
    project_root: Path
) -> Set[str]:
    """
    Get schema names that should trigger full validation.

    If a schema file is modified, all files using that schema should be validated.

    Args:
        changed_files: Set of changed file paths
        project_root: Project root directory

    Returns:
        Set of schema names (e.g., {'brand', 'filament', 'variant'})
    """
    schema_names = set()

    for file_path in changed_files:
        try:
            relative = file_path.relative_to(project_root)
        except ValueError:
            continue

        # Check if file is in schemas/ directory
        if str(relative).startswith('schemas/'):
            # Extract schema name from filename
            # e.g., schemas/brand_schema.json -> 'brand'
            schema_filename = file_path.stem
            if schema_filename.endswith('_schema'):
                schema_name = schema_filename.replace('_schema', '')
                schema_names.add(schema_name)
            elif schema_filename.endswith('_logo'):
                # logo schemas like brand_logo.json
                schema_name = schema_filename  # Keep full name
                schema_names.add(schema_name)

    return schema_names


def should_validate_store_ids(
    changed_files: Set[Path],
    project_root: Path
) -> bool:
    """
    Determine if store ID validation should run based on changed files.

    Store ID validation cross-references files and should run if:
    - Any sizes.json file changed
    - Any store.json file changed
    - The stores/ directory structure changed

    Args:
        changed_files: Set of changed file paths
        project_root: Project root directory

    Returns:
        True if store ID validation should run
    """
    for file_path in changed_files:
        try:
            relative = file_path.relative_to(project_root)
        except ValueError:
            continue

        # Check for sizes.json or store.json changes
        if file_path.name in ['sizes.json', 'store.json']:
            return True

        # Check if file is in stores/ directory
        if str(relative).startswith('stores/'):
            return True

    return False


def should_validate_gtin(
    changed_files: Set[Path],
    project_root: Path
) -> bool:
    """
    Determine if GTIN/EAN validation should run based on changed files.

    GTIN validation should run if:
    - Any sizes.json file changed (contains GTIN/EAN fields)

    Args:
        changed_files: Set of changed file paths
        project_root: Project root directory

    Returns:
        True if GTIN validation should run
    """
    for file_path in changed_files:
        # Check for sizes.json changes
        if file_path.name == 'sizes.json':
            return True

    return False


def expand_tasks_for_dependencies(
    tasks: List[ValidationTask],
    changed_files: Set[Path],
    project_root: Path,
    all_tasks: List[ValidationTask]
) -> List[ValidationTask]:
    """
    Expand filtered tasks to include dependent validations.

    This ensures that when a file changes, all related validations are performed.

    Rules:
    - If brand.json changes -> include brand logo validation
    - If filament.json changes -> include variant validations
    - If schema changes -> include all files using that schema

    Args:
        tasks: Currently filtered tasks
        changed_files: Set of changed file paths
        project_root: Project root directory
        all_tasks: Complete list of all tasks (for finding dependencies)

    Returns:
        Expanded list of tasks including dependencies
    """
    expanded_tasks = tasks.copy()
    task_paths = {task.path for task in expanded_tasks}

    # Rule: brand.json -> include logo validation
    for file_path in changed_files:
        if file_path.name == 'brand.json':
            brand_dir = file_path.parent

            # Find logo tasks for this brand
            for task in all_tasks:
                if (task.task_type == 'logo' and
                    task.path.parent == brand_dir and
                    task.path not in task_paths):
                    expanded_tasks.append(task)
                    task_paths.add(task.path)

        # Rule: filament.json -> include variant validations
        elif file_path.name == 'filament.json':
            filament_dir = file_path.parent

            # Find variant tasks for this filament
            for task in all_tasks:
                if (task.task_type == 'json' and
                    task.extra_data and
                    task.extra_data.get('schema_name') == 'variant' and
                    filament_dir in task.path.parents and
                    task.path not in task_paths):
                    expanded_tasks.append(task)
                    task_paths.add(task.path)

        # Rule: store.json -> include store logo validation
        elif file_path.name == 'store.json':
            store_dir = file_path.parent

            # Find logo tasks for this store
            for task in all_tasks:
                if (task.task_type == 'logo' and
                    task.path.parent == store_dir and
                    task.path not in task_paths):
                    expanded_tasks.append(task)
                    task_paths.add(task.path)

    return expanded_tasks
