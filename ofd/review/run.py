"""
End to end: fetch a PR, run the free tiers, optionally the model, and post.

``review()`` writes everything ``post()`` needs into one JSON document, so CI can run
the two as separate steps (and upload the document as an artifact), while a local
``ofd review run --pr N`` just prints what it would post.
"""

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ofd.review import github
from ofd.review.checks import enrichment_gaps, run_checks
from ofd.review.context import build_context
from ofd.review.flipflop import detect
from ofd.review.gitdata import TreeReader, changed_files, git
from ofd.review.model import DEFAULT_BUDGET_USD, DEFAULT_MODEL, ModelError, available, run_model
from ofd.review.render import (
    MARKER,
    extract_ai_section,
    inline_comments,
    summary,
)
from ofd.review.suggest import build_suggestions


def log(message: str) -> None:
    print(message, file=sys.stderr)


def fetch_pr(repo_dir: Path, repo: str, pr: dict) -> tuple[str, str, str]:
    """Fetch the PR head and its base branch; returns (merge base, head, history ref).

    The history ref is what the flip-flop scan walks: the base branch as it is now, so
    a stale branch that writes back values fixed on main since it forked is caught
    (#183 put Geeetech's template temperatures back that way). Once merged, the base
    branch contains the PR itself, so it falls back to the base as the PR saw it.
    """
    number = pr["number"]
    git(
        repo_dir,
        "fetch",
        "--no-tags",
        "--quiet",
        f"https://github.com/{repo}.git",
        f"+refs/pull/{number}/head:refs/ofd-review/{number}/head",
        f"+refs/heads/{pr['base']['ref']}:refs/ofd-review/{number}/base",
    )
    head = pr["head"]["sha"]
    # base.sha rather than the branch tip: once merged, the head is an ancestor of the
    # tip, and a merge base with it would be the head itself.
    merge_base = git(repo_dir, "merge-base", pr["base"]["sha"], head).strip()
    history = pr["base"]["sha"] if pr.get("merged") else f"refs/ofd-review/{number}/base"
    return merge_base, head, history


@contextmanager
def head_worktree(repo_dir: Path, head: str):
    """A detached, throwaway checkout of the head for the model's read-only tools."""
    with tempfile.TemporaryDirectory(prefix="ofd-review-") as tmp:
        path = Path(tmp) / "head"
        git(repo_dir, "worktree", "add", "--detach", "--quiet", str(path), head)
        try:
            yield path
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=repo_dir,
                capture_output=True,
            )


def review(
    repo_dir: Path,
    repo: str,
    number: int,
    use_model: bool = True,
    model: str = DEFAULT_MODEL,
    budget_usd: float = DEFAULT_BUDGET_USD,
    head_dir: Path | None = None,
) -> dict:
    pr = github.pull_request(repo, number)
    base, head, history = fetch_pr(repo_dir, repo, pr)
    log(f"#{number}: reviewing {base[:7]}..{head[:7]}")

    changes, others = changed_files(repo_dir, base, head)
    base_tree, head_tree = TreeReader(repo_dir, base), TreeReader(repo_dir, head)
    findings = run_checks(changes, base_tree, head_tree)
    flipflops = detect(repo_dir, history, changes)
    log(f"#{number}: {len(changes)} files, {len(findings)} findings, {len(flipflops)} flip-flops")

    result: dict = {
        "pr": number,
        "base_sha": base,
        "head_sha": head,
        "findings": [f.to_dict() for f in findings],
        "flipflops": [ff.to_dict() for ff in flipflops],
        "model": None,
        "comments": [],
    }
    if not use_model or not changes:
        return result
    if not available():
        result["model"] = {"error": "the `claude` CLI is not installed"}
        return result

    context = build_context(pr, changes, others, findings, flipflops, enrichment_gaps(changes))
    try:
        if head_dir:
            model_result = run_model(context, head_dir, model, budget_usd)
        else:
            with head_worktree(repo_dir, head) as path:
                model_result = run_model(context, path, model, budget_usd)
    except ModelError as exc:
        log(f"#{number}: {exc}")
        result["model"] = {"error": str(exc)}
        return result

    ai = model_result.review
    outcome = build_suggestions(ai.get("enrichments", []), changes)
    comments, not_placed = inline_comments(ai, outcome, changes)
    result.update(
        model={
            "model": model_result.model,
            "cost_usd": model_result.cost_usd,
            "summary": ai.get("summary", ""),
            "findings": ai.get("findings", []),
            "enrichments": ai.get("enrichments", []),
        },
        comments=comments,
        not_placed=not_placed,
        suggestion_count=len(outcome.suggestions),
        suggestions=[asdict(s) for s in outcome.suggestions],
        manual=outcome.manual,
        rejected=outcome.rejected,
    )
    log(
        f"#{number}: model ${model_result.cost_usd:.2f}, {len(ai.get('findings', []))} findings, "
        f"{len(outcome.suggestions)} suggestions, {len(outcome.rejected)} rejected"
    )
    return result


def merge_url(number: int) -> str:
    base = os.environ.get("OFD_WEBUI_URL", "").rstrip("/")
    return f"{base}/review/merge/{number}" if base else ""


def run_url() -> str:
    server, repo, run = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def render(result: dict, previous_body: str = "") -> str:
    return summary(
        result,
        previous_ai=extract_ai_section(previous_body),
        run_url=run_url(),
        merge_url=merge_url(result["pr"]),
    )


def post(repo: str, result: dict) -> None:
    number = result["pr"]
    current = github.pull_request(repo, number)
    if current["head"]["sha"] != result["head_sha"]:
        log(f"#{number}: head moved to {current['head']['sha'][:7]} during the review; not posting")
        return

    existing = github.find_marker_comment(repo, number, MARKER)
    github.upsert_comment(repo, number, existing, render(result, (existing or {}).get("body", "")))

    if result.get("comments"):
        try:
            github.create_review(
                repo,
                number,
                result["head_sha"],
                "AI review: see the summary comment.",
                result["comments"],
            )
        except github.GitHubError as exc:
            # A line GitHub won't accept rejects the whole review; the summary still stands.
            log(f"#{number}: inline review rejected: {exc}")
