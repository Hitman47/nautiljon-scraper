import json
import os
import tempfile
import types
import unittest

from scraper_nautiljon import NautiljonScraper


def make_row(label: str):
    slug = "hash" if label == "#" else label.lower()
    return {
        "url_fiche": f"https://www.nautiljon.com/mangas/test-{slug}.html",
        "titre": f"Test {label}",
        "extraction_time": "2026-01-01 00:00:00",
    }


class DiffStateTests(unittest.TestCase):
    def make_scraper(self, out_dir: str) -> NautiljonScraper:
        return NautiljonScraper(out_dir=out_dir, delay=0)

    def seed_letter(self, scraper: NautiljonScraper, letter: str) -> None:
        label = scraper._letter_label(letter)
        scraper.save_letter_files(scraper._letter_tag(letter), [make_row(label)], partial=False)

    def install_fake_letter_scrape(self, scraper: NautiljonScraper, outcomes, calls=None) -> None:
        def fake(this, letter, **kwargs):
            label = this._letter_label(letter)
            if calls is not None:
                calls.append(label)
            outcome = outcomes.get(label, "success")
            flags = {
                "listing_failed": outcome == "listing_failed",
                "detail_failed": outcome == "detail_failed",
                "limited": outcome == "limited",
            }
            if outcome in {"listing_failed", "detail_failed"}:
                this.session_stats["errors"] += 1
            this.session_stats["diff_by_letter"][label] = flags
            rows = [make_row(label)]
            this.session_stats["series_by_letter"][label] = len(rows)
            return rows

        scraper.scrape_letter_diff = types.MethodType(fake, scraper)

    def test_blocked_first_letter_does_not_mark_success(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_letter(scraper, "a")
            self.install_fake_letter_scrape(scraper, {"A": "listing_failed"})

            result = scraper.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
                abort_after_listing_failures=1,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "listing_inaccessible")
            self.assertFalse(os.path.exists(scraper._last_success_path("diff")))
            state = scraper._load_json_dict(scraper._state_path("last_diff_run"))
            self.assertEqual(state["status"], "failed")

    def test_subset_is_partial_and_never_monthly_success(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_letter(scraper, "a")
            self.install_fake_letter_scrape(scraper, {})

            result = scraper.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "partial")
            self.assertEqual(result.reason, "controlled_subset_complete")
            self.assertFalse(os.path.exists(scraper._last_success_path("diff")))

    def test_complete_catalog_writes_valid_success(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            for letter in scraper.get_all_letters():
                self.seed_letter(scraper, letter)
            self.install_fake_letter_scrape(scraper, {})

            result = scraper.scrape_all_letters_diff(
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(len(result.completed_letters), 27)
            self.assertTrue(scraper._validate_final_exports(result.export_paths))
            success = scraper._load_last_success("diff")
            self.assertEqual(success["status"], "success")
            self.assertEqual(success["rows_count"], 27)

    def test_invalid_old_success_marker_is_ignored(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper._write_json_atomic(scraper._last_success_path("diff"), {
                "completed_at": "2026-07-28T12:00:00",
                "rows_count": 100,
                "export_paths": {},
            })

            should_skip, _, _ = scraper.should_skip_recent_success("diff", 30)

            self.assertFalse(should_skip)

    def test_run_checkpoint_skips_completed_letters(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_letter(scraper, "a")
            self.seed_letter(scraper, "b")
            config = scraper._diff_run_config(["a", "b"], None, None, None, True)
            scraper._save_diff_run_checkpoint(config, ["A"])
            calls = []
            self.install_fake_letter_scrape(scraper, {}, calls=calls)

            result = scraper.scrape_all_letters_diff(
                letters=["a", "b"],
                min_days_between_diff_exports=0,
                resume=True,
            )

            self.assertEqual(calls, ["B"])
            self.assertEqual(result.completed_letters, ["A", "B"])
            self.assertEqual(result.status, "partial")

    def test_letter_resume_retries_exact_failed_page(self):
        with tempfile.TemporaryDirectory() as out_dir:
            first = self.make_scraper(out_dir)
            self.seed_letter(first, "a")
            row = make_row("A")
            attempts = []

            def first_fetch(this, letter, page_num):
                attempts.append(page_num)
                if page_num == 0:
                    return "https://example.test/a?page=0", [row]
                raise RuntimeError("listing blocked")

            first.fetch_listing_page = types.MethodType(first_fetch, first)
            first.scrape_letter_diff("a", drop_missing=False, resume=True)
            checkpoint = first._load_json_dict(first._letter_checkpoint_path("A"))
            self.assertEqual(attempts, [0, 1, 1, 1])
            self.assertEqual(checkpoint["page_num"], 1)

            second = self.make_scraper(out_dir)
            resumed_pages = []

            def second_fetch(this, letter, page_num):
                resumed_pages.append(page_num)
                return f"https://example.test/a?page={page_num}", []

            second.fetch_listing_page = types.MethodType(second_fetch, second)
            second.scrape_letter_diff("a", drop_missing=False, resume=True)

            self.assertEqual(resumed_pages[0], 1)
            self.assertFalse(os.path.exists(second._letter_checkpoint_path("A")))

    def test_diagnose_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = os.path.join(temp_dir, "must-not-exist")
            scraper = self.make_scraper(out_dir)

            def fake_endpoint(this, label, url, kind="generic", expected_letter=None):
                return {
                    "label": label,
                    "url": url,
                    "ok": True,
                    "status_code": 200,
                    "waf_blocked": False,
                    "rows": 50 if kind == "listing" else None,
                    "parsed_fields": 10 if kind == "detail" else None,
                }

            scraper._diagnose_endpoint = types.MethodType(fake_endpoint, scraper)
            report = scraper.diagnose()

            self.assertTrue(report["ready_for_diff"])
            self.assertFalse(os.path.exists(out_dir))


if __name__ == "__main__":
    unittest.main()
