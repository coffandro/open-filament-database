"""Tests for enrichment of existing data (ofd/enrich).

The network is replaced by a fake fetcher, so each case states exactly what the
"source page" says and checks that only a value proven by it gets written.
"""

import json
from types import SimpleNamespace

from ofd.enrich.gaps import brand_gaps
from ofd.enrich.run import apply, pr_body
from ofd.enrich.verify import (
    Page,
    gtin_valid,
    normalize,
    number_in,
    quote_on_page,
    site_name,
    verify,
)

TDS = "https://acme.example/tds/pla-basic.pdf"


class FakeFetcher:
    def __init__(self, pages: dict[str, str], pdf=()):
        self.pages = pages
        self.pdf = set(pdf)

    def get(self, url):
        if url not in self.pages:
            return Page(url, False, error="HTTP 403")
        return Page(url, True, normalize(self.pages[url]), is_pdf=url in self.pdf)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def tree(tmp_path, filament=None, sizes=None):
    write(
        tmp_path / "data/acme/brand.json",
        {"id": "acme", "name": "Acme", "website": "https://acme.example"},
    )
    write(tmp_path / "data/acme/PLA/material.json", {"material": "PLA"})
    write(
        tmp_path / "data/acme/PLA/basic/filament.json",
        filament
        or {"id": "basic", "name": "PLA Basic", "diameter_tolerance": 0.02, "density": 1.24},
    )
    write(
        tmp_path / "data/acme/PLA/basic/black/variant.json",
        {"id": "black", "name": "Black", "color_hex": "#000000"},
    )
    write(
        tmp_path / "data/acme/PLA/basic/black/sizes.json",
        sizes or [{"filament_weight": 1000, "diameter": 1.75}],
    )
    write(
        tmp_path / "stores/acmeshop/store.json",
        {"id": "acmeshop", "storefront_url": "https://www.acme.example/"},
    )
    return tmp_path


NO_TEMPLATES = SimpleNamespace(templates={})


def temp(value, quote, pointer="/max_print_temperature", url=TDS):
    return {
        "path": "data/acme/PLA/basic/filament.json",
        "pointer": pointer,
        "value": value,
        "source_url": url,
        "source_quote": quote,
        "source_type": "primary",
    }


# --- verification ---------------------------------------------------------------


def test_quote_must_be_on_the_page_and_value_in_the_quote(tmp_path):
    fetcher = FakeFetcher({TDS: "Printing temperature: 190 – 220 °C. Bed: 50-60 °C."})
    stores = tmp_path / "stores"
    assert verify(temp(220, "Printing temperature: 190 - 220 °C"), fetcher, stores) is None
    assert (
        verify(temp(230, "Printing temperature: 190 - 220 °C"), fetcher, stores)
        == "value is not in the quote"
    )
    assert (
        verify(temp(220, "Nozzle: 190-220"), fetcher, stores)
        == "quote not found on the source page"
    )
    assert "did not load" in verify(temp(220, "x", url="https://blocked.example/"), fetcher, stores)


def test_number_matching_respects_boundaries_and_decimal_commas():
    assert number_in(220, "190-220 °c")
    assert not number_in(22, "190-220 °c")
    assert number_in(1.24, "density 1,24 g/cm3")
    assert not number_in(True, "1")


def test_quote_with_an_ellipsis_matches_in_order():
    page = Page("u", True, normalize("Nozzle temperature 200 °C. Bed temperature 60 °C."))
    assert quote_on_page("Nozzle temperature … Bed temperature 60", page)
    assert not quote_on_page("Bed temperature … Nozzle temperature", page)


def test_gtin_check_digit():
    assert gtin_valid("6975337031901")
    assert not gtin_valid("6975337031902")
    assert not gtin_valid("12345")


