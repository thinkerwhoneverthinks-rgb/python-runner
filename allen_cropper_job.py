"""Allen Cropper Job & Automation Engine.

Automates the Allen PDF Question Cropper workflow:
1. Accepts PDF via direct browser upload or downloadable link.
2. Invokes no_qn_code_copper.py AS IS (preserving all logic untouched).
3. Option to generate ZIP archives (organized by Topic, Section & Answer Key).
4. Option to upload cropped question images to Cloudinary and generate clean JSON (as in cropper/upload.py).
5. Supports both ZIP + Cloudinary JSON simultaneously.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent
WORKSPACE_DIR = HERE / "workspace"
DEFAULT_INPUTS_DIR = WORKSPACE_DIR / "allen_cropper_inputs"
DEFAULT_OUTPUTS_DIR = WORKSPACE_DIR / "allen_cropper_outputs"

DEFAULT_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# Default Cloudinary configuration (credentials loaded from environment or UI)
DEFAULT_CLOUDINARY_CONFIG = {
    "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    "api_key": os.getenv("CLOUDINARY_API_KEY", ""),
    "api_secret": os.getenv("CLOUDINARY_API_SECRET", ""),
    "base_folder": "quiz_app",
    "secure": True,
}

# --- Cloudinary & JSON Utilities (from cropper/upload.py) ---
SKIP_WORDS = {'questions', 'based', 'on', 'of', 'the', 'and', 'in', 'for', 'with', 'a', 'an', 'to', 'ug', 'formula'}


def natural_sort_key(s: str) -> list:
    """Sorts filenames logically (q2.png before q10.png)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def extract_q_num(filename: str) -> Optional[str]:
    """Extracts the number from filenames like 'q24.png'."""
    match = re.search(r'\d+', filename)
    return match.group(0) if match else None


def abbreviate_name(name: str, max_chars_per_word: int = 4) -> str:
    """Dynamically generates a short abbreviation from any name."""
    s = name.lower()
    s = s.replace('&', ' and ').replace('_', ' ').replace('-', ' ')
    s = s.replace('(', ' ').replace(')', ' ').replace(',', ' ')
    words = s.split()
    result_parts = []
    for word in words:
        clean = re.sub(r'[^a-z0-9]', '', word)
        if not clean or clean in SKIP_WORDS:
            continue
        if re.match(r'^20\d{2}$', clean):
            clean = clean[2:]
        result_parts.append(clean[:max_chars_per_word])
    return ''.join(result_parts)


def sanitize_path(path: str) -> str:
    """Sanitizes a path string for use in Cloudinary folder names."""
    replacements = {
        '&': 'and', '#': '', '%': '', '@': '', '!': '', '+': 'plus',
        '=': '', '{': '', '}': '', '[': '', ']': '', '|': '', '\\': '/',
        ';': '', ':': '', '"': '', "'": '', '<': '', '>': '', '?': '',
        '^': '', '~': '', '`': ''
    }
    for char, replacement in replacements.items():
        path = path.replace(char, replacement)
    return path


def generate_question_id(folder_name: str, category: str, sub_category: str, index: int) -> str:
    """Generates hierarchical ID: {folder_abbr}-{category_abbr}-{subcategory_abbr}-q{index}."""
    folder_part = abbreviate_name(folder_name, max_chars_per_word=3) or "quiz"
    cat_part = abbreviate_name(category, max_chars_per_word=4) or "gen"
    sub_part = abbreviate_name(sub_category, max_chars_per_word=4) or "gen"
    return f"{folder_part}-{cat_part}-{sub_part}-q{index}"


