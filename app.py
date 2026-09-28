"""Extraction Runner Web Server & Backend API.

Dual-Engine Extraction Suite:
1. Quizard Extractor (Playwright-based test & syllabus extraction)
2. Ray Book Extractor (PW & Streamfiles XOR decryption + Watermark removal)
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import quizard_extractor as QUIZARD
import ray_book_extractor as RBE
import allen_cropper_job as ACJ
import uuid

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
WORK_DIR = HERE / "workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

QUIZARD_OUTPUT_DIR = WORK_DIR / "quizard_outputs"
QUIZARD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

RAY_BOOK_OUTPUT_DIR = WORK_DIR / "ray_book_outputs"
RAY_BOOK_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLEN_INPUTS_DIR = WORK_DIR / "allen_cropper_inputs"
ALLEN_INPUTS_DIR.mkdir(parents=True, exist_ok=True)

ALLEN_OUTPUTS_DIR = WORK_DIR / "allen_cropper_outputs"
ALLEN_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# Quizard extraction state
active_quizard_job: Optional[QUIZARD.QuizardJob] = None
quizard_lock = threading.Lock()

# Ray Book extraction state
active_ray_book_job: Optional[RBE.RayBookJob] = None
ray_book_lock = threading.Lock()

# Allen Cropper extraction state
active_allen_job: Optional[ACJ.AllenCropperJob] = None
allen_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Keep server log concise
        pass

    def send_json(self, data: Any, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def serve_file(self, filepath: Path, forced_type: Optional[str] = None, download_name: Optional[str] = None):
        if not filepath.is_file():
            self.send_error(404, f"File Not Found: {filepath.name}")
            return
        ctype = forced_type or mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if download_name:
                ascii_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', download_name)
                encoded_name = urllib.parse.quote(download_name)
                self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")

    def do_GET(self):
        global active_quizard_job, active_ray_book_job, active_allen_job
        parsed = urlparse(self.path)
        path = parsed.path

        # UI Pages & Static Assets
        if path in ("/", "/index.html"):
            self.serve_file(STATIC_DIR / "index.html", "text/html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self.serve_file(STATIC_DIR / rel)

        # -------------------------------------------------------------
        # Quizard Extractor APIs
        # -------------------------------------------------------------
        elif path == "/api/quizard/status":
            with quizard_lock:
                if not active_quizard_job:
                    self.send_json({"state": "idle", "message": "No Quizard job running", "logs": []})
                    return
                qs = parse_qs(parsed.query)
                since_idx = int(qs.get("since", ["0"])[0])
                res = {
                    "id": active_quizard_job.id,
                    "state": active_quizard_job.state,
                    "message": active_quizard_job.message,
                    "done": active_quizard_job.done,
                    "total": active_quizard_job.total,
                    "active_batch": active_quizard_job.active_batch,
                    "active_test": active_quizard_job.active_test,
                    "logs": active_quizard_job.logs[since_idx:],
                    "log_total": len(active_quizard_job.logs),
                    "summary": active_quizard_job.summary,
                    "test_details": getattr(active_quizard_job, "test_details", []),
                    "text_format_count": getattr(active_quizard_job, "text_format_count", 0),
                    "image_only_count": getattr(active_quizard_job, "image_only_count", 0),
                    "skipped_brain_count": len(getattr(active_quizard_job, "skipped_brain_list", [])),
                    "skipped_brain_list": getattr(active_quizard_job, "skipped_brain_list", []),
                    "zip_files": active_quizard_job.zip_files,
                    "json_files": active_quizard_job.json_files,
                    "failed_tracker": active_quizard_job.failed_tracker,
                    "duplicate_tracker": getattr(active_quizard_job, "duplicate_tracker", {}),
                    "duplicate_list": getattr(active_quizard_job, "duplicate_list", []),
                    "skipped_tracker": getattr(active_quizard_job, "skipped_tracker", {}),
                    "skipped_list": getattr(active_quizard_job, "skipped_list", []),
                }
                self.send_json(res)

        elif path == "/api/quizard/brain-stats":
            self.send_json(QUIZARD.brain.get_stats())

        elif path == "/api/quizard/download":
            qs = parse_qs(parsed.query)
            rel_file = qs.get("file", [""])[0]
            if not rel_file:
                self.send_error(400, "Missing file parameter")
                return
            safe_path = (QUIZARD_OUTPUT_DIR / rel_file).resolve()
            if not str(safe_path).startswith(str(QUIZARD_OUTPUT_DIR.resolve())):
                self.send_error(403, "Access Denied")
                return
            if not safe_path.is_file():
                self.send_error(404, "File Not Found")
                return
            ctype = "application/zip" if safe_path.suffix.lower() == ".zip" else "application/json"
            self.serve_file(safe_path, forced_type=ctype, download_name=safe_path.name)

        elif path == "/api/quizard/files":
            zips = []
            for z in QUIZARD_OUTPUT_DIR.glob("*.zip"):
                zips.append({
                    "filename": z.name,
                    "size_bytes": z.stat().st_size,
                    "modified": z.stat().st_mtime
                })
            self.send_json({"zips": sorted(zips, key=lambda x: x["modified"], reverse=True)})

        # -------------------------------------------------------------
        # Ray Book Extractor APIs
        # -------------------------------------------------------------
        elif path == "/api/ray-book/status":
            with ray_book_lock:
                if not active_ray_book_job:
                    self.send_json({"state": "idle", "message": "No Ray Book job running", "logs": []})
                    return
                qs = parse_qs(parsed.query)
                since_idx = int(qs.get("since", ["0"])[0])
                res = {
                    "id": active_ray_book_job.id,
                    "mode": getattr(active_ray_book_job, "mode", "urls"),
                    "state": active_ray_book_job.state,
                    "message": active_ray_book_job.message,
                    "done": active_ray_book_job.done,
                    "total": active_ray_book_job.total,
                    "active_book": active_ray_book_job.active_book,
                    "active_chapter": getattr(active_ray_book_job, "active_chapter", ""),
                    "active_page": active_ray_book_job.active_page,
                    "current_phase": active_ray_book_job.current_phase,
                    "logs": active_ray_book_job.logs[since_idx:],
                    "log_total": len(active_ray_book_job.logs),
                    "extracted_files": active_ray_book_job.extracted_files,
                    "book_zips": getattr(active_ray_book_job, "book_zips", []),
                    "part_zips": getattr(active_ray_book_job, "part_zips", []),
                    "zip_file_info": active_ray_book_job.master_zip or active_ray_book_job.zip_file_info,
                    "master_zip": getattr(active_ray_book_job, "master_zip", None),
                    "errors": active_ray_book_job.errors
                }
                self.send_json(res)

        elif path == "/api/ray-book/download":
            qs = parse_qs(parsed.query)
            rel_file = qs.get("file", [""])[0]
            if not rel_file:
                self.send_error(400, "Missing file parameter")
                return
            safe_path = (RAY_BOOK_OUTPUT_DIR / rel_file).resolve()
            if not str(safe_path).startswith(str(RAY_BOOK_OUTPUT_DIR.resolve())):
                self.send_error(403, "Access Denied")
                return
            if not safe_path.is_file():
                self.send_error(404, "File Not Found")
                return
            ctype = "application/zip" if safe_path.suffix.lower() == ".zip" else "application/pdf"
            self.serve_file(safe_path, forced_type=ctype, download_name=safe_path.name)

        elif path == "/api/ray-book/files":
            files = []
            for p in RAY_BOOK_OUTPUT_DIR.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".pdf", ".zip"):
                    rel = p.relative_to(RAY_BOOK_OUTPUT_DIR).as_posix()
                    files.append({
                        "filename": rel,
                        "display_name": p.name,
                        "is_zip": p.suffix.lower() == ".zip",
                        "size_bytes": p.stat().st_size,
                        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                        "modified": p.stat().st_mtime
                    })
            self.send_json({"files": sorted(files, key=lambda x: x["modified"], reverse=True)})

        # -------------------------------------------------------------
        # Allen Cropper APIs
        # -------------------------------------------------------------
        elif path == "/api/cropper/status":
            with allen_lock:
                if not active_allen_job:
                    self.send_json({"state": "idle", "message": "No Allen Cropper job running", "logs": []})
                    return
                qs = parse_qs(parsed.query)
                since_idx = int(qs.get("since", ["0"])[0])
                res = {
                    "id": active_allen_job.id,
                    "state": active_allen_job.state,
                    "message": active_allen_job.message,
                    "done": active_allen_job.done,
                    "total": active_allen_job.total,
                    "prefix": active_allen_job.prefix,
                    "generate_zip": active_allen_job.generate_zip,
                    "upload_cloudinary": active_allen_job.upload_cloudinary,
                    "logs": active_allen_job.logs[since_idx:],
                    "log_total": len(active_allen_job.logs),
                    "zip_files": active_allen_job.zip_files,
                    "json_files": active_allen_job.json_files,
                    "questions_preview": getattr(active_allen_job, "questions_preview", []),
                    "uploaded_question_count": getattr(active_allen_job, "uploaded_question_count", 0),
                }
                self.send_json(res)

        elif path == "/api/cropper/download":
            qs = parse_qs(parsed.query)
            rel_file = qs.get("file", [""])[0]
            if not rel_file:
                self.send_error(400, "Missing file parameter")
                return
            safe_path = (ALLEN_OUTPUTS_DIR / rel_file).resolve()
            if not str(safe_path).startswith(str(ALLEN_OUTPUTS_DIR.resolve())):
                self.send_error(403, "Access Denied")
                return
            if not safe_path.is_file():
                self.send_error(404, "File Not Found")
                return
            ctype = "application/zip" if safe_path.suffix.lower() == ".zip" else "application/json"
            self.serve_file(safe_path, forced_type=ctype, download_name=safe_path.name)

        elif path == "/api/cropper/files":
            files = []
            for p in ALLEN_OUTPUTS_DIR.rglob("*"):
                if "cropped_files" in p.parts:
                    continue
                if p.is_file() and p.suffix.lower() in (".zip", ".json"):
                    rel = p.relative_to(ALLEN_OUTPUTS_DIR).as_posix()
                    files.append({
                        "filename": rel,
                        "display_name": p.name,
                        "is_zip": p.suffix.lower() == ".zip",
                        "size_bytes": p.stat().st_size,
                        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                        "modified": p.stat().st_mtime
                    })
            self.send_json({"files": sorted(files, key=lambda x: x["modified"], reverse=True)})

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        global active_quizard_job, active_ray_book_job, active_allen_job
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # -------------------------------------------------------------
        # Quizard Extractor POST Endpoints
        # -------------------------------------------------------------
        if path == "/api/quizard/start":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            cat = payload.get("category", "").strip()
            if not cat:
                self.send_json({"error": "Category name is required"}, code=400)
                return
            batches = payload.get("batches", "all").strip()
            base_url = payload.get("base_url", QUIZARD.DEFAULT_BASE_URL).strip()
            use_syllabus = bool(payload.get("use_api_syllabus", True))
            headless = bool(payload.get("headless", True))
            download_mode = payload.get("download_mode", "new_only").strip().lower()

            with quizard_lock:
                if active_quizard_job and active_quizard_job.state in ("queued", "running"):
                    self.send_json({"id": active_quizard_job.id, "state": active_quizard_job.state, "message": "A Quizard job is already running"})
                    return

                active_quizard_job = QUIZARD.QuizardJob(
                    category=cat,
                    batches=batches,
                    base_url=base_url,
                    use_api_syllabus=use_syllabus,
                    headless=headless,
                    download_mode=download_mode,
                    output_dir=QUIZARD_OUTPUT_DIR
                )
                t = threading.Thread(target=active_quizard_job.run, daemon=True)
                t.start()
                self.send_json({"id": active_quizard_job.id, "state": "queued"})

        elif path == "/api/quizard/rescan-brain":
            stats = QUIZARD.brain.scan_library()
            self.send_json({"success": True, "stats": stats})

        elif path == "/api/quizard/stop":
            with quizard_lock:
                if active_quizard_job and active_quizard_job.state in ("queued", "running"):
                    active_quizard_job.stop()
                    self.send_json({"success": True, "message": "Stop signal sent"})
                else:
                    self.send_json({"success": False, "message": "No active job running"})

        elif path == "/api/quizard/open-folder":
            try:
                folder = str(QUIZARD_OUTPUT_DIR.resolve())
                if os.name == 'nt':
                    os.startfile(folder)
                self.send_json({"success": True, "path": folder})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        # -------------------------------------------------------------
        # Ray Book Extractor POST Endpoints
        # -------------------------------------------------------------
        elif path == "/api/ray-book/analyse-book":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as e:
                self.send_json({"error": f"Invalid JSON payload: {e}"}, code=400)
                return
            book_id = payload.get("book_id", "").strip()
            token = payload.get("token", "").strip() or RBE.DEFAULT_TOKEN
            if not book_id:
                self.send_json({"error": "Book ID or URL is required."}, code=400)
                return
            result = RBE.analyse_book(book_id, token)
            self.send_json(result)

        elif path == "/api/ray-book/analyse-cohort":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as e:
                self.send_json({"error": f"Invalid JSON payload: {e}"}, code=400)
                return
            cohort = payload.get("cohort", "").strip()
            token = payload.get("token", "").strip() or RBE.DEFAULT_TOKEN
            if not cohort:
                self.send_json({"error": "Cohort name is required."}, code=400)
                return
            result = RBE.analyse_cohort(cohort, token)
            self.send_json(result)

        elif path == "/api/ray-book/start":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as e:
                self.send_json({"error": f"Invalid JSON payload: {e}"}, code=400)
                return

            mode = payload.get("mode", "urls").strip()
            books = payload.get("books", [])
            items = payload.get("items", [])

            if not books and not items:
                self.send_json({"error": "Please provide books or items to extract."}, code=400)
                return

            cohort_name = payload.get("cohort_name", "").strip()
            default_token = payload.get("token", "").strip() or RBE.DEFAULT_TOKEN
            zip_name = payload.get("zip_name", "Extracted_Books.zip").strip()
            remove_watermarks = bool(payload.get("remove_watermarks", True))
            period_h = int(payload.get("period_h", 500))
            c_wm = float(payload.get("c_wm", 138.0))

            with ray_book_lock:
                if active_ray_book_job and active_ray_book_job.state in ("queued", "running"):
                    self.send_json({"id": active_ray_book_job.id, "state": active_ray_book_job.state, "message": "A Ray Book job is already running"})
                    return

                active_ray_book_job = RBE.RayBookJob(
                    mode=mode,
                    books=books,
                    items=items,
                    cohort_name=cohort_name,
                    default_token=default_token,
                    zip_name=zip_name,
                    remove_watermarks=remove_watermarks,
                    period_h=period_h,
                    c_wm=c_wm,
                    output_dir=RAY_BOOK_OUTPUT_DIR
                )
                t = threading.Thread(target=active_ray_book_job.run, daemon=True)
                t.start()
                self.send_json({"id": active_ray_book_job.id, "state": "queued"})

        elif path == "/api/ray-book/stop":
            with ray_book_lock:
                if active_ray_book_job and active_ray_book_job.state in ("queued", "running"):
                    active_ray_book_job.stop()
                    self.send_json({"success": True, "message": "Stop signal sent to Ray Book extractor"})
                else:
                    self.send_json({"success": False, "message": "No active job running"})

        elif path == "/api/ray-book/open-folder":
            try:
                folder = str(RAY_BOOK_OUTPUT_DIR.resolve())
                if os.name == 'nt':
                    os.startfile(folder)
                self.send_json({"success": True, "path": folder})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        # -------------------------------------------------------------
        # Allen Cropper POST Endpoints
        # -------------------------------------------------------------
        elif path == "/api/cropper/upload-pdf":
            raw_filename = self.headers.get("X-Filename", "")
            if raw_filename:
                filename = unquote(raw_filename)
            else:
                filename = f"upload_{int(time.time())}.pdf"

            clean_name = re.sub(r'[\\/*?:"<>|]', '_', filename).strip()
            if not clean_name.lower().endswith(".pdf"):
                clean_name += ".pdf"

            file_id = f"pdf_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            saved_filename = f"{file_id}_{clean_name}"
            dest_path = ALLEN_INPUTS_DIR / saved_filename

            try:
                with open(dest_path, "wb") as f:
                    f.write(body)

                file_size = dest_path.stat().st_size
                self.send_json({
                    "success": True,
                    "file_id": file_id,
                    "filename": clean_name,
                    "file_path": str(dest_path.resolve()),
                    "size_bytes": file_size,
                    "size_mb": round(file_size / (1024 * 1024), 2)
                })
            except Exception as e:
                self.send_json({"error": f"Failed to save uploaded PDF: {e}"}, code=500)

        elif path == "/api/cropper/start":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as e:
                self.send_json({"error": f"Invalid JSON payload: {e}"}, code=400)
                return

            input_type = payload.get("input_type", "file").strip()
            pdf_path = payload.get("file_path", "").strip()
            pdf_url = payload.get("pdf_url", "").strip()
            prefix = payload.get("prefix", "").strip()
            generate_zip = bool(payload.get("generate_zip", True))
            upload_cloudinary = bool(payload.get("upload_cloudinary", False))
            cloudinary_config = payload.get("cloudinary_config", None)

            if input_type == "url" and not pdf_url:
                self.send_json({"error": "Please provide a valid PDF download link."}, code=400)
                return
            elif input_type == "file" and not pdf_path:
                self.send_json({"error": "Please select or upload a PDF file first."}, code=400)
                return

            with allen_lock:
                if active_allen_job and active_allen_job.state in ("queued", "running"):
                    self.send_json({
                        "id": active_allen_job.id,
                        "state": active_allen_job.state,
                        "message": "An Allen Cropper job is already running."
                    })
                    return

                active_allen_job = ACJ.AllenCropperJob(
                    input_type=input_type,
                    pdf_path=pdf_path,
                    pdf_url=pdf_url,
                    prefix=prefix,
                    generate_zip=generate_zip,
                    upload_cloudinary=upload_cloudinary,
                    cloudinary_config=cloudinary_config,
                    output_dir=ALLEN_OUTPUTS_DIR
                )
                t = threading.Thread(target=active_allen_job.run, daemon=True)
                t.start()
                self.send_json({"id": active_allen_job.id, "state": "queued"})

        elif path == "/api/cropper/stop":
            with allen_lock:
                if active_allen_job and active_allen_job.state in ("queued", "running"):
                    active_allen_job.stop()
                    self.send_json({"success": True, "message": "Stop signal sent to Allen Cropper"})
                else:
                    self.send_json({"success": False, "message": "No active job running"})

        elif path == "/api/cropper/open-folder":
            try:
                folder = str(ALLEN_OUTPUTS_DIR.resolve())
                if os.name == 'nt':
                    os.startfile(folder)
                self.send_json({"success": True, "path": folder})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        else:
            self.send_error(404, "Not Found")


def run(port: int = 8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n=======================================================")
    print(f"🚀 Extraction Suite Web Server active on port {port}")
    print(f"👉 Local access:  http://localhost:{port}")
    print(f"👉 Quizard outputs:      {QUIZARD_OUTPUT_DIR.resolve()}")
    print(f"👉 Ray Book outputs:     {RAY_BOOK_OUTPUT_DIR.resolve()}")
    print(f"👉 Allen Cropper outputs: {ALLEN_OUTPUTS_DIR.resolve()}")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extraction Suite Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    run(args.port)
