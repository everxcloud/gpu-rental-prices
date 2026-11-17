"""Offline tests for scripts/refresh.py (standard library only, no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh  # noqa: E402

FIRST_EDITION = ROOT / "data" / "gpu_rental_prices_2026_11.csv"


class PageTest(unittest.TestCase):
    def test_visible_text_skips_scripts_and_styles(self) -> None:
        page = refresh.Page.from_html(
            "https://example.com",
            "<p>H100</p><script>var p = '$9.99';</script><style>.a{}</style><b> $2.50 </b>",
        )
        self.assertEqual(page.text, "H100 | $2.50")

    def test_one_rejects_ambiguous_matches(self) -> None:
        with self.assertRaises(refresh.Unreadable):
            refresh.one(r"\$(\d+)", "$1 | $2")
        self.assertEqual(refresh.one(r"\$(\d+)", "$1 | x").group(1), "1")

    def test_money_rejects_non_prices(self) -> None:
        self.assertEqual(refresh.money("$113.666"), 113.666)
        for value in ("abc", "0", "$-1", "$1,234.50"):
            with self.assertRaises(refresh.Unreadable):
                refresh.money(value)

    def test_format_price_matches_published_style(self) -> None:
        self.assertEqual(refresh.format_price(6.0), "6")
        self.assertEqual(refresh.format_price(3.5), "3.5")
        self.assertEqual(refresh.format_price(0.9135), "0.9135")


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = refresh.read_rows(FIRST_EDITION)

    def test_extractor_registered_for_every_provider(self) -> None:
        providers = {row["provider"] for row in self.rows}
        self.assertEqual(providers, set(refresh.EXTRACTORS))

    def test_writer_reproduces_published_edition_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for path in refresh.write_edition(self.rows, "2026_11", Path(tmp)):
                self.assertEqual(path.read_bytes(), (ROOT / "data" / path.name).read_bytes())

    def test_unreadable_pages_keep_previous_values(self) -> None:
        def broken_fetch(url: str) -> str:
            raise OSError("connection refused")

        outcomes = refresh.refresh(self.rows, "2026-12-01", fetcher=broken_fetch)
        self.assertEqual([o.row for o in outcomes], self.rows)
        self.assertTrue(all(o.reason and "not fetched" in o.reason for o in outcomes))
        text = refresh.report(outcomes, "2026_11", "2026_12")
        self.assertIn("**0 of 75**", text)
        self.assertIn("## Unreadable rows", text)

    def test_unrecognized_layout_is_unreadable_not_guessed(self) -> None:
        outcomes = refresh.refresh(
            self.rows, "2026-12-01", fetcher=lambda url: "<p>H100 SXM $1.00 /hr</p>"
        )
        self.assertTrue(all(o.reason is not None for o in outcomes))
        self.assertEqual([o.row for o in outcomes], self.rows)

    def test_read_price_updates_row_and_date(self) -> None:
        aws_rows = [r for r in self.rows if r["provider"] == "AWS"]

        def fake_extractor(page: refresh.Page) -> dict[refresh.Key, refresh.Quote | str]:
            return {("H100 SXM", "reserved-capacity-block"): refresh.Quote(6.5, "new listing")}

        original = refresh.EXTRACTORS["AWS"]
        refresh.EXTRACTORS["AWS"] = fake_extractor
        try:
            outcomes = refresh.refresh(aws_rows, "2026-12-01", fetcher=lambda url: "")
        finally:
            refresh.EXTRACTORS["AWS"] = original
        first, *others = outcomes
        self.assertEqual(first.reason, None)
        self.assertTrue(first.changed)
        self.assertEqual(first.row["price_usd_per_gpu_hour"], "6.5")
        self.assertEqual(first.row["listing"], "new listing")
        self.assertEqual(first.row["retrieved"], "2026-12-01")
        self.assertTrue(all(o.reason == "no reader for this row" for o in others))
        self.assertIn("+8.9%", refresh.report(outcomes, "2026_11", "2026_12"))


if __name__ == "__main__":
    unittest.main()
