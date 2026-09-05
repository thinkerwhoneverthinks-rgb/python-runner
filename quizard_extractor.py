"""Quizard Extraction Engine for Python Runner.

Automates test discovery, question extraction, answer key parsing,
and syllabus extraction from Quizard, formatted for Quizzy.
Runs asynchronously in background threads with live log streaming.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import uuid
import zipfile

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.sync_api import sync_playwright

DEFAULT_BASE_URL = "https://quizard-v3-new-4fb72be6e76b.herokuapp.com/"

EXTRACTION_JS = """
(args) => {
    const { testName, testId, batchPrefix, duration } = args;
    
    const data = {
        id: testId,
        name: testName,
        description: `${batchPrefix} - ${testName}`,
        duration: duration,
        marking: { correct: 4, incorrect: -1 },
        sections: [],
        syllabus: "" 
    };

    let currentSectionObj = null;
    let currentSecKey = "";
    let qNum = 1;
    const optMap = { 'A': 0, 'B': 1, 'C': 2, 'D': 3 };

    const elements = document.querySelectorAll("h3, table tr");
    
    elements.forEach(el => {
        if (el.tagName === "H3" && el.innerText.toUpperCase().includes("SECTION")) {
            let text = el.innerText.toUpperCase();
            
            // Clean up name (e.g., "Section : PHYSICS" -> "Physics")
            let secName = text.replace(/SECTION\\s*:?/i, '').trim();
            secName = secName.charAt(0).toUpperCase() + secName.slice(1).toLowerCase();
            
            currentSectionObj = { name: secName, questions: [] };
            currentSecKey = secName.substring(0, 3).toLowerCase();
            qNum = 1;
            
            data.sections.push(currentSectionObj);
        } 
        else if (el.tagName === "TR" && currentSectionObj) {
            const tds = el.querySelectorAll("td");
            if (tds.length >= 3) {
                const qNumberStr = tds[0].innerText.trim();
                const img = tds[1].querySelector("img");
                const imageUrl = img ? (img.src || img.getAttribute('src')) : null;
                
                if (imageUrl && !isNaN(parseInt(qNumberStr))) {
                    let correctAns = 0; 
                    let qType = "MCQ"; 
                    
                    const divs = tds[2].querySelectorAll("div");
                    let answerText = "";
                    divs.forEach(div => {
                        if (div.innerText.includes("Correct Answer")) {
                            answerText = div.innerText;
                        }
                    });

                    if (answerText) {
                        const match = answerText.match(/Correct Answers?\\s*:\\s*(.*)/i);
                        if (match) {
                            let rawAns = match[1].trim().toUpperCase();
                            
                            // 1. Detect MULTI-MCQ
                            if (answerText.toLowerCase().includes("answers") || rawAns.includes(',')) {
                                qType = "MULTI_MCQ";
                                let parts = rawAns.match(/[A-D]/g) || [];
                                correctAns = parts.map(s => optMap[s]);
                            } 
                            // 2. Detect INTEGER
                            else if (/^-?\\d+(\\.\\d+)?$/.test(rawAns)) {
                                qType = "INTEGER";
                                correctAns = parseFloat(rawAns);
                            } 
                            // 3. Detect Standard MCQ
                            else {
                                qType = "MCQ";
                                correctAns = optMap[rawAns] !== undefined ? optMap[rawAns] : rawAns;
                            }
                        }
                    }

                    let questionData = {
                        id: `${testId}_${currentSecKey}_q${qNum}`,
                        image_url: imageUrl,
                        type: qType,
                        correct: correctAns
                    };

                    if (qType !== "INTEGER") {
                        questionData.options = ["A", "B", "C", "D"];
                    }

                    currentSectionObj.questions.push(questionData);
                    qNum++;
                }
            }
        }
    });

    // Fallback: Extract Syllabus from Instructions modal 
    const instructionsContent = document.getElementById('instructionsContent');
    if (instructionsContent) {
        const html = instructionsContent.innerHTML;
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = html;
        data.syllabus = tempDiv.textContent.trim();
    } else {
        data.syllabus = "Syllabus not available";
    }

    return data;
}
"""


def generate_id_slug(prefix: str, raw_name: str) -> str:
    clean_name = re.sub(r'[^a-z0-9\s]', ' ', raw_name.lower())
    clean_name = re.sub(r'\s+', '_', clean_name).strip('_')
    return f"{prefix}_{clean_name}"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-\.\(\) ]', '_', name)


def clean_html_to_text(html_content: str) -> str:
    if not html_content:
        return "Syllabus not available"
    
    text = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(div|p|ul|ol|li|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    text = text.strip()

    match = re.search(r'General Instructions\s*(.*?)\s*Test Instructions', text, re.IGNORECASE | re.DOTALL)
    if match:
        extracted = match.group(1).strip()
        if extracted:
            return extracted
            
    return text


def format_quizzy_syllabus(raw_syllabus: str, test_name: str) -> dict:
    """Formats raw syllabus text into Quizzy's structured syllabus object."""
    if not raw_syllabus or not raw_syllabus.strip() or raw_syllabus.strip().lower() == "syllabus not available":
        return {
            "enabled": False,
            "type": "text",
            "title": f"{test_name} Syllabus",
            "buttonLabel": "Syllabus",
            "content": "Syllabus not available"
        }
    
    text = raw_syllabus.strip()
    if "📌" in text:
        return {
            "enabled": True,
            "type": "text",
            "title": f"{test_name} Syllabus",
            "buttonLabel": "Syllabus",
            "content": text
        }
    
    subj_pattern = re.compile(r'(?i)(?:^|\n)\s*(?:•\s*)?(Physics|Chemistry|Botany|Zoology|Mathematics|Biology)\s*[:\-]?\s*')
    if subj_pattern.search(text):
        parts = subj_pattern.split(text)
        subjects: Dict[str, List[str]] = {}
        for i in range(1, len(parts), 2):
            sname = parts[i].capitalize()
            scontent = parts[i + 1].strip()
            clines = [l.strip().lstrip('•*- ').strip() for l in scontent.split('\n') if l.strip().lstrip('•*- ').strip()]
            if clines:
                subjects[sname] = clines
        
        if subjects:
            formatted_blocks = []
            for sname, items in subjects.items():
                bullets = [f"• {it}" for it in items]
                formatted_blocks.append(f"📌 {sname.upper()}\n" + "\n".join(bullets))
            text = "\n\n".join(formatted_blocks)

    return {
        "enabled": True,
        "type": "text",
        "title": f"{test_name} Syllabus",
        "buttonLabel": "Syllabus",
        "content": text
    }


