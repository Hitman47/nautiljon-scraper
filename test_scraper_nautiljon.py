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
    ListingPartitionError,
    NautiljonAccessBlockedError,
    NautiljonScraper,
    RunResult,
)


def make_row(label: str):
    slug = "hash" if label == "#" else label.lower()
    return {
        "url_fiche": f"https://www.nautiljon.com/mangas/test-{slug}.html",
        "titre": f"Test {label}",
        "extraction_time": "2026-01-01 00:00:00",
    }


class DiffStateTests(unittest.TestCase):
    def test_monthly_keeps_latest_export_when_last_batch_only_cleans_queue(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http", detail_mode="deferred")
            scraper.scrape_all_letters_diff = mock.Mock(return_value=RunResult(
                status="success", reason="complete_catalog_exported", export_paths={"json_path": "old.json"}))
            scraper.enrich_detail_queue = mock.Mock(side_effect=[
                RunResult(status="success", reason="detail_batch_complete_queue_pending", export_paths={"json_path": "enriched.json"}),
                RunResult(status="success", reason="detail_queue_complete")])
            scraper._monthly_wait = mock.Mock()
            result = scraper.run_monthly()
            self.assertEqual(result.export_paths, {"json_path": "enriched.json"})
            scraper._monthly_wait.assert_not_called()

    def seed_enrichment_queue(self, scraper, count, cleanup_count=0):
        rows = [dict(make_row("A"), titre=f"A {i:03d}", url_fiche=f"https://www.nautiljon.com/mangas/a{i:03d}.html") for i in range(count)]
        scraper.save_letter_files("A", rows, partial=False)
        queue = {}
        for i, row in enumerate(rows):
            scraper._queue_detail(queue, "A", row, "volume_changed" if i < cleanup_count else "new_series", ready=True)
        scraper._save_detail_queue(queue)
        return rows

    def test_local_cleanup_does_not_consume_network_budget_and_writes_are_batched(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_enrichment_queue(scraper, 22, cleanup_count=20)
            scraper._fetch_full_series_data = mock.Mock(side_effect=lambda row: dict(row, genres="Action"))
            scraper._sleep_detail_delay = mock.Mock()
            with mock.patch.object(scraper, "save_letter_files", wraps=scraper.save_letter_files) as save:
                result = scraper.enrich_detail_queue(max_items=2, continuous=True)
            self.assertEqual(result.reason, "detail_queue_complete")
            self.assertEqual(scraper._fetch_full_series_data.call_count, 2)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(scraper._load_detail_queue(), {})

    def test_cleanup_only_never_starts_network_or_moves_success_timer(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            self.seed_enrichment_queue(scraper, 20, cleanup_count=20)
            scraper._resolve_access_cooldown = mock.Mock(side_effect=AssertionError("network"))
            scraper._flaresolverr_public_ips = mock.Mock(side_effect=AssertionError("network"))
            result = scraper.enrich_detail_queue(max_items=1)
            self.assertEqual(result.reason, "detail_queue_complete")
            self.assertIsNone(scraper._load_last_success("enrich"))
            self.assertFalse(scraper._load_detail_queue())

    def test_continuous_batches_keep_session_counters_and_cross_batch_delay(self):
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.dict(os.environ, {
            "NAUTILJON_ENRICH_MIN_INTERVAL_MINUTES": "30", "NAUTILJON_DETAIL_BATCH_SIZE": "2",
            "NAUTILJON_DETAIL_BATCH_PAUSE_MIN": "10", "NAUTILJON_DETAIL_BATCH_PAUSE_MAX": "10",
            "NAUTILJON_REQUEST_BURST_SIZE": "2", "NAUTILJON_REQUEST_BURST_PAUSE_MIN": "20",
            "NAUTILJON_REQUEST_BURST_PAUSE_MAX": "20"}):
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            self.seed_enrichment_queue(scraper, 3)
            scraper._flaresolverr_public_ips = mock.Mock(return_value=("203.0.113.1", "203.0.113.1"))
            scraper.close_browser = mock.Mock()
            scraper._sleep_for = mock.Mock(return_value=0)
            scraper._sleep_detail_delay = mock.Mock()
            def fetch(row):
                scraper._pace_detail_request()
                scraper._pace_remote_request(row["url_fiche"], "test")
                return dict(row, genres="Action")
            scraper._fetch_full_series_data = mock.Mock(side_effect=fetch)
            self.assertEqual(scraper.enrich_detail_queue(2, continuous=True).reason, "detail_batch_complete_queue_pending")
            self.assertEqual(scraper.enrich_detail_queue(2, continuous=True, cleanup=False).reason, "detail_queue_complete")
            scraper._flaresolverr_public_ips.assert_called_once()
            scraper.close_browser.assert_not_called()
            self.assertEqual(scraper._detail_request_count, 3)
            self.assertEqual(scraper._remote_request_count, 3)
            self.assertEqual(scraper._sleep_detail_delay.call_count, 3)
            reasons = [call.args[1] for call in scraper._sleep_for.call_args_list]
            self.assertTrue(any("2 fiches" in reason for reason in reasons))
            self.assertTrue(any("2 navigations" in reason for reason in reasons))

    def test_batch_save_failure_keeps_queue_for_replay(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_enrichment_queue(scraper, 2)
            scraper._fetch_full_series_data = mock.Mock(side_effect=lambda row: dict(row, genres="Action"))
            scraper._sleep_detail_delay = mock.Mock()
            scraper.save_letter_files = mock.Mock(side_effect=OSError("disk failure"))
            scraper.close_browser = mock.Mock()
            with self.assertRaises(OSError):
                scraper.enrich_detail_queue(2, continuous=True)
            self.assertEqual(len(scraper._load_detail_queue()), 2)
            scraper.close_browser.assert_called()

    def test_block_flushes_prior_success_but_preserves_failed_item(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            rows = self.seed_enrichment_queue(scraper, 2)
            scraper._fetch_full_series_data = mock.Mock(side_effect=[dict(rows[0], genres="Action"), NautiljonAccessBlockedError("blocked")])
            scraper._sleep_detail_delay = mock.Mock()
            scraper.close_browser = mock.Mock()
            scraper._record_access_cooldown = mock.Mock()
            result = scraper.enrich_detail_queue(2, continuous=True)
            self.assertEqual(result.reason, "access_blocked")
            self.assertEqual(list(scraper._load_detail_queue()), [rows[1]["url_fiche"]])
            saved = scraper._load_json_list(scraper._letter_paths("A")[0])
            self.assertEqual(saved[0]["genres"], "Action")
            scraper.close_browser.assert_called()

    def test_failed_attempts_count_towards_batch_limit(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            self.seed_enrichment_queue(scraper, 3)
            scraper._fetch_full_series_data = mock.Mock(side_effect=RuntimeError("network"))
            result = scraper.enrich_detail_queue(1, continuous=True)
            scraper._fetch_full_series_data.assert_called_once()
            self.assertEqual(result.reason, "detail_errors")
            self.assertEqual(len(scraper._load_detail_queue()), 3)

    def install_partition_fixture(self, scraper, calls, ignored=False, capped=False):
        scraper._flaresolverr_letter_urls = {"S": "https://www.nautiljon.com/mangas/?q=s&st=signed"}
        scraper._sleep_delay = mock.Mock()
        def fetch(url):
            calls.append(url)
            scraper._last_flaresolverr_url = url
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            filters = [(k, v) for k, values in query.items() for v in values if k.endswith("[]")]
            ids = [0, 1] if not filters or "types_include[]" in query else [2, 3, 4]
            total = 4 if not filters or capped else len(ids)
            fields = ''.join(f'<input name="{k}" value="{v}" checked>' for k, v in filters) if not ignored else ''
            html = f'<h2>Mangas ({total} résultats)</h2><input name="types_include[]" value="1"><input name="types_exclude[]" value="1">{fields}<p>{",".join(map(str, ids))}</p>'
            scraper._last_flaresolverr_html = html
            return html
        def parse(html):
            ids = html.split('<p>')[1].split('</p>')[0].split(',')
            return [dict(make_row("S"), titre=f"Series {i}", url_fiche=f"https://www.nautiljon.com/mangas/s{i}.html") for i in ids if i]
        scraper._fetch_html_flaresolverr = mock.Mock(side_effect=fetch)
        scraper.extract_series_list_from_html = mock.Mock(side_effect=parse)

    def test_capped_search_splits_and_finishes_union_without_duplicates(self):
        with tempfile.TemporaryDirectory() as out_dir, mock.patch("scraper_nautiljon.LISTING_RESULT_CAP", 4):
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
            calls = []
            self.install_partition_fixture(scraper, calls)
            rows = scraper.scrape_letter_diff("s")
            self.assertEqual(len(rows), 5)
            self.assertEqual(len({row["url_fiche"] for row in rows}), 5)
            self.assertFalse(scraper.session_stats["diff_by_letter"]["S"]["listing_failed"])
            self.assertFalse(os.path.exists(scraper._letter_checkpoint_path("S")))
            self.assertTrue(any("types_exclude" in url for url in calls))

    def test_partition_cursor_resumes_without_restarting_completed_branch(self):
        with tempfile.TemporaryDirectory() as out_dir, mock.patch("scraper_nautiljon.LISTING_RESULT_CAP", 4):
            first = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
            calls = []
            self.install_partition_fixture(first, calls)
            first.scrape_letter_diff("s", max_pages=1)
            checkpoint = first._load_json_dict(first._letter_checkpoint_path("S"))
            self.assertEqual(checkpoint["page_num"], 1)
            self.assertEqual(checkpoint["partition_cursor"]["pending"][0]["filters"], [["types_exclude[]", "1"]])
            second = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
            resumed_calls = []
            self.install_partition_fixture(second, resumed_calls)
            rows = second.scrape_letter_diff("s")
            self.assertEqual(len(rows), 5)
            self.assertEqual(len(resumed_calls), 1)
            self.assertIn("types_exclude", resumed_calls[0])

    def test_ignored_filters_or_unsplittable_cap_never_finalize(self):
        for ignored, capped in [(True, False), (False, True)]:
            with self.subTest(ignored=ignored), tempfile.TemporaryDirectory() as out_dir, mock.patch("scraper_nautiljon.LISTING_RESULT_CAP", 4):
                scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
                self.seed_letter(scraper, "s")
                calls = []
                self.install_partition_fixture(scraper, calls, ignored=ignored, capped=capped)
                scraper.scrape_letter_diff("s")
                self.assertTrue(scraper.session_stats["diff_by_letter"]["S"]["pagination_stalled"])
                self.assertLessEqual(len(calls), 3)
                self.assertTrue(os.path.exists(scraper._letter_checkpoint_path("S")))
                self.assertEqual(len(scraper._load_json_list(scraper._letter_paths("S")[0])), 1)

    def test_exact_multiple_total_finishes_full_page_and_migrates_old_probe(self):
        for page in (3, 4):
            with self.subTest(page=page):
                scraper = NautiljonScraper(delay=0, backend="flaresolverr")
                scraper._flaresolverr_letter_urls = {"Z": "https://www.nautiljon.com/mangas/?q=z&st=token"}
                html = '<h2>Mangas commençant par la lettre Z (200 résultats)</h2>'
                rows = [dict(make_row("Z"), titre=f"Z {i}", url_fiche=f"https://www.nautiljon.com/mangas/z{i}.html") for i in range(50)]
                def fetch(url):
                    scraper._last_flaresolverr_url = url
                    return html
                scraper._fetch_html_flaresolverr = mock.Mock(side_effect=fetch)
                scraper.extract_series_list_from_html = mock.Mock(return_value=rows)
                _, result = scraper._fetch_listing_page_flaresolverr("z", page)
                self.assertEqual(len(result), 50)
                self.assertIn(("Z", page), scraper._listing_complete_pages)
                self.assertFalse(scraper._flaresolverr_listing_has_next("z", page))
                self.assertIn("dbt=150", scraper._fetch_html_flaresolverr.call_args.args[0])

    def test_listing_total_and_complementary_dimensions(self):
        self.assertEqual(NautiljonScraper._listing_total('<h2>Mangas (2\u202f500 résultats)</h2>'), 2500)
        self.assertEqual(NautiljonScraper._listing_total('<title>Mangas (0 résultat)</title>'), 0)
        self.assertIsNone(NautiljonScraper._listing_total('<h2>Cloudflare</h2>'))
        html = '<input name="types_include[]" value="1"><input name="types_exclude[]" value="1"><input name="types_include[]" value="2">'
        self.assertEqual(NautiljonScraper._listing_split_dimensions(html), [("types", "1")])

    def test_partition_multiple_pages_validate_totals_and_reject_repeats(self):
        for mode in ("valid", "repeated", "changed_total", "missing_total"):
            with self.subTest(mode=mode):
                scraper = NautiljonScraper(delay=0, backend="flaresolverr")
                scraper._flaresolverr_letter_urls = {"S": "https://www.nautiljon.com/mangas/?q=s&st=x"}
                scraper._listing_partition_cursors[("S", 0)] = {
                    "version": 1, "dimensions": [], "splits": 0, "union": [], "minimum_total": 51, "root_sample": [],
                    "pending": [{"filters": [["types_include[]", "1"]], "offset": 0, "dimension": 0, "seen": [], "total": None}]}
                def fetch(url):
                    scraper._last_flaresolverr_url = url
                    second = "dbt=50" in url
                    total = 52 if second and mode == "changed_total" else 51
                    heading = "" if second and mode == "missing_total" else f'<h2>Mangas ({total} résultats)</h2>'
                    return heading + '<input name="types_include[]" value="1" checked>' + ('SECOND' if second else 'FIRST')
                def parse(html):
                    indexes = [0 if mode == "repeated" else 50] if 'SECOND' in html else range(50)
                    return [dict(make_row("S"), titre=f"S {i}", url_fiche=f"https://www.nautiljon.com/mangas/s{i}.html") for i in indexes]
                scraper._fetch_html_flaresolverr = mock.Mock(side_effect=fetch)
                scraper.extract_series_list_from_html = mock.Mock(side_effect=parse)
                scraper._fetch_listing_page_flaresolverr("s", 0)
                if mode == "valid":
                    _, rows = scraper._fetch_listing_page_flaresolverr("s", 1)
                    self.assertEqual(len(rows), 1)
                    self.assertIn(("S", 1), scraper._listing_complete_pages)
                else:
                    with self.assertRaises(ListingPartitionError):
                        scraper._fetch_listing_page_flaresolverr("s", 1)
                    self.assertNotIn(("S", 1), scraper._listing_complete_pages)

    def test_empty_partition_with_explicit_zero_can_finish(self):
        scraper = NautiljonScraper(delay=0, backend="flaresolverr")
        scraper._flaresolverr_letter_urls = {"S": "https://www.nautiljon.com/mangas/?q=s&st=x"}
        scraper._listing_partition_cursors[("S", 0)] = {
            "version": 1, "dimensions": [], "splits": 0, "union": [], "minimum_total": 0, "root_sample": [],
            "pending": [{"filters": [["types_exclude[]", "1"]], "offset": 0, "dimension": 0, "seen": [], "total": None}]}
        scraper._fetch_html_flaresolverr = mock.Mock(return_value='<h2>Mangas (0 résultat)</h2><input name="types_exclude[]" value="1" checked>')
        scraper.extract_series_list_from_html = mock.Mock(return_value=[])
        _, rows = scraper._fetch_listing_page_flaresolverr("s", 0)
        self.assertEqual(rows, [])
        self.assertIn(("S", 0), scraper._listing_complete_pages)

    def test_signed_partition_refresh_preserves_all_filters_and_offset(self):
        scraper = NautiljonScraper(delay=0, backend="flaresolverr")
        scraper._fetch_html_flaresolverr = mock.Mock(side_effect=["Votre session de recherche a expiré", "OK"])
        def reload():
            scraper._flaresolverr_letter_urls["S"] = "https://www.nautiljon.com/mangas/?q=s&st=fresh"
        scraper._load_flaresolverr_letter_urls = mock.Mock(side_effect=reload)
        scraper._save_flaresolverr_debug = mock.Mock(return_value={})
        url = "https://www.nautiljon.com/mangas/?q=s&st=old&dbt=50&types_exclude%5B%5D=1&types_exclude%5B%5D=2&encours_vos_include%5B%5D=1"
        scraper._fetch_signed_listing_html("s", url)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(scraper._fetch_html_flaresolverr.call_args.args[0]).query)
        self.assertEqual(params["st"], ["fresh"])
        self.assertEqual(params["dbt"], ["50"])
        self.assertEqual(params["types_exclude[]"], ["1", "2"])
        self.assertEqual(params["encours_vos_include[]"], ["1"])

    def test_reset_listing_offset_removes_old_probe_offset(self):
        scraper = self.make_scraper("unused")
        self.assertEqual(scraper._url_with_dbt("https://www.nautiljon.com/mangas/?q=z&dbt=50", 0),
                         "https://www.nautiljon.com/mangas/?q=z")

    def test_full_page_without_next_link_probes_next_offset(self):
        for repeated in (False, True):
            with self.subTest(repeated=repeated), tempfile.TemporaryDirectory() as out_dir:
                scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
                rows = [dict(make_row("S"), titre=f"Series {i}", url_fiche=f"https://www.nautiljon.com/mangas/s{i}.html") for i in range(50)]
                tail = [dict(make_row("S"), titre="Series tail")]
                scraper._last_flaresolverr_html = '<input name="st" value="token">'
                def fetch(letter, page):
                    if page == 0:
                        return "https://www.nautiljon.com/mangas/?q=s&st=token", rows
                    self.assertEqual(page, 1)
                    self.assertIn("dbt=50", scraper._flaresolverr_listing_urls[("S", 1)])
                    self.assertIn("st=token", scraper._flaresolverr_listing_urls[("S", 1)])
                    return "https://www.nautiljon.com/mangas/?q=s&st=token&dbt=50", rows if repeated else tail
                scraper.fetch_listing_page = mock.Mock(side_effect=fetch)
                scraper._sleep_delay = mock.Mock()
                scraper.scrape_letter_diff("s", drop_missing=True)
                self.assertEqual(scraper.fetch_listing_page.call_count, 2)
                stats = scraper.session_stats["diff_by_letter"]["S"]
                self.assertEqual(bool(stats.get("pagination_stalled")), repeated)
                self.assertEqual(stats["listing_failed"], repeated)
                if repeated:
                    checkpoint = scraper._load_json_dict(scraper._letter_checkpoint_path("S"))
                    self.assertEqual(checkpoint["page_num"], 1)
                    self.assertTrue(checkpoint["pagination_probe"])
                    self.assertFalse(checkpoint["listing_complete"])
                else:
                    self.assertFalse(os.path.exists(scraper._letter_checkpoint_path("S")))

    def test_resumed_empty_probe_is_not_a_successful_end(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr", detail_mode="deferred")
            scraper.save_letter_files("S", [make_row("S")], partial=True)
            scraper._write_json_atomic(scraper._letter_checkpoint_path("S"), {
                "settings": {"checkpoint_version": LETTER_CHECKPOINT_VERSION, "letter": "s"},
                "page_num": 50, "pagination_probe": True, "listing_complete": False,
                "next_listing_url": "https://www.nautiljon.com/mangas/?q=s&st=token&dbt=2500"})
            scraper.fetch_listing_page = mock.Mock(return_value=("https://www.nautiljon.com/mangas/?q=s&dbt=2500", []))
            scraper.scrape_letter_diff("s")
            scraper.fetch_listing_page.assert_called_once_with("s", 50)
            self.assertTrue(scraper.session_stats["diff_by_letter"]["S"]["pagination_stalled"])
            self.assertTrue(os.path.exists(scraper._letter_checkpoint_path("S")))

    def test_stalled_pagination_continues_letters_but_monthly_does_not_retry(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http", detail_mode="deferred")
            calls = []
            self.install_fake_letter_scrape(scraper, {"S": "pagination_stalled"}, calls)
            scraper._sleep_letter_pause = mock.Mock()
            scraper._monthly_wait = mock.Mock()
            result = scraper.run_monthly(letters=["s", "t"], min_days_between_diff_exports=0)
            self.assertEqual(calls, ["S", "T"])
            self.assertEqual(result.reason, "monthly_diff_pagination_stalled")
            self.assertEqual(result.completed_letters, ["T"])
            self.assertFalse(result.export_paths)
            scraper._monthly_wait.assert_not_called()

    def test_coverage_failure_continues_other_letters_and_never_exports(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            calls = []
            for letter in scraper.get_all_letters():
                self.seed_letter(scraper, letter)
            self.install_fake_letter_scrape(scraper, {"Q": "coverage_failed"}, calls)
            scraper._sleep_letter_pause = mock.Mock()
            scraper.export_all_data = mock.Mock(side_effect=AssertionError("premature export"))
            result = scraper.scrape_all_letters_diff(min_days_between_diff_exports=0)
            self.assertIn("R", calls)
            self.assertIn("#", calls)
            self.assertNotIn("Q", result.completed_letters)
            self.assertEqual(len(result.completed_letters), 26)
            self.assertEqual(result.reason, "coverage_incomplete")
            self.assertIsNone(scraper._load_last_success("diff"))
            scraper.export_all_data.assert_not_called()

    def test_coverage_deadline_survives_restart_and_skips_only_pending_letter(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            checkpoint = {"listing_complete": True, "coverage_pending": True,
                          "updated_at": (datetime.now() - timedelta(minutes=40)).isoformat()}
            scraper._write_json_atomic(scraper._letter_checkpoint_path("Q"), checkpoint)
            restarted = self.make_scraper(out_dir)
            calls = []
            self.install_fake_letter_scrape(restarted, {}, calls)
            restarted._sleep_letter_pause = mock.Mock()
            with mock.patch.dict(os.environ, {"NAUTILJON_COVERAGE_CONFIRMATION_MIN_SECONDS": "3600"}):
                remaining = restarted._coverage_retry_remaining("q")
                self.assertTrue(1190 < remaining <= 1200)
                result = restarted.scrape_all_letters_diff(letters=["q", "r"], min_days_between_diff_exports=0)
                self.assertEqual(calls, ["R"])
                self.assertEqual(result.reason, "coverage_incomplete")
                self.assertEqual(restarted._load_json_dict(restarted._letter_checkpoint_path("Q")), checkpoint)
                checkpoint["updated_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
                restarted._write_json_atomic(restarted._letter_checkpoint_path("Q"), checkpoint)
                self.assertEqual(restarted._coverage_retry_remaining("Q"), 0)
                restarted.scrape_all_letters_diff(letters=["q", "r"], min_days_between_diff_exports=0)
                self.assertEqual(calls, ["R", "Q"])

    def test_monthly_waits_only_until_earliest_coverage_deadline(self):
        for remaining, expected in [([1200, 2000], 1200), ([0, 2000], 0)]:
            with self.subTest(remaining=remaining), tempfile.TemporaryDirectory() as out_dir:
                scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http", detail_mode="deferred")
                scraper.scrape_all_letters_diff = mock.Mock(side_effect=[
                    RunResult(status="partial", reason="coverage_incomplete", requested_letters=["Q", "R"]),
                    RunResult(status="success", reason="complete_catalog_exported")])
                scraper._coverage_retry_remaining = mock.Mock(side_effect=remaining)
                scraper.enrich_detail_queue = mock.Mock(return_value=RunResult(status="success", reason="detail_queue_empty"))
                scraper._monthly_wait = mock.Mock()
                self.assertEqual(scraper.run_monthly().reason, "monthly_complete")
                self.assertEqual(scraper._monthly_wait.call_count, 1)
                self.assertEqual(scraper._monthly_wait.call_args.args[0], expected)

    def test_recent_monthly_export_skips_network_without_moving_success_date(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http", detail_mode="deferred")
            for letter in scraper.get_all_letters():
                self.seed_letter(scraper, letter)
            self.install_fake_letter_scrape(scraper, {})
            exported = scraper.scrape_all_letters_diff(min_days_between_diff_exports=0)
            scraper.mark_success("monthly", exported.rows_count, exported.export_paths)
            before = {mode: scraper._load_last_success(mode) for mode in ("diff", "monthly")}
            scraper.backend = "flaresolverr"
            scraper._resolve_access_cooldown = mock.Mock(side_effect=AssertionError("network check"))
            scraper._flaresolverr_public_ips = mock.Mock(side_effect=AssertionError("IP check"))
            scraper.enrich_detail_queue = mock.Mock(side_effect=AssertionError("empty enrichment"))
            for _ in range(2):
                result = scraper.run_monthly()
                self.assertEqual((result.status, result.reason), ("skipped", "recent_complete_export"))
            self.assertEqual(before, {mode: scraper._load_last_success(mode) for mode in before})
            scraper._resolve_access_cooldown.assert_not_called()
            scraper._flaresolverr_public_ips.assert_not_called()

    def test_recent_monthly_export_still_resumes_pending_details_without_phase_pause(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http", detail_mode="deferred")
            scraper.scrape_all_letters_diff = mock.Mock(return_value=RunResult(
                status="skipped", reason="recent_complete_export"
            ))
            scraper._ready_detail_queue_count = mock.Mock(return_value=1)
            scraper._load_detail_queue = mock.Mock(return_value={"url": {"ready": True}})
            scraper.enrich_detail_queue = mock.Mock(return_value=RunResult(
                status="success", reason="detail_queue_complete"
            ))
            scraper._monthly_wait = mock.Mock()
            self.assertEqual(scraper.run_monthly().reason, "monthly_complete")
            scraper.enrich_detail_queue.assert_called_once()
            scraper._monthly_wait.assert_not_called()

    def test_expired_search_captures_failed_response_once_before_recovery(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            expired = "<html><title>Expiration</title>Votre session de recherche a expiré</html>"
            def load_index():
                scraper._flaresolverr_letter_urls["A"] = "https://www.nautiljon.com/mangas/?q=a&st=fresh"
            def fetch(url):
                scraper._last_flaresolverr_url = url
                return expired if "st=fresh" not in url else "<html></html>"
            scraper._load_flaresolverr_letter_urls = mock.Mock(side_effect=load_index)
            scraper._fetch_html_flaresolverr = mock.Mock(side_effect=fetch)
            scraper.extract_series_list_from_html = mock.Mock(return_value=[])
            for page in (1, 2):
                scraper._flaresolverr_listing_urls[("A", page)] = f"https://www.nautiljon.com/mangas/?q=a&dbt={page * 50}"
                scraper._fetch_listing_page_flaresolverr("a", page)
            self.assertEqual(scraper.session_stats["search_session_expirations"], 2)
            files = os.listdir(os.path.join(out_dir, "debug"))
            self.assertEqual(len(files), 2)
            metadata = scraper._load_json_dict(scraper._last_flaresolverr_debug["metadata"])
            self.assertNotIn("st=fresh", metadata["final_url"])
            with open(scraper._last_flaresolverr_debug["html"], encoding="utf-8") as handle:
                self.assertEqual(handle.read(), expired)

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
            "NAUTILJON_REQUEST_BURST_SIZE",
            "NAUTILJON_REQUEST_BURST_PAUSE_MIN",
            "NAUTILJON_REQUEST_BURST_PAUSE_MAX",
            "NAUTILJON_DETAIL_DELAY_MIN",
            "NAUTILJON_DETAIL_DELAY_MAX",
            "NAUTILJON_DETAIL_BATCH_SIZE",
            "NAUTILJON_DETAIL_BATCH_PAUSE_MIN",
            "NAUTILJON_DETAIL_BATCH_PAUSE_MAX",
            "NAUTILJON_LETTER_PAUSE_MIN",
            "NAUTILJON_LETTER_PAUSE_MAX",
            "NAUTILJON_FAILURE_PAUSE_MIN",
            "NAUTILJON_FAILURE_PAUSE_MAX",
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX",
        }
        clean_env = {key: value for key, value in os.environ.items() if key not in pacing_keys}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            scraper = NautiljonScraper(delay=4, delay_min=4, delay_max=7, backend="http")

        self.assertEqual(scraper.batch_size, 80)
        self.assertEqual((scraper.batch_pause_min, scraper.batch_pause_max), (45.0, 90.0))
        self.assertEqual(scraper.request_burst_size, 20)
        self.assertEqual(
            (scraper.request_burst_pause_min, scraper.request_burst_pause_max),
            (120.0, 240.0),
        )
        self.assertEqual((scraper.detail_delay_min, scraper.detail_delay_max), (10.0, 15.0))
        self.assertEqual(scraper.detail_batch_size, 15)
        self.assertEqual((scraper.detail_batch_pause_min, scraper.detail_batch_pause_max), (45.0, 75.0))
        self.assertEqual((scraper.letter_pause_min, scraper.letter_pause_max), (20.0, 45.0))
        self.assertEqual((scraper.failure_pause_min, scraper.failure_pause_max), (120.0, 300.0))
        self.assertEqual(scraper.block_recovery_attempts, 1)
        self.assertEqual(
            (scraper.block_recovery_pause_min, scraper.block_recovery_pause_max),
            (60.0, 120.0),
        )

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

    def test_pacer_adds_long_pause_after_configured_navigation_burst(self):
        env = {
            "NAUTILJON_REQUEST_BURST_SIZE": "2",
            "NAUTILJON_REQUEST_BURST_PAUSE_MIN": "600",
            "NAUTILJON_REQUEST_BURST_PAUSE_MAX": "600",
            "NAUTILJON_BATCH_SIZE": "0",
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "scraper_nautiljon.time.monotonic", return_value=100.0
        ), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper = NautiljonScraper(delay=1, delay_min=1, delay_max=1, backend="http")
            for _ in range(3):
                scraper._pace_remote_request("https://www.nautiljon.com/mangas/", "test")

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 600.0])

    def test_detail_pacer_adds_a_break_before_sixteenth_detail(self):
        env = {
            "NAUTILJON_DETAIL_BATCH_SIZE": "15",
            "NAUTILJON_DETAIL_BATCH_PAUSE_MIN": "45",
            "NAUTILJON_DETAIL_BATCH_PAUSE_MAX": "45",
        }
        with mock.patch.dict(os.environ, env), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper = NautiljonScraper(delay=0, backend="http")
            for _ in range(16):
                scraper._pace_detail_request()

        sleep.assert_called_once_with(45.0)
        self.assertEqual(scraper.session_stats["detail_requests"], 16)

    def test_detail_block_rotates_flaresolverr_session_and_recovers_once(self):
        env = {
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS": "1",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN": "60",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX": "60",
        }
        with mock.patch.dict(os.environ, env), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper = NautiljonScraper(delay=0, backend="flaresolverr")
            scraper.fetch_html = mock.Mock(
                side_effect=[
                    NautiljonAccessBlockedError("refus temporaire"),
                    "<html>detail valide</html>",
                ]
            )
            scraper.extract_series_detail_from_html = mock.Mock(return_value={"type_detail": "Shonen"})
            scraper.close_flaresolverr = mock.Mock()

            result = scraper._fetch_full_series_data(make_row("F"))

        self.assertIsNotNone(result)
        self.assertEqual(scraper.fetch_html.call_count, 2)
        scraper.close_flaresolverr.assert_called_once_with()
        sleep.assert_called_once_with(60.0)
        self.assertEqual(scraper.session_stats["transient_blocks"], 1)
        self.assertEqual(scraper.session_stats["transient_recoveries"], 1)
        self.assertEqual(scraper.session_stats["confirmed_blocks"], 0)

    def test_repeated_detail_block_is_confirmed_after_spaced_retry(self):
        env = {
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS": "1",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN": "60",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX": "60",
        }
        with mock.patch.dict(os.environ, env), mock.patch("scraper_nautiljon.time.sleep") as sleep:
            scraper = NautiljonScraper(delay=0, backend="flaresolverr")
            scraper.fetch_html = mock.Mock(side_effect=NautiljonAccessBlockedError("toujours bloque"))
            scraper.close_flaresolverr = mock.Mock()

            with self.assertRaisesRegex(NautiljonAccessBlockedError, "Blocage confirme"):
                scraper._fetch_full_series_data(make_row("F"))

        self.assertEqual(scraper.fetch_html.call_count, 2)
        scraper.close_flaresolverr.assert_called_once_with()
        sleep.assert_called_once_with(60.0)
        self.assertEqual(scraper.session_stats["transient_blocks"], 1)
        self.assertEqual(scraper.session_stats["transient_recoveries"], 0)
        self.assertEqual(scraper.session_stats["confirmed_blocks"], 1)

    def test_listing_block_is_retried_with_a_fresh_flaresolverr_session(self):
        env = {
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS": "1",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN": "0",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX": "0",
        }
        with mock.patch.dict(os.environ, env):
            scraper = NautiljonScraper(delay=0, backend="flaresolverr")
            scraper._fetch_listing_page_flaresolverr = mock.Mock(
                side_effect=[
                    NautiljonAccessBlockedError("listing temporairement refuse"),
                    ("https://www.nautiljon.com/mangas/?q=f", [make_row("F")]),
                ]
            )
            scraper.close_flaresolverr = mock.Mock()

            url, rows = scraper.fetch_listing_page("f", 0)

        self.assertEqual(url, "https://www.nautiljon.com/mangas/?q=f")
        self.assertEqual(len(rows), 1)
        self.assertEqual(scraper._fetch_listing_page_flaresolverr.call_count, 2)
        scraper.close_flaresolverr.assert_called_once_with()

    def test_session_rotation_preserves_current_page_pagination_hint(self):
        env = {
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS": "1",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN": "0",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX": "0",
        }
        with mock.patch.dict(os.environ, env):
            scraper = NautiljonScraper(delay=0, backend="flaresolverr")
            key = ("F", 3)
            scraper._flaresolverr_page_has_next[key] = True
            operation = mock.Mock(
                side_effect=[NautiljonAccessBlockedError("temporaire"), "ok"]
            )

            def clear_session_state():
                scraper._flaresolverr_page_has_next.clear()

            scraper.close_flaresolverr = mock.Mock(side_effect=clear_session_state)

            result = scraper._with_flaresolverr_block_recovery(operation, "fiche test")

        self.assertEqual(result, "ok")
        self.assertTrue(scraper._flaresolverr_page_has_next[key])

    def test_missing_listing_values_do_not_trigger_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = {
            "titre": "Fairy Tail",
            "titre_alternatif": "N/A",
            "url_fiche": "https://www.nautiljon.com/mangas/fairy+tail.html",
            "type_liste": "Shonen",
            "nb_vol_vo_liste": "63",
            "nb_vol_vf_liste": "63",
            "age_liste": "12 ans et +",
            "date_vf_liste": "2008",
            "date_vo_liste": "2006",
            "note_liste": "8.39/10",
        }
        current = {key: "N/A" for key in existing}
        current.update({"titre": existing["titre"], "url_fiche": existing["url_fiche"]})

        self.assertFalse(scraper._series_changed_on_list(existing, current))

    def test_real_listing_value_change_still_triggers_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = make_row("F")
        existing["nb_vol_vf_liste"] = "20"
        current = dict(existing)
        current["nb_vol_vf_liste"] = "21"

        self.assertTrue(scraper._series_changed_on_list(existing, current))
        self.assertEqual(
            scraper._series_change_reasons(existing, current),
            {"nb_vol_vf_liste": ("20", "21")},
        )
        self.assertTrue(scraper._series_change_requires_detail(existing, current))

    def test_first_known_volume_value_does_not_trigger_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = make_row("F")
        existing["nb_vol_vf_liste"] = "N/A"
        current = dict(existing)
        current["nb_vol_vf_liste"] = "21"

        self.assertTrue(scraper._series_changed_on_list(existing, current))
        self.assertEqual(
            scraper._series_change_reasons(existing, current),
            {"nb_vol_vf_liste": ("N/A", "21")},
        )
        self.assertFalse(scraper._series_change_requires_detail(existing, current))

    def test_first_observed_listing_status_establishes_baseline_without_refresh(self):
        scraper = self.make_scraper("unused")
        existing = make_row("F")
        existing.update({"nb_vol_vo_liste": "7", "statut_vo_liste": "N/A", "date_vo_liste": "0"})
        current = dict(existing)
        current.update({
            "nb_vol_vo_liste": "7 (En cours)",
            "statut_vo_liste": "En cours",
            "date_vo_liste": "-",
        })

        self.assertFalse(scraper._series_changed_on_list(existing, current))
        self.assertFalse(scraper._series_change_requires_detail(existing, current))

    def test_existing_listing_status_transition_triggers_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = scraper._normalize_row(make_row("F"))
        existing.update({"nb_vol_vo_liste": "7", "statut_vo_liste": "En cours"})
        current = dict(existing)
        current.update({"nb_vol_vo_liste": "7 (Terminé)", "statut_vo_liste": "Terminé"})

        self.assertEqual(
            scraper._series_change_reasons(existing, current),
            {"statut_vo_liste": ("En cours", "Terminé")},
        )
        self.assertTrue(scraper._series_change_requires_detail(existing, current))

    def test_non_volume_listing_change_is_saved_without_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = make_row("F")
        existing["titre_alternatif"] = "Ancien titre"
        current = dict(existing)
        current["titre_alternatif"] = "Nouveau titre"

        self.assertTrue(scraper._series_changed_on_list(existing, current))
        self.assertFalse(scraper._series_change_requires_detail(existing, current))

    def test_note_only_change_is_merged_without_detail_refresh(self):
        scraper = self.make_scraper("unused")
        existing = make_row("F")
        existing.update({"note_liste": "8.39/10", "nb_vol_vf_liste": "20"})
        current = dict(existing)
        current.update({"note_liste": "8.40/10", "nb_vol_vf_liste": "N/A"})

        self.assertFalse(scraper._series_changed_on_list(existing, current))
        merged = scraper._merge_observed_list_fields(existing, current)
        self.assertEqual(merged["note_liste"], "8.40/10")
        self.assertEqual(merged["nb_vol_vf_liste"], "20")

    def test_known_listing_values_are_preserved_when_current_parse_is_missing(self):
        current = {
            "titre": "Fairy Tail",
            "url_fiche": "https://www.nautiljon.com/mangas/fairy+tail.html",
            "type_liste": "N/A",
            "nb_vol_vf_liste": "N/A",
        }
        existing = {
            "titre": "Fairy Tail",
            "url_fiche": current["url_fiche"],
            "type_liste": "Shonen",
            "nb_vol_vf_liste": "63",
        }

        NautiljonScraper._preserve_known_list_fields(current, existing)

        self.assertEqual(current["type_liste"], "Shonen")
        self.assertEqual(current["nb_vol_vf_liste"], "63")

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
            scraper._record_access_cooldown("managed challenge", "203.0.113.10")

            second = self.make_scraper(out_dir)
            second._current_egress_public_ip = mock.Mock(return_value="203.0.113.10")
            result = second.scrape_all_letters_diff(
                letters=["a"],
                min_days_between_diff_exports=0,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "access_cooldown_active")

    def test_access_cooldown_is_cleared_when_public_ip_changes(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper._record_access_cooldown("access_blocked", "203.0.113.10")

            second = self.make_scraper(out_dir)
            second._current_egress_public_ip = mock.Mock(return_value="198.51.100.42")

            self.assertIsNone(second._resolve_access_cooldown())
            self.assertFalse(os.path.exists(second._state_path("access_cooldown")))

    def test_gluetun_auto_rotation_changes_ip_and_clears_cooldown(self):
        env = {
            "NAUTILJON_GLUETUN_AUTO_ROTATE": "true",
            "NAUTILJON_GLUETUN_ROTATE_MIN_INTERVAL_MINUTES": "60",
            "NAUTILJON_GLUETUN_ROTATE_STOP_SECONDS": "0",
            "NAUTILJON_GLUETUN_ROTATE_TIMEOUT_SECONDS": "10",
        }
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.dict(os.environ, env):
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            scraper._record_access_cooldown("access_blocked", "203.0.113.10")
            scraper.close_browser = mock.Mock()
            responses = iter(
                [
                    {"public_ip": "203.0.113.10"},
                    {"status": "stopped"},
                    {"status": "running"},
                    {"status": "running"},
                    {"public_ip": "198.51.100.42"},
                ]
            )
            scraper._gluetun_control_json = mock.Mock(side_effect=lambda *args, **kwargs: next(responses))

            changed = scraper._maybe_rotate_gluetun_for_block("access_blocked")

            self.assertTrue(changed)
            self.assertFalse(os.path.exists(scraper._state_path("access_cooldown")))
            rotation = scraper._load_json_dict(scraper._state_path("gluetun_rotation"))
            self.assertEqual(rotation["outcome"], "ip_changed")
            self.assertEqual(rotation["before_ip"], "203.0.113.10")
            self.assertEqual(rotation["after_ip"], "198.51.100.42")

    def test_gluetun_auto_rotation_rate_limit_is_persistent(self):
        env = {
            "NAUTILJON_GLUETUN_AUTO_ROTATE": "true",
            "NAUTILJON_GLUETUN_ROTATE_MIN_INTERVAL_MINUTES": "60",
        }
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.dict(os.environ, env):
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            scraper._save_gluetun_rotation_state(
                datetime.now(),
                "access_blocked",
                "ip_unchanged",
                "203.0.113.10",
                "203.0.113.10",
            )
            scraper._gluetun_control_json = mock.Mock(
                side_effect=AssertionError("rate-limited rotation must not call Gluetun")
            )

            changed = scraper._maybe_rotate_gluetun_for_block("access_cooldown_active")

            self.assertFalse(changed)
            scraper._gluetun_control_json.assert_not_called()

    def test_legacy_cooldown_uses_one_canary_then_clears(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = self.make_scraper(out_dir)
            scraper._write_json_atomic(
                scraper._state_path("access_cooldown"),
                {
                    "blocked_at": datetime.now().isoformat(timespec="seconds"),
                    "resume_after": (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds"),
                    "cooldown_hours": 24,
                    "reason": "access_blocked",
                },
            )
            scraper._current_egress_public_ip = mock.Mock(return_value="198.51.100.42")
            scraper.fetch_html = mock.Mock(return_value="<html><title>Mangas</title></html>")

            self.assertIsNone(scraper._resolve_access_cooldown())
            scraper.fetch_html.assert_called_once_with("https://www.nautiljon.com/mangas/")
            self.assertFalse(os.path.exists(scraper._state_path("access_cooldown")))

    def test_legacy_cooldown_uses_spaced_flaresolverr_recovery_before_binding_ip(self):
        env = {
            "NAUTILJON_BLOCK_RECOVERY_ATTEMPTS": "1",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MIN": "0",
            "NAUTILJON_BLOCK_RECOVERY_PAUSE_MAX": "0",
        }
        with tempfile.TemporaryDirectory() as out_dir, mock.patch.dict(os.environ, env):
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="flaresolverr")
            scraper._write_json_atomic(
                scraper._state_path("access_cooldown"),
                {
                    "blocked_at": datetime.now().isoformat(timespec="seconds"),
                    "resume_after": (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds"),
                    "cooldown_hours": 24,
                    "reason": "access_blocked",
                },
            )
            scraper._current_egress_public_ip = mock.Mock(return_value="198.51.100.42")
            scraper.fetch_html = mock.Mock(
                side_effect=NautiljonAccessBlockedError("toujours bloque")
            )
            scraper.close_flaresolverr = mock.Mock()

            cooldown = scraper._resolve_access_cooldown()

            self.assertEqual(scraper.fetch_html.call_count, 2)
            scraper.close_flaresolverr.assert_called_once_with()
            self.assertEqual(cooldown["blocked_public_ip"], "198.51.100.42")
            saved = scraper._load_json_dict(scraper._state_path("access_cooldown"))
            self.assertEqual(saved["blocked_public_ip"], "198.51.100.42")

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
                "listing_failed": outcome in {"listing_failed", "pagination_stalled"},
                "pagination_stalled": outcome == "pagination_stalled",
                "access_blocked": outcome == "access_blocked",
                "detail_failed": outcome == "detail_failed",
                "limited": outcome == "limited",
                "coverage_failed": outcome == "coverage_failed",
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

        self.assertNotIn("note_liste", detail)
        self.assertNotIn("nb_vol_vf_liste", detail)
        self.assertNotIn("type_liste", detail)
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
        # Cookie injection causes an extra, unpaced navigation in FlareSolverr.
        self.assertNotIn("cookies", calls[1])
        self.assertNotIn("cookies", calls[2])

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

    def test_pagination_adds_form_token_and_preserves_search_filters(self):
        current = "https://www.nautiljon.com/mangas/?q=o&st=old&edition_sup=2&webcomic=&dbt=400"
        for suffix, expected in [("", "form-token"), ("&amp;st=", "form-token"), ("&amp;st=link-token", "link-token")]:
            with self.subTest(suffix=suffix):
                html = '<input name="st" value="form-token"><a href="?dbt=450' + suffix + '">10</a>'
                url = NautiljonScraper._extract_next_listing_url(html, current, 8)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
                self.assertEqual(query["st"], [expected])
                self.assertEqual(query["q"], ["o"])
                self.assertEqual(query["edition_sup"], ["2"])
                self.assertEqual(query["webcomic"], [""])
                self.assertEqual(query["dbt"], ["450"])

    def test_pagination_falls_back_to_current_token_and_rejects_unrelated_links(self):
        html = '''
        <a href="https://example.org/mangas/?q=o&dbt=450">Wrong host</a>
        <a href="/mangas/other.html?q=o&dbt=450">Wrong path</a>
        <a href="?q=z&dbt=450">Wrong letter</a>
        <a href="?q=o&dbt=500">Wrong page</a>
        <a href="?q=o&dbt=450">Next</a>
        '''
        url = NautiljonScraper._extract_next_listing_url(
            html, "https://www.nautiljon.com/mangas/?q=o&st=current", 8
        )
        self.assertEqual(url, "https://www.nautiljon.com/mangas/?q=o&dbt=450&st=current")

    def test_unsigned_pagination_needs_no_index_reload(self):
        scraper = NautiljonScraper(out_dir="unused", delay=0, backend="flaresolverr")
        scraper._flaresolverr_letter_urls = {"O": "https://www.nautiljon.com/mangas/?q=o&st=initial"}
        calls = []
        def fetch(url):
            calls.append(url)
            scraper._last_flaresolverr_url = url
            if len(calls) == 1:
                return '<input name="st" value="signed"><a href="?q=o&amp;dbt=50" onclick="addToken()">2</a>'
            self.assertIn("st=signed", url)
            return "<html></html>"
        scraper._fetch_html_flaresolverr = mock.Mock(side_effect=fetch)
        scraper.extract_series_list_from_html = mock.Mock(return_value=[])
        scraper._fetch_listing_page_flaresolverr("o", 0)
        scraper._fetch_listing_page_flaresolverr("o", 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(scraper.session_stats.get("search_session_expirations", 0), 0)

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

    def test_deferred_diff_finishes_listing_without_opening_details(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="flaresolverr",
                detail_mode="deferred",
            )
            scraper._mark_detail_baseline_established(1000)
            row = make_row("A")

            def fetch(this, letter, page_num):
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/a", [dict(row)]

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            scraper._fetch_full_series_data = mock.Mock(
                side_effect=AssertionError("deferred diff must not fetch details")
            )

            result = scraper.scrape_letter_diff("a", drop_missing=True, resume=False)

            self.assertEqual(len(result), 1)
            scraper._fetch_full_series_data.assert_not_called()
            queue = scraper._load_detail_queue()
            self.assertEqual(len(queue), 1)
            item = next(iter(queue.values()))
            self.assertTrue(item["ready"])
            self.assertEqual(item["reasons"], ["new_series"])
            self.assertEqual(result[0]["titre_original"], "N/A")

    def test_initial_deferred_baseline_does_not_queue_historical_catalogue(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="flaresolverr",
                detail_mode="deferred",
            )
            row = make_row("A")

            def fetch(this, letter, page_num):
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/a", [dict(row)]

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            result = scraper.scrape_letter_diff("a", drop_missing=True, resume=False)

            self.assertEqual(len(result), 1)
            self.assertEqual(scraper._load_detail_queue(), {})

    def test_deferred_status_change_preserves_details_and_queues_refresh(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="flaresolverr",
                detail_mode="deferred",
            )
            existing = scraper._normalize_row(make_row("F"))
            existing.update({
                "nb_vol_vo_liste": "7 (En cours)",
                "statut_vo_liste": "En cours",
                "genres": "Aventure",
            })
            scraper.save_letter_files("F", [existing], partial=False)
            current = dict(existing)
            current.update({
                "nb_vol_vo_liste": "7 (Terminé)",
                "statut_vo_liste": "Terminé",
            })

            def fetch(this, letter, page_num):
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/f", [current]

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            result = scraper.scrape_letter_diff("f", drop_missing=True, resume=False)

            self.assertEqual(result[0]["statut_vo_liste"], "Terminé")
            self.assertEqual(result[0]["genres"], "Aventure")
            item = next(iter(scraper._load_detail_queue().values()))
            self.assertEqual(item["reasons"], ["status_changed"])
            self.assertTrue(item["ready"])

    def test_deferred_volume_change_preserves_details_and_queues_refresh(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="flaresolverr",
                detail_mode="deferred",
            )
            existing = scraper._normalize_row(make_row("F"))
            existing["nb_vol_vf_liste"] = "4"
            existing["genres"] = "Aventure"
            scraper.save_letter_files("F", [existing], partial=False)
            current = dict(existing)
            current["nb_vol_vf_liste"] = "5"

            def fetch(this, letter, page_num):
                this._flaresolverr_page_has_next[(this._letter_tag(letter), page_num)] = False
                return "https://example.test/f", [current]

            scraper.fetch_listing_page = types.MethodType(fetch, scraper)
            result = scraper.scrape_letter_diff("f", drop_missing=True, resume=False)

            self.assertEqual(result[0]["nb_vol_vf_liste"], "5")
            self.assertEqual(result[0]["genres"], "Aventure")
            item = next(iter(scraper._load_detail_queue().values()))
            self.assertEqual(item["reasons"], ["volume_changed_verified"])
            self.assertTrue(item["ready"])

    def test_enrich_queue_updates_letter_and_removes_successful_item(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="http",
                detail_mode="deferred",
            )
            row = scraper._normalize_row(make_row("A"))
            scraper.save_letter_files("A", [row], partial=False)
            queue = {}
            scraper._queue_detail(queue, "A", row, "new_series", ready=True)
            scraper._save_detail_queue(queue)

            enriched = dict(row)
            enriched["genres"] = "Action"
            scraper._fetch_full_series_data = mock.Mock(return_value=enriched)

            result = scraper.enrich_detail_queue(max_items=1)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.reason, "detail_queue_complete")
            self.assertEqual(scraper._load_detail_queue(), {})
            saved = scraper._load_json_list(scraper._letter_paths("A")[0])
            self.assertEqual(saved[0]["genres"], "Action")
            self.assertTrue(os.path.isfile(result.export_paths["json_path"]))

    def test_legacy_volume_queue_is_pruned_when_detail_is_already_current(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http")
            row = scraper._normalize_row(make_row("A"))
            row["nb_vol_vo_liste"] = "18"
            row["nb_vol_vo_detail"] = "18 (Terminé)"
            scraper.save_letter_files("A", [row], partial=False)
            queue = {}
            scraper._queue_detail(queue, "A", row, "volume_changed", ready=True)
            scraper._save_detail_queue(queue)
            scraper._fetch_full_series_data = mock.Mock(
                side_effect=AssertionError("already current detail must not be fetched")
            )

            result = scraper.enrich_detail_queue(max_items=1)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.reason, "detail_queue_complete")
            self.assertEqual(scraper._load_detail_queue(), {})
            scraper._fetch_full_series_data.assert_not_called()

    def test_legacy_volume_queue_is_kept_when_detail_is_stale(self):
        scraper = self.make_scraper("unused")
        row = scraper._normalize_row(make_row("A"))
        row["nb_vol_vo_liste"] = "18"
        row["nb_vol_vo_detail"] = "17 (En cours)"

        self.assertTrue(scraper._volume_detail_refresh_still_needed(row))

    def test_enrich_block_keeps_item_in_queue(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="http",
                detail_mode="deferred",
            )
            scraper._verified_public_ips = ("198.51.100.10", "198.51.100.10")
            row = scraper._normalize_row(make_row("A"))
            scraper.save_letter_files("A", [row], partial=False)
            queue = {}
            scraper._queue_detail(queue, "A", row, "new_series", ready=True)
            scraper._save_detail_queue(queue)
            scraper._fetch_full_series_data = mock.Mock(
                side_effect=NautiljonAccessBlockedError("refus confirme")
            )

            result = scraper.enrich_detail_queue(max_items=1)

            self.assertEqual(result.status, "partial")
            self.assertEqual(result.reason, "access_blocked")
            kept = scraper._load_detail_queue()
            self.assertIn(row["url_fiche"], kept)
            self.assertEqual(kept[row["url_fiche"]]["attempts"], 1)

    def test_enrich_respects_minimum_interval_without_fetching(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(out_dir=out_dir, delay=0, backend="http")
            row = scraper._normalize_row(make_row("A"))
            scraper.save_letter_files("A", [row], partial=False)
            queue = {}
            scraper._queue_detail(queue, "A", row, "new_series", ready=True)
            scraper._save_detail_queue(queue)
            scraper._write_json_atomic(
                scraper._last_success_path("enrich"),
                {
                    "status": "success",
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
            scraper._fetch_full_series_data = mock.Mock(
                side_effect=AssertionError("minimum interval must skip network access")
            )

            result = scraper.enrich_detail_queue(max_items=12)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "enrich_interval_active")
            scraper._fetch_full_series_data.assert_not_called()
            self.assertEqual(len(scraper._load_detail_queue()), 1)

    def test_monthly_retries_diff_then_drains_detail_queue_automatically(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="http",
                detail_mode="deferred",
            )
            scraper.scrape_all_letters_diff = mock.Mock(
                side_effect=[
                    RunResult(status="partial", reason="listing_inaccessible"),
                    RunResult(
                        status="success",
                        reason="complete_catalog_exported",
                        rows_count=1234,
                        requested_letters=["A"],
                        completed_letters=["A"],
                        export_paths={"json_path": "catalogue.json"},
                    ),
                ]
            )
            scraper._ready_detail_queue_count = mock.Mock(return_value=2)
            scraper.enrich_detail_queue = mock.Mock(
                side_effect=[
                    RunResult(status="success", reason="detail_batch_complete_queue_pending"),
                    RunResult(
                        status="success",
                        reason="detail_queue_complete",
                        export_paths={"json_path": "enriched.json"},
                    ),
                ]
            )
            scraper._monthly_wait = mock.Mock()

            result = scraper.run_monthly(enrich_max_items=12)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.reason, "monthly_complete")
            self.assertEqual(result.rows_count, 1234)
            self.assertEqual(scraper.scrape_all_letters_diff.call_count, 2)
            self.assertEqual(scraper.enrich_detail_queue.call_count, 2)
            self.assertEqual(scraper._monthly_wait.call_count, 2)
            scraper.enrich_detail_queue.assert_called_with(max_items=12, continuous=True, cleanup=False)
            monthly_state = scraper._load_json_dict(scraper._state_path("last_monthly_run"))
            self.assertEqual(monthly_state["status"], "success")

    def test_monthly_retries_immediately_after_successful_gluetun_rotation(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="http",
                detail_mode="deferred",
            )
            scraper.scrape_all_letters_diff = mock.Mock(
                side_effect=[
                    RunResult(status="partial", reason="access_blocked"),
                    RunResult(status="success", reason="complete_catalog_exported"),
                ]
            )
            scraper._maybe_rotate_gluetun_for_block = mock.Mock(return_value=True)
            scraper._ready_detail_queue_count = mock.Mock(return_value=0)
            scraper.enrich_detail_queue = mock.Mock(
                return_value=RunResult(status="success", reason="detail_queue_complete")
            )
            scraper._monthly_wait = mock.Mock()

            result = scraper.run_monthly()

            self.assertEqual(result.reason, "monthly_complete")
            self.assertEqual(scraper.scrape_all_letters_diff.call_count, 2)
            scraper._maybe_rotate_gluetun_for_block.assert_called_once_with("access_blocked")
            scraper._monthly_wait.assert_not_called()

    def test_monthly_stops_on_nonrecoverable_configuration_error(self):
        with tempfile.TemporaryDirectory() as out_dir:
            scraper = NautiljonScraper(
                out_dir=out_dir,
                delay=0,
                backend="http",
                detail_mode="deferred",
            )
            scraper.scrape_all_letters_diff = mock.Mock(
                return_value=RunResult(
                    status="failed",
                    reason="full_catalog_requires_drop_missing",
                )
            )
            scraper._monthly_wait = mock.Mock()

            result = scraper.run_monthly(drop_missing=False)

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.reason,
                "monthly_diff_full_catalog_requires_drop_missing",
            )
            scraper._monthly_wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
