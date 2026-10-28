"""
Enrich one brand: gaps -> one model pass -> independent verification -> write files.

The result is a report (JSON) and a PR body (Markdown); the workflow does the git
side, so ``ofd enrich run --brand X`` on a laptop just edits the working tree for the
developer to inspect with ``git diff``.
"""

import copy
import json
import sys
from pathlib import Path

from ofd.enrich.gaps import Gap, brand_gaps
from ofd.enrich.verify import Fetcher, verify
from ofd.review.checks import MaterialStats
from ofd.review.gitdata import TreeReader
from ofd.review.jsonlines import resolve, set_value
from ofd.review.model import DEFAULT_BUDGET_USD, DEFAULT_MODEL, ModelError, run_model
from ofd.review.suggest import SCHEMAS_DIR, indent_of, render_json, schema_errors
from ofd.scripts.style_data import build_key_order_map, sort_json_keys

HERE = Path(__file__).parent
GUIDELINES = HERE / "guidelines.md"
SCHEMA = HERE / "schema.json"
DEFAULT_LIMIT = 30


def log(message: str) -> None:
    print(message, file=sys.stderr)


def build_context(brand: str, root: Path, gaps: list[Gap]) -> str:
    brand_data = json.loads((root / "data" / brand / "brand.json").read_text(encoding="utf-8"))
    lines = [
        f"# Brand: {brand_data.get('name', brand)}",
        "",
        f"Website: {brand_data.get('website', 'unknown')}",
        f"Folder: `data/{brand}/`. Stores are in `stores/`.",
        "",
        "## Gaps",
        "",
    ]
    for gap in gaps:
        current = {k: v for k, v in gap.current.items() if v not in (None, "", [])}
        lines.append(f"### {gap.label}")
        lines.append(f"- path: `{gap.path}`, entity pointer: `{gap.prefix or '(root)'}`")
        if gap.missing:
            lines.append(f"- missing: {', '.join(gap.missing)}")
        if gap.recheck:
            lines.append(f"- recheck (template values): {', '.join(gap.recheck)}")
        if current:
            lines.append(f"- current: `{json.dumps(current, ensure_ascii=False)}`")
        lines.append("")
    return "\n".join(lines)


def apply(root: Path, enrichments: list[dict], gaps: list[Gap], report: dict) -> None:
    """Write verified values into the tree, in the repo's JSON style."""
    allowed: dict[str, set[str]] = {}
    for gap in gaps:
        allowed.setdefault(gap.path, set()).update(gap.pointers())
    key_order = build_key_order_map(SCHEMAS_DIR)

    by_path: dict[str, list[dict]] = {}
    for e in enrichments:
        by_path.setdefault(e["path"], []).append(e)

    for path, items in by_path.items():
        file = root / path
        text = file.read_text(encoding="utf-8")
        original = json.loads(text)
        doc = copy.deepcopy(original)
        kind = file.stem
        baseline = schema_errors(doc, kind)
        written: set[str] = set()
        changed = False
        for e in items:
            pointer = e["pointer"]
            if pointer not in allowed.get(path, set()) or pointer in written:
                report["rejected"].append({**e, "reason": "not one of the gaps for this run"})
                continue
            try:
                old = resolve(doc, pointer)
            except (KeyError, IndexError, TypeError):
                old = None
            if old == e["value"]:
                report["confirmed"].append(e)
                written.add(pointer)
                continue
            candidate = copy.deepcopy(doc)
            set_value(candidate, pointer, e["value"])
            new_errors = schema_errors(candidate, kind) - baseline
            if new_errors:
                report["rejected"].append({**e, "reason": f"fails the schema: {min(new_errors)}"})
                continue
            doc = candidate
            written.add(pointer)
            changed = True
            report["applied"].append({**e, "old": old})

        if not changed:
            continue
        info = key_order.get(kind)
        if info is not None:
            unsorted = json.dumps(original, ensure_ascii=False)
            if json.dumps(sort_json_keys(original, info, set()), ensure_ascii=False) == unsorted:
                doc = sort_json_keys(doc, info, set())
        file.write_text(render_json(doc, indent_of(text), text.endswith("\n")), encoding="utf-8")


