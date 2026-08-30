"""Batch AI Math & Formula Solver using Google Gemini API.

Compiles all selected question crops into a single optimized, compact batch PDF,
submits it in ONE SINGLE API call to Gemini (gemini-1.5-flash / gemini-2.0-flash)
with structured JSON output, and enriches questions with high-accuracy LaTeX math and options.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import fitz
from PIL import Image
import requests


def compile_batch_pdf(
    questions: List[Dict[str, Any]],
    output_pdf_path: Path
) -> Path:
    """Compiles multiple question crop images into a single compact, high-quality PDF."""
    doc = fitz.open()

    for q in questions:
        q_id = q.get("id", "q")
        img_path = q.get("crop_path") or q.get("dest_path")
        if not img_path or not os.path.exists(img_path):
            continue

        try:
            # Optimize and compress image using PIL to prevent huge PDF payloads
            with Image.open(str(img_path)) as pil_img:
                # Convert to RGB if RGBA/P
                if pil_img.mode in ("RGBA", "P"):
                    pil_img = pil_img.convert("RGB")
                
                # Resize if unnecessarily large (max 1400px width is plenty for crisp text)
                max_dim = 1400
                w, h = pil_img.size
                if w > max_dim or h > max_dim:
                    scale = min(max_dim / w, max_dim / h)
                    pil_img = pil_img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                
                # Save as compressed JPEG buffer
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85, optimize=True)
                jpeg_bytes = buf.getvalue()
                cur_w, cur_h = pil_img.size

            # Create a page in PyMuPDF
            # Use 72 points per inch coordinate system
            pt_w = max(cur_w * 0.75 + 40, 400)
            pt_h = cur_h * 0.75 + 80
            page = doc.new_page(width=pt_w, height=pt_h)

            # Insert question ID header
            header_text = f"=== QUESTION_ID: {q_id} ==="
            page.insert_text(
                fitz.Point(20, 30),
                header_text,
                fontsize=15,
                fontname="helv",
                color=(0, 0, 0)
            )

            # Insert the compressed image
            target_rect = fitz.Rect(20, 45, 20 + cur_w * 0.75, 45 + cur_h * 0.75)
            page.insert_image(target_rect, stream=jpeg_bytes)

        except Exception as e:
            print(f"[AI Batch] Error inserting image for {q_id}: {e}")

    doc.save(str(output_pdf_path), deflate=True, garbage=4)
    doc.close()
    return output_pdf_path


def solve_math_batch_gemini(
    questions: List[Dict[str, Any]],
    api_key: str,
    work_dir: Path
) -> Dict[str, Dict[str, Any]]:
    """Sends question crops to Gemini in a single batch PDF request.
    
    Returns:
        Dict mapping q_id -> {"prompt": str, "options": List[str]}
    """
    if not questions or not api_key:
        return {}

    batch_pdf_path = work_dir / "math_batch_document.pdf"
    compile_batch_pdf(questions, batch_pdf_path)

    if not batch_pdf_path.exists() or batch_pdf_path.stat().st_size == 0:
        return {}

    pdf_size_kb = batch_pdf_path.stat().st_size / 1024
    print(f"[AI Batch] Compiled compact batch document ({len(questions)} questions, {pdf_size_kb:.1f} KB). Uploading to Gemini...")

    pdf_bytes = batch_pdf_path.read_bytes()
    b64_data = base64.b64encode(pdf_bytes).decode("utf-8")

    prompt = (
        "You are an expert Math & Science LaTeX transcription engine.\n"
        "You are provided with a document containing multiple cropped question images.\n"
        "Each page has a header like '=== QUESTION_ID: <id> ==='.\n\n"
        "STRICT INSTRUCTIONS:\n"
        "1. For each QUESTION_ID, transcribe the question text and all 4 multiple choice options.\n"
        "2. Convert all mathematical equations, formulas, fractions, integrals, exponents, chemical subscripts/superscripts, and greek symbols into clean LaTeX ($...$ for inline math, $$...$$ for block math).\n"
        "3. Preserve chemical formulas and isotopes faithfully (e.g. ^{12}_{6}C, M_2O_3, etc.).\n"
        "4. DO NOT solve or answer the questions. Only transcribe the text and options accurately.\n"
        "5. Output valid JSON ONLY adhering to the following schema:\n"
        "{\n"
        '  "<QUESTION_ID>": {\n'
        '    "prompt": "Question text with $LaTeX$ formulas",\n'
        '    "options": [\n'
        '      "Option (1) text or formula",\n'
        '      "Option (2) text or formula",\n'
        '      "Option (3) text or formula",\n'
        '      "Option (4) text or formula"\n'
        '    ]\n'
        '  }\n'
        '}\n'
    )

    # List of models to try in order of preference
    models_to_try = [
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro"
    ]

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": b64_data
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1
        }
    }

    headers = {"Content-Type": "application/json"}
    
    last_err = None
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        try:
            print(f"[AI Batch] Calling Gemini model: {model_name} (payload size: {len(b64_data)/1024:.1f} KB Base64)...")
            # Connect timeout: 30s, Read/processing timeout: 180s
            resp = requests.post(url, headers=headers, json=payload, timeout=(30, 180))
            
            if resp.status_code == 200:
                resp_json = resp.json()
                raw_text = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )

                if raw_text:
                    parsed_results = json.loads(raw_text)
                    print(f"[AI Batch] Successfully transcribed {len(parsed_results)} questions with {model_name}.")
                    return parsed_results
            else:
                print(f"[AI Batch] Model {model_name} returned status {resp.status_code}: {resp.text[:300]}")
                last_err = f"Status {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            print(f"[AI Batch] Error with model {model_name}: {e}")
            last_err = str(e)
            time.sleep(1)

    print(f"[AI Batch] Failed all Gemini models. Last error: {last_err}")
    return {}