def fetch_syllabus(page, base_url: str, batch_id: str, batch_name: str, test_id: str) -> str:
    try:
        encoded_batch_name = urllib.parse.quote(batch_name)
        api_url = f"{base_url.rstrip('/')}/instructions/{batch_id}/{encoded_batch_name}/{test_id}/batch_test"
        response = page.request.get(api_url, timeout=10000)
        
        if response.status == 200:
            res_json = response.json()
            raw_html = res_json.get("instructions_html", "")
            return clean_html_to_text(raw_html)
    except Exception:
        pass
    return "Syllabus not available"


class QuizardJob:
    """Thread-safe background runner for Quizard batch extraction."""

    def __init__(
        self,
        category: str,
        batches: str = "all",
        base_url: str = DEFAULT_BASE_URL,
        use_api_syllabus: bool = True,
        headless: bool = True,
        output_dir: Optional[Path] = None,
    ):
        self.id = uuid.uuid4().hex[:10]
        self.base_url = (base_url or DEFAULT_BASE_URL).strip()
        self.category = category.strip()
        self.batch_input = (batches or "all").strip()
        self.use_api_syllabus = use_api_syllabus
        self.headless = headless

        self.output_dir = output_dir or (Path(__file__).parent / "workspace" / "quizard_outputs")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.state = "queued"  # queued, running, finished, stopped, error
        self.message = "Job queued..."
        self.done = 0
        self.total = 1
        self.active_batch = ""
        self.active_test = ""
        self.stop_requested = False

        self.logs: List[Dict[str, str]] = []
        self.failed_tracker: Dict[str, List[str]] = {}
        self.skipped_tracker: Dict[str, List[str]] = {}
        self.skipped_list: List[Dict[str, Any]] = []
        self.zip_files: List[Dict[str, Any]] = []
        self.json_files: List[Dict[str, Any]] = []
        self.summary: Dict[str, Any] = {}
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def log(self, msg: str, level: str = "info"):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {"time": now_str, "level": level, "msg": msg}
        self.logs.append(entry)
        if len(self.logs) > 1000:
            self.logs = self.logs[-1000:]
        print(f"[{now_str}] [{level.upper()}] {msg}")

    def progress(self, done: int, total: int, msg: str, active_batch: str = "", active_test: str = ""):
        self.done = done
        self.total = max(total, 1)
        self.message = msg
        if active_batch:
            self.active_batch = active_batch
        if active_test:
            self.active_test = active_test

    def stop(self):
        self.stop_requested = True
        self.log("⏹️ Stop requested by user. Terminating process...", level="warning")
        self.message = "Stopping job..."

    def run(self):
        self.state = "running"
        self.start_time = time.time()
        self.log(f"🚀 Starting Quizard extraction job [{self.id}]")
        self.log(f"Category: '{self.category}' | Batches: '{self.batch_input}' | Headless: {self.headless}")
        self.progress(0, 10, "Launching Playwright browser session...")

        total_saved_count = 0
        total_skipped_count = 0
        total_tests_processed = 0

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                self.log(f"🌐 Navigating to Quizard at {self.base_url} ...")
                self.progress(1, 10, f"Navigating to {self.base_url}...")
                page.goto(self.base_url)

                if self.stop_requested:
                    browser.close()
                    self._finish_stopped()
                    return

                batches_to_process: List[str] = []

                if self.batch_input.lower() == 'all':
                    self.log(f"🔍 Discovering all batches under category '{self.category}'...")
                    self.progress(2, 10, f"Finding batches for '{self.category}'...")
                    
                    cat_loc = page.get_by_text(self.category, exact=True).first
                    cat_loc.click()
                    page.wait_for_timeout(2000)

                    page.evaluate("""() => {
                        const scrollables = Array.from(document.querySelectorAll('*')).filter(el => {
                            return el.scrollHeight > el.clientHeight && window.getComputedStyle(el).overflowY !== 'visible';
                        });
                        scrollables.forEach(s => { s.scrollTop = s.scrollHeight; });
                    }""")
                    page.wait_for_timeout(1500)

                    batches_to_process = page.evaluate('''(catName) => {
                        const headers = Array.from(document.querySelectorAll('*')).filter(el => el.innerText && el.innerText.trim() === catName);
                        if (headers.length === 0) return [];
                        const activeHeader = headers[headers.length - 1]; 
                        
                        let container = activeHeader.parentElement;
                        while(container && container.clientHeight === container.scrollHeight) {
                            container = container.parentElement;
                            if(!container) break;
                        }
                        if(!container) container = document.body;
                        
                        const elements = Array.from(container.querySelectorAll('*')).filter(el => {
                            return el.children.length === 0 && el.innerText && el.innerText.trim().length > 3;
                        });
                        const exclude = ['For JEE', 'For NEET', catName, 'Start Test'];
                        const results = elements.map(e => e.innerText.trim()).filter(text => !exclude.includes(text));
                        return [...new Set(results)]; 
                    }''', self.category)

                    if not batches_to_process:
                        self.log("❌ Failed to automatically find any batches under that category.", level="error")
                        browser.close()
                        self.state = "error"
                        self.message = f"No batches discovered for category '{self.category}'"
                        return

                    self.log(f"📊 Discovered {len(batches_to_process)} batches: {', '.join(batches_to_process)}", level="success")
                else:
                    batches_to_process = [b.strip() for b in self.batch_input.split(',') if b.strip()]
                    self.log(f"📋 Queued {len(batches_to_process)} specific batches: {', '.join(batches_to_process)}")

                batch_total = len(batches_to_process)

                for b_idx, current_batch in enumerate(batches_to_process, 1):
                    if self.stop_requested:
                        break

                    self.active_batch = current_batch
                    self.log(f"▶️ STARTING BATCH [{b_idx}/{batch_total}]: '{current_batch}'", level="batch")
                    self.progress(b_idx - 1, batch_total, f"Batch {b_idx}/{batch_total}: {current_batch}", active_batch=current_batch)

                    current_id_prefix = re.sub(r'[^a-z0-9]', '_', current_batch.lower())
                    current_id_prefix = re.sub(r'_+', '_', current_id_prefix).strip('_')

                    safe_batch_name = sanitize_filename(current_batch)
                    batch_dir = self.output_dir / safe_batch_name
                    batch_dir.mkdir(parents=True, exist_ok=True)

                    try:
                        page.goto(self.base_url)
                        page.wait_for_load_state("networkidle")
                        page.get_by_text(self.category, exact=True).first.click()
                        page.wait_for_timeout(1000)

                        batch_locator = page.get_by_text(current_batch, exact=True).first
                        batch_locator.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        batch_locator.click()
                        page.wait_for_load_state("networkidle")
                        batch_url = page.url

                        test_cards_data = page.evaluate("""() => {
                            const cards = Array.from(document.querySelectorAll('.test-card'));
                            if (cards.length > 0) {
                                return cards.map(card => {
                                    const titleEl = card.querySelector('.test-card-title');
                                    const instrBtn = card.querySelector('.btn-instructions');
                                    let batchId = null;
                                    let testId = null;
                                    
                                    const clickAttr = instrBtn ? instrBtn.getAttribute('onclick') : "";
                                    const match = clickAttr.match(/['"]([a-f0-9]{24})['"]\\s*,\\s*['"]([a-f0-9]{24})['"]/i);
                                    if (match) {
                                        batchId = match[1];
                                        testId = match[2];
                                    }
                                    
                                    return {
                                        title: titleEl ? titleEl.innerText.trim() : "Unknown_Test",
                                        batchId: batchId,
                                        testId: testId
                                    };
                                });
                            } else {
                                const btns = Array.from(document.querySelectorAll('button')).filter(b => b.innerText.trim().match(/Start Test/i));
                                return btns.map((btn, index) => {
                                    let parent = btn.parentElement;
                                    let title = `Test_${index+1}`;
                                    while (parent) {
                                        const text = parent.innerText;
                                        if (text && text.includes('Questions') && text.includes('Start Test')) {
                                            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                                            title = lines[0]; 
                                            break;
                                        }
                                        parent = parent.parentElement;
                                    }
                                    return { title: title, batchId: null, testId: null };
                                });
                            }
                        }""")

                        start_buttons = page.locator('button:has-text("Start Test")')
                        test_count = start_buttons.count()
                        self.log(f"📋 Found {test_count} tests in batch '{current_batch}'.")

                        for i in range(test_count):
                            if self.stop_requested:
                                break

                            tdata = test_cards_data[i] if i < len(test_cards_data) else {"title": f"Test_{i+1}", "batchId": None, "testId": None}
                            raw_test_name = tdata["title"]
                            batch_id = tdata["batchId"]
                            internal_test_id = tdata["testId"]
                            self.active_test = raw_test_name
                            total_tests_processed += 1

                            file_name = f"{sanitize_filename(raw_test_name)}.json"
                            json_path = batch_dir / file_name

                            if json_path.exists():
                                self.log(f"   ⏭️ Skipping: {raw_test_name} (File already exists) ({i + 1}/{test_count})", level="info")
                                total_skipped_count += 1
                                if current_batch not in self.skipped_tracker:
                                    self.skipped_tracker[current_batch] = []
                                self.skipped_tracker[current_batch].append(raw_test_name)
                                self.skipped_list.append({
                                    "batch": current_batch,
                                    "test": raw_test_name,
                                    "filename": file_name,
                                    "rel_path": f"{safe_batch_name}/{file_name}",
                                    "size_bytes": json_path.stat().st_size,
                                    "reason": "File already exists in output directory"
                                })
                                self.json_files.append({
                                    "batch": current_batch,
                                    "test": raw_test_name,
                                    "filename": file_name,
                                    "rel_path": f"{safe_batch_name}/{file_name}",
                                    "size_bytes": json_path.stat().st_size
                                })
                                continue

                            max_attempts = 3
                            json_saved = False
                            attempt = 0

                            while attempt < max_attempts and not json_saved:
                                if self.stop_requested:
                                    break
                                attempt += 1
                                try:
                                    self.log(f"   ⚙️ Processing Test: {raw_test_name} ({i + 1}/{test_count})")
                                    self.progress(
                                        b_idx - 1,
                                        batch_total,
                                        f"[{b_idx}/{batch_total}] Test {i + 1}/{test_count}: {raw_test_name}",
                                        active_batch=current_batch,
                                        active_test=raw_test_name
                                    )

                                    syllabus_text = None
                                    if self.use_api_syllabus:
                                        if batch_id and internal_test_id:
                                            syllabus_text = fetch_syllabus(page, self.base_url, batch_id, current_batch, internal_test_id)
                                        else:
                                            syllabus_text = "Syllabus not available (No IDs found on page)"

                                    test_id = generate_id_slug(current_id_prefix, raw_test_name)
                                    duration = 60 if "short" in raw_test_name.lower() else 180

                                    buttons = page.locator('button:has-text("Start Test")')
                                    buttons.nth(i).click()

                                    start_quiz_btn = page.locator("text=/Start Quiz/i").first
                                    start_quiz_btn.wait_for(state="visible", timeout=15000)
                                    start_quiz_btn.scroll_into_view_if_needed()
                                    page.wait_for_timeout(1500)
                                    start_quiz_btn.click()

                                    page.wait_for_load_state("domcontentloaded")

                                    submit_btn = page.locator("text=/Submit/i").first
                                    submit_btn.wait_for(state="visible", timeout=15000)
                                    submit_btn.scroll_into_view_if_needed()
                                    page.wait_for_timeout(1000)
                                    submit_btn.click(force=True)

                                    page.wait_for_timeout(1000)
                                    page.locator("text=/Confirm/i").first.click()

                                    page.wait_for_selector('h3:has-text("Section")', timeout=20000)

                                    js_args = {
                                        "testName": raw_test_name,
                                        "testId": test_id,
                                        "batchPrefix": current_batch,
                                        "duration": duration
                                    }
                                    extracted_data = page.evaluate(EXTRACTION_JS, js_args)

                                    if self.use_api_syllabus and syllabus_text is not None:
                                        extracted_data["syllabus"] = syllabus_text

                                    # Format syllabus and clean sections for Quizzy website
                                    raw_syl = extracted_data.get("syllabus", "")
                                    extracted_data["syllabus"] = format_quizzy_syllabus(raw_syl, raw_test_name)
                                    extracted_data["sections"] = [s for s in extracted_data.get("sections", []) if len(s.get("questions", [])) > 0]

                                    with open(json_path, "w", encoding="utf-8") as f:
                                        json.dump(extracted_data, f, indent=2, ensure_ascii=False)

                                    json_saved = True
                                    total_saved_count += 1
                                    self.log(f"      ✅ Saved JSON: {file_name}", level="success")
                                    self.json_files.append({
                                        "batch": current_batch,
                                        "test": raw_test_name,
                                        "filename": file_name,
                                        "rel_path": f"{safe_batch_name}/{file_name}",
                                        "size_bytes": json_path.stat().st_size
                                    })

                                except Exception as e:
                                    self.log(f"      ❌ Attempt {attempt}/{max_attempts} failed for {raw_test_name}: {e}", level="warning")
                                    if attempt < max_attempts and not self.stop_requested:
                                        self.log(f"      🔄 Retrying in 2 seconds... ({max_attempts - attempt} attempts left)")
                                        time.sleep(2)
                                        page.goto(batch_url)
                                        page.wait_for_load_state("networkidle")
                                        continue
                                    else:
                                        self.log(f"      ❌ All {max_attempts} attempts failed for {raw_test_name}. Skipping.", level="error")
                                        if current_batch not in self.failed_tracker:
                                            self.failed_tracker[current_batch] = []
                                        self.failed_tracker[current_batch].append(f"{raw_test_name} (Index {i+1}) - {e}")

                            page.goto(batch_url)
                            page.wait_for_load_state("networkidle")

                    except Exception as e:
                        self.log(f"❌ Failed processing batch '{current_batch}': {e}", level="error")
                        if current_batch not in self.failed_tracker:
                            self.failed_tracker[current_batch] = []
                        self.failed_tracker[current_batch].append(f"ENTIRE BATCH FAILED: {e}")

                    # Package batch files into ZIP (only JSONs, NO empty pdf folder)
                    batch_json_files = list(batch_dir.glob("*.json"))
                    if batch_json_files:
                        zip_file_name = f"{safe_batch_name}.zip"
                        zip_path = self.output_dir / zip_file_name
                        self.log(f"📦 Packaging {len(batch_json_files)} test JSONs into '{zip_file_name}'...")
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            for jf in batch_json_files:
                                arcname = os.path.join(safe_batch_name, jf.name)
                                zipf.write(jf, arcname)

                        self.zip_files.append({
                            "batch": current_batch,
                            "filename": zip_file_name,
                            "rel_path": zip_file_name,
                            "size_bytes": zip_path.stat().st_size,
                            "test_count": len(batch_json_files)
                        })
                        self.log(f"🎉 Batch execution completed for '{current_batch}'! Zip size: {zip_path.stat().st_size / 1024:.1f} KB", level="success")

                browser.close()

            if self.stop_requested:
                self._finish_stopped()
                return

            self.end_time = time.time()
            elapsed_sec = int(self.end_time - (self.start_time or self.end_time))

            # Build comprehensive execution summary
            failed_batches_str = ", ".join(self.failed_tracker.keys()) if self.failed_tracker else ""
            total_failed_tests = sum(len(v) for v in self.failed_tracker.values())

            self.summary = {
                "batches_processed": len(batches_to_process),
                "total_saved": total_saved_count,
                "total_skipped": total_skipped_count,
                "total_failed": total_failed_tests,
                "skipped_tracker": self.skipped_tracker,
                "skipped_list": self.skipped_list,
                "failed_tracker": self.failed_tracker,
                "failed_batches_string": failed_batches_str,
                "elapsed_seconds": elapsed_sec,
                "zip_files": self.zip_files,
                "json_files_count": len(self.json_files)
            }

            self.state = "finished"
            self.progress(batch_total, batch_total, "Extraction completed successfully!")
            self.log("=======================================================", level="batch")
            self.log(f"🏁 EXECUTION FINISHED in {elapsed_sec}s!", level="success")
            self.log(f"✅ Saved: {total_saved_count} | ⏭️ Skipped: {total_skipped_count} | ❌ Failed: {total_failed_tests}")
            if self.failed_tracker:
                self.log(f"⚠️ Failed batches retry list: {failed_batches_str}", level="warning")
            self.log("=======================================================", level="batch")

        except Exception as e:
            self.state = "error"
            self.message = f"Error: {e}"
            self.log(f"💥 Extraction failed with exception: {e}", level="error")

    def _finish_stopped(self):
        self.state = "stopped"
        self.message = "Job aborted by user."
        self.log("🛑 Job execution was stopped.", level="warning")
