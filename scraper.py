import os
import re
import json
import zipfile
import shutil
from playwright.sync_api import sync_playwright

# ================= CONFIGURATION =================
BASE_URL_TEST = "https://quizard-v3-new-4fb72be6e76b.herokuapp.com/"
BASE_URL_DPP = "https://quizard-v3-new-4fb72be6e76b.herokuapp.com/dpp"

# =================================================
# ⬇️ PASTE YOUR COMPRESSED ARCHIVE TEXT HERE ⬇️
# Example: "test_1 dpp_1_slug some_other_slug"
# =================================================
COMPRESSED_ARCHIVE = """

"""
# =================================================

def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9_\-\.\(\) ]', '_', name)
    return re.sub(r'_+', '_', clean).strip(' _')

def generate_slug(name: str) -> str:
    clean = re.sub(r'[^a-z0-9\s]', ' ', name.lower())
    return re.sub(r'\s+', '_', clean).strip('_')

EXTRACTION_JS_TEST = """
(args) => {
    const { testName, testId, batchPrefix, duration } = args;
    const originalData = {
        id: testId, name: testName, description: `${batchPrefix} - ${testName}`,
        duration: duration, marking: { correct: 4, incorrect: -1 },
        sections: [
            { name: "Physics", questions: [] }, { name: "Chemistry", questions: [] },
            { name: "Botany", questions: [] }, { name: "Zoology", questions: [] }
        ]
    };

    let currentSectionIdx = 0; let qCounters = { 0: 1, 1: 1, 2: 1, 3: 1 };
    const shortKeys = { 0: "phy", 1: "chem", 2: "bot", 3: "zoo" };
    const elements = document.querySelectorAll("h3, table tr");
    
    elements.forEach(el => {
        if (el.tagName === "H3" && el.innerText.toUpperCase().includes("SECTION")) {
            const secName = el.innerText.toUpperCase();
            if (secName.includes("PHYSICS")) currentSectionIdx = 0;
            else if (secName.includes("CHEMISTRY")) currentSectionIdx = 1;
            else if (secName.includes("BOTANY")) currentSectionIdx = 2;
            else if (secName.includes("ZOOLOGY")) currentSectionIdx = 3;
        } else if (el.tagName === "TR") {
            const tds = el.querySelectorAll("td");
            if (tds.length >= 2) {
                const img = tds[1].querySelector("img");
                const imageUrl = img ? img.src : null;
                let rawAnswerText = "";
                const textContent = tds[tds.length - 1].innerText;
                const match = textContent.match(/Correct Answer\\s*:\\s*([^\\n\\r]+)/i);
                if (match) rawAnswerText = match[1].trim();

                if (imageUrl) {
                    originalData.sections[currentSectionIdx].questions.push({
                        id: `${testId}_${shortKeys[currentSectionIdx]}_q${qCounters[currentSectionIdx]++}`,
                        image_url: imageUrl, raw_answer: rawAnswerText
                    });
                }
            }
        }
    });
    originalData.sections = originalData.sections.filter(s => s.questions.length > 0);
    return originalData;
}
"""

EXTRACTION_JS_DPP = """
(args) => {
    const { dppName, dppSlug } = args;
    const questions = []; let qCounter = 1;
    const rows = document.querySelectorAll("table tr");
    rows.forEach(tr => {
        const tds = tr.querySelectorAll("td");
        if (tds.length >= 2) {
            const img = tds[1].querySelector("img");
            const imageUrl = img ? img.src : null;
            let rawAnswerText = "";
            const match = tds[tds.length - 1].innerText.match(/Correct Answer\\s*:\\s*([^\\n\\r]+)/i);
            if (match) rawAnswerText = match[1].trim();

            if (imageUrl) {
                questions.push({ q_num: qCounter++, image_url: imageUrl, raw_answer: rawAnswerText });
            }
        }
    });
    return { dpp_name: dppName, questions: questions };
}
"""

