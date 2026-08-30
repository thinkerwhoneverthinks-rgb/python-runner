"""Gemini AI PDF Quiz Parser with Chunking, mhchem, and SMILES support.

Uses the official modern google.genai SDK with fallback to direct REST API.
Splits multi-page PDFs into configurable chunks (default 10 pages) to prevent
hallucination, prompts Gemini Flash with minified caveman JSON schema to save 40%
tokens, and extracts chemical formulas in LaTeX mhchem (\ce{...}) and SMILES.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import fitz
import requests

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


CAVEMAN_PROMPT = """You are an expert exam paper and quiz parser.
Extract ALL multiple-choice questions from the provided PDF page(s).

STRICT PARSING RULES:
1. ONLY extract questions that exist in the PDF. DO NOT invent questions.
2. For ANY Chemistry formula, chemical reaction, ionic equation, or chemical name, ALWAYS format in LaTeX mhchem syntax: \\ce{...}, e.g. \\ce{CH3-CH2-OH}, \\ce{KMnO4 + HCl -> KCl + MnCl2 + H2O + Cl2}, \\ce{Fe^{2+}}.
3. For Physics/Math equations, use standard LaTeX enclosed in $...$ or $$...$$.
4. If a question contains a 2D organic molecular branch/structure, extract its SMILES string in the "s" field.
5. NEAT LINE BREAKS & FORMATTING: For multi-part text (such as Assertion-Reason statements, instructions, or multi-line questions), insert <br /> tags between sections so they render cleanly.
   Example: "Given below are two statements: one is labelled as Assertion (A) and the other is labelled as Reason (R).<br />Assertion (A): Equal volume of all gases...<br />Reason (R): Atom is the fundamental entity...<br />In the light of the above statements, choose the most appropriate answer:"
6. LINKED / PARAGRAPH QUESTIONS: If a group of questions shares a common instruction paragraph, passage, context, or diagram (e.g. "Paragraph for Question Nos. 10 to 12" with a graph/text):
   - You MUST prepend the common paragraph text (the entire context/instructions) to the beginning of the "q" field of EVERY question in that group.
   - You MUST set "d": true for EVERY question in that group if the common paragraph contains a diagram, graph, or visual drawing.
7. MATCH THE COLUMN / MULTI-LIST: For "Match Column I with Column II" or multi-list questions (2, 3, or 4 columns/lists), extract a structured object in the "m" field:
   "m": {
     "listI": { "title": "List-I (Title)", "items": [{ "label": "P", "text": "...", "image_url": null }] },
     "listII": { "title": "List-II (Title)", "items": [{ "label": "1", "text": "...", "image_url": null }] }
   }
   If not a match-the-column question, set "m": null.
8. DIAGRAM FLAG ("d"): If a question or its common paragraph contains a diagram, circuit, graph, apparatus, complex visual table, or drawing that CANNOT be rendered cleanly with plain text/mhchem, set "d": true. Otherwise set "d": false.
9. JSON MINIFIED KEYS:
   - "n": Question number (integer, e.g. 1, 2, 3...)
   - "q": Question prompt text (clean string with \\ce{...}, LaTeX, <br /> line breaks)
   - "o": Array of 4 option strings, e.g. ["opt 1", "opt 2", "opt 3", "opt 4"]
   - "a": Correct option index (0 for 1/A, 1 for 2/B, 2 for 3/C, 3 for 4/D, or null if answer key not found on this page)
   - "e": Solution or explanation text (or null)
   - "d": true if diagram/figure needs cropping, false if clean text
   - "s": SMILES string if applicable (or null)
   - "m": matchLists object for match the column (or null)
   - "sub": Subject (e.g. "CHEMISTRY", "PHYSICS", "BIOLOGY", "MATHEMATICS")
   - "top": Main Topic / Chapter name (DO NOT invent randomly. Use provided custom instructions or "General" if not explicitly clear)
   - "subtop": Subtopic name (DO NOT invent randomly. Use provided custom instructions or "General")

