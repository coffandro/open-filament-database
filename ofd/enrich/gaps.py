"""
What a brand's existing entries are missing.

A gap is one entity (a filament, or one spool row in a sizes.json) and the fields
worth researching on it: fields that are absent, and temperatures that are still the
generic per-material template most brands share (see ``MaterialStats``). Only these
pointers may be written by an enrichment run; anything else the model proposes is
dropped, so a run can never quietly rewrite data that was already sourced.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ofd.review.checks import TEMPLATE_FIELDS, MaterialStats, material_of

FILAMENT_FIELDS = (
    *TEMPLATE_FIELDS,
    "max_dry_temperature",
    "data_sheet_url",
    "safety_sheet_url",
)
SIZE_FIELDS = ("gtin", "article_number", "purchase_links")


@dataclass
class Gap:
    path: str  # repo-relative file
    prefix: str  # JSON pointer of the entity inside the file ("" or "/<index>")
    label: str  # human-readable, for the prompt and the PR body
    missing: list[str] = field(default_factory=list)
    # Fields that hold a value, but one that looks copied from the template.
    recheck: list[str] = field(default_factory=list)
    current: dict = field(default_factory=dict)

    @property
    def fields(self) -> list[str]:
        return [*self.missing, *self.recheck]

    def pointers(self) -> set[str]:
        return {f"{self.prefix}/{f}" for f in self.fields}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def brand_gaps(root: Path, brand: str, stats: MaterialStats, limit: int) -> list[Gap]:
    """Gaps for ``brand``: filaments first (the data that matters most), then spools."""
    brand_dir = root / "data" / brand
    brand_data = _load(brand_dir / "brand.json") or {}
    brand_name = brand_data.get("name", brand)
    filament_gaps: list[Gap] = []
    size_gaps: list[Gap] = []

    for filament_file in sorted(brand_dir.glob("*/*/filament.json")):
        filament = _load(filament_file)
        if not isinstance(filament, dict) or filament.get("discontinued"):
            continue
        rel = str(filament_file.relative_to(root))
        material = material_of(rel)
        label = f"{brand_name} · {material} · {filament.get('name', filament_file.parent.name)}"

        gap = Gap(rel, "", label, current={k: filament.get(k) for k in FILAMENT_FIELDS})
        gap.missing = [f for f in FILAMENT_FIELDS if filament.get(f) in (None, "")]
        combo = tuple(filament.get(f) for f in TEMPLATE_FIELDS)
        if combo in stats.templates.get(material, {}):
            gap.recheck = list(TEMPLATE_FIELDS)
        if gap.fields:
            filament_gaps.append(gap)

        for sizes_file in sorted(filament_file.parent.glob("*/sizes.json")):
            variant = _load(sizes_file.parent / "variant.json") or {}
            if variant.get("discontinued"):
                continue
            sizes = _load(sizes_file)
            if not isinstance(sizes, list):
                continue
            for index, size in enumerate(sizes):
                if not isinstance(size, dict) or size.get("discontinued"):
                    continue
                missing = [f for f in SIZE_FIELDS if not size.get(f)]
                if size.get("ean"):  # deprecated alias, already a barcode
                    missing = [f for f in missing if f != "gtin"]
                if not missing:
                    continue
                refill = " refill" if size.get("container_material") == "refill" else ""
                size_gaps.append(
                    Gap(
                        str(sizes_file.relative_to(root)),
                        f"/{index}",
                        f"{label} · {variant.get('name', sizes_file.parent.name)} · "
                        f"{size.get('filament_weight')} g {size.get('diameter')} mm{refill}",
                        missing,
                        current={k: size.get(k) for k in (*SIZE_FIELDS, "ean")},
                    )
                )

    return (filament_gaps + size_gaps)[:limit]
