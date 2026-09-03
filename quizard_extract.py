import os
import re
import json
import zipfile
import shutil
import time
import urllib.parse
from playwright.sync_api import sync_playwright

# ================= CONFIGURATION =================
BASE_URL = "https://quizard-v3-new-4fb72be6e76b.herokuapp.com/"
# =================================================

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
        extracted_syllabus = match.group(1).strip()
        if extracted_syllabus:
            return extracted_syllabus
            
    return text

def format_quizzy_syllabus(raw_syllabus: str, test_name: str) -> dict:
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
        subjects = {}
        for i in range(1, len(parts), 2):
            sname = parts[i].capitalize()
            scontent = parts[i+1].strip()
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
    except Exception as e:
        pass
    return "Syllabus not available"

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

    // Use the robust sequential iteration from the old working script
    const elements = document.querySelectorAll("h3, table tr");
    
    elements.forEach(el => {
        if (el.tagName === "H3" && el.innerText.toUpperCase().includes("SECTION")) {
            let text = el.innerText.toUpperCase();
            
            // Clean up name (e.g., "Section : PHYSICS" -> "Physics")
            let secName = text.replace(/SECTION\\s*:?/i, '').trim();
            secName = secName.charAt(0).toUpperCase() + secName.slice(1).toLowerCase();
            
            currentSectionObj = { name: secName, questions: [] };
            currentSecKey = secName.substring(0, 3).toLowerCase();
            qNum = 1; // Reset question counter for the new section
            
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

                    // Only append A,B,C,D options if it's a multiple choice type
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

def main():
    print("=======================================================")
    print("          QUIZARD DYNAMIC EXTRACTION SCRIPT            ")
    print("=======================================================")
    
    category_input = input("Enter Category Name (e.g., 'DROPPER Tests'): ").strip()
    batch_input = input("Enter Batch Name(s) separated by commas, or type 'all': ").strip()
    
    # === YES/NO SYLLABUS PROMPT ===
    syllabus_input = input("Extract detailed syllabus via background API? (yes/no): ").strip().lower()
    use_api_syllabus = syllabus_input in ['yes', 'y']
    
    failed_tracker = {}
    
    print("\n🚀 Starting browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"🌐 Navigating to {BASE_URL} ...")
        page.goto(BASE_URL)

        batches_to_process = []

        if batch_input.lower() == 'all':
            print(f"🔍 'All' selected. Discovering all batches under '{category_input}'...")
            page.get_by_text(category_input, exact=True).first.click()
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
            }''', category_input)

            if not batches_to_process:
                print("❌ Failed to find any batches automatically.")
                browser.close()
                return
            print(f"📊 Found {len(batches_to_process)} batches: {batches_to_process}")
        else:
            batches_to_process = [b.strip() for b in batch_input.split(',') if b.strip()]
            print(f"📋 Queued {len(batches_to_process)} specific batches to process.")

        for current_batch in batches_to_process:
            print(f"\n=======================================================")
            print(f"▶️ STARTING BATCH: {current_batch}")
            print(f"=======================================================")
            
            current_id_prefix = re.sub(r'[^a-z0-9]', '_', current_batch.lower())
            current_id_prefix = re.sub(r'_+', '_', current_id_prefix).strip('_')

            safe_batch_name = sanitize_filename(current_batch)
            temp_dir = os.path.join(os.getcwd(), safe_batch_name)
            os.makedirs(temp_dir, exist_ok=True)

            try:
                page.goto(BASE_URL)
                page.wait_for_load_state("networkidle")
                page.get_by_text(category_input, exact=True).first.click()
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
                print(f"📋 Found {test_count} tests in {current_batch}.")

                for i in range(test_count):
                    tdata = test_cards_data[i] if i < len(test_cards_data) else {"title": f"Test_{i+1}", "batchId": None, "testId": None}
                    raw_test_name = tdata["title"]
                    batch_id = tdata["batchId"]
                    internal_test_id = tdata["testId"]
                    
                    file_name = f"{sanitize_filename(raw_test_name)}.json"
                    json_path = os.path.join(temp_dir, file_name)
                    
                    if os.path.exists(json_path):
                        print(f"   ⏭️ Skipping: {raw_test_name} (File already exists) ({i + 1}/{test_count})")
                        continue

                    max_attempts = 3
                    json_saved = False
                    attempt = 0

                    while attempt < max_attempts and not json_saved:
                        attempt += 1
                        try:
                            print(f"\n   ⚙️ Processing Test: {raw_test_name} ({i + 1}/{test_count})")
                            
                            # === CONDITIONAL SYLLABUS EXTRACTION ===
                            syllabus_text = None
                            if use_api_syllabus:
                                if batch_id and internal_test_id:
                                    syllabus_text = fetch_syllabus(page, BASE_URL, batch_id, current_batch, internal_test_id)
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

                            # Run DOM JS Extraction
                            js_args = {
                                "testName": raw_test_name,
                                "testId": test_id,
                                "batchPrefix": current_batch,
                                "duration": duration
                            }
                            extracted_data = page.evaluate(EXTRACTION_JS, js_args)

                            if use_api_syllabus and syllabus_text is not None:
                                extracted_data["syllabus"] = syllabus_text

                            # Format syllabus and clean sections for Quizzy website
                            raw_syl = extracted_data.get("syllabus", "")
                            extracted_data["syllabus"] = format_quizzy_syllabus(raw_syl, raw_test_name)
                            extracted_data["sections"] = [s for s in extracted_data.get("sections", []) if len(s.get("questions", [])) > 0]

                            with open(json_path, "w", encoding="utf-8") as f:
                                json.dump(extracted_data, f, indent=2, ensure_ascii=False)
                            
                            json_saved = True
                            print(f"      ✅ Saved JSON: {file_name}")

                        except Exception as e:
                            print(f"      ❌ Attempt {attempt}/{max_attempts} failed for {raw_test_name}: {str(e)}")
                            if attempt < max_attempts:
                                print(f"      🔄 Retrying in 2 seconds... ({max_attempts - attempt} attempts left)")
                                time.sleep(2)
                                page.goto(batch_url) # Reset page state for retry
                                page.wait_for_load_state("networkidle")
                                continue
                            else:
                                print(f"      ❌ All {max_attempts} attempts failed for {raw_test_name}. Skipping.")
                                if current_batch not in failed_tracker:
                                    failed_tracker[current_batch] = []
                                failed_tracker[current_batch].append(f"{raw_test_name} (Index {i+1}) - JSON failed to generate")

                    page.goto(batch_url)
                    page.wait_for_load_state("networkidle")

            except Exception as e:
                print(f"❌ Failed processing batch {current_batch}: {e}")
                if current_batch not in failed_tracker:
                    failed_tracker[current_batch] = []
                failed_tracker[current_batch].append("ENTIRE BATCH FAILED OR CRASHED")
                continue 

            if any(os.scandir(temp_dir)):
                zip_file_name = f"{safe_batch_name}.zip"
                print(f"\n📦 Packaging batch files into '{zip_file_name}'...")
                with zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(temp_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, os.path.dirname(temp_dir))
                            zipf.write(file_path, arcname)

            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"🎉 Batch execution completed for '{current_batch}'!")

        browser.close()
        
    print("\n=======================================================")
    print("                    EXECUTION SUMMARY                  ")
    print("=======================================================")
    if not failed_tracker:
        print("✅ ALL TESTS COMPLETED SUCCESSFULLY! No errors found.")
    else:
        print("⚠️ THE FOLLOWING TESTS FAILED TO GENERATE:\n")
        failed_batches_string = ", ".join(failed_tracker.keys())
        for batch, tests in failed_tracker.items():
            print(f"🔴 Batch: {batch}")
            for t in tests:
                print(f"   └── ❌ {t}")
        print("\n💡 COPY THIS TEXT TO RETRY JUST THE FAILED BATCHES:")
        print(f"   {failed_batches_string}")
        print("\n(Note: The script will automatically skip tests that were already")
        print(" successful in previous runs if the JSON file is still there.)")
    print("=======================================================\n")

if __name__ == "__main__":
    main()