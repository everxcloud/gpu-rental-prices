#!/usr/bin/env python3
"""Integrity check for the GPU rental price dataset (Python standard library only).

Checks every monthly edition in data/ (gpu_rental_prices_YYYY_MM.csv, .json and
summary_YYYY_MM.csv): the price CSV (columns, values), that the JSON file holds the same
rows, and that the summary CSV matches statistics recomputed from the price rows. Pass
one or more editions (YYYY_MM) as arguments to check only those. Exits with status 1 if
any check fails.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EDITION_RE = re.compile(r"gpu_rental_prices_(\d{4}_(?:0[1-9]|1[0-2]))\.csv$")
FIRST_EDITION = "2026_11"
FIRST_EDITION_ROWS = 75
PRICE_COLUMNS = [
    "provider",
    "gpu",
    "offer",
    "price_usd_per_gpu_hour",
    "listing",
    "source_url",
    "retrieved",
]
SUMMARY_COLUMNS = [
    "gpu",
    "offer",
    "providers",
    "min_usd_per_gpu_hour",
    "median_usd_per_gpu_hour",
    "max_usd_per_gpu_hour",
    "spread_max_over_min",
]
GPUS = ["H100 SXM", "H100 PCIe", "H200", "B200", "A100 80GB", "L40S", "RTX 4090", "RTX 5090"]
OFFERS = ["on-demand", "spot-community", "reserved-capacity-block"]
TOLERANCE = 1e-6
SPREAD_TOLERANCE = 0.0005 + 1e-9  # spread is rounded to three decimals; allow exact half-way values

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def edition_files(edition: str) -> tuple[Path, Path, Path]:
    return (
        DATA / f"gpu_rental_prices_{edition}.csv",
        DATA / f"gpu_rental_prices_{edition}.json",
        DATA / f"summary_{edition}.csv",
    )


def check_prices(edition: str) -> list[dict[str, object]]:
    columns, rows = read_csv(edition_files(edition)[0])
    if columns != PRICE_COLUMNS:
        fail(f"price CSV columns {columns} != {PRICE_COLUMNS}")
    if not rows:
        fail("price CSV has no rows")
    if edition == FIRST_EDITION and len(rows) != FIRST_EDITION_ROWS:
        fail(f"price CSV has {len(rows)} rows, expected {FIRST_EDITION_ROWS}")

    records: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    sources: dict[str, str] = {}
    for line, row in enumerate(rows, start=2):
        key = (row["provider"], row["gpu"], row["offer"])
        if key in seen:
            fail(f"line {line}: duplicate provider/GPU/offer {key}")
        seen.add(key)
        if not row["provider"]:
            fail(f"line {line}: empty provider")
        if row["gpu"] not in GPUS:
            fail(f"line {line}: unknown GPU {row['gpu']!r}")
        if row["offer"] not in OFFERS:
            fail(f"line {line}: unknown offer {row['offer']!r}")
        try:
            price = float(row["price_usd_per_gpu_hour"])
        except ValueError:
            fail(f"line {line}: price {row['price_usd_per_gpu_hour']!r} is not a number")
            continue
        if not math.isfinite(price) or price <= 0:
            fail(f"line {line}: price {price} must be a positive number")
        if not row["listing"]:
            fail(f"line {line}: empty listing")
        if not row["source_url"].startswith("https://"):
            fail(f"line {line}: source URL {row['source_url']!r} is not HTTPS")
        if sources.setdefault(row["provider"], row["source_url"]) != row["source_url"]:
            fail(f"line {line}: provider {row['provider']} has more than one source URL")
        try:
            if date.fromisoformat(row["retrieved"]) > date.today():
                fail(f"line {line}: retrieved {row['retrieved']!r} is in the future")
        except ValueError:
            fail(f"line {line}: retrieved {row['retrieved']!r} is not an ISO date")
        records.append({**row, "price_usd_per_gpu_hour": price})
    return records


def check_json(edition: str, records: list[dict[str, object]]) -> None:
    data = json.loads(edition_files(edition)[1].read_text(encoding="utf-8"))
    if not isinstance(data, list):
        fail("JSON file is not an array")
        return
    if len(data) != len(records):
        fail(f"JSON has {len(data)} rows, CSV has {len(records)}")
    for index, (item, record) in enumerate(zip(data, records)):
        if list(item) != PRICE_COLUMNS:
            fail(f"JSON row {index}: keys {list(item)} != {PRICE_COLUMNS}")
        if not isinstance(item.get("price_usd_per_gpu_hour"), (int, float)):
            fail(f"JSON row {index}: price is not a number")
        if item != record:
            fail(f"JSON row {index} differs from CSV row: {item} != {record}")


def expected_summary(records: list[dict[str, object]]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for offer in OFFERS:
        for gpu in GPUS:
            prices = [
                float(r["price_usd_per_gpu_hour"])  # type: ignore[arg-type]
                for r in records
                if r["gpu"] == gpu and r["offer"] == offer
            ]
            if not prices:
                continue
            low, high = min(prices), max(prices)
            summary.append(
                {
                    "gpu": gpu,
                    "offer": offer,
                    "providers": len(prices),
                    "min": low,
                    "median": median(prices),
                    "max": high,
                    "spread": high / low,
                }
            )
    return summary


def check_summary(edition: str, records: list[dict[str, object]]) -> None:
    columns, rows = read_csv(edition_files(edition)[2])
    if columns != SUMMARY_COLUMNS:
        fail(f"summary columns {columns} != {SUMMARY_COLUMNS}")
    expected = expected_summary(records)
    if len(rows) != len(expected):
        fail(f"summary has {len(rows)} rows, expected {len(expected)}")
    for line, (row, want) in enumerate(zip(rows, expected), start=2):
        label = f"summary line {line} ({want['gpu']}, {want['offer']})"
        if (row["gpu"], row["offer"]) != (want["gpu"], want["offer"]):
            fail(f"{label}: found {row['gpu']}, {row['offer']}")
            continue
        try:
            if int(row["providers"]) != want["providers"]:
                fail(f"{label}: providers {row['providers']} != {want['providers']}")
            for column, key in (
                ("min_usd_per_gpu_hour", "min"),
                ("median_usd_per_gpu_hour", "median"),
                ("max_usd_per_gpu_hour", "max"),
            ):
                if abs(float(row[column]) - float(want[key])) > TOLERANCE:  # type: ignore[arg-type]
                    fail(f"{label}: {column} {row[column]} != {want[key]}")
            if abs(float(row["spread_max_over_min"]) - float(want["spread"])) > SPREAD_TOLERANCE:  # type: ignore[arg-type]
                fail(f"{label}: spread {row['spread_max_over_min']} != {want['spread']:.3f}")
        except ValueError as error:
            fail(f"{label}: {error}")


def editions() -> list[str]:
    return sorted(m.group(1) for p in DATA.glob("*.csv") if (m := EDITION_RE.search(p.name)))


def check_edition(edition: str) -> list[dict[str, object]] | None:
    missing = [p for p in edition_files(edition) if not p.is_file()]
    if missing:
        fail(f"missing {', '.join(str(p.relative_to(ROOT)) for p in missing)}")
        return None
    records = check_prices(edition)
    check_json(edition, records)
    check_summary(edition, records)
    return records


def main(argv: list[str]) -> int:
    global errors
    selected = argv or editions()
    if not selected:
        print(f"FAIL: no gpu_rental_prices_YYYY_MM.csv in {DATA.relative_to(ROOT)}")
        return 1
    failed = 0
    for edition in selected:
        errors = []
        if not EDITION_RE.search(f"gpu_rental_prices_{edition}.csv"):
            fail("edition must be YYYY_MM")
            records = None
        else:
            records = check_edition(edition)
        if errors:
            failed += len(errors)
            for message in errors:
                print(f"FAIL [{edition}]: {message}")
            continue
        if records is None:
            continue
        providers = len({r["provider"] for r in records})
        print(
            f"OK [{edition}]: {len(records)} price rows, {providers} providers, "
            "summary and JSON consistent"
        )
    if failed:
        print(f"{failed} error(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