Return ONLY a valid JSON array of question objects."""


def extract_answer_keys_from_pdf(doc: fitz.Document) -> Dict[int, int]:
    """Scans the last pages of a document to find any Answer Key tables."""
    answers: Dict[int, int] = {}
    num_pages = len(doc)
    scan_pages = range(max(0, num_pages - 4), num_pages)

    for p_idx in scan_pages:
        text = doc[p_idx].get_text()
        if "ANSWER" in text.upper() and "KEY" in text.upper():
            # Pattern: 1.(2) or 1. 2 or Q.1 (2) or 1 - B
            matches = re.findall(r'(?:Q\.?\s*)?(\d{1,3})\s*[\.\:\-\s]\s*[\(\[]?([1-4]|[A-D])[\)\]]?', text)
            for q_str, ans_str in matches:
                try:
                    q_num = int(q_str)
                    if ans_str in ("A", "1"):
                        ans_idx = 0
                    elif ans_str in ("B", "2"):
                        ans_idx = 1
                    elif ans_str in ("C", "3"):
                        ans_idx = 2
                    elif ans_str in ("D", "4"):
                        ans_idx = 3
                    else:
                        continue
                    answers[q_num] = ans_idx
                except ValueError:
                    pass
    return answers


def clean_json_response(raw_text: str) -> List[Dict[str, Any]]:
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
    except Exception as e:
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
        raise ValueError(f"Could not parse Gemini JSON response: {e}\nRaw preview: {clean[:200]}")
    return []


def post_process_paragraph_questions(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Propagates common paragraph text and diagram flags to all questions in a group.
    
    Detects patterns like 'Paragraph for Question Nos. 10 to 12' and ensures questions
    10, 11, and 12 all contain the main paragraph part and have d: true.
    """
    if not questions:
        return questions

    q_map = {q.get("n"): q for q in questions if q.get("n") is not None}
    
    # Pattern to match paragraph question ranges: e.g. "Paragraph for Question Nos. 10 to 12"
    range_pattern = re.compile(
        r'(?i)(?:paragraph|passage|directions|instructions|read\s+the\s+following)\s+(?:for\s+)?(?:question|q\.?)\s*(?:nos?\.?)?\s*(\d+)\s*(?:to|-|and)\s*(\d+)'
    )

    for q_num, q in sorted(q_map.items()):
        text = q.get("q", "")
        match = range_pattern.search(text)
        if match:
            try:
                start_num = int(match.group(1))
                end_num = int(match.group(2))
            except ValueError:
                continue

            if start_num <= q_num <= end_num:
                # Split prompt to extract the common context before the specific question
                lines = re.split(r'\n|<br\s*/?>', text)
                non_empty = [line.strip() for line in lines if line.strip()]
                
                if len(non_empty) > 1:
                    # Everything except the last non-empty item is the common context
                    common_part = "<br />".join(non_empty[:-1]).strip()
                else:
                    common_part = text.strip()

                has_diagram = q.get("d", False)

                for num in range(start_num, end_num + 1):
                    target_q = q_map.get(num)
                    if not target_q:
                        continue

                    # Set diagram flag to true if parent has diagram
                    if has_diagram:
                        target_q["d"] = True

                    # Prepend common paragraph if not already present in the target question
                    t_text = target_q.get("q", "")
                    if num != q_num and common_part:
                        # Check if target already has the common text
                        clean_check = re.sub(r'[^a-zA-Z0-9]+', '', common_part[:30]).lower()
                        clean_target = re.sub(r'[^a-zA-Z0-9]+', '', t_text).lower()
                        if clean_check and clean_check not in clean_target:
                            t_clean = re.sub(r'^\s*(?:Q\.?\s*)?\d+[\.\:\-\s\)]*', '', t_text).strip()
                            target_q["q"] = f"{common_part}<br />{t_clean}"

    return questions


