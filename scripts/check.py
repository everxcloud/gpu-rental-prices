#!/usr/bin/env python3
"""Integrity check for the GPU rental price dataset (Python standard library only).

Verifies the price CSV (row count, columns, values), that the JSON file holds the same
rows, and that the summary CSV matches statistics recomputed from the price rows.
Exits with status 1 if any check fails.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES_CSV = ROOT / "data" / "gpu_rental_prices_2026_11.csv"
PRICES_JSON = ROOT / "data" / "gpu_rental_prices_2026_11.json"
SUMMARY_CSV = ROOT / "data" / "summary_2026_11.csv"

EXPECTED_ROWS = 75
RETRIEVED = "2026-10-26"
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
SPREAD_TOLERANCE = 0.0005  # spread is rounded to three decimals

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


def check_prices() -> list[dict[str, object]]:
    columns, rows = read_csv(PRICES_CSV)
    if columns != PRICE_COLUMNS:
        fail(f"price CSV columns {columns} != {PRICE_COLUMNS}")
    if len(rows) != EXPECTED_ROWS:
        fail(f"price CSV has {len(rows)} rows, expected {EXPECTED_ROWS}")

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
        if row["retrieved"] != RETRIEVED:
            fail(f"line {line}: retrieved {row['retrieved']!r} != {RETRIEVED}")
        try:
            date.fromisoformat(row["retrieved"])
        except ValueError:
            fail(f"line {line}: retrieved {row['retrieved']!r} is not an ISO date")
        records.append({**row, "price_usd_per_gpu_hour": price})
    return records


def check_json(records: list[dict[str, object]]) -> None:
    data = json.loads(PRICES_JSON.read_text(encoding="utf-8"))
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


def check_summary(records: list[dict[str, object]]) -> None:
    columns, rows = read_csv(SUMMARY_CSV)
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


def main() -> int:
    for path in (PRICES_CSV, PRICES_JSON, SUMMARY_CSV):
        if not path.is_file():
            print(f"FAIL: missing {path.relative_to(ROOT)}")
            return 1
    records = check_prices()
    check_json(records)
    check_summary(records)
    if errors:
        for message in errors:
            print(f"FAIL: {message}")
        print(f"{len(errors)} error(s)")
        return 1
    providers = len({r["provider"] for r in records})
    print(f"OK: {len(records)} price rows, {providers} providers, summary and JSON consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
