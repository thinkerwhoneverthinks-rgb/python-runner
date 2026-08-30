"""Exporter module for Allen Parser.

Generates final structured JSON and packages organized ZIP archives
with section folders (Conceptual Questions, PYQ, Analytical Questions)
containing the cropped question images.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List

SECTION_FOLDER_NAMES = {
    "conceptual": "Conceptual Questions",
    "pyq": "PYQ",
    "analytical": "Analytical Questions",
    "advanced": "Advanced Questions",
}


def build_quizzy_json(questions: List[Dict[str, Any]], title: str = "Allen Module Test") -> Dict[str, Any]:
    """Transforms questions into Quizzy Test Configuration format."""
    sections_map: Dict[str, List[Dict[str, Any]]] = {}

    for q in questions:
        ex_name = q.get("exercise_name", "Conceptual Questions")
        if ex_name not in sections_map:
            sections_map[ex_name] = []

        folder_name = SECTION_FOLDER_NAMES.get(q["exercise_key"], "Conceptual Questions")
        img_rel_path = f"{folder_name}/{q['image_filename']}"

        is_crop = (q["mode"] == "crop")
        
        if is_crop:
            q_text = f'<div style="text-align:center"><img src="{img_rel_path}" style="max-width:100%; height:auto;" /></div>'
            opts = ["(1)", "(2)", "(3)", "(4)"]
            img_url = img_rel_path
        else:
            q_text = q.get("prompt", "")
            opts = [opt if opt else f"({i+1})" for i, opt in enumerate(q.get("options", ["", "", "", ""]))]
            img_url = None

        q_item = {
            "id": q.get("id") or f"q_{q.get('num', 1)}",
            "type": "mcq",
            "tag": q.get("tag", ""),
            "question": q_text,
            "image_url": img_url,
            "options": opts,
            "correct": q.get("correct_index", 0),
            "explanation": "",
            "marks": 4
        }
        sections_map[ex_name].append(q_item)

    sections = []
    for s_name, s_qs in sections_map.items():
        sections.append({
            "name": s_name,
            "questions": s_qs
        })

    return {
        "id": f"allen-{uuid.uuid4().hex[:8]}",
        "name": title,
        "displayName": title,
        "duration": 180,
        "marking": {"correct": 4, "incorrect": -1},
        "sections": sections
    }


def build_aiot_json(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Transforms questions into AIOT offline JSON array format."""
    out = []
    for q in questions:
        folder_name = SECTION_FOLDER_NAMES.get(q["exercise_key"], "Conceptual Questions")
        img_rel_path = f"{folder_name}/{q['image_filename']}"

        if q["mode"] == "crop":
            prompt_html = f'<div style="text-align:center"><img src="{img_rel_path}" style="max-width:100%; height:auto;" /></div>'
            options_html = [
                f'<div style="text-align:justify">(1)</div>\n',
                f'<div style="text-align:justify">(2)</div>\n',
                f'<div style="text-align:justify">(3)</div>\n',
                f'<div style="text-align:justify">(4)</div>\n',
            ]
        else:
            prompt_html = f'<div style="text-align:justify">{q.get("prompt", "")}</div>\n'
            options_html = [
                f'<div style="text-align:justify">{opt}</div>\n' if opt else f'<div style="text-align:justify">({i+1})</div>\n'
                for i, opt in enumerate(q.get("options", ["", "", "", ""]))
            ]

        item = {
            "id": str(uuid.uuid4()),
            "sequence": q.get("sequence", 1),
            "subject": q.get("subject", "PHYSICS"),
            "topic": q.get("topic", "General"),
            "exercise": q.get("exercise_name", "Conceptual Questions"),
            "prompt": prompt_html,
            "options": options_html,
            "correctIndex": q.get("correct_index", 0),
            "solution": "",
            "marks": "+4"
        }
        out.append(item)
    return out


def create_export_zip(
    data: Dict[str, Any],
    crops_dir: Path,
    output_zip_path: Path
) -> Path:
    """Creates an organized ZIP bundle containing:
    - Exercise subfolders (Conceptual Questions/, PYQ/, Analytical Questions/) with cropped images
    - questions.json (Quizzy Schema)
    - aiot_questions.json (AIOT Schema)
    - project_data.json (Raw pipeline data)
    """
    questions = data.get("questions", [])
    title = data.get("metadata", {}).get("source_pdf", "Allen Questions").replace(".pdf", "")

    quizzy_data = build_quizzy_json(questions, title=title)
    aiot_data = build_aiot_json(questions)

    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Write Quizzy format questions.json
        zf.writestr("questions.json", json.dumps(quizzy_data, indent=2, ensure_ascii=False).encode("utf-8"))

        # 2. Write AIOT offline format
        zf.writestr("aiot_questions.json", json.dumps(aiot_data, indent=2, ensure_ascii=False).encode("utf-8"))

        # 3. Write raw project data
        zf.writestr("project_data.json", json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))

        # 4. Add cropped images into organized exercise directories
        for q in questions:
            img_fname = q.get("image_filename")
            if not img_fname:
                continue

            src_img = crops_dir / img_fname
            if src_img.exists():
                folder_name = SECTION_FOLDER_NAMES.get(q["exercise_key"], "Conceptual Questions")
                zip_target_path = f"{folder_name}/{img_fname}"
                zf.write(src_img, arcname=zip_target_path)

    return output_zip_path
