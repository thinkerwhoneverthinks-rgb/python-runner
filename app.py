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

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
WORK_DIR = HERE / "workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

QUIZARD_OUTPUT_DIR = WORK_DIR / "quizard_outputs"
QUIZARD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

RAY_BOOK_OUTPUT_DIR = WORK_DIR / "ray_book_outputs"
RAY_BOOK_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Quizard extraction state
active_quizard_job: Optional[QUIZARD.QuizardJob] = None
quizard_lock = threading.Lock()

# Ray Book extraction state
active_ray_book_job: Optional[RBE.RayBookJob] = None
ray_book_lock = threading.Lock()


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
        global active_quizard_job, active_ray_book_job
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
                    "zip_files": active_quizard_job.zip_files,
                    "json_files": active_quizard_job.json_files,
                    "failed_tracker": active_quizard_job.failed_tracker,
                    "duplicate_tracker": getattr(active_quizard_job, "duplicate_tracker", {}),
                    "duplicate_list": getattr(active_quizard_job, "duplicate_list", []),
                    "skipped_tracker": getattr(active_quizard_job, "skipped_tracker", {}),
                    "skipped_list": getattr(active_quizard_job, "skipped_list", []),
                }
                self.send_json(res)

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
                    "state": active_ray_book_job.state,
                    "message": active_ray_book_job.message,
                    "done": active_ray_book_job.done,
                    "total": active_ray_book_job.total,
                    "active_book": active_ray_book_job.active_book,
                    "active_page": active_ray_book_job.active_page,
                    "current_phase": active_ray_book_job.current_phase,
                    "logs": active_ray_book_job.logs[since_idx:],
                    "log_total": len(active_ray_book_job.logs),
                    "extracted_files": active_ray_book_job.extracted_files,
                    "zip_file_info": active_ray_book_job.zip_file_info,
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
            for p in RAY_BOOK_OUTPUT_DIR.iterdir():
                if p.is_file() and p.suffix.lower() in (".pdf", ".zip"):
                    files.append({
                        "filename": p.name,
                        "is_zip": p.suffix.lower() == ".zip",
                        "size_bytes": p.stat().st_size,
                        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                        "modified": p.stat().st_mtime
                    })
            self.send_json({"files": sorted(files, key=lambda x: x["modified"], reverse=True)})

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        global active_quizard_job, active_ray_book_job
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
                    output_dir=QUIZARD_OUTPUT_DIR
                )
                t = threading.Thread(target=active_quizard_job.run, daemon=True)
                t.start()
                self.send_json({"id": active_quizard_job.id, "state": "queued"})

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
        elif path == "/api/ray-book/start":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as e:
                self.send_json({"error": f"Invalid JSON payload: {e}"}, code=400)
                return

            items = payload.get("items", [])
            if not items or not isinstance(items, list):
                self.send_json({"error": "Items list is required (at least 1 book URL)."}, code=400)
                return

            if len(items) > 30:
                self.send_json({"error": "Maximum 30 books can be processed in a single batch."}, code=400)
                return

            default_token = payload.get("token", "").strip()
            zip_name = payload.get("zip_name", "Extracted_Books.zip").strip()
            remove_watermarks = bool(payload.get("remove_watermarks", True))
            period_h = int(payload.get("period_h", 500))
            c_wm = float(payload.get("c_wm", 138.0))

            with ray_book_lock:
                if active_ray_book_job and active_ray_book_job.state in ("queued", "running"):
                    self.send_json({"id": active_ray_book_job.id, "state": active_ray_book_job.state, "message": "A Ray Book job is already running"})
                    return

                active_ray_book_job = RBE.RayBookJob(
                    items=items,
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

        else:
            self.send_error(404, "Not Found")


def run(port: int = 8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n=======================================================")
    print(f"🚀 Extraction Suite Web Server active on port {port}")
    print(f"👉 Local access:  http://localhost:{port}")
    print(f"👉 Quizard outputs:   {QUIZARD_OUTPUT_DIR.resolve()}")
    print(f"👉 Ray Book outputs:  {RAY_BOOK_OUTPUT_DIR.resolve()}")
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
