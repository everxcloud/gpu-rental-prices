#!/usr/bin/env python3
"""Re-read every provider pricing page and write a new monthly edition of the dataset.

For each row of the latest edition, the provider's extractor reads the official pricing
page again and returns the current list price for that (gpu, offer) pair. Extractors never
guess: when a page cannot be fetched or a price cannot be located unambiguously, the row
is reported as unreadable and keeps its previous value, flagged in the report.

Usage:
    python3 scripts/refresh.py                 # dry run: print the report only
    python3 scripts/refresh.py --write         # write data/*_YYYY_MM.{csv,json} + summary
    python3 scripts/refresh.py --report out.md # also write the Markdown report

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EDITION_RE = re.compile(r"gpu_rental_prices_(\d{4})_(\d{2})\.csv$")
USER_AGENT = "Mozilla/5.0 (compatible; EverXCloud-price-dataset/1.0; +https://everxcloud.ai)"
TIMEOUT_SECONDS = 30
PRICE_COLUMNS = [
    "provider",
    "gpu",
    "offer",
    "price_usd_per_gpu_hour",
    "listing",
    "source_url",
    "retrieved",
]
GPUS = ["H100 SXM", "H100 PCIe", "H200", "B200", "A100 80GB", "L40S", "RTX 4090", "RTX 5090"]
OFFERS = ["on-demand", "spot-community", "reserved-capacity-block"]
# A change larger than this ratio is reported as suspicious so the reviewer double-checks it.
SUSPICIOUS_RATIO = 0.5


# --------------------------------------------------------------------------------------
# Page model
# --------------------------------------------------------------------------------------


class _TextCollector(HTMLParser):
    """Collects visible text nodes, skipping script, style and noscript content."""

    _SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._depth:
            self.segments.append(text)


@dataclass(frozen=True)
class Page:
    """A fetched pricing page: raw HTML plus its visible text segments."""

    url: str
    raw: str
    segments: tuple[str, ...]

    @classmethod
    def from_html(cls, url: str, raw: str) -> Page:
        collector = _TextCollector()
        collector.feed(raw)
        collector.close()
        return cls(url=url, raw=raw, segments=tuple(collector.segments))

    @property
    def text(self) -> str:
        """Visible text with segments joined by ' | ' (stable separator for regexes)."""
        return " | ".join(self.segments)


@dataclass(frozen=True)
class Quote:
    """A price read from a page, in USD per GPU-hour, with the listing it came from."""

    price: float
    listing: str | None = None  # None keeps the previous edition's listing text


Key = tuple[str, str]  # (gpu, offer)
Extractor = Callable[[Page], "dict[Key, Quote | str]"]


class Unreadable(Exception):
    """Raised by an extractor when the page layout is not the one it expects."""


def money(value: str) -> float:
    """Parse '$1,234.50' / '1234.5' into a float; raise Unreadable otherwise."""
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        price = float(cleaned)
    except ValueError as error:
        raise Unreadable(f"not a price: {value!r}") from error
    if not 0 < price < 1000:
        raise Unreadable(f"price out of range: {value!r}")
    return price


def one(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    """Return the single match of pattern in text; zero or several matches are unreadable."""
    matches = list(re.finditer(pattern, text, flags))
    if len(matches) != 1:
        raise Unreadable(f"{len(matches)} matches for {pattern!r}")
    return matches[0]


def first(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    """Return the first match of pattern in text; no match is unreadable."""
    match = re.search(pattern, text, flags)
    if match is None:
        raise Unreadable(f"no match for {pattern!r}")
    return match


def per_gpu(total: float, gpus: int) -> float:
    return round(total / gpus, 6)


def gather(readers: dict[Key, Callable[..., Quote]]) -> dict[Key, Quote | str]:
    """Run one reader per (gpu, offer); a reader raising Unreadable yields its reason.

    Readers take no required argument (defaults may bind loop variables).
    """
    results: dict[Key, Quote | str] = {}
    for key, reader in readers.items():
        try:
            results[key] = reader()
        except Unreadable as error:
            results[key] = str(error)
    return results


# --------------------------------------------------------------------------------------
# Provider extractors (one per provider, keyed by the CSV provider name)
# --------------------------------------------------------------------------------------

PRICE = r"\$(\d[\d,]*(?:\.\d+)?)"  # a dollar amount, captured without the "$"


"""Extractors for Runpod, Lambda, Salad and Lium (group A)."""

# --------------------------------------------------------------------------------------
# Runpod: per-GPU Product entries in the JSON-LD block carry Community and Secure prices.
# --------------------------------------------------------------------------------------

_RUNPOD_SLUGS = {
    "H100 SXM": "h100-sxm",
    "H100 PCIe": "h100-pcie",
    "H200": "h200",
    "B200": "b200",
    "L40S": "l40s",
    "RTX 4090": "rtx-4090",
    "RTX 5090": "rtx-5090",
}


def _runpod_products(page: Page) -> dict[str, dict[str, float]]:
    """Map '#gpu-<slug>' to {'Secure Cloud': price, 'Community Cloud': price}."""
    products: dict[str, dict[str, float]] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("@type") == "Product" and "/pricing/#gpu-" in str(node.get("@id")):
                offers = node.get("offers", {})
                prices = {o["name"]: money(str(o["price"])) for o in offers.get("offers", [])}
                products[str(node["@id"]).rsplit("#gpu-", 1)[1]] = prices
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    blocks = re.findall(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", page.raw, re.DOTALL)
    if not blocks:
        raise Unreadable("no JSON-LD block")
    for block in blocks:
        try:
            walk(json.loads(block))
        except (ValueError, KeyError, TypeError) as error:
            raise Unreadable(f"bad JSON-LD: {error}") from error
    return products


def _runpod_price(page: Page, slug: str, cloud: str) -> float:
    prices = _runpod_products(page).get(slug)
    if prices is None or cloud not in prices:
        raise Unreadable(f"no {cloud} offer for {slug}")
    return prices[cloud]


def _runpod_gpu(page: Page, gpu: str, cloud: str) -> Quote:
    price = _runpod_price(page, _RUNPOD_SLUGS[gpu], cloud)
    return Quote(price)


def _runpod_a100_secure(page: Page) -> Quote:
    pcie = _runpod_price(page, "a100-pcie", "Secure Cloud")
    sxm = _runpod_price(page, "a100-sxm", "Secure Cloud")
    if pcie == sxm:
        return Quote(pcie, "Secure Cloud, PCIe and SXM")
    low, high = min(pcie, sxm), max(pcie, sxm)
    variant, other = ("PCIe", "SXM") if pcie <= sxm else ("SXM", "PCIe")
    return Quote(low, f"Secure Cloud, {variant} (cheapest; {other}: ${high:.2f})")


def runpod(page: Page) -> dict[Key, Quote | str]:
    readers: dict[Key, Callable[..., Quote]] = {}
    for gpu in _RUNPOD_SLUGS:
        readers[(gpu, "on-demand")] = lambda g=gpu: _runpod_gpu(page, g, "Secure Cloud")
        readers[(gpu, "spot-community")] = lambda g=gpu: _runpod_gpu(page, g, "Community Cloud")
    readers[("A100 80GB", "on-demand")] = lambda: _runpod_a100_secure(page)
    readers[("A100 80GB", "spot-community")] = lambda: Quote(
        _runpod_price(page, "a100-pcie", "Community Cloud"), "Community Cloud, PCIe"
    )
    return gather(readers)


# --------------------------------------------------------------------------------------
# Lambda: the instances section has one table per tab (8x, 4x, 2x, 1x), in that order.
# --------------------------------------------------------------------------------------

_LAMBDA_TABS = ["8x", "4x", "2x", "1x"]


def _lambda_tables(page: Page) -> dict[str, dict[tuple[str, str], float]]:
    """Return {tab: {(plan, VRAM/GPU): price}} for the 'Instances pricing' section."""
    start = page.raw.find("Instances pricing")
    if start < 0:
        raise Unreadable("no 'Instances pricing' section")
    section = page.raw[start:]
    tabs = re.findall(r'role="tab"[^>]*>([^<]*)<', section[:6000])
    if tabs != _LAMBDA_TABS:
        raise Unreadable(f"unexpected instance tabs {tabs}")
    tables = re.findall(r"<table.*?</table>", section, re.DOTALL)
    if len(tables) < len(_LAMBDA_TABS):
        raise Unreadable(f"{len(tables)} instance tables")
    result: dict[str, dict[tuple[str, str], float]] = {}
    for tab, table in zip(_LAMBDA_TABS, tables):
        if "PRICE/GPU/HR" not in table:
            raise Unreadable(f"{tab} table has no per-GPU price column")
        rows: dict[tuple[str, str], float] = {}
        for row in re.findall(r"<tr.*?</tr>", table, re.DOTALL):
            plan = re.search(r"<th[^>]*>([^<]+)</th>", row)
            vram = re.search(r'data-label="VRAM/GPU"[^>]*>([^<]+)<', row)
            price = re.search(r'data-label="PRICE/GPU/HR\*"[^>]*>\s*(\$[\d.,]+)\s*<', row)
            if plan and vram and price:
                rows[(plan.group(1).strip(), vram.group(1).strip())] = money(price.group(1))
        result[tab] = rows
    return result


def _lambda_price(page: Page, tab: str, plan: str, vram: str) -> float:
    try:
        return _lambda_tables(page)[tab][(plan, vram)]
    except KeyError as error:
        raise Unreadable(f"no {plan} {vram} row in the {tab} table") from error


def _lambda_8x_vs_1x(page: Page, plan8: str, plan1: str, vram: str) -> Quote:
    eight = _lambda_price(page, "8x", plan8, vram)
    one_gpu = _lambda_price(page, "1x", plan1, vram)
    return Quote(eight, f"8x instance, per GPU (1x: ${one_gpu:.2f})")


def lambda_labs(page: Page) -> dict[Key, Quote | str]:
    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: _lambda_8x_vs_1x(
                page, "NVIDIA HGX H100", "NVIDIA H100 SXM", "80 GB"
            ),
            ("H100 PCIe", "on-demand"): lambda: Quote(
                _lambda_price(page, "1x", "NVIDIA H100 PCIe", "80 GB"), "1x instance"
            ),
            ("B200", "on-demand"): lambda: _lambda_8x_vs_1x(
                page, "NVIDIA HGX B200", "NVIDIA B200 SXM6", "180 GB"
            ),
            ("A100 80GB", "on-demand"): lambda: Quote(
                _lambda_price(page, "8x", "NVIDIA HGX A100", "80 GB"), "8x SXM instance, per GPU"
            ),
        }
    )


# --------------------------------------------------------------------------------------
# Salad: one priority-tier table (High, Medium, Low, Lowest) in USD per GPU-hour.
# --------------------------------------------------------------------------------------

_SALAD_HEADER = "GPU | VRAM | High | Medium | Low | Lowest | $/mo (Lowest)"
_SALADPRICE = r"\$(\d+\.\d+)"


def _salad_row(page: Page, gpu: str) -> Quote:
    if _SALAD_HEADER not in page.text:
        raise Unreadable("priority-tier table header changed")
    row = one(
        rf"\| {re.escape(gpu)} \| \d+ GB \| {_SALADPRICE} \| {_SALADPRICE} \| {_SALADPRICE} "
        rf"\| {_SALADPRICE} \| \$[\d,.]+ \|",
        page.text,
    )
    high, lowest = money(row.group(1)), money(row.group(4))
    return Quote(lowest, f"distributed, lowest (batch) priority; high priority ${high:.3f}")


def salad(page: Page) -> dict[Key, Quote | str]:
    return gather(
        {
            ("RTX 4090", "spot-community"): lambda: _salad_row(page, "RTX 4090"),
            ("RTX 5090", "spot-community"): lambda: _salad_row(page, "RTX 5090"),
        }
    )


# --------------------------------------------------------------------------------------
# Lium: the static page states the live ask range; the lowest ask is the marketplace price.
# --------------------------------------------------------------------------------------


def _lium_lowest_ask(page: Page) -> Quote:
    shown = one(
        r"Price per GPU-hour \| \$(\d+\.\d+) – \$(\d+\.\d+) \| Live asks across available pods",
        page.text,
    )
    low = money(shown.group(1))
    smallest = one(r"Smallest rental: 1 GPU at \$(\d+\.\d+)/hour", page.text)
    schema_low = one(r'"lowPrice":"(\d+\.\d+)"', page.raw)
    if not (low == money(smallest.group(1)) == money(schema_low.group(1))):
        raise Unreadable("lowest ask differs between page sections")
    return Quote(low)


def lium(page: Page) -> dict[Key, Quote | str]:
    return gather({("RTX 4090", "spot-community"): lambda: _lium_lowest_ask(page)})


"""Extractors for group B: Verda, Together AI, CoreWeave, AWS Capacity Blocks."""


# ---- Verda (DataCrunch) ----------------------------------------------------------------


def _verda_row(page: Page, hardware: str) -> tuple[float, float]:
    """(on-demand, spot) of a 1x row in the self-service GPU instances table."""
    start = one(r"GPU instances \| 1x, 2x, 4x, or 8x", page.text).end()
    end = first(r"\| Confidential computing", page.text[start:]).start() + start
    table = page.text[start:end]
    rows = re.findall(
        rf"\b1x {hardware} \| \d+ \| \d+ GB \| \d+ GB \| {PRICE}/h \| {PRICE}/h", table
    )
    if not rows:
        raise Unreadable(f"no 1x {hardware} row in the GPU instances table")
    if len(set(rows)) != 1:  # the same GPU may be listed with several CPU/RAM shapes
        raise Unreadable(f"conflicting prices for 1x {hardware}: {sorted(set(rows))}")
    return money(rows[0][0]), money(rows[0][1])


def verda_datacrunch(page: Page) -> dict[Key, Quote | str]:
    gpus = {
        "H100 SXM": "H100 SXM5 80GB",
        "H200": "H200 SXM5 141GB",
        "B200": "B200 SXM6 180GB",
        "A100 80GB": "A100 SXM4 80GB",
        "L40S": "L40S 48GB",
    }
    readers: dict[Key, Callable[..., Quote]] = {}
    for gpu, hardware in gpus.items():
        readers[(gpu, "on-demand")] = lambda h=hardware: Quote(_verda_row(page, h)[0])
        readers[(gpu, "spot-community")] = lambda h=hardware: Quote(_verda_row(page, h)[1])
    return gather(readers)


# ---- Together AI -----------------------------------------------------------------------


def _together_cluster_row(page: Page, hardware: str) -> tuple[float, float]:
    """(preemptible, on-demand pay-as-you-go) from the GPU Clusters table."""
    start = one(r"GPU Clusters \| On-demand hourly rates and reserved capacity", page.text).end()
    end = first(r"\| Need more GPU capacity\?", page.text[start:]).start() + start
    row = one(
        rf"NVIDIA \| {hardware} \| {PRICE} \| Checking… \| {PRICE} \| Checking… \|",
        page.text[start:end],
    )
    return money(row.group(1)), money(row.group(2))


def together_ai(page: Page) -> dict[Key, Quote | str]:
    readers: dict[Key, Callable[..., Quote]] = {}
    for gpu, hardware in {"H100 SXM": "HGX H100", "H200": "HGX H200", "B200": "HGX B200"}.items():
        readers[(gpu, "on-demand")] = lambda h=hardware: Quote(_together_cluster_row(page, h)[1])
        readers[(gpu, "spot-community")] = lambda h=hardware: Quote(
            _together_cluster_row(page, h)[0]
        )
    return gather(readers)


# ---- CoreWeave -------------------------------------------------------------------------


def _coreweave_total(page: Page, model: str) -> float:
    """8x instance on-demand total in the North America on-demand table."""
    start = one(r"REGION: NORTH AMERICA \| GPU Model", page.text).end()
    end = first(r"REGION: EUROPE", page.text[start:]).start() + start
    section = page.text[start:end]
    card = one(rf"NVIDIA {model} \| On-Demand Price: \| {PRICE} \| / Hour", section)
    return money(card.group(1))


def _coreweave_quote(page: Page, model: str, label: str) -> Quote:
    total = _coreweave_total(page, model)
    return Quote(per_gpu(total, 8), listing=f"{label}8x instance ${total:.2f} / 8, North America")


def coreweave(page: Page) -> dict[Key, Quote | str]:
    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: _coreweave_quote(page, "HGX H100", "HGX H100, "),
            ("H200", "on-demand"): lambda: _coreweave_quote(page, "HGX H200", "HGX H200, "),
            ("B200", "on-demand"): lambda: _coreweave_quote(page, "HGX B200", "HGX B200, "),
            ("A100 80GB", "on-demand"): lambda: _coreweave_quote(page, "A100", ""),
            ("L40S", "on-demand"): lambda: _coreweave_quote(page, "L40S", ""),
        }
    )


# ---- AWS EC2 Capacity Blocks -----------------------------------------------------------


def _aws_quote(page: Page, instance: str, region: str, accelerator: str) -> Quote:
    """Per-accelerator rate of one instance type in one region (checked against total / 8)."""
    row = one(
        rf"\| {re.escape(instance)} \| {re.escape(region)} \| {PRICE} USD \({PRICE} USD\) "
        rf"\| 8 x {accelerator} \|",
        page.text,
    )
    total, listed = money(row.group(1)), money(row.group(2))
    price = per_gpu(total, 8)
    if abs(price - listed) > 0.001:  # page's own per-accelerator figure must agree
        raise Unreadable(f"{instance} {region}: ${total} / 8 != listed ${listed}")
    return Quote(listed, listing=f"{instance} Capacity Block, {region}, ${total} / 8")


def aws(page: Page) -> dict[Key, Quote | str]:
    off = "reserved-capacity-block"
    return gather(
        {
            ("H100 SXM", off): lambda: _aws_quote(
                page, "p5.48xlarge", "US East (N. Virginia)", "H100"
            ),
            ("H200", off): lambda: _aws_quote(page, "p5e.48xlarge", "US East (Ohio)", "H200"),
            ("B200", off): lambda: _aws_quote(
                page, "p6-b200.48xlarge", "US East (N. Virginia)", "B200"
            ),
            ("A100 80GB", off): lambda: _aws_quote(
                page, "p4de.24xlarge", "US East (N. Virginia)", "A100"
            ),
        }
    )


def _section(text: str, start: str, end: str, what: str) -> str:
    """Return the text between a start marker and the next end marker (both must exist)."""
    begin = one(start, text).end()
    stop = re.search(end, text[begin:])
    if stop is None:
        raise Unreadable(f"{what}: end marker not found")
    return text[begin : begin + stop.start()]


def nebius(page: Page) -> dict[Key, Quote | str]:
    # Table columns: GPU | vCPUs | RAM | Spot, GPU-hour | On-demand, GPU-hour
    table = _section(
        page.text,
        r"Preemptible, GPU-hour \* \| On-demand, GPU-hour",
        r"\* Spot prices",
        "Nebius GPU table",
    )

    def row(name: str, prefix: str = "") -> Quote:
        pattern = (
            rf"NVIDIA {name} \| [\d-]+ \| [\d-]+ \| (?:from {PRICE}\*{{0,2}}|-) \| "
            rf"{prefix}{PRICE}"
        )
        return Quote(money(one(pattern, table).group(2)))

    return gather(
        {
            ("H200", "on-demand"): lambda: row("HGX H200"),
            ("B200", "on-demand"): lambda: row("HGX B200"),
            ("L40S", "on-demand"): lambda: row("L40S with Intel CPU", "from "),
        }
    )


def digitalocean(page: Page) -> dict[Key, Quote | str]:
    def card(name: str) -> Quote:
        pattern = rf"NVIDIA {name} \| {PRICE} \| /GPU/hour \| On-Demand Price"
        return Quote(money(one(pattern, page.text).group(1)))

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: card("HGX H100"),
            ("H200", "on-demand"): lambda: card("HGX H200"),
            ("L40S", "on-demand"): lambda: card("L40S"),
        }
    )


def hyperstack(page: Page) -> dict[Key, Quote | str]:
    on_demand = _section(
        page.text,
        r"On-Demand GPU \| Pricing \| GPU Model \| VRAM \(GB\) \| Max pCPUs per GPU \| "
        r"Max RAM \(GB\) per GPU \| Pricing Per Hour",
        r"\| Reservation \| Pricing",
        "Hyperstack on-demand table",
    )
    spot = _section(
        page.text,
        r"Spot VM \| Pricing \| Learn More \| GPU Model \| Spot VM",
        r"\| (?:Contact|Get|Frequently|Why|Ready)|$",
        "Hyperstack spot table",
    )

    def row(name: str) -> Quote:
        pattern = rf"NVIDIA {name} \| \d+ \| \d+ \| \d+ \| {PRICE}"
        return Quote(money(one(pattern, on_demand).group(1)))

    def spot_row(name: str) -> Quote:
        return Quote(money(one(rf"NVIDIA {name} \| {PRICE}", spot).group(1)))

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: row("H100 SXM"),
            ("H200", "on-demand"): lambda: row("H200 SXM"),
            ("B200", "on-demand"): lambda: row("B200"),
            ("A100 80GB", "on-demand"): lambda: row("A100"),
            ("A100 80GB", "spot-community"): lambda: spot_row("A100"),
        }
    )


def thunder_compute(page: Page) -> dict[Key, Quote | str]:
    def instance(name: str, launch: str) -> Quote:
        pattern = (
            rf"\| {name} \| \d+ GB \| [^|]+ \| [^|]+ \| [^|]+ \| {PRICE} \| /hr \| Launch {launch}"
        )
        return Quote(money(one(pattern, page.text).group(1)))

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: instance("H100 SXM", "H100"),
            ("A100 80GB", "on-demand"): lambda: instance("A100 80GB", "A100"),
        }
    )


def _visible_price(page: Page, pattern: str) -> Quote:
    """Read the single price captured by pattern in the visible text."""
    return Quote(money(one(pattern, page.text).group(1)))


def jarvislabs(page: Page) -> dict[Key, Quote | str]:
    # Table row: name | VRAM | GB | vCPUs | RAM | GB | on-demand price (1 GPU per instance)
    def row(name: str) -> Quote:
        specs = r"\d+ \| GB \| \d+ \| \d+ \| GB"
        pattern = rf"NVIDIA {name} \| {specs} \| \$([\d,.]+) \| [−-] \| \d+ \| % \|"
        return _visible_price(page, pattern)

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: row("H100 SXM"),
            ("H200", "on-demand"): lambda: row("H200 SXM"),
            ("A100 80GB", "on-demand"): lambda: row("A100 80GB"),
        }
    )


def massed_compute(page: Page) -> dict[Key, Quote | str]:
    # Single-GPU row: name | × | 1 | $price | /hr
    def x1(name: str) -> Quote:
        return _visible_price(page, rf"(?:^| \| ){re.escape(name)} \| × \| 1 \| \$([\d,.]+) \| /hr")

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: x1("H100 SXM5 (80GB)"),
            ("H200", "on-demand"): lambda: x1("H200 NVL (141GB)"),
            ("A100 80GB", "on-demand"): lambda: x1("A100 (80GB)"),
            ("L40S", "on-demand"): lambda: x1("L40S (48GB)"),
        }
    )


def oblivus(page: Page) -> dict[Key, Quote | str]:
    # Summary list: name | specs | On-Demand: $price/hr
    def entry(name: str) -> Quote:
        return _visible_price(
            page,
            rf"(?:^| \| ){re.escape(name)} \| [^|]*\| On-Demand: \$([\d,.]+)/hr",
        )

    return gather(
        {
            ("H100 SXM", "on-demand"): lambda: entry("H100 80GB SXM5"),
            ("H100 PCIe", "on-demand"): lambda: entry("H100 80GB PCIE"),
            ("H200", "on-demand"): lambda: entry("H200 141GB SXM5"),
            ("A100 80GB", "on-demand"): lambda: entry("A100 80GB PCIE"),
            ("RTX 4090", "on-demand"): lambda: entry("RTX 4090"),
        }
    )


def cloudrift(page: Page) -> dict[Key, Quote | str]:
    # Pricing cards: GPU | $price | / | hr | VRAM line
    def card(name: str, vram: str) -> Quote:
        pattern = rf"(?:^| \| ){name} \| \$([\d,.]+) \| / \| hr \| {vram} GDDR\w+ VRAM"
        return _visible_price(page, pattern)

    return gather(
        {
            ("RTX 4090", "on-demand"): lambda: card("RTX 4090", "24 GB"),
            ("RTX 5090", "on-demand"): lambda: card("RTX 5090", "32 GB"),
        }
    )


EXTRACTORS: dict[str, Extractor] = {
    "AWS": aws,
    "CloudRift": cloudrift,
    "CoreWeave": coreweave,
    "DigitalOcean": digitalocean,
    "Hyperstack": hyperstack,
    "JarvisLabs": jarvislabs,
    "Lambda": lambda_labs,
    "Lium": lium,
    "Massed Compute": massed_compute,
    "Nebius": nebius,
    "Oblivus": oblivus,
    "Runpod": runpod,
    "Salad": salad,
    "Thunder Compute": thunder_compute,
    "Together AI": together_ai,
    "Verda (DataCrunch)": verda_datacrunch,
}


# --------------------------------------------------------------------------------------
# Refresh pipeline
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """Result of re-reading one row: the new row, and why it was not read (if unreadable)."""

    row: dict[str, str]
    previous: dict[str, str]
    reason: str | None  # None when the price was read from the page

    @property
    def changed(self) -> bool:
        return self.reason is None and price_of(self.row) != price_of(self.previous)


def price_of(row: dict[str, str]) -> float:
    return float(row["price_usd_per_gpu_hour"])


def format_price(price: float) -> str:
    """Shortest decimal form, as in the published CSV (6.0 -> '6', 3.50 -> '3.5')."""
    text = f"{price:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def latest_edition(data_dir: Path = DATA) -> tuple[str, Path]:
    """Return ('YYYY_MM', path) of the newest price CSV in data_dir."""
    editions = sorted(
        (match.group(1) + "_" + match.group(2), path)
        for path in data_dir.glob("gpu_rental_prices_*.csv")
        if (match := EDITION_RE.search(path.name))
    )
    if not editions:
        raise SystemExit(f"no gpu_rental_prices_YYYY_MM.csv in {data_dir}")
    return editions[-1]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != PRICE_COLUMNS:
            raise SystemExit(f"{path.name}: unexpected columns {reader.fieldnames}")
        return list(reader)


def fetch(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS source URL {url!r}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        body: bytes = response.read()
    return body.decode(charset, errors="replace")


def read_provider(
    provider: str, url: str, fetcher: Callable[[str], str]
) -> dict[Key, Quote | str] | str:
    """Fetch and parse one provider page; return quotes, or a reason string for the page."""
    extractor = EXTRACTORS.get(provider)
    if extractor is None:
        return "no extractor for this provider"
    try:
        page = Page.from_html(url, fetcher(url))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return f"page not fetched: {error}"
    try:
        return extractor(page)
    except Unreadable as error:
        return f"page layout not recognized: {error}"


def refresh(
    rows: list[dict[str, str]], retrieved: str, fetcher: Callable[[str], str] = fetch
) -> list[Outcome]:
    """Re-read every row; unreadable rows keep their previous price, listing and date."""
    pages: dict[tuple[str, str], dict[Key, Quote | str] | str] = {}
    outcomes: list[Outcome] = []
    for previous in rows:
        page_key = (previous["provider"], previous["source_url"])
        if page_key not in pages:
            pages[page_key] = read_provider(*page_key, fetcher)
        quotes = pages[page_key]
        quote = (
            quotes
            if isinstance(quotes, str)
            else quotes.get((previous["gpu"], previous["offer"]), "no reader for this row")
        )
        if isinstance(quote, str):
            outcomes.append(Outcome(row=dict(previous), previous=previous, reason=quote))
            continue
        row = {
            **previous,
            "price_usd_per_gpu_hour": format_price(quote.price),
            "listing": quote.listing if quote.listing is not None else previous["listing"],
            "retrieved": retrieved,
        }
        outcomes.append(Outcome(row=row, previous=previous, reason=None))
    return outcomes


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def summarize(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Per (gpu, offer) statistics, in the same order and format as the published summary."""
    summary: list[dict[str, str]] = []
    for offer in OFFERS:
        for gpu in GPUS:
            prices = [price_of(r) for r in rows if r["gpu"] == gpu and r["offer"] == offer]
            if not prices:
                continue
            low, high = min(prices), max(prices)
            summary.append(
                {
                    "gpu": gpu,
                    "offer": offer,
                    "providers": str(len(prices)),
                    "min_usd_per_gpu_hour": repr(round(low, 6)),
                    "median_usd_per_gpu_hour": repr(round(median(prices), 6)),
                    "max_usd_per_gpu_hour": repr(round(high, 6)),
                    "spread_max_over_min": f"{high / low:.3f}",
                }
            )
    return summary


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_edition(rows: list[dict[str, str]], edition: str, data_dir: Path = DATA) -> list[Path]:
    prices_csv = data_dir / f"gpu_rental_prices_{edition}.csv"
    prices_json = data_dir / f"gpu_rental_prices_{edition}.json"
    summary_csv = data_dir / f"summary_{edition}.csv"
    write_csv(prices_csv, rows)
    records = [{**r, "price_usd_per_gpu_hour": price_of(r)} for r in rows]
    prices_json.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(summary_csv, summarize(rows))
    return [prices_csv, prices_json, summary_csv]


