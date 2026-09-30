"""Ray Book Extraction & Watermark Removal Engine.

Dual Mode & Batch Processing for PW & Streamfiles encrypted books:
- Direct URLs Mode: Unlimited batch download with custom names.
- Book ID Mode: Inspect full book chapters, interactive chapter selection, ZIP per book.
- Cohort Mode: Inspect cohorts (e.g. 12th JEE, Dropper JEE, 12th NEET, Dropper NEET),
  interactive book & chapter selection, ZIP per book, ZIP per 5 books, and ZIP of whole cohort.
- XOR decryption on-the-fly and nanmedian watermark template removal.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import datetime
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import fitz  # PyMuPDF
import numpy as np
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

XOR_KEY = [90, 165, 195, 60, 15, 240, 150, 105]

# Pre-configured Bearer token so user does not need to enter it manually
DEFAULT_TOKEN = "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3ODg4NjM1MTQsImV4cCI6MTc4OTQ2ODMxNC4zNjQsImRhdGEiOnsiX2lkIjoiNjQxNTFiNjM3NmIwODEwMTBjMzk4NWVhIiwidXNlcm5hbWUiOiI4MjI0ODE1Njk3IiwiZmlyc3ROYW1lIjoiQWJoYXkgQW5hbmQiLCJsYXN0TmFtZSI6IkFuYW5kIiwib3JnYW5pemF0aW9uIjp7Il9pZCI6IjVlYjM5M2VlOTVmYWI3NDY4YTc5ZDE4OSIsIndlYnNpdGUiOiJwaHlzaWNzd2FsbGFoLmNvbSIsIm5hbWUiOiJQaHlzaWNzd2FsbGFoIn0sImVtYWlsIjoicnVuaWFuYW5kMDM4QGdtYWlsLmNvbSIsInJvbGVzIjpbIjViMjdiZDk2NTg0MmY5NTBhNzc4YzZlZiJdLCJjb3VudHJ5R3JvdXAiOiJJTiIsInR5cGUiOiJVU0VSIn0sImp0aSI6IllKRUdlVkYxUktXSmNvYXlzNXdMVXdfNjQxNTFiNjM3NmIwODEwMTBjMzk4NWVhIn0.D4XthP4Kx8gOv-goCbeHG-4dUpTObl3p4nzMtDKWOzc"


def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames across OSes."""
    clean = re.sub(r'[\\/*?:"<>|]', '_', (name or '').strip())
    clean = re.sub(r'\s+', '_', clean)
    clean = clean.strip(' ._')
    return clean or "extracted_book"


def extract_asset_ref(url_or_ref: str) -> str:
    """Extracts asset_ref parameter from URL or returns literal if already raw ref."""
    raw = (url_or_ref or "").strip()
    if not raw:
        return ""
    if "asset_ref=" in raw:
        try:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            if "asset_ref" in qs and qs["asset_ref"]:
                return qs["asset_ref"][0].strip()
        except Exception:
            pass
        match = re.search(r'asset_ref=([a-zA-Z0-9%+\/=_-]+)', raw)
        if match:
            return urllib.parse.unquote(match.group(1).strip())
    return raw


def extract_book_id(query_or_url: str) -> str:
    """Extracts book ID from book_chapters query or raw string."""
    raw = (query_or_url or "").strip()
    if not raw:
        return ""
    if "book_chapters=" in raw:
        try:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            if "book_chapters" in qs and qs["book_chapters"]:
                return qs["book_chapters"][0].strip()
        except Exception:
            pass
        match = re.search(r'book_chapters=([a-zA-Z0-9_\-]+)', raw)
        if match:
            return match.group(1).strip()
    return raw


def normalize_cohort(input_str: str) -> str:
    """Normalizes friendly cohort inputs into API cohort identifiers.
    e.g. '12th jee' -> '12_JEE', 'dropper neet' -> 'DROPPER_NEET', '11th jee' -> '11_JEE', '10th boards' -> '10_BOARDS'
    """
    s = (input_str or "").strip()
    if not s:
        return ""
    clean = re.sub(r'[\s\-_]+', '_', s).upper()
    clean = re.sub(r'^12TH_', '12_', clean)
    clean = re.sub(r'^11TH_', '11_', clean)
    clean = re.sub(r'^10TH_', '10_', clean)
    clean = re.sub(r'^9TH_', '9_', clean)
    clean = re.sub(r'^CLASS_12_', '12_', clean)
    clean = re.sub(r'^CLASS_11_', '11_', clean)
    clean = re.sub(r'^CLASS_10_', '10_', clean)
    clean = re.sub(r'^CLASS_9_', '9_', clean)
    return clean