def enrich_brand(
    root: Path,
    brand: str,
    limit: int = DEFAULT_LIMIT,
    model: str = DEFAULT_MODEL,
    budget_usd: float = DEFAULT_BUDGET_USD,
    write: bool = True,
) -> dict:
    if not (root / "data" / brand / "brand.json").exists():
        raise ValueError(f"no brand folder data/{brand}")
    stats = MaterialStats(TreeReader(root, "HEAD"))
    gaps = brand_gaps(root, brand, stats, limit)
    report: dict = {
        "brand": brand,
        "gaps": [vars(g) | {"fields": g.fields} for g in gaps],
        "applied": [],
        "confirmed": [],
        "unverified": [],
        "rejected": [],
        "not_found": [],
        "model": None,
    }
    log(f"{brand}: {len(gaps)} gaps")
    if not gaps:
        return report

    try:
        result = run_model(
            build_context(brand, root, gaps),
            root,
            model,
            budget_usd,
            guidelines=GUIDELINES,
            schema_file=SCHEMA,
        )
    except ModelError as exc:
        report["model"] = {"error": str(exc)}
        log(f"{brand}: {exc}")
        return report

    report["model"] = {"model": result.model, "cost_usd": result.cost_usd}
    report["not_found"] = result.review.get("not_found", [])
    fetcher = Fetcher()
    verified = []
    for e in result.review.get("enrichments", []):
        reason = verify(e, fetcher, root / "stores")
        if reason:
            report["unverified"].append({**e, "reason": reason})
        else:
            verified.append(e)

    if write:
        apply(root, verified, gaps, report)
    else:
        report["applied"] = verified
    log(
        f"{brand}: ${result.cost_usd:.2f}, {len(report['applied'])} applied, "
        f"{len(report['confirmed'])} confirmed, {len(report['unverified'])} unverified, "
        f"{len(report['rejected'])} rejected"
    )
    return report


# --- PR body ------------------------------------------------------------------


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return "<br>".join(f"{v.get('store_id')}: {v.get('url')}" for v in value)
    return f"`{json.dumps(value, ensure_ascii=False)}`"


def _quote(e: dict) -> str:
    quote = " ".join((e.get("source_quote") or "").split())
    return f"“{quote[:140]}{'…' if len(quote) > 140 else ''}”" if quote else ""


def pr_body(report: dict) -> str:
    labels = {(g["path"], g["prefix"]): g["label"] for g in report["gaps"]}

    def label(e: dict) -> str:
        prefix = e["pointer"].rsplit("/", 1)[0]
        return labels.get((e["path"], prefix), e["path"])

    def field(e: dict) -> str:
        return e["pointer"].rsplit("/", 1)[-1]

    lines = [
        f"Automated enrichment of **{report['brand']}**: {len(report['gaps'])} gaps researched.",
        "",
        "Every value below was re-checked by a script, separately from the model: for "
        "numbers, GTINs and article numbers the source was fetched, the quote is on it and "
        "the value is in the quote; datasheet and purchase links were opened and load. "
        "The script can't tell whether a source is about this exact product, so please "
        "spot-check the sources before merging.",
        "",
    ]
    if report["applied"]:
        lines += [
            "## Changes",
            "",
            "| Entity | Field | Was | Now | Source |",
            "|---|---|---|---|---|",
        ]
        for e in report["applied"]:
            secondary = " (secondary)" if e.get("source_type") == "secondary" else ""
            lines.append(
                f"| {label(e)} | `{field(e)}` | {_fmt(e.get('old'))} | {_fmt(e['value'])} | "
                f"[source]({e['source_url']}){secondary} {_quote(e)} |"
            )
        lines.append("")
    if report["confirmed"]:
        lines += ["## Confirmed by a source (unchanged)", ""]
        lines += [
            f"- {label(e)} `{field(e)}` = {_fmt(e['value'])}: [source]({e['source_url']})"
            for e in report["confirmed"]
        ]
        lines.append("")
    if report["unverified"] or report["rejected"]:
        lines += [
            "<details><summary>Proposed but not applied "
            f"({len(report['unverified']) + len(report['rejected'])})</summary>",
            "",
        ]
        for e in report["unverified"] + report["rejected"]:
            lines.append(
                f"- {label(e)} `{field(e)}` = {_fmt(e.get('value'))} "
                f"([source]({e.get('source_url')})): {e['reason']}"
            )
        lines += ["", "</details>", ""]
    if report["not_found"]:
        lines += ["<details><summary>Researched, nothing found</summary>", ""]
        lines += [
            f"- `{n['path']}` {', '.join(n['fields'])}: {n['note']}" for n in report["not_found"]
        ]
        lines += ["", "</details>", ""]
    model = report.get("model") or {}
    if model.get("error"):
        lines.append(f"The model pass failed: {model['error']}")
    elif model:
        lines.append(f"<sub>{model['model']} · ${model['cost_usd']:.2f}</sub>")
    return "\n".join(lines) + "\n"