def md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def report(outcomes: list[Outcome], previous_edition: str, edition: str) -> str:
    """Markdown report for the pull request body."""
    readable = [o for o in outcomes if o.reason is None]
    changed = [o for o in readable if o.changed]
    unreadable = [o for o in outcomes if o.reason is not None]
    lines = [
        f"Automated refresh of edition `{previous_edition}` into `{edition}`.",
        "",
        f"- Rows read from the provider pages: **{len(readable)} of {len(outcomes)}**",
        f"- Prices changed: **{len(changed)}**",
        f"- Unreadable rows (previous price kept, needs a manual check): **{len(unreadable)}**",
        "",
        "Review every changed and unreadable row against its source page before merging.",
    ]
    if changed:
        lines += [
            "",
            "## Changed prices",
            "",
            "| Provider | GPU | Offer | Previous | New | Change | Listing |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for o in changed:
            old, new = price_of(o.previous), price_of(o.row)
            ratio = (new - old) / old
            flag = " ⚠️" if abs(ratio) > SUSPICIOUS_RATIO else ""
            lines.append(
                f"| {md_cell(o.row['provider'])} | {o.row['gpu']} | {o.row['offer']} "
                f"| {o.previous['price_usd_per_gpu_hour']} | {o.row['price_usd_per_gpu_hour']} "
                f"| {ratio:+.1%}{flag} | {md_cell(o.row['listing'])} |"
            )
    if unreadable:
        lines += [
            "",
            "## Unreadable rows (previous value kept)",
            "",
            "| Provider | GPU | Offer | Kept price | Read on | Reason | Source |",
            "|---|---|---|---:|---|---|---|",
        ]
        for o in unreadable:
            lines.append(
                f"| {md_cell(o.row['provider'])} | {o.row['gpu']} | {o.row['offer']} "
                f"| {o.row['price_usd_per_gpu_hour']} | {o.row['retrieved']} "
                f"| {md_cell(o.reason or '')} | {o.row['source_url']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="write the new edition files")
    parser.add_argument("--edition", help="edition to write, YYYY_MM (default: current month)")
    parser.add_argument("--report", type=Path, help="write the Markdown report to this file")
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    edition = args.edition or f"{today.year}_{today.month:02d}"
    if not re.fullmatch(r"\d{4}_(0[1-9]|1[0-2])", edition):
        parser.error(f"invalid edition {edition!r}, expected YYYY_MM")
    previous_edition, previous_path = latest_edition()
    if args.write and edition <= previous_edition:
        parser.error(f"edition {edition} is not newer than the latest one ({previous_edition})")

    outcomes = refresh(read_rows(previous_path), retrieved=today.isoformat())
    text = report(outcomes, previous_edition, edition)
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    if args.write:
        for path in write_edition([o.row for o in outcomes], edition):
            print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
