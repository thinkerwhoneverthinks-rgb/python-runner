"""Allen Hybrid Parser Web Server & Backend API.

Endpoints:
- POST /api/upload: Upload PDF, set Gemini API Key and chunk size.
- GET  /api/status: Poll job progress and retrieve parsed questions.
- POST /api/cloudinary-upload: Upload crops to Cloudinary with local WebP pre-optimization.
- POST /api/export-json: Export cleaned website schema JSON with custom prefix.
- GET  /api/data: Retrieve active dataset.
- GET  /studio: Open the interactive Review Studio.
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import uuid

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import clean_exporter as CLEAN_EXP
import cloudinary_service as CLOUD_SRV
import pipeline as PIPE
import quizard_extractor as QUIZARD

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
WORK_DIR = HERE / "workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)
QUIZARD_OUTPUT_DIR = WORK_DIR / "quizard_outputs"
QUIZARD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Global active project state
active_job: Optional[Job] = None
active_data: Dict[str, Any] = {"metadata": {}, "questions": []}
active_crops_dir: Path = WORK_DIR / "crops"
state_lock = threading.Lock()

# Quizard extraction state
active_quizard_job: Optional[QUIZARD.QuizardJob] = None
quizard_lock = threading.Lock()


class Job:
    def __init__(
        self,
        pdf_bytes: bytes,
        filename: str,
        work_dir: Path,
        api_key: Optional[str] = None,
        chunk_size: int = 10,
        model_name: str = "deepseek",
        custom_prompt: str = "",
        prompts: Optional[List[str]] = None,
        ai_order: Optional[List[str]] = None,
        answer_key_mode: str = "last",
        answer_key_pages: str = ""
    ):
        self.id = uuid.uuid4().hex[:10]
        self.dir = work_dir / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pdf_path = self.dir / filename
        self.pdf_path.write_bytes(pdf_bytes)
        self.crops_dir = self.dir / "crops"
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.chunk_size = chunk_size
        self.model_name = model_name or "deepseek"
        self.custom_prompt = custom_prompt
        self.prompts = prompts or [custom_prompt]
        self.ai_order = ai_order or ["deepseek", "qwen", "perplexity"]
        self.answer_key_mode = answer_key_mode or "last"
        self.answer_key_pages = answer_key_pages or ""
        self.state = "queued"
        self.message = "Queued for processing..."
        self.done = 0
        self.total = 4
        self.error: Optional[str] = None
        self.result_data: Optional[Dict[str, Any]] = None

    def progress(self, i: int, n: int, msg: str):
        self.done, self.total, self.message = i, max(n, 1), msg

    def run(self):
        global active_data, active_crops_dir
        self.state = "running"
        try:
            res = PIPE.process_pdf_pipeline(
                self.pdf_path,
                self.dir,
                prompts=self.prompts,
                ai_order=self.ai_order,
                api_key=self.api_key,
                chunk_size=self.chunk_size,
                model_name=self.model_name,
                custom_prompt=self.custom_prompt,
                answer_key_mode=self.answer_key_mode,
                answer_key_pages=self.answer_key_pages,
                progress_callback=self.progress
            )
            self.result_data = res
            with state_lock:
                active_data = res
                active_crops_dir = self.crops_dir
            self.state = "finished"
            self.message = "Processing complete!"
        except Exception as e:
            self.state = "error"
            self.error = str(e) + "\n" + traceback.format_exc()
            self.message = f"Error: {e}"


def parse_multipart_upload(body: bytes, content_type: str) -> Tuple[Optional[str], Optional[bytes], Optional[str], int, str, str, List[str], List[str], str, str]:
    """Extracts upload parameters, including prompts array (1..10), ai_order, and answer_key options."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return None, None, None, 10, "deepseek", "", [], ["deepseek", "qwen", "perplexity"], "last", ""
    boundary = ("--" + (m.group(1) or m.group(2)).strip()).encode()

    filename = None
    pdf_bytes = None
    api_key = None
    chunk_size = 10
    model_name = "deepseek"
    custom_prompt = ""
    prompt_parts = [""] * 10
    ai_order = ["deepseek", "qwen", "perplexity"]
    answer_key_mode = "last"
    answer_key_pages = ""

    for part in body.split(boundary):
        if not part.strip(b"\r\n-") or b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        head_s = head.decode("utf-8", "replace")
        data_clean = data.rstrip(b"\r\n")

        if 'name="pdf"' in head_s or 'name="file"' in head_s:
            fn_m = re.search(r'filename="([^"]+)"', head_s)
            if fn_m:
                filename = os.path.basename(fn_m.group(1))
            pdf_bytes = data_clean
        elif 'name="apiKey"' in head_s or 'name="api_key"' in head_s:
            api_key = data_clean.decode("utf-8", "replace").strip()
        elif 'name="chunkSize"' in head_s or 'name="chunk_size"' in head_s:
            try:
                chunk_size = int(data_clean.decode("utf-8", "replace").strip())
            except ValueError:
                chunk_size = 10
        elif 'name="model"' in head_s or 'name="modelName"' in head_s or 'name="model_name"' in head_s:
            m_val = data_clean.decode("utf-8", "replace").strip()
            if m_val:
                model_name = m_val
        elif 'name="customPrompt"' in head_s or 'name="custom_prompt"' in head_s:
            custom_prompt = data_clean.decode("utf-8", "replace").strip()
        elif 'name="answerKeyMode"' in head_s or 'name="answer_key_mode"' in head_s:
            answer_key_mode = data_clean.decode("utf-8", "replace").strip()
        elif 'name="answerKeyPages"' in head_s or 'name="answer_key_pages"' in head_s:
            answer_key_pages = data_clean.decode("utf-8", "replace").strip()
        elif 'name="aiOrder"' in head_s or 'name="ai_order"' in head_s:
            raw_order = data_clean.decode("utf-8", "replace").strip()
            if raw_order:
                try:
                    parsed_order = json.loads(raw_order)
                    if isinstance(parsed_order, list):
                        ai_order = [str(x).lower().strip() for x in parsed_order if x]
                except Exception:
                    ai_order = [x.lower().strip() for x in raw_order.split(",") if x.strip()]
        else:
            for p_idx in range(1, 11):
                if f'name="prompt{p_idx}"' in head_s:
                    prompt_parts[p_idx - 1] = data_clean.decode("utf-8", "replace").strip()
                    break

    active_prompts = [p for p in prompt_parts if p]
    if not active_prompts and custom_prompt:
        active_prompts = [custom_prompt]

    return filename, pdf_bytes, api_key, chunk_size, model_name, custom_prompt, active_prompts, ai_order, answer_key_mode, answer_key_pages



