import streamlit as st
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import tempfile
import os
import time
import random
import sys

# Configuration constants
USER_DATA_DIR = os.path.abspath("./grok_session")
PAGE_TIMEOUT_MS = 45000
UPLOAD_TIMEOUT_MS = 35000
GENERATION_TIMEOUT_SECONDS = 600


def human_delay(min_seconds=1.5, max_seconds=3.5):
    """Simulates realistic human hesitation."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def process_with_grok(pdf_path: str, prompt: str) -> str:
    with Stealth().use_sync(sync_playwright()) as p:
        # Launch persistent browser context
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--enable-features=ClipboardAPI",
                "--enable-blink-features=ClipboardAPI",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-position=0,0",
                "--ignore-certificate-errors",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1366, "height": 850},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        page = context.pages[0] if context.pages else context.new_page()

        try:
            # -------------------------------------------------------------
            # STEP 1: Open Grok & Verify Login
            # -------------------------------------------------------------
            page.goto("https://grok.com/", timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            human_delay(2.0, 3.5)

            interface_ready = False
            try:
                page.wait_for_selector(
                    'textarea, div[data-testid="chat-input"], [placeholder*="How can I help you"]', 
                    state="visible", 
                    timeout=5000
                )
                interface_ready = True
            except Exception:
                pass

            if not interface_ready:
                st.warning("⚠️ Manual action needed: Please complete login in the opened browser window.")
                try:
                    page.wait_for_selector(
                        'textarea, div[data-testid="chat-input"], [placeholder*="How can I help you"]', 
                        state="visible", 
                        timeout=180000 
                    )
                    st.success("Login successful! Proceeding...")
                    human_delay(3.0, 5.0)
                except Exception:
                    return "Error: Timed out waiting for login or the chat interface did not load."

            # Dismiss optional banners
            try:
                dismiss_btn = page.locator('button:has-text("Dismiss")').first
                if dismiss_btn.is_visible(timeout=3000):
                    dismiss_btn.click(delay=random.randint(50, 150))
                    human_delay(0.5, 1.0)
            except Exception:
                pass

            # -------------------------------------------------------------
            # STEP 2: Hybrid Humanoid Upload (UI Click First -> Fallback to Direct)
            # -------------------------------------------------------------
            print("Attempting humanoid file upload...")
            upload_success = False

            try:
                # Try clicking the attach button like a human
                attach_btn = page.locator('button[aria-label="Attach"], button[data-testid="attach-button"]').first
                attach_btn.wait_for(state="visible", timeout=5000)
                
                attach_btn.hover()
                human_delay(0.5, 1.0)
                attach_btn.click(delay=random.randint(50, 150))
                human_delay(0.8, 1.5)

                # Look for 'Upload a file' option in the menu
                upload_menu_item = page.locator('text="Upload a file"').first
                
                with page.expect_file_chooser(timeout=5000) as fc_menu:
                    upload_menu_item.wait_for(state="visible", timeout=3000)
                    upload_menu_item.hover()
                    human_delay(0.3, 0.8)
                    upload_menu_item.click(delay=random.randint(50, 150))
                
                human_delay(1.0, 2.0) # Simulate human OS selection pause
                fc_menu.value.set_files(pdf_path)
                upload_success = True
                print("Humanoid menu upload successful.")
            except Exception:
                print("Humanoid menu click failed. Falling back to direct hidden input injection...")

            # Fallback mechanism if UI click approach failed
            if not upload_success:
                try:
                    file_input = page.locator('input[type="file"][name="files"]').first
                    file_input.wait_for(state="attached", timeout=3000)
                    file_input.set_input_files(pdf_path)
                    print("File uploaded successfully via direct injection fallback.")
                except Exception as e:
                    return f"Error: All file upload methods failed: {e}"

            # Wait for the file attachment badge to render
            print("Waiting for file to attach...")
            try:
                page.wait_for_selector(
                    'div[role="list"][aria-label*="attachment"], [class*="attachment"]:not(.hidden)',
                    timeout=UPLOAD_TIMEOUT_MS,
                )
            except Exception:
                pass 
            human_delay(2.0, 4.0)

            # -------------------------------------------------------------
            # STEP 3: Strict Humanoid Prompt Pasting
            # -------------------------------------------------------------
            print("Copying prompt to clipboard and pasting...")
            
            page.evaluate("async (text) => await navigator.clipboard.writeText(text)", prompt)
            
            chat_input = page.locator(
                'textarea, div[data-testid="chat-input"] .ProseMirror, div[contenteditable="true"][role="textbox"]'
            ).first
            
            chat_input.hover()
            human_delay(0.5, 1.0)
            chat_input.click(delay=random.randint(50, 150))
            human_delay(1.0, 2.0)

            modifier = "Meta" if sys.platform == "darwin" else "Control"
            page.keyboard.press(f"{modifier}+v", delay=random.randint(100, 200))
            human_delay(2.0, 4.0)

            print("Hitting Send...")
            page.keyboard.press("Enter", delay=random.randint(80, 200))
            human_delay(3.0, 5.0)

            # -------------------------------------------------------------
            # STEP 4: Monitor Generation & Humanoid Copy Button Click
            # -------------------------------------------------------------
            print("Waiting for Grok to start generating the JSON code block...")
            
            code_block = page.locator('pre.shiki').last
            
            try:
                code_block.wait_for(state="visible", timeout=60000)
                print("Code block detected! Monitoring output...")
            except Exception:
                return "Error: Timed out waiting for Grok to generate a code block."

            start_time = time.time()
            last_text = ""
            stable_count = 0
            
            while time.time() - start_time < GENERATION_TIMEOUT_SECONDS:
                try:
                    current_text = code_block.inner_text()
                    
                    if int(time.time()) % 3 == 0:
                        print(f"Tracking Grok's output... Currently at {len(current_text)} characters.")
                        
                    if current_text and len(current_text) > 50:
                        if current_text == last_text:
                            stable_count += 1
                            if stable_count >= 4:  # Output remained stable for ~6 seconds
                                print("Generation complete!")
                                break
                        else:
                            stable_count = 0
                            last_text = current_text
                except Exception:
                    pass
                time.sleep(1.5)

            # Human pause before reading and clicking copy
            human_delay(2.5, 4.5)

            print("Extracting via humanoid Copy button click...")
            final_clipboard_text = ""
            try:
                # Locate the SVG vector path for the copy button
                copy_btn = page.locator('button:has(svg path[d*="-5.155700"])').last

                if copy_btn.count() > 0:
                    copy_btn.scroll_into_view_if_needed()
                    human_delay(0.5, 1.2)
                    copy_btn.hover()
                    human_delay(0.5, 1.0)
                    
                    page.evaluate("() => navigator.clipboard && navigator.clipboard.writeText('')")
                    
                    copy_btn.click(delay=random.randint(50, 150), force=True)
                    print("Clicked the SVG 'Copy' button successfully.")
                    human_delay(1.5, 2.5)

                    final_clipboard_text = page.evaluate("""async () => {
                        try {
                            if (navigator.clipboard && navigator.clipboard.readText) {
                                return await navigator.clipboard.readText();
                            }
                            return "";
                        } catch (e) {
                            return "";
                        }
                    }""")
                else:
                    print("SVG Copy button not found.")
            except Exception as e:
                print(f"Copy button interaction failed: {e}")

            # Assign extracted text or use DOM text fallback
            if final_clipboard_text and len(final_clipboard_text.strip()) > 50:
                extracted_json = final_clipboard_text.strip()
                print(f"Successfully extracted {len(extracted_json)} chars from clipboard.")
            else:
                print("Using safe DOM text fallback.")
                extracted_json = last_text

            # -------------------------------------------------------------
            # STEP 5: Clean JSON formatting fences & simulate natural typing out
            # -------------------------------------------------------------
            cleaned_json = extracted_json.strip()
            if cleaned_json.startswith("```json"):
                cleaned_json = cleaned_json[7:]
            elif cleaned_json.startswith("```"):
                cleaned_json = cleaned_json[3:]
            if cleaned_json.endswith("```"):
                cleaned_json = cleaned_json[:-3]

            return cleaned_json.strip()

        finally:
            context.close()


# ---------------- Streamlit UI ----------------
st.set_page_config(page_title="Grok PDF Parser", page_icon="⚡", layout="wide")
st.title("⚡ Grok MCQ & Exam PDF Extractor")

with st.expander("🛠️ Prompt Settings", expanded=False):
    default_prompt = r"""You are an expert exam-paper and multiple-choice-question parser.

