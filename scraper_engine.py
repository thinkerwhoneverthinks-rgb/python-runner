"""Playwright AI Web Scraper Engine for DeepSeek, Perplexity, and Qwen.

Integrates browser automation to parse PDF chunks without relying on direct API keys.
Supports:
- Multi-part prompts (Prompt 1..4) pasted sequentially into composer to prevent text-to-file conversions.
- Load balancing with max 2 consecutive chunks per AI provider.
- Automatic fallback chain across selected AI engines.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(page: Any):
        pass

# Base paths for browser sessions
BASE_DIR = Path(__file__).parent
SCRAPER_DIR = BASE_DIR.parent / "ai-scraper"

def get_session_dir(name: str) -> Path:
    local_p = BASE_DIR / name
    if local_p.exists():
        return local_p
    fallback_p = SCRAPER_DIR / name
    if fallback_p.exists():
        return fallback_p
    return local_p

DEEPSEEK_SESSION = get_session_dir("deepseek_session")
PERPLEXITY_SESSION = get_session_dir("perplexity_session")
QWEN_SESSION = get_session_dir("qwen_session")
GROK_SESSION = get_session_dir("grok_session")

PAGE_TIMEOUT_MS = 35000
UPLOAD_TIMEOUT_MS = 30000
GENERATION_TIMEOUT_SECONDS = 600


def human_delay(min_seconds: float = 1.5, max_seconds: float = 3.5):
    """Adds a randomized delay to simulate human typing and hesitation."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def parse_clean_json(raw_text: str) -> List[Dict[str, Any]]:
    """Strips markdown code fences and parses JSON safely."""
    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    clean = clean.strip()

    try:
        data = json.loads(clean)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("questions", "data", "items", "results"):
                if k in data and isinstance(data[k], list):
                    return data[k]
            return [data]
    except Exception:
        # Fallback regex extraction of JSON objects
        json_objs = re.findall(r'\{[^{}]*"n"\s*:\s*\d+.*?\}', clean, re.DOTALL)
        res = []
        for jo in json_objs:
            try:
                res.append(json.loads(jo))
            except Exception:
                pass
        if res:
            return res
        raise ValueError(f"Could not parse JSON response from scraper.\nRaw preview: {clean[:200]}")
    return []


def kill_stale_chrome_processes():
    """Kills lingering Chrome/Chromium zombie processes on Linux runners."""
    if sys.platform != "win32":
        try:
            import subprocess
            subprocess.run(["pkill", "-9", "-f", "chrome"], capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "chromium"], capture_output=True)
            time.sleep(1)
        except Exception:
            pass


def cleanup_session_locks(session_dir: Path):
    """Removes stale Chromium lock files (SingletonLock, LOCK) created by previous browser runs."""
    kill_stale_chrome_processes()
    if not session_dir.exists():
        return
    lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
    try:
        for item in session_dir.glob("*"):
            if item.name in lock_names or item.name.startswith("Singleton"):
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                except Exception:
                    pass
        default_dir = session_dir / "Default"
        if default_dir.exists():
            for item in default_dir.glob("LOCK*"):
                try:
                    item.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass


def paste_multipart_prompt(page: Any, chat_input_locator: Any, prompts: List[str]):
    """Pastes multi-part prompts (Prompt 1 to 10) into the chat input.
    
    Pasting each part individually prevents web chat UIs (DeepSeek, Perplexity, Qwen)
    from converting long single-block text into a .txt file attachment.
    """
    valid_prompts = [p.strip() for p in prompts if p and p.strip()]
    if not valid_prompts:
        return

    chat_input_locator.hover()
    human_delay(0.5, 1.0)
    chat_input_locator.click(delay=random.randint(60, 150))
    human_delay(0.8, 1.5)

    modifier = "Meta" if sys.platform == "darwin" else "Control"

    for idx, prompt_part in enumerate(valid_prompts):
        page.evaluate("text => navigator.clipboard.writeText(text)", prompt_part)
        human_delay(0.3, 0.8)
        page.keyboard.press(f"{modifier}+V", delay=random.randint(50, 120))
        human_delay(0.5, 1.0)
        
        # Add line break if there are subsequent prompt parts
        if idx < len(valid_prompts) - 1:
            page.keyboard.press("Shift+Enter")
            human_delay(0.3, 0.6)


