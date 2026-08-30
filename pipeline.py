"""Allen Hybrid Unified Pipeline Orchestrator.

1. High-efficiency Gemini AI extraction (LaTeX mhchem \ce{...}, SMILES, and caveman token optimization).
2. Configurable PDF Chunking (5, 10, 15 pages) to eliminate AI hallucination on large documents.
3. Selective Cropper Bridge running original cropper engine to crop ONLY diagram questions (d: true).
4. Assembles data for the Review Studio and Clean Website Exporter.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import fitz

import clean_exporter as CLEAN_EXP
import cloudinary_service as CLOUD
import cropper_bridge as CROP_BRIDGE
import gemini_parser as GEMINI
import parser as PARSER
import scraper_engine as SCRAPER


@dataclass
class StudioQuestion:
    id: str
    sequence: int
    num: int
    tag: str
    subject: str
    topic: str
    exercise_key: str
    exercise_name: str
    subtopic: str
    prompt: str
    options: List[str]
    correct_index: int
    solution: str
    smiles: Optional[str]
    has_diagram: bool
    mode: str           # 'crop' or 'text'
    image_filename: str
    crop_path: str      # local absolute path to cropped PNG
    image_data_uri: str # base64 data URI for instant Studio preview
    match_lists: Optional[Dict[str, Any]] = None
    type: str = "mcq"
    cloudinary_url: Optional[str] = None


def process_pdf_pipeline(
    pdf_path: Path,
    output_dir: Path,
    prompts: Optional[List[str]] = None,
    ai_order: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    chunk_size: int = 10,
    model_name: str = "deepseek",
    custom_prompt: str = "",
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, Any]:
    """Runs the complete parsing and cropping pipeline using Playwright Scrapers.
    
    Args:
        pdf_path: Path to the input PDF file.
        output_dir: Directory where crops and job data are stored.
        prompts: Multi-part prompt list [Prompt 1, Prompt 2, Prompt 3, Prompt 4].
        ai_order: List of preferred AI engines in fallback order (e.g. ['deepseek', 'qwen', 'perplexity']).
        chunk_size: Pages per chunk for AI parsing (default 10).
        progress_callback: Callback for progress updates.
        
    Returns:
        Dict with "metadata" and "questions" for the Studio.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = output_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    def progress(step: int, total: int, msg: str):
        if progress_callback:
            progress_callback(step, total, msg)

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    doc_title = pdf_path.stem

    parsed_ai_questions: List[Dict[str, Any]] = []

    # Format multi-part prompts list
    prompt_list: List[str] = []
    if prompts and any(p.strip() for p in prompts):
        prompt_list = [p for p in prompts if p.strip()]
    elif custom_prompt and custom_prompt.strip():
        prompt_list = [custom_prompt.strip()]
    else:
        prompt_list = [GEMINI.CAVEMAN_PROMPT]

    if not ai_order:
        ai_order = ["deepseek", "qwen", "perplexity"]

    # Step 1: Slice PDF into Chunks & Execute AI Scrapers with Load Balancer
    progress(1, 4, f"Splitting PDF into chunks ({chunk_size} pages/chunk) for AI Scrapers...")

    # Calculate chunk ranges
    if chunk_size <= 0 or total_pages <= chunk_size:
        chunks_ranges = [(0, total_pages - 1)]
    else:
        chunks_ranges = []
        for start in range(0, total_pages, chunk_size):
            end = min(start + chunk_size - 1, total_pages - 1)
            chunks_ranges.append((start, end))

    chunk_files: List[Path] = []
    import tempfile
    temp_chunk_dir = tempfile.TemporaryDirectory()
    try:
        for idx, (p_start, p_end) in enumerate(chunks_ranges, start=1):
            chunk_doc = fitz.open()
            chunk_doc.insert_pdf(doc, from_page=p_start, to_page=p_end)
            c_file = Path(temp_chunk_dir.name) / f"chunk_{idx}.pdf"
            chunk_doc.save(str(c_file))
            chunk_doc.close()
            chunk_files.append(c_file)

        # Process chunks through Scraper Engine with Fallback & Max 2 Consecutive Limit
        chunk_results = SCRAPER.process_chunks_with_load_balancer(
            chunk_files,
            prompt_list,
            ai_order,
            progress_callback=lambda cur, tot, m: progress(1, 4, f"AI Chunk {cur}/{tot}: {m}")
        )

        # Merge chunk questions handling boundary continuations
        parsed_ai_questions = PARSER.merge_parsed_chunks(chunk_results)

    except Exception as scraper_err:
        print(f"[Pipeline] AI Scraper Error: {scraper_err}")
        # Fallback to Gemini if api_key provided
        if api_key and api_key.strip():
            print("[Pipeline] Retrying via Gemini API fallback...")
            parsed_ai_questions = GEMINI.parse_pdf_with_gemini(
                pdf_path,
                api_key=api_key.strip(),
                chunk_size=chunk_size,
                model_name=model_name,
                custom_prompt=custom_prompt
            )
    finally:
        try:
            temp_chunk_dir.cleanup()
        except Exception:
            pass


    # Determine which questions contain diagrams (d: true)
    diagram_q_nums: Set[int] = set()
    for q in parsed_ai_questions:
        if q.get("d") is True:
            diagram_q_nums.add(q.get("n", 0))

    # Step 2: Selective Cropper Bridge (runs original cropper without trimming)
    progress(2, 4, f"Running Cropper engine (cropping {len(diagram_q_nums)} diagram questions)...")
    crops_map = CROP_BRIDGE.run_cropper_engine(pdf_path, crops_dir, diagram_q_nums=None)

    # Step 3: Assemble Studio Questions
    progress(3, 4, "Assembling questions for Review Studio...")
    assembled_questions: List[Dict[str, Any]] = []

    # If Gemini returned questions, use them as primary structure
    if parsed_ai_questions:
        for idx, ai_q in enumerate(parsed_ai_questions, start=1):
            q_num = ai_q.get("n", idx)
            has_diag = bool(ai_q.get("d", False))
            
            crop_info = crops_map.get(q_num, {})
            crop_path_str = crop_info.get("crop_path", "")
            img_filename = crop_info.get("image_filename", f"q_{q_num}.png" if has_diag else "")

            # If crop exists on disk, generate data URI for instant studio preview
            data_uri = ""
            if crop_path_str and os.path.exists(crop_path_str):
                try:
                    with open(crop_path_str, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                        data_uri = f"data:image/png;base64,{b64}"
                except Exception:
                    pass

            # Mode is 'crop' if diagram flagged and crop exists, otherwise 'text'
            mode = "crop" if (has_diag and crop_path_str and os.path.exists(crop_path_str)) else "text"

            sq = StudioQuestion(
                id=f"q_{q_num}",
                sequence=idx,
                num=q_num,
                tag=f"Q{q_num:03d}",
                subject=ai_q.get("sub", "CHEMISTRY"),
                topic=ai_q.get("top", crop_info.get("topic", doc_title)),
                exercise_key="conceptual",
                exercise_name=crop_info.get("section", "Questions"),
                subtopic=ai_q.get("subtop", crop_info.get("subtopic", "General")),
                prompt=ai_q.get("q", ""),
                options=ai_q.get("o", ["", "", "", ""]),
                correct_index=ai_q.get("a") if ai_q.get("a") is not None else 0,
                solution=ai_q.get("e", "") or "",
                smiles=ai_q.get("s"),
                has_diagram=has_diag,
                mode=mode,
                image_filename=img_filename,
                crop_path=crop_path_str,
                image_data_uri=data_uri,
                match_lists=ai_q.get("m"),
                type="mcq"
            )
            assembled_questions.append(asdict(sq))

    else:
        # Fallback: create from cropper results directly
        for idx, (q_num, cinfo) in enumerate(sorted(crops_map.items(), key=lambda x: x[0]), start=1):
            crop_path_str = cinfo.get("crop_path", "")
            data_uri = ""
            if crop_path_str and os.path.exists(crop_path_str):
                try:
                    with open(crop_path_str, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                        data_uri = f"data:image/png;base64,{b64}"
                except Exception:
                    pass

            sq = StudioQuestion(
                id=f"q_{q_num}",
                sequence=idx,
                num=q_num,
                tag=f"Q{q_num:03d}",
                subject="CHEMISTRY",
                topic=cinfo.get("topic", doc_title),
                exercise_key="conceptual",
                exercise_name=cinfo.get("section", "Questions"),
                subtopic=cinfo.get("subtopic", "General"),
                prompt=f"Question {q_num}",
                options=["(1)", "(2)", "(3)", "(4)"],
                correct_index=0,
                solution="",
                smiles=None,
                has_diagram=True,
                mode="crop",
                image_filename=cinfo.get("image_filename", f"q_{q_num}.png"),
                crop_path=crop_path_str,
                image_data_uri=data_uri
            )
            assembled_questions.append(asdict(sq))

    progress(4, 4, "Pipeline complete! Ready for Review Studio.")

    return {
        "metadata": {
            "title": doc_title,
            "filename": pdf_path.name,
            "total_pages": total_pages,
            "total_questions": len(assembled_questions),
            "diagram_count": len([q for q in assembled_questions if q.get("has_diagram") or q.get("mode") == "crop"]),
            "text_count": len([q for q in assembled_questions if q.get("mode") == "text"]),
            "chunk_size": chunk_size
        },
        "questions": assembled_questions
    }


def format_raw_questions_for_studio(raw_data: Any, doc_title: str = "Manual Import") -> Dict[str, Any]:
    """Formats raw JSON array or dictionary into standard Studio dataset structure."""
    if isinstance(raw_data, dict):
        if "questions" in raw_data and isinstance(raw_data["questions"], list):
            qs = raw_data["questions"]
            meta = raw_data.get("metadata", {"filename": f"{doc_title}.json"})
        else:
            qs = [raw_data]
            meta = {"filename": f"{doc_title}.json"}
    elif isinstance(raw_data, list):
        qs = raw_data
        meta = {"filename": f"{doc_title}.json"}
    else:
        qs = []
        meta = {"filename": f"{doc_title}.json"}

    formatted_qs = []
    for idx, q in enumerate(qs, start=1):
        if not isinstance(q, dict):
            continue

        q_num = q.get("n", q.get("num", idx))
        has_diag = bool(q.get("d", q.get("has_diagram", False)))
        mode = q.get("mode", "crop" if has_diag and (q.get("crop_path") or q.get("image_filename")) else "text")

        opts = q.get("o", q.get("options", ["", "", "", ""]))
        if not isinstance(opts, list):
            opts = ["", "", "", ""]
        opts = list(opts)
        while len(opts) < 4:
            opts.append("")

        correct_idx = q.get("a", q.get("correct_index", 0))
        if isinstance(correct_idx, str):
            try:
                correct_idx = int(correct_idx)
            except ValueError:
                correct_idx = 0

        formatted_qs.append({
            "id": q.get("id", f"q_{idx}"),
            "sequence": idx,
            "num": q_num,
            "tag": q.get("tag", f"Q{q_num:03d}" if isinstance(q_num, int) else f"Q{idx:03d}"),
            "subject": q.get("sub", q.get("subject", "CHEMISTRY")),
            "topic": q.get("top", q.get("topic", doc_title)),
            "exercise_key": q.get("exercise_key", "conceptual"),
            "exercise_name": q.get("exercise_name", "Conceptual Questions"),
            "subtopic": q.get("subtop", q.get("subtopic", "General")),
            "prompt": q.get("q", q.get("prompt", "")),
            "options": opts,
            "correct_index": correct_idx if isinstance(correct_idx, int) else 0,
            "solution": q.get("e", q.get("solution", "")) or "",
            "smiles": q.get("s", q.get("smiles")),
            "has_diagram": has_diag,
            "mode": mode,
            "image_filename": q.get("image_filename", f"q_{q_num}.png" if has_diag else ""),
            "crop_path": q.get("crop_path", ""),
            "image_data_uri": q.get("image_data_uri", ""),
            "match_lists": q.get("m", q.get("match_lists")),
            "type": q.get("type", "mcq"),
            "cloudinary_url": q.get("cloudinary_url")
        })

    return {
        "metadata": meta,
        "questions": formatted_qs
    }