def detect_question_type(q_id: str, raw_ans: str, image_url: str):
    letter_to_idx = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    multi_match = re.findall(r'[A-D]', raw_ans.upper())
    is_comma_separated = ',' in raw_ans or len(multi_match) > 1

    if is_comma_separated and len(multi_match) > 1:
        return {
            "id": q_id, "type": "multi_mcq", "question": "", "image_url": image_url,
            "options": ["A", "B", "C", "D"],
            "correct": [letter_to_idx[opt] for opt in multi_match if opt in letter_to_idx]
        }
    elif len(multi_match) == 1 and raw_ans.upper() in letter_to_idx:
        return {
            "id": q_id, "type": "mcq", "question": "", "image_url": image_url,
            "options": ["A", "B", "C", "D"], "correct": letter_to_idx[raw_ans.upper()]
        }
    elif re.match(r'^-?\d+(\.\d+)?$', raw_ans):
        return {
            "id": q_id, "type": "integer", "question": "", "image_url": image_url,
            "answer": raw_ans
        }
    else:
        return {
            "id": q_id, "type": "mcq", "question": "", "image_url": image_url,
            "options": ["A", "B", "C", "D"], "correct": 0
        }

def build_test_schema(raw_data: dict) -> dict:
    myjson = {
        "id": raw_data.get("id"), "name": raw_data.get("name"),
        "description": raw_data.get("description"), "duration": raw_data.get("duration", 60),
        "marking": raw_data.get("marking", {"correct": 4, "incorrect": -1}), "sections": []
    }
    for sec in raw_data.get("sections", []):
        sec_obj = {"name": sec.get("name"), "questions": []}
        for q in sec.get("questions", []):
            sec_obj["questions"].append(detect_question_type(q.get("id"), q.get("raw_answer", ""), q.get("image_url")))
        myjson["sections"].append(sec_obj)
    return myjson

def build_dpp_schema(chapter_name: str, batch_name: str, dpp_list: list) -> dict:
    chap_slug = generate_slug(chapter_name)
    myjson = {
        "id": f"{generate_slug(batch_name)}_{chap_slug}", "name": chapter_name,
        "description": f"DPP Collection for {chapter_name} ({batch_name})",
        "duration": 60, "marking": { "correct": 4, "incorrect": -1 }, "sections": []
    }
    for dpp in dpp_list:
        dpp_slug = generate_slug(dpp.get("dpp_name", "DPP"))
        section = { "name": dpp.get("dpp_name", "DPP"), "questions": [] }
        for q in dpp.get("questions", []):
            q_id = f"{chap_slug}_{dpp_slug}_q{q.get('q_num', 1)}"
            section["questions"].append(detect_question_type(q_id, q.get("raw_answer", ""), q.get("image_url")))
        myjson["sections"].append(section)
    return myjson

