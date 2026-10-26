"""
Markdown for the summary comment and the inline review comments.

The summary is one comment per PR, found by ``MARKER`` and edited in place. The AI
part sits between ``AI_START``/``AI_END`` so a free re-check after a push can keep the
last model review instead of wiping it.
"""

import json

from ofd.review.gitdata import ChangedFile
from ofd.review.jsonlines import line_of

MARKER = "<!-- ofd-ai-review -->"
AI_START = "<!-- ofd-ai-review:ai -->"
AI_END = "<!-- /ofd-ai-review:ai -->"

ICONS = {"error": "🔴", "warning": "🟡", "note": "🔵"}
SEVERITY_ORDER = {"error": 0, "warning": 1, "note": 2}


def _bullet(level: str, path: str, message: str, pointer: str = "") -> str:
    where = f"`{path}`" + (f" `{pointer}`" if pointer else "")
    return f"- {ICONS.get(level, '•')} {where}: {message}"


def _sorted(items: list[dict], key: str) -> list[dict]:
    return sorted(items, key=lambda f: (SEVERITY_ORDER.get(f.get(key), 9), f.get("path", "")))


def _value_line(e: dict) -> str:
    note = f" ({e['note']})" if e.get("note") else ""
    value = json.dumps(e["value"], ensure_ascii=False)
    return f"- `{e['pointer']}` = `{value}` ({e['confidence']}, [source]({e['source_url']})){note}"


def suggestion_body(suggestion) -> str:
    lines = ["**Suggested data**, from the linked sources. Commit it if it checks out:", ""]
    lines += [_value_line(e) for e in suggestion.enrichments]
    lines += ["", "```suggestion", *suggestion.replacement, "```"]
    return "\n".join(lines)


def inline_comments(
    model_review: dict | None, outcome, changes: list[ChangedFile]
) -> tuple[list[dict], list[dict]]:
    """``(comments, not_placed)`` for the PR review API.

    Model findings go inline when GitHub will accept their line; the rest are
    returned so the summary can list them.
    """
    by_path = {c.path: c for c in changes}
    comments: list[dict] = []
    not_placed: list[dict] = []
    for finding in (model_review or {}).get("findings", []):
        change = by_path.get(finding.get("path"))
        line = None
        if change and change.head_text:
            line = line_of(change.head_text, finding.get("pointer") or "")
        if change is None or line not in change.commentable:
            not_placed.append(finding)
            continue
        comments.append(
            {
                "path": change.path,
                "line": line,
                "side": "RIGHT",
                "body": f"{ICONS.get(finding['severity'], '')} {finding['message']}",
            }
        )
    for suggestion in outcome.suggestions if outcome else []:
        comment = {
            "path": suggestion.path,
            "line": suggestion.end_line,
            "side": "RIGHT",
            "body": suggestion_body(suggestion),
        }
        if suggestion.start_line != suggestion.end_line:
            comment["start_line"] = suggestion.start_line
            comment["start_side"] = "RIGHT"
        comments.append(comment)
    return comments, not_placed


def ai_section(result: dict) -> str:
    model = result.get("model")
    if not model:
        return ""
    lines = [AI_START, "### AI review", ""]
    if model.get("error"):
        lines += [f"The model pass did not complete: {model['error']}", ""]
        return "\n".join(lines + [AI_END])

    lines += [model.get("summary", "").strip(), ""]
    if result.get("not_placed"):
        lines += ["**Other findings**", ""]
        lines += [
            _bullet(f["severity"], f["path"], f["message"], f.get("pointer", ""))
            for f in _sorted(result["not_placed"], "severity")
        ]
        lines.append("")

    inline_findings = len(model.get("findings", [])) - len(result.get("not_placed", []))
    suggestions = result.get("suggestion_count", 0)
    if inline_findings or suggestions:
        bits = []
        if inline_findings:
            bits.append(f"{inline_findings} finding{'s' if inline_findings != 1 else ''}")
        if suggestions:
            bits.append(
                f"{suggestions} data suggestion{'s' if suggestions != 1 else ''} "
                f"(**Commit suggestion** applies one)"
            )
        lines += [f"Inline on the diff: {', '.join(bits)}.", ""]

    if result.get("manual"):
        lines += [
            "<details><summary>Data found for lines outside this diff (add by hand)</summary>",
            "",
        ]
        for enrichment, why in result["manual"]:
            lines.append(f"`{enrichment['path']}`, {why}:")
            lines.append(_value_line(enrichment))
        lines += ["", "</details>", ""]
    if result.get("rejected"):
        lines += ["<details><summary>Proposed values dropped by validation</summary>", ""]
        for enrichment, why in result["rejected"]:
            lines.append(f"- `{enrichment.get('path')}` `{enrichment.get('pointer')}`: {why}")
        lines += ["", "</details>", ""]

    lines.append(
        f"<sub>{model.get('model')} · ${model.get('cost_usd', 0):.2f} · reviewed "
        f"{result['head_sha'][:7]}</sub>"
    )
    return "\n".join(lines + [AI_END])


def summary(result: dict, previous_ai: str = "", run_url: str = "", merge_url: str = "") -> str:
    lines = [MARKER, "## Automated review", ""]

    findings = _sorted(result.get("findings", []), "level")
    lines += ["### Checks", ""]
    if findings:
        lines += [
            _bullet(f["level"], f["path"], f["message"], f.get("pointer", "")) for f in findings
        ]
    else:
        lines.append("No issues found by the automated checks.")
    lines.append("")

    if result.get("flipflops"):
        lines += ["### Values going back and forth", ""]
        lines += [f"- `{ff['path']}`: {ff['message']}" for ff in result["flipflops"]]
        lines.append("")

    ai = ai_section(result)
    if not ai and previous_ai:
        ai = previous_ai
    if ai:
        lines += [ai, ""]
    elif not result.get("model"):
        lines += [
            "<sub>No AI review on this run. A maintainer can ask for one by commenting "
            "`/review`.</sub>",
            "",
        ]

    footer = f"<sub>Checks ran on {result['head_sha'][:7]}"
    footer += f" · [workflow run]({run_url})</sub>" if run_url else "</sub>"
    lines.append(footer)
    if merge_url:
        lines += ["", f"**Maintainers:** [✅ LGTM & merge]({merge_url})"]
    return "\n".join(lines) + "\n"


def extract_ai_section(body: str) -> str:
    start, end = body.find(AI_START), body.find(AI_END)
    if start == -1 or end == -1:
        return ""
    return body[start : end + len(AI_END)]
