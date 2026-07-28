import json
import os
import tempfile
import types
import unittest
from unittest import mock

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
        return NautiljonScraper(out_dir=out_dir, delay=0, backend="http")

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
            f'<a href="/mangas/?q={label.lower()}&st=token">{label}</a>'
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
        self.assertIn("q=a&st=token", calls[1])
        self.assertIn("q=b&st=token", calls[2])

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
