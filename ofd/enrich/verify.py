"""
Independent proof for every value the model proposes.

The model is asked for the URL it read a value from and the verbatim text containing
it. Here that page is fetched again, without the model, and the value survives only
if the quote is on the page and the value is in the quote. A model that misreads,
guesses or invents fails this, which is the point: Haiku is cheap enough to run on
every brand because a wrong answer is dropped, never merged.

Pages behind bot protection fail too. Those are reported in the PR as "could not
verify" for a human to check, rather than trusted.
"""

import io
import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from ofd.review.checks import looks_like_product_page
from ofd.validation.data_quality import is_storefront_root

USER_AGENT = (
    "Mozilla/5.0 (compatible; OpenFilamentDatabase-enrich/1.0; "
    "+https://github.com/OpenFilamentCollective/open-filament-database)"
)
MAX_BYTES = 15 * 1024 * 1024
TIMEOUT = 25

# pypdf logs a warning for every font it can't fully decode; the text is still usable.
logging.getLogger("pypdf").setLevel(logging.ERROR)

TEMPERATURE_FIELDS = {
    "min_print_temperature",
    "max_print_temperature",
    "min_bed_temperature",
    "max_bed_temperature",
    "max_dry_temperature",
}
SHEET_FIELDS = {"data_sheet_url", "safety_sheet_url"}


@dataclass
class Page:
    url: str
    ok: bool
    text: str = ""
    is_pdf: bool = False
    error: str = ""


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # only needed for PDF sources

    reader = PdfReader(io.BytesIO(data))
    return " ".join(page.extract_text() or "" for page in reader.pages)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[‐-―−]", "-", text)  # dashes and minus signs
    text = text.replace(" ", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


class Fetcher:
    """Fetch each URL once per run."""

    def __init__(self):
        self._cache: dict[str, Page] = {}

    def get(self, url: str) -> Page:
        if url not in self._cache:
            self._cache[url] = self._fetch(url)
        return self._cache[url]

    def _fetch(self, url: str) -> Page:
        if not url.startswith("https://"):
            return Page(url, False, error="not an https URL")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = response.read(MAX_BYTES)
                content_type = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            return Page(url, False, error=f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return Page(url, False, error=str(getattr(exc, "reason", exc)))

        if "pdf" in content_type or data.startswith(b"%PDF"):
            try:
                return Page(url, True, normalize(pdf_text(data)), is_pdf=True)
            except ImportError:
                return Page(url, False, is_pdf=True, error="pypdf is not installed")
            except Exception as exc:  # pypdf raises many kinds on broken files
                return Page(url, False, is_pdf=True, error=f"unreadable PDF ({exc})")
        return Page(url, True, normalize(html_text(data.decode(charset, errors="replace"))))


def quote_on_page(quote: str, page: Page) -> bool:
    """The quote, or each part of it around an ellipsis, appears on the page in order."""
    fragments = [f for f in re.split(r"…|\.\.\.", normalize(quote)) if len(f.strip()) >= 4]
    if not fragments:
        return False
    position = 0
    for fragment in fragments:
        found = page.text.find(fragment.strip(), position)
        if found == -1:
            return False
        position = found + len(fragment.strip())
    return True


def number_in(value, text: str) -> bool:
    """``value`` written in ``text``, as 1.24 or 1,24, not as part of a longer number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    forms = {str(value)}
    if float(value).is_integer():
        forms.add(str(int(value)))
    forms |= {f.replace(".", ",") for f in forms}
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", text) for f in forms)


def gtin_valid(code: str) -> bool:
    """GS1 mod-10 check digit for GTIN-8/12/13/14."""
    if not re.fullmatch(r"\d{8}|\d{12,14}", code):
        return False
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(reversed(digits[:-1]), start=1))
    return (10 - total % 10) % 10 == digits[-1]


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_name(host: str) -> str:
    """``amazon`` for amazon.com, www.amazon.co.uk and amazon.de alike.

    One store entry covers a shop's country sites (the ``amazon`` store links
    amazon.com, .co.uk, .de and .se today), so links are matched on the name, not
    the full host. Two-part country suffixes (co.uk, com.au) are dropped whole.
    """
    labels = host.split(".")
    if (
        len(labels) >= 3
        and labels[-2] in ("co", "com", "org", "net", "ac")
        and len(labels[-1]) == 2
    ):
        labels = labels[:-2]
    else:
        labels = labels[:-1]
    return labels[-1] if labels else host


def _store_host(stores_dir: Path, store_id: str) -> str | None:
    try:
        store = json.loads((stores_dir / store_id / "store.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _host(store.get("storefront_url", "")) or None


def _verify_quoted(enrichment: dict, fetcher: Fetcher) -> str | None:
    """Shared rule: the source loads and the quote is on it."""
    quote = enrichment.get("source_quote") or ""
    if not quote.strip():
        return "no source_quote"
    page = fetcher.get(enrichment.get("source_url", ""))
    if not page.ok:
        return f"source did not load ({page.error})"
    if not quote_on_page(quote, page):
        return "quote not found on the source page"
    return None


def verify(enrichment: dict, fetcher: Fetcher, stores_dir: Path) -> str | None:
    """None when the enrichment is proven, otherwise why it is not."""
    field = enrichment["pointer"].rsplit("/", 1)[-1]
    value = enrichment["value"]
    quote = normalize(enrichment.get("source_quote") or "")

    if field in TEMPERATURE_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 500:
            return "temperature must be a whole number of °C"
        if problem := _verify_quoted(enrichment, fetcher):
            return problem
        return None if number_in(value, quote) else "value is not in the quote"

    if field in SHEET_FIELDS:
        if not isinstance(value, str):
            return "must be a URL"
        page = fetcher.get(value)
        if not page.ok:
            return f"document did not load ({page.error})"
        if not page.is_pdf and looks_like_product_page(value):
            return "is a shop listing, not a datasheet"
        keywords = (
            ("safety data", "sds", "msds")
            if field == "safety_sheet_url"
            else (
                "technical data",
                "data sheet",
                "datasheet",
                "tds",
            )
        )
        if not page.is_pdf and not any(k in page.text for k in keywords):
            return "page does not look like a datasheet"
        return None

    if field == "gtin":
        if not isinstance(value, str) or not gtin_valid(value):
            return "not a valid GTIN (check digit)"
        if problem := _verify_quoted(enrichment, fetcher):
            return problem
        return None if value in quote.replace(" ", "") else "GTIN is not in the quote"

    if field == "article_number":
        if not isinstance(value, str) or not value.strip():
            return "must be a string"
        if problem := _verify_quoted(enrichment, fetcher):
            return problem
        return None if normalize(value) in quote else "article number is not in the quote"

    if field == "purchase_links":
        if not isinstance(value, list) or not value:
            return "must be a non-empty list of links"
        for link in value:
            if not isinstance(link, dict):
                return "each link must be an object"
            url, store_id = link.get("url", ""), link.get("store_id", "")
            store_host = _store_host(stores_dir, store_id)
            if store_host is None:
                return f"store_id {store_id!r} is not a store under stores/"
            if site_name(_host(url)) != site_name(store_host):
                return f"{url} is not on {store_id}'s site ({store_host})"
            if is_storefront_root(url):
                return f"{url} is the shop's front page"
            page = fetcher.get(url)
            if not page.ok:
                return f"{url} did not load ({page.error})"
        return None

    return f"field {field!r} is not one enrichment writes"
