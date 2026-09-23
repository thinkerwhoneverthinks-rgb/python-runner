"""Ray Book Extraction & Watermark Removal Engine.

Batch processing for PW & Streamfiles encrypted books:
- Supports batch download of up to 30 books with custom names.
- Decrypts XOR-ciphered PDF pages on-the-fly.
- Template-based nanmedian watermark removal using PyMuPDF and OpenCV.
- Live progress streaming, log emission, individual downloads, and custom ZIP exports.
"""

from __future__ import annotations

import base64
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
    clean = re.sub(r'[\\/*?:"<>|]', '_', name.strip())
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
    """Thread-safe batch processor for Ray Book extraction and watermark removal."""

    def __init__(
        self,
        items: List[Dict[str, str]],
        default_token: str = "",
        zip_name: str = "Extracted_Books.zip",
        remove_watermarks: bool = True,
        period_h: int = 500,
        c_wm: float = 138.0,
        output_dir: Optional[Path] = None,
    ):
        self.id = uuid.uuid4().hex[:10]
        self.items = items[:30]  # capped at 30 items
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

        self.state = "queued"  # queued, running, finished, stopped, error
        self.message = "Job queued..."
        self.done = 0
        self.total = max(len(self.items), 1)
        self.active_book = ""
        self.active_page = 0
        self.current_phase = ""  # downloading, watermarking, archiving
        self.stop_requested = False

        self.logs: List[Dict[str, str]] = []
        self.extracted_files: List[Dict[str, Any]] = []
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
        self.log("Stopping batch extraction...", "warning")

    def run(self):
        self.state = "running"
        self.start_time = time.time()
        total_items = len(self.items)
        self.total = total_items
        self.log(f"Starting Ray Book batch extraction for {total_items} book(s)...", "info")

        if not self.default_token and not any(it.get("token") for it in self.items):
            self.default_token = DEFAULT_TOKEN

        success_count = 0

        try:
            for idx, item in enumerate(self.items):
                if self.stop_requested:
                    break

                url_raw = (item.get("url") or "").strip()
                raw_name = (item.get("custom_name") or "").strip()
                if not raw_name:
                    raw_name = f"Book_{idx + 1}"
                custom_name = sanitize_filename(raw_name)

                book_token = (item.get("token") or "").strip()
                if book_token and not book_token.lower().startswith("bearer "):
                    book_token = f"Bearer {book_token}"
                active_token = book_token or self.default_token or DEFAULT_TOKEN

                asset_ref = extract_asset_ref(url_raw)
                if not asset_ref:
                    self.log(f"[{idx + 1}/{total_items}] ❌ Invalid URL / asset_ref for '{custom_name}'. Skipping.", "error")
                    self.errors.append(f"'{custom_name}': Invalid URL / asset_ref")
                    self.done += 1
                    continue

                self.active_book = custom_name
                self.current_phase = "downloading"
                self.message = f"Processing ({idx + 1}/{total_items}): {custom_name}"
                self.log(f"[{idx + 1}/{total_items}] 📖 Processing: {custom_name}...", "info")

                raw_pdf_path = self.temp_dir / f"raw_{idx}_{custom_name}.pdf"
                final_pdf_path = self.output_dir / f"{custom_name}.pdf"

                try:
                    # Phase 1: Download and decrypt pages
                    self.log(f"[{idx + 1}/{total_items}] [Phase 1] Downloading & decrypting pages from server...", "info")
                    pages_fetched = self._download_and_decrypt(asset_ref, active_token, raw_pdf_path, idx, total_items)

                    if self.stop_requested:
                        break

                    if pages_fetched == 0 or not raw_pdf_path.exists():
                        raise ValueError(f"No pages fetched for '{custom_name}'. Token might be expired or URL invalid.")

                    # Phase 2: Watermark removal or direct copy
                    if self.remove_watermarks:
                        self.current_phase = "watermarking"
                        self.log(f"[{idx + 1}/{total_items}] [Phase 2] Synthesizing watermark template & cleaning pages...", "info")
                        self._remove_watermarks(raw_pdf_path, final_pdf_path, idx, total_items)
                    else:
                        self.log(f"[{idx + 1}/{total_items}] Watermark removal skipped by user option. Saving raw book.", "info")
                        shutil.copyfile(raw_pdf_path, final_pdf_path)

                    # Clean up raw temp file
                    if raw_pdf_path.exists():
                        try:
                            os.remove(raw_pdf_path)
                        except Exception:
                            pass

                    file_size = final_pdf_path.stat().st_size
                    size_mb = file_size / (1024 * 1024)
                    self.log(f"[{idx + 1}/{total_items}] ✅ Finished: {final_pdf_path.name} ({pages_fetched} pages, {size_mb:.2f} MB)", "success")

                    self.extracted_files.append({
                        "name": custom_name,
                        "filename": final_pdf_path.name,
                        "pages": pages_fetched,
                        "size_bytes": file_size,
                        "size_mb": round(size_mb, 2),
                        "status": "completed",
                        "path": str(final_pdf_path.resolve())
                    })
                    success_count += 1

                except Exception as e:
                    err_msg = str(e)
                    self.log(f"[{idx + 1}/{total_items}] ❌ Failed processing '{custom_name}': {err_msg}", "error")
                    self.errors.append(f"'{custom_name}': {err_msg}")
                finally:
                    self.done += 1

            # Phase 3: Build master ZIP archive if any books succeeded
            if success_count > 0 and not self.stop_requested:
                self.current_phase = "archiving"
                self.log(f"📦 Creating consolidated ZIP archive '{self.zip_name}' with {success_count} file(s)...", "info")
                zip_dest = self.output_dir / self.zip_name
                try:
                    with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as zf:
                        for ef in self.extracted_files:
                            p = Path(ef["path"])
                            if p.exists():
                                zf.write(p, arcname=ef["filename"])
                    zip_size = zip_dest.stat().st_size
                    zip_mb = zip_size / (1024 * 1024)
                    self.zip_file_info = {
                        "filename": self.zip_name,
                        "size_bytes": zip_size,
                        "size_mb": round(zip_mb, 2),
                        "file_count": success_count,
                        "path": str(zip_dest.resolve())
                    }
                    self.log(f"🎉 Master ZIP archive created successfully: {self.zip_name} ({zip_mb:.2f} MB)", "success")
                except Exception as e:
                    self.log(f"Warning: Failed to create ZIP archive: {e}", "warning")

            # Clean up temp folder
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)

            self.end_time = time.time()
            elapsed = self.end_time - self.start_time

            if self.stop_requested:
                self.state = "stopped"
                self.message = f"Stopped after {self.done} items ({success_count} succeeded, took {elapsed:.1f}s)"
                self.log(self.message, "warning")
            elif success_count == 0 and total_items > 0:
                self.state = "error"
                self.message = f"All {total_items} items failed to extract."
                self.log(self.message, "error")
            else:
                self.state = "finished"
                self.message = f"Successfully processed {success_count}/{total_items} book(s) in {elapsed:.1f}s"
                self.log(f"🏆 {self.message}", "success")
        finally:
            self._close_playwright()

    def _download_and_decrypt(self, asset_ref: str, token: str, output_raw: Path, book_idx: int, total_books: int) -> int:
        """Downloads encrypted pages sequentially, reverses XOR cipher, and merges into raw PDF.
        Features automatic fallback to Chromium browser engine if cloud/datacenter IP gets HTTP 403.
        """
        session = requests.Session()
        headers = {
            "Authorization": token,
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

        # Pre-seed session cookies by visiting the viewer page first
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

                # 1. Try fast HTTP session first (unless browser engine already activated)
                if not use_browser_engine:
                    try:
                        response = session.get(url, timeout=20)
                        if response.status_code == 200:
                            data = response.json()
                        elif response.status_code == 403:
                            self.log(f"   [P.{page_num}] Received HTTP 403 on direct connection (Cloudflare Datacenter filter). Launching Chromium engine...", "warning")
                            use_browser_engine = True
                        else:
                            if page_num == 1:
                                raise ValueError(f"Server returned HTTP {response.status_code}: {response.text[:200]}")
                            self.log(f"   Reached end of pages (HTTP {response.status_code} on page {page_num}). Total fetched: {page_num - 1}", "info")
                            break
                    except ValueError:
                        raise
                    except Exception as e:
                        self.log(f"   [P.{page_num}] Direct connection error: {e}. Switching to Chromium engine...", "warning")
                        use_browser_engine = True

                # 2. Seamlessly use Chromium browser page if Cloudflare blocks datacenter IP
                if use_browser_engine:
                    try:
                        if pw_page is None:
                            self.log(f"   [P.{page_num}] Initializing browser session on viewer page...", "info")
                            pw_ctx = self._get_playwright_context()
                            pw_page = pw_ctx.new_page()
                            viewer_url = f"https://books.streamfiles.eu.org/viewer.php?asset_ref={asset_ref}"
                            pw_page.goto(viewer_url, wait_until="domcontentloaded", timeout=45000)
                            time.sleep(1.0)

                        data = pw_page.evaluate('''
                            async ([targetUrl, bearerToken]) => {
                                const res = await fetch(targetUrl, {
                                    headers: { 'Authorization': bearerToken }
                                });
                                return await res.json();
                            }
                        ''', [url, token])
                    except Exception as e:
                        if page_num == 1:
                            raise ValueError(f"Browser engine fetch error: {e}")
                        self.log(f"   Reached end of pages on page {page_num}: {e}", "info")
                        break

                if not data:
                    break

                b64_payload = data.get('data')
                if not b64_payload:
                    self.log(f"   Reached end of chapter. Total pages fetched: {page_num - 1}", "info")
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
                    self.log(f"   [{self.active_book}] Fetched & decrypted page {page_num}...", "info")

                page_num += 1
                time.sleep(0.15)  # courteous delay

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

    def _remove_watermarks(self, input_pdf: Path, output_pdf: Path, book_idx: int, total_books: int):
        """Processes document and eliminates repeating watermarks using master template nanmedian."""
        doc = fitz.open(str(input_pdf.resolve()))
        total_pages = len(doc)
        if total_pages == 0:
            doc.close()
            return

        self.log(f"   Analyzing document and synthesizing watermark master template...", "info")
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

            if (idx + 1) % 15 == 0 or (idx + 1) == total_pages:
                self.log(f"   Cleaned watermarks on page {idx + 1}/{total_pages}...", "info")

        doc.save(str(output_pdf.resolve()), garbage=4, deflate=True)
        doc.close()


def standalone_cli():
    """CLI runner if executed directly."""
    import argparse
    parser = argparse.ArgumentParser(description="Ray Book Extractor & Watermark Remover")
    parser.add_argument("--url", help="Target book viewer URL or asset_ref", default="")
    parser.add_argument("--token", help="Bearer Authorization Token", default="")
    parser.add_argument("--name", help="Custom name for the book", default="Final_Extracted_Book")
    parser.add_argument("--no-clean", action="store_true", help="Skip watermark removal")
    args = parser.parse_args()

    # Fallback to interactive input if arguments are missing
    url = args.url or input("Enter Target Book URL (or asset_ref): ").strip()
    token = args.token or input("Enter Authorization Bearer Token: ").strip()
    name = args.name or input("Enter Custom Book Name (leave empty for default): ").strip() or "Final_Extracted_Book"

    job = RayBookJob(
        items=[{"url": url, "custom_name": name, "token": token}],
        default_token=token,
        zip_name=f"{name}.zip",
        remove_watermarks=not args.no_clean
    )
    job.run()


if __name__ == "__main__":
    standalone_cli()
