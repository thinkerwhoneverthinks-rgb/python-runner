"""Clean Exporter for Website JSON Schema.

Transforms parsed questions into the exact Quizzy test configuration schema
matching the user's website format with top-level metadata, global scoring rules,
syllabus objects, sections grouped by subject, and 3-tier image / matchLists support.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional


def slugify(text: str) -> str:
    """Converts string into a clean lowercase underscore slug."""
    s = str(text or "").lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


def format_question_id(
    prefix: str,
    topic: str,
    subtopic: str,
    sequence: int
) -> str:
    """Formats question ID according to user custom pattern:
    {prefix}_{topicname}_{subtopicname}_q{sequence}
    
    If prefix is empty, falls back to a standard q{sequence}.
    """
    if not prefix or not prefix.strip():
        return f"q{sequence}"

    clean_pref = prefix.strip().rstrip('_')
    t_slug = slugify(topic) or "general"
    st_slug = slugify(subtopic) or "general"

    return f"{clean_pref}_{t_slug}_{st_slug}_q{sequence}"


def clean_text_formatting(text: str) -> str:
    """Cleans up text formatting: removes redundant wrapping div tags, converts newlines to <br />."""
    if not text:
        return ""
    
    s = text.strip()
    # Strip wrapping <div style="..."> ... </div> if it wraps the entire text
    m = re.match(r'^<div[^>]*>(.*)</div>$', s, re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    
    # Replace plain \n with <br /> if there are no existing <br> tags
    if "\n" in s and not re.search(r'<br\s*/?>', s, re.IGNORECASE):
        s = re.sub(r'\r?\n', '<br />\n', s)
        
    return s.strip()


def build_website_questions_json(
    questions: List[Dict[str, Any]],
    id_prefix: str = "",
    cloudinary_urls: Optional[Dict[str, str]] = None,
    default_marks: str = "+4",
    test_metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Builds the complete Quizzy test configuration JSON matching the schema."""
    cloudinary_urls = cloudinary_urls or {}
    test_metadata = test_metadata or {}

    # 1. Top-Level Test Parameters
    test_id = test_metadata.get("id") or (slugify(id_prefix) if id_prefix else f"test-{uuid.uuid4().hex[:8]}")
    test_name = test_metadata.get("name") or "MAJOR TEST"
    display_name = test_metadata.get("displayName") or test_name
    description = test_metadata.get("description") or "Full Syllabus Test"
    
    try:
        duration = int(test_metadata.get("duration", 180))
    except (ValueError, TypeError):
        duration = 180

    marking = test_metadata.get("marking")
    if not marking or not isinstance(marking, dict):
        try:
            c_mark = int(str(test_metadata.get("correct_marks", "4")).lstrip('+'))
        except (ValueError, TypeError):
            c_mark = 4
        try:
            i_mark = int(str(test_metadata.get("incorrect_marks", "-1")))
        except (ValueError, TypeError):
            i_mark = -1
        marking = {"correct": c_mark, "incorrect": i_mark}

    # Optional syllabus object
    syllabus = test_metadata.get("syllabus")
    if not syllabus and test_metadata.get("syllabus_content"):
        syllabus = {
            "enabled": True,
            "type": "text",
            "content": test_metadata.get("syllabus_content")
        }

    # 2. Group Questions into Sections by Subject
    sections_map: Dict[str, List[Dict[str, Any]]] = {}
    subject_order: List[str] = []

    for idx, q in enumerate(questions, start=1):
        seq = q.get("sequence", idx)
        raw_subj = (q.get("subject") or q.get("sub") or "CHEMISTRY").strip().upper()
        topic = q.get("topic") or q.get("top") or "General"
        subtopic = q.get("subtopic") or q.get("subtop") or "General"
        
        if raw_subj not in sections_map:
            sections_map[raw_subj] = []
            subject_order.append(raw_subj)

        # Question ID
        q_id = format_question_id(id_prefix, topic, subtopic, seq)
        q_type = q.get("type") or "mcq"

        # Question Prompt
        raw_prompt = q.get("prompt") or q.get("question") or q.get("q") or ""
        clean_prompt = clean_text_formatting(raw_prompt)

        # Cloudinary / Image URL resolution
        mode = q.get("mode", "text")
        img_filename = q.get("image_filename", "")
        crop_path = q.get("crop_path", "")
        has_diagram = q.get("has_diagram", False) or q.get("d", False) or mode == "crop"

        img_url = (
            cloudinary_urls.get(crop_path)
            or cloudinary_urls.get(img_filename)
            or q.get("image_url")
            or q.get("cloudinary_url")
        )
        if not img_url and has_diagram and img_filename:
            img_url = f"crops/{img_filename}"

        # Clean Options
        raw_options = q.get("options") or q.get("o") or []
        if not raw_options or len(raw_options) == 0:
            raw_options = ["(1)", "(2)", "(3)", "(4)"]
        
        cleaned_options = [clean_text_formatting(opt) for opt in raw_options]

        # Correct Answer
        correct_val = q.get("correct")
        if correct_val is None:
            correct_val = q.get("correct_index", q.get("correctIndex", q.get("a", 0)))
        try:
            if isinstance(correct_val, list):
                correct = [int(x) for x in correct_val]
            else:
                correct = int(correct_val)
        except (ValueError, TypeError):
            correct = 0

        # Explanation
        raw_expl = q.get("solution") or q.get("explanation") or q.get("e") or ""
        clean_expl = clean_text_formatting(raw_expl)

        # MatchLists if present
        match_lists = q.get("match_lists") or q.get("matchLists") or q.get("m")

        item: Dict[str, Any] = {
            "id": q_id,
            "type": q_type,
            "question": clean_prompt,
            "image_url": img_url if img_url else None,
            "options": cleaned_options,
            "correct": correct
        }

        if clean_expl:
            item["explanation"] = clean_expl

        if q.get("explanation_image"):
            item["explanation_image"] = q.get("explanation_image")

        if match_lists and isinstance(match_lists, dict):
            item["matchLists"] = match_lists

        if q.get("smiles"):
            item["smiles"] = q["smiles"]

        sections_map[raw_subj].append(item)

    # 3. Assemble Sections
    sections: List[Dict[str, Any]] = []
    for subj in subject_order:
        sections.append({
            "name": subj,
            "icon": "·",
            "questions": sections_map[subj]
        })

    # 4. Final Output Object
    output: Dict[str, Any] = {
        "id": test_id,
        "name": test_name,
        "displayName": display_name,
        "description": description,
        "duration": duration,
        "marking": marking,
        "sections": sections
    }

    if syllabus:
        output["syllabus"] = syllabus

    return output
