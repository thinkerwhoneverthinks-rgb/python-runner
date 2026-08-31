import streamlit as st
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import tempfile
import os
import time
import random
import re
import sys

USER_DATA_DIR = os.path.abspath("./qwen_session")
PAGE_TIMEOUT_MS = 30000
UPLOAD_TIMEOUT_MS = 25000
GENERATION_TIMEOUT_SECONDS = 600


def human_delay(min_seconds=1.0, max_seconds=3.0):
    time.sleep(random.uniform(min_seconds, max_seconds))

def process_with_qwen(pdf_path: str, prompt: str) -> str:
    with sync_playwright() as p:
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
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.pages[0] if context.pages else context.new_page()
        stealth_sync(page)
        
        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://chat.qwen.ai"
            )
        except Exception:
            pass

        try:
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.bring_to_front()
            print("Opening Qwen...")
            page.goto("https://chat.qwen.ai/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(3000)

            # 1. Check if login is required, but never wait indefinitely.
            login_required = False
            try:
                login_required = page.get_by_role("button", name=re.compile("log in|sign in", re.I)).first.is_visible(timeout=2500)
            except Exception:
                pass

            if login_required:
                st.warning("Please log into Qwen in the popup browser. Waiting up to 90 seconds...")
                try:
                    page.wait_for_function(
                        """() => !!document.querySelector(\"textarea, [contenteditable='true']\")""",
                        timeout=90000
                    )
                    st.success("Qwen is ready. Proceeding with extraction...")
                except Exception:
                    return "Error: Qwen login was not completed or the chat input did not load within 90 seconds."

            # Qwen can render the composer as either a textarea or contenteditable div.
            try:
                page.wait_for_selector("textarea, [contenteditable='true']", state="visible", timeout=30000)
            except Exception:
                return "Error: Qwen opened, but its chat input did not load. Check the popup browser and try again."

            human_delay(1.0, 2.0)

            # 2. Upload the PDF
            pdf_filename = os.path.basename(pdf_path)
            upload_success = False

            print("Attempting to upload file...")

            file_inputs = page.locator("input[type='file']").all()
            if file_inputs:
                print("Hidden file input(s) found. Injecting PDF...")
                for fi in file_inputs:
                    try:
                        fi.set_input_files(pdf_path)
                    except Exception:
                        pass

                human_delay(2.0, 3.0)
                if page.get_by_text(pdf_filename[:5]).count() > 0 or page.locator("[class*='file'], [class*='attachment']").count() > 0:
                    print("Upload visually confirmed via direct injection!")
                    upload_success = True

            if not upload_success:
                print("Direct injection unconfirmed. Attempting precise UI click strategy...")
                try:
                    chat_box = page.locator("textarea").last
                    chat_container = chat_box.locator("xpath=ancestor::div[position()<=5]").last
                    potential_svgs = chat_container.locator("xpath=.//*[local-name()='svg']").all()

                    for svg in potential_svgs:
                        try:
                            html_content = svg.evaluate("el => el.outerHTML.toLowerCase()")
                            if any(kw in html_content for kw in ["attachment", "upload", "add", "plus"]):
                                clickable_target = svg.locator("xpath=ancestor::button | ancestor::div[contains(@class, 'btn') or contains(@class, 'icon')] | ancestor::span").first
                                if clickable_target.count() == 0:
                                    clickable_target = svg

                                try:
                                    with page.expect_file_chooser(timeout=3000) as fc_info:
                                        clickable_target.click(force=True)
                                    fc_info.value.set_files(pdf_path)
                                    print("File chooser intercepted directly from button!")
                                    upload_success = True
                                    break
                                except Exception:
                                    pass 

                                human_delay(1.0, 1.5)
                                menu_options = [
                                    page.get_by_text("Upload attachment", exact=False),
                                    page.get_by_text("attachment", exact=False),
                                    page.get_by_text("file", exact=False),
                                    page.get_by_text("Upload", exact=False)
                                ]

                                menu_clicked = False
                                for option in menu_options:
                                    if option.count() > 0 and option.first.is_visible():
                                        print("Dropdown menu detected! Clicking...")
                                        with page.expect_file_chooser(timeout=4000) as fc_info:
                                            option.first.click(force=True)
                                        fc_info.value.set_files(pdf_path)
                                        print("File chooser intercepted from menu!")
                                        upload_success = True
                                        menu_clicked = True
                                        break

                                if menu_clicked:
                                    break
                        except Exception:
                            continue

                except Exception as e:
                    print(f"SVG interaction failed: {e}")

            print("Patiently waiting for the file to upload over the network...")
            try:
                page.get_by_text(pdf_filename[:8]).first.wait_for(state="visible", timeout=UPLOAD_TIMEOUT_MS)
                print("File badge appeared! Waiting briefly for backend processing...")
                page.wait_for_timeout(5000)
            except Exception:
                print("Could not visually confirm the file name within 25 seconds; proceeding.")
                page.wait_for_timeout(2000)

            human_delay(1.5, 2.5)

            # 3. Enter Prompt via OS clipboard simulation
            print("Typing prompt...")
            chat_input = page.locator("textarea, [contenteditable='true']").last
            chat_input.hover()
            human_delay(0.5, 1.2)
            chat_input.click(delay=random.randint(50, 150)) 
            human_delay(0.5, 1.5)

            page.evaluate("text => navigator.clipboard.writeText(text)", prompt)
            human_delay(0.5, 1.0)
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            page.keyboard.press(f"{modifier}+V", delay=random.randint(50, 150))
            
            human_delay(2.0, 4.0)

            # 4. Submit Question
            submission_success = False
            for attempt in range(5): 
                page.keyboard.press("Enter", delay=random.randint(80, 200))
                human_delay(1.5, 2.5)

                try:
                    chat_box = page.locator("textarea").last
                    chat_container = chat_box.locator("xpath=ancestor::div[position()<=5]").last
                    send_svgs = chat_container.locator("xpath=.//*[local-name()='svg']").all()

                    send_clicked = False
                    for s_icon in reversed(send_svgs):
                        try:
                            if s_icon.is_visible():
                                html_content = s_icon.evaluate("el => el.outerHTML.toLowerCase()")
                                if "send" in html_content or "plane" in html_content:
                                    s_icon.click(force=True)
                                    send_clicked = True
                                    break
                        except Exception:
                            pass

                    if not send_clicked:
                        send_btn = page.locator("button:has(svg)").last
                        if send_btn.is_visible(timeout=2000):
                            send_btn.click(delay=random.randint(50, 150))
                except Exception:
                    pass

                human_delay(4.0, 6.0)

                current_input_text = chat_input.evaluate("el => el.value || el.innerText || el.textContent || ''")
                if not current_input_text or len(current_input_text.strip()) < 50:
                    submission_success = True
                    print("Submission accepted by Qwen!")
                    break 

                print(f"Submission attempt {attempt + 1} ignored by Qwen. Retrying...")
                human_delay(5.0, 8.0) 

            if not submission_success:
                return "Error: Could not submit the prompt. The website rejected the send button."

            # ============================================
            # 5. WAIT FOR GENERATION & EXTRACT TEXT
            # ============================================
            print("Waiting for generation to start...")
            time.sleep(5)

            last_text = ""
            stable_count = 0
            regenerations_attempted = 0

            generation_started = time.monotonic()
            for iteration in range(GENERATION_TIMEOUT_SECONDS // 2):
                time.sleep(2)
                if time.monotonic() - generation_started > GENERATION_TIMEOUT_SECONDS:
                    return "Error: Qwen did not finish generating within 10 minutes."

                current_text = ""

                # --- METHOD 1: Try Copy button + clipboard ---
                try:
                    assistant_msgs = page.locator(".qwen-chat-message.qwen-chat-message-assistant").all()
                    if assistant_msgs and len(assistant_msgs) > 0:
                        last_msg = assistant_msgs[-1]

                        copy_candidates = [
                            last_msg.get_by_role("button", name=re.compile(r"^copy", re.I)).first,
                            last_msg.locator("button[aria-label^='Copy']").first,
                            last_msg.locator("button[title^='Copy']").first,
                            last_msg.locator("[class*='copy']").first,
                        ]

                        for copy_btn in copy_candidates:
                            if copy_btn.count() > 0 and copy_btn.is_visible(timeout=1000):
                                page.evaluate("""async () => {
                                    if (navigator.clipboard) {
                                        await navigator.clipboard.writeText('');
                                    }
                                }""")

                                copy_btn.click(force=True)
                                page.wait_for_timeout(500)
                                human_delay(1.5, 2.5)

                                clipboard_text = page.evaluate("""async () => {
                                    try {
                                        if (navigator.clipboard && navigator.clipboard.readText) {
                                            const text = await navigator.clipboard.readText();
                                            return text || "";
                                        }
                                        return "";
                                    } catch (e) {
                                        return "";
                                    }
                                }""")

                                if clipboard_text and len(clipboard_text.strip()) > 100:
                                    current_text = clipboard_text.strip()
                                    print(f"Method 1 (Copy): {len(current_text)} chars")
                                    break

                        if current_text:
                            pass 
                except Exception as e:
                    print(f"Method 1 failed: {e}")

                # --- METHOD 2: Get raw text from markdown paragraphs and reconstruct ---
                if not current_text or len(current_text) < 100:
                    try:
                        raw_text = page.evaluate("""() => {
                            const assistants = document.querySelectorAll('.qwen-chat-message.qwen-chat-message-assistant');
                            if (assistants.length === 0) return "";
                            const last = assistants[assistants.length - 1];

                            let result = [];
                            const walk = (node) => {
                                if (node.nodeType === Node.TEXT_NODE) {
                                    const text = node.textContent;
                                    if (text && text.trim()) {
                                        result.push(text);
                                    }
                                } else if (node.nodeType === Node.ELEMENT_NODE) {
                                    if (node.getAttribute('aria-hidden') === 'true') return;
                                    if (node.classList && node.classList.contains('katex-mathml')) return;

                                    if (node.classList && node.classList.contains('katex-html')) {
                                        const text = node.textContent;
                                        if (text) result.push(text);
                                        return;
                                    }

                                    if (node.classList && node.classList.contains('qwen-markdown-escape')) {
                                        const text = node.textContent;
                                        if (text) result.push(text);
                                        return;
                                    }

                                    for (let child of node.childNodes) {
                                        walk(child);
                                    }
                                }
                            };

                            walk(last);
                            return result.join(' ');
                        }""")

                        if raw_text and len(raw_text.strip()) > 100:
                            current_text = raw_text.strip()
                            print(f"Method 2 (DOM walk): {len(current_text)} chars")
                    except Exception as e:
                        print(f"Method 2 failed: {e}")

                # --- METHOD 3: Simple paragraph concatenation (last resort) ---
                if not current_text or len(current_text) < 100:
                    try:
                        paragraphs = page.locator(".qwen-markdown-paragraph").all()
                        if paragraphs and len(paragraphs) > 0:
                            texts = []
                            for p in paragraphs:
                                try:
                                    t = p.inner_text(timeout=1000)
                                    if t and t.strip():
                                        texts.append(t.strip())
                                except Exception:
                                    pass
                            if texts:
                                current_text = "\n".join(texts)
                                print(f"Method 3 (Paragraphs): {len(current_text)} chars")
                    except Exception as e:
                        print(f"Method 3 failed: {e}")

                # --- SELF-HEALING: Detect generation errors ---
                if current_text and any(err in current_text.lower() for err in ["please regenerate", "content is empty", "network error", "something went wrong"]):
                    if regenerations_attempted < 2:
                        print("Detected Qwen generation error. Attempting to click Regenerate...")
                        try:
                            regen_selectors = [
                                "svg use[*|href*='qwpcicon-regenerate']",
                                "button:has-text('Regenerate')",
                                "[class*='regenerate']",
                                "svg[class*='regenerate']"
                            ]
                            for rsel in regen_selectors:
                                rbtn = page.locator(rsel).first
                                if rbtn.count() > 0 and rbtn.is_visible(timeout=2000):
                                    rbtn.click(force=True)
                                    print("Regenerate clicked!")
                                    break

                            regenerations_attempted += 1
                            human_delay(4.0, 6.0)
                            last_text = ""
                            stable_count = 0
                            continue 
                        except Exception as e:
                            print(f"Could not click regenerate: {e}")
                    else:
                        return "Error: Qwen repeatedly failed to generate content, even after multiple regenerate attempts."

                # --- DETECT IF GENERATING ---
                is_generating = False
                try:
                    stop_selectors = [
                        "button:has-text('Stop')",
                        "[class*='stop']",
                        "svg[class*='stop']",
                        "[class*='generating']",
                        "[class*='loading']"
                    ]
                    for ssel in stop_selectors:
                        try:
                            sbtn = page.locator(ssel).last
                            if sbtn.count() > 0:
                                visible = sbtn.is_visible(timeout=500)
                                if visible:
                                    is_generating = True
                                    break
                        except Exception:
                            continue
                except Exception:
                    pass

                try:
                    dots = page.locator("[class*='dot'], [class*='pulse'], [class*='typing']").all()
                    if any(d.is_visible(timeout=300) for d in dots if d.count() > 0):
                        is_generating = True
                except Exception:
                    pass

                # --- STABILITY CHECK ---
                if current_text and len(current_text) > 100:
                    if current_text == last_text:
                        if not is_generating:
                            stable_count += 1
                            print(f"Stable {stable_count}/3 | {len(current_text)} chars | Generating: {is_generating}")
                            if stable_count >= 3:
                                print("\nDone! Generation complete.")
                                break
                        else:
                            print(f"Stable but still generating... | {len(current_text)} chars")
                            stable_count = 0
                    else:
                        print(f"Growing... {len(current_text)} chars")
                        stable_count = 0
                        last_text = current_text
                else:
                    print(f"Iteration {iteration}: No substantial text yet...")

            # --- POST-PROCESSING: Clean up the extracted text ---
            if last_text:
                start_idx = last_text.find('[')
                end_idx = last_text.rfind(']')
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    last_text = last_text[start_idx:end_idx+1]

                cleaned = last_text

                pattern = r'(\d+\.?\d*)\s*\n\s*t\s*\n\s*i\s*\n\s*m\s*\n\s*e\s*\n\s*s\s*\n\s*10\s*\n\s*(\d+)\s*\n\s*\1\s*\n\s*times10\s*\n\s*\2'

                def replace_math(match):
                    num = match.group(1)
                    exp = match.group(2)
                    return f"{num} \\times 10^{{{exp}}}"

                cleaned = re.sub(pattern, replace_math, cleaned, flags=re.IGNORECASE)

                cleaned = re.sub(r'(\d+\.?\d*)\s*\n\s*t\s*\n\s*i\s*\n\s*m\s*\n\s*e\s*\n\s*s\s*\n\s*10\s*\n\s*(\d+)', 
                                lambda m: f"{m.group(1)} \\times 10^{{{m.group(2)}}}", 
                                cleaned, flags=re.IGNORECASE)

                cleaned = re.sub(r'N\s*\n\s*A\s*\n\s*N\s*\n\s*A', 'N_A', cleaned)
                cleaned = re.sub(r'N\s*\n\s*A', 'N_A', cleaned)
                cleaned = re.sub(r'f\s*\n\s*r\s*\n\s*a\s*\n\s*c', '\\frac', cleaned)
                cleaned = re.sub(r'"\s*\n+\s*"', '" "', cleaned)

                lines = cleaned.split('\n')
                filtered_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped in ['t', 'i', 'm', 'e', 's', 'f', 'r', 'a', 'c', 'N', 'A', 'n', 'g', 'h']:
                        continue
                    filtered_lines.append(line)

                cleaned = '\n'.join(filtered_lines)
                last_text = cleaned

            return last_text if last_text else "No response text captured."

        except Exception as e:
            return f"An error occurred during Playwright execution: {str(e)}"
        finally:
            context.close()

# --- STREAMLIT UI ---

st.set_page_config(page_title="Qwen PDF Extractor", layout="wide")
st.title("PDF to Qwen JSON Extractor")
st.markdown("Automate extraction with randomized, human-like interactions and submission retries.")

with st.sidebar:
    st.header("Extraction Configuration")
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

    prompt_text = st.text_area("Prompt", value=default_prompt, height=320)

uploaded_file = st.file_uploader("Upload Exam PDF", type=["pdf"])

if st.button("Start Extraction", type="primary"):
    if not uploaded_file:
        st.error("Please upload a PDF first.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(uploaded_file.read())
            tmp_pdf_path = tmp_file.name

        with st.spinner("Processing PDF with Qwen... Keep an eye on the popup browser."):
            extracted_json = process_with_qwen(tmp_pdf_path, prompt_text)

        st.success("Extraction Complete!")
        st.subheader("Extracted Output:")
        st.code(extracted_json, language="json")

        if os.path.exists(tmp_pdf_path):
            try:
                os.remove(tmp_pdf_path)
            except PermissionError:
                pass