class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Concise logging
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

    def do_GET(self):
        global active_job, active_quizard_job
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.serve_file(STATIC_DIR / "index.html", "text/html")
        elif path in ("/studio", "/studio.html"):
            self.serve_file(STATIC_DIR / "studio.html", "text/html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self.serve_file(STATIC_DIR / rel)
        elif path.startswith("/crops/"):
            rel = path[len("/crops/"):]
            with state_lock:
                crop_file = active_crops_dir / rel
            self.serve_file(crop_file)
        elif path == "/api/status":
            if not active_job:
                self.send_json({"state": "idle", "message": "No job running"})
                return
            res = {
                "id": active_job.id,
                "state": active_job.state,
                "message": active_job.message,
                "done": active_job.done,
                "total": active_job.total,
                "error": active_job.error,
            }
            if active_job.state == "finished" and active_job.result_data:
                res["data"] = active_job.result_data
            self.send_json(res)
        elif path == "/api/data":
            with state_lock:
                self.send_json(active_data)
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
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self):
        global active_job, active_quizard_job
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if path == "/api/upload":
            ct = self.headers.get("Content-Type", "")
            fn, pdf_b, api_key, chunk_size, model_name, custom_prompt, active_prompts, ai_order, ak_mode, ak_pages = parse_multipart_upload(body, ct)
            if not fn or not pdf_b:
                self.send_json({"error": "No PDF file provided"}, code=400)
                return

            with state_lock:
                if active_job and active_job.state in ("queued", "running"):
                    self.send_json({"id": active_job.id, "state": active_job.state, "message": "A job is already running"})
                    return

                active_job = Job(
                    pdf_b,
                    fn,
                    WORK_DIR,
                    api_key=api_key,
                    chunk_size=chunk_size,
                    model_name=model_name,
                    custom_prompt=custom_prompt,
                    prompts=active_prompts,
                    ai_order=ai_order,
                    answer_key_mode=ak_mode,
                    answer_key_pages=ak_pages
                )
                t = threading.Thread(target=active_job.run, daemon=True)
                t.start()
                self.send_json({"id": active_job.id, "state": "queued"})

        elif path == "/api/cloudinary-upload":
            try:
                payload = json.loads(body.decode("utf-8"))
                credentials = payload.get("credentials", {})
                crops_to_upload = payload.get("crops", [])

                def prog(i, n, msg):
                    pass

                url_map = CLOUD_SRV.upload_images_to_cloudinary(credentials, crops_to_upload, prog)
                self.send_json({"success": True, "url_map": url_map})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        elif path == "/api/import-json":
            try:
                ct = self.headers.get("Content-Type", "")
                raw_text = ""
                doc_title = "Manual JSON Import"
                if "multipart/form-data" in ct:
                    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ct)
                    if m:
                        boundary = ("--" + (m.group(1) or m.group(2)).strip()).encode()
                        for part in body.split(boundary):
                            if not part.strip(b"\r\n-") or b"\r\n\r\n" not in part:
                                continue
                            head, data = part.split(b"\r\n\r\n", 1)
                            head_s = head.decode("utf-8", "replace")
                            if 'filename="' in head_s:
                                fn_m = re.search(r'filename="([^"]+)"', head_s)
                                if fn_m:
                                    doc_title = Path(fn_m.group(1)).stem
                                raw_text = data.rstrip(b"\r\n").decode("utf-8", "replace")
                                break
                            elif 'name="jsonText"' in head_s or 'name="json"' in head_s:
                                raw_text = data.rstrip(b"\r\n").decode("utf-8", "replace")
                else:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                        if isinstance(payload, dict) and "json_text" in payload:
                            raw_text = payload["json_text"]
                            doc_title = payload.get("title", doc_title)
                        elif isinstance(payload, (list, dict)):
                            raw_text = json.dumps(payload)
                    except Exception:
                        raw_text = body.decode("utf-8", "replace")

                # Clean markdown fences if any
                clean_t = raw_text.strip()
                if clean_t.startswith("```"):
                    clean_t = re.sub(r"^```(?:json)?\s*", "", clean_t)
                    clean_t = re.sub(r"\s*```$", "", clean_t)
                
                raw_json = json.loads(clean_t.strip())
                formatted_data = PIPE.format_raw_questions_for_studio(raw_json, doc_title=doc_title)
                
                with state_lock:
                    active_data = formatted_data

                self.send_json({
                    "success": True, 
                    "count": len(formatted_data.get("questions", [])),
                    "data": formatted_data
                })
            except Exception as e:
                self.send_json({"error": f"Failed to import JSON: {e}"}, code=400)

        elif path == "/api/export-json":
            try:
                payload = json.loads(body.decode("utf-8"))
                questions = payload.get("questions", [])
                id_prefix = payload.get("id_prefix", "")
                cloudinary_urls = payload.get("cloudinary_urls", {})
                test_metadata = payload.get("test_metadata", {})
                
                export_data = CLEAN_EXP.build_website_questions_json(
                    questions,
                    id_prefix=id_prefix,
                    cloudinary_urls=cloudinary_urls,
                    test_metadata=test_metadata
                )
                self.send_json({"success": True, "json_data": export_data})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        elif path == "/api/update-question":
            try:
                payload = json.loads(body.decode("utf-8"))
                qid = payload.get("id")
                with state_lock:
                    for q in active_data.get("questions", []):
                        if q.get("id") == qid or str(q.get("num")) == str(qid):
                            q.update(payload.get("updates", {}))
                            break
                self.send_json({"success": True})
            except Exception as e:
                self.send_json({"error": str(e)}, code=500)

        elif path == "/api/quizard/start":
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

        elif path == "/api/quizard/load-to-studio":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                rel_path = payload.get("rel_path", "")
                json_path = (QUIZARD_OUTPUT_DIR / rel_path).resolve()
                if not json_path.is_file():
                    self.send_json({"error": "File not found"}, code=404)
                    return
                raw_data = json.loads(json_path.read_text(encoding="utf-8"))
                flat_questions = []
                q_seq = 1
                for sec in raw_data.get("sections", []):
                    sec_name = sec.get("name", "General")
                    for q in sec.get("questions", []):
                        q_id = q.get("id", f"q{q_seq}")
                        opts = q.get("options", ["A", "B", "C", "D"])
                        corr = q.get("correct", 0)
                        img_url = q.get("image_url", "")
                        q_type = q.get("type", "MCQ")
                        flat_questions.append({
                            "id": q_id,
                            "sequence": q_seq,
                            "num": q_seq,
                            "tag": f"q_{q_seq}",
                            "subject": sec_name,
                            "topic": raw_data.get("name", "Quizard Test"),
                            "exercise_key": "quizard_batch",
                            "exercise_name": sec_name,
                            "subtopic": q_type,
                            "prompt": f"[{sec_name}] Question {q_seq} ({q_type})",
                            "options": opts if isinstance(opts, list) else ["A", "B", "C", "D"],
                            "correct_index": corr if isinstance(corr, int) else 0,
                            "solution": "",
                            "smiles": None,
                            "has_diagram": bool(img_url),
                            "mode": "crop" if img_url else "text",
                            "image_filename": "",
                            "crop_path": "",
                            "image_data_uri": img_url,
                            "cloudinary_url": img_url,
                            "type": q_type
                        })
                        q_seq += 1

                formatted_data = {
                    "metadata": {
                        "title": raw_data.get("name", "Quizard Test"),
                        "id": raw_data.get("id", ""),
                        "description": raw_data.get("description", ""),
                        "duration": raw_data.get("duration", 180),
                        "marking": raw_data.get("marking", {"correct": 4, "incorrect": -1}),
                        "syllabus": raw_data.get("syllabus", {})
                    },
                    "questions": flat_questions
                }
                with state_lock:
                    active_data = formatted_data
                self.send_json({"success": True, "count": len(flat_questions), "title": raw_data.get("name", "Test")})
            except Exception as e:
                self.send_json({"error": f"Failed to load to studio: {e}"}, code=500)

        else:
            self.send_error(404, "Endpoint Not Found")

    def serve_file(self, file_path: Path, forced_type: Optional[str] = None, download_name: Optional[str] = None):
        if not file_path.is_file():
            self.send_error(404, "File Not Found")
            return

        ctype = forced_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        try:
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if download_name:
                safe_name = download_name.replace('"', '').replace('\r', '').replace('\n', '')
                self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")


def run_server(port: int = 8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n=======================================================")
    print(f"🚀 Allen Hybrid Parser Studio running on:")
    print(f"   http://127.0.0.1:{port} or http://localhost:{port}")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Allen Hybrid Parser Studio")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default 8080)")
    args = parser.parse_args()
    run_server(args.port)
