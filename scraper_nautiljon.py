from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.nautiljon.com"
DEFAULT_TIMEOUT = 45
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
BANNED_TYPE_KEYWORDS = ["yaoi", "yuri"]
PREFERRED_FIELDS = [
    "url_fiche", "titre", "titre_alternatif", "extraction_time",
    "titre_original", "origine", "annee_vo",
    "type_liste", "type_detail",
    "genres", "themes",
    "scenariste", "dessinateur",
    "editeur_vo", "prepublication",
    "nb_vol_vo_liste", "nb_vol_vf_liste", "nb_vol_vo_detail", "nb_vol_vf_detail",
    "date_vo_liste", "date_vf_liste", "date_vo_detail", "date_vf_detail",
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


class NautiljonScraper:
    def __init__(
        self,
        out_dir: str = ".",
        delay: float = 2.0,
        delay_min: Optional[float] = None,
        delay_max: Optional[float] = None,
    ):
        self.out_dir = out_dir
        self.delay = delay
        self.delay_min = delay_min
        self.delay_max = delay_max
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
        }

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD", "POST"),
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
        if delay > 0:
            time.sleep(delay)
        return delay

    def _ensure_dirs(self) -> Tuple[str, str, str, str]:
        exports_dir = os.path.join(self.out_dir, "exports")
        checkpoints_dir = os.path.join(self.out_dir, "checkpoints")
        letters_dir = os.path.join(self.out_dir, "letters")
        state_dir = os.path.join(self.out_dir, "state")
        for path in (exports_dir, checkpoints_dir, letters_dir, state_dir):
            os.makedirs(path, exist_ok=True)
        return exports_dir, checkpoints_dir, letters_dir, state_dir

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
        completed_at = str(data.get("completed_at", ""))
        try:
            age = datetime.now() - datetime.fromisoformat(completed_at)
        except ValueError:
            return False, data, None
        return age < timedelta(days=min_days), data, age

    def mark_success(self, mode: str, rows_count: int, export_paths: Dict[str, str]) -> None:
        payload = {
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "rows_count": rows_count,
            "export_paths": export_paths,
            "session_stats": self.session_stats,
        }
        path = self._last_success_path(mode)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, path)
        print(f"  OK marqueur de succes {mode}: {path}")

    def _write_csv(self, path: str, rows: List[Dict[str, str]]) -> None:
        keys = set()
        for row in rows:
            keys.update(row.keys())
        fieldnames = [field for field in PREFERRED_FIELDS]
        extras = sorted(key for key in keys if key not in set(PREFERRED_FIELDS))
        fieldnames.extend(extras)
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _clean_spaces(str(row.get(field, "N/A") or "N/A")) for field in fieldnames})

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

    def export_all_data(self, rows: List[Dict[str, str]], base_filename: Optional[str] = None) -> Dict[str, str]:
        exports_dir, _, _, _ = self._ensure_dirs()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = base_filename or f"nautiljon_concat_{timestamp}"
        json_path = os.path.join(exports_dir, f"{base_filename}.json")
        csv_path = os.path.join(exports_dir, f"{base_filename}.csv")
        stats_path = os.path.join(exports_dir, f"{base_filename}_stats.json")
        report_path = os.path.join(exports_dir, f"{base_filename}_rapport.txt")

        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        self._write_csv(csv_path, rows)
        with open(stats_path, "w", encoding="utf-8") as handle:
            json.dump(self.session_stats, handle, ensure_ascii=False, indent=2, default=str)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write("RAPPORT DE SCRAPING NAUTILJON\n")
            handle.write(f"Date: {_now_str()}\n")
            handle.write("=" * 50 + "\n\n")
            handle.write(f"Series totales: {len(rows)}\n")
            handle.write(f"Erreurs: {self.session_stats.get('errors', 0)}\n")
            handle.write(f"Ignorées par type: {self.session_stats.get('skipped_by_type', 0)}\n")
            handle.write(f"Duree: {self.session_stats.get('duration', 'N/A')}\n")
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
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                with open(path, "r", newline="", encoding=encoding) as handle:
                    sample = handle.read(4096)
                    handle.seek(0)
                    dialect = csv.Sniffer().sniff(sample, delimiters=";,")
                    reader = csv.DictReader(handle, dialect=dialect)
                    return [
                        {str(k): _clean_spaces(v or "N/A") for k, v in row.items() if k}
                        for row in reader
                        if row
                    ]
            except Exception:
                continue
        raise RuntimeError(f"CSV illisible: {path}")

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
        for path in sorted(csv_files):
            rows = self._read_csv_rows(path)
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
        print(f"OK import CSV termine: {imported} lignes lues, {len(all_rows)} URLs uniques")
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

    def fetch_html(self, url: str) -> str:
        response = self.session.get(_ensure_abs_url(url), timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        html = response.text
        if self._blocked_by_waf(html):
            raise RuntimeError("Nautiljon a renvoye une page de blocage/WAF.")
        return html

    def _blocked_by_waf(self, html: str) -> bool:
        text = _norm(html)
        return (
            "sorry you have been blocked" in text
            or "you are unable to access nautiljon.com" in text
            or "unable to access nautiljon.com" in text
            or "access denied" in text
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
            f"{BASE_URL}/mangas/?q={encoded}",
            f"{BASE_URL}/mangas/?search={encoded}",
            f"{BASE_URL}/mangas/?lettre={encoded}",
            f"{BASE_URL}/mangas/{encoded}.html",
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
        last_error = ""
        for url in self._listing_candidate_urls(letter, page_num):
            try:
                html = self.fetch_html(url)
                rows = self.extract_series_list_from_html(html)
                if rows:
                    return url, rows
                last_error = "aucune entree"
            except Exception as exc:
                last_error = str(exc)[:160]
                continue
        raise RuntimeError(f"Listing introuvable pour {letter} page {page_num}: {last_error}")

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

    def extract_series_detail_from_html(self, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        detail = {field: "N/A" for field in PREFERRED_FIELDS if field not in {"url_fiche", "titre", "titre_alternatif", "extraction_time"}}
        detail["_titre_fr_fallback"] = self._extract_title_fr(soup)
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

    def _fetch_full_series_data(self, series: Dict[str, str]) -> Optional[Dict[str, str]]:
        if self.is_banned_type(series.get("type_liste", "")):
            self.session_stats["skipped_by_type"] += 1
            return None
        html = self.fetch_html(series["url_fiche"])
        detail = self.extract_series_detail_from_html(html)
        probe = " ".join([series.get("type_liste", ""), detail.get("type_detail", ""), detail.get("genres", ""), detail.get("themes", "")])
        if self.is_banned_type(probe):
            self.session_stats["skipped_by_type"] += 1
            return None
        self._backfill_list_fields_from_detail(series, detail)
        detail.pop("_titre_fr_fallback", None)
        return self._normalize_row({**series, **detail})

    def _series_changed_on_list(self, existing: Dict[str, str], current: Dict[str, str]) -> bool:
        fields = [field for field in ListRow.__dataclass_fields__.keys() if field != "extraction_time"]
        return any(_clean_spaces(existing.get(field, "N/A")) != _clean_spaces(current.get(field, "N/A")) for field in fields)

    def _series_is_stale(self, existing: Dict[str, str], refresh_stale_days: Optional[int]) -> bool:
        if not refresh_stale_days or refresh_stale_days <= 0:
            return False
        extracted_at = _parse_extraction_time(existing.get("extraction_time"))
        if extracted_at is None:
            return True
        return (datetime.now() - extracted_at).days >= refresh_stale_days

    def scrape_letter_diff(
        self,
        letter: str,
        max_pages: Optional[int] = None,
        max_series: Optional[int] = None,
        refresh_stale_days: Optional[int] = None,
        drop_missing: bool = True,
        flush_every: int = 25,
    ) -> List[Dict[str, str]]:
        label = self._letter_label(letter)
        tag = self._letter_tag(letter)
        print(f"\n{'=' * 60}\nLETTRE {label} - DIFF HTTP\n{'=' * 60}")

        existing_rows = self._load_json_list(self._letter_paths(tag)[0])
        if not existing_rows:
            existing_rows = self._read_existing_csv_for_letter(tag)
        existing_by_url = {
            _ensure_abs_url(row.get("url_fiche", "")): self._normalize_row(row)
            for row in existing_rows
            if row.get("url_fiche")
        }
        print(f"  Base existante: {len(existing_by_url)} series")

        updated_rows: List[Dict[str, str]] = []
        seen_urls = set()
        page_num = 0
        empty_pages = 0
        successful_listing_pages = 0
        listing_failed = False
        since_flush = 0
        counters = {"new": 0, "changed": 0, "stale": 0, "reused": 0, "removed": 0}

        while True:
            if max_pages is not None and page_num >= max_pages:
                break
            try:
                page_url, page_series = self.fetch_listing_page(letter, page_num)
                print(f"  Page {page_num + 1}: {len(page_series)} entrees ({page_url})")
            except Exception as exc:
                empty_pages += 1
                print(f"  Page {page_num + 1} indisponible ({empty_pages}/3): {str(exc)[:160]}")
                if empty_pages >= 3:
                    listing_failed = True
                    break
                page_num += 1
                continue

            if not page_series:
                empty_pages += 1
                if empty_pages >= 3:
                    break
            else:
                successful_listing_pages += 1
                empty_pages = 0
                for series in page_series:
                    url = _ensure_abs_url(series.get("url_fiche", ""))
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    series["url_fiche"] = url
                    if self.is_banned_type(series.get("type_liste", "")):
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
                        needs_detail = True
                    elif self._series_is_stale(existing, refresh_stale_days):
                        action = "stale"
                        needs_detail = True

                    if needs_detail:
                        print(f"    MAJ {action}: {(series.get('titre') or 'N/A')[:60]}")
                        try:
                            full = self._fetch_full_series_data(series)
                            if full:
                                updated_rows.append(full)
                                counters[action] += 1
                                self._sleep_delay()
                        except Exception as exc:
                            self.session_stats["errors"] += 1
                            print(f"    Erreur detail: {str(exc)[:140]}")
                            if existing:
                                updated_rows.append(existing)
                            continue
                    else:
                        updated_rows.append(existing)
                        counters["reused"] += 1

                    since_flush += 1
                    if flush_every and since_flush >= flush_every:
                        self.save_letter_files(tag, updated_rows, partial=True)
                        since_flush = 0

                self.save_letter_files(tag, updated_rows, partial=True)
                if max_series is not None and len(updated_rows) >= max_series:
                    break

            page_num += 1
            self._sleep_delay()

        if successful_listing_pages == 0 and existing_by_url:
            self.session_stats["errors"] += 1
            existing_kept = sorted(existing_by_url.values(), key=lambda row: _norm(row.get("titre", "")))
            self.session_stats["series_by_letter"][label] = len(existing_kept)
            self.session_stats["total_series"] += len(existing_kept)
            self.session_stats["diff_by_letter"][label] = {
                "new": 0,
                "changed": 0,
                "stale": 0,
                "reused": len(existing_kept),
                "removed": 0,
                "listing_failed": True,
            }
            print(
                f"  Listing {label} inaccessible: conservation de {len(existing_kept)} series existantes, "
                "aucune suppression appliquee."
            )
            return existing_kept

        if not drop_missing and existing_by_url:
            updated_rows.extend(existing_by_url.values())
        else:
            if listing_failed:
                print("  Fin de listing incertaine: les entrees absentes sont conservees.")
                updated_rows.extend(existing_by_url.values())
            else:
                counters["removed"] = len(existing_by_url)

        updated_rows = sorted(updated_rows, key=lambda row: _norm(row.get("titre", "")))
        self.save_letter_files(tag, updated_rows, partial=False)
        self._remove_partial_files(tag)
        self.session_stats["series_by_letter"][label] = len(updated_rows)
        self.session_stats["total_series"] += len(updated_rows)
        self.session_stats["diff_by_letter"][label] = counters
        print(f"  Lettre {label}: {len(updated_rows)} series ({counters})")
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
        min_days_between_diff_exports: int = 30,
        force: bool = False,
    ) -> List[Dict[str, str]]:
        should_skip, last_success, age = self.should_skip_recent_success("diff", min_days_between_diff_exports)
        if should_skip and not force and last_success and age is not None:
            print(
                "Diff ignore: dernier diff finalise le "
                f"{last_success.get('completed_at')} ({age.days} jours)."
            )
            return []

        self.session_stats["start_time"] = datetime.now()
        letters_to_scrape = letters or self.get_all_letters()
        all_rows: List[Dict[str, str]] = []
        fatal_error = False
        try:
            for index, letter in enumerate(letters_to_scrape, start=1):
                print(f"\nProgression: {index}/{len(letters_to_scrape)}")
                rows = self.scrape_letter_diff(
                    letter,
                    max_pages=max_pages_per_letter,
                    max_series=max_series_per_letter,
                    refresh_stale_days=refresh_stale_days,
                    drop_missing=drop_missing,
                )
                all_rows.extend(rows)
                if index < len(letters_to_scrape):
                    pause = self._compute_delay(multiplier=3)
                    print(f"Pause {pause:.1f}s avant la lettre suivante")
                    time.sleep(pause)
        except Exception as exc:
            fatal_error = True
            print(f"Erreur fatale: {exc}")
        finally:
            self.session_stats["end_time"] = datetime.now()
            if self.session_stats["start_time"]:
                self.session_stats["duration"] = str(self.session_stats["end_time"] - self.session_stats["start_time"])
            combined = self.concat_letters()
            export_paths: Dict[str, str] = {}
            if combined:
                export_paths = self.export_all_data(combined, base_filename=f"nautiljon_diff_concat_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            if combined and not fatal_error and self.session_stats.get("errors", 0) == 0:
                self.mark_success("diff", len(combined), export_paths)
        return all_rows

    def probe_discovery(self, letters: Optional[List[str]] = None, max_pages: int = 1) -> None:
        for letter in letters or ["a"]:
            for page_num in range(max_pages):
                url, rows = self.fetch_listing_page(letter, page_num)
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


def _new_scraper(args: argparse.Namespace) -> NautiljonScraper:
    out_dir = args.out_dir or os.environ.get("NAUTILJON_OUT_DIR", "./output")
    os.makedirs(out_dir, exist_ok=True)
    delay = args.delay if args.delay is not None else _env_float("NAUTILJON_DELAY", 2.0)
    delay_min = args.delay_min
    delay_max = args.delay_max
    if delay_min is None and os.environ.get("NAUTILJON_DELAY_MIN", "").strip():
        delay_min = _env_float("NAUTILJON_DELAY_MIN", delay)
    if delay_max is None and os.environ.get("NAUTILJON_DELAY_MAX", "").strip():
        delay_max = _env_float("NAUTILJON_DELAY_MAX", delay)
    if (delay_min is None) != (delay_max is None):
        raise ValueError("NAUTILJON_DELAY_MIN et NAUTILJON_DELAY_MAX doivent etre definis ensemble.")
    return NautiljonScraper(out_dir=out_dir, delay=delay, delay_min=delay_min, delay_max=delay_max)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scraper Nautiljon HTTP sans Selenium.")
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
    diff.add_argument("--refresh-stale-days", type=int, default=_env_optional_int("NAUTILJON_REFRESH_STALE_DAYS"))
    diff.add_argument("--keep-missing", action="store_true", default=not _env_bool("NAUTILJON_DROP_MISSING", True))
    diff.add_argument("--min-days-between-diff-exports", type=int, default=_env_int("NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS", 30))
    diff.add_argument("--force", action="store_true", default=_env_bool("NAUTILJON_FORCE_SCRAPE", False))

    probe = sub.add_parser("probe-discovery", help="Teste la decouverte HTTP des listings sans Selenium.")
    probe.add_argument("--letters", default=os.environ.get("NAUTILJON_LETTERS", "a"))
    probe.add_argument("--max-pages", type=int, default=1)

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
        scraper.scrape_all_letters_diff(
            letters=_parse_letters(args.letters),
            max_pages_per_letter=args.max_pages_per_letter,
            max_series_per_letter=args.max_series_per_letter,
            refresh_stale_days=args.refresh_stale_days,
            drop_missing=not args.keep_missing,
            min_days_between_diff_exports=args.min_days_between_diff_exports,
            force=args.force,
        )
    elif command == "probe-discovery":
        scraper.probe_discovery(letters=_parse_letters(args.letters), max_pages=args.max_pages)
    else:
        parser.error(f"Commande inconnue: {command}")


if __name__ == "__main__":
    main()