def test_purchase_link_must_be_on_the_store_site_and_load(tmp_path):
    stores = tree(tmp_path) / "stores"
    product = "https://acme.example/products/pla-basic-black"
    fetcher = FakeFetcher({product: "PLA Basic Black"})

    def link(url, store="acmeshop"):
        return {"pointer": "/0/purchase_links", "value": [{"store_id": store, "url": url}]}

    assert verify(link(product), fetcher, stores) is None
    assert "not on acmeshop" in verify(link("https://amazon.example/dp/X"), fetcher, stores)
    assert "front page" in verify(link("https://acme.example/"), fetcher, stores)
    assert "not a store" in verify(link(product, "nope"), fetcher, stores)


def test_one_store_covers_its_country_sites():
    assert site_name("amazon.co.uk") == site_name("amazon.com") == site_name("shop.amazon.de")
    assert site_name("amazon.com") != site_name("amzn.to")


def test_datasheet_url_must_be_a_document(tmp_path):
    fetcher = FakeFetcher(
        {TDS: "anything", "https://acme.example/products/pla": "Buy now"}, pdf={TDS}
    )
    stores = tmp_path / "stores"

    def sheet(url):
        return {"pointer": "/data_sheet_url", "value": url, "source_url": url}

    assert verify(sheet(TDS), fetcher, stores) is None
    assert verify(sheet("https://acme.example/products/pla"), fetcher, stores) == (
        "is a shop listing, not a datasheet"
    )


# --- gaps and apply -------------------------------------------------------------


def test_gaps_list_missing_fields_and_template_rechecks(tmp_path):
    root = tree(
        tmp_path,
        filament={
            "id": "basic",
            "name": "PLA Basic",
            "diameter_tolerance": 0.02,
            "density": 1.24,
            "min_print_temperature": 190,
            "max_print_temperature": 230,
            "min_bed_temperature": 50,
            "max_bed_temperature": 70,
        },
    )
    stats = SimpleNamespace(templates={"PLA": {(190, 230, 50, 70): 68}})
    filament_gap, size_gap = brand_gaps(root, "acme", stats, limit=10)
    assert filament_gap.recheck == [
        "min_print_temperature",
        "max_print_temperature",
        "min_bed_temperature",
        "max_bed_temperature",
    ]
    assert "data_sheet_url" in filament_gap.missing
    assert size_gap.prefix == "/0" and size_gap.missing == [
        "gtin",
        "article_number",
        "purchase_links",
    ]


def _report():
    return {"applied": [], "confirmed": [], "unverified": [], "rejected": [], "not_found": []}


def test_apply_writes_only_gap_fields_in_schema_order(tmp_path):
    root = tree(tmp_path)
    gaps = brand_gaps(root, "acme", NO_TEMPLATES, limit=10)
    report = _report()
    apply(
        root,
        [temp(220, "q"), temp("PLA Ultra", "q", pointer="/name")],
        gaps,
        report,
    )
    data = json.loads((root / "data/acme/PLA/basic/filament.json").read_text())
    assert data["max_print_temperature"] == 220
    assert data["name"] == "PLA Basic"
    assert list(data)[-1] == "max_print_temperature"
    assert [r["reason"] for r in report["rejected"]] == ["not one of the gaps for this run"]
    assert report["applied"][0]["old"] is None


def test_apply_drops_values_that_break_the_schema(tmp_path):
    root = tree(tmp_path)
    gaps = brand_gaps(root, "acme", NO_TEMPLATES, limit=10)
    report = _report()
    apply(root, [temp("hot", "q")], gaps, report)
    assert report["applied"] == []
    assert report["rejected"][0]["reason"].startswith("fails the schema")


def test_pr_body_lists_changes_and_what_was_not_applied(tmp_path):
    root = tree(tmp_path)
    gaps = brand_gaps(root, "acme", NO_TEMPLATES, limit=10)
    report = {
        **_report(),
        "brand": "acme",
        "gaps": [vars(g) for g in gaps],
        "applied": [{**temp(220, "Printing temperature 190-220"), "old": None}],
        "unverified": [{**temp(240, "x"), "reason": "quote not found on the source page"}],
        "model": {"model": "claude-haiku-5-5", "cost_usd": 0.03},
    }
    body = pr_body(report)
    assert "| Acme · PLA · PLA Basic | `max_print_temperature` | — | `220` |" in body
    assert "quote not found on the source page" in body