def generate_pdf(page_context, button_locator, pdf_dest_path):
    try:
        if button_locator.is_visible(timeout=5000):
            button_locator.scroll_into_view_if_needed()
            with page_context.expect_page() as new_page_info:
                button_locator.click()
            print_tab = new_page_info.value
            print_tab.evaluate("window.print = function() {};")
            print_tab.wait_for_load_state("networkidle")
            print_tab.pdf(path=pdf_dest_path, format="A4", print_background=True, margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"})
            print_tab.close()
    except Exception as e:
        pass

def main():
    print("=======================================================")
    print("      QUIZARD UNIFIED SCRAPER (HARDCODED DELTA)        ")
    print("=======================================================")
    
    # Load from hardcoded string
    already_downloaded = set(slug.strip() for slug in COMPRESSED_ARCHIVE.split() if slug.strip())
    session_new_downloads = []

    # Read inputs from GitHub Actions
    choice = os.environ.get("SCRAPE_MODE", "1").strip()
    json_format = os.environ.get("JSON_FORMAT", "myjson").strip()
    category_input = os.environ.get("CATEGORY_NAME", "DROPPER Tests").strip()
    batch_input = os.environ.get("BATCH_NAME", "").strip()
    sub_input = os.environ.get("SUBJECT_NAME", "").strip()
    chap_input = os.environ.get("CHAPTER_NAME", "").strip()
    
    safe_cat_name = sanitize_filename(category_input)
    temp_dir = os.path.join(os.getcwd(), safe_cat_name)
    format_base = os.path.join(temp_dir, json_format)
    pdf_base = os.path.join(temp_dir, "pdf")
    
    for d in [format_base, pdf_base]: os.makedirs(d, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # ==================== TEST SCRAPER ====================
        if choice == '1':
            page.goto(BASE_URL_TEST)
            batches_to_process = [b.strip() for b in batch_input.split(',') if b.strip()]
            
            for batch in batches_to_process:
                format_dir = os.path.join(format_base, sanitize_filename(batch))
                pdf_dir = os.path.join(pdf_base, sanitize_filename(batch))
                for d in [format_dir, pdf_dir]: os.makedirs(d, exist_ok=True)

                page.goto(BASE_URL_TEST)
                page.wait_for_load_state("networkidle")
                page.get_by_text(category_input, exact=True).first.click()
                page.wait_for_timeout(1000)
                page.get_by_text(batch, exact=True).first.click()
                page.wait_for_load_state("networkidle")
                batch_url = page.url 

                test_names = page.evaluate("() => Array.from(document.querySelectorAll('button')).filter(b => b.innerText.trim().match(/Start Test/i)).map(b => b.parentElement.innerText.split('\\n')[0])")
                test_count = page.locator('button:has-text("Start Test")').count()

                for i in range(test_count):
                    raw_test_name = test_names[i] if i < len(test_names) else f"Test_{i+1}"
                    file_name = f"{sanitize_filename(raw_test_name)}.json"
                    pdf_path = os.path.join(pdf_dir, f"{sanitize_filename(raw_test_name)}.pdf")
                    test_id = generate_slug(f"{batch}_{raw_test_name}")
                    
                    if test_id in already_downloaded:
                        print(f"   ⏭️ Skipped (From Block): {raw_test_name}")
                        continue
                    
                    print(f"   ⚙️ Processing Test: {raw_test_name} ({i+1}/{test_count})")
                    try:
                        page.locator('button:has-text("Start Test")').nth(i).click()
                        page.locator("text=/Start Quiz/i").first.wait_for(state="visible", timeout=15000)
                        page.locator("text=/Start Quiz/i").first.click()
                        page.wait_for_load_state("domcontentloaded")
                        page.locator("text=/Submit/i").first.wait_for(state="visible", timeout=15000)
                        page.locator("text=/Submit/i").first.click(force=True)
                        page.wait_for_timeout(1000)
                        page.locator("text=/Confirm/i").first.click()
                        page.wait_for_selector('h3:has-text("Section")', timeout=20000)

                        raw_data = page.evaluate(EXTRACTION_JS_TEST, {
                            "testName": raw_test_name, "testId": test_id,
                            "batchPrefix": batch, "duration": 60
                        })

                        with open(os.path.join(format_dir, file_name), "w", encoding="utf-8") as f:
                            if json_format == "original":
                                json.dump(raw_data, f, indent=2, ensure_ascii=False)
                            else:
                                json.dump(build_test_schema(raw_data), f, indent=2, ensure_ascii=False)

                        generate_pdf(context, page.locator('button:has-text("Print Result")').first, pdf_path)
                        already_downloaded.add(test_id)
                        session_new_downloads.append(raw_test_name)
                    except Exception as e:
                        print(f"      ❌ Error: {e}")

                    page.goto(batch_url)
                    page.wait_for_load_state("networkidle")

        # ==================== DPP SCRAPER ====================
        elif choice == '2':
            page.goto(BASE_URL_DPP)
            page.wait_for_load_state("networkidle")
            page.get_by_text(category_input, exact=False).first.click()
            page.wait_for_timeout(1000)
            page.get_by_text(batch_input, exact=True).first.click()
            page.wait_for_load_state("networkidle")
            
            subject_card = page.locator(f"div:has-text('{sub_input}')").filter(has=page.locator("button, a:has-text('Open')")).last
            subject_card.locator("button, a:has-text('Open')").first.click()
            page.wait_for_load_state("networkidle")
            chapters_page_url = page.url

            chaps_to_process = [c.strip() for c in chap_input.split(',') if c.strip()]
            
            for chap in chaps_to_process:
                chap_id = generate_slug(f"{batch_input}_{sub_input}_{chap}")
                if chap_id in already_downloaded:
                    print(f"\n⏭️ Skipped Chapter (From Block): {chap}")
                    continue

                print(f"\n▶️ Processing Chapter: {chap}")
                page.goto(chapters_page_url)
                page.wait_for_load_state("networkidle")

                chap_card = page.locator(f"div:has-text('{chap}')").filter(has=page.locator("button, a:has-text('Open')")).last
                if not chap_card.is_visible(): continue
                chap_card.locator("button, a:has-text('Open')").first.click()
                page.wait_for_load_state("networkidle")
                dpp_list_url = page.url

                dpp_titles = page.evaluate("() => Array.from(document.querySelectorAll('button, a')).filter(b => b.innerText.match(/Start Test/i)).map(b => b.parentElement.innerText.split('\\n')[0])")
                dpp_count = page.locator('button:has-text("Start Test"), a:has-text("Start Test")').count()

                chapter_raw_dpps = []
                rel_folder = os.path.join(sanitize_filename(batch_input), sanitize_filename(sub_input))
                pdf_chap_folder = os.path.join(pdf_base, rel_folder, sanitize_filename(chap))
                os.makedirs(pdf_chap_folder, exist_ok=True)

                for d_idx in range(dpp_count):
                    dpp_name = dpp_titles[d_idx] if d_idx < len(dpp_titles) else f"DPP_{d_idx + 1}"
                    pdf_path = os.path.join(pdf_chap_folder, f"{sanitize_filename(dpp_name)}.pdf")
                    try:
                        page.locator('button:has-text("Start Test"), a:has-text("Start Test")').nth(d_idx).click()
                        page.locator("text=/Start Quiz/i").first.wait_for(state="visible", timeout=15000)
                        page.locator("text=/Start Quiz/i").first.click()
                        page.wait_for_load_state("domcontentloaded")
                        page.locator("text=/Submit/i").first.wait_for(state="visible", timeout=15000)
                        page.locator("text=/Submit/i").first.click(force=True)
                        page.wait_for_timeout(1000)
                        page.locator("text=/Confirm/i").first.click()
                        page.wait_for_selector("table tr", timeout=20000)

                        chapter_raw_dpps.append(page.evaluate(EXTRACTION_JS_DPP, {"dppName": dpp_name, "dppSlug": generate_slug(dpp_name)}))
                        generate_pdf(context, page.locator('button:has-text("Print Result")').first, pdf_path)
                    except Exception as e:
                        print(f"      ❌ Error on {dpp_name}: {e}")
                    
                    page.goto(dpp_list_url)
                    page.wait_for_load_state("networkidle")

                format_dest = os.path.join(format_base, rel_folder)
                os.makedirs(format_dest, exist_ok=True)
                json_filename = f"{sanitize_filename(chap)}.json"

                with open(os.path.join(format_dest, json_filename), "w", encoding="utf-8") as f:
                    if json_format == "original":
                        json.dump(chapter_raw_dpps, f, indent=2, ensure_ascii=False)
                    else:
                        json.dump(build_dpp_schema(chap, batch_input, chapter_raw_dpps), f, indent=2, ensure_ascii=False)

                already_downloaded.add(chap_id)
                session_new_downloads.append(chap)

        browser.close()

    # --- ZIP ---
    if os.path.exists(temp_dir) and any(os.scandir(temp_dir)):
        zip_file_name = f"{safe_cat_name}.zip"
        with zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), temp_dir))
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n=======================================================")
    print("                 DOWNLOAD SUMMARY                      ")
    print("=======================================================")
    print(f"Items Ignored (From Block): {len(already_downloaded) - len(session_new_downloads)}")
    print(f"Newly Downloaded: {len(session_new_downloads)}")
    
    new_compressed_archive = " ".join(already_downloaded)
    print("\n=======================================================")
    print("      ⬇️ YOUR NEW COMPRESSED ARCHIVE BLOCK ⬇️             ")
    print(" (Copy the text below and paste it into the script)    ")
    print("=======================================================\n")
    print(new_compressed_archive)
    print("\n=======================================================\n")

if __name__ == "__main__":
    main()