def locate_cropper_script() -> Path:
    """Locates no_qn_code_copper.py in workspace root or current directory."""
    candidates = [
        HERE.parent / "no_qn_code_copper.py",
        HERE / "no_qn_code_copper.py",
        Path(r"c:\Users\Abhishek Pandey\Desktop\Anurag\antigravity\no_qn_code_copper.py"),
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError("Could not find no_qn_code_copper.py in workspace.")


def download_pdf(url: str, output_path: Path, log_fn: Optional[Callable[[str, str], None]] = None) -> Path:
    """Downloads a PDF from a direct URL or Google Drive link."""
    clean_url = (url or "").strip()
    if not clean_url:
        raise ValueError("Empty URL provided")

    if log_fn:
        log_fn(f"🌐 Initiating PDF download from: {clean_url[:90]}...", "info")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1"
    })

    # Transform Google Drive sharing links
    gd_match = re.search(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)', clean_url)
    if gd_match:
        file_id = gd_match.group(1)
        clean_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        if log_fn:
            log_fn(f"Detected Google Drive link. Converted to direct download: id={file_id}", "info")

    resp = session.get(clean_url, stream=True, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    # Handle Google Drive large file virus warning page
    if "drive.google.com" in clean_url and "confirm=" not in clean_url:
        for k, v in resp.cookies.items():
            if k.startswith("download_warning"):
                confirm_url = f"{clean_url}&confirm={v}"
                resp = session.get(confirm_url, stream=True, timeout=30, allow_redirects=True)
                break

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                total_bytes += len(chunk)

    mb = total_bytes / (1024 * 1024)
    if log_fn:
        log_fn(f"✅ PDF downloaded successfully ({mb:.2f} MB)", "success")

    # Quick check for PDF magic header
    with open(output_path, "rb") as f:
        header = f.read(5)
        if not header.startswith(b"%PDF"):
            if log_fn:
                log_fn("⚠️ Warning: Downloaded file header does not begin with %PDF. Processing will continue.", "warning")

    return output_path


class AllenCropperJob:
    """Manages an active Allen PDF Question Cropping process."""

    def __init__(
        self,
        input_type: str,  # 'file' or 'url'
        pdf_path: Optional[str] = None,
        pdf_url: Optional[str] = None,
        prefix: str = "",
        generate_zip: bool = True,
        upload_cloudinary: bool = False,
        cloudinary_config: Optional[Dict[str, Any]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.id = f"allen_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.input_type = input_type
        self.pdf_path_str = pdf_path
        self.pdf_url = pdf_url
        self.prefix = (prefix or "").strip()
        self.generate_zip = bool(generate_zip)
        self.upload_cloudinary = bool(upload_cloudinary)
        self.c_config = dict(DEFAULT_CLOUDINARY_CONFIG)
        if cloudinary_config:
            self.c_config.update(cloudinary_config)

        self.output_root = output_dir or DEFAULT_OUTPUTS_DIR
        self.job_dir = self.output_root / self.id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.crop_out_dir = self.job_dir / "cropped_files"

        self.state = "queued"  # queued, running, finished, stopped, error
        self.message = "Job queued"
        self.done = 0
        self.total = 0
        self.start_time: float = 0
        self.end_time: float = 0
        self.logs: List[Dict[str, str]] = []
        self.lock = threading.Lock()
        self.stop_requested = False
        self.process: Optional[subprocess.Popen] = None

        self.zip_files: List[Dict[str, Any]] = []
        self.json_files: List[Dict[str, Any]] = []
        self.questions_preview: List[Dict[str, Any]] = []
        self.uploaded_question_count = 0

    def log(self, msg: str, level: str = "info"):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {"time": now, "msg": str(msg), "level": level}
        with self.lock:
            self.logs.append(entry)
        try:
            print(f"[{now}] [{level.upper()}] [AllenCropper] {msg}")
        except Exception:
            pass

    def stop(self):
        self.stop_requested = True
        self.log("🛑 Stop signal received. Terminating process...", "warning")
        if self.process:
            try:
                self.process.terminate()
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
            except Exception as e:
                self.log(f"Error terminating process: {e}", "warning")

    def run(self):
        self.start_time = time.time()
        self.state = "running"
        self.message = "Starting Allen Cropper pipeline..."
        self.log(f"🚀 Job started: {self.id}", "info")
        self.log(f"Options: Generate ZIP={self.generate_zip}, Cloudinary Upload={self.upload_cloudinary}, Prefix='{self.prefix}'", "info")

        try:
            # 1. Resolve local PDF file path
            local_pdf: Path
            if self.input_type == "url":
                if not self.pdf_url:
                    raise ValueError("No PDF URL provided.")
                temp_pdf = self.job_dir / "downloaded_module.pdf"
                local_pdf = download_pdf(self.pdf_url, temp_pdf, log_fn=self.log)
            else:
                if not self.pdf_path_str:
                    raise ValueError("No uploaded PDF file path provided.")
                local_pdf = Path(self.pdf_path_str)
                if not local_pdf.is_file():
                    raise FileNotFoundError(f"PDF file not found at: {local_pdf}")
                self.log(f"📄 Using uploaded PDF: {local_pdf.name} ({local_pdf.stat().st_size / (1024*1024):.2f} MB)", "info")

            if self.stop_requested:
                self.state = "stopped"
                self.message = "Job stopped before cropping"
                return

            # 2. Locate no_qn_code_copper.py
            cropper_script = locate_cropper_script()
            self.log(f"🛠️ Cropper engine located: {cropper_script.name} (Using logic AS IS)", "info")

            # 3. Execute no_qn_code_copper.py via subprocess
            # Arguments: script.py <pdf_path> <out_dir> <prefix>
            cmd = [
                sys.executable,
                str(cropper_script),
                str(local_pdf.resolve()),
                str(self.crop_out_dir.resolve()),
                self.prefix,
            ]
            self.log(f"▶️ Executing: {' '.join([str(c) for c in cmd])}", "info")

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )

            # Stream stdout line by line
            page_re = re.compile(r"Page\s+(\d+)/(\d+)\s+done", re.I)
            zip_re = re.compile(r"Zip:\s*(.+?)\s*\((\d+)\s*files\)", re.I)

            if self.process.stdout:
                for line in self.process.stdout:
                    clean_line = line.rstrip()
                    if not clean_line:
                        continue

                    # Parse progress
                    pm = page_re.search(clean_line)
                    if pm:
                        self.done = int(pm.group(1))
                        self.total = int(pm.group(2))
                        self.message = f"Cropping page {self.done}/{self.total}..."
                        self.log(clean_line, "info")
                        continue

                    # Parse output zip
                    zm = zip_re.search(clean_line)
                    if zm:
                        self.log(f"📦 {clean_line}", "success")
                        continue

                    # Highlight key events
                    if "audit:" in clean_line or "OK:" in clean_line:
                        self.log(clean_line, "success")
                    elif "Saved merged answer_key.json" in clean_line:
                        self.log(f"🔑 {clean_line.strip()}", "success")
                    elif "Warning:" in clean_line or "needs review" in clean_line:
                        self.log(clean_line, "warning")
                    else:
                        self.log(clean_line, "info")

            rc = self.process.wait()

            if self.stop_requested:
                self.state = "stopped"
                self.message = "Job stopped by user"
                self.log(self.message, "warning")
                return

            if rc != 0:
                raise RuntimeError(f"no_qn_code_copper.py exited with error code {rc}")

            self.log("✨ PDF cropping completed successfully!", "success")

            # 4. Handle ZIP files
            # no_qn_code_copper.py writes ZIP files to os.path.dirname(crop_out_dir) which is self.job_dir!
            created_zips = list(self.job_dir.glob("*.zip"))
            # Also check workspace dir if script saved to parent
            for pz in self.output_root.glob(f"{self.prefix}*.zip"):
                if pz not in created_zips:
                    # Move to job_dir for tidy isolation
                    dest = self.job_dir / pz.name
                    try:
                        shutil.move(str(pz), str(dest))
                        created_zips.append(dest)
                    except Exception:
                        pass

            if self.generate_zip:
                for z in created_zips:
                    sz = z.stat().st_size
                    mb = sz / (1024 * 1024)
                    rel_name = f"{self.id}/{z.name}"
                    self.zip_files.append({
                        "filename": z.name,
                        "relative_path": rel_name,
                        "size_bytes": sz,
                        "size_mb": round(mb, 2),
                        "path": str(z.resolve())
                    })
                    self.log(f"📦 ZIP Archive Available: {z.name} ({mb:.2f} MB)", "success")
            else:
                self.log("Option 'Generate ZIP' unchecked: Skipping ZIP archive registration.", "info")

            # 5. Handle Cloudinary Upload & JSON generation if requested
            if self.upload_cloudinary and not self.stop_requested:
                self.message = "Uploading cropped questions to Cloudinary..."
                self.log("☁️ Starting Cloudinary upload and JSON generation...", "info")
                self._process_cloudinary_upload()

            self.end_time = time.time()
            elapsed = self.end_time - self.start_time

            self.state = "finished"
            summary_parts = []
            if self.zip_files:
                summary_parts.append(f"{len(self.zip_files)} ZIP file(s)")
            if self.json_files:
                summary_parts.append(f"{self.uploaded_question_count} questions in JSON")
            summary_str = " & ".join(summary_parts) if summary_parts else "Processed successfully"
            self.message = f"Done in {elapsed:.1f}s ({summary_str})"
            self.log(f"🏆 {self.message}", "success")

        except Exception as e:
            self.end_time = time.time()
            self.state = "error"
            self.message = f"Error: {e}"
            self.log(f"❌ Pipeline failed: {e}", "error")

    def _process_cloudinary_upload(self):
        """Processes cropped question folders, uploads images to Cloudinary, and generates formatted JSON."""
        # Configure Cloudinary
        cloudinary.config(
            cloud_name=self.c_config["cloud_name"],
            api_key=self.c_config["api_key"],
            api_secret=self.c_config["api_secret"],
            secure=True
        )
        base_folder = self.c_config.get("base_folder", "quiz_app")
        self.log(f"☁️ Cloudinary Target Account: {self.c_config['cloud_name']} | Base Folder: {base_folder}", "info")

        if not self.crop_out_dir.exists():
            self.log("No cropped_files directory found to upload.", "warning")
            return

        # Find topic directories inside cropped_files
        topic_subdirs = [d for d in self.crop_out_dir.iterdir() if d.is_dir()]
        if not topic_subdirs:
            topic_subdirs = [self.crop_out_dir]

        total_questions_all_topics = []

        for topic_dir in topic_subdirs:
            topic_name = topic_dir.name
            self.log(f"📁 Processing topic for Cloudinary: {topic_name}", "info")

            # 1. Pre-load answer keys
            answer_keys = {}
            for root, dirs, files in os.walk(str(topic_dir)):
                for file in files:
                    if file.lower() == 'answer_key.json':
                        t_dir = os.path.dirname(root)
                        try:
                            with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                                answer_keys[t_dir] = json.load(f)
                            self.log(f"Loaded answer key from {file} in {os.path.basename(t_dir)}", "info")
                        except Exception as e:
                            self.log(f"Error reading answer key {file}: {e}", "warning")

            # 2. Count image files first for accurate progress
            image_tasks = []
            for root, dirs, files in os.walk(str(topic_dir)):
                if '.git' in root or 'answer key' in root.lower() or '__macosx' in root.lower():
                    continue
                rel_path = os.path.relpath(root, str(topic_dir))
                if rel_path == '.':
                    continue
                parts = rel_path.replace('\\', '/').split('/')
                category = parts[0]
                sub_category = parts[1] if len(parts) > 1 else "General"

                for file in sorted(files, key=natural_sort_key):
                    file_lower = file.lower()
                    if file_lower.endswith(('.png', '.jpg', '.jpeg')) and file_lower.startswith('q'):
                        image_tasks.append((root, file, category, sub_category))

            total_imgs = len(image_tasks)
            self.log(f"Found {total_imgs} question images to upload for '{topic_name}'", "info")
            if total_imgs == 0:
                continue

            # 3. Upload images and collect questions
            collected_folders = []
            folder_name = self.prefix or topic_name

            # Group by folder
            grouped_folders = {}
            for root, file, category, sub_category in image_tasks:
                if root not in grouped_folders:
                    grouped_folders[root] = {"category": category, "sub_category": sub_category, "files": []}
                grouped_folders[root]["files"].append(file)

            uploaded_count = 0
            for root, data in grouped_folders.items():
                if self.stop_requested:
                    break

                category = data["category"]
                sub_category = data["sub_category"]
                files = data["files"]

                # Find matching answer key
                current_topic_dir = root
                ans_data = {}
                while current_topic_dir != str(topic_dir) and current_topic_dir != os.path.dirname(str(topic_dir)):
                    if current_topic_dir in answer_keys:
                        ans_data = answer_keys[current_topic_dir]
                        break
                    current_topic_dir = os.path.dirname(current_topic_dir)

                questions_list = []
                original_first_q = None

                for index, file in enumerate(sorted(files, key=natural_sort_key), start=1):
                    if self.stop_requested:
                        break

                    uploaded_count += 1
                    file_rel_path = os.path.relpath(root, str(topic_dir)).replace('\\', '/')
                    sanitized_rel_path = sanitize_path(file_rel_path)
                    cloudinary_folder = f"{base_folder}/{sanitized_rel_path}"

                    cloudinary_url = None
                    max_retries = 3
                    img_file_path = os.path.join(root, file)

                    for attempt in range(1, max_retries + 1):
                        try:
                            res = cloudinary.uploader.upload(
                                img_file_path,
                                folder=cloudinary_folder,
                                use_filename=True,
                                unique_filename=False,
                                overwrite=True
                            )
                            cloudinary_url = res.get('secure_url')
                            break
                        except Exception as retry_err:
                            if attempt < max_retries:
                                time.sleep(1.5 * attempt)
                            else:
                                self.log(f"Cloudinary upload failed for {file}: {retry_err}", "warning")

                    if not cloudinary_url:
                        continue

                    q_num = extract_q_num(file)
                    q_key_str = f"q{q_num}"
                    answer = ans_data.get(q_key_str) or ans_data.get(q_num) or "Answer not found"

                    if original_first_q is None and q_num:
                        try:
                            original_first_q = int(q_num)
                        except ValueError:
                            pass

                    question_id = generate_question_id(folder_name, category, sub_category, index)

                    questions_list.append({
                        "id": question_id,
                        "category": category,
                        "sub_category": sub_category,
                        "original_file": file,
                        "image_url": cloudinary_url,
                        "correct_answer": answer,
                        "is_active": True
                    })

                    if uploaded_count % 10 == 0 or uploaded_count == total_imgs:
                        self.log(f"☁️ Uploaded {uploaded_count}/{total_imgs} questions to Cloudinary...", "info")

                if questions_list:
                    cat_order = {"Analytical Questions": 0, "Conceptual Questions": 1, "PYQ": 2}
                    collected_folders.append({
                        "questions": questions_list,
                        "sort_index": original_first_q if original_first_q is not None else 0,
                        "cat_order": cat_order.get(category, 99)
                    })

            # Sort folders
            collected_folders.sort(key=lambda x: (x["cat_order"], x["sort_index"]))

            flat_json = []
            for cf in collected_folders:
                flat_json.extend(cf["questions"])

            total_questions_all_topics.extend(flat_json)

            # Write JSON file for this topic
            clean_out_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', folder_name) or "quiz_output"
            json_filename = f"{clean_out_name}.json"
            json_filepath = self.job_dir / json_filename

            with open(json_filepath, 'w', encoding='utf-8') as f:
                json.dump(flat_json, f, indent=4, ensure_ascii=False)

            sz = json_filepath.stat().st_size
            self.json_files.append({
                "filename": json_filename,
                "relative_path": f"{self.id}/{json_filename}",
                "size_bytes": sz,
                "size_kb": round(sz / 1024, 2),
                "question_count": len(flat_json),
                "path": str(json_filepath.resolve())
            })
            self.log(f"💾 Formatted Quiz JSON created: {json_filename} ({len(flat_json)} questions)", "success")

        self.uploaded_question_count = len(total_questions_all_topics)
        if total_questions_all_topics:
            self.questions_preview = total_questions_all_topics[:10]
