import streamlit as st
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import tempfile
import os
import time
import random
import re
import sys

USER_DATA_DIR = os.path.abspath("./perplexity_session")
PAGE_TIMEOUT_MS = 35000
GENERATION_TIMEOUT_SECONDS = 600

def human_delay(min_seconds=1.5, max_seconds=3.5):
    """Adds a randomized delay to simulate human hesitation."""
    time.sleep(random.uniform(min_seconds, max_seconds))

def process_with_perplexity(pdf_path: str, prompt: str) -> str:
    with sync_playwright() as p:
        # Launch persistent browser context to retain session/login tokens
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--enable-features=ClipboardAPI",
                "--enable-blink-features=ClipboardAPI",
                "--disable-blink-features=AutomationControlled"
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1366, "height": 850},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        page = context.pages[0] if context.pages else context.new_page()
        stealth_sync(page)

        # Grant clipboard permissions to avoid UI permission dialogs
        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://www.perplexity.ai"
            )
        except Exception:
            pass

        try:
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.bring_to_front()
            print("Navigating to Perplexity...")
            page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            human_delay(3.0, 5.0)

            # -------------------------------------------------------------
            # STEP 1: Handle Cookie Banner & Login
            # -------------------------------------------------------------
            try:
                cookie_btn = page.locator("button:has-text('Allow all'), button:has-text('Only necessary')").first
                if cookie_btn.is_visible(timeout=2500):
                    human_delay(1.0, 2.0)
                    cookie_btn.hover()
                    human_delay(0.5, 1.0)
                    cookie_btn.click(delay=random.randint(50, 150))
                    print("Dismissed cookie banner.")
            except Exception:
                pass

            # Check if login modal is blocking the screen
            login_required = False
            try:
                login_modal = page.locator("text='Sign up below to unlock the full potential of Perplexity'").first
                login_required = login_modal.is_visible(timeout=2500)
            except Exception:
                pass

            if login_required:
                st.warning("Please log into Perplexity in the opened browser window. Waiting up to 90s...")
                try:
                    page.wait_for_selector('#ask-input', state="visible", timeout=90000)
                    st.success("Perplexity login detected! Proceeding...")
                    human_delay(3.0, 5.0)
                except Exception:
                    return "Error: Perplexity login timed out or composer did not load."

            # -------------------------------------------------------------
            # STEP 2: File Upload (PDF)
            # -------------------------------------------------------------
            print("Uploading PDF...")
            human_delay(1.5, 3.0)
            page.locator('input[type="file"]').set_input_files(pdf_path)
            
            # Wait for the file to attach and render
            human_delay(3.0, 5.0) 

            # -------------------------------------------------------------
            # STEP 3: Input Prompt & Submit via OS clipboard simulation
            # -------------------------------------------------------------
            print("Preparing to write prompt...")
            ask_input = page.locator('#ask-input')
            ask_input.wait_for(state="visible", timeout=20000)
            
            ask_input.hover()
            human_delay(0.5, 1.2)
            ask_input.click(delay=random.randint(60, 200))
            human_delay(1.0, 2.0)

            print("Pasting prompt via OS clipboard simulation...")
            page.evaluate("text => navigator.clipboard.writeText(text)", prompt)
            human_delay(0.5, 1.0)
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            page.keyboard.press(f"{modifier}+V", delay=random.randint(50, 150))
            
            print("Reviewing prompt...")
            human_delay(3.0, 5.0)

            # Submit message with retry logic
            submission_success = False
            for attempt in range(3):
                print(f"Hitting Send (Attempt {attempt + 1})...")
                page.keyboard.press("Enter", delay=random.randint(80, 200))
                human_delay(2.0, 4.0)

                # Verify submission
                current_val = ask_input.inner_text()
                if not current_val or len(current_val.strip()) < 10 or page.url != "https://www.perplexity.ai/":
                    submission_success = True
                    print("Prompt successfully submitted!")
                    break
                
                print("Message didn't send. Retrying...")
                human_delay(2.0, 4.0)

            if not submission_success:
                return "Error: Could not submit the prompt."

            # -------------------------------------------------------------
            # STEP 4: Wait for Generation & Extract via Clipboard
            # -------------------------------------------------------------
            print("Waiting for generation to finish...")
            human_delay(4.0, 6.0)

            extracted_json = ""
            try:
                # Wait for the 'Copy code' button
                copy_btn = page.locator('button[aria-label="Copy code"]').last
                copy_btn.wait_for(state="visible", timeout=GENERATION_TIMEOUT_SECONDS * 1000)
                print("Generation complete. Reading output...")
                human_delay(2.0, 4.0)
                
                # Humanoid scroll & hover
                copy_btn.scroll_into_view_if_needed()
                human_delay(0.5, 1.5)
                copy_btn.hover()
                human_delay(0.5, 1.2)
                
                # Reset clipboard
                page.evaluate("() => navigator.clipboard && navigator.clipboard.writeText('')")
                
                # Click the copy button
                copy_btn.click(delay=random.randint(50, 150))
                print("Clicked 'Copy code' button.")
                human_delay(0.5, 1.0)

                # Read from clipboard
                extracted_json = page.evaluate("""async () => {
                    try {
                        if (navigator.clipboard && navigator.clipboard.readText) {
                            return await navigator.clipboard.readText();
                        }
                        return "";
                    } catch (e) {
                        return "";
                    }
                }""")

                if not extracted_json or len(extracted_json.strip()) < 10:
                    print("Clipboard empty. Falling back to DOM extraction...")
                    extracted_json = page.locator('pre code').last.inner_text()

            except Exception as e:
                print(f"Timeout or error interacting with Copy button: {e}")
                print("Falling back to DOM extraction...")
                try:
                    extracted_json = page.locator('pre code').last.inner_text()
                except Exception:
                    pass

            # -------------------------------------------------------------
            # STEP 5: JSON Sanitization
            # -------------------------------------------------------------
            if extracted_json:
                extracted_json = re.sub(r"^`{3}(?:json)?\s*", "", extracted_json.strip(), flags=re.IGNORECASE)
                extracted_json = re.sub(r"\s*`{3}$", "", extracted_json.strip())

                start_idx = extracted_json.find("[")
                end_idx = extracted_json.rfind("]")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    extracted_json = extracted_json[start_idx : end_idx + 1]

            return extracted_json if extracted_json else "No response text captured."

        except Exception as e:
            return f"An error occurred: {str(e)}"
        finally:
            context.close()

# --- STREAMLIT UI ---

st.set_page_config(page_title="Perplexity PDF Extractor", layout="wide")
st.title("Perplexity JSON Extractor")
st.markdown("Automated batch question extraction via Perplexity web UI automation.")

with st.sidebar:
    st.header("Extraction Prompt")
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

    prompt_text = st.text_area("Extraction Prompt", value=default_prompt, height=320)

uploaded_file = st.file_uploader("Upload Exam PDF", type=["pdf"])

if st.button("Start Extraction", type="primary"):
    if not uploaded_file:
        st.error("Please upload a PDF first.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(uploaded_file.read())
            tmp_pdf_path = tmp_file.name

        with st.spinner("Processing PDF with Perplexity... Keep an eye on the popup browser."):
            extracted_json = process_with_perplexity(tmp_pdf_path, prompt_text)

        st.success("Extraction Complete!")
        st.subheader("Extracted JSON Output:")
        st.code(extracted_json, language="json")

        if os.path.exists(tmp_pdf_path):
            try:
                os.remove(tmp_pdf_path)
            except PermissionError:
                pass