from __future__ import annotations

import argparse
import csv
import html as html_lib
import ipaddress
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


BASE_URL = "https://www.nautiljon.com"
DEFAULT_RSS_FEEDS = ["http://feeds.feedburner.com/nautiljon/NdFI"]
DEFAULT_TIMEOUT = 45
DEFAULT_DIAGNOSE_TIMEOUT = 15
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
BANNED_TYPE_KEYWORDS = ["yaoi", "yuri"]
DATA_SCHEMA_VERSION = 2
LETTER_CHECKPOINT_VERSION = 2
VF_RELEASE_FIELDS = [
    "dernier_tome_vf_numero", "dernier_tome_vf_date",
    "dernier_tome_vf_url", "dernier_tome_vf_couverture",
    "prochain_tome_vf_numero", "prochain_tome_vf_date",
    "prochain_tome_vf_url", "prochain_tome_vf_couverture",
]
PREFERRED_FIELDS = [
    "url_fiche", "titre", "titre_alternatif", "extraction_time",
    "titre_original", "origine", "annee_vo",
    "type_liste", "type_detail",
    "genres", "themes",
    "scenariste", "dessinateur",
    "editeur_vo", "prepublication",
    "nb_vol_vo_liste", "nb_vol_vf_liste", "nb_vol_vo_detail", "nb_vol_vf_detail",
    "date_vo_liste", "date_vf_liste", "date_vo_detail", "date_vf_detail",
    *VF_RELEASE_FIELDS, "parutions_vf_verifiees_le",
    "note_liste", "note_detail",
    "nb_chapitres_vo", "statut_vo", "nb_chapitres_vf", "statut_vf",
    "age_liste", "age_detail",
    "public_averti", "disponible_france",
]


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_spaces(value: str) -> str:
    value = (value or "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _norm(value: str) -> str:
    value = _clean_spaces(value).lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def _is_na(value: Optional[str]) -> bool:
    return value is None or _clean_spaces(value) in {"", "N/A", "NA", "n/a"}


def _ensure_abs_url(url: str) -> str:
    if not url:
        return ""
    return urljoin(BASE_URL + "/", url)


def _parse_extraction_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = _clean_spaces(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_optional_int(name: str) -> Optional[int]:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "oui", "on"}


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        cleaned = _clean_spaces(value)
        if cleaned and not _is_na(cleaned):
            return cleaned
    return "N/A"


@dataclass
class ListRow:
    titre: str = "N/A"
    titre_alternatif: str = "N/A"
    url_fiche: str = "N/A"
    type_liste: str = "N/A"
    nb_vol_vo_liste: str = "N/A"
    nb_vol_vf_liste: str = "N/A"
    age_liste: str = "N/A"
    date_vf_liste: str = "N/A"
    date_vo_liste: str = "N/A"
    note_liste: str = "N/A"
    extraction_time: str = ""


@dataclass
class RunResult:
    status: str
    reason: str
    rows_count: int = 0
    requested_letters: List[str] = field(default_factory=list)
    completed_letters: List[str] = field(default_factory=list)
    export_paths: Dict[str, str] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.status in {"success", "skipped"} else 1


class NautiljonAccessBlockedError(RuntimeError):
    pass


class NautiljonScraper:
    def __init__(
        self,
        out_dir: str = ".",
        delay: float = 4.0,
        delay_min: Optional[float] = None,
        delay_max: Optional[float] = None,
        backend: str = "selenium",
        browser_headless: bool = False,
    ):
        self.out_dir = out_dir
        self.delay = delay
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.backend = backend.strip().lower()
        self.browser_headless = browser_headless
        self.driver: Optional[webdriver.Chrome] = None
        self.browser_process: Optional[subprocess.Popen] = None
        self._browser_log_handle = None
        self._browser_letter_urls: Dict[str, str] = {}
        self.flaresolverr_session_id: Optional[str] = None
        self._last_flaresolverr_url = ""
        self._last_flaresolverr_html = ""
        self._flaresolverr_letter_urls: Dict[str, str] = {}
        self._flaresolverr_listing_urls: Dict[Tuple[str, int], str] = {}
        self._flaresolverr_page_has_next: Dict[Tuple[str, int], bool] = {}
        self._last_flaresolverr_debug: Dict[str, str] = {}
        conservative_defaults = delay > 0 or delay_min is not None or delay_max is not None
        self.batch_size = max(0, _env_int("NAUTILJON_BATCH_SIZE", 80 if conservative_defaults else 0))
        self.batch_pause_min = max(
            0.0,
            _env_float("NAUTILJON_BATCH_PAUSE_MIN", 45.0 if conservative_defaults else 0.0),
        )
        self.batch_pause_max = max(
            self.batch_pause_min,
            _env_float("NAUTILJON_BATCH_PAUSE_MAX", 90.0 if conservative_defaults else 0.0),
        )
        self.detail_delay_min = max(
            0.0,
            _env_float("NAUTILJON_DETAIL_DELAY_MIN", 10.0 if conservative_defaults else 0.0),
        )
        self.detail_delay_max = max(
            self.detail_delay_min,
            _env_float("NAUTILJON_DETAIL_DELAY_MAX", 15.0 if conservative_defaults else 0.0),
        )
        self.detail_batch_size = max(
            0,
            _env_int("NAUTILJON_DETAIL_BATCH_SIZE", 15 if conservative_defaults else 0),
        )
        self.detail_batch_pause_min = max(
            0.0,
            _env_float("NAUTILJON_DETAIL_BATCH_PAUSE_MIN", 45.0 if conservative_defaults else 0.0),
        )
        self.detail_batch_pause_max = max(
            self.detail_batch_pause_min,
            _env_float("NAUTILJON_DETAIL_BATCH_PAUSE_MAX", 75.0 if conservative_defaults else 0.0),
        )
        self.detail_window_size = max(
            0,
            _env_int("NAUTILJON_DETAIL_WINDOW_SIZE", 5 if conservative_defaults else 0),
        )
        self.detail_window_seconds = max(
            0.0,
            _env_float("NAUTILJON_DETAIL_WINDOW_SECONDS", 900.0 if conservative_defaults else 0.0),
        )
        self.letter_pause_min = max(
            0.0,
            _env_float("NAUTILJON_LETTER_PAUSE_MIN", 20.0 if conservative_defaults else 0.0),
        )
        self.letter_pause_max = max(
            self.letter_pause_min,
            _env_float("NAUTILJON_LETTER_PAUSE_MAX", 45.0 if conservative_defaults else 0.0),
        )
        self.failure_pause_min = max(
            0.0,
            _env_float("NAUTILJON_FAILURE_PAUSE_MIN", 120.0 if conservative_defaults else 0.0),
        )
        self.failure_pause_max = max(
            self.failure_pause_min,
            _env_float("NAUTILJON_FAILURE_PAUSE_MAX", 300.0 if conservative_defaults else 0.0),
        )
        self.block_cooldown_hours = max(0.0, _env_float("NAUTILJON_BLOCK_COOLDOWN_HOURS", 24.0))
        self._remote_request_count = 0
        self._detail_request_count = 0
        self._last_remote_request_at = 0.0
        self._anti_ban_sleep_seconds = 0.0
        self._verified_public_ips: Optional[Tuple[str, str]] = None
        self.session = self._build_session()
        self.session_stats = {
            "total_series": 0,
            "series_by_letter": {},
            "diff_by_letter": {},
            "errors": 0,
            "skipped_by_type": 0,
            "start_time": None,
            "end_time": None,
            "duration": None,
            "remote_requests": 0,
            "detail_requests": 0,
            "anti_ban_sleep_seconds": 0.0,
            "anti_ban_config": {
                "delay_min": self.delay_min if self.delay_min is not None else self.delay,
                "delay_max": self.delay_max if self.delay_max is not None else self.delay,
                "batch_size": self.batch_size,
                "batch_pause_min": self.batch_pause_min,
                "batch_pause_max": self.batch_pause_max,
                "detail_delay_min": self.detail_delay_min,
                "detail_delay_max": self.detail_delay_max,
                "detail_batch_size": self.detail_batch_size,
                "detail_batch_pause_min": self.detail_batch_pause_min,
                "detail_batch_pause_max": self.detail_batch_pause_max,
                "detail_window_size": self.detail_window_size,
                "detail_window_seconds": self.detail_window_seconds,
                "letter_pause_min": self.letter_pause_min,
                "letter_pause_max": self.letter_pause_max,
                "failure_pause_min": self.failure_pause_min,
                "failure_pause_max": self.failure_pause_max,
                "block_cooldown_hours": self.block_cooldown_hours,
            },
        }

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=2.0,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(DEFAULT_HEADERS)
        session.cookies.set("cookieconsent_status", "dismiss", domain=".nautiljon.com")
        return session

    def _compute_delay(self, multiplier: float = 1.0) -> float:
        if self.delay_min is not None and self.delay_max is not None:
            lower = max(0.0, min(self.delay_min, self.delay_max)) * multiplier
            upper = max(0.0, max(self.delay_min, self.delay_max)) * multiplier
            return random.uniform(lower, upper)
        return max(0.0, self.delay * multiplier)

    def _sleep_delay(self, multiplier: float = 1.0) -> float:
        delay = self._compute_delay(multiplier=multiplier)
        self._sleep_for(delay, "temporisation standard", announce=False)
        return delay

    def _sleep_detail_delay(self) -> float:
        delay = random.uniform(self.detail_delay_min, self.detail_delay_max)
        self._sleep_for(delay, "temporisation apres fiche detail", announce=False)
        return delay

    def _pace_detail_request(self) -> None:
        self._pace_detail_window()
        if (
            self.detail_batch_size > 0
            and self._detail_request_count > 0
            and self._detail_request_count % self.detail_batch_size == 0
        ):
            pause = random.uniform(self.detail_batch_pause_min, self.detail_batch_pause_max)
            self._sleep_for(
                pause,
                f"repos apres {self._detail_request_count} fiches detail",
                announce=True,
            )
        self._detail_request_count += 1
        self.session_stats["detail_requests"] = self._detail_request_count

    def _pace_detail_window(self) -> None:
        """Apply a restart-safe rolling limit to expensive browser detail pages."""
        if self.detail_window_size <= 0 or self.detail_window_seconds <= 0:
            return

        path = self._state_path("detail_rate_limit")
        payload = self._load_json_dict(path)
        now = time.time()
        raw_requests = payload.get("requests", []) if payload else []
        requests_in_window: List[float] = []
        if isinstance(raw_requests, list):
            for value in raw_requests:
                try:
                    timestamp = float(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= now - timestamp < self.detail_window_seconds:
                    requests_in_window.append(timestamp)

        requests_in_window.sort()
        if len(requests_in_window) >= self.detail_window_size:
            wait_seconds = max(
                0.0,
                requests_in_window[-self.detail_window_size]
                + self.detail_window_seconds
                - now,
            )
            if wait_seconds > 0:
                self._sleep_for(
                    wait_seconds,
                    (
                        f"limite glissante: {self.detail_window_size} fiches detail "
                        f"par {self.detail_window_seconds / 60:g} min"
                    ),
                    announce=True,
                )
                now = time.time()
                requests_in_window = [
                    timestamp
                    for timestamp in requests_in_window
                    if 0 <= now - timestamp < self.detail_window_seconds
                ]

        requests_in_window.append(now)
        self._write_json_atomic(
            path,
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "window_size": self.detail_window_size,
                "window_seconds": self.detail_window_seconds,
                "requests": requests_in_window,
            },
        )

    @staticmethod
    def _is_nautiljon_url(url: str) -> bool:
        host = (urlsplit(_ensure_abs_url(url)).hostname or "").lower()
        return host == "nautiljon.com" or host.endswith(".nautiljon.com")

    def _sleep_for(self, seconds: float, reason: str, announce: bool = True) -> float:
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            return 0.0
        if announce:
            print(f"Pause anti-ban {seconds:.1f}s: {reason}")
        time.sleep(seconds)
        self._anti_ban_sleep_seconds += seconds
        self.session_stats["anti_ban_sleep_seconds"] = round(self._anti_ban_sleep_seconds, 3)
        return seconds

    def _pace_remote_request(self, url: str, context: str) -> float:
        """Serialize and pace every Nautiljon navigation from every backend."""
        if not self._is_nautiljon_url(url):
            return 0.0

        wait_seconds = 0.0
        wait_reason = f"avant {context}"
        now = time.monotonic()
        if self._last_remote_request_at > 0:
            target_delay = self._compute_delay()
            elapsed = max(0.0, now - self._last_remote_request_at)
            wait_seconds = max(0.0, target_delay - elapsed)

        if (
            self.batch_size > 0
            and self._remote_request_count > 0
            and self._remote_request_count % self.batch_size == 0
        ):
            batch_pause = random.uniform(self.batch_pause_min, self.batch_pause_max)
            if batch_pause > wait_seconds:
                wait_seconds = batch_pause
                wait_reason = f"repos apres {self._remote_request_count} requetes"

        self._sleep_for(wait_seconds, wait_reason, announce=wait_seconds >= 5)
        self._last_remote_request_at = time.monotonic()
        self._remote_request_count += 1
        self.session_stats["remote_requests"] = self._remote_request_count
        return wait_seconds

    def _sleep_letter_pause(self) -> float:
        pause = random.uniform(self.letter_pause_min, self.letter_pause_max)
        return self._sleep_for(pause, "repos entre deux lettres", announce=True)

    def _sleep_failure_pause(self) -> float:
        pause = random.uniform(self.failure_pause_min, self.failure_pause_max)
        return self._sleep_for(pause, "erreur reseau avant nouvelle tentative", announce=True)

    def _ensure_dirs(self) -> Tuple[str, str, str, str]:
        exports_dir = os.path.join(self.out_dir, "exports")
        checkpoints_dir = os.path.join(self.out_dir, "checkpoints")
        letters_dir = os.path.join(self.out_dir, "letters")
        state_dir = os.path.join(self.out_dir, "state")
        for path in (exports_dir, checkpoints_dir, letters_dir, state_dir):
            os.makedirs(path, exist_ok=True)
        return exports_dir, checkpoints_dir, letters_dir, state_dir

    def _discovery_dir(self) -> str:
        path = os.path.join(self.out_dir, "discovery")
        os.makedirs(path, exist_ok=True)
        return path

    def _letter_paths(self, letter_tag: str) -> Tuple[str, str, str, str]:
        _, _, letters_dir, _ = self._ensure_dirs()
        base = os.path.join(letters_dir, f"nautiljon_lettre_{letter_tag}")
        return base + ".json", base + ".csv", base + ".partial.json", base + ".partial.csv"

    def _last_success_path(self, mode: str = "diff") -> str:
        _, _, _, state_dir = self._ensure_dirs()
        safe_mode = re.sub(r"[^a-z0-9_.-]+", "_", mode.lower()).strip("_") or "diff"
        return os.path.join(state_dir, f"last_{safe_mode}_success.json")

    def _load_json_list(self, path: str) -> List[Dict[str, str]]:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except Exception:
            pass
        return []

    def _load_last_success(self, mode: str = "diff") -> Optional[Dict[str, object]]:
        try:
            with open(self._last_success_path(mode), "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return None

    def should_skip_recent_success(self, mode: str, min_days: int) -> Tuple[bool, Optional[Dict[str, object]], Optional[timedelta]]:
        if min_days <= 0:
            return False, None, None
        data = self._load_last_success(mode)
        if not data:
            return False, None, None
        if data.get("status") not in {None, "success"}:
            return False, data, None
        if mode == "diff" and data.get("data_schema_version") != DATA_SCHEMA_VERSION:
            print("Ancien marqueur de succes ignore: schema de donnees obsolete.")
            return False, data, None
        export_paths = data.get("export_paths")
        if not isinstance(export_paths, dict) or not self._validate_final_exports(export_paths):
            print("Ancien marqueur de succes ignore: export final absent ou incomplet.")
            return False, data, None
        completed_at = str(data.get("completed_at", ""))
        try:
            age = datetime.now() - datetime.fromisoformat(completed_at)
        except ValueError:
            return False, data, None
        return age < timedelta(days=min_days), data, age

    def mark_success(self, mode: str, rows_count: int, export_paths: Dict[str, str]) -> None:
        payload = {
            "status": "success",
            "data_schema_version": DATA_SCHEMA_VERSION,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "rows_count": rows_count,
            "export_paths": export_paths,
            "session_stats": self.session_stats,
        }
        path = self._last_success_path(mode)
        self._write_json_atomic(path, payload)
        print(f"  OK marqueur de succes {mode}: {path}")

    def _write_json_atomic(self, path: str, payload: object) -> None:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, path)

    def _state_path(self, name: str) -> str:
        _, checkpoints_dir, _, state_dir = self._ensure_dirs()
        if name == "diff_checkpoint":
            return os.path.join(checkpoints_dir, "diff_run.json")
        safe_name = re.sub(r"[^a-z0-9_.-]+", "_", name.lower()).strip("_") or "run"
        return os.path.join(state_dir, f"{safe_name}.json")

    def _record_access_cooldown(self, reason: str, blocked_public_ip: str = "") -> str:
        blocked_at = datetime.now()
        resume_after = blocked_at + timedelta(hours=self.block_cooldown_hours)
        if not blocked_public_ip and self._verified_public_ips:
            direct_ip, flaresolverr_ip = self._verified_public_ips
            if direct_ip == flaresolverr_ip:
                blocked_public_ip = direct_ip
        if not blocked_public_ip:
            try:
                blocked_public_ip = self._current_egress_public_ip()
            except Exception:
                blocked_public_ip = ""
        payload = {
            "blocked_at": blocked_at.isoformat(timespec="seconds"),
            "resume_after": resume_after.isoformat(timespec="seconds"),
            "cooldown_hours": self.block_cooldown_hours,
            "reason": reason,
            "blocked_public_ip": blocked_public_ip,
        }
        path = self._state_path("access_cooldown")
        self._write_json_atomic(path, payload)
        print(
            "Quarantaine anti-ban active jusqu'au "
            f"{payload['resume_after']} ({self.block_cooldown_hours:g} h)."
        )
        return path

    def _active_access_cooldown(self) -> Optional[Dict[str, object]]:
        path = self._state_path("access_cooldown")
        payload = self._load_json_dict(path)
        if not payload:
            return None
        try:
            resume_after = datetime.fromisoformat(str(payload.get("resume_after", "")))
        except ValueError:
            return None
        if datetime.now() >= resume_after:
            return None
        payload["path"] = path
        return payload

    def _current_egress_public_ip(self) -> str:
        if self._verified_public_ips:
            direct_ip, flaresolverr_ip = self._verified_public_ips
        elif self.backend == "flaresolverr":
            direct_ip, flaresolverr_ip = self._flaresolverr_public_ips()
            self._verified_public_ips = (direct_ip, flaresolverr_ip)
        else:
            response = requests.get("http://api.ipify.org?format=json", timeout=15)
            response.raise_for_status()
            direct_ip = str(ipaddress.ip_address(response.json().get("ip", "")))
            flaresolverr_ip = direct_ip
            self._verified_public_ips = (direct_ip, flaresolverr_ip)
        if direct_ip != flaresolverr_ip:
            raise RuntimeError("les IP publiques de Gluetun et FlareSolverr sont differentes")
        return direct_ip

    def _resolve_access_cooldown(self) -> Optional[Dict[str, object]]:
        cooldown = self._active_access_cooldown()
        if not cooldown:
            return None
        try:
            current_ip = self._current_egress_public_ip()
        except Exception as exc:
            print(
                "Quarantaine conservee: IP publique actuelle impossible a verifier "
                f"({str(exc)[:180]})."
            )
            return cooldown

        blocked_ip = str(cooldown.get("blocked_public_ip", "") or "").strip()
        if blocked_ip:
            if current_ip != blocked_ip:
                self._clear_access_cooldown()
                print(
                    f"IP de sortie changee ({blocked_ip} -> {current_ip}): "
                    "quarantaine anti-ban levee automatiquement."
                )
                return None
            return cooldown

        # Compatibilite avec les marqueurs crees avant l'enregistrement de l'IP.
        # Un unique canari permet de migrer sans lever aveuglement une interdiction.
        print(
            "Ancienne quarantaine sans IP enregistree: verification unique de la "
            f"nouvelle sortie {current_ip}."
        )
        try:
            self.fetch_html(f"{BASE_URL}/mangas/")
        except NautiljonAccessBlockedError:
            cooldown.pop("path", None)
            cooldown["blocked_public_ip"] = current_ip
            self._write_json_atomic(self._state_path("access_cooldown"), cooldown)
            return cooldown
        except Exception as exc:
            print(
                "Quarantaine conservee: canari de migration non concluant "
                f"({str(exc)[:180]})."
            )
            return cooldown

        self._clear_access_cooldown()
        print(
            f"Canari reussi depuis {current_ip}: ancienne quarantaine anti-ban levee automatiquement."
        )
        return None

    def _clear_access_cooldown(self) -> None:
        path = self._state_path("access_cooldown")
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            print(f"Avertissement: quarantaine anti-ban non supprimee ({str(exc)[:160]}).")

    def mark_run_state(self, mode: str, result: RunResult) -> str:
        payload = {
            "mode": mode,
            "data_schema_version": DATA_SCHEMA_VERSION,
            "status": result.status,
            "reason": result.reason,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "rows_count": result.rows_count,
            "requested_letters": result.requested_letters,
            "completed_letters": result.completed_letters,
            "export_paths": result.export_paths,
            "session_stats": self.session_stats,
        }
        path = self._state_path(f"last_{mode}_run")
        self._write_json_atomic(path, payload)
        print(f"  ETAT {mode.upper()}: {result.status.upper()} ({result.reason})")
        print(f"  Rapport d'etat: {path}")
        return path

    def _load_json_dict(self, path: str) -> Optional[Dict[str, object]]:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _validate_final_exports(self, export_paths: Dict[str, str]) -> bool:
        required = ("json_path", "csv_path", "stats_path", "report_path")
        return all(
            isinstance(export_paths.get(key), str)
            and os.path.isfile(export_paths[key])
            and os.path.getsize(export_paths[key]) > 0
            for key in required
        )

    def _validate_final_letter_files(self, letters: List[str]) -> bool:
        for letter in letters:
            final_json, final_csv, _, _ = self._letter_paths(self._letter_tag(letter))
            if not os.path.isfile(final_json) or not os.path.isfile(final_csv):
                return False
            if os.path.getsize(final_json) == 0 or os.path.getsize(final_csv) == 0:
                return False
            if not self._load_json_list(final_json):
                return False
        return True

    def _diff_run_config(
        self,
        letters: List[str],
        max_pages_per_letter: Optional[int],
        max_series_per_letter: Optional[int],
        refresh_stale_days: Optional[int],
        drop_missing: bool,
        max_missing_ratio: float = 0.15,
    ) -> Dict[str, object]:
        return {
            "data_schema_version": DATA_SCHEMA_VERSION,
            "letters": letters,
            "max_pages_per_letter": max_pages_per_letter,
            "max_series_per_letter": max_series_per_letter,
            "refresh_stale_days": refresh_stale_days,
            "drop_missing": drop_missing,
            "max_missing_ratio": max_missing_ratio,
        }

    def _load_diff_run_checkpoint(self, config: Dict[str, object], resume: bool) -> List[str]:
        if not resume:
            return []
        checkpoint = self._load_json_dict(self._state_path("diff_checkpoint"))
        if not checkpoint or checkpoint.get("config") != config:
            return []
        completed = checkpoint.get("completed_letters", [])
        if not isinstance(completed, list):
            return []
        configured_letters = config.get("letters", [])
        configured_labels = {
            self._letter_label(str(letter))
            for letter in configured_letters
        } if isinstance(configured_letters, list) else set()
        result = [str(letter) for letter in completed if str(letter) in configured_labels]
        if result:
            print(f"Reprise du diff: {len(result)} lettre(s) deja finalisee(s): {', '.join(result)}")
        return result

    def _save_diff_run_checkpoint(self, config: Dict[str, object], completed_letters: List[str]) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "config": config,
            "completed_letters": completed_letters,
        }
        self._write_json_atomic(self._state_path("diff_checkpoint"), payload)

    def _letter_checkpoint_path(self, letter_tag: str) -> str:
        _, checkpoints_dir, _, _ = self._ensure_dirs()
        return os.path.join(checkpoints_dir, f"nautiljon_lettre_{letter_tag}.json")

    def _archive_letter_progress(self, letter_tag: str, checkpoint_path: str, reason: str) -> Optional[str]:
        paths = [checkpoint_path, *self._letter_paths(letter_tag)[2:]]
        existing_paths = [path for path in paths if os.path.isfile(path)]
        if not existing_paths:
            return None
        _, checkpoints_dir, _, _ = self._ensure_dirs()
        safe_reason = re.sub(r"[^a-z0-9_-]+", "_", _norm(reason)).strip("_") or "incompatible"
        archive_dir = os.path.join(
            checkpoints_dir,
            "archive",
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{letter_tag}_{safe_reason}",
        )
        os.makedirs(archive_dir, exist_ok=True)
        for path in existing_paths:
            shutil.copy2(path, os.path.join(archive_dir, os.path.basename(path)))
        print(f"  Progression precedente archivee avant remplacement: {archive_dir}")
        return archive_dir

    def _letter_checkpoint_is_compatible(self, letter_tag: str, checkpoint: Dict[str, object]) -> bool:
        settings = checkpoint.get("settings")
        if not isinstance(settings, dict):
            return False
        saved_letter = str(settings.get("letter", "") or "")
        try:
            checkpoint_version = int(settings.get("checkpoint_version", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (
            bool(saved_letter)
            and self._letter_tag(saved_letter) == letter_tag
            and checkpoint_version == LETTER_CHECKPOINT_VERSION
        )

    @staticmethod
    def _checkpoint_listing_is_complete(checkpoint: Dict[str, object]) -> bool:
        if checkpoint.get("listing_complete") is True:
            return True
        try:
            page_num = int(checkpoint.get("page_num", 0) or 0)
            accessible = int(checkpoint.get("accessible_listing_pages", 0) or 0)
            successful = int(checkpoint.get("successful_listing_pages", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (
            page_num > 0
            and page_num == accessible == successful
            and not str(checkpoint.get("next_listing_url", "") or "")
        )

    def _coverage_checkpoint_is_complete(
        self,
        letter_tag: str,
        checkpoint: Dict[str, object],
    ) -> bool:
        if checkpoint.get("listing_complete") is True:
            return True
        if not self._checkpoint_listing_is_complete(checkpoint):
            return False
        report = self._load_json_dict(self._state_path("last_diff_run"))
        if not report or report.get("reason") != "coverage_incomplete":
            return False
        session_stats = report.get("session_stats")
        diff_by_letter = session_stats.get("diff_by_letter") if isinstance(session_stats, dict) else None
        counters = diff_by_letter.get(letter_tag) if isinstance(diff_by_letter, dict) else None
        return bool(
            isinstance(counters, dict)
            and counters.get("coverage_failed")
            and not counters.get("listing_failed")
            and not counters.get("detail_failed")
            and not counters.get("limited")
        )

    def _coverage_gap_confirmed_by_archive(
        self,
        letter_tag: str,
        baseline_urls: set,
        current_missing_urls: set,
        current_rows_count: int,
        current_checkpoint: Dict[str, object],
    ) -> bool:
        if not current_missing_urls or not baseline_urls:
            return False
        ratio = len(current_missing_urls) / len(baseline_urls)
        max_confirmed_ratio = max(
            0.0,
            _env_float("NAUTILJON_MAX_CONFIRMED_MISSING_RATIO", 0.35),
        )
        if ratio > max_confirmed_ratio:
            return False
        try:
            current_time = datetime.fromisoformat(str(current_checkpoint.get("updated_at", "")))
        except ValueError:
            return False
        min_age_seconds = max(
            0.0,
            _env_float("NAUTILJON_COVERAGE_CONFIRMATION_MIN_SECONDS", 3600.0),
        )
        _, checkpoints_dir, _, _ = self._ensure_dirs()
        archive_root = os.path.join(checkpoints_dir, "archive")
        if not os.path.isdir(archive_root):
            return False
        checkpoint_name = f"nautiljon_lettre_{letter_tag}.json"
        partial_name = f"nautiljon_lettre_{letter_tag}.partial.json"
        with os.scandir(archive_root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                archived_checkpoint = self._load_json_dict(os.path.join(entry.path, checkpoint_name))
                archived_rows = self._load_json_list(os.path.join(entry.path, partial_name))
                if not archived_checkpoint or not archived_rows:
                    continue
                if not self._checkpoint_listing_is_complete(archived_checkpoint):
                    continue
                try:
                    archived_time = datetime.fromisoformat(str(archived_checkpoint.get("updated_at", "")))
                except ValueError:
                    continue
                if abs((current_time - archived_time).total_seconds()) < min_age_seconds:
                    continue
                archived_urls = {
                    _ensure_abs_url(row.get("url_fiche", ""))
                    for row in archived_rows
                    if row.get("url_fiche")
                }
                if baseline_urls - archived_urls != current_missing_urls:
                    continue
                stable_margin = max(2, int(max(current_rows_count, len(archived_rows)) * 0.02))
                if abs(current_rows_count - len(archived_rows)) > stable_margin:
                    continue
                return True
        return False

    def _remove_checkpoint(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _write_csv(self, path: str, rows: List[Dict[str, str]]) -> None:
        keys = set()
        for row in rows:
            keys.update(row.keys())
        fieldnames = [field for field in PREFERRED_FIELDS]
        extras = sorted(key for key in keys if key not in set(PREFERRED_FIELDS))
        fieldnames.extend(extras)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _clean_spaces(str(row.get(field, "N/A") or "N/A")) for field in fieldnames})
        os.replace(tmp_path, path)

    def save_letter_files(self, letter_tag: str, rows: List[Dict[str, str]], partial: bool = False) -> None:
        final_json, final_csv, partial_json, partial_csv = self._letter_paths(letter_tag)
        json_path = partial_json if partial else final_json
        csv_path = partial_csv if partial else final_csv
        tmp_json = json_path + ".tmp"
        with open(tmp_json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_json, json_path)
        self._write_csv(csv_path, rows)
        if not partial:
            print(f"  Lettre sauvegardee: {os.path.basename(final_json)} / {os.path.basename(final_csv)}")

    def save_controlled_letter_files(self, letter_tag: str, rows: List[Dict[str, str]]) -> Dict[str, str]:
        control_dir = os.path.join(self.out_dir, "control")
        os.makedirs(control_dir, exist_ok=True)
        base = os.path.join(control_dir, f"nautiljon_lettre_{letter_tag}.control")
        json_path = base + ".json"
        csv_path = base + ".csv"
        tmp_json = json_path + ".tmp"
        with open(tmp_json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_json, json_path)
        self._write_csv(csv_path, rows)
        print(f"  Controle sauvegarde sans modifier la lettre finale: {json_path} / {csv_path}")
        return {"json_path": json_path, "csv_path": csv_path}

    def _letter_cache_paths(self, letter_tag: str) -> Tuple[str, str, str]:
        cache_dir = os.path.join(self.out_dir, "letter-cache")
        os.makedirs(cache_dir, exist_ok=True)
        base = os.path.join(cache_dir, f"nautiljon_lettre_{letter_tag}.validated")
        marker_path = self._state_path(f"letter_{letter_tag}_success")
        return base + ".json", base + ".csv", marker_path

    def _save_letter_cache(
        self,
        letter_tag: str,
        rows: List[Dict[str, str]],
        counters: Dict[str, object],
        source_mode: str,
        completed_at: Optional[str] = None,
    ) -> Dict[str, object]:
        json_path, csv_path, marker_path = self._letter_cache_paths(letter_tag)
        normalized_rows = [self._normalize_row(row) for row in rows]
        self._write_json_atomic(json_path, normalized_rows)
        self._write_csv(csv_path, normalized_rows)
        payload: Dict[str, object] = {
            "status": "success",
            "version": DATA_SCHEMA_VERSION,
            "letter": letter_tag,
            "completed_at": completed_at or datetime.now().isoformat(timespec="seconds"),
            "rows_count": len(normalized_rows),
            "json_path": json_path,
            "csv_path": csv_path,
            "source_mode": source_mode,
            "backend": self.backend,
            "excluded_types": list(BANNED_TYPE_KEYWORDS),
            "stats": dict(counters),
        }
        self._write_json_atomic(marker_path, payload)
        print(f"  Cache valide {letter_tag}: {len(normalized_rows)} series ({marker_path})")
        return payload

    def _validate_letter_cache(
        self,
        letter_tag: str,
        marker: Dict[str, object],
        min_days: int,
        max_missing_ratio: float,
    ) -> Optional[Tuple[List[Dict[str, str]], timedelta]]:
        if min_days <= 0 or marker.get("status") != "success" or marker.get("letter") != letter_tag:
            return None
        if marker.get("version") != DATA_SCHEMA_VERSION:
            return None
        if marker.get("excluded_types") != list(BANNED_TYPE_KEYWORDS):
            return None
        try:
            age = datetime.now() - datetime.fromisoformat(str(marker.get("completed_at", "")))
        except ValueError:
            return None
        if age < timedelta(0) or age >= timedelta(days=min_days):
            return None
        stats = marker.get("stats")
        if not isinstance(stats, dict):
            return None
        if any(stats.get(key) for key in ("listing_failed", "access_blocked", "coverage_failed", "detail_failed", "limited")):
            return None
        try:
            missing_ratio = float(stats.get("missing_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if max_missing_ratio >= 0 and missing_ratio > max_missing_ratio:
            return None
        json_path = marker.get("json_path")
        csv_path = marker.get("csv_path")
        if not isinstance(json_path, str) or not isinstance(csv_path, str):
            return None
        if not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0:
            return None
        rows = [self._normalize_row(row) for row in self._load_json_list(json_path)]
        if not rows or len(rows) != int(marker.get("rows_count", 0) or 0):
            return None
        urls = [_ensure_abs_url(row.get("url_fiche", "")) for row in rows]
        if any(not url for url in urls) or len(set(urls)) != len(rows):
            return None
        if any(self._letter_tag_for_row(row) != letter_tag for row in rows):
            return None
        return rows, age

    def _adopt_recent_control_cache(
        self,
        letter_tag: str,
        min_days: int,
        max_missing_ratio: float,
    ) -> Optional[Dict[str, object]]:
        report = self._load_json_dict(self._state_path("last_diff_run"))
        if not report or report.get("status") != "partial" or report.get("reason") != "controlled_subset_complete":
            return None
        if report.get("data_schema_version") != DATA_SCHEMA_VERSION:
            return None
        completed = report.get("completed_letters")
        if not isinstance(completed, list) or letter_tag not in completed:
            return None
        completed_at = str(report.get("completed_at", ""))
        try:
            age = datetime.now() - datetime.fromisoformat(completed_at)
        except ValueError:
            return None
        if min_days <= 0 or age < timedelta(0) or age >= timedelta(days=min_days):
            return None
        session_stats = report.get("session_stats")
        diff_by_letter = session_stats.get("diff_by_letter") if isinstance(session_stats, dict) else None
        counters = diff_by_letter.get(letter_tag) if isinstance(diff_by_letter, dict) else None
        if not isinstance(counters, dict):
            return None
        if any(counters.get(key) for key in ("listing_failed", "access_blocked", "coverage_failed", "detail_failed", "limited")):
            return None
        try:
            missing_ratio = float(counters.get("missing_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if max_missing_ratio >= 0 and missing_ratio > max_missing_ratio:
            return None
        if int(counters.get("missing_count", 0) or 0) != 0:
            print(
                f"  Controle recent {letter_tag} non adopte: il contient des fiches historiques "
                "conservees mais non observees."
            )
            return None
        base = os.path.join(self.out_dir, "control", f"nautiljon_lettre_{letter_tag}.control")
        control_json = base + ".json"
        control_csv = base + ".csv"
        rows = [self._normalize_row(row) for row in self._load_json_list(control_json)]
        if not rows or not os.path.isfile(control_csv) or os.path.getsize(control_csv) == 0:
            return None
        print(f"  Adoption du controle recent de la lettre {letter_tag} dans le cache valide.")
        return self._save_letter_cache(
            letter_tag,
            rows,
            counters,
            source_mode="controlled_legacy",
            completed_at=completed_at,
        )

    def _load_reusable_letter_cache(
        self,
        letter_tag: str,
        min_days: int,
        max_missing_ratio: float,
    ) -> Optional[Tuple[List[Dict[str, str]], Dict[str, object], timedelta]]:
        _, _, marker_path = self._letter_cache_paths(letter_tag)
        marker = self._load_json_dict(marker_path)
        if not marker:
            marker = self._adopt_recent_control_cache(letter_tag, min_days, max_missing_ratio)
        if not marker:
            return None
        validated = self._validate_letter_cache(letter_tag, marker, min_days, max_missing_ratio)
        if not validated:
            return None
        rows, age = validated
        return rows, marker, age

    def _remove_partial_files(self, letter_tag: str) -> None:
        _, _, partial_json, partial_csv = self._letter_paths(letter_tag)
        for path in (partial_json, partial_csv):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def concat_letters(self) -> List[Dict[str, str]]:
        _, _, letters_dir, _ = self._ensure_dirs()
        rows: List[Dict[str, str]] = []
        files = [
            os.path.join(letters_dir, name)
            for name in os.listdir(letters_dir)
            if name.startswith("nautiljon_lettre_") and name.endswith(".json") and ".partial." not in name
        ]

        def sort_key(path: str) -> Tuple[int, str]:
            match = re.search(r"nautiljon_lettre_(.+)\.json$", os.path.basename(path))
            tag = match.group(1) if match else path
            return (0, tag) if tag == "HASH" else (1, tag)

        for path in sorted(files, key=sort_key):
            rows.extend(self._load_json_list(path))
        return rows

    def _known_urls(self) -> set:
        urls = set()
        for row in self.concat_letters():
            url = _ensure_abs_url(row.get("url_fiche", ""))
            if url:
                urls.add(url)
        return urls

    def _letter_tag_from_title(self, title: str) -> str:
        normalized = _norm(title)
        for char in normalized:
            if "a" <= char <= "z":
                return char.upper()
            if char.isdigit():
                return "HASH"
        return "HASH"

    def _candidate_url_from_title(self, title: str) -> str:
        normalized = _norm(title)
        normalized = normalized.replace("&", " et ")
        slug = re.sub(r"[^a-z0-9]+", "+", normalized).strip("+")
        return f"{BASE_URL}/mangas/{slug}.html" if slug else ""

    def _extract_candidate_titles_from_news_title(self, title: str) -> List[str]:
        cleaned = _clean_spaces(html_lib.unescape(title)).strip(" -")
        patterns = [
            r"^(?P<title>.+?),\s+nouveau titre\b",
            r"^(?P<title>.+?)\s+revient\s+en\s+manga\b",
            r"^Le manga (?P<title>.+?)\s+(?:a|à)\s+para[iî]tre\b",
            r"^Le manga (?P<title>.+?)\s+revient\b",
            r"^Le manga (?P<title>.+?)\s+arrive\b",
            r"^Le manga (?P<title>.+?)\s+d[eé]barque\b",
            r"^Le manga (?P<title>.+?)\s+chez\b",
        ]
        titles: List[str] = []
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                titles.append(match.group("title"))
        if not titles and re.search(r"\bmanga\b", cleaned, re.IGNORECASE):
            trimmed = re.sub(r"^Le manga\s+", "", cleaned, flags=re.IGNORECASE)
            trimmed = re.split(
                r"\s+(?:aux|a|à|revient|arrive|d[eé]barque|est annonc[eé]|sortira)\b",
                trimmed,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            titles.append(trimmed)

        result: List[str] = []
        seen = set()
        for value in titles:
            value = _clean_spaces(value).strip(" \"'“”«»")
            if len(value) < 2:
                continue
            key = _norm(value)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def discover_rss_candidates(
        self,
        feed_urls: Optional[List[str]] = None,
        merge_candidates: bool = False,
    ) -> List[Dict[str, str]]:
        feed_urls = feed_urls or DEFAULT_RSS_FEEDS
        known_urls = self._known_urls()
        candidates: List[Dict[str, str]] = []
        seen_urls = set(known_urls)

        for feed_url in feed_urls:
            print(f"Flux RSS: {feed_url}")
            response = self.session.get(feed_url, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
            import xml.etree.ElementTree as ET

            root = ET.fromstring(response.text)
            for item in root.findall("./channel/item"):
                news_title = item.findtext("title") or ""
                news_link = item.findtext("link") or ""
                if "/actualite/mangas/" not in news_link and "manga" not in _norm(news_title):
                    continue
                pub_date = item.findtext("pubDate") or ""
                parsed_date = ""
                if pub_date:
                    try:
                        parsed_date = parsedate_to_datetime(pub_date).isoformat()
                    except Exception:
                        parsed_date = pub_date
                for title in self._extract_candidate_titles_from_news_title(news_title):
                    if self.is_banned_type(title):
                        self.session_stats["skipped_by_type"] += 1
                        continue
                    url = self._candidate_url_from_title(title)
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    candidates.append(self._normalize_row({
                        "titre": title,
                        "url_fiche": url,
                        "extraction_time": _now_str(),
                        "discovery_source": "rss",
                        "discovery_news_title": news_title,
                        "discovery_news_url": news_link,
                        "discovery_pub_date": parsed_date or "N/A",
                        "discovery_status": "candidate_unverified",
                    }))

        discovery_dir = self._discovery_dir()
        json_path = os.path.join(discovery_dir, "nautiljon_rss_candidates.json")
        csv_path = os.path.join(discovery_dir, "nautiljon_rss_candidates.csv")
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(candidates, handle, ensure_ascii=False, indent=2)
        self._write_csv(csv_path, candidates)
        self.session_stats["rss_candidates"] = len(candidates)
        print(f"OK candidats RSS: {csv_path} ({len(candidates)} nouveaux candidats)")

        if merge_candidates and candidates:
            self.merge_candidate_rows(candidates)
        return candidates

    def merge_candidate_rows(self, candidates: List[Dict[str, str]]) -> int:
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for row in candidates:
            grouped.setdefault(self._letter_tag_from_title(row.get("titre", "")), []).append(row)

        added = 0
        for tag, rows in sorted(grouped.items()):
            final_json, _, _, _ = self._letter_paths(tag)
            existing_rows = self._load_json_list(final_json)
            if not existing_rows:
                existing_rows = self._read_existing_csv_for_letter(tag)
            known = {_ensure_abs_url(row.get("url_fiche", "")) for row in existing_rows if row.get("url_fiche")}
            for row in rows:
                url = _ensure_abs_url(row.get("url_fiche", ""))
                if url and url not in known:
                    existing_rows.append(row)
                    known.add(url)
                    added += 1
            if rows:
                existing_rows = sorted([self._normalize_row(row) for row in existing_rows], key=lambda row: _norm(row.get("titre", "")))
                self.save_letter_files(tag, existing_rows, partial=False)
        print(f"OK candidats fusionnes dans les lettres: {added}")
        return added

    def export_all_data(self, rows: List[Dict[str, str]], base_filename: Optional[str] = None) -> Dict[str, str]:
        exports_dir, _, _, _ = self._ensure_dirs()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = base_filename or f"nautiljon_concat_{timestamp}"
        json_path = os.path.join(exports_dir, f"{base_filename}.json")
        csv_path = os.path.join(exports_dir, f"{base_filename}.csv")
        stats_path = os.path.join(exports_dir, f"{base_filename}_stats.json")
        report_path = os.path.join(exports_dir, f"{base_filename}_rapport.txt")

        self._write_json_atomic(json_path, rows)
        self._write_csv(csv_path, rows)
        self._write_json_atomic(stats_path, self.session_stats)
        tmp_report_path = report_path + ".tmp"
        with open(tmp_report_path, "w", encoding="utf-8") as handle:
            handle.write("RAPPORT DE SCRAPING NAUTILJON\n")
            handle.write(f"Date: {_now_str()}\n")
            handle.write("=" * 50 + "\n\n")
            handle.write(f"Series totales: {len(rows)}\n")
            handle.write(f"Erreurs: {self.session_stats.get('errors', 0)}\n")
            handle.write(f"Ignorées par type: {self.session_stats.get('skipped_by_type', 0)}\n")
            handle.write(f"Duree: {self.session_stats.get('duration', 'N/A')}\n")
        os.replace(tmp_report_path, report_path)
        print(f"OK export final: {csv_path} ({len(rows)} lignes)")
        return {
            "json_path": json_path,
            "csv_path": csv_path,
            "stats_path": stats_path,
            "report_path": report_path,
            "base_filename": base_filename,
        }

    def _letter_tag_for_row(self, row: Dict[str, str]) -> str:
        title = _first_non_empty([row.get("titre", ""), row.get("url_fiche", "")])
        if title == "N/A":
            return "HASH"
        first = _norm(title[:1]).upper()
        if re.fullmatch(r"[A-Z]", first):
            return first
        return "HASH"

    def _read_csv_rows(self, path: str) -> List[Dict[str, str]]:
        errors: List[str] = []
        for encoding in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1"):
            try:
                rows = self._read_csv_rows_with_encoding(path, encoding, errors="strict")
                return rows
            except Exception as exc:
                errors.append(f"{encoding}: {exc}")
                continue

        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                rows = self._read_csv_rows_with_encoding(path, encoding, errors="replace")
                return rows
            except Exception as exc:
                errors.append(f"{encoding}/replace: {exc}")
                continue

        details = " | ".join(errors[-5:])
        raise RuntimeError(f"CSV illisible: {path}" + (f" ({details})" if details else ""))

    def _read_csv_rows_with_encoding(self, path: str, encoding: str, errors: str = "strict") -> List[Dict[str, str]]:
        with open(path, "r", newline="", encoding=encoding, errors=errors) as handle:
            sample = handle.read(8192)
            handle.seek(0)
            if not sample.strip("\ufeff\r\n\t ;,"):
                return []

            delimiter = ";"
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,")
                delimiter = dialect.delimiter or ";"
            except csv.Error:
                if sample.count(",") > sample.count(";") and "url_fiche;" not in sample[:2000]:
                    delimiter = ","

            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames:
                return []

            reader.fieldnames = [
                _clean_spaces((field or "").lstrip("\ufeff"))
                for field in reader.fieldnames
            ]
            if not any(field in PREFERRED_FIELDS for field in reader.fieldnames):
                raise ValueError(f"en-tetes CSV inattendus: {reader.fieldnames[:5]}")
            return [
                {
                    _clean_spaces(str(key).lstrip("\ufeff")): _clean_spaces(value or "N/A")
                    for key, value in row.items()
                    if key
                }
                for row in reader
                if row
            ]

    def import_existing_csv(self) -> List[Dict[str, str]]:
        exports_dir, _, letters_dir, _ = self._ensure_dirs()
        csv_files: List[str] = []
        for directory in (letters_dir, self.out_dir, exports_dir):
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                if name.lower().endswith(".csv") and ".partial." not in name.lower():
                    csv_files.append(os.path.join(directory, name))
        csv_files = sorted(dict.fromkeys(csv_files))
        if not csv_files:
            raise RuntimeError(f"Aucun CSV trouve dans {letters_dir}, {self.out_dir} ou {exports_dir}")

        by_letter: Dict[str, Dict[str, Dict[str, str]]] = {}
        imported = 0
        empty_csv_files = 0
        for path in sorted(csv_files):
            rows = self._read_csv_rows(path)
            if not rows:
                empty_csv_files += 1
                print(f"  CSV vide ignore: {path}")
                continue
            inferred_tag = self._letter_tag_from_filename(path)
            for row in rows:
                row = self._normalize_row(row)
                tag = inferred_tag or self._letter_tag_for_row(row)
                url = _ensure_abs_url(row.get("url_fiche", ""))
                if not url:
                    continue
                row["url_fiche"] = url
                by_letter.setdefault(tag, {})[url] = row
                imported += 1

        all_rows: List[Dict[str, str]] = []
        for tag, rows_by_url in sorted(by_letter.items(), key=lambda item: (0, item[0]) if item[0] == "HASH" else (1, item[0])):
            rows = sorted(rows_by_url.values(), key=lambda row: _norm(row.get("titre", "")))
            self.save_letter_files(tag, rows, partial=False)
            all_rows.extend(rows)

        self.session_stats["total_series"] = len(all_rows)
        export_paths = self.export_all_data(all_rows, base_filename=f"nautiljon_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        self.mark_success("import", len(all_rows), export_paths)
        print(
            f"OK import CSV termine: {imported} lignes lues, "
            f"{len(all_rows)} URLs uniques, {empty_csv_files} CSV vides ignores"
        )
        return all_rows

    def _letter_tag_from_filename(self, path: str) -> Optional[str]:
        name = os.path.basename(path)
        match = re.search(r"nautiljon_lettre_([A-Z]|HASH|#)\.csv$", name, re.IGNORECASE)
        if not match:
            return None
        tag = match.group(1).upper()
        return "HASH" if tag == "#" else tag

    def _normalize_row(self, row: Dict[str, str]) -> Dict[str, str]:
        normalized = {field: _clean_spaces(str(row.get(field, "N/A") or "N/A")) for field in PREFERRED_FIELDS}
        for key, value in row.items():
            if key not in normalized:
                normalized[key] = _clean_spaces(str(value or "N/A"))
        if _is_na(normalized.get("extraction_time")):
            normalized["extraction_time"] = _now_str()
        normalized["url_fiche"] = _ensure_abs_url(normalized.get("url_fiche", ""))
        return normalized

    def setup_browser(self) -> webdriver.Chrome:
        if self.driver:
            return self.driver

        debug_dir = os.path.join(self.out_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        log_path = os.path.join(debug_dir, f"chromedriver_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        profile_dir = os.environ.get(
            "NAUTILJON_BROWSER_PROFILE",
            os.path.join(self.out_dir, "browser-profile"),
        )
        os.makedirs(profile_dir, exist_ok=True)

        browser_binary = os.environ.get("NAUTILJON_CHROME_BINARY", "/usr/bin/chromium")
        attach_browser = _env_bool("NAUTILJON_BROWSER_ATTACH", False)
        options = webdriver.ChromeOptions()

        common_args = [
            "--no-sandbox",
            "--window-size=1280,800",
            "--lang=fr-FR",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-features=Translate,MediaRouter",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-breakpad",
            "--renderer-process-limit=2",
            "--disk-cache-size=67108864",
            "--media-cache-size=16777216",
            "--remote-allow-origins=*",
        ]

        if attach_browser:
            if not os.path.isfile(browser_binary):
                raise RuntimeError(f"Binaire Chromium introuvable: {browser_binary}")
            debugger_address = os.environ.get("NAUTILJON_DEBUGGER_ADDRESS", "127.0.0.1:9222").strip()
            self._start_browser_process(browser_binary, profile_dir, debugger_address, common_args)
            options.debugger_address = debugger_address
        else:
            if os.path.isfile(browser_binary):
                options.binary_location = browser_binary
            if self.browser_headless:
                options.add_argument("--headless=new")
            for argument in common_args:
                options.add_argument(argument)
            options.add_argument(f"--user-data-dir={os.path.abspath(profile_dir)}")
            options.add_experimental_option("prefs", {
                "intl.accept_languages": "fr-FR,fr,en-US,en",
                "profile.default_content_setting_values.notifications": 2,
                "profile.exit_type": "Normal",
                "profile.exited_cleanly": True,
            })

        driver_binary = os.environ.get("NAUTILJON_CHROMEDRIVER", "/usr/bin/chromedriver")
        service = ChromeService(
            executable_path=driver_binary if os.path.isfile(driver_binary) else None,
            log_output=log_path,
        )
        browser_mode = "attache au Chromium autonome" if attach_browser else "lance par ChromeDriver"
        print(f"Navigateur Selenium: chromium ({'headless' if self.browser_headless else 'Xvfb visible'}, {browser_mode})")
        print(f"Profil persistant: {profile_dir}")
        print(f"Log ChromeDriver: {log_path}")
        try:
            self.driver = webdriver.Chrome(service=service, options=options)
            self.driver.set_page_load_timeout(60)
            self.driver.set_window_size(1280, 800)
            self.driver.get("about:blank")
            version = self.driver.capabilities.get("browserVersion", "inconnue")
            print(f"Session Selenium active, Chromium {version}")
            return self.driver
        except WebDriverException as exc:
            self._stop_browser_process()
            raise RuntimeError(f"Impossible de demarrer Chromium/Selenium: {str(exc)[:500]}") from exc

    def _start_browser_process(
        self,
        browser_binary: str,
        profile_dir: str,
        debugger_address: str,
        common_args: List[str],
    ) -> None:
        if self.browser_process and self.browser_process.poll() is None:
            return

        host, separator, port_text = debugger_address.rpartition(":")
        if not separator or not port_text.isdigit():
            raise RuntimeError(f"NAUTILJON_DEBUGGER_ADDRESS invalide: {debugger_address}")
        host = host or "127.0.0.1"
        port = int(port_text)

        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = os.path.join(profile_dir, name)
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError:
                pass

        debug_dir = os.path.join(self.out_dir, "debug")
        browser_log_path = os.path.join(
            debug_dir,
            f"chromium_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        )
        self._browser_log_handle = open(browser_log_path, "a", encoding="utf-8")
        command = [
            browser_binary,
            f"--remote-debugging-address={host}",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={os.path.abspath(profile_dir)}",
            *common_args,
        ]
        if self.browser_headless:
            command.append("--headless=new")
        command.append("about:blank")

        print(f"Demarrage autonome de Chromium: {debugger_address}")
        print(f"Log Chromium: {browser_log_path}")
        self.browser_process = subprocess.Popen(
            command,
            stdout=self._browser_log_handle,
            stderr=subprocess.STDOUT,
        )

        endpoint = f"http://{debugger_address}/json/version"
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.browser_process.poll() is not None:
                self._stop_browser_process()
                raise RuntimeError(f"Chromium autonome s'est arrete avant son attachement. Log: {browser_log_path}")
            try:
                response = requests.get(endpoint, timeout=0.5)
                if response.ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)

        self._stop_browser_process()
        raise RuntimeError(f"Port de debogage Chromium indisponible apres 20 secondes: {endpoint}")

    def _stop_browser_process(self) -> None:
        process = self.browser_process
        self.browser_process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._browser_log_handle:
            self._browser_log_handle.close()
            self._browser_log_handle = None

    def close_browser(self) -> None:
        try:
            if self.driver:
                self.driver.quit()
        finally:
            self.driver = None
            self._browser_letter_urls.clear()
            self._stop_browser_process()
            self.close_flaresolverr()

    def _flaresolverr_api_url(self) -> str:
        url = os.environ.get("NAUTILJON_FLARESOLVERR_URL", "http://flaresolverr:8191/v1").strip().rstrip("/")
        if not url:
            raise RuntimeError("NAUTILJON_FLARESOLVERR_URL est vide")
        return url if url.endswith("/v1") else url + "/v1"

    @staticmethod
    def _flaresolverr_proxy() -> Optional[Dict[str, str]]:
        url = os.environ.get("NAUTILJON_FLARESOLVERR_PROXY_URL", "").strip()
        if not url:
            return None
        proxy = {"url": url}
        username = os.environ.get("NAUTILJON_FLARESOLVERR_PROXY_USERNAME", "").strip()
        password = os.environ.get("NAUTILJON_FLARESOLVERR_PROXY_PASSWORD", "").strip()
        if username:
            proxy["username"] = username
        if password:
            proxy["password"] = password
        return proxy

    def _flaresolverr_post(self, payload: Dict[str, object]) -> Dict[str, object]:
        timeout_ms = max(1000, _env_int("NAUTILJON_FLARESOLVERR_TIMEOUT_MS", 120000))
        try:
            response = requests.post(
                self._flaresolverr_api_url(),
                json=payload,
                timeout=(5, max(30, timeout_ms / 1000 + 15)),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"FlareSolverr inaccessible: {str(exc)[:300]}") from exc
        if not isinstance(data, dict) or data.get("status") != "ok":
            message = data.get("message", "reponse API invalide") if isinstance(data, dict) else "reponse API invalide"
            raise RuntimeError(f"FlareSolverr a refuse la requete: {message}")
        return data

    def setup_flaresolverr(self) -> str:
        if self.flaresolverr_session_id:
            return self.flaresolverr_session_id
        session_id = f"nautiljon-{os.getpid()}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        attempts = max(1, _env_int("NAUTILJON_FLARESOLVERR_STARTUP_ATTEMPTS", 30))
        retry_delay = max(0, _env_float("NAUTILJON_FLARESOLVERR_STARTUP_DELAY", 2.0))
        payload: Dict[str, object] = {"cmd": "sessions.create", "session": session_id}
        proxy = self._flaresolverr_proxy()
        if proxy:
            payload["proxy"] = proxy
        for attempt in range(1, attempts + 1):
            try:
                self._flaresolverr_post(payload)
                break
            except RuntimeError as exc:
                if attempt == attempts:
                    raise RuntimeError(
                        f"FlareSolverr non pret apres {attempts} tentative(s): {exc}"
                    ) from exc
                print(f"FlareSolverr pas encore pret ({attempt}/{attempts}); nouvel essai dans {retry_delay:g}s")
                time.sleep(retry_delay)
        self.flaresolverr_session_id = session_id
        print(f"Session FlareSolverr active: {session_id}")
        return session_id

    def close_flaresolverr(self) -> None:
        session_id = self.flaresolverr_session_id
        self.flaresolverr_session_id = None
        self._flaresolverr_letter_urls.clear()
        self._flaresolverr_listing_urls.clear()
        self._flaresolverr_page_has_next.clear()
        if not session_id:
            return
        try:
            self._flaresolverr_post({"cmd": "sessions.destroy", "session": session_id})
            print(f"Session FlareSolverr fermee: {session_id}")
        except Exception as exc:
            print(f"Avertissement: fermeture FlareSolverr impossible: {str(exc)[:180]}")

    def _fetch_html_flaresolverr(self, url: str) -> str:
        timeout_ms = max(1000, _env_int("NAUTILJON_FLARESOLVERR_TIMEOUT_MS", 120000))
        target_url = _ensure_abs_url(url)
        self._pace_remote_request(target_url, "navigation FlareSolverr")
        payload: Dict[str, object] = {
            "cmd": "request.get",
            "url": target_url,
            "session": self.setup_flaresolverr(),
            "maxTimeout": timeout_ms,
        }
        if (urlsplit(target_url).hostname or "").endswith("nautiljon.com"):
            payload["cookies"] = [{
                "name": "cookieconsent_status",
                "value": "dismiss",
                "domain": ".nautiljon.com",
                "path": "/",
            }]
        data = self._flaresolverr_post(payload)
        solution = data.get("solution")
        if not isinstance(solution, dict):
            raise RuntimeError("FlareSolverr: solution absente")
        status_code = int(solution.get("status", 0) or 0)
        html = solution.get("response", "")
        self._last_flaresolverr_url = str(solution.get("url", url))
        if status_code in {403, 429} and (not isinstance(html, str) or not html):
            raise NautiljonAccessBlockedError(f"FlareSolverr: HTTP {status_code} pour {url}")
        if not isinstance(html, str) or not html:
            raise RuntimeError(f"FlareSolverr: reponse vide pour {url}")
        self._last_flaresolverr_html = html
        if self._nautiljon_access_blocked(html):
            raise NautiljonAccessBlockedError(
                "Nautiljon a interdit l'IP de sortie pour abus; arret immediat sans nouvelle tentative"
            )
        if self._blocked_by_waf(html):
            raise NautiljonAccessBlockedError(
                f"FlareSolverr n'a pas resolu Cloudflare pour {url}"
            )
        if status_code in {403, 429}:
            raise NautiljonAccessBlockedError(f"FlareSolverr: HTTP {status_code} pour {url}")
        if status_code >= 400:
            raise RuntimeError(f"FlareSolverr: HTTP {status_code} pour {url}")
        return html

    def _save_browser_debug(self, context: str) -> Dict[str, str]:
        if not self.driver:
            return {}
        debug_dir = os.path.join(self.out_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        safe_context = re.sub(r"[^a-zA-Z0-9_.-]+", "_", context).strip("_") or "page"
        base = os.path.join(debug_dir, f"selenium_{safe_context}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        paths: Dict[str, str] = {}
        try:
            html_path = base + ".html"
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(self.driver.page_source)
            paths["html"] = html_path
        except Exception:
            pass
        try:
            png_path = base + ".png"
            self.driver.save_screenshot(png_path)
            paths["screenshot"] = png_path
        except Exception:
            pass
        return paths

    def _save_flaresolverr_debug(self, context: str, html: str, requested_url: str) -> Dict[str, str]:
        debug_dir = os.path.join(self.out_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        safe_context = re.sub(r"[^a-zA-Z0-9_.-]+", "_", context).strip("_") or "page"
        base = os.path.join(
            debug_dir,
            f"flaresolverr_{safe_context}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )
        paths: Dict[str, str] = {}
        try:
            html_path = base + ".html"
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(html)
            paths["html"] = html_path
        except OSError:
            pass
        try:
            soup = BeautifulSoup(html, "html.parser")
            metadata_path = base + ".json"
            metadata = {
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "requested_url": requested_url,
                "final_url": self._last_flaresolverr_url or requested_url,
                "title": _clean_spaces(soup.title.get_text(" ", strip=True)) if soup.title else "",
                "html_bytes": len(html.encode("utf-8", errors="replace")),
            }
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2)
            paths["metadata"] = metadata_path
        except OSError:
            pass
        self._last_flaresolverr_debug = paths
        return paths

    def _dismiss_cookie_consent(self) -> bool:
        if not self.driver:
            return False
        css_selectors = [
            "#didomi-notice-agree-button",
            "#onetrust-accept-btn-handler",
            "button[mode='primary']",
            "button[aria-label*='Accepter']",
            "button[aria-label*='accepter']",
        ]
        xpaths = [
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'tout accepter')]",
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accepter')]",
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"j'accepte\")]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accepter')]",
        ]

        contexts = [None]
        try:
            contexts.extend(self.driver.find_elements(By.CSS_SELECTOR, "iframe"))
        except Exception:
            pass
        for frame in contexts:
            try:
                self.driver.switch_to.default_content()
                if frame is not None:
                    self.driver.switch_to.frame(frame)
                elements = []
                for selector in css_selectors:
                    elements.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))
                for xpath in xpaths:
                    elements.extend(self.driver.find_elements(By.XPATH, xpath))
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        self.driver.execute_script("arguments[0].click();", element)
                        self.driver.switch_to.default_content()
                        print("Consentement cookies accepte automatiquement.")
                        time.sleep(0.5)
                        return True
            except Exception:
                continue
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        return False

    def _wait_browser_page(self, context: str) -> None:
        if not self.driver:
            raise RuntimeError("Driver Selenium non initialise")
        try:
            WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except TimeoutException as exc:
            debug = self._save_browser_debug(context)
            raise RuntimeError(f"Page Selenium vide ou trop lente ({context}). Debug: {debug}") from exc

        wait_seconds = _env_int("NAUTILJON_CLOUDFLARE_WAIT_SECONDS", 120)
        challenge_deadline = time.time() + wait_seconds
        announced = False
        while True:
            html = self.driver.page_source
            if self._cloudflare_challenge(html):
                if not announced:
                    print(f"Verification Cloudflare detectee; attente automatique jusqu'a {wait_seconds}s.")
                    announced = True
                if time.time() >= challenge_deadline:
                    debug = self._save_browser_debug(context)
                    raise NautiljonAccessBlockedError(
                        f"La verification Cloudflare ne s'est pas terminee apres {wait_seconds}s "
                        f"({context}). Debug: {debug}"
                    )
                time.sleep(1)
                continue
            if self._blocked_by_waf(html):
                debug = self._save_browser_debug(context)
                raise NautiljonAccessBlockedError(
                    f"Cloudflare bloque le navigateur Selenium ({context}). Debug: {debug}"
                )
            break
        if announced:
            print("Verification Cloudflare terminee.")
        self._dismiss_cookie_consent()

    def _browser_get(self, url: str, context: str) -> str:
        driver = self.setup_browser()
        target_url = _ensure_abs_url(url)
        self._pace_remote_request(target_url, f"navigation Selenium {context}")
        try:
            driver.get(target_url)
        except WebDriverException as exc:
            debug = self._save_browser_debug(context)
            raise RuntimeError(f"Navigation Selenium impossible ({context}): {str(exc)[:300]}. Debug: {debug}") from exc
        self._wait_browser_page(context)
        return driver.page_source

    def _find_visible_search_input(self):
        if not self.driver:
            return None
        selectors = [
            "input[name='q']",
            "input[type='search']",
            "#content input[type='text']",
            "form input[type='text']",
        ]
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        return None

    def _submit_manga_search_form(self, query: str) -> bool:
        search_input = self._find_visible_search_input()
        if not self.driver or search_input is None:
            return False
        try:
            search_input.click()
            search_input.send_keys(Keys.CONTROL, "a")
            search_input.send_keys(query)
            form = search_input.find_element(By.XPATH, "./ancestor::form[1]")
            buttons = form.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], button")
            self._pace_remote_request(self.driver.current_url, "soumission du formulaire mangas")
            if buttons:
                self.driver.execute_script("arguments[0].click();", buttons[0])
            else:
                search_input.send_keys(Keys.ENTER)
            self._sleep_delay()
            self._wait_browser_page("mangas_form_submit")
            return True
        except Exception as exc:
            print(f"Soumission du formulaire Nautiljon impossible: {str(exc)[:160]}")
            return False

    def _click_manga_letter_link(self, letter: str) -> bool:
        if not self.driver:
            return False
        target = self._letter_label(letter)
        xpaths = [
            f"//a[normalize-space(.)='{target}' and contains(@href, '/mangas')]",
            f"//*[@id='content']//a[normalize-space(.)='{target}']",
            f"//a[normalize-space(.)='{target}']",
        ]
        for xpath in xpaths:
            for link in self.driver.find_elements(By.XPATH, xpath):
                if not link.is_displayed() or not link.is_enabled():
                    continue
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                    self._pace_remote_request(self.driver.current_url, f"clic lettre {target}")
                    self.driver.execute_script("arguments[0].click();", link)
                    self._sleep_delay()
                    self._wait_browser_page("mangas_letter_click")
                    return True
                except Exception:
                    continue
        return False

    def open_manga_letter_index(self, letter: str) -> str:
        driver = self.setup_browser()
        label = self._letter_label(letter)
        query = "#" if letter == "%23" else letter.lower()
        print(f"Initialisation Selenium de la lettre {label} via l'interface Nautiljon.")
        self._browser_get(f"{BASE_URL}/mangas/", "mangas_root")
        if self._click_manga_letter_link(letter) and not self._search_session_expired(driver.page_source):
            return driver.current_url

        self._browser_get(f"{BASE_URL}/mangas/", "mangas_root_form")
        if not self._submit_manga_search_form(query):
            raise RuntimeError("Impossible d'initialiser la recherche Nautiljon via son interface")
        if self._search_session_expired(driver.page_source):
            raise RuntimeError("La session de recherche Nautiljon expire immediatement")
        return driver.current_url

    def _fetch_listing_page_selenium(self, letter: str, page_num: int) -> Tuple[str, List[Dict[str, str]]]:
        driver = self.setup_browser()
        tag = self._letter_tag(letter)
        if page_num == 0 or tag not in self._browser_letter_urls:
            self._browser_letter_urls[tag] = self.open_manga_letter_index(letter)
        page_url = self._url_with_dbt(self._browser_letter_urls[tag], page_num)
        if page_num > 0 or driver.current_url != page_url:
            html = self._browser_get(page_url, f"listing_{tag}_{page_num + 1}")
        else:
            html = driver.page_source
        if self._search_session_expired(html):
            self._browser_letter_urls[tag] = self.open_manga_letter_index(letter)
            page_url = self._url_with_dbt(self._browser_letter_urls[tag], page_num)
            html = self._browser_get(page_url, f"listing_{tag}_{page_num + 1}_recovery")
        rows = self.extract_series_list_from_html(html)
        expected_tag = self._letter_tag(letter)
        matching_rows = [row for row in rows if self._letter_tag_for_row(row) == expected_tag]
        if rows and len(matching_rows) < max(1, int(len(rows) * 0.8)):
            raise RuntimeError(
                f"Listing Selenium incoherent pour {self._letter_label(letter)}: "
                f"{len(matching_rows)}/{len(rows)} titres correspondent"
            )
        return driver.current_url, matching_rows

    @staticmethod
    def _manga_search_form_template(
        soup: BeautifulSoup,
    ) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
        """Return the GET form action and the controls a browser would submit."""
        for form in soup.find_all("form"):
            method = str(form.get("method", "get")).lower()
            action = _ensure_abs_url(str(form.get("action", "")))
            parsed_action = urlsplit(action)
            if method != "get" or parsed_action.path.rstrip("/") != "/mangas":
                continue
            if not form.find(["input", "textarea"], attrs={"name": "q"}):
                continue

            params: List[Tuple[str, str]] = []
            for field in form.find_all(["input", "select", "textarea"]):
                name = str(field.get("name", ""))
                if not name or field.has_attr("disabled"):
                    continue

                if field.name == "input":
                    input_type = str(field.get("type", "text")).lower()
                    if input_type in {"button", "file", "image", "reset", "submit"}:
                        continue
                    if input_type in {"checkbox", "radio"} and not field.has_attr("checked"):
                        continue
                    params.append((name, str(field.get("value", ""))))
                    continue

                if field.name == "textarea":
                    params.append((name, field.get_text()))
                    continue

                options = [option for option in field.find_all("option") if not option.has_attr("disabled")]
                selected = [option for option in options if option.has_attr("selected")]
                if field.has_attr("multiple"):
                    chosen = selected
                else:
                    chosen = selected or options[:1]
                for option in chosen:
                    params.append((name, str(option.get("value", option.get_text(" ", strip=True)))))
            return action, params
        return None

    @staticmethod
    def _manga_search_url(
        template: Tuple[str, List[Tuple[str, str]]],
        query: str,
    ) -> str:
        action, form_params = template
        params: List[Tuple[str, str]] = []
        query_replaced = False
        for name, value in form_params:
            if name == "q":
                params.append((name, query))
                query_replaced = True
            else:
                params.append((name, value))
        if not query_replaced:
            params.insert(0, ("q", query))

        parsed = urlsplit(action)
        action_params = parse_qsl(parsed.query, keep_blank_values=True)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(action_params + params, doseq=True),
                parsed.fragment,
            )
        )

    def _load_flaresolverr_letter_urls(self) -> None:
        if self._flaresolverr_letter_urls:
            return
        html = self._fetch_html_flaresolverr(f"{BASE_URL}/mangas/")
        soup = BeautifulSoup(html, "html.parser")
        search_template = self._manga_search_form_template(soup)
        for anchor in soup.find_all("a", href=True):
            label = _clean_spaces(anchor.get_text(" ", strip=True)).upper()
            if label == "#" or re.fullmatch(r"[A-Z]", label):
                href = str(anchor.get("href", ""))
                parsed = urlsplit(urljoin(BASE_URL, href))
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                expected_query = "#" if label == "#" else label.lower()
                if "/mangas/" in parsed.path and query.get("q", "").lower() == expected_query:
                    self._flaresolverr_letter_urls[label] = _ensure_abs_url(href)
        missing = [self._letter_label(letter) for letter in self.get_all_letters() if self._letter_label(letter) not in self._flaresolverr_letter_urls]
        if missing:
            debug = self._save_flaresolverr_debug(
                "mangas_index_missing",
                html,
                f"{BASE_URL}/mangas/",
            )
            raise NautiljonAccessBlockedError(
                "FlareSolverr: index Nautiljon non exploitable; liens alphabetiques "
                f"introuvables: {', '.join(missing)}. Debug: {debug}"
            )
        if search_template:
            for label in self._flaresolverr_letter_urls:
                search_query = "#" if label == "#" else label.lower()
                self._flaresolverr_letter_urls[label] = self._manga_search_url(
                    search_template,
                    search_query,
                )
        mode = "formulaire signe" if search_template else "liens alphabetiques"
        print(
            f"Index Nautiljon initialise via FlareSolverr: "
            f"{len(self._flaresolverr_letter_urls)} lettres ({mode})"
        )

    @staticmethod
    def _extract_next_listing_url(html: str, current_url: str, page_num: int) -> Optional[str]:
        target_offset = (page_num + 1) * 50
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = urljoin(current_url, str(anchor.get("href", "")))
            parsed = urlsplit(href)
            if "/mangas/" not in parsed.path:
                continue
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            try:
                offset = int(query.get("dbt", "-1"))
            except ValueError:
                continue
            if offset == target_offset:
                return href
        return None

    def _set_flaresolverr_listing_url(self, letter: str, page_num: int, url: str) -> None:
        if url:
            self._flaresolverr_listing_urls[(self._letter_tag(letter), page_num)] = _ensure_abs_url(url)

    def _flaresolverr_listing_has_next(self, letter: str, page_num: int) -> bool:
        return self._flaresolverr_page_has_next.get((self._letter_tag(letter), page_num), False)

    def _fetch_listing_page_flaresolverr(self, letter: str, page_num: int) -> Tuple[str, List[Dict[str, str]]]:
        label = self._letter_label(letter)
        key = (self._letter_tag(letter), page_num)
        page_url = self._flaresolverr_listing_urls.get(key)
        if not page_url:
            self._load_flaresolverr_letter_urls()
            page_url = self._url_with_dbt(self._flaresolverr_letter_urls[label], page_num)
        html = self._fetch_html_flaresolverr(page_url)
        if self._search_session_expired(html):
            self._flaresolverr_letter_urls.clear()
            self._load_flaresolverr_letter_urls()
            page_url = self._url_with_dbt(self._flaresolverr_letter_urls[label], page_num)
            html = self._fetch_html_flaresolverr(page_url)
        final_url = self._last_flaresolverr_url or page_url
        rows = self.extract_series_list_from_html(html)
        expected_tag = self._letter_tag(letter)
        matching_rows = [row for row in rows if self._letter_tag_for_row(row) == expected_tag]
        if rows and len(matching_rows) < max(1, int(len(rows) * 0.8)):
            raise RuntimeError(
                f"Listing FlareSolverr incoherent pour {label}: "
                f"{len(matching_rows)}/{len(rows)} titres correspondent"
            )
        next_url = self._extract_next_listing_url(html, final_url, page_num)
        self._flaresolverr_page_has_next[key] = bool(next_url)
        if next_url:
            self._set_flaresolverr_listing_url(letter, page_num + 1, next_url)
        return final_url, matching_rows

    def browser_test(self, letter: str = "a") -> Dict[str, object]:
        print("=" * 60)
        print("TEST SELENIUM NAUTILJON - AUCUN EXPORT MODIFIE")
        print("=" * 60)
        report: Dict[str, object] = {"letter": self._letter_label(letter), "listing_ok": False, "detail_ok": False}
        try:
            _, rows = self._fetch_listing_page_selenium(letter, 0)
            report["listing_rows"] = len(rows)
            report["listing_ok"] = len(rows) > 0
            if rows:
                html = self._browser_get(rows[0]["url_fiche"], "browser_test_detail")
                detail = self.extract_series_detail_from_html(html)
                useful = [value for key, value in detail.items() if key != "_titre_fr_fallback" and not _is_na(value)]
                report["detail_fields"] = len(useful)
                report["detail_ok"] = len(useful) > 0
                report["sample_title"] = rows[0].get("titre", "N/A")
        except Exception as exc:
            report["error"] = str(exc)
            if self.driver:
                report["current_url"] = self.driver.current_url
                report["page_title"] = self.driver.title
            report["debug"] = self._save_browser_debug("browser_test_failed")
        finally:
            self.close_browser()
        report["ready_for_diff"] = bool(report["listing_ok"] and report["detail_ok"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("VERDICT SELENIUM: " + ("PRET POUR DIFF CONTROLE" if report["ready_for_diff"] else "BLOQUE"))
        return report

    def browser_smoke(self) -> bool:
        print("TEST DEMARRAGE CHROMIUM / SELENIUM")
        try:
            driver = self.setup_browser()
            print(f"URL navigateur: {driver.current_url}")
            print("VERDICT NAVIGATEUR: OK")
            return True
        except Exception as exc:
            print(f"VERDICT NAVIGATEUR: ECHEC ({str(exc)[:500]})")
            return False
        finally:
            self.close_browser()

    def _flaresolverr_public_ips(self) -> Tuple[str, str]:
        direct_response = requests.get("http://api.ipify.org?format=json", timeout=15)
        direct_response.raise_for_status()
        direct_ip = str(ipaddress.ip_address(direct_response.json().get("ip", "")))
        flaresolverr_ip_html = self._fetch_html_flaresolverr("http://api.ipify.org?format=json")
        ip_match = re.search(r'"ip"\s*:\s*"([^"]+)"', flaresolverr_ip_html)
        if not ip_match:
            raise RuntimeError("IP de sortie FlareSolverr introuvable dans la reponse ipify")
        result = (direct_ip, str(ipaddress.ip_address(ip_match.group(1))))
        self._verified_public_ips = result
        return result

    def flaresolverr_test(self, letter: str = "a") -> Dict[str, object]:
        print("=" * 60)
        print("TEST FLARESOLVERR NAUTILJON - AUCUN EXPORT MODIFIE")
        print("=" * 60)
        report: Dict[str, object] = {
            "letter": self._letter_label(letter),
            "api_ok": False,
            "same_public_ip": False,
            "listing_ok": False,
            "detail_ok": False,
        }
        try:
            direct_ip, flaresolverr_ip = self._flaresolverr_public_ips()
            report["api_ok"] = True
            report["gluetun_public_ip"] = direct_ip
            report["flaresolverr_public_ip"] = flaresolverr_ip
            report["same_public_ip"] = direct_ip == flaresolverr_ip

            listing_url, rows = self.fetch_listing_page(letter, 0)
            report["listing_rows"] = len(rows)
            report["listing_ok"] = len(rows) > 0
            report["listing_final_url"] = listing_url
            if not rows and self._last_flaresolverr_html:
                report["search_session_expired"] = self._search_session_expired(
                    self._last_flaresolverr_html
                )
                report["debug"] = self._save_flaresolverr_debug(
                    f"listing_{self._letter_tag(letter).lower()}_empty",
                    self._last_flaresolverr_html,
                    listing_url,
                )
            if rows:
                html = self.fetch_html(rows[0]["url_fiche"])
                detail = self.extract_series_detail_from_html(html)
                useful = [value for key, value in detail.items() if key != "_titre_fr_fallback" and not _is_na(value)]
                report["detail_fields"] = len(useful)
                report["detail_ok"] = len(useful) > 0
                report["sample_title"] = rows[0].get("titre", "N/A")
        except Exception as exc:
            report["error"] = str(exc)
            if self._last_flaresolverr_debug:
                report["debug"] = dict(self._last_flaresolverr_debug)
        finally:
            self.close_flaresolverr()
        report["ready_for_diff"] = bool(
            report["api_ok"]
            and report["same_public_ip"]
            and report["listing_ok"]
            and report["detail_ok"]
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("VERDICT FLARESOLVERR: " + ("PRET POUR DIFF CONTROLE" if report["ready_for_diff"] else "BLOQUE"))
        return report

    def fetch_html(self, url: str) -> str:
        if self.backend == "selenium":
            return self._browser_get(url, "fiche_detail")
        if self.backend == "flaresolverr":
            return self._fetch_html_flaresolverr(url)
        target_url = _ensure_abs_url(url)
        self._pace_remote_request(target_url, "requete HTTP")
        response = self.session.get(target_url, timeout=DEFAULT_TIMEOUT)
        response.encoding = response.encoding or "utf-8"
        html = response.text
        if self._nautiljon_access_blocked(html):
            raise NautiljonAccessBlockedError(
                "Nautiljon a interdit l'IP de sortie pour abus; arret immediat sans nouvelle tentative"
            )
        if self._blocked_by_waf(html):
            raise NautiljonAccessBlockedError("Nautiljon a renvoye une page de blocage/WAF.")
        if response.status_code in {403, 429} or response.headers.get("cf-mitigated") == "challenge":
            raise NautiljonAccessBlockedError(
                f"Nautiljon refuse la requete HTTP {response.status_code}; arret sans nouvelle tentative."
            )
        response.raise_for_status()
        return html

    def _nautiljon_access_blocked(self, html: str) -> bool:
        text = _norm(html)
        return (
            "interdite pour abus" in text
            and (
                "recuperation de donnees" in text
                or "pour debloquer votre acces" in text
                or "utilisation d'un vpn" in text
            )
        )

    def _blocked_by_waf(self, html: str) -> bool:
        text = _norm(html)
        return (
            self._nautiljon_access_blocked(html)
            or "sorry you have been blocked" in text
            or "you are unable to access nautiljon.com" in text
            or "unable to access nautiljon.com" in text
            or "just a moment" in text
            or "performing security verification" in text
            or "access denied" in text
            or self._cloudflare_challenge(html)
        )

    def _cloudflare_challenge(self, html: str) -> bool:
        text = _norm(html)
        raw = (html or "").lower()
        return (
            "verification de securite en cours" in text
            or "verification en cours" in text
            or "checking your browser" in text
            or "just a moment" in text
            or "performing security verification" in text
            or "cf-chl-" in raw
            or "challenges.cloudflare.com" in raw
        )

    def _search_session_expired(self, html: str) -> bool:
        return "session de recherche a expire" in _norm(html)

    def _pick_results_table(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        content = soup.select_one("#content") or soup
        tables = content.select("table")
        best = None
        best_score = 0
        for table in tables:
            links = table.select("a[href*='/mangas/']")
            score = len([anchor for anchor in links if anchor.get("href", "").endswith(".html")])
            classes = " ".join(table.get("class", [])).lower()
            if "liste" in classes:
                score += 5
            if "search" in classes:
                score += 2
            if score > best_score:
                best = table
                best_score = score
        return best if best_score > 0 else None

    def _extract_headers(self, table: BeautifulSoup) -> List[str]:
        headers: List[str] = []
        for th in table.select("thead th"):
            label = th.get_text(" ", strip=True) or th.get("title") or ""
            if not label:
                img = th.find("img")
                label = img.get("alt", "") if img else ""
            headers.append(_norm(label))
        return headers

    def _map_list_fields_from_row(self, headers: List[str], values: List[str]) -> Dict[str, str]:
        if not headers or len(headers) != len(values):
            return {}

        def pick(keys: Sequence[str]) -> str:
            for idx, header in enumerate(headers):
                if any(key in header for key in keys):
                    return values[idx] or "N/A"
            return "N/A"

        mapped = {
            "type_liste": pick(["type"]),
            "nb_vol_vo_liste": pick(["vol vo", "tomes vo", "volumes vo", "vo"]),
            "nb_vol_vf_liste": pick(["vol vf", "tomes vf", "volumes vf", "vf"]),
            "age_liste": pick(["age", "âge"]),
            "date_vf_liste": pick(["date vf", "parution vf", "sortie vf"]),
            "date_vo_liste": pick(["date vo", "parution vo", "sortie vo"]),
            "note_liste": pick(["note", "rating"]),
        }
        return {} if all(_is_na(value) for value in mapped.values()) else mapped

    def extract_series_list_from_html(self, html: str) -> List[Dict[str, str]]:
        if self._search_session_expired(html):
            return []
        soup = BeautifulSoup(html, "html.parser")
        table = self._pick_results_table(soup)
        if not table:
            return []

        headers = self._extract_headers(table)
        rows = table.select("tbody tr") or table.select("tr")
        series_list: List[Dict[str, str]] = []
        seen_urls = set()
        for row in rows:
            anchor = row.select_one("a.eTitre") or row.find("a", href=re.compile(r"/mangas/.*\.html"))
            if not anchor:
                continue
            url = _ensure_abs_url(anchor.get("href", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = _clean_spaces(anchor.get_text(" ", strip=True)) or "N/A"
            alt_title = "N/A"
            alt = row.select_one("span.infos_small")
            if alt:
                alt_title = _clean_spaces(alt.get_text(" ", strip=True)).strip("()") or "N/A"

            tds = row.find_all("td")
            tds_text = [_clean_spaces(td.get_text(" ", strip=True)) or "N/A" for td in tds]
            mapped = self._map_list_fields_from_row(headers, tds_text)
            if not mapped and len(tds_text) >= 9:
                mapped = {
                    "type_liste": tds_text[2],
                    "nb_vol_vo_liste": tds_text[3],
                    "nb_vol_vf_liste": tds_text[4],
                    "age_liste": tds_text[5],
                    "date_vf_liste": tds_text[6],
                    "date_vo_liste": tds_text[7],
                    "note_liste": tds_text[8],
                }

            row_obj = ListRow(
                titre=title,
                titre_alternatif=alt_title,
                url_fiche=url,
                extraction_time=_now_str(),
                **{key: value for key, value in mapped.items() if key in ListRow.__dataclass_fields__},
            )
            series_list.append(row_obj.__dict__)
        return series_list

    def _listing_candidate_urls(self, letter: str, page_num: int) -> List[str]:
        query = "#" if letter == "%23" else letter.lower()
        offset = page_num * 50
        encoded = quote_plus(query)
        candidates = [
            f"{BASE_URL}/mangas/{encoded}.html",
            f"{BASE_URL}/mangas/?q={encoded}",
            f"{BASE_URL}/mangas/?search={encoded}",
            f"{BASE_URL}/mangas/?lettre={encoded}",
        ]
        if offset > 0:
            candidates = [self._url_with_dbt(url, page_num) for url in candidates]
        return candidates

    def _url_with_dbt(self, url: str, page_num: int) -> str:
        if page_num <= 0:
            return url
        offset = page_num * 50
        parts = urlsplit(url)
        items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "dbt"]
        items.append(("dbt", str(offset)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(items), parts.fragment))

    def fetch_listing_page(self, letter: str, page_num: int) -> Tuple[str, List[Dict[str, str]]]:
        if self.backend == "selenium":
            return self._fetch_listing_page_selenium(letter, page_num)
        if self.backend == "flaresolverr":
            return self._fetch_listing_page_flaresolverr(letter, page_num)
        last_error = ""
        accessible_empty_url = ""
        for url in self._listing_candidate_urls(letter, page_num):
            try:
                html = self.fetch_html(url)
                rows = self.extract_series_list_from_html(html)
                if rows:
                    expected_tag = self._letter_tag(letter)
                    matching_rows = [row for row in rows if self._letter_tag_for_row(row) == expected_tag]
                    if len(matching_rows) >= max(1, int(len(rows) * 0.8)):
                        return url, matching_rows
                    last_error = (
                        f"resultats incoherents pour {self._letter_label(letter)} "
                        f"({len(matching_rows)}/{len(rows)} correspondent)"
                    )
                    continue
                if not self._search_session_expired(html):
                    accessible_empty_url = accessible_empty_url or url
                last_error = "page accessible sans entree"
            except Exception as exc:
                last_error = str(exc)[:160]
                continue
        if accessible_empty_url:
            return accessible_empty_url, []
        raise RuntimeError(f"Listing introuvable pour {letter} page {page_num}: {last_error}")

    def _diagnose_endpoint(
        self,
        label: str,
        url: str,
        kind: str = "generic",
        expected_letter: Optional[str] = None,
        timeout: int = DEFAULT_DIAGNOSE_TIMEOUT,
    ) -> Dict[str, object]:
        result: Dict[str, object] = {
            "label": label,
            "url": url,
            "ok": False,
            "status_code": None,
            "waf_blocked": False,
        }
        try:
            self._pace_remote_request(url, f"diagnostic {label}")
            response = requests.get(
                url,
                headers=dict(self.session.headers),
                cookies=self.session.cookies.get_dict(),
                timeout=timeout,
            )
            html = response.text
            result.update({
                "status_code": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(response.content),
                "waf_blocked": self._blocked_by_waf(html),
            })
            if kind == "listing":
                rows = self.extract_series_list_from_html(html) if response.ok and not result["waf_blocked"] else []
                matching_rows = rows
                if expected_letter:
                    expected_tag = self._letter_tag(expected_letter)
                    matching_rows = [row for row in rows if self._letter_tag_for_row(row) == expected_tag]
                result["rows"] = len(rows)
                result["matching_rows"] = len(matching_rows)
                result["ok"] = (
                    response.ok
                    and not result["waf_blocked"]
                    and len(rows) > 0
                    and len(matching_rows) >= max(1, int(len(rows) * 0.8))
                )
            elif kind == "ip":
                public_ip = ""
                if response.ok:
                    try:
                        public_ip = str(ipaddress.ip_address(response.json().get("ip", "")))
                    except (TypeError, ValueError):
                        public_ip = ""
                result["public_ip"] = public_ip
                result["ok"] = response.ok and bool(public_ip)
            elif kind == "detail":
                detail = self.extract_series_detail_from_html(html) if response.ok and not result["waf_blocked"] else {}
                useful = [value for key, value in detail.items() if key != "_titre_fr_fallback" and not _is_na(value)]
                result["parsed_fields"] = len(useful)
                result["ok"] = response.ok and not result["waf_blocked"] and len(useful) > 0
            elif kind == "rss":
                import xml.etree.ElementTree as ET

                items = []
                if response.ok:
                    items = ET.fromstring(html).findall("./channel/item")
                result["items"] = len(items)
                result["ok"] = response.ok and len(items) > 0
            else:
                result["ok"] = response.ok and not result["waf_blocked"]
        except Exception as exc:
            result["error"] = str(exc)[:240]
        return result

    def diagnose(
        self,
        letter: str = "a",
        detail_url: str = f"{BASE_URL}/mangas/one+piece.html",
        rss_feed_urls: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        print("=" * 60)
        print("DIAGNOSTIC NAUTILJON - AUCUNE ECRITURE")
        print("=" * 60)
        checks: List[Tuple[str, str, str, Optional[str]]] = [
            ("ip_sortie", "https://api.ipify.org?format=json", "ip", None),
            ("robots", f"{BASE_URL}/robots.txt", "generic", None),
            ("sitemap", f"{BASE_URL}/sitemap.xml", "generic", None),
        ]
        for index, url in enumerate(self._listing_candidate_urls(letter, 0), start=1):
            checks.append((f"listing_{index}", url, "listing", letter))
        checks.append(("fiche_connue", detail_url, "detail", None))
        for index, feed_url in enumerate(rss_feed_urls or DEFAULT_RSS_FEEDS, start=1):
            checks.append((f"rss_{index}", feed_url, "rss", None))

        endpoints = [
            self._diagnose_endpoint(label, url, kind, expected_letter)
            for label, url, kind, expected_letter in checks
        ]

        egress_ok = any(item["label"] == "ip_sortie" and item["ok"] for item in endpoints)
        listing_ok = any(str(item["label"]).startswith("listing_") and item["ok"] for item in endpoints)
        detail_ok = any(item["label"] == "fiche_connue" and item["ok"] for item in endpoints)
        rss_ok = any(str(item["label"]).startswith("rss_") and item["ok"] for item in endpoints)
        ip_result = next((item for item in endpoints if item["label"] == "ip_sortie"), {})
        public_ip = str(ip_result.get("public_ip", "")) or "INCONNUE"
        ready_for_diff = egress_ok and listing_ok and detail_ok
        report = {
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "public_ip": public_ip,
            "egress_ok": egress_ok,
            "listing_ok": listing_ok,
            "detail_ok": detail_ok,
            "rss_ok": rss_ok,
            "ready_for_diff": ready_for_diff,
            "endpoints": endpoints,
        }

        for item in endpoints:
            status = "OK" if item["ok"] else "ECHEC"
            suffix = " WAF/CLOUDFLARE" if item.get("waf_blocked") else ""
            ip_suffix = f" IP={item['public_ip']}" if item.get("public_ip") else ""
            print(
                f"{status:6} {str(item['label']):14} HTTP={item.get('status_code')}"
                f" lignes={item.get('rows', '-')} champs={item.get('parsed_fields', '-')}{ip_suffix}{suffix}"
            )
        print("-" * 60)
        if ready_for_diff:
            print("VERDICT: PRET POUR UN DIFF CONTROLE")
        else:
            print("VERDICT: BLOQUE - NE PAS LANCER LE DIFF")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("=" * 60)
        print(f"IP SORTIE VPN: {public_ip}")
        print(f"LISTINGS: {'OK' if listing_ok else 'BLOQUES'} | FICHE: {'OK' if detail_ok else 'BLOQUEE'}")
        if ready_for_diff:
            print("VERDICT FINAL: PRET POUR UN DIFF CONTROLE")
        else:
            print("VERDICT FINAL: BLOQUE - NE PAS LANCER LE DIFF")
        return report

    def get_all_letters(self) -> List[str]:
        return [chr(i) for i in range(ord("a"), ord("z") + 1)] + ["%23"]

    def _letter_label(self, letter: str) -> str:
        return "#" if letter == "%23" else letter.upper()

    def _letter_tag(self, letter: str) -> str:
        return "HASH" if letter == "%23" else letter.upper()

    def is_banned_type(self, value: str) -> bool:
        text = _norm(value)
        return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in BANNED_TYPE_KEYWORDS)

    def _find_best_info_ul(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        content = soup.select_one("#content") or soup
        candidates = content.select("ul.mb10") or content.select("ul")
        known = {
            "titre original", "origine", "type", "genres", "themes", "thèmes",
            "scenariste", "scénariste", "dessinateur", "editeur vo", "éditeur vo",
            "prepublie dans", "prépublié dans", "nb chapitres vo", "nb chapitres vf",
            "nb volumes vo", "nb volumes vf", "age conseille", "âge conseillé",
            "pour public averti", "se trouve dans le commerce en france", "note",
        }
        best = None
        best_score = 0
        for ul in candidates:
            score = 0
            for li in ul.find_all("li"):
                text = _norm(li.get_text(" ", strip=True))
                if ":" in text and text.split(":", 1)[0].strip() in known:
                    score += 1
            if score > best_score:
                best = ul
                best_score = score
        return best

    def _extract_title_fr(self, soup: BeautifulSoup) -> str:
        h1 = soup.select_one("#content h1") or soup.find("h1")
        if h1:
            return _clean_spaces(h1.get_text(" ", strip=True)) or "N/A"
        og = soup.find("meta", attrs={"property": "og:title"})
        return _clean_spaces(og.get("content", "")) if og else "N/A"

    def _extract_kv_from_info_ul(self, ul: BeautifulSoup) -> Dict[str, str]:
        kv: Dict[str, str] = {}
        for li in ul.find_all("li", recursive=False):
            raw = _clean_spaces(li.get_text(" ", strip=True))
            if ":" not in raw:
                continue
            label, value_raw = raw.split(":", 1)
            label_n = _norm(label)
            links = [_clean_spaces(anchor.get_text(" ", strip=True)) for anchor in li.find_all("a")]
            links = [link for link in links if link]
            if links and label_n in {"genres", "themes", "thèmes", "prépublié dans", "prepublie dans"}:
                value = " - ".join(dict.fromkeys(links))
            else:
                value = _clean_spaces(value_raw)
            kv[label_n] = value or "N/A"
        return kv

    def _extract_vf_releases(self, soup: BeautifulSoup) -> Dict[str, str]:
        releases = {field: "N/A" for field in VF_RELEASE_FIELDS}
        container = soup.select_one("li.nav_vols")
        if not container:
            return releases

        for block in container.find_all("div", recursive=False):
            heading = block.find("strong")
            heading_norm = _norm(heading.get_text(" ", strip=True)) if heading else ""
            if "dernier paru" in heading_norm:
                prefix = "dernier_tome_vf"
            elif "a paraitre" in heading_norm:
                prefix = "prochain_tome_vf"
            else:
                continue

            anchor = block.find("a", href=True)
            image = block.find("img")
            volume_sources = [
                anchor.get("title", "") if anchor else "",
                image.get("alt", "") if image else "",
            ]
            for source in volume_sources:
                match = re.search(r"\bvol\.?\s*([0-9]+(?:[.,][0-9]+)?)\b", source, flags=re.IGNORECASE)
                if match:
                    releases[f"{prefix}_numero"] = match.group(1).replace(",", ".")
                    break

            date_node = block.select_one("span.infos_small")
            if date_node:
                releases[f"{prefix}_date"] = _clean_spaces(date_node.get_text(" ", strip=True)) or "N/A"
            if anchor:
                releases[f"{prefix}_url"] = _ensure_abs_url(anchor.get("href", "")) or "N/A"
            if image:
                image_url = image.get("src") or image.get("data-src") or ""
                releases[f"{prefix}_couverture"] = _ensure_abs_url(image_url) or "N/A"
        return releases

    def extract_series_detail_from_html(self, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        list_fields = set(ListRow.__dataclass_fields__)
        detail = {field: "N/A" for field in PREFERRED_FIELDS if field not in list_fields}
        detail["_titre_fr_fallback"] = self._extract_title_fr(soup)
        detail.update(self._extract_vf_releases(soup))
        detail["parutions_vf_verifiees_le"] = _now_str()
        ul = self._find_best_info_ul(soup)
        if not ul:
            return detail
        kv = self._extract_kv_from_info_ul(ul)

        def get_any(*labels: str) -> str:
            for label in labels:
                value = kv.get(_norm(label))
                if value and not _is_na(value):
                    return value
            return "N/A"

        detail["titre_original"] = get_any("Titre original")
        detail["origine"] = get_any("Origine")
        detail["type_detail"] = get_any("Type")
        detail["genres"] = get_any("Genres")
        detail["themes"] = get_any("Thèmes", "Themes")
        detail["scenariste"] = get_any("Scénariste", "Scenariste")
        detail["dessinateur"] = get_any("Dessinateur")
        detail["editeur_vo"] = get_any("Éditeur VO", "Editeur VO")
        detail["prepublication"] = get_any("Prépublié dans", "Prepublie dans")
        detail["nb_vol_vo_detail"] = get_any("Nb volumes VO", "Nb volume VO")
        detail["nb_vol_vf_detail"] = get_any("Nb volumes VF", "Nb volume VF")
        detail["date_vo_detail"] = get_any("Date de sortie VO", "Date de parution VO", "Sortie VO", "Parution VO")
        detail["date_vf_detail"] = get_any("Date de sortie VF", "Date de parution VF", "Sortie VF", "Parution VF")
        detail["note_detail"] = get_any("Note")
        detail["age_detail"] = get_any("Âge conseillé", "Age conseillé")
        detail["public_averti"] = get_any("Pour public averti")
        detail["disponible_france"] = get_any("Se trouve dans le commerce en France", "Disponible en France")

        if not _is_na(detail["origine"]):
            match = re.search(r"(\d{4})", detail["origine"])
            if match:
                detail["annee_vo"] = match.group(1)

        for src, count_key, status_key in (
            (get_any("Nb chapitres VO"), "nb_chapitres_vo", "statut_vo"),
            (get_any("Nb chapitres VF"), "nb_chapitres_vf", "statut_vf"),
        ):
            if _is_na(src):
                continue
            if "(" in src and ")" in src:
                before, after = src.split("(", 1)
                detail[count_key] = _clean_spaces(before)
                detail[status_key] = _clean_spaces(after.replace(")", ""))
            else:
                detail[count_key] = _clean_spaces(src)
        return detail

    def _backfill_list_fields_from_detail(self, series: Dict[str, str], detail: Dict[str, str]) -> None:
        mapping = {
            "titre": "_titre_fr_fallback",
            "type_liste": "type_detail",
            "nb_vol_vo_liste": "nb_vol_vo_detail",
            "nb_vol_vf_liste": "nb_vol_vf_detail",
            "date_vo_liste": "date_vo_detail",
            "date_vf_liste": "date_vf_detail",
            "age_liste": "age_detail",
            "note_liste": "note_detail",
        }
        for dst, src in mapping.items():
            if _is_na(series.get(dst)) and not _is_na(detail.get(src)):
                series[dst] = detail[src]

    @staticmethod
    def _preserve_known_list_fields(current: Dict[str, str], existing: Dict[str, str]) -> None:
        for field in ListRow.__dataclass_fields__:
            if field in {"extraction_time", "url_fiche"}:
                continue
            if _is_na(current.get(field)) and not _is_na(existing.get(field)):
                current[field] = existing[field]

    @staticmethod
    def _merge_observed_list_fields(existing: Dict[str, str], current: Dict[str, str]) -> Dict[str, str]:
        merged = dict(existing)
        for field in ListRow.__dataclass_fields__:
            if field in {"extraction_time", "url_fiche"}:
                continue
            observed = current.get(field)
            if not _is_na(observed):
                merged[field] = observed
        return merged

    def _fetch_full_series_data(self, series: Dict[str, str]) -> Optional[Dict[str, str]]:
        if self.is_banned_type(series.get("type_liste", "")):
            self.session_stats["skipped_by_type"] += 1
            return None
        self._pace_detail_request()
        html = self.fetch_html(series["url_fiche"])
        detail = self.extract_series_detail_from_html(html)
        probe = " ".join([series.get("type_liste", ""), detail.get("type_detail", ""), detail.get("genres", ""), detail.get("themes", "")])
        if self.is_banned_type(probe):
            self.session_stats["skipped_by_type"] += 1
            return None
        self._backfill_list_fields_from_detail(series, detail)
        detail.pop("_titre_fr_fallback", None)
        return self._normalize_row({**series, **detail})

    def _series_change_reasons(
        self,
        existing: Dict[str, str],
        current: Dict[str, str],
    ) -> Dict[str, Tuple[str, str]]:
        # La note varie au fil des votes et peut etre actualisee depuis le listing
        # sans recharger toute la fiche. L'URL sert deja de cle de rapprochement.
        fields = (
            "titre",
            "titre_alternatif",
            "type_liste",
            "nb_vol_vo_liste",
            "nb_vol_vf_liste",
            "age_liste",
            "date_vf_liste",
            "date_vo_liste",
        )
        reasons: Dict[str, Tuple[str, str]] = {}
        for field in fields:
            previous = _clean_spaces(existing.get(field, "N/A"))
            observed = _clean_spaces(current.get(field, "N/A"))
            # Une cellule absente du listing n'est pas une information nouvelle.
            # La traiter comme un changement transformait les erreurs de parsing en
            # rafales de consultations des fiches detail.
            if _is_na(observed):
                continue
            if self._listing_comparison_value(field, previous) != self._listing_comparison_value(
                field,
                observed,
            ):
                reasons[field] = (previous, observed)
        return reasons

    @staticmethod
    def _listing_comparison_value(field: str, value: str) -> str:
        cleaned = _clean_spaces(value)
        normalized = _norm(cleaned)
        if field in {"date_vf_liste", "date_vo_liste"} and normalized in {
            "0",
            "-",
            "–",
            "—",
            "n/a",
            "na",
        }:
            return ""
        if field in {"nb_vol_vo_liste", "nb_vol_vf_liste"}:
            match = re.search(r"\d+", cleaned)
            if match:
                return str(int(match.group(0)))
        return normalized

    def _series_change_requires_detail(
        self,
        existing: Dict[str, str],
        current: Dict[str, str],
    ) -> bool:
        reasons = self._series_change_reasons(existing, current)
        return any(field in {"nb_vol_vo_liste", "nb_vol_vf_liste"} for field in reasons)

    def _series_changed_on_list(self, existing: Dict[str, str], current: Dict[str, str]) -> bool:
        return bool(self._series_change_reasons(existing, current))

    def _series_vf_is_ongoing(self, existing: Dict[str, str]) -> bool:
        return "en cours" in _norm(existing.get("nb_vol_vf_detail", ""))

    def _series_needs_release_refresh(
        self,
        existing: Dict[str, str],
        refresh_stale_days: Optional[int],
    ) -> bool:
        if not self._series_vf_is_ongoing(existing):
            return False
        checked_at = _parse_extraction_time(existing.get("parutions_vf_verifiees_le"))
        if checked_at is None:
            return True
        if not refresh_stale_days or refresh_stale_days <= 0:
            return False
        return (datetime.now() - checked_at).days >= refresh_stale_days

    def scrape_letter_diff(
        self,
        letter: str,
        max_pages: Optional[int] = None,
        max_series: Optional[int] = None,
        refresh_stale_days: Optional[int] = None,
        drop_missing: bool = True,
        max_missing_ratio: float = 0.15,
        flush_every: int = 25,
        resume: bool = True,
    ) -> List[Dict[str, str]]:
        label = self._letter_label(letter)
        tag = self._letter_tag(letter)
        print(f"\n{'=' * 60}\nLETTRE {label} - DIFF {self.backend.upper()}\n{'=' * 60}")

        existing_rows = self._load_json_list(self._letter_paths(tag)[0])
        if not existing_rows:
            existing_rows = self._read_existing_csv_for_letter(tag)
        existing_by_url = {
            _ensure_abs_url(row.get("url_fiche", "")): self._normalize_row(row)
            for row in existing_rows
            if row.get("url_fiche")
        }
        initial_existing_count = len(existing_by_url)
        initial_existing_urls = set(existing_by_url)
        print(f"  Base existante: {len(existing_by_url)} series")

        updated_rows: List[Dict[str, str]] = []
        seen_urls = set()
        page_num = 0
        empty_pages = 0
        accessible_listing_pages = 0
        successful_listing_pages = 0
        listing_failed = False
        access_blocked = False
        detail_abort = False
        consecutive_detail_failures = 0
        consecutive_page_failures = 0
        max_detail_failures = max(1, _env_int("NAUTILJON_ABORT_AFTER_DETAIL_FAILURES", 2))
        max_page_failures = max(1, _env_int("NAUTILJON_PAGE_FAILURE_RETRIES", 1))
        since_flush = 0
        counters = {"new": 0, "changed": 0, "parutions": 0, "reused": 0, "removed": 0}
        errors_before_letter = self.session_stats["errors"]
        checkpoint_path = self._letter_checkpoint_path(tag)
        resumed_from_checkpoint = False
        listing_complete = False
        coverage_confirmed = False
        letter_settings = {
            "checkpoint_version": LETTER_CHECKPOINT_VERSION,
            "letter": letter,
            "max_pages": max_pages,
            "max_series": max_series,
            "refresh_stale_days": refresh_stale_days,
            "drop_missing": drop_missing,
            "max_missing_ratio": max_missing_ratio,
        }

        checkpoint = self._load_json_dict(checkpoint_path)
        partial_rows = self._load_json_list(self._letter_paths(tag)[2])
        if resume:
            if checkpoint and self._letter_checkpoint_is_compatible(tag, checkpoint) and partial_rows:
                updated_rows = [self._normalize_row(row) for row in partial_rows]
                for row in updated_rows:
                    self._backfill_list_fields_from_detail(row, row)
                    url = _ensure_abs_url(row.get("url_fiche", ""))
                    previous = existing_by_url.get(url)
                    if previous:
                        self._preserve_known_list_fields(row, previous)
                seen_urls = {
                    _ensure_abs_url(row.get("url_fiche", ""))
                    for row in updated_rows
                    if row.get("url_fiche")
                }
                for url in seen_urls:
                    existing_by_url.pop(url, None)
                page_num = int(checkpoint.get("page_num", 0) or 0)
                accessible_listing_pages = int(checkpoint.get("accessible_listing_pages", 0) or 0)
                successful_listing_pages = int(checkpoint.get("successful_listing_pages", 0) or 0)
                next_listing_url = str(checkpoint.get("next_listing_url", "") or "")
                if self.backend == "flaresolverr" and next_listing_url:
                    self._set_flaresolverr_listing_url(letter, page_num, next_listing_url)
                saved_counters = checkpoint.get("counters")
                if isinstance(saved_counters, dict):
                    counters.update({key: int(saved_counters.get(key, value) or 0) for key, value in counters.items()})
                resumed_from_checkpoint = True
                listing_complete = self._coverage_checkpoint_is_complete(tag, checkpoint)
                if listing_complete:
                    coverage_confirmed = self._coverage_gap_confirmed_by_archive(
                        tag,
                        initial_existing_urls,
                        set(existing_by_url),
                        len(updated_rows),
                        checkpoint,
                    )
                    if coverage_confirmed:
                        print(
                            f"  Couverture {label} confirmee par deux listings complets et stables; "
                            "finalisation depuis le checkpoint sans nouvelle requete."
                        )
                    else:
                        self._archive_letter_progress(
                            tag,
                            checkpoint_path,
                            "couverture_a_reconfirmer",
                        )
                        updated_rows = []
                        seen_urls = set()
                        existing_by_url = {
                            _ensure_abs_url(row.get("url_fiche", "")): self._normalize_row(row)
                            for row in existing_rows
                            if row.get("url_fiche")
                        }
                        page_num = 0
                        accessible_listing_pages = 0
                        successful_listing_pages = 0
                        counters = {"new": 0, "changed": 0, "parutions": 0, "reused": 0, "removed": 0}
                        resumed_from_checkpoint = False
                        listing_complete = False
                print(
                    f"  Reprise lettre {label}: "
                    + (
                        f"listing complet, {len(updated_rows)} serie(s) deja traitee(s)."
                        if listing_complete
                        else f"page {page_num + 1}, {len(updated_rows)} serie(s) deja traitee(s)."
                    )
                )
            elif checkpoint:
                self._archive_letter_progress(tag, checkpoint_path, "checkpoint_non_reutilisable")
        elif checkpoint:
            self._archive_letter_progress(tag, checkpoint_path, "reprise_desactivee")

        def save_checkpoint(next_page: int) -> None:
            self.save_letter_files(tag, updated_rows, partial=True)
            payload = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "data_schema_version": DATA_SCHEMA_VERSION,
                "settings": letter_settings,
                "page_num": next_page,
                "accessible_listing_pages": accessible_listing_pages,
                "successful_listing_pages": successful_listing_pages,
                "counters": counters,
                "listing_complete": listing_complete,
            }
            if self.backend == "flaresolverr":
                payload["next_listing_url"] = self._flaresolverr_listing_urls.get((tag, next_page), "")
            self._write_json_atomic(checkpoint_path, payload)

        if resumed_from_checkpoint and not listing_complete:
            candidates = [
                (index, row)
                for index, row in enumerate(updated_rows)
                if self._series_needs_release_refresh(row, refresh_stale_days)
            ]
            if candidates:
                print(
                    f"  Reprise: {len(candidates)} serie(s) VF en cours "
                    "a completer ou rafraichir pour les parutions."
                )
            for index, row in candidates:
                try:
                    refreshed = self._fetch_full_series_data(dict(row))
                    if refreshed:
                        updated_rows[index] = refreshed
                        counters["parutions"] += 1
                        consecutive_detail_failures = 0
                        self._sleep_detail_delay()
                except NautiljonAccessBlockedError as exc:
                    self.session_stats["errors"] += 1
                    access_blocked = True
                    listing_failed = True
                    print(f"    ACCES BLOQUE pendant la reprise des parutions: {str(exc)[:220]}")
                    break
                except Exception as exc:
                    self.session_stats["errors"] += 1
                    consecutive_detail_failures += 1
                    print(f"    Erreur migration parutions: {str(exc)[:140]}")
                    if consecutive_detail_failures >= max_detail_failures:
                        detail_abort = True
                        print(
                            "    Trop d'erreurs de fiches consecutives; arret prudent de la lettre "
                            f"({consecutive_detail_failures}/{max_detail_failures})."
                        )
                        break
            save_checkpoint(page_num)

        while not listing_complete:
            if access_blocked or detail_abort:
                break
            if max_pages is not None and page_num >= max_pages:
                break
            try:
                page_url, page_series = self.fetch_listing_page(letter, page_num)
                accessible_listing_pages += 1
                consecutive_page_failures = 0
                print(f"  Page {page_num + 1}: {len(page_series)} entrees ({page_url})")
            except NautiljonAccessBlockedError as exc:
                access_blocked = True
                listing_failed = True
                print(f"  ACCES BLOQUE: {str(exc)[:240]}")
                save_checkpoint(page_num)
                break
            except Exception as exc:
                consecutive_page_failures += 1
                print(
                    f"  Page {page_num + 1} indisponible, tentative "
                    f"{consecutive_page_failures}/{max_page_failures}: {str(exc)[:160]}"
                )
                save_checkpoint(page_num)
                if consecutive_page_failures >= max_page_failures:
                    listing_failed = True
                    break
                self._sleep_failure_pause()
                continue

            if not page_series:
                if self.backend == "flaresolverr":
                    listing_failed = True
                    print("  Page vide inattendue via FlareSolverr: fin de listing non validee.")
                    save_checkpoint(page_num)
                    break
                empty_pages += 1
                if empty_pages >= 3:
                    break
            else:
                successful_listing_pages += 1
                new_on_page = 0
                for series in page_series:
                    url = _ensure_abs_url(series.get("url_fiche", ""))
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1
                    series["url_fiche"] = url
                    if self.is_banned_type(series.get("type_liste", "")):
                        existing_by_url.pop(url, None)
                        self.session_stats["skipped_by_type"] += 1
                        continue
                    if max_series is not None and len(updated_rows) >= max_series:
                        break

                    existing = existing_by_url.pop(url, None)
                    action = "reuse"
                    needs_detail = False
                    if existing is None:
                        action = "new"
                        needs_detail = True
                    elif self._series_changed_on_list(existing, series):
                        action = "changed"
                        needs_detail = self._series_change_requires_detail(existing, series)
                    elif self._series_needs_release_refresh(existing, refresh_stale_days):
                        action = "parutions"
                        needs_detail = True

                    if needs_detail:
                        change_suffix = ""
                        if action == "changed" and existing:
                            reasons = self._series_change_reasons(existing, series)
                            summary = ", ".join(
                                f"{field}: {before} -> {after}"
                                for field, (before, after) in reasons.items()
                            )
                            if summary:
                                change_suffix = f" [{summary[:180]}]"
                        print(
                            f"    MAJ {action}: {(series.get('titre') or 'N/A')[:60]}"
                            f"{change_suffix}"
                        )
                        try:
                            if existing:
                                self._preserve_known_list_fields(series, existing)
                            full = self._fetch_full_series_data(series)
                            if full:
                                updated_rows.append(full)
                                counters[action] += 1
                                consecutive_detail_failures = 0
                                self._sleep_detail_delay()
                        except NautiljonAccessBlockedError as exc:
                            self.session_stats["errors"] += 1
                            access_blocked = True
                            listing_failed = True
                            seen_urls.discard(url)
                            if existing:
                                existing_by_url[url] = existing
                            print(f"    ACCES BLOQUE pendant une fiche detail: {str(exc)[:220]}")
                            break
                        except Exception as exc:
                            self.session_stats["errors"] += 1
                            consecutive_detail_failures += 1
                            print(f"    Erreur detail: {str(exc)[:140]}")
                            if existing:
                                updated_rows.append(existing)
                            if consecutive_detail_failures >= max_detail_failures:
                                detail_abort = True
                                print(
                                    "    Trop d'erreurs de fiches consecutives; arret prudent de la lettre "
                                    f"({consecutive_detail_failures}/{max_detail_failures})."
                                )
                                break
                            continue
                    else:
                        updated_rows.append(self._merge_observed_list_fields(existing, series))
                        if action == "changed":
                            reasons = self._series_change_reasons(existing, series)
                            summary = ", ".join(
                                f"{field}: {before} -> {after}"
                                for field, (before, after) in reasons.items()
                            )
                            print(
                                f"    MAJ listing: {(series.get('titre') or 'N/A')[:60]}"
                                + (f" [{summary[:180]}]" if summary else "")
                            )
                            counters["changed"] += 1
                        else:
                            counters["reused"] += 1

                    since_flush += 1
                    if flush_every and since_flush >= flush_every:
                        save_checkpoint(page_num)
                        since_flush = 0

                if access_blocked or detail_abort:
                    save_checkpoint(page_num)
                    break
                save_checkpoint(page_num + 1)
                if new_on_page == 0:
                    empty_pages += 1
                    print(f"  Page {page_num + 1} sans nouvelle URL ({empty_pages}/3).")
                    if empty_pages >= 3:
                        break
                else:
                    empty_pages = 0
                if max_series is not None and len(updated_rows) >= max_series:
                    break
                if self.backend == "flaresolverr" and not self._flaresolverr_listing_has_next(letter, page_num):
                    if len(page_series) >= 50:
                        listing_failed = True
                        print(
                            f"  Pagination incoherente: page {page_num + 1} pleine "
                            "mais aucun lien vers la page suivante."
                        )
                        save_checkpoint(page_num)
                    else:
                        print(f"  Fin de pagination explicite apres la page {page_num + 1}.")
                        listing_complete = True
                        save_checkpoint(page_num + 1)
                    break

            if not page_series:
                save_checkpoint(page_num + 1)

            page_num += 1
            self._sleep_delay()

        if accessible_listing_pages == 0:
            self.session_stats["errors"] += 1
            existing_kept = sorted(
                updated_rows + list(existing_by_url.values()),
                key=lambda row: _norm(row.get("titre", "")),
            )
            self.session_stats["series_by_letter"][label] = len(existing_kept)
            self.session_stats["total_series"] += len(existing_kept)
            self.session_stats["diff_by_letter"][label] = {
                "new": 0,
                "changed": 0,
                "parutions": 0,
                "reused": len(existing_kept),
                "removed": 0,
                "listing_failed": True,
                "access_blocked": access_blocked,
                "coverage_failed": False,
                "detail_failed": False,
                "limited": False,
            }
            if existing_kept:
                print(
                    f"  Listing {label} inaccessible: conservation de {len(existing_kept)} series existantes, "
                    "aucune suppression appliquee."
                )
            else:
                print(f"  Listing {label} inaccessible: aucune donnee locale a mettre a jour.")
            return existing_kept

        detail_failed = self.session_stats["errors"] > errors_before_letter
        limited = max_pages is not None or max_series is not None
        missing_count = len(existing_by_url)
        missing_ratio = (missing_count / initial_existing_count) if initial_existing_count else 0.0
        coverage_exceeded = bool(
            not limited
            and initial_existing_count >= 20
            and max_missing_ratio >= 0
            and missing_ratio > max_missing_ratio
        )
        if coverage_exceeded and not coverage_confirmed:
            coverage_confirmed = self._coverage_gap_confirmed_by_archive(
                tag,
                initial_existing_urls,
                set(existing_by_url),
                len(updated_rows),
                self._load_json_dict(checkpoint_path) or {},
            )
        coverage_failed = coverage_exceeded and not coverage_confirmed
        if coverage_exceeded and coverage_confirmed:
            print(
                f"  Couverture atypique mais stable: {missing_count}/{initial_existing_count} fiches "
                "historiques absentes sur deux listings complets; mise a jour acceptee."
            )
        if coverage_failed:
            print(
                f"  Couverture invalide: {missing_count}/{initial_existing_count} fiches historiques absentes "
                f"({missing_ratio:.1%}), seuil autorise {max_missing_ratio:.1%}."
            )
        if listing_failed or coverage_failed or detail_failed or limited:
            safe_rows = sorted(
                updated_rows + list(existing_by_url.values()),
                key=lambda row: _norm(row.get("titre", "")),
            )
            counters["listing_failed"] = listing_failed
            counters["access_blocked"] = access_blocked
            counters["coverage_failed"] = coverage_failed
            counters["missing_count"] = missing_count
            counters["missing_ratio"] = round(missing_ratio, 6)
            counters["detail_failed"] = detail_failed
            counters["limited"] = limited
            self.session_stats["series_by_letter"][label] = len(safe_rows)
            self.session_stats["total_series"] += len(safe_rows)
            self.session_stats["diff_by_letter"][label] = counters
            if listing_failed:
                print("  Fin de listing incertaine: fichier final inchange, checkpoint conserve.")
            elif coverage_failed:
                print("  Couverture anormale: fichier final inchange, checkpoint conserve.")
            elif detail_failed:
                print("  Detail(s) inaccessible(s): fichier final inchange, checkpoint conserve.")
            else:
                print("  Execution limitee: fichier final inchange, checkpoint conserve.")
            return safe_rows

        cache_rows = list(updated_rows)
        if not drop_missing:
            updated_rows.extend(existing_by_url.values())
        else:
            counters["removed"] = missing_count

        updated_rows = sorted(updated_rows, key=lambda row: _norm(row.get("titre", "")))
        cache_rows = updated_rows if drop_missing else sorted(cache_rows, key=lambda row: _norm(row.get("titre", "")))
        if drop_missing:
            self.save_letter_files(tag, updated_rows, partial=False)
        else:
            self.save_controlled_letter_files(tag, updated_rows)
        self._remove_partial_files(tag)
        self._remove_checkpoint(checkpoint_path)
        counters["listing_failed"] = False
        counters["access_blocked"] = False
        counters["coverage_failed"] = False
        counters["missing_count"] = missing_count
        counters["missing_ratio"] = round(missing_ratio, 6)
        counters["detail_failed"] = False
        counters["limited"] = False
        self.session_stats["series_by_letter"][label] = len(updated_rows)
        self.session_stats["total_series"] += len(updated_rows)
        self.session_stats["diff_by_letter"][label] = counters
        try:
            self._save_letter_cache(
                tag,
                cache_rows,
                counters,
                source_mode="final" if drop_missing else "controlled",
            )
            counters["cache_saved"] = True
        except Exception as exc:
            counters["cache_saved"] = False
            print(f"  Avertissement: cache de la lettre {label} non sauvegarde ({str(exc)[:180]}).")
        suffix = "controle uniquement" if not drop_missing else "fichier final mis a jour"
        print(f"  Lettre {label}: {len(updated_rows)} series, {suffix} ({counters})")
        return updated_rows

    def _read_existing_csv_for_letter(self, tag: str) -> List[Dict[str, str]]:
        _, csv_path, _, _ = self._letter_paths(tag)
        if not os.path.exists(csv_path):
            return []
        return [self._normalize_row(row) for row in self._read_csv_rows(csv_path)]

    def scrape_all_letters_diff(
        self,
        letters: Optional[List[str]] = None,
        max_pages_per_letter: Optional[int] = None,
        max_series_per_letter: Optional[int] = None,
        refresh_stale_days: Optional[int] = None,
        drop_missing: bool = True,
        max_missing_ratio: float = 0.15,
        min_days_between_diff_exports: int = 30,
        abort_after_listing_failures: int = 1,
        flush_every: int = 25,
        resume: bool = True,
        force: bool = False,
    ) -> RunResult:
        all_catalog_letters = self.get_all_letters()
        letters_to_scrape = letters or all_catalog_letters
        requested_labels = [self._letter_label(letter) for letter in letters_to_scrape]
        pacing = self.session_stats["anti_ban_config"]
        print(
            "Cadence anti-ban: "
            f"{pacing['delay_min']:g}-{pacing['delay_max']:g}s entre requetes, "
            f"pause {pacing['batch_pause_min']:g}-{pacing['batch_pause_max']:g}s "
            f"toutes les {pacing['batch_size']} requetes, "
            f"{pacing['letter_pause_min']:g}-{pacing['letter_pause_max']:g}s entre lettres."
        )
        print(
            "Cadence fiches detail: "
            f"{pacing['detail_delay_min']:g}-{pacing['detail_delay_max']:g}s apres chaque fiche, "
            f"pause {pacing['detail_batch_pause_min']:g}-{pacing['detail_batch_pause_max']:g}s "
            f"toutes les {pacing['detail_batch_size']} fiches, "
            f"limite persistante {pacing['detail_window_size']} fiches par "
            f"{pacing['detail_window_seconds'] / 60:g} min."
        )
        cooldown = self._resolve_access_cooldown()
        if cooldown:
            print(
                "Diff refuse pendant la quarantaine anti-ban. Reprise autorisee apres "
                f"{cooldown.get('resume_after')}."
            )
            result = RunResult(
                status="failed",
                reason="access_cooldown_active",
                requested_letters=requested_labels,
            )
            self.mark_run_state("diff", result)
            return result
        all_catalog_requested = (
            set(letters_to_scrape) == set(all_catalog_letters)
            and len(letters_to_scrape) == len(all_catalog_letters)
            and max_pages_per_letter is None
            and max_series_per_letter is None
        )
        full_catalog_requested = all_catalog_requested and drop_missing
        if all_catalog_requested and not drop_missing:
            print(
                "Diff complet refuse: NAUTILJON_DROP_MISSING=false est reserve aux controles "
                "sur une liste explicite de lettres. Utilisez NAUTILJON_DROP_MISSING=true "
                "pour les 27 lettres."
            )
            result = RunResult(
                status="failed",
                reason="full_catalog_requires_drop_missing",
                requested_letters=requested_labels,
            )
            self.mark_run_state("diff", result)
            return result
        if letters is None and force:
            print(
                "Diff complet refuse: NAUTILJON_FORCE_SCRAPE=true ignorerait tous les "
                "caches recents. Remettez NAUTILJON_FORCE_SCRAPE=false, ou indiquez "
                "explicitement une liste limitee de lettres pour un controle force."
            )
            result = RunResult(
                status="failed",
                reason="full_catalog_force_refused",
                requested_letters=requested_labels,
            )
            self.mark_run_state("diff", result)
            return result
        should_skip, last_success, age = self.should_skip_recent_success("diff", min_days_between_diff_exports)
        if full_catalog_requested and should_skip and not force and last_success and age is not None:
            print(
                "Diff ignore: dernier diff finalise le "
                f"{last_success.get('completed_at')} ({age.days} jours)."
            )
            result = RunResult(
                status="skipped",
                reason="recent_complete_export",
                rows_count=int(last_success.get("rows_count", 0) or 0),
                requested_letters=requested_labels,
                completed_letters=requested_labels,
                export_paths=dict(last_success.get("export_paths", {})),
            )
            self.mark_run_state("diff", result)
            return result

        if self.backend == "flaresolverr":
            try:
                if self._verified_public_ips:
                    gluetun_ip, flaresolverr_ip = self._verified_public_ips
                else:
                    gluetun_ip, flaresolverr_ip = self._flaresolverr_public_ips()
                print(f"IP Gluetun: {gluetun_ip} | IP FlareSolverr: {flaresolverr_ip}")
                if gluetun_ip != flaresolverr_ip:
                    raise RuntimeError("les IP publiques de Gluetun et FlareSolverr sont differentes")
            except Exception as exc:
                self.close_flaresolverr()
                result = RunResult(
                    status="failed",
                    reason="flaresolverr_preflight_failed",
                    requested_letters=requested_labels,
                )
                print(f"Diff refuse: preflight FlareSolverr en echec ({str(exc)[:300]}).")
                self.mark_run_state("diff", result)
                return result

        self.session_stats["start_time"] = datetime.now()
        config = self._diff_run_config(
            letters_to_scrape,
            max_pages_per_letter,
            max_series_per_letter,
            refresh_stale_days,
            drop_missing,
            max_missing_ratio,
        )
        completed_letters = self._load_diff_run_checkpoint(config, resume)
        fatal_error = False
        aborted_listing_failures = False
        incomplete_reason = ""
        consecutive_listing_failures = 0
        try:
            if self.backend == "selenium":
                self.setup_browser()
            for index, letter in enumerate(letters_to_scrape, start=1):
                label = self._letter_label(letter)
                tag = self._letter_tag(letter)
                if label in completed_letters:
                    print(f"\nProgression: {index}/{len(letters_to_scrape)} - lettre {label} deja finalisee, ignoree")
                    continue
                print(f"\nProgression: {index}/{len(letters_to_scrape)}")
                if full_catalog_requested and not force:
                    cached = self._load_reusable_letter_cache(
                        tag,
                        min_days_between_diff_exports,
                        max_missing_ratio,
                    )
                    if cached:
                        cached_rows, marker, cache_age = cached
                        self.save_letter_files(tag, cached_rows, partial=False)
                        checkpoint_path = self._letter_checkpoint_path(tag)
                        if os.path.isfile(checkpoint_path) or any(
                            os.path.isfile(path) for path in self._letter_paths(tag)[2:]
                        ):
                            self._archive_letter_progress(
                                tag,
                                checkpoint_path,
                                "remplace_par_cache_valide",
                            )
                            self._remove_partial_files(tag)
                            self._remove_checkpoint(checkpoint_path)
                        cached_stats = marker.get("stats")
                        stats = dict(cached_stats) if isinstance(cached_stats, dict) else {}
                        stats.update({
                            "cache_reused": True,
                            "cache_age_days": cache_age.total_seconds() / 86400,
                            "cache_source_mode": marker.get("source_mode", "unknown"),
                        })
                        self.session_stats["series_by_letter"][label] = len(cached_rows)
                        self.session_stats["total_series"] += len(cached_rows)
                        self.session_stats["diff_by_letter"][label] = stats
                        completed_letters.append(label)
                        self._save_diff_run_checkpoint(config, completed_letters)
                        print(
                            f"  Lettre {label} reutilisee depuis son cache valide "
                            f"({cache_age.total_seconds() / 86400:.1f} jour, {len(cached_rows)} series)."
                        )
                        continue
                self.scrape_letter_diff(
                    letter,
                    max_pages=max_pages_per_letter,
                    max_series=max_series_per_letter,
                    refresh_stale_days=refresh_stale_days,
                    drop_missing=drop_missing,
                    max_missing_ratio=max_missing_ratio,
                    flush_every=flush_every,
                    resume=resume,
                )
                diff_stats = self.session_stats["diff_by_letter"].get(label, {})
                letter_incomplete = bool(
                    diff_stats.get("listing_failed")
                    or diff_stats.get("access_blocked")
                    or diff_stats.get("coverage_failed")
                    or diff_stats.get("detail_failed")
                    or diff_stats.get("limited")
                )
                if not letter_incomplete:
                    completed_letters.append(label)
                    self._save_diff_run_checkpoint(config, completed_letters)
                if diff_stats.get("access_blocked"):
                    aborted_listing_failures = True
                    incomplete_reason = "access_blocked"
                    self._record_access_cooldown("access_blocked")
                    print(
                        "Diff interrompu immediatement: IP de sortie bloquee par Nautiljon. "
                        "Les donnees et le checkpoint sont conserves."
                    )
                    break
                if diff_stats.get("listing_failed"):
                    incomplete_reason = "listing_inaccessible"
                    consecutive_listing_failures += 1
                    if abort_after_listing_failures > 0 and consecutive_listing_failures >= abort_after_listing_failures:
                        aborted_listing_failures = True
                        incomplete_reason = "listing_inaccessible"
                        print(
                            "Diff interrompu: "
                            f"{consecutive_listing_failures} listing(s) consecutif(s) inaccessible(s). "
                            "Les donnees existantes sont conservees."
                        )
                        break
                else:
                    consecutive_listing_failures = 0
                if diff_stats.get("coverage_failed"):
                    incomplete_reason = "coverage_incomplete"
                    print("Diff interrompu: couverture du listing incoherente avec la base existante.")
                    break
                if diff_stats.get("detail_failed"):
                    incomplete_reason = "detail_inaccessible"
                    print("Diff interrompu: au moins une fiche detail est inaccessible.")
                    break
                if diff_stats.get("limited"):
                    incomplete_reason = "execution_limited"
                    break
                if index < len(letters_to_scrape):
                    self._sleep_letter_pause()
        except NautiljonAccessBlockedError as exc:
            fatal_error = True
            aborted_listing_failures = True
            incomplete_reason = "access_blocked"
            self.session_stats["errors"] += 1
            self._record_access_cooldown(str(exc))
            print(f"Acces Nautiljon bloque: {str(exc)[:240]}")
        except Exception as exc:
            fatal_error = True
            incomplete_reason = "fatal_error"
            self.session_stats["errors"] += 1
            print(f"Erreur fatale: {exc}")
        finally:
            self.close_browser()
            self.session_stats["end_time"] = datetime.now()
            if self.session_stats["start_time"]:
                self.session_stats["duration"] = str(self.session_stats["end_time"] - self.session_stats["start_time"])
        combined = self.concat_letters()
        all_requested_completed = set(completed_letters) == set(requested_labels)
        export_paths: Dict[str, str] = {}
        status = "failed"
        reason = incomplete_reason or "incomplete_run"

        if all_requested_completed and full_catalog_requested and not fatal_error and self.session_stats.get("errors", 0) == 0:
            if not self._validate_final_letter_files(letters_to_scrape):
                reason = "final_letter_files_invalid"
            else:
                try:
                    export_paths = self.export_all_data(
                        combined,
                        base_filename=f"nautiljon_diff_concat_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    )
                    if combined and self._validate_final_exports(export_paths):
                        status = "success"
                        reason = "complete_catalog_exported"
                    else:
                        reason = "final_export_invalid"
                except Exception as exc:
                    self.session_stats["errors"] += 1
                    reason = "final_export_failed"
                    print(f"Erreur export final: {str(exc)[:180]}")
        elif all_requested_completed and not full_catalog_requested:
            status = "partial"
            reason = "controlled_subset_complete"
        elif completed_letters:
            status = "partial"
        elif aborted_listing_failures and reason != "access_blocked":
            reason = "listing_inaccessible"

        result = RunResult(
            status=status,
            reason=reason,
            rows_count=len(combined),
            requested_letters=requested_labels,
            completed_letters=completed_letters,
            export_paths=export_paths,
        )
        if status == "success":
            self.mark_success("diff", len(combined), export_paths)
            self._remove_checkpoint(self._state_path("diff_checkpoint"))
            self._clear_access_cooldown()
        elif all_requested_completed:
            self._remove_checkpoint(self._state_path("diff_checkpoint"))
        self.mark_run_state("diff", result)
        return result

    def probe_discovery(self, letters: Optional[List[str]] = None, max_pages: int = 1) -> None:
        for letter in letters or ["a"]:
            for page_num in range(max_pages):
                url, rows = self.fetch_listing_page(letter, page_num)
                if not rows:
                    raise RuntimeError(f"Listing accessible mais vide pour {self._letter_label(letter)} page {page_num + 1}")
                print(f"{self._letter_label(letter)} page {page_num + 1}: {len(rows)} lignes via {url}")


def _parse_letters(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    letters = []
    for chunk in raw.split(","):
        value = chunk.strip().lower()
        if not value:
            continue
        letters.append("%23" if value == "#" else value)
    return letters


def _parse_csv_list(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    values = [_clean_spaces(chunk) for chunk in raw.split(",")]
    values = [value for value in values if value]
    return values or None


def _new_scraper(args: argparse.Namespace) -> NautiljonScraper:
    out_dir = args.out_dir or os.environ.get("NAUTILJON_OUT_DIR", "./output")
    delay = args.delay if args.delay is not None else _env_float("NAUTILJON_DELAY", 4.0)
    delay_min = args.delay_min
    delay_max = args.delay_max
    if delay_min is None and os.environ.get("NAUTILJON_DELAY_MIN", "").strip():
        delay_min = _env_float("NAUTILJON_DELAY_MIN", delay)
    if delay_max is None and os.environ.get("NAUTILJON_DELAY_MAX", "").strip():
        delay_max = _env_float("NAUTILJON_DELAY_MAX", delay)
    if (delay_min is None) != (delay_max is None):
        raise ValueError("NAUTILJON_DELAY_MIN et NAUTILJON_DELAY_MAX doivent etre definis ensemble.")
    backend = os.environ.get("NAUTILJON_BACKEND", "selenium").strip().lower()
    if backend not in {"selenium", "flaresolverr", "http"}:
        raise ValueError("NAUTILJON_BACKEND doit valoir selenium, flaresolverr ou http.")
    return NautiljonScraper(
        out_dir=out_dir,
        delay=delay,
        delay_min=delay_min,
        delay_max=delay_max,
        backend=backend,
        browser_headless=_env_bool("NAUTILJON_BROWSER_HEADLESS", False),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scraper Nautiljon Selenium avec reprise securisee.")
    parser.add_argument("--out-dir", default=os.environ.get("NAUTILJON_OUT_DIR", "./output"))
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--delay-min", type=float, default=None)
    parser.add_argument("--delay-max", type=float, default=None)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("import-csv", help="Importe les CSV presents dans output/letters et genere les JSON.")
    sub.add_parser("concat", help="Concatene les lettres deja importees/scrapees.")

    diff = sub.add_parser("diff", help="Diff mensuel avec decouverte des nouvelles fiches.")
    diff.add_argument("--letters", default=os.environ.get("NAUTILJON_LETTERS"))
    diff.add_argument("--max-pages-per-letter", type=int, default=_env_optional_int("NAUTILJON_MAX_PAGES_PER_LETTER"))
    diff.add_argument("--max-series-per-letter", type=int, default=_env_optional_int("NAUTILJON_MAX_SERIES_PER_LETTER"))
    diff.add_argument(
        "--refresh-stale-days",
        type=int,
        default=_env_optional_int("NAUTILJON_REFRESH_STALE_DAYS"),
        help="Rafraichit uniquement les parutions des series dont la VF est en cours.",
    )
    diff.add_argument("--keep-missing", action="store_true", default=not _env_bool("NAUTILJON_DROP_MISSING", True))
    diff.add_argument(
        "--max-missing-ratio",
        type=float,
        default=_env_float("NAUTILJON_MAX_MISSING_RATIO", 0.15),
        help="Refuse la finalisation si la part de fiches historiques absentes depasse ce seuil.",
    )
    diff.add_argument("--min-days-between-diff-exports", type=int, default=_env_int("NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS", 30))
    diff.add_argument("--abort-after-listing-failures", type=int, default=_env_int("NAUTILJON_ABORT_AFTER_LISTING_FAILURES", 1))
    diff.add_argument("--flush-every", type=int, default=_env_int("NAUTILJON_FLUSH_EVERY", 25))
    diff.add_argument("--resume", dest="resume", action="store_true", default=_env_bool("NAUTILJON_RESUME", True))
    diff.add_argument("--no-resume", dest="resume", action="store_false")
    diff.add_argument("--force", action="store_true", default=_env_bool("NAUTILJON_FORCE_SCRAPE", False))

    rss = sub.add_parser("discover-rss", help="Decouvre des candidats de nouvelles fiches depuis les flux RSS.")
    rss.add_argument("--rss-feeds", default=os.environ.get("NAUTILJON_RSS_FEEDS"))
    rss.add_argument("--merge-candidates", action="store_true", default=_env_bool("NAUTILJON_MERGE_RSS_CANDIDATES", False))

    probe = sub.add_parser("probe-discovery", help="Teste la decouverte HTTP des listings sans Selenium.")
    probe.add_argument("--letters", default=os.environ.get("NAUTILJON_LETTERS", "a"))
    probe.add_argument("--max-pages", type=int, default=1)

    diagnose = sub.add_parser("diagnose", help="Teste le reseau, les listings et une fiche sans ecrire de donnees.")
    diagnose.add_argument("--letter", default=os.environ.get("NAUTILJON_DIAGNOSE_LETTER", "a"))
    diagnose.add_argument(
        "--detail-url",
        default=os.environ.get("NAUTILJON_DIAGNOSE_DETAIL_URL", f"{BASE_URL}/mangas/one+piece.html"),
    )
    diagnose.add_argument("--rss-feeds", default=os.environ.get("NAUTILJON_RSS_FEEDS"))

    browser_test = sub.add_parser("browser-test", help="Teste un listing et une fiche via Selenium sans exporter.")
    browser_test.add_argument("--letter", default=os.environ.get("NAUTILJON_DIAGNOSE_LETTER", "a"))

    flaresolverr_test = sub.add_parser(
        "flaresolverr-test",
        help="Teste IP, listing et fiche via FlareSolverr sans exporter.",
    )
    flaresolverr_test.add_argument("--letter", default=os.environ.get("NAUTILJON_DIAGNOSE_LETTER", "a"))

    sub.add_parser("browser-smoke", help="Verifie uniquement le demarrage de Chromium et ChromeDriver.")

    sub.add_parser("selftest", help="Tests parser hors reseau.")
    return parser


def _run_selftests() -> None:
    scraper = NautiljonScraper(delay=0)
    list_html = """
    <div id="content"><table class="liste"><thead><tr>
    <th>Titre</th><th>Type</th><th>Vol VO</th><th>Vol VF</th><th>Age</th><th>Date VF</th><th>Date VO</th><th>Note</th>
    </tr></thead><tbody><tr>
    <td><a class="eTitre" href="/mangas/test.html">Test Manga</a><span class="infos_small">(Alt)</span></td>
    <td>Seinen</td><td>2</td><td>1</td><td>14 ans et +</td><td>2020</td><td>2018</td><td>8/10</td>
    </tr></tbody></table></div>
    """
    rows = scraper.extract_series_list_from_html(list_html)
    assert len(rows) == 1
    assert rows[0]["url_fiche"] == "https://www.nautiljon.com/mangas/test.html"
    assert rows[0]["titre"] == "Test Manga"

    detail_html = """
    <div id="content"><h1>Test Manga</h1><ul class="mb10">
    <li>Titre original : テスト</li>
    <li>Origine : Japon - 2024</li>
    <li>Type : Seinen</li>
    <li>Genres : <a>Action</a> <a>Drame</a></li>
    <li>Nb chapitres VO : 12 (Terminé)</li>
    <li>Pour public averti : Non</li>
    </ul></div>
    """
    detail = scraper.extract_series_detail_from_html(detail_html)
    assert detail["titre_original"] == "テスト"
    assert detail["annee_vo"] == "2024"
    assert detail["genres"] == "Action - Drame"
    assert detail["nb_chapitres_vo"] == "12"
    assert detail["statut_vo"] == "Terminé"
    titles = scraper._extract_candidate_titles_from_news_title(
        "Le manga Dungeon Band à paraître le mois prochain aux éditions VEGA"
    )
    assert titles == ["Dungeon Band"]
    assert scraper._candidate_url_from_title(titles[0]) == "https://www.nautiljon.com/mangas/dungeon+band.html"
    print("Self-tests OK")


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    command = args.command or os.environ.get("NAUTILJON_COMMAND", "diff")
    if command == "selftest":
        _run_selftests()
        return
    try:
        scraper = _new_scraper(args)
    except ValueError as exc:
        parser.error(str(exc))

    if command == "import-csv":
        scraper.import_existing_csv()
    elif command == "concat":
        combined = scraper.concat_letters()
        if combined:
            scraper.export_all_data(combined)
        else:
            print("Aucune lettre JSON trouvee.")
    elif command == "diff":
        result = scraper.scrape_all_letters_diff(
            letters=_parse_letters(args.letters),
            max_pages_per_letter=args.max_pages_per_letter,
            max_series_per_letter=args.max_series_per_letter,
            refresh_stale_days=args.refresh_stale_days,
            drop_missing=not args.keep_missing,
            max_missing_ratio=args.max_missing_ratio,
            min_days_between_diff_exports=args.min_days_between_diff_exports,
            abort_after_listing_failures=args.abort_after_listing_failures,
            flush_every=args.flush_every,
            resume=args.resume,
            force=args.force,
        )
        raise SystemExit(result.exit_code)
    elif command == "discover-rss":
        scraper.discover_rss_candidates(
            feed_urls=_parse_csv_list(args.rss_feeds),
            merge_candidates=args.merge_candidates,
        )
    elif command == "probe-discovery":
        scraper.probe_discovery(letters=_parse_letters(args.letters), max_pages=args.max_pages)
    elif command == "diagnose":
        report = scraper.diagnose(
            letter=args.letter,
            detail_url=args.detail_url,
            rss_feed_urls=_parse_csv_list(args.rss_feeds),
        )
        raise SystemExit(0 if report["ready_for_diff"] else 1)
    elif command == "browser-test":
        report = scraper.browser_test(letter="%23" if args.letter == "#" else args.letter.lower())
        raise SystemExit(0 if report["ready_for_diff"] else 1)
    elif command == "flaresolverr-test":
        report = scraper.flaresolverr_test(letter="%23" if args.letter == "#" else args.letter.lower())
        raise SystemExit(0 if report["ready_for_diff"] else 1)
    elif command == "browser-smoke":
        raise SystemExit(0 if scraper.browser_smoke() else 1)
    else:
        parser.error(f"Commande inconnue: {command}")


if __name__ == "__main__":
    main()