def run_deepseek_scraper(pdf_path: Path, prompts: List[str]) -> str:
    """Automates DeepSeek web interface to process a PDF chunk."""
    s_path = DEEPSEEK_SESSION.resolve()
    cleanup_session_locks(s_path)
    session_dir = str(s_path)
    os.makedirs(session_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
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
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://chat.deepseek.com")
        except Exception:
            pass

        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.bring_to_front()
        print("[DeepSeek] Navigating to https://chat.deepseek.com/ ...")
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        human_delay(2.0, 4.0)

        # Cookie banner check
        try:
            cookie_btn = page.locator(".cookie_banner-accept-all-button, .cookie_banner-accept-essential-button").first
            if cookie_btn.count() > 0 and cookie_btn.is_visible(timeout=2000):
                cookie_btn.click()
        except Exception:
            pass

        # Chat textarea check
        chat_textarea = page.locator("textarea[placeholder*='Message DeepSeek'], textarea").first
        try:
            chat_textarea.wait_for(state="visible", timeout=25000)
        except Exception:
            raise RuntimeError("DeepSeek chat composer input not visible. Please verify login in browser.")

        # Upload PDF
        upload_success = False
        file_inputs = page.locator("input[type='file']").all()
        if file_inputs:
            for fi in file_inputs:
                try:
                    fi.set_input_files(str(pdf_path))
                    upload_success = True
                    break
                except Exception:
                    pass

        if not upload_success:
            paperclip = page.locator(".bf38813a div[role='button'], div[role='button'].f02f0e25").first
            if paperclip.count() > 0:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    paperclip.click()
                fc_info.value.set_files(str(pdf_path))
                upload_success = True

        human_delay(3.0, 5.0)

        # Multi-part prompt paste
        chat_input = page.locator("textarea[placeholder*='Message DeepSeek'], textarea").last
        paste_multipart_prompt(page, chat_input, prompts)
        human_delay(2.0, 3.5)

        # Submit
        submission_success = False
        for attempt in range(4):
            page.keyboard.press("Enter", delay=100)
            human_delay(1.5, 2.5)

            try:
                send_btn = page.locator("div[role='button'].ds-button--circle:not(._52c986b), div[role='button'].ds-button--primary.ds-button--circle").last
                if send_btn.is_visible(timeout=1500):
                    send_btn.click()
            except Exception:
                pass

            human_delay(2.0, 4.0)
            val = chat_input.evaluate("el => el.value || el.innerText || ''")
            if not val or len(val.strip()) < 30:
                submission_success = True
                break

        if not submission_success:
            raise RuntimeError("Could not submit prompt to DeepSeek.")

        # Wait for generation
        last_text = ""
        stable_count = 0
        gen_start = time.monotonic()

        while time.monotonic() - gen_start < GENERATION_TIMEOUT_SECONDS:
            time.sleep(2)
            try:
                cont_btn = page.locator("div[role='button']:has-text('Continue'), button:has-text('Continue')").last
                if cont_btn.is_visible(timeout=500):
                    cont_btn.click()
                    stable_count = 0
                    time.sleep(2)
                    continue
            except Exception:
                pass

            current_text = ""
            try:
                code_pres = page.locator(".md-code-block pre, pre code, pre, .ds-markdown pre").all()
                if code_pres:
                    current_text = code_pres[-1].inner_text(timeout=500)
                else:
                    assistant_msgs = page.locator(".ds-markdown.ds-assistant-message-main-content, .ds-markdown, .ds-message").all()
                    if assistant_msgs:
                        current_text = assistant_msgs[-1].inner_text(timeout=500)
            except Exception:
                pass

            # Fast completion check: If valid JSON array or closing code block detected
            if current_text and len(current_text.strip()) > 80:
                trimmed = current_text.strip()
                if (trimmed.startswith("[") and trimmed.endswith("]")) or (trimmed.startswith("```") and trimmed.endswith("```")):
                    try:
                        parsed = parse_clean_json(trimmed)
                        if parsed and len(parsed) > 0:
                            print(f"[DeepSeek] Clean JSON completed ({len(parsed)} questions detected)!")
                            context.close()
                            return trimmed
                    except Exception:
                        pass

            is_generating = False
            try:
                stop_btns = page.locator("button:has-text('Stop'), [class*='stop'], .ds-icon--stop, [aria-label*='Stop']").all()
                if any(sb.is_visible(timeout=300) for sb in stop_btns if sb.count() > 0):
                    is_generating = True
            except Exception:
                pass

            if current_text and len(current_text) > 80:
                if current_text == last_text:
                    if not is_generating:
                        stable_count += 1
                        if stable_count >= 2:
                            break
                    else:
                        stable_count = 0
                else:
                    stable_count = 0
                    last_text = current_text

        # Try Copy code button first
        try:
            copy_btns = page.locator(".md-code-block-banner div[role='button'], div[role='button']:has-text('Copy'), .code-info-button-text, button:has-text('Copy')").all()
            if copy_btns:
                copy_btn = copy_btns[-1]
                if copy_btn.is_visible(timeout=1000):
                    copy_btn.click(delay=100, force=True)
                    human_delay(0.5, 1.0)
                    copied = page.evaluate("""async () => {
                        try {
                            if (navigator.clipboard && navigator.clipboard.readText) {
                                return await navigator.clipboard.readText();
                            }
                            return "";
                        } catch (e) {
                            return "";
                        }
                    }""")
                    if copied and len(copied.strip()) > 50:
                        context.close()
                        return copied
        except Exception:
            pass

        context.close()
        if last_text:
            return last_text
        raise RuntimeError("Failed to retrieve response text from DeepSeek.")


def run_perplexity_scraper(pdf_path: Path, prompts: List[str]) -> str:
    """Automates Perplexity AI web interface to process a PDF chunk."""
    s_path = PERPLEXITY_SESSION.resolve()
    cleanup_session_locks(s_path)
    session_dir = str(s_path)
    os.makedirs(session_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
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
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.perplexity.ai")
        except Exception:
            pass

        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.bring_to_front()
        print("[Perplexity] Navigating to https://www.perplexity.ai/ ...")
        page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        human_delay(2.0, 4.0)

        # Upload file
        page.locator('input[type="file"]').first.set_input_files(str(pdf_path))
        human_delay(3.0, 5.0)

        # Multi-part prompt paste
        ask_input = page.locator('#ask-input').first
        ask_input.wait_for(state="visible", timeout=20000)
        paste_multipart_prompt(page, ask_input, prompts)
        human_delay(2.0, 3.5)

        # Submit
        submission_success = False
        for attempt in range(3):
            page.keyboard.press("Enter", delay=100)
            human_delay(2.0, 4.0)
            current_val = ask_input.inner_text()
            if not current_val or len(current_val.strip()) < 10 or page.url != "https://www.perplexity.ai/":
                submission_success = True
                break

        if not submission_success:
            raise RuntimeError("Could not submit prompt to Perplexity.")

        # Wait for generation & copy button
        gen_start = time.monotonic()
        while time.monotonic() - gen_start < GENERATION_TIMEOUT_SECONDS:
            time.sleep(2)
            try:
                copy_btn = page.locator('button[aria-label="Copy code"]').last
                if copy_btn.is_visible(timeout=500):
                    copy_btn.click()
                    human_delay(0.5, 1.0)
                    copied = page.evaluate("navigator.clipboard.readText()")
                    if copied and len(copied) > 50:
                        context.close()
                        return copied
            except Exception:
                pass

        # Fallback reading text blocks
        assistant_text = ""
        try:
            code_blocks = page.locator("pre code").all()
            if code_blocks:
                assistant_text = code_blocks[-1].inner_text()
            else:
                prose_blocks = page.locator(".prose").all()
                if prose_blocks:
                    assistant_text = prose_blocks[-1].inner_text()
        except Exception:
            pass

        context.close()
        if assistant_text:
            return assistant_text
        raise RuntimeError("Failed to retrieve response text from Perplexity.")


def run_qwen_scraper(pdf_path: Path, prompts: List[str]) -> str:
    """Automates Qwen AI web interface to process a PDF chunk."""
    s_path = QWEN_SESSION.resolve()
    cleanup_session_locks(s_path)
    session_dir = str(s_path)
    os.makedirs(session_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
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
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://chat.qwen.ai")
        except Exception:
            pass

        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.bring_to_front()
        print("[Qwen] Navigating to https://chat.qwen.ai/ ...")
        page.goto("https://chat.qwen.ai/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        human_delay(2.0, 4.0)

        # Chat input check
        try:
            page.wait_for_selector("textarea, [contenteditable='true']", state="visible", timeout=30000)
        except Exception:
            raise RuntimeError("Qwen chat input did not load. Please check Qwen login.")

        # Upload file
        file_inputs = page.locator("input[type='file']").all()
        if file_inputs:
            for fi in file_inputs:
                try:
                    fi.set_input_files(str(pdf_path))
                    break
                except Exception:
                    pass

        human_delay(3.0, 5.0)

        # Multi-part prompt paste
        chat_box = page.locator("textarea, [contenteditable='true']").last
        paste_multipart_prompt(page, chat_box, prompts)
        human_delay(2.0, 3.5)

        # Submit
        for attempt in range(3):
            page.keyboard.press("Enter", delay=100)
            human_delay(2.0, 3.0)

        # Wait for copy button or pre blocks
        gen_start = time.monotonic()
        while time.monotonic() - gen_start < GENERATION_TIMEOUT_SECONDS:
            time.sleep(2)
            try:
                code_pre = page.locator("pre").all()
                if code_pre:
                    last_pre = code_pre[-1].inner_text(timeout=500)
                    if len(last_pre) > 100:
                        context.close()
                        return last_pre
            except Exception:
                pass

        context.close()
        raise RuntimeError("Failed to retrieve response text from Qwen.")


def run_grok_scraper(pdf_path: Path, prompts: List[str]) -> str:
    """Automates Grok (xAI) web interface (grok.com) to process a PDF chunk."""
    s_path = GROK_SESSION.resolve()
    cleanup_session_locks(s_path)
    session_dir = str(s_path)
    os.makedirs(session_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--enable-features=ClipboardAPI",
                "--enable-blink-features=ClipboardAPI",
                "--disable-blink-features=AutomationControlled"
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1366, "height": 850},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = context.pages[0] if context.pages else context.new_page()
        stealth_sync(page)

        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://grok.com")
        except Exception:
            pass

        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.bring_to_front()
        print("[Grok] Navigating to https://grok.com/ ...")
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        human_delay(2.0, 4.0)

        # Upload file
        upload_success = False
        try:
            attach_btn = page.locator('button[aria-label="Attach"], button[data-testid="attach-button"]').first
            if attach_btn.is_visible(timeout=3000):
                attach_btn.click(delay=100)
                human_delay(0.5, 1.0)
                upload_menu_item = page.locator('text="Upload a file"').first
                with page.expect_file_chooser(timeout=4000) as fc_menu:
                    upload_menu_item.click(delay=100)
                fc_menu.value.set_files(str(pdf_path))
                upload_success = True
        except Exception:
            pass

        if not upload_success:
            try:
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(str(pdf_path))
                upload_success = True
            except Exception:
                pass

        human_delay(3.0, 5.0)

        # Multi-part prompt paste
        chat_box = page.locator('textarea, div[data-testid="chat-input"] .ProseMirror, div[contenteditable="true"][role="textbox"]').first
        chat_box.wait_for(state="visible", timeout=20000)
        paste_multipart_prompt(page, chat_box, prompts)
        human_delay(2.0, 3.5)

        # Submit
        for attempt in range(3):
            page.keyboard.press("Enter", delay=100)
            human_delay(2.0, 3.5)

        # Wait for generation
        last_text = ""
        stable_count = 0
        gen_start = time.monotonic()

        while time.monotonic() - gen_start < GENERATION_TIMEOUT_SECONDS:
            time.sleep(2)
            current_text = ""
            try:
                code_blocks = page.locator("pre.shiki, pre code, pre, div.prose pre").all()
                if code_blocks:
                    current_text = code_blocks[-1].inner_text(timeout=500)
                else:
                    msgs = page.locator("div.message-bubble, div[data-testid*='message'], div.prose").all()
                    if msgs:
                        current_text = msgs[-1].inner_text(timeout=500)
            except Exception:
                pass

            if current_text and len(current_text.strip()) > 80:
                trimmed = current_text.strip()
                if (trimmed.startswith("[") and trimmed.endswith("]")) or (trimmed.startswith("```") and trimmed.endswith("```")):
                    try:
                        parsed = parse_clean_json(trimmed)
                        if parsed and len(parsed) > 0:
                            print(f"[Grok] Clean JSON completed ({len(parsed)} questions detected)!")
                            context.close()
                            return trimmed
                    except Exception:
                        pass

                if current_text == last_text:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    last_text = current_text

        # Try Copy button
        try:
            copy_btn = page.locator('button:has(svg path[d*="-5.155700"]), button[aria-label*="Copy"], button:has-text("Copy")').last
            if copy_btn.is_visible(timeout=1000):
                copy_btn.click(delay=100, force=True)
                human_delay(0.5, 1.0)
                copied = page.evaluate("""async () => {
                    try {
                        if (navigator.clipboard && navigator.clipboard.readText) {
                            return await navigator.clipboard.readText();
                        }
                        return "";
                    } catch (e) {
                        return "";
                    }
                }""")
                if copied and len(copied.strip()) > 50:
                    context.close()
                    return copied
        except Exception:
            pass

        context.close()
        if last_text:
            return last_text
        raise RuntimeError("Failed to retrieve response text from Grok.")


def execute_single_chunk(pdf_chunk_path: Path, prompts: List[str], provider: str) -> List[Dict[str, Any]]:
    """Dispatches chunk to specific AI provider and parses JSON response."""
    provider_clean = provider.strip().lower()

    if provider_clean == "deepseek":
        raw_output = run_deepseek_scraper(pdf_chunk_path, prompts)
    elif provider_clean == "perplexity":
        raw_output = run_perplexity_scraper(pdf_chunk_path, prompts)
    elif provider_clean == "qwen":
        raw_output = run_qwen_scraper(pdf_chunk_path, prompts)
    elif provider_clean in ("grok", "groq"):
        raw_output = run_grok_scraper(pdf_chunk_path, prompts)
    else:
        raise ValueError(f"Unknown AI provider requested: {provider}")

    return parse_clean_json(raw_output)


def process_chunks_with_load_balancer(
    pdf_chunks: List[Path],
    prompts: List[str],
    ai_order: List[str],
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> List[List[Dict[str, Any]]]:
    """Processes PDF chunks with fallback chain and max 2 consecutive chunks per AI constraint."""
    if not ai_order:
        ai_order = ["deepseek", "qwen", "perplexity", "grok"]

    results: List[List[Dict[str, Any]]] = []
    current_ai_idx = 0
    consecutive_count = 0

    total_chunks = len(pdf_chunks)

    for i, chunk_path in enumerate(pdf_chunks, start=1):
        # Enforce max 2 consecutive chunks constraint
        if consecutive_count >= 2:
            current_ai_idx = (current_ai_idx + 1) % len(ai_order)
            consecutive_count = 0

        target_provider = ai_order[current_ai_idx]
        chunk_success = False
        parsed_questions: List[Dict[str, Any]] = []

        # Try target provider, fallback to remaining providers if failed
        attempt_providers = [target_provider] + [p for p in ai_order if p != target_provider]

        for prov in attempt_providers:
            if progress_callback:
                progress_callback(i, total_chunks, f"Processing chunk {i}/{total_chunks} using {prov.upper()}...")

            try:
                print(f"[LoadBalancer] Processing Chunk {i}/{total_chunks} with {prov}...")
                parsed_questions = execute_single_chunk(chunk_path, prompts, prov)
                chunk_success = True
                
                # If provider succeeded, update active AI index and increment consecutive count
                if prov == target_provider:
                    consecutive_count += 1
                else:
                    # Switched via fallback
                    current_ai_idx = ai_order.index(prov)
                    consecutive_count = 1

                print(f"[LoadBalancer] Chunk {i} successfully processed by {prov} (Consecutive: {consecutive_count}/2)")
                break
            except Exception as err:
                print(f"[LoadBalancer] Error processing chunk {i} with {prov}: {err}")

        if not chunk_success:
            raise RuntimeError(f"All AI providers in order {ai_order} failed for Chunk {i}.")

        results.append(parsed_questions)

    return results
