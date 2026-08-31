import streamlit as st
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import tempfile
import os
import time
import random
import re
import sys

USER_DATA_DIR = os.path.abspath("./deepseek_session")
PAGE_TIMEOUT_MS = 35000
UPLOAD_TIMEOUT_MS = 30000
GENERATION_TIMEOUT_SECONDS = 600


def human_delay(min_seconds=1.5, max_seconds=3.5):
    """Adds a randomized delay to simulate human hesitation."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def process_with_deepseek(pdf_path: str, prompt: str) -> str:
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

        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://chat.deepseek.com",
            )
        except Exception:
            pass

        try:
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.bring_to_front()
            print("Navigating to DeepSeek...")
            page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )
            human_delay(3.0, 5.0) # Human waiting for page to settle

            # -------------------------------------------------------------
            # STEP 1: Handle Cookie Banner & Check Login
            # -------------------------------------------------------------
            try:
                cookie_accept = page.locator(
                    ".cookie_banner-accept-all-button, .cookie_banner-accept-essential-button"
                ).first
                if cookie_accept.count() > 0 and cookie_accept.is_visible(timeout=2500):
                    human_delay(1.0, 2.0) 
                    cookie_accept.hover()
                    human_delay(0.5, 1.0) 
                    cookie_accept.click(delay=random.randint(50, 150))
                    print("Dismissed cookie banner.")
            except Exception:
                pass

            # Check if login form is displayed
            login_required = False
            try:
                auth_form = page.locator(
                    ".ds-sign-in-form-wrapper, input[placeholder*='Phone number / email address']"
                ).first
                login_required = auth_form.is_visible(timeout=2500)
            except Exception:
                pass

            if login_required:
                st.warning(
                    "Please log into DeepSeek in the opened browser window. Waiting up to 90s..."
                )
                try:
                    page.wait_for_selector(
                        "textarea[placeholder*='Message DeepSeek'], textarea",
                        state="visible",
                        timeout=90000,
                    )
                    st.success("DeepSeek login detected! Proceeding...")
                    human_delay(3.0, 5.0) 
                except Exception:
                    return "Error: DeepSeek login timed out or chat composer did not load."

            # Ensure the composer textarea is ready
            try:
                chat_textarea = page.locator(
                    "textarea[placeholder*='Message DeepSeek'], textarea"
                ).first
                chat_textarea.wait_for(state="visible", timeout=20000)
            except Exception:
                return "Error: Could not locate DeepSeek chat composer input."

            print("Resting before upload...")
            human_delay(2.0, 4.0)

            # -------------------------------------------------------------
            # STEP 2 & 3: File Upload (PDF)
            # -------------------------------------------------------------
            pdf_filename = os.path.basename(pdf_path)
            print(f"Uploading PDF: {pdf_filename}...")
            upload_success = False

            file_inputs = page.locator("input[type='file']").all()
            if file_inputs:
                for fi in file_inputs:
                    try:
                        human_delay(2.0, 3.5)
                        fi.set_input_files(pdf_path)
                        upload_success = True
                        print("Direct file input injection successful.")
                        break
                    except Exception:
                        pass

            # Fallback: Click paperclip icon to trigger file chooser
            if not upload_success:
                print("Attempting UI file chooser trigger...")
                try:
                    paperclip_btn = page.locator(
                        ".bf38813a div[role='button'], div[role='button'].f02f0e25"
                    ).first
                    if paperclip_btn.count() > 0:
                        paperclip_btn.hover()
                        human_delay(0.5, 1.5)
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            paperclip_btn.click(delay=random.randint(50, 150))
                        
                        human_delay(2.0, 4.0) 
                        fc_info.value.set_files(pdf_path)
                        upload_success = True
                except Exception as e:
                    print(f"UI file upload trigger failed: {e}")

            # Confirm file badge appeared in chat box
            print("Waiting for file to process...")
            try:
                page.locator("._25c7358, .e70accd6, [class*='file']").first.wait_for(
                    state="visible", timeout=UPLOAD_TIMEOUT_MS
                )
                print("File uploaded and confirmed visually.")
                human_delay(3.0, 5.0) 
            except Exception:
                print("File badge wait timed out; continuing with prompt.")

            # -------------------------------------------------------------
            # STEP 4: Input Prompt & Submit via OS clipboard simulation
            # -------------------------------------------------------------
            print("Preparing to write prompt...")
            human_delay(2.5, 4.5) 

            chat_input = page.locator(
                "textarea[placeholder*='Message DeepSeek'], textarea"
            ).last
            
            chat_input.hover()
            human_delay(0.5, 1.2)
            chat_input.click(delay=random.randint(60, 200))
            human_delay(1.0, 2.0)

            print("Pasting prompt via OS clipboard simulation...")
            page.evaluate("text => navigator.clipboard.writeText(text)", prompt)
            human_delay(0.5, 1.0)
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            page.keyboard.press(f"{modifier}+V", delay=random.randint(50, 150))
            
            print("Reviewing prompt...")
            human_delay(4.0, 6.0) 

            # Submit message
            submission_success = False
            for attempt in range(4):
                print(f"Hitting Send (Attempt {attempt + 1})...")
                page.keyboard.press("Enter", delay=random.randint(80, 200))
                human_delay(1.5, 2.5)

                try:
                    send_btn = page.locator(
                        "div[role='button'].ds-button--circle:not(._52c986b), div[role='button'].ds-button--primary.ds-button--circle"
                    ).last
                    if send_btn.is_visible(timeout=1500):
                        send_btn.hover()
                        human_delay(0.5, 1.0)
                        send_btn.click(delay=random.randint(60, 180))
                except Exception:
                    pass

                human_delay(3.0, 5.0)

                current_val = chat_input.evaluate(
                    "el => el.value || el.innerText || ''"
                )
                if not current_val or len(current_val.strip()) < 30:
                    submission_success = True
                    print("Prompt successfully submitted to DeepSeek!")
                    break

                print("Message didn't send. Retrying...")
                human_delay(2.0, 4.0)

            if not submission_success:
                return "Error: Could not submit the prompt to DeepSeek."

            # -------------------------------------------------------------
            # STEP 5: Wait for Generation (Silent Tracking & Continue Clicks)
            # -------------------------------------------------------------
            print("Waiting for generation to start...")
            human_delay(4.0, 6.0)

            last_text = ""
            stable_count = 0
            generation_started = time.monotonic()

            for iteration in range(GENERATION_TIMEOUT_SECONDS // 2):
                time.sleep(2)
                if time.monotonic() - generation_started > GENERATION_TIMEOUT_SECONDS:
                    return "Error: Generation timed out after 10 minutes."

                try:
                    continue_btn = page.locator("div[role='button']:has-text('Continue')").last
                    if continue_btn.is_visible(timeout=500):
                        print("DeepSeek paused. Clicking 'Continue'...")
                        continue_btn.hover()
                        human_delay(0.8, 1.5)
                        continue_btn.click(delay=random.randint(60, 150))
                        stable_count = 0 
                        human_delay(2.0, 3.0)
                        continue  
                except Exception:
                    pass

                current_text = ""
                try:
                    code_pre = page.locator(".md-code-block pre").all()
                    if code_pre:
                        current_text = code_pre[-1].inner_text(timeout=500)
                    else:
                        assistant_msgs = page.locator(".ds-markdown.ds-assistant-message-main-content, .ds-message").all()
                        if assistant_msgs:
                            current_text = assistant_msgs[-1].inner_text(timeout=500)
                except Exception:
                    pass

                is_generating = False
                try:
                    stop_indicators = page.locator("button:has-text('Stop'), [class*='stop'], .ds-icon--stop").all()
                    if any(si.is_visible(timeout=300) for si in stop_indicators if si.count() > 0):
                        is_generating = True
                except Exception:
                    pass

                if current_text and len(current_text) > 80:
                    if current_text == last_text:
                        if not is_generating:
                            stable_count += 1
                            print(f"Output Stable ({stable_count}/3) | Length: {len(current_text)} chars")
                            if stable_count >= 3:
                                print("Generation completed and completely stabilized.")
                                human_delay(2.0, 4.0)
                                break
                        else:
                            stable_count = 0
                    else:
                        stable_count = 0
                        last_text = current_text
                else:
                    print(f"Iteration {iteration}: Awaiting content stream...")

            # -------------------------------------------------------------
            # STEP 6: Final Single Extraction (One-time Copy Click)
            # -------------------------------------------------------------
            print("Extracting final payload via Copy button...")
            final_clipboard_text = ""
            
            try:
                code_banners = page.locator(".md-code-block-banner").all()
                if code_banners:
                    target_banner = code_banners[-1]
                    copy_btn = target_banner.locator("div[role='button']:has-text('Copy'), .code-info-button-text").first

                    if copy_btn.count() > 0:
                        copy_btn.scroll_into_view_if_needed()
                        human_delay(0.5, 1.5)
                        copy_btn.hover()
                        human_delay(0.5, 1.2)
                        
                        page.evaluate("() => navigator.clipboard && navigator.clipboard.writeText('')")
                        
                        copy_btn.click(delay=random.randint(50, 150), force=True)
                        print("Clicked 'Copy' button successfully.")
                        human_delay(0.5, 1.0) 

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
            except Exception as e:
                print(f"Copy button interaction failed: {e}")

            if final_clipboard_text and len(final_clipboard_text.strip()) > 50:
                last_text = final_clipboard_text.strip()
                print(f"Successfully extracted {len(last_text)} chars from clipboard.")
            else:
                print("Clipboard empty. Falling back to monitored DOM text.")

            # -------------------------------------------------------------
            # STEP 7: JSON Array Sanitization
            # -------------------------------------------------------------
            if last_text:
                last_text = re.sub(r"^`{3}(?:json)?\s*", "", last_text.strip(), flags=re.IGNORECASE)
                last_text = re.sub(r"\s*`{3}$", "", last_text.strip())

                start_idx = last_text.find("[")
                end_idx = last_text.rfind("]")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    last_text = last_text[start_idx : end_idx + 1]

            return last_text if last_text else "No response text captured."

        except Exception as e:
            return f"An error occurred during Playwright execution: {str(e)}"
        finally:
            context.close()


# --- STREAMLIT UI ---

st.set_page_config(page_title="DeepSeek PDF Extractor", layout="wide")
st.title("Exam PDF to DeepSeek JSON Extractor")
st.markdown("Automated batch question extraction via DeepSeek web UI automation.")

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

        with st.spinner("Processing PDF with DeepSeek... Keep an eye on the popup browser."):
            extracted_json = process_with_deepseek(tmp_pdf_path, prompt_text)

        st.success("Extraction Complete!")
        st.subheader("Extracted JSON Output:")
        st.code(extracted_json, language="json")

        if os.path.exists(tmp_pdf_path):
            try:
                os.remove(tmp_pdf_path)
            except PermissionError:
                pass