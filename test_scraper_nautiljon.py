import json
import os
import tempfile
import types
import unittest
import urllib.parse
from datetime import datetime, timedelta
from unittest import mock

from scraper_nautiljon import (
    DATA_SCHEMA_VERSION,
    LETTER_CHECKPOINT_VERSION,
    NautiljonAccessBlockedError,
    NautiljonScraper,
)


def make_row(label: str):
    slug = "hash" if label == "#" else label.lower()
    return {
        "url_fiche": f"https://www.nautiljon.com/mangas/test-{slug}.html",
        "titre": f"Test {label}",
        "extraction_time": "2026-01-01 00:00:00",
    }


class DiffStateTests(unittest.TestCase):
    def make_scraper(self, out_dir: str) -> NautiljonScraper:
        return NautiljonScraper(out_dir=out_dir, delay=0, backend="http")

    def seed_letter(self, scraper: NautiljonScraper, letter: str) -> None:
        label = scraper._letter_label(letter)
        scraper.save_letter_files(scraper._letter_tag(letter), [make_row(label)], partial=False)

    def test_default_pacing_profile_is_prudent_without_excessive_breaks(self):
        pacing_keys = {
            "NAUTILJON_BATCH_SIZE",
            "NAUTILJON_BATCH_PAUSE_MIN",
            "NAUTILJON_BATCH_PAUSE_MAX",
            "NAUTILJON_LETTER_PAUSE_MIN",
            "NAUTILJON_LETTER_PAUSE_MAX",
            "NAUTILJON_FAILURE_PAUSE_MIN",
            "NAUTILJON_FAILURE_PAUSE_MAX",
        }
        clean_env = {key: value for key, value in os.environ.items() if key not in pacing_keys}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            scraper = NautiljonScraper(delay=4, delay_min=4, delay_max=7, backend="http")

        self.assertEqual(scraper.batch_size, 80)
        self.assertEqual((scraper.batch_pause_min, scraper.batch_pause_max), (45.0, 90.0))
        self.assertEqual((scraper.letter_pause_min, scraper.letter_pause_max), (20.0, 45.0))
        self.assertEqual((scraper.failure_pause_min, scraper.failure_pause_max), (120.0, 300.0))

    def test_pacer_serializes_requests_and_adds_batch_break(self):
        env = {
            "NAUTILJON_BATCH_SIZE": "2",
            "NAUTILJON_BATCH_PAUSE_MIN": "30",
            "NAUTILJON_BATCH_PAUSE_MAX": "30",
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "scraper_nautiljon.time.monotonic", return_value=100.0
        ), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper = NautiljonScraper(delay=8, delay_min=8, delay_max=8, backend="http")
            scraper._pace_remote_request("https://www.nautiljon.com/mangas/", "root")
            scraper._pace_remote_request("https://www.nautiljon.com/mangas/?q=a", "listing")
            scraper._pace_remote_request("https://www.nautiljon.com/mangas/test.html", "detail")

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [8.0, 30.0])
        self.assertEqual(scraper.session_stats["remote_requests"], 3)
        self.assertEqual(scraper.session_stats["anti_ban_sleep_seconds"], 38.0)

    def test_pacer_ignores_non_nautiljon_services(self):
        scraper = self.make_scraper("unused")
        with mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper._pace_remote_request("https://api.ipify.org?format=json", "ip")

        sleep.assert_not_called()
        self.assertEqual(scraper.session_stats["remote_requests"], 0)

    def test_http_challenge_aborts_without_retry(self):
        scraper = self.make_scraper("unused")
        response = mock.Mock()
        response.status_code = 403
        response.headers = {"cf-mitigated": "challenge"}
        response.encoding = "utf-8"
        response.text = "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>"
        scraper.session.get = mock.Mock(return_value=response)

        with self.assertRaises(NautiljonAccessBlockedError):
            scraper.fetch_html("https://www.nautiljon.com/mangas/")

        scraper.session.get.assert_called_once()
        response.raise_for_status.assert_not_called()

    def test_access_cooldown_prevents_immediate_restart(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper._record_access_cooldown("managed challenge")

            second = self.make_scraper(out_dir)
            result = second.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "access_cooldown_active")

    def test_generic_listing_failure_is_not_hammered(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            attempts = []

            def failed_fetch(this, letter, page_num):
                attempts.append(page_num)
                raise RuntimeError("temporary failure")

            scraper.fetch_listing_page = types.MethodType(failed_fetch, scraper)
            scraper.scrape_letter_diff("a", drop_missing=False)

            self.assertEqual(attempts, [0])
            self.assertTrue(scraper.session_stats["diff_by_letter"]["A"]["listing_failed"])

    def test_repeated_detail_failures_stop_current_page(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            rows = [make_row("A") for _ in range(3)]
            for index, row in enumerate(rows):
                row["url_fiche"] = f"https://www.nautiljon.com/mangas/failure-{index}.html"
            calls = []
            scraper.fetch_listing_page = types.MethodType(
                lambda this, letter, page_num: ("https://example.test/a", rows),
                scraper,
            )

            def failed_detail(this, series):
                calls.append(series["url_fiche"])
                raise RuntimeError("detail unavailable")

            scraper._fetch_full_series_data = types.MethodType(failed_detail, scraper)
            scraper.scrape_letter_diff("a", max_pages=1, drop_missing=False)

            self.assertEqual(len(calls), 2)
            self.assertTrue(scraper.session_stats["diff_by_letter"]["A"]["detail_failed"])

    def install_fake_letter_scrape(self, scraper: NautiljonScraper, outcomes, calls=None) -> None:
        def fake(this, letter, **kwargs):
            label = this._letter_label(letter)
            if calls is not None:
                calls.append(label)
            outcome = outcomes.get(label, "success")
            flags = {
                "listing_failed": outcome == "listing_failed",
                "access_blocked": outcome == "access_blocked",
                "detail_failed": outcome == "detail_failed",
                "limited": outcome == "limited",
            }
            if outcome in {"listing_failed", "access_blocked", "detail_failed"}:
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

    def test_nautiljon_access_block_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            self.seed_letter(scraper, "a")
            attempts = []

            def blocked_fetch(this, letter, page_num):
                attempts.append(page_num)
                raise NautiljonAccessBlockedError("IP interdite pour abus")

            scraper.fetch_listing_page = types.MethodType(blocked_fetch, scraper)
            scraper.scrape_letter_diff("a", drop_missing=False, resume=True)

            self.assertEqual(attempts, [0])
            self.assertTrue(scraper.session_stats["diff_by_letter"]["A"]["access_blocked"])
            self.assertTrue(os.path.exists(scraper._letter_checkpoint_path("A")))

    def test_detail_access_block_stops_immediately_on_same_page(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            first = make_row("A")
            second = make_row("A")
            second["url_fiche"] = "https://www.nautiljon.com/mangas/second-a.html"
            detail_calls = []

            scraper.fetch_listing_page = types.MethodType(
                lambda this, letter, page_num: ("https://example.test/a", [first, second]),
                scraper,
            )

            def blocked_detail(this, series):
                detail_calls.append(series["url_fiche"])
                raise NautiljonAccessBlockedError("IP interdite pour abus")

            scraper._fetch_full_series_data = types.MethodType(blocked_detail, scraper)
            scraper.scrape_letter_diff("a", drop_missing=False, resume=True)

            stats = scraper.session_stats["diff_by_letter"]["A"]
            checkpoint = scraper._load_json_dict(scraper._letter_checkpoint_path("A"))
            self.assertEqual(len(detail_calls), 1)
            self.assertTrue(stats["access_blocked"])
            self.assertEqual(checkpoint["page_num"], 0)

    def test_access_block_has_dedicated_run_reason(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_letter(scraper, "a")
            self.install_fake_letter_scrape(scraper, {"A": "access_blocked"})

            result = scraper.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "access_blocked")

    def test_extracts_last_and_upcoming_vf_volumes_from_series_page(self):
        scraper = self.make_scraper("unused")
        html = """
        <div id="content"><h1>Smoking Behind The Supermarket With You</h1></div>
        <li class="nav_vols fright">
          <div class="acenter inline-block"><strong>Dernier paru</strong><br>
            <a href="/mangas/smoking/volume-6,1011514.html" title="Vol. 6">
              <img src="/imagesmin/manga_volumes/volume-6.webp?123" alt="Smoking Vol. 6">
            </a><br><span class="infos_small">15/05/2026</span>
          </div>
          <div class="acenter inline-block"><strong>À paraître</strong><br>
            <a href="/mangas/smoking/volume-7,1021408.html" title="Vol. 7">
              <img src="/imagesmin/manga_volumes/volume-7.webp?456" alt="Smoking Vol. 7">
            </a><br><span class="infos_small">30/10/2026</span>
          </div>
        </li>
        """

        detail = scraper.extract_series_detail_from_html(html)

        self.assertEqual(detail["dernier_tome_vf_numero"], "6")
        self.assertEqual(detail["dernier_tome_vf_date"], "15/05/2026")
        self.assertEqual(
            detail["dernier_tome_vf_url"],
            "https://www.nautiljon.com/mangas/smoking/volume-6,1011514.html",
        )
        self.assertEqual(
            detail["dernier_tome_vf_couverture"],
            "https://www.nautiljon.com/imagesmin/manga_volumes/volume-6.webp?123",
        )
        self.assertEqual(detail["prochain_tome_vf_numero"], "7")
        self.assertEqual(detail["prochain_tome_vf_date"], "30/10/2026")
        self.assertFalse(detail["parutions_vf_verifiees_le"].startswith("N/A"))

    def test_release_refresh_only_targets_ongoing_vf_series(self):
        scraper = self.make_scraper("unused")
        ongoing = {
            "nb_vol_vf_detail": "6 (En cours)",
            "parutions_vf_verifiees_le": "N/A",
            "extraction_time": "2020-01-01 00:00:00",
        }
        completed = {
            "nb_vol_vf_detail": "6 (Terminé)",
            "parutions_vf_verifiees_le": "N/A",
            "extraction_time": "2020-01-01 00:00:00",
        }

        self.assertTrue(scraper._series_needs_release_refresh(ongoing, 30))
        self.assertFalse(scraper._series_needs_release_refresh(completed, 30))

        ongoing["parutions_vf_verifiees_le"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(scraper._series_needs_release_refresh(ongoing, 30))

    def test_completed_vf_series_is_not_refetched_due_to_age(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            row = scraper._normalize_row(make_row("A"))
            row["nb_vol_vf_detail"] = "12 (Terminé)"
            row["extraction_time"] = "2020-01-01 00:00:00"
            scraper.save_letter_files("A", [row], partial=False)

            pages = {0: [dict(row)], 1: [], 2: [], 3: []}
            scraper.fetch_listing_page = types.MethodType(
                lambda this, letter, page_num: (f"https://example.test/a?page={page_num}", pages[page_num]),
                scraper,
            )
            scraper._fetch_full_series_data = types.MethodType(
                lambda this, series: self.fail("Une VF terminee ne doit pas etre rechargee par anciennete."),
                scraper,
            )

            scraper.scrape_letter_diff(
                "a",
                refresh_stale_days=30,
                drop_missing=False,
                resume=False,
            )

            self.assertEqual(scraper.session_stats["diff_by_letter"]["A"]["reused"], 1)

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

    def test_complete_catalog_refuses_control_mode(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper.get_all_letters = types.MethodType(lambda this: ["a", "b"], scraper)
            calls = []
            self.install_fake_letter_scrape(scraper, {}, calls=calls)

            result = scraper.scrape_all_letters_diff(
                letters=None,
                drop_missing=False,
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "full_catalog_requires_drop_missing")
            self.assertEqual(calls, [])

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

    def test_full_diff_adopts_recent_controlled_letter_without_scraping_it(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper.get_all_letters = types.MethodType(lambda this: ["a", "b"], scraper)
            self.seed_letter(scraper, "a")
            self.seed_letter(scraper, "b")
            controlled_rows = [make_row("A")]
            controlled_rows[0]["titre"] = "A Test"
            extra = make_row("A")
            extra["url_fiche"] = "https://www.nautiljon.com/mangas/another-a.html"
            extra["titre"] = "Another A"
            controlled_rows.append(extra)
            scraper.save_controlled_letter_files("A", controlled_rows)
            scraper._write_json_atomic(scraper._state_path("last_diff_run"), {
                "mode": "diff",
                "data_schema_version": DATA_SCHEMA_VERSION,
                "status": "partial",
                "reason": "controlled_subset_complete",
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "completed_letters": ["A"],
                "session_stats": {
                    "diff_by_letter": {
                        "A": {
                            "listing_failed": False,
                            "access_blocked": False,
                            "coverage_failed": False,
                            "detail_failed": False,
                            "limited": False,
                            "missing_count": 0,
                            "missing_ratio": 0.0,
                        }
                    }
                },
            })
            scraper.save_letter_files("A", [make_row("A")], partial=True)
            scraper._write_json_atomic(scraper._letter_checkpoint_path("A"), {
                "settings": {"letter": "a"},
                "page_num": 7,
            })
            calls = []
            self.install_fake_letter_scrape(scraper, {}, calls=calls)

            result = scraper.scrape_all_letters_diff(
                min_days_between_diff_exports=30,
                force=False,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(calls, ["B"])
            self.assertEqual(len(scraper._load_json_list(scraper._letter_paths("A")[0])), 2)
            self.assertTrue(scraper.session_stats["diff_by_letter"]["A"]["cache_reused"])
            self.assertFalse(os.path.exists(scraper._letter_checkpoint_path("A")))
            self.assertFalse(os.path.exists(scraper._letter_paths("A")[2]))
            archive_root = os.path.join(out_dir, "checkpoints", "archive")
            self.assertTrue(os.path.isdir(archive_root))

            forced = self.make_scraper(out_dir)
            forced.get_all_letters = types.MethodType(lambda this: ["a", "b"], forced)
            forced_calls = []
            self.install_fake_letter_scrape(forced, {}, calls=forced_calls)

            forced_result = forced.scrape_all_letters_diff(
                min_days_between_diff_exports=30,
                force=True,
            )

            self.assertEqual(forced_result.status, "failed")
            self.assertEqual(forced_result.reason, "full_catalog_force_refused")
            self.assertEqual(forced_calls, [])

            forced_subset_result = forced.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=30,
                force=True,
            )

            self.assertEqual(forced_subset_result.status, "partial")
            self.assertEqual(forced_calls, ["A"])

    def test_expired_controlled_letter_is_scraped_again(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper.get_all_letters = types.MethodType(lambda this: ["a", "b"], scraper)
            self.seed_letter(scraper, "a")
            self.seed_letter(scraper, "b")
            scraper.save_controlled_letter_files("A", [make_row("A")])
            scraper._write_json_atomic(scraper._state_path("last_diff_run"), {
                "mode": "diff",
                "data_schema_version": DATA_SCHEMA_VERSION,
                "status": "partial",
                "reason": "controlled_subset_complete",
                "completed_at": (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds"),
                "completed_letters": ["A"],
                "session_stats": {
                    "diff_by_letter": {
                        "A": {
                            "listing_failed": False,
                            "access_blocked": False,
                            "coverage_failed": False,
                            "detail_failed": False,
                            "limited": False,
                            "missing_count": 0,
                            "missing_ratio": 0.0,
                        }
                    }
                },
            })
            calls = []
            self.install_fake_letter_scrape(scraper, {}, calls=calls)

            result = scraper.scrape_all_letters_diff(
                min_days_between_diff_exports=30,
                force=False,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(calls, ["A", "B"])

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
            first.scrape_letter_diff(
                "a",
                refresh_stale_days=180,
                drop_missing=False,
                resume=True,
            )
            checkpoint = first._load_json_dict(first._letter_checkpoint_path("A"))
            self.assertEqual(attempts, [0, 1])
            self.assertEqual(checkpoint["page_num"], 1)

            second = self.make_scraper(out_dir)
            resumed_pages = []

            def second_fetch(this, letter, page_num):
                resumed_pages.append(page_num)
                return f"https://example.test/a?page={page_num}", []

            second.fetch_listing_page = types.MethodType(second_fetch, second)
            second.scrape_letter_diff(
                "a",
                refresh_stale_days=30,
                drop_missing=False,
                resume=True,
            )

            self.assertEqual(resumed_pages[0], 1)
            self.assertFalse(os.path.exists(second._letter_checkpoint_path("A")))

    def test_flaresolverr_resume_reuses_checkpointed_next_url(self):
        with tempfile.TemporaryDirectory() as out_dir:
            first = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            self.seed_letter(first, "a")
            row = make_row("A")
            exact_next_url = "https://www.nautiljon.com/mangas/?q=a&st=next-token&dbt=50"

            def first_fetch(this, letter, page_num):
                if page_num == 0:
                    this._set_flaresolverr_listing_url(letter, 1, exact_next_url)
                    this._flaresolverr_page_has_next[(this._letter_tag(letter), 0)] = True
                    return "https://www.nautiljon.com/mangas/?q=a&st=first-token", [row]
                raise RuntimeError("listing blocked")

            first.fetch_listing_page = types.MethodType(first_fetch, first)
            first.scrape_letter_diff("a", drop_missing=False, resume=True)

            checkpoint = first._load_json_dict(first._letter_checkpoint_path("A"))
            self.assertEqual(checkpoint["page_num"], 1)
            self.assertEqual(checkpoint["next_listing_url"], exact_next_url)

            second = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            resumed_urls = []

            def second_fetch(this, letter, page_num):
                resumed_urls.append(this._flaresolverr_listing_urls.get((this._letter_tag(letter), page_num)))
                return exact_next_url, [row]

            second.fetch_listing_page = types.MethodType(second_fetch, second)
            second.scrape_letter_diff("a", drop_missing=False, resume=True)

            self.assertEqual(resumed_urls, [exact_next_url])
            self.assertFalse(os.path.exists(second._letter_checkpoint_path("A")))

    def test_legacy_letter_checkpoint_is_archived_and_restarted(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            self.seed_letter(scraper, "e")
            scraper.save_letter_files("E", [make_row("E")], partial=True)
            scraper._write_json_atomic(
                scraper._letter_checkpoint_path("E"),
                {"settings": {"letter": "e"}, "page_num": 10},
            )
            calls = []

            def fetch(this, letter, page_num):
                calls.append(page_num)
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/e", [make_row("E")]

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            scraper.scrape_letter_diff("e", drop_missing=True, resume=True)

            self.assertEqual(calls, [0])
            archive_root = os.path.join(out_dir, "checkpoints", "archive")
            self.assertTrue(os.path.isdir(archive_root))

    def test_current_letter_checkpoint_version_is_compatible(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0)
        checkpoint = {
            "settings": {
                "letter": "e",
                "checkpoint_version": LETTER_CHECKPOINT_VERSION,
            }
        }

        self.assertTrue(scraper._letter_checkpoint_is_compatible("E", checkpoint))

    def test_seen_banned_series_does_not_count_as_missing_coverage(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            existing = []
            listing = []
            for index in range(20):
                row = make_row("E")
                row["titre"] = f"Example E {index}"
                row["url_fiche"] = f"https://www.nautiljon.com/mangas/example-e-{index}.html"
                row["type_liste"] = "Yaoi" if index == 0 else "Seinen"
                existing.append(dict(row))
                listing.append(dict(row))
            scraper.save_letter_files("E", existing, partial=False)

            def fetch(this, letter, page_num):
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/e", listing

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            result = scraper.scrape_letter_diff("e", drop_missing=True, resume=True)

            self.assertEqual(len(result), 19)
            stats = scraper.session_stats["diff_by_letter"]["E"]
            self.assertFalse(stats["coverage_failed"])
            self.assertEqual(stats["missing_count"], 0)
            self.assertEqual(scraper.session_stats["skipped_by_type"], 1)

    def test_stable_complete_coverage_gap_is_finalized_without_network_retry(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            baseline = []
            for index in range(20):
                row = make_row("E")
                row["titre"] = f"Example E {index}"
                row["url_fiche"] = f"https://www.nautiljon.com/mangas/example-e-{index}.html"
                baseline.append(scraper._normalize_row(row))
            current = baseline[:15]
            scraper.save_letter_files("E", baseline, partial=False)
            scraper.save_letter_files("E", current, partial=True)

            current_checkpoint = {
                "updated_at": "2026-09-18T12:00:00",
                "settings": {
                    "checkpoint_version": LETTER_CHECKPOINT_VERSION,
                    "letter": "e",
                },
                "page_num": 1,
                "accessible_listing_pages": 1,
                "successful_listing_pages": 1,
                "next_listing_url": "",
                "counters": {"new": 0, "changed": 0, "parutions": 0, "reused": 15, "removed": 0},
            }
            scraper._write_json_atomic(scraper._letter_checkpoint_path("E"), current_checkpoint)
            scraper._write_json_atomic(
                scraper._state_path("last_diff_run"),
                {
                    "reason": "coverage_incomplete",
                    "session_stats": {
                        "diff_by_letter": {
                            "E": {
                                "coverage_failed": True,
                                "listing_failed": False,
                                "detail_failed": False,
                                "limited": False,
                            }
                        }
                    },
                },
            )

            archive_dir = os.path.join(out_dir, "checkpoints", "archive", "stable_E")
            os.makedirs(archive_dir)
            archived_checkpoint = dict(current_checkpoint)
            archived_checkpoint["updated_at"] = "2026-09-18T10:00:00"
            scraper._write_json_atomic(
                os.path.join(archive_dir, "nautiljon_lettre_E.json"),
                archived_checkpoint,
            )
            scraper._write_json_atomic(
                os.path.join(archive_dir, "nautiljon_lettre_E.partial.json"),
                current,
            )
            scraper.fetch_listing_page = mock.Mock(
                side_effect=AssertionError("a confirmed complete listing must not be fetched again")
            )

            result = scraper.scrape_letter_diff(
                "e",
                drop_missing=True,
                max_missing_ratio=0.15,
                resume=True,
            )

            self.assertEqual(len(result), 15)
            scraper.fetch_listing_page.assert_not_called()
            stats = scraper.session_stats["diff_by_letter"]["E"]
            self.assertFalse(stats["coverage_failed"])
            self.assertEqual(stats["missing_count"], 5)
            self.assertEqual(stats["removed"], 5)
            self.assertFalse(os.path.exists(scraper._letter_checkpoint_path("E")))
            self.assertEqual(len(scraper._load_json_list(scraper._letter_paths("E")[0])), 15)

    def test_stable_coverage_gap_above_hard_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            baseline_urls = {
                f"https://www.nautiljon.com/mangas/example-e-{index}.html"
                for index in range(20)
            }
            current_urls = set(sorted(baseline_urls)[:10])
            missing_urls = baseline_urls - current_urls
            current_rows = [
                {"url_fiche": url, "titre": url.rsplit("/", 1)[-1]}
                for url in current_urls
            ]
            checkpoint = {
                "updated_at": "2026-09-18T12:00:00",
                "page_num": 1,
                "accessible_listing_pages": 1,
                "successful_listing_pages": 1,
                "listing_complete": True,
            }
            archive_dir = os.path.join(out_dir, "checkpoints", "archive", "stable_E")
            os.makedirs(archive_dir)
            archived_checkpoint = dict(checkpoint)
            archived_checkpoint["updated_at"] = "2026-09-18T10:00:00"
            scraper._write_json_atomic(
                os.path.join(archive_dir, "nautiljon_lettre_E.json"),
                archived_checkpoint,
            )
            scraper._write_json_atomic(
                os.path.join(archive_dir, "nautiljon_lettre_E.partial.json"),
                current_rows,
            )

            self.assertFalse(
                scraper._coverage_gap_confirmed_by_archive(
                    "E",
                    baseline_urls,
                    missing_urls,
                    len(current_rows),
                    checkpoint,
                )
            )

    def test_flaresolverr_exact_resume_url_bypasses_alphabet_index(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        exact_url = "https://www.nautiljon.com/mangas/?q=b&st=saved-token&dbt=100"
        scraper._set_flaresolverr_listing_url("b", 2, exact_url)
        scraper._fetch_html_flaresolverr = mock.Mock(return_value="<html><body></body></html>")
        scraper._last_flaresolverr_url = exact_url
        scraper._load_flaresolverr_letter_urls = mock.Mock(
            side_effect=AssertionError("alphabet index must not be loaded during exact resume")
        )

        final_url, rows = scraper._fetch_listing_page_flaresolverr("b", 2)

        self.assertEqual(final_url, exact_url)
        self.assertEqual(rows, [])
        scraper._load_flaresolverr_letter_urls.assert_not_called()
        scraper._fetch_html_flaresolverr.assert_called_once_with(exact_url)

    def test_missing_flaresolverr_alphabet_saves_debug_and_blocks(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            scraper._last_flaresolverr_url = "https://www.nautiljon.com/mangas/"
            scraper._fetch_html_flaresolverr = mock.Mock(
                return_value="<html><head><title>Page inattendue</title></head><body>vide</body></html>"
            )

            with self.assertRaises(NautiljonAccessBlockedError):
                scraper._load_flaresolverr_letter_urls()

            self.assertIn("html", scraper._last_flaresolverr_debug)
            self.assertIn("metadata", scraper._last_flaresolverr_debug)
            self.assertTrue(os.path.isfile(scraper._last_flaresolverr_debug["html"]))
            metadata = scraper._load_json_dict(scraper._last_flaresolverr_debug["metadata"])
            self.assertEqual(metadata["title"], "Page inattendue")

    def test_flaresolverr_test_saves_empty_listing_debug(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            listing_url = "https://www.nautiljon.com/mangas/?q=a"
            scraper._flaresolverr_public_ips = mock.Mock(return_value=("203.0.113.1", "203.0.113.1"))
            scraper.fetch_listing_page = mock.Mock(return_value=(listing_url, []))
            scraper._last_flaresolverr_url = listing_url
            scraper._last_flaresolverr_html = (
                "<html><head><title>Liste vide</title></head>"
                "<body>Votre session de recherche a expire.</body></html>"
            )
            scraper.close_flaresolverr = mock.Mock()

            report = scraper.flaresolverr_test("a")

            self.assertFalse(report["ready_for_diff"])
            self.assertTrue(report["search_session_expired"])
            self.assertEqual(report["listing_final_url"], listing_url)
            self.assertTrue(os.path.isfile(report["debug"]["html"]))

    def test_controlled_diff_does_not_replace_final_letter(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_letter(scraper, "a")
            final_json = scraper._letter_paths("A")[0]
            baseline = scraper._load_json_list(final_json)
            new_row = make_row("A")
            new_row["url_fiche"] = "https://www.nautiljon.com/mangas/new-a.html"
            pages = {0: [new_row], 1: [], 2: [], 3: []}

            def fake_fetch(this, letter, page_num):
                return f"https://example.test/a?page={page_num}", pages[page_num]

            scraper.fetch_listing_page = types.MethodType(fake_fetch, scraper)
            scraper._fetch_full_series_data = types.MethodType(
                lambda this, series: this._normalize_row(series),
                scraper,
            )

            rows = scraper.scrape_letter_diff("a", drop_missing=False, resume=False)

            self.assertEqual(scraper._load_json_list(final_json), baseline)
            self.assertEqual(len(rows), 2)
            control_json = os.path.join(out_dir, "control", "nautiljon_lettre_A.control.json")
            self.assertEqual(len(scraper._load_json_list(control_json)), 2)
            marker = scraper._load_json_dict(scraper._state_path("letter_A_success"))
            self.assertEqual(marker["status"], "success")
            self.assertEqual(marker["source_mode"], "controlled")
            self.assertEqual(marker["rows_count"], 1)

    def test_large_missing_ratio_preserves_final_letter(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            old_rows = []
            for index in range(20):
                row = make_row("A")
                row["url_fiche"] = f"https://www.nautiljon.com/mangas/old-a-{index}.html"
                row["titre"] = f"A Old {index}"
                old_rows.append(scraper._normalize_row(row))
            scraper.save_letter_files("A", old_rows, partial=False)
            final_json = scraper._letter_paths("A")[0]
            pages = {0: [old_rows[0]], 1: [], 2: [], 3: []}

            def fake_fetch(this, letter, page_num):
                return f"https://example.test/a?page={page_num}", pages[page_num]

            scraper.fetch_listing_page = types.MethodType(fake_fetch, scraper)
            scraper.scrape_letter_diff(
                "a",
                drop_missing=True,
                max_missing_ratio=0.15,
                resume=False,
            )

            self.assertEqual(len(scraper._load_json_list(final_json)), 20)
            self.assertTrue(scraper.session_stats["diff_by_letter"]["A"]["coverage_failed"])
            self.assertTrue(os.path.exists(scraper._letter_checkpoint_path("A")))

    def test_full_flaresolverr_page_without_next_link_is_incomplete(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            rows = []
            for index in range(50):
                row = make_row("A")
                row["url_fiche"] = f"https://www.nautiljon.com/mangas/a-{index}.html"
                row["titre"] = f"A {index:02d}"
                rows.append(scraper._normalize_row(row))
            scraper.save_letter_files("A", rows, partial=False)
            scraper.fetch_listing_page = types.MethodType(
                lambda this, letter, page_num: ("https://example.test/a", rows),
                scraper,
            )

            scraper.scrape_letter_diff("a", drop_missing=True, resume=False)

            stats = scraper.session_stats["diff_by_letter"]["A"]
            self.assertTrue(stats["listing_failed"])
            self.assertEqual(len(scraper._load_json_list(scraper._letter_paths("A")[0])), 50)

    def test_diagnose_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = os.path.join(temp_dir, "must-not-exist")
            scraper = self.make_scraper(out_dir)

            def fake_endpoint(this, label, url, kind="generic", expected_letter=None):
                result = {
                    "label": label,
                    "url": url,
                    "ok": True,
                    "status_code": 200,
                    "waf_blocked": False,
                    "rows": 50 if kind == "listing" else None,
                    "parsed_fields": 10 if kind == "detail" else None,
                }
                if kind == "ip":
                    result["public_ip"] = "203.0.113.10"
                return result

            scraper._diagnose_endpoint = types.MethodType(fake_endpoint, scraper)
            report = scraper.diagnose()

            self.assertTrue(report["ready_for_diff"])
            self.assertEqual(report["public_ip"], "203.0.113.10")
            self.assertFalse(os.path.exists(out_dir))

    def test_ip_diagnostic_reports_public_ip(self):
        scraper = self.make_scraper("unused")
        response = mock.Mock()
        response.text = '{"ip":"198.51.100.42"}'
        response.content = response.text.encode("utf-8")
        response.status_code = 200
        response.ok = True
        response.url = "https://api.ipify.org/?format=json"
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"ip": "198.51.100.42"}

        with mock.patch("scraper_nautiljon.requests.get", return_value=response):
            result = scraper._diagnose_endpoint(
                "ip_sortie",
                "https://api.ipify.org?format=json",
                kind="ip",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["public_ip"], "198.51.100.42")

    def test_browser_backend_dispatches_listing_to_selenium(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="selenium")

        def fake_listing(this, letter, page_num):
            return "https://example.test/a", [make_row("A")]

        scraper._fetch_listing_page_selenium = types.MethodType(fake_listing, scraper)
        url, rows = scraper.fetch_listing_page("a", 0)

        self.assertEqual(url, "https://example.test/a")
        self.assertEqual(len(rows), 1)

    def test_detects_french_cloudflare_challenge(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="selenium")
        html = """
        <html><title>Un instant...</title><body>
        <h1>Vérification de sécurité en cours</h1>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
        </body></html>
        """

        self.assertTrue(scraper._cloudflare_challenge(html))
        self.assertTrue(scraper._blocked_by_waf(html))

    def test_detects_nautiljon_ip_abuse_block(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        html = """
        <html><body>IP (193.43.69.227) interdite pour abus.
        Ce probleme peut etre du a de la recuperation de donnees sur le site (interdit)
        ou a l'utilisation d'un VPN. Pour debloquer votre acces, contactez-nous.</body></html>
        """

        self.assertTrue(scraper._nautiljon_access_blocked(html))
        self.assertTrue(scraper._blocked_by_waf(html))

    def test_flaresolverr_fetch_reuses_one_session(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        calls = []

        def fake_post(this, payload):
            calls.append(payload)
            if payload["cmd"] == "request.get":
                return {
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "url": payload["url"],
                        "response": "<html><body>OK</body></html>",
                    },
                }
            return {"status": "ok"}

        scraper._flaresolverr_post = types.MethodType(fake_post, scraper)
        first = scraper._fetch_html_flaresolverr("https://www.nautiljon.com/mangas/a.html")
        second = scraper._fetch_html_flaresolverr("https://www.nautiljon.com/mangas/b.html")
        scraper.close_flaresolverr()

        self.assertIn("OK", first)
        self.assertIn("OK", second)
        self.assertEqual([call["cmd"] for call in calls], ["sessions.create", "request.get", "request.get", "sessions.destroy"])
        self.assertEqual(calls[1]["session"], calls[2]["session"])
        self.assertIn("cookies", calls[1])

    def test_flaresolverr_session_waits_for_service_startup(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        calls = []

        def fake_post(this, payload):
            calls.append(payload)
            if len(calls) < 3:
                raise RuntimeError("connexion refusee")
            return {"status": "ok"}

        scraper._flaresolverr_post = types.MethodType(fake_post, scraper)
        env = {
            "NAUTILJON_FLARESOLVERR_STARTUP_ATTEMPTS": "3",
            "NAUTILJON_FLARESOLVERR_STARTUP_DELAY": "0",
        }
        with mock.patch.dict(os.environ, env), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            session_id = scraper.setup_flaresolverr()

        self.assertTrue(session_id.startswith("nautiljon-"))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_flaresolverr_session_uses_configured_vpn_proxy(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        calls = []

        def fake_post(this, payload):
            calls.append(payload)
            return {"status": "ok"}

        scraper._flaresolverr_post = types.MethodType(fake_post, scraper)
        env = {
            "NAUTILJON_FLARESOLVERR_PROXY_URL": "http://gluetun-nord:8888",
            "NAUTILJON_FLARESOLVERR_PROXY_USERNAME": "nautiljon",
            "NAUTILJON_FLARESOLVERR_PROXY_PASSWORD": "secret",
        }
        with mock.patch.dict(os.environ, env):
            scraper.setup_flaresolverr()

        self.assertEqual(calls[0]["proxy"], {
            "url": "http://gluetun-nord:8888",
            "username": "nautiljon",
            "password": "secret",
        })

    def test_flaresolverr_rejects_unsolved_challenge(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")

        def fake_post(this, payload):
            if payload["cmd"] == "request.get":
                return {
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "url": payload["url"],
                        "response": "<title>Un instant…</title><h1>Vérification de sécurité en cours</h1>",
                    },
                }
            return {"status": "ok"}

        scraper._flaresolverr_post = types.MethodType(fake_post, scraper)
        with self.assertRaisesRegex(RuntimeError, "n'a pas resolu Cloudflare"):
            scraper._fetch_html_flaresolverr("https://www.nautiljon.com/mangas/a.html")
        scraper.close_flaresolverr()

    def test_flaresolverr_listing_uses_dynamic_letter_links(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        labels = ["#"] + [chr(code) for code in range(ord("A"), ord("Z") + 1)]
        root_html = "".join(
            f'<a href="/mangas/?q={"%23" if label == "#" else label.lower()}">{label}</a>'
            for label in labels
        )
        calls = []

        def fake_fetch(this, url):
            calls.append(url)
            this._last_flaresolverr_url = url
            return root_html if url.endswith("/mangas/") else "<html>listing</html>"

        def fake_parse(this, html):
            query = calls[-1].split("q=", 1)[1].split("&", 1)[0]
            row = make_row(query.upper())
            row["titre"] = f"{query.upper()} Test"
            return [row]

        scraper._fetch_html_flaresolverr = types.MethodType(fake_fetch, scraper)
        scraper.extract_series_list_from_html = types.MethodType(fake_parse, scraper)

        _, a_rows = scraper.fetch_listing_page("a", 0)
        _, b_rows = scraper.fetch_listing_page("b", 0)

        self.assertEqual(a_rows[0]["titre"], "A Test")
        self.assertEqual(b_rows[0]["titre"], "B Test")
        self.assertEqual(calls.count("https://www.nautiljon.com/mangas/"), 1)
        self.assertIn("q=a", calls[1])
        self.assertIn("q=b", calls[2])

    def test_flaresolverr_listing_submits_signed_search_form(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        labels = ["#"] + [chr(code) for code in range(ord("A"), ord("Z") + 1)]
        root_html = """
        <form action="/mangas/" method="get">
          <input name="q" type="text" value="">
          <input name="st" type="hidden" value="signed-token">
          <select name="webcomic"><option value="">----</option><option value="1">Oui</option></select>
          <input name="edition_sup" type="hidden" value="2">
          <input name="types_include[]" type="checkbox" value="12">
          <input type="submit" value="Search">
        </form>
        """ + "".join(
            f'<a href="/mangas/?q={"%23" if label == "#" else label.lower()}">{label}</a>'
            for label in labels
        )
        calls = []

        def fake_fetch(this, url):
            calls.append(url)
            this._last_flaresolverr_url = url
            return root_html if url.endswith("/mangas/") else "<html>listing</html>"

        def fake_parse(this, html):
            row = make_row("A")
            row["titre"] = "A Test"
            return [row]

        scraper._fetch_html_flaresolverr = types.MethodType(fake_fetch, scraper)
        scraper.extract_series_list_from_html = types.MethodType(fake_parse, scraper)

        _, rows = scraper.fetch_listing_page("a", 0)

        self.assertEqual(rows[0]["titre"], "A Test")
        parsed = urllib.parse.urlsplit(calls[1])
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.assertEqual(params["q"], ["a"])
        self.assertEqual(params["st"], ["signed-token"])
        self.assertEqual(params["webcomic"], [""])
        self.assertEqual(params["edition_sup"], ["2"])
        self.assertNotIn("types_include[]", params)

    def test_flaresolverr_listing_follows_exact_next_token(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        labels = ["#"] + [chr(code) for code in range(ord("A"), ord("Z") + 1)]
        root_html = "".join(
            f'<a href="/mangas/?q={"%23" if label == "#" else label.lower()}&st=token-1">{label}</a>'
            for label in labels
        )
        first_page = """
        <table><tr><td><a href="/mangas/alpha.html">Alpha</a></td></tr></table>
        <a href="/mangas/?q=a&amp;st=token-2&amp;dbt=50">Suivante</a>
        """
        second_page = """
        <table><tr><td><a href="/mangas/aster.html">Aster</a></td></tr></table>
        """
        calls = []

        def fake_fetch(this, url):
            calls.append(url)
            this._last_flaresolverr_url = url
            if url.endswith("/mangas/"):
                return root_html
            if "st=token-2" in url:
                return second_page
            return first_page

        scraper._fetch_html_flaresolverr = types.MethodType(fake_fetch, scraper)

        _, first_rows = scraper.fetch_listing_page("a", 0)
        _, second_rows = scraper.fetch_listing_page("a", 1)

        self.assertEqual(first_rows[0]["titre"], "Alpha")
        self.assertEqual(second_rows[0]["titre"], "Aster")
        self.assertIn("st=token-2", calls[-1])
        self.assertTrue(scraper._flaresolverr_listing_has_next("a", 0))
        self.assertFalse(scraper._flaresolverr_listing_has_next("a", 1))

    def test_diff_rejects_flaresolverr_ip_mismatch(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            scraper._flaresolverr_public_ips = types.MethodType(
                lambda this: ("198.51.100.10", "203.0.113.20"),
                scraper,
            )

            result = scraper.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
                force=True,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "flaresolverr_preflight_failed")
            self.assertFalse(os.path.exists(scraper._last_success_path("diff")))

    def test_browser_test_checks_listing_and_detail_without_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = os.path.join(temp_dir, "output")
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="selenium")

            def fake_listing(this, letter, page_num):
                return "https://example.test/a", [make_row("A")]

            def fake_get(this, url, context):
                return """
                <div id="content"><h1>Test A</h1><ul class="mb10">
                <li>Titre original : Test</li><li>Origine : Japon - 2026</li>
                <li>Type : Seinen</li><li>Genres : Action</li>
                </ul></div>
                """

            scraper._fetch_listing_page_selenium = types.MethodType(fake_listing, scraper)
            scraper._browser_get = types.MethodType(fake_get, scraper)
            scraper.close_browser = types.MethodType(lambda this: None, scraper)
            report = scraper.browser_test("a")

            self.assertTrue(report["ready_for_diff"])
            self.assertFalse(os.path.exists(os.path.join(out_dir, "exports")))


if __name__ == "__main__":
    unittest.main()