Extract ALL multiple-choice questions that actually exist in the provided PDF page(s).

## ABSOLUTE OUTPUT CONTRACT — FOLLOW THIS EXACTLY
1. Your entire response MUST contain exactly ONE Markdown fenced code block.
2. The code-block opening MUST be exactly: ```json
3. The code-block closing MUST be exactly: ```
4. Put the complete JSON array between those two lines.
5. Output NOTHING before the opening fence and NOTHING after the closing fence.
6. Do not write explanations, reasoning, notes, headings, comments, apologies, or status messages.
7. The JSON must be complete, valid, and parseable by Python `json.loads()`. Never output truncated JSON.
8. Use double quotes for every JSON key and every JSON string. Never use single quotes.
9. Escape every backslash inside JSON strings.
10. Do not render formulas as visual math. Keep formulas as plain text inside JSON strings.
11. Do not invent, solve, correct, or infer information that is not supported by the PDF.

## QUESTION EXTRACTION RULES
1. Extract ONLY questions that actually exist in the PDF. Never invent or infer missing questions.
2. Preserve question numbering exactly as printed in the PDF in "n". If numbering restarts in a new exercise, preserve that new numbering.
3. Extract the question text ("q") and all 4 options ("o") faithfully from the PDF.
4. For linked or paragraph-based questions, prepend the common paragraph to the "q" field.
5. Use `<br />` inside string values to represent meaningful line breaks.
6. Do not include page numbers, watermarks, URLs, repeated headers, or footer text in question content.