def get_api_headers(token: str = "") -> Dict[str, str]:
    """Returns headers required for streamfiles books API."""
    act_token = (token or "").strip() or DEFAULT_TOKEN
    if act_token and not act_token.lower().startswith("bearer "):
        act_token = f"Bearer {act_token}"
    return {
        "Authorization": act_token,
        "x-authorization": act_token,
        "client-id": "5eb393ee95fab7468a79d189",
        "client-type": "WEB",
        "randomid": "f4fbd160-4407-4886-b48f-1a9463e0acde",
        "x-sdk-version": "0.0.20-alpha-1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    }


def fetch_streamfiles_api(url: str, token: str = "", timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Attempts fast requests.get, falling back to Playwright in-browser fetch if blocked (e.g. HTTP 403/Cloudflare on GitHub Actions)."""
    headers = get_api_headers(token)
    act_token = headers.get("Authorization", "")

    # 1. Fast requests.get attempt
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return res.json()
        elif res.status_code not in (403, 520, 521, 522, 523, 524):
            try:
                return res.json()
            except Exception:
                return None
    except Exception:
        pass

    # 2. In-browser fetch fallback with Playwright
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
            try:
                page.goto("https://books.streamfiles.eu.org/viewer.php?asset_ref=init", wait_until="domcontentloaded", timeout=12000)
            except Exception:
                pass
            data = page.evaluate('''
                async ([targetUrl, bearerToken]) => {
                    try {
                        const res = await fetch(targetUrl, {
                            headers: {
                                'Authorization': bearerToken,
                                'x-authorization': bearerToken,
                                'client-id': '5eb393ee95fab7468a79d189',
                                'client-type': 'WEB'
                            }
                        });
                        return await res.json();
                    } catch(e) {
                        return null;
                    }
                }
            ''', [url, act_token])
            browser.close()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def analyse_book(book_id_or_url: str, token: str = "") -> Dict[str, Any]:
    """Fetches and analyses all chapters for a given Book ID."""
    book_id = extract_book_id(book_id_or_url)
    if not book_id:
        return {"success": False, "error": "Invalid book ID or URL provided."}

    api_url = f"https://books.streamfiles.eu.org/api/books.php?book_chapters={book_id}"
    json_data = fetch_streamfiles_api(api_url, token=token, timeout=20)
    if not json_data:
        return {"success": False, "error": f"Failed to connect to API or retrieve data for Book ID: {book_id}. (If streamfiles.eu.org is down, please try again when origin recovers or paste direct chapter URLs)."}

    chapters_raw = []
    book_title = "Unknown Book"

    if json_data.get("data") and isinstance(json_data["data"], dict):
        chapters_raw = json_data["data"].get("chapterDetails", []) or []
        book_title = json_data["data"].get("title") or book_title
    elif json_data.get("chapterDetails"):
        chapters_raw = json_data.get("chapterDetails", []) or []
        book_title = json_data.get("title") or book_title

    if not chapters_raw:
        return {"success": False, "error": f"No chapter data found for Book ID: {book_id}"}

    base_viewer_url = "https://books.streamfiles.eu.org/viewer.php?asset_ref="
    chapters = []
    for chap in chapters_raw:
        eq = chap.get("encryptedQuery")
        if not eq:
            continue
        c_num = chap.get("displayChapterNumber")
        if c_num is None:
            c_num = len(chapters) + 1
        c_title = (chap.get("title") or f"Chapter {c_num}").strip()
        chapters.append({
            "id": chap.get("_id") or str(len(chapters) + 1),
            "chapter_number": c_num,
            "title": c_title,
            "asset_ref": eq,
            "viewer_url": f"{base_viewer_url}{eq}"
        })

    return {
        "success": True,
        "book": {
            "id": book_id,
            "title": book_title.strip(),
            "total_chapters": len(chapters),
            "chapters": chapters
        }
    }


def analyse_cohort(cohort_input: str, token: str = "") -> Dict[str, Any]:
    """Fetches explore pages for a cohort, extracts unique books, and inspects their chapters."""
    if not cohort_input or not cohort_input.strip():
        return {"success": False, "error": "Cohort name cannot be empty."}

    headers = get_api_headers(token)
    raw_str = cohort_input.strip()
    norm = normalize_cohort(raw_str)
    raw_upper = raw_str.upper().replace(" ", "_")
    candidates = []
    for cand in [
        norm,
        raw_str,
        raw_upper,
        norm.replace("_BOARDS", "_BOARD"),
        norm.replace("_BOARDS", "_CBSE"),
        raw_str.lower(),
        raw_str.lower().replace(" ", "_"),
    ]:
        if cand and cand not in candidates:
            candidates.append(cand)

    cohort_json = None
    used_cohort = norm

    for cand in candidates:
        api_url = f"https://books.streamfiles.eu.org/api/books.php?cohort={urllib.parse.quote(cand)}"
        data = fetch_streamfiles_api(api_url, token=token, timeout=15)
        if data and data.get("data") and data["data"].get("explorePages"):
            cohort_json = data
            used_cohort = cand
            break

    if not cohort_json:
        return {
            "success": False,
            "error": f"No books returned from API for cohort '{cohort_input}'. Popular verified cohorts include: '12th jee', 'dropper jee', '11th jee', '12th neet', 'dropper neet', '11th neet'. You can also use Mode 1 (Book ID) to extract any specific book directly."
        }

    explore_pages = cohort_json.get("data", {}).get("explorePages", [])
    extracted_books = []

    for page in explore_pages:
        items = page.get("explorePage", {}).get("books", []) or []
        for item in items:
            if item.get("books") and isinstance(item["books"], list):
                for b in item["books"]:
                    if b.get("bookId"):
                        extracted_books.append({"id": b["bookId"], "title": b.get("title") or "Book"})
            elif item.get("bookId") or item.get("_id"):
                extracted_books.append({"id": item.get("bookId") or item.get("_id"), "title": item.get("title") or "Book"})

    seen = set()
    unique_books = []
    for b in extracted_books:
        bid = b["id"]
        if bid and bid not in seen:
            seen.add(bid)
            unique_books.append(b)

    if not unique_books:
        return {"success": False, "error": f"No books discovered in cohort '{cohort_input}'."}

    # Fetch chapters in parallel for all books
    def _fetch_single_book_chaps(b):
        bid = b["id"]
        try:
            url = f"https://books.streamfiles.eu.org/api/books.php?book_chapters={bid}"
            cd = fetch_streamfiles_api(url, token=token, timeout=12)
            if cd:
                data = cd.get("data") if isinstance(cd.get("data"), dict) else cd
                c_title = data.get("title") or b["title"]
                c_list = data.get("chapterDetails", []) or []
                chapters = []
                base_viewer = "https://books.streamfiles.eu.org/viewer.php?asset_ref="
                for c in c_list:
                    eq = c.get("encryptedQuery")
                    if not eq:
                        continue
                    num = c.get("displayChapterNumber")
                    if num is None:
                        num = len(chapters) + 1
                    t = (c.get("title") or f"Chapter {num}").strip()
                    chapters.append({
                        "id": c.get("_id") or str(len(chapters) + 1),
                        "chapter_number": num,
                        "title": t,
                        "asset_ref": eq,
                        "viewer_url": f"{base_viewer}{eq}"
                    })
                return {
                    "id": bid,
                    "title": c_title.strip(),
                    "chapter_count": len(chapters),
                    "chapters": chapters
                }
        except Exception:
            pass
        return {
            "id": bid,
            "title": b["title"].strip(),
            "chapter_count": 0,
            "chapters": []
        }

    with ThreadPoolExecutor(max_workers=10) as executor:
        books_data = list(executor.map(_fetch_single_book_chaps, unique_books))

    total_chapters = sum(b["chapter_count"] for b in books_data)
    display_name = used_cohort.replace("_", " ").title()

    return {
        "success": True,
        "cohort": used_cohort,
        "cohort_display": display_name,
        "total_books": len(books_data),
        "total_chapters": total_chapters,
        "books": books_data
    }


def clean_page_image(arr: np.ndarray, alpha_3d: np.ndarray, c_wm: float, dilated_wm_mask: np.ndarray) -> np.ndarray:
    """Inverts repeating semi-transparent watermark on color document images."""
    restored = (arr.astype(float) - alpha_3d * c_wm) / (1.0 - alpha_3d)
    restored = np.clip(np.round(restored), 0, 255).astype(np.uint8)

    orig_min = np.min(arr, axis=-1).astype(int)
    orig_max = np.max(arr, axis=-1).astype(int)
    orig_is_neutral_paper = (orig_min >= 210) & ((orig_max - orig_min) <= 4)

    rest_min = np.min(restored, axis=-1).astype(int)
    rest_is_bright = (rest_min >= 242)

    is_true_white_paper = orig_is_neutral_paper & rest_is_bright
    restored[dilated_wm_mask & is_true_white_paper] = 255

    return restored


def extract_master_template(doc: fitz.Document, period_h: int = 500) -> np.ndarray:
    """Synthesizes master watermark template using nan-median across candidate white pages."""
    candidate_pages = []
    if len(doc) == 0:
        raise ValueError("PDF document has 0 pages.")

    first_images = doc[0].get_images()
    if not first_images:
        raise ValueError("No raster images found in the first page.")

    first_base = doc.extract_image(first_images[0][0])
    full_h = first_base['height']
    full_w = first_base['width']

    for i in range(len(doc)):
        images = doc[i].get_images()
        if not images:
            continue
        xref = images[0][0]
        base = doc.extract_image(xref)
        arr = cv2.imdecode(np.frombuffer(base['image'], np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None or arr.shape[0] != full_h or arr.shape[1] != full_w:
            continue

        if np.mean(arr > 215) > 0.82:
            candidate_pages.append(arr)

        if len(candidate_pages) >= 35:
            break

    if len(candidate_pages) >= 10:
        stack = np.stack(candidate_pages, axis=0).astype(float)
        stack[stack < 215] = np.nan
        master_template = np.nanmedian(stack, axis=0)
        master_template = np.nan_to_num(master_template, nan=255.0)
    else:
        candidate_tiles = []
        for arr in candidate_pages:
            for offset in range(0, arr.shape[0] - period_h + 1, period_h):
                candidate_tiles.append(arr[offset:offset + period_h, :full_w])
        if not candidate_tiles:
            raise ValueError("Could not find sufficient white pages to extract watermark template.")

        tile_stack = np.stack(candidate_tiles, axis=0).astype(float)
        tile_stack[tile_stack < 215] = np.nan
        tile = np.nanmedian(tile_stack, axis=0)
        tile = np.nan_to_num(tile, nan=255.0)

        master_template = np.full((full_h, full_w), 255.0, dtype=np.float32)
        for offset in range(0, full_h, period_h):
            chunk_h = min(period_h, full_h - offset)
            master_template[offset:offset + chunk_h, :full_w] = tile[:chunk_h, :full_w]

    return master_template


class RayBookJob:
    """Thread-safe batch processor for Ray Book extraction and watermark removal.

    Supports:
    - Mode 'urls': Unlimited direct URLs / asset_refs.
    - Mode 'book': Single book extraction with selected chapters -> ZIP per book.
    - Mode 'cohort': Cohort extraction with selected books & chapters ->
      ZIP per book, ZIP per 5 books, and Master ZIP of whole cohort!
    """

    def __init__(
        self,
        mode: str = "urls",
        books: Optional[List[Dict[str, Any]]] = None,
        items: Optional[List[Dict[str, str]]] = None,
        cohort_name: str = "",
        default_token: str = "",
        zip_name: str = "Extracted_Books.zip",
        remove_watermarks: bool = True,
        period_h: int = 500,
        c_wm: float = 138.0,
        output_dir: Optional[Path] = None,
    ):
        self.id = uuid.uuid4().hex[:10]
        self.mode = mode  # "urls", "book", "cohort"
        self.cohort_name = sanitize_filename(cohort_name or "Cohort")

        self.default_token = (default_token or "").strip() or DEFAULT_TOKEN
        if self.default_token and not self.default_token.lower().startswith("bearer "):
            self.default_token = f"Bearer {self.default_token}"

        clean_zip = sanitize_filename(zip_name or "Extracted_Books")
        if not clean_zip.lower().endswith(".zip"):
            clean_zip += ".zip"
        self.zip_name = clean_zip

        self.remove_watermarks = remove_watermarks
        self.period_h = period_h
        self.c_wm = c_wm

        self.output_dir = output_dir or (Path(__file__).parent / "workspace" / "ray_book_outputs")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.output_dir / f"_temp_{self.id}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Normalize structured books representation
        if books:
            self.books = books
        elif items:
            # Mode "urls": Direct items list (unlimited)
            self.books = [{
                "id": "direct_batch",
                "title": sanitize_filename(clean_zip.replace(".zip", "") or "Batch_Extraction"),
                "chapters": [
                    {
                        "id": str(i + 1),
                        "chapter_number": i + 1,
                        "title": it.get("custom_name") or f"Item_{i + 1}",
                        "asset_ref": extract_asset_ref(it.get("url") or ""),
                        "custom_name": sanitize_filename(it.get("custom_name") or f"Book_{i + 1}"),
                        "token": it.get("token") or ""
                    }
                    for i, it in enumerate(items)
                    if extract_asset_ref(it.get("url") or "")
                ]
            }]
        else:
            self.books = []

        # Count total chapters to extract
        total_chaps = 0
        for b in self.books:
            total_chaps += len(b.get("chapters", []))
        self.total = max(total_chaps, 1)
        self.done = 0

        self.state = "queued"  # queued, running, finished, stopped, error
        self.message = "Job queued..."
        self.active_book = ""
        self.active_chapter = ""
        self.active_page = 0
        self.current_phase = ""  # downloading, watermarking, archiving
        self.stop_requested = False

        self.logs: List[Dict[str, str]] = []
        self.extracted_files: List[Dict[str, Any]] = []
        self.book_zips: List[Dict[str, Any]] = []
        self.part_zips: List[Dict[str, Any]] = []
        self.master_zip: Optional[Dict[str, Any]] = None
        self.zip_file_info: Optional[Dict[str, Any]] = None
        self.errors: List[str] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

        self._pw = None
        self._pw_browser = None
        self._pw_context = None

    def _get_playwright_context(self):
        if not self._pw_context:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._pw_browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            self._pw_context = self._pw_browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            )
        return self._pw_context

    def _close_playwright(self):
        if self._pw_browser:
            try:
                self._pw_browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._pw_browser = None
        self._pw_context = None

    def log(self, msg: str, level: str = "info"):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.logs.append({"time": now_str, "msg": msg, "level": level})
        print(f"[{now_str}] [{level.upper()}] {msg}", flush=True)

    def stop(self):
        self.stop_requested = True
        self.state = "stopped"
        self.message = "Stop requested by user..."
        self.log("Stopping extraction job...", "warning")

    def run(self):
        self.state = "running"
        self.start_time = time.time()
        num_books = len(self.books)
        self.log(f"Starting Ray Book extraction: {num_books} book(s), {self.total} total chapter(s)...", "info")

        success_chapters = 0
        books_completed = []  # stores paths and book info

        try:
            for b_idx, book in enumerate(self.books):
                if self.stop_requested:
                    break

                raw_b_title = book.get("title") or f"Book_{b_idx + 1}"
                clean_b_title = sanitize_filename(raw_b_title)
                chapters = book.get("chapters", [])

                if not chapters:
                    continue

                self.active_book = clean_b_title
                self.log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "info")
                self.log(f"📖 [{b_idx + 1}/{num_books}] Processing Book: {clean_b_title} ({len(chapters)} chapter(s))", "info")

                # Dedicated folder for this book's chapter PDFs
                book_dir = self.output_dir / clean_b_title
                book_dir.mkdir(parents=True, exist_ok=True)

                book_extracted_pdfs = []

                for c_idx, chap in enumerate(chapters):
                    if self.stop_requested:
                        break

                    asset_ref = extract_asset_ref(chap.get("asset_ref") or chap.get("url") or "")
                    chap_num = chap.get("chapter_number", c_idx + 1)
                    raw_c_title = chap.get("title") or f"Chapter {chap_num}"
                    clean_c_title = sanitize_filename(raw_c_title)

                    # Standardized chapter filename
                    try:
                        num_pad = f"{int(chap_num):02d}"
                    except Exception:
                        num_pad = str(chap_num)
                    pdf_filename = f"{num_pad} - {clean_c_title}.pdf"

                    self.active_chapter = f"{clean_b_title} / {pdf_filename}"
                    self.current_phase = "downloading"
                    self.message = f"[{self.done + 1}/{self.total}] Downloading: {pdf_filename}"
                    self.log(f"   ⬇ [Chap {c_idx + 1}/{len(chapters)}] Fetching: {pdf_filename}...", "info")

                    token = chap.get("token") or self.default_token
                    if token and not token.lower().startswith("bearer "):
                        token = f"Bearer {token}"

                    if not asset_ref:
                        self.log(f"   ❌ Missing asset_ref for '{pdf_filename}'. Skipping.", "error")
                        self.errors.append(f"{clean_b_title} - {pdf_filename}: Missing asset_ref")
                        self.done += 1
                        continue

                    raw_pdf_path = self.temp_dir / f"raw_{self.id}_{b_idx}_{c_idx}.pdf"
                    final_pdf_path = book_dir / pdf_filename

                    try:
                        pages_fetched = self._download_and_decrypt(asset_ref, token, raw_pdf_path)

                        if self.stop_requested:
                            break

                        if pages_fetched == 0 or not raw_pdf_path.exists():
                            raise ValueError(f"0 pages fetched for '{pdf_filename}'. Token expired or URL invalid.")

                        if self.remove_watermarks:
                            self.current_phase = "watermarking"
                            self.message = f"[{self.done + 1}/{self.total}] Cleaning watermarks: {pdf_filename}"
                            self.log(f"   ✨ Inverting watermarks on {pages_fetched} page(s)...", "info")
                            self._remove_watermarks(raw_pdf_path, final_pdf_path)
                        else:
                            shutil.copyfile(raw_pdf_path, final_pdf_path)

                        if raw_pdf_path.exists():
                            try:
                                os.remove(raw_pdf_path)
                            except Exception:
                                pass

                        file_size = final_pdf_path.stat().st_size
                        size_mb = file_size / (1024 * 1024)
                        self.log(f"   ✅ Saved: {pdf_filename} ({pages_fetched} pages, {size_mb:.2f} MB)", "success")

                        rel_path = f"{clean_b_title}/{pdf_filename}"
                        file_record = {
                            "book_name": clean_b_title,
                            "filename": rel_path,
                            "display_name": pdf_filename,
                            "pages": pages_fetched,
                            "size_bytes": file_size,
                            "size_mb": round(size_mb, 2),
                            "path": str(final_pdf_path.resolve())
                        }
                        self.extracted_files.append(file_record)
                        book_extracted_pdfs.append(file_record)
                        success_chapters += 1

                    except Exception as e:
                        err_msg = str(e)
                        self.log(f"   ❌ Failed '{pdf_filename}': {err_msg}", "error")
                        self.errors.append(f"{clean_b_title} / {pdf_filename}: {err_msg}")
                    finally:
                        self.done += 1

                # -------------------------------------------------------------
                # Tier 1 ZIP: Create ZIP per Book
                # -------------------------------------------------------------
                if book_extracted_pdfs:
                    self.current_phase = "archiving"
                    book_zip_name = f"{clean_b_title}.zip"
                    book_zip_path = self.output_dir / book_zip_name
                    try:
                        with zipfile.ZipFile(book_zip_path, "w", zipfile.ZIP_DEFLATED) as bzf:
                            for ef in book_extracted_pdfs:
                                p = Path(ef["path"])
                                if p.exists():
                                    bzf.write(p, arcname=p.name)

                        b_size = book_zip_path.stat().st_size
                        b_mb = b_size / (1024 * 1024)
                        book_zip_info = {
                            "book_title": clean_b_title,
                            "filename": book_zip_name,
                            "size_bytes": b_size,
                            "size_mb": round(b_mb, 2),
                            "file_count": len(book_extracted_pdfs),
                            "path": str(book_zip_path.resolve())
                        }
                        self.book_zips.append(book_zip_info)
                        self.log(f"📦 [ZIP Per Book] Created: {book_zip_name} ({len(book_extracted_pdfs)} chapters, {b_mb:.2f} MB)", "success")
                        books_completed.append({
                            "title": clean_b_title,
                            "dir": book_dir,
                            "zip_info": book_zip_info
                        })
                    except Exception as e:
                        self.log(f"Failed to create book ZIP for '{clean_b_title}': {e}", "warning")

            # -------------------------------------------------------------
            # Tier 2 ZIP: For Cohort Mode -> Create ZIP per 5 Books
            # -------------------------------------------------------------
            if self.mode == "cohort" and books_completed and not self.stop_requested:
                self.current_phase = "archiving"
                self.log(f"📦 Building batch ZIPs per 5 books...", "info")
                chunk_size = 5
                for part_idx, i in enumerate(range(0, len(books_completed), chunk_size), start=1):
                    chunk_books = books_completed[i:i + chunk_size]
                    start_num = i + 1
                    end_num = i + len(chunk_books)
                    part_zip_name = f"{self.cohort_name}_Part_{part_idx:02d}_(Books_{start_num:02d}-{end_num:02d}).zip"
                    part_zip_path = self.output_dir / part_zip_name
                    try:
                        with zipfile.ZipFile(part_zip_path, "w", zipfile.ZIP_DEFLATED) as pzf:
                            for b in chunk_books:
                                b_dir = b["dir"]
                                if b_dir.exists():
                                    for pdf_file in b_dir.glob("*.pdf"):
                                        pzf.write(pdf_file, arcname=f"{b['title']}/{pdf_file.name}")

                        p_size = part_zip_path.stat().st_size
                        p_mb = p_size / (1024 * 1024)
                        part_info = {
                            "part_num": part_idx,
                            "range": f"Books {start_num}-{end_num}",
                            "filename": part_zip_name,
                            "size_bytes": p_size,
                            "size_mb": round(p_mb, 2),
                            "book_count": len(chunk_books),
                            "path": str(part_zip_path.resolve())
                        }
                        self.part_zips.append(part_info)
                        self.log(f"📦 [ZIP Per 5 Books] Created Part {part_idx}: {part_zip_name} ({len(chunk_books)} books, {p_mb:.2f} MB)", "success")
                    except Exception as e:
                        self.log(f"Failed to create part ZIP {part_zip_name}: {e}", "warning")

            # -------------------------------------------------------------
            # Tier 3 ZIP: Consolidated Master ZIP of Whole Batch / Cohort
            # -------------------------------------------------------------
            if books_completed and not self.stop_requested:
                self.current_phase = "archiving"
                master_name = self.zip_name
                if self.mode == "cohort":
                    master_name = f"{self.cohort_name}_Complete_All_Books.zip"
                elif self.mode == "book" and len(books_completed) == 1:
                    master_name = f"{books_completed[0]['title']}_Full_Book.zip"

                if not master_name.lower().endswith(".zip"):
                    master_name += ".zip"

                master_zip_path = self.output_dir / master_name
                self.log(f"📦 [ZIP of Whole] Generating master consolidated ZIP: {master_name}...", "info")
                try:
                    with zipfile.ZipFile(master_zip_path, "w", zipfile.ZIP_DEFLATED) as mzf:
                        for b in books_completed:
                            b_dir = b["dir"]
                            if b_dir.exists():
                                for pdf_file in b_dir.glob("*.pdf"):
                                    mzf.write(pdf_file, arcname=f"{b['title']}/{pdf_file.name}")

                    m_size = master_zip_path.stat().st_size
                    m_mb = m_size / (1024 * 1024)
                    self.master_zip = {
                        "filename": master_name,
                        "size_bytes": m_size,
                        "size_mb": round(m_mb, 2),
                        "book_count": len(books_completed),
                        "chapter_count": success_chapters,
                        "path": str(master_zip_path.resolve())
                    }
                    self.zip_file_info = self.master_zip
                    self.log(f"🎉 Master ZIP Archive created: {master_name} ({len(books_completed)} books, {m_mb:.2f} MB)", "success")
                except Exception as e:
                    self.log(f"Warning: Failed to create Master ZIP: {e}", "warning")

            # Clean up temp working folder
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)

            self.end_time = time.time()
            elapsed = self.end_time - self.start_time

            if self.stop_requested:
                self.state = "stopped"
                self.message = f"Stopped after {self.done} chapters ({success_chapters} succeeded, took {elapsed:.1f}s)"
                self.log(self.message, "warning")
            elif success_chapters == 0 and self.total > 0:
                self.state = "error"
                self.message = f"All {self.total} chapter(s) failed to extract."
                self.log(self.message, "error")
            else:
                self.state = "finished"
                self.message = f"Completed extraction of {success_chapters} chapter(s) across {len(books_completed)} book(s) in {elapsed:.1f}s"
                self.log(f"🏆 {self.message}", "success")

        finally:
            self._close_playwright()

    def _download_and_decrypt(self, asset_ref: str, token: str, output_raw: Path) -> int:
        """Downloads encrypted pages sequentially, reverses XOR cipher, and merges into raw PDF.
        Features automatic fallback to Chromium browser engine if cloud/datacenter IP gets HTTP 403.
        """
        session = requests.Session()
        headers = {
            "Authorization": token,
            "x-authorization": token,
            "Referer": f"https://books.streamfiles.eu.org/viewer.php?asset_ref={asset_ref}",
            "Origin": "https://books.streamfiles.eu.org",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        }
        session.headers.update(headers)

        try:
            session.get(f"https://books.streamfiles.eu.org/viewer.php?asset_ref={asset_ref}", timeout=10)
        except Exception:
            pass

        merged_pdf = fitz.open()
        page_num = 1
        use_browser_engine = False
        pw_page = None

        try:
            while not self.stop_requested:
                url = f"https://books.streamfiles.eu.org/api/page.php?asset_ref={asset_ref}&page={page_num}"
                data = None

                # 1. Try fast HTTP session first
                if not use_browser_engine:
                    try:
                        response = session.get(url, timeout=20)
                        if response.status_code == 200:
                            data = response.json()
                        elif response.status_code == 403:
                            self.log(f"      [P.{page_num}] Received HTTP 403 on direct connection. Launching Chromium engine...", "warning")
                            use_browser_engine = True
                        else:
                            if page_num == 1:
                                raise ValueError(f"Server returned HTTP {response.status_code}: {response.text[:200]}")
                            break
                    except ValueError:
                        raise
                    except Exception as e:
                        self.log(f"      [P.{page_num}] Direct connection error: {e}. Switching to Chromium engine...", "warning")
                        use_browser_engine = True

                # 2. Chromium engine fallback
                if use_browser_engine:
                    try:
                        if pw_page is None:
                            self.log(f"      [P.{page_num}] Initializing browser session on viewer page...", "info")
                            pw_ctx = self._get_playwright_context()
                            pw_page = pw_ctx.new_page()
                            viewer_url = f"https://books.streamfiles.eu.org/viewer.php?asset_ref={asset_ref}"
                            pw_page.goto(viewer_url, wait_until="domcontentloaded", timeout=45000)
                            time.sleep(1.0)

                        data = pw_page.evaluate('''
                            async ([targetUrl, bearerToken]) => {
                                const res = await fetch(targetUrl, {
                                    headers: { 'Authorization': bearerToken, 'x-authorization': bearerToken }
                                });
                                return await res.json();
                            }
                        ''', [url, token])
                    except Exception as e:
                        if page_num == 1:
                            raise ValueError(f"Browser engine fetch error: {e}")
                        break

                if not data:
                    break

                b64_payload = data.get('data')
                if not b64_payload:
                    break

                # Reverse the XOR Cipher
                encrypted_bytes = base64.b64decode(b64_payload)
                decrypted_bytes = bytearray(len(encrypted_bytes))
                for i in range(len(encrypted_bytes)):
                    decrypted_bytes[i] = encrypted_bytes[i] ^ XOR_KEY[i % 8] ^ (i % 256)

                page_doc = fitz.open(stream=decrypted_bytes, filetype="pdf")
                merged_pdf.insert_pdf(page_doc)
                page_doc.close()

                self.active_page = page_num
                if page_num % 10 == 0 or page_num == 1:
                    self.log(f"      [{self.active_chapter}] Decrypted page {page_num}...", "info")

                page_num += 1
                time.sleep(0.12)

        finally:
            if pw_page is not None:
                try:
                    pw_page.close()
                except Exception:
                    pass

        total_pages = page_num - 1
        if total_pages > 0:
            merged_pdf.save(str(output_raw.resolve()))
        merged_pdf.close()
        return total_pages

    def _remove_watermarks(self, input_pdf: Path, output_pdf: Path):
        """Processes document and eliminates repeating watermarks using master template nanmedian."""
        doc = fitz.open(str(input_pdf.resolve()))
        total_pages = len(doc)
        if total_pages == 0:
            doc.close()
            return

        try:
            full_template = extract_master_template(doc, period_h=self.period_h)
            full_template[full_template < 215.0] = 255.0
            full_template[full_template > 250.0] = 255.0

            alpha = np.clip((255.0 - full_template) / (255.0 - self.c_wm), 0.0, 0.35)
            alpha_3d = np.stack([alpha] * 3, axis=-1)

            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
            dilated_wm_mask = cv2.dilate((alpha > 0.003).astype(np.uint8), kernel).astype(bool)

            for idx, page in enumerate(doc):
                if self.stop_requested:
                    break
                images = page.get_images()
                if not images:
                    continue

                xref = images[0][0]
                base = doc.extract_image(xref)
                arr = cv2.imdecode(np.frombuffer(base['image'], np.uint8), cv2.IMREAD_COLOR)
                if arr is None:
                    continue

                restored = clean_page_image(arr, alpha_3d, self.c_wm, dilated_wm_mask)
                _, buf = cv2.imencode('.png', restored, [cv2.IMWRITE_PNG_COMPRESSION, 6])
                page.replace_image(xref, stream=buf.tobytes())

            doc.save(str(output_pdf.resolve()), garbage=4, deflate=True)
        finally:
            doc.close()


def standalone_cli():
    """CLI runner if executed directly."""
    import argparse
    parser = argparse.ArgumentParser(description="Ray Book Extractor & Watermark Remover")
    parser.add_argument("--url", help="Target book viewer URL or asset_ref", default="")
    parser.add_argument("--token", help="Bearer Authorization Token", default="")
    parser.add_argument("--name", help="Custom name for the book", default="Final_Extracted_Book")
    parser.add_argument("--book-id", help="Book ID to extract all chapters", default="")
    parser.add_argument("--cohort", help="Cohort name (e.g. 12th jee, dropper jee)", default="")
    parser.add_argument("--no-clean", action="store_true", help="Skip watermark removal")
    args = parser.parse_args()

    token = args.token or DEFAULT_TOKEN

    if args.cohort:
        print(f"Analysing cohort '{args.cohort}'...")
        res = analyse_cohort(args.cohort, token)
        if not res.get("success"):
            print("Error:", res.get("error"))
            return
        print(f"Found {res['total_books']} books with {res['total_chapters']} total chapters.")
        job = RayBookJob(
            mode="cohort",
            books=res["books"],
            cohort_name=res["cohort"],
            default_token=token,
            remove_watermarks=not args.no_clean
        )
        job.run()
    elif args.book_id:
        print(f"Analysing Book ID '{args.book_id}'...")
        res = analyse_book(args.book_id, token)
        if not res.get("success"):
            print("Error:", res.get("error"))
            return
        b = res["book"]
        print(f"Book: {b['title']} ({b['total_chapters']} chapters)")
        job = RayBookJob(
            mode="book",
            books=[b],
            zip_name=f"{sanitize_filename(b['title'])}.zip",
            default_token=token,
            remove_watermarks=not args.no_clean
        )
        job.run()
    else:
        url = args.url or input("Enter Target Book URL (or asset_ref): ").strip()
        name = args.name or input("Enter Custom Book Name (leave empty for default): ").strip() or "Final_Extracted_Book"
        job = RayBookJob(
            mode="urls",
            items=[{"url": url, "custom_name": name, "token": token}],
            default_token=token,
            zip_name=f"{name}.zip",
            remove_watermarks=not args.no_clean
        )
        job.run()


if __name__ == "__main__":
    standalone_cli()
