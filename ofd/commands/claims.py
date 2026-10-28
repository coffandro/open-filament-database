"""
Claims command - enforce brand-representative sign-off on PRs touching claimed brands.

``ofd claims validate`` checks the registry itself; ``ofd claims check`` evaluates a PR
against it via the ``gh`` CLI and, with ``--post``, publishes the ``Brand sign-off``
commit status, requests reviews from the representatives and keeps one sticky comment
up to date. See .github/workflows/brand_signoff.yaml.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

from ofd.brand_claims import CLAIMS_FILE, evaluate, load_claims, pending_brands, validate_claims

project_root = Path(__file__).parent.parent.parent

STATUS_CONTEXT = "Brand sign-off"
WAIVER_LABEL = "brand-signoff-waived"
COMMENT_MARKER = "<!-- ofd-brand-signoff -->"


def register_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register the claims subcommand and its actions."""
    parser = subparsers.add_parser(
        "claims",
        help="Brand claims: representative sign-off on claimed brands",
        description=(
            "Brands listed in ofd/brand_claims.json are claimed: a PR touching "
            "data/<brand_id>/ needs an approval from one of the brand's representatives."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ofd claims validate                  Check the registry is well-formed and brands exist
  ofd claims check --pr 791            Show which claimed brands PR #791 touches and their state
  ofd claims check --pr 791 --post     ...and publish the status / reviews / comment (CI only)
        """,
    )
    parser.add_argument("--claims-file", default=str(project_root / CLAIMS_FILE))
    actions = parser.add_subparsers(title="actions", dest="claims_action", metavar="<action>")

    p_val = actions.add_parser("validate", help="Validate the claims registry")
    p_val.add_argument("--data-dir", default=str(project_root / "data"))
    p_val.set_defaults(func=run_validate)

    p_check = actions.add_parser("check", help="Evaluate a PR's brand sign-off")
    p_check.add_argument("--pr", type=int, required=True, help="Pull request number")
    p_check.add_argument(
        "--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY or gh's current repo)"
    )
    p_check.add_argument(
        "--post",
        action="store_true",
        help="Publish the commit status, request reviewers and update the PR comment",
    )
    p_check.set_defaults(func=run_check)

    parser.set_defaults(func=lambda args: _no_action(parser))


def _no_action(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 1


def run_validate(args: argparse.Namespace) -> int:
    errors = validate_claims(Path(args.claims_file), Path(args.data_dir))
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        return 1
    print(f"{len(load_claims(Path(args.claims_file)))} claimed brand(s), registry OK")
    return 0


# GitHub I/O -------------------------------------------------------------------


def _gh(
    *args: str,
    input: dict | None = None,
    paginate: bool = False,
    check: bool = True,
    token: str | None = None,
):
    cmd = ["gh", "api", *args]
    if paginate:
        cmd[2:2] = ["--paginate", "--slurp"]
    if input is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(
        cmd,
        input=json.dumps(input) if input is not None else None,
        capture_output=True,
        text=True,
        env={**os.environ, "GH_TOKEN": token} if token else None,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh api {args[0]} failed: {proc.stderr.strip()}")
    if proc.returncode != 0:
        return None
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    # --slurp wraps each page in an outer list
    return [item for page in out for item in page] if paginate else out


def _default_repo() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _render_comment(
    state: dict[str, dict],
    head_sha: str,
    outcome: str,
    review_request_failed: list[str],
) -> str:
    """The sticky PR comment; ``outcome`` is "blocked", "signed_off" or "waived"."""
    brands = ", ".join(f"`{b}`" for b in state)
    if outcome == "blocked":
        lines = [
            "### 🚫 Merging is blocked until a brand representative signs off",
            "",
            f"This PR changes data of {brands}, which is **claimed** by its manufacturer, "
            f"so a representative has to approve it. The required **{STATUS_CONTEXT}** "
            "check stays pending until then, so this cannot be merged by accident.",
            "",
            "To unblock it, either:",
            "- a representative listed below approves the **latest commit** "
            "(*Files changed → Review changes → Approve*), or",
            f"- a maintainer adds the `{WAIVER_LABEL}` label, e.g. when the "
            "representative signed off somewhere else. Say where in a comment.",
        ]
    elif outcome == "waived":
        lines = [
            "### ⚠️ Brand sign-off waived by a maintainer",
            "",
            f"This PR changes data of {brands}, which is claimed by its manufacturer. "
            f"The `{WAIVER_LABEL}` label overrides the representative approval. "
            "Remove the label to require it again.",
        ]
    else:
        lines = [
            "### ✅ Brand sign-off complete",
            "",
            f"A representative of {brands} approved the latest commit.",
        ]
    lines += ["", "| Brand | Representatives | Status |", "|---|---|---|"]
    for brand, s in state.items():
        reps = ", ".join(f"@{r}" for r in s["representatives"])
        if s["changes_requested_by"]:
            status = "changes requested by " + ", ".join(f"@{r}" for r in s["changes_requested_by"])
        elif s["signed_off_by"]:
            status = f"signed off by @{s['signed_off_by']}"
        else:
            status = "awaiting approval"
        lines.append(f"| `{brand}` | {reps} | {status} |")
    lines += [
        "",
        f"<sub>Evaluated at {head_sha[:7]}. New commits need a fresh approval.</sub>",
    ]
    if review_request_failed:
        lines += [
            "",
            "<sub>GitHub could not add "
            + ", ".join(review_request_failed)
            + " as reviewer (not a repository collaborator), so they are mentioned here instead.</sub>",
        ]
    return COMMENT_MARKER + "\n" + "\n".join(lines)


def run_check(args: argparse.Namespace) -> int:
    repo = args.repo or _default_repo()
    claims = load_claims(Path(args.claims_file))

    pr = _gh(f"repos/{repo}/pulls/{args.pr}")
    head_sha = pr["head"]["sha"]
    author = pr["user"]["login"]
    waived = any(lbl["name"] == WAIVER_LABEL for lbl in pr["labels"])

    files = _gh(f"repos/{repo}/pulls/{args.pr}/files?per_page=100", paginate=True)
    paths = [f["filename"] for f in files] + [
        f["previous_filename"] for f in files if f.get("previous_filename")
    ]
    # The files endpoint stops at 3000 entries; anything past that is invisible to us,
    # so fail closed rather than miss a claimed brand.
    truncated = len(files) < pr["changed_files"]

    reviews = _gh(f"repos/{repo}/pulls/{args.pr}/reviews?per_page=100", paginate=True)
    state = evaluate(paths, claims, author, reviews, head_sha)
    pending = pending_brands(state)

    if waived:
        status, desc = "success", f"Waived by maintainer ({WAIVER_LABEL})"
    elif truncated:
        status, desc = (
            "failure",
            "PR too large to list every file; a maintainer must review and waive",
        )
    elif not state:
        status, desc = "success", "No claimed brands touched"
    elif pending:
        status, desc = "pending", "Awaiting brand representative: " + ", ".join(pending)
    else:
        status, desc = (
            "success",
            "Signed off by " + ", ".join(f"{s['signed_off_by']} ({b})" for b, s in state.items()),
        )

    print(json.dumps({"pr": args.pr, "head": head_sha, "brands": state}, indent=2))
    print(f"{STATUS_CONTEXT}: {status} - {desc}")

    if not args.post:
        return 0 if status == "success" else 1

    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    _gh(
        f"repos/{repo}/statuses/{head_sha}",
        "-X",
        "POST",
        input={
            "state": status,
            "context": STATUS_CONTEXT,
            "description": desc[:140],
            **({"target_url": run_url} if run_url else {}),
        },
    )

    if not state:
        return 0

    # Comments and review requests go out as the repo bot when its token is set
    # (OFD_BOT_TOKEN); the status stays on the workflow token, which can write statuses.
    bot = os.environ.get("OFD_BOT_TOKEN") or None

    # Ask the representatives of still-pending brands for a review. GitHub only allows
    # requests to collaborators; anyone else is reached through the comment mention.
    want = sorted(
        {r for b in pending for r in state[b]["representatives"] if r.lower() != author.lower()}
    )
    failed = []
    if want and not waived:
        if (
            _gh(
                f"repos/{repo}/pulls/{args.pr}/requested_reviewers",
                "-X",
                "POST",
                input={"reviewers": want},
                check=False,
                token=bot,
            )
            is None
        ):
            failed = want

    outcome = "waived" if waived else "blocked" if pending else "signed_off"
    body = _render_comment(state, head_sha, outcome, failed)
    comments = _gh(f"repos/{repo}/issues/{args.pr}/comments?per_page=100", paginate=True)
    mine = next((c for c in comments if COMMENT_MARKER in c["body"]), None)
    if mine is not None and mine["body"] == body:
        return 0
    # Editing fails when the marked comment isn't ours (posted under another token, or
    # someone copied the marker); post a fresh one then.
    if mine is None or (
        _gh(
            f"repos/{repo}/issues/comments/{mine['id']}",
            "-X",
            "PATCH",
            input={"body": body},
            check=False,
            token=bot,
        )
        is None
    ):
        _gh(
            f"repos/{repo}/issues/{args.pr}/comments",
            "-X",
            "POST",
            input={"body": body},
            token=bot,
        )
    return 0