def parse_chunk_with_gemini(
    chunk_pdf_path: Path,
    api_key: str,
    model_name: str = "gemini-1.5-flash",
    custom_prompt: str = ""
) -> List[Dict[str, Any]]:
    """Uploads a PDF chunk to Gemini and parses it using the caveman schema."""
    api_key = api_key.strip()
    model_name = model_name.strip() or "gemini-1.5-flash"
    
    final_prompt = CAVEMAN_PROMPT
    if custom_prompt.strip():
        final_prompt += f"\n\n[USER CUSTOM INSTRUCTIONS]:\n{custom_prompt.strip()}"

    # 1. Try official google.genai SDK
    if HAS_GENAI:
        try:
            client = genai.Client(api_key=api_key)
            uploaded = client.files.upload(file=str(chunk_pdf_path))
            
            # Wait if processing
            for _ in range(10):
                if getattr(uploaded, "state", None) == "ACTIVE" or not hasattr(uploaded, "state"):
                    break
                time.sleep(1)

            response = client.models.generate_content(
                model=model_name,
                contents=[uploaded, final_prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            return clean_json_response(response.text)
        except Exception as e:
            err_msg = str(e)
            if "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
                raise ValueError("Invalid Gemini API Key. Please get a free API key starting with 'AIzaSy...' from https://aistudio.google.com/")
            print(f"[google-genai SDK notice]: {e}, trying direct REST fallback with model {model_name}...")

    # 2. Direct REST Fallback (using base64 PDF inline data)
    pdf_bytes = chunk_pdf_path.read_bytes()
    b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": b64_pdf
                        }
                    },
                    {
                        "text": final_prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=90)
    
    if resp.status_code != 200:
        err_body = resp.text
        if "API_KEY_INVALID" in err_body or "API key not valid" in err_body:
            raise ValueError("Invalid Gemini API Key. Please get a valid key from Google AI Studio (https://aistudio.google.com/).")
        raise RuntimeError(f"Gemini API Error ({model_name} HTTP {resp.status_code}): {err_body}")

    resp_json = resp.json()
    try:
        candidate_text = resp_json["candidates"][0]["content"]["parts"][0]["text"]
        return clean_json_response(candidate_text)
    except Exception as e:
        raise ValueError(f"Failed to extract candidates from Gemini response: {e}")


def parse_pdf_with_gemini(
    pdf_path: Path,
    api_key: str,
    chunk_size: int = 10,
    model_name: str = "gemini-1.5-flash",
    custom_prompt: str = "",
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> List[Dict[str, Any]]:
    """Splits PDF into configurable chunks and parses with Gemini.
    
    Args:
        pdf_path: Absolute path to input PDF.
        api_key: Google Gemini API key.
        chunk_size: Pages per chunk (e.g. 10). If 0 or >= total pages, runs as single file.
        model_name: Gemini model name (default 'gemini-1.5-flash').
        progress_callback: Callback reporting (current_step, total_steps, message).
    """
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    
    # Extract answer key map if present in the document
    global_answers = extract_answer_keys_from_pdf(doc)

    all_questions: List[Dict[str, Any]] = []
    
    # Determine chunk boundaries
    if chunk_size <= 0 or total_pages <= chunk_size:
        chunks = [(0, total_pages - 1)]
    else:
        chunks = []
        for start in range(0, total_pages, chunk_size):
            end = min(start + chunk_size - 1, total_pages - 1)
            chunks.append((start, end))

    total_chunks = len(chunks)
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for idx, (p_start, p_end) in enumerate(chunks):
            chunk_num = idx + 1
            if progress_callback:
                progress_callback(
                    chunk_num,
                    total_chunks,
                    f"Processing Chunk {chunk_num}/{total_chunks} (Pages {p_start+1}–{p_end+1}) using {model_name}..."
                )
            
            # Slices pages into chunk PDF
            chunk_doc = fitz.open()
            chunk_doc.insert_pdf(doc, from_page=p_start, to_page=p_end)
            chunk_pdf_file = Path(temp_dir) / f"chunk_{chunk_num}.pdf"
            chunk_doc.save(str(chunk_pdf_file))
            chunk_doc.close()
            
            # Parse chunk with Gemini
            try:
                chunk_qs = parse_chunk_with_gemini(
                    chunk_pdf_file, 
                    api_key, 
                    model_name=model_name,
                    custom_prompt=custom_prompt
                )
                
                # Assign page offset metadata & match global answers
                for q in chunk_qs:
                    q_num = q.get("n", 0)
                    if (q.get("a") is None or q.get("a") == "") and q_num in global_answers:
                        q["a"] = global_answers[q_num]
                    q["chunk_index"] = chunk_num
                    q["page_range"] = f"{p_start+1}-{p_end+1}"
                
                all_questions.extend(chunk_qs)
            except Exception as e:
                print(f"[Gemini Chunk {chunk_num} Error]: {e}")
                # Re-raise so the user is informed about invalid key or quota errors
                raise e

    # Apply paragraph question propagation for linked questions
    all_questions = post_process_paragraph_questions(all_questions)

    # Deduplicate by question number if needed
    seen_nums = set()
    deduped_qs = []
    for q in all_questions:
        q_num = q.get("n")
        if q_num and q_num in seen_nums:
            continue
        if q_num:
            seen_nums.add(q_num)
        deduped_qs.append(q)

    # Sort sequentially by question number
    deduped_qs.sort(key=lambda x: x.get("n", 0))
    return deduped_qs