## MATCH-THE-COLUMN QUESTIONS — STRUCTURE OF "m"
For match questions, use structured object in "m":
{
  "listI": {
    "title": "List-I",
    "items": [
      { "label": "A", "text": "..." },
      { "label": "B", "text": "..." },
      { "label": "C", "text": "..." },
      { "label": "D", "text": "..." }
    ]
  },
  "listII": {
    "title": "List-II",
    "items": [
      { "label": "P", "text": "..." },
      { "label": "Q", "text": "..." },
      { "label": "R", "text": "..." },
      { "label": "S", "text": "..." }
    ]
  }
}
For non-match questions, set "m": null.
The "o" array contains the 4 combination choices: ["(A) -> (P), (B) -> (Q), (C) -> (R), (D) -> (S)", ...].

## EXERCISE AND TOPIC RULES
- "exnm": Exercise name/heading printed in the PDF (e.g. "Exercise - I (Conceptual Questions)").
- "top": Explicit topic heading from the PDF (e.g. "QUESTIONS BASED ON MOLES"). If no subheadings exist under an exercise, use "top": "general".
- Never invent topics from question subject matter.

## FORMATTING RULES
1. For Chemistry formulas and reactions, use LaTeX mhchem syntax: \\ce{H2O}, \\ce{KMnO4 + HCl -> KCl + MnCl2 + H2O + Cl2}, \\ce{Fe^{2+}}.
2. For Physics/Math, use LaTeX: $\\frac{1}{2}$, $6.02 \\times 10^{23}$.
3. SMILES string in "s" if 2D organic structure exists; else "s": null.
4. Set "d": true if diagram needs cropping; else "d": false.

## JSON KEYS AND HIERARCHY RULES
1. Use only these keys: "n", "q", "o", "a", "d", "s", "m", "sub", "top", "exnm".
2. Do NOT use "subtop".
3. Do NOT use "e" (no explanations in PDF).
4. "sub": "CHEMISTRY" (or relevant subject).
5. "a": Zero-based integer index of correct option (0, 1, 2, 3) from PDF answer key, or null if no key.

## FINAL SELF-CHECK BEFORE RESPONDING
- Starts with ```json and ends with ```.
- Exactly ONE fenced code block with valid JSON array.
- Output ONLY the JSON code block."""

    prompt_text = st.text_area("Extraction Prompt", value=default_prompt, height=280)

uploaded_file = st.file_uploader("Upload Exam PDF", type=["pdf"])

if st.button("Start Extraction", type="primary"):
    if not uploaded_file:
        st.error("Please upload a PDF file first.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(uploaded_file.read())
            tmp_pdf_path = tmp_file.name

        with st.spinner("Processing PDF with Grok... Keep an eye on the browser session."):
            extracted_json = process_with_grok(tmp_pdf_path, prompt_text)

        st.success("Extraction Complete!")
        st.subheader("Extracted JSON Output:")
        st.code(extracted_json, language="json")

        if os.path.exists(tmp_pdf_path):
            os.remove(tmp_pdf_path)