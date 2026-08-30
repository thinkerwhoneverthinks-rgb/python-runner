"""Allen PDF Structure, Question, and Answer Key Parser.

Ground-up rewrite tuned for real Allen module PDFs (Nomenclature, etc.).

Key observations from the actual PDF:
- Two-column layout: left col (Q 1-6), right col (Q 7-12) per page
- Question blocks: each question is one PyMuPDF block starting with "N." text
- Options layout: sometimes horizontal (all on same y), sometimes vertical (each on own row)
- CCN tags: appear as the last word of the options block OR on their own line following the options
- Answer key: tabular -- a 'Question' block with Q numbers, followed by an 'Answer' block with answer numbers
- Header banners ('Classification', 'Classification and Nomenclature') span page width and appear at very top
- Page bracket numbers like [31] appear in footer — must be excluded
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import fitz

# ──────────────────────────────────────────────────────────── constants ──

EXERCISE_PATTERNS = [
    (re.compile(r"EXERCISE\s*[-–—]?\s*I(?!I)", re.I), "conceptual", "Conceptual Questions"),
    (re.compile(r"EXERCISE\s*[-–—]?\s*II(?!I)", re.I), "pyq", "PYQ"),
    (re.compile(r"EXERCISE\s*[-–—]?\s*III\b", re.I), "analytical", "Analytical Questions"),
    (re.compile(r"EXERCISE\s*[-–—]?\s*IV\b", re.I), "advanced", "Advanced Questions"),
]

# Tag code: e.g. CCN001, PML042, CCN069
TAG_RE = re.compile(r"^[A-Z]{2,4}\d{3,4}$")

# Question number: "1." or "12." at start of word
QNUM_WORD_RE = re.compile(r"^(\d{1,3})\.$")

# Footer page bracket e.g. [31]
PAGEBRACKET_RE = re.compile(r"^\[\s*\d+\s*\]$")

# Inline option e.g. "(1)" or "(A)"
OPTION_MARKER_RE = re.compile(r"^\((\d|[A-Da-d])\)$")

# Text that indicates answers section header
ANSWER_KEY_RE = re.compile(r"ANSWER\s*KEY", re.I)

# Generic page-wide banners to skip entirely
SKIP_BANNER_PATTERNS = [
    re.compile(r"www\.allen\.in", re.I),
    re.compile(r"^NEET\s*:", re.I),
    re.compile(r"^PRE-MEDICAL\s*:", re.I),
    re.compile(r"^\[\s*\d+\s*\]$"),
    re.compile(r"@\w+"),   # social media watermarks like @saqi_khannnn
]


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] if name else "Untitled"


def is_skip_line(text: str) -> bool:
    """True for footer, header, or bracket lines that should not be parsed."""
    t = text.strip()
    for pat in SKIP_BANNER_PATTERNS:
        if pat.search(t):
            return True
    if PAGEBRACKET_RE.match(t):
        return True
    return False


def is_topic_banner(text: str, page_w: float, x0: float, x1: float) -> bool:
    """True if the line spans most of the page width and is likely a chapter title banner."""
    if detect_exercise_section(text):
        return False
    span = x1 - x0
    # Banners span at least 60% of the page width
    return span > page_w * 0.5 and len(text.strip()) >= 4


# ──────────────────────────────────────────────────── block-level parsing ──

def clean_span_text(t: str) -> str:
    """Normalize private use area symbols from Allen PDF fonts."""
    t = t.replace('\uf0b4', ' × ')  # multiplication cross
    t = t.replace('\uf0ba', ' ≡ ')  # triple bond
    t = t.replace('\u2013', '–').replace('\u2014', '—')
    return t


def extract_rich_text_from_line(line_spans: List[Dict[str, Any]]) -> str:
    """
    Reconstructs line text with LaTeX superscripts ^{...} and subscripts _{...}
    based on relative font size and baseline y-origin.
    """
    if not line_spans:
        return ""

    sizes = [s['size'] for s in line_spans if len(s.get('text', '').strip()) > 0]
    if not sizes:
        return "".join(clean_span_text(s.get('text', '')) for s in line_spans)
    base_size = max(set(sizes), key=sizes.count)

    base_ys = [s['origin'][1] for s in line_spans if abs(s['size'] - base_size) < 0.5]
    base_y = sum(base_ys) / len(base_ys) if base_ys else line_spans[0]['origin'][1]

    out = []
    for s in line_spans:
        t = clean_span_text(s.get('text', ''))
        if not t:
            continue

        size = s.get('size', base_size)
        orig_y = s.get('origin', [0, base_y])[1]

        is_small = size <= base_size * 0.85
        is_super = is_small and (orig_y < base_y - 1.2)
        is_sub = is_small and (orig_y > base_y + 0.6)

        clean_t = t.strip()
        if is_super and clean_t:
            leading_space = " " if t.startswith(" ") else ""
            trailing_space = " " if t.endswith(" ") else ""
            out.append(f"{leading_space}^{{{clean_t}}}{trailing_space}")
        elif is_sub and clean_t:
            leading_space = " " if t.startswith(" ") else ""
            trailing_space = " " if t.endswith(" ") else ""
            out.append(f"{leading_space}_{{{clean_t}}}{trailing_space}")
        else:
            out.append(t)

    return "".join(out)


def get_text_blocks(page: fitz.Page) -> List[Dict[str, Any]]:
    """
    Returns rich text blocks with LaTeX superscripts/subscripts from PyMuPDF dict extraction,
    filtered and sorted by position.
    """
    raw_dict = page.get_text("dict")
    blocks = []
    for bno, b in enumerate(raw_dict.get("blocks", [])):
        if b.get("type") != 0:  # skip image blocks
            continue
        lines_text = []
        for l in b.get("lines", []):
            line_str = extract_rich_text_from_line(l.get("spans", []))
            if line_str.strip():
                lines_text.append(line_str.strip())

        text = "\n".join(lines_text).strip()
        if not text or is_skip_line(text):
            continue

        bbox = b.get("bbox", [0, 0, 0, 0])
        blocks.append({
            "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3],
            "text": text, "bno": bno
        })
    blocks.sort(key=lambda b: (b["y0"], b["x0"]))
    return blocks



def get_lines(page: fitz.Page) -> List[Dict[str, Any]]:
    """
    Group words on a page into visual lines with bbox + text.
    Used for column splitting and topic detection.
    """
    words = page.get_text("words")
    groups: Dict[Tuple[int, int], list] = defaultdict(list)
    for w in words:
        x0, y0, x1, y1, text, block, line, wno = w
        groups[(block, line)].append(w)

    lines = []
    for key, ws in groups.items():
        ws.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in ws).strip()
        if not text or is_skip_line(text):
            continue
        x0 = min(w[0] for w in ws)
        y0 = min(w[1] for w in ws)
        x1 = max(w[2] for w in ws)
        y1 = max(w[3] for w in ws)
        lines.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "text": text, "words": ws
        })
    lines.sort(key=lambda l: l["y0"])
    return lines


# ──────────────────────────────────────────────────── topic detection ──

def detect_topic_title(blocks: List[Dict[str, Any]], page_w: float) -> Optional[str]:
    """
    Find the chapter/topic title. In Allen PDFs this is a wide banner at the top
    e.g. 'Classification and Nomenclature'.
    """
    for b in blocks:
        text = b["text"].strip()
        # Must be in top 15% of page
        if b["y0"] > 130:
            break
        if is_topic_banner(text, page_w, b["x0"], b["x1"]):
            # Clean out sub-strings like 'www.allen.in', brackets
            cleaned = re.sub(r"www\.\S+", "", text, flags=re.I)
            cleaned = re.sub(r"\[\s*\d+\s*\]", "", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) >= 3:
                return sanitize(cleaned)
    return None


def detect_exercise_section(text: str) -> Optional[Tuple[str, str]]:
    """Checks if text contains Exercise I, II, III header. Returns (key, label)."""
    for pat, key, label in EXERCISE_PATTERNS:
        if pat.search(text):
            return key, label
    return None


# ──────────────────────────────────────────────────── answer key parsing ──

def parse_answer_keys(doc: fitz.Document) -> Dict[str, Dict[int, int]]:
    """
    Parses ALLEN-style tabular answer keys.

    Format found in real PDFs:
        EXERCISE-I (Conceptual Questions)   ANSWER KEY
        Question  1  2  3  4  5  6  7 ...
        Answer    1  1  3  1  3  3  4 ...
        Question  16 17 18 ...
        Answer    3  1  3  ...

    The 'Question' and 'Answer' appear as the FIRST word in separate text blocks.
    """
    answers_by_section: Dict[str, Dict[int, int]] = defaultdict(dict)
    curr_section = "conceptual"

    for page_no in range(len(doc)):
        page = doc[page_no]
        blocks = get_text_blocks(page)

        in_answer_section = False
        pending_questions: List[int] = []  # question numbers waiting for their answers

        for b in blocks:
            text = b["text"].strip()

            # Detect exercise section changes
            sec = detect_exercise_section(text)
            if sec:
                curr_section = sec[0]

            # Detect ANSWER KEY header
            if ANSWER_KEY_RE.search(text):
                in_answer_section = True
                pending_questions = []
                continue

            if not in_answer_section:
                continue

            # The block text might look like:
            #   "Question\n1\n2\n3\n4\n5\n6\n..." (each number on own line)
            # or all on one line: "Question 1 2 3 4 5 ..."
            lines_in_block = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if not lines_in_block:
                continue

            first_word = lines_in_block[0].lower()

            if first_word in ("question", "que.", "que"):
                # Extract the question numbers from remaining lines/words
                rest = "\n".join(lines_in_block[1:])
                nums = re.findall(r"\b(\d{1,3})\b", rest)
                pending_questions = [int(n) for n in nums]

            elif first_word in ("answer", "ans.", "ans") and pending_questions:
                # Extract the answer values
                rest = "\n".join(lines_in_block[1:])
                ans_tokens = re.findall(r"\b([1-4])\b", rest)
                for i, ans_str in enumerate(ans_tokens):
                    if i < len(pending_questions):
                        qn = pending_questions[i]
                        idx = int(ans_str) - 1  # convert 1-4 to 0-3
                        answers_by_section[curr_section][qn] = idx
                pending_questions = []

    return dict(answers_by_section)


# ──────────────────────────────────────────────── column splitting ──

def detect_column_divider(page: fitz.Page) -> float:
    """
    Find the x-coordinate dividing the left and right question columns.
    In Allen PDFs, question numbers are typically at a small x (left margin ~50pt).
    We look for the gap in question number x-positions.
    """
    words = page.get_text("words")
    qnum_x_positions = []
    for w in words:
        x0, y0, x1, y1, text, *_ = w
        if QNUM_WORD_RE.match(text.strip()):
            qnum_x_positions.append(x0)

    if not qnum_x_positions:
        return page.rect.width / 2.0

    # If all Q numbers at left (single column) or two distinct x clusters
    sorted_x = sorted(set(round(x / 5) * 5 for x in qnum_x_positions))  # round to nearest 5
    if len(sorted_x) <= 1:
        return page.rect.width / 2.0

    # Two clusters: left col at ~50pt and right col at ~305pt
    # The divider is the midpoint between the rightmost left and leftmost right cluster
    left_xs = [x for x in sorted_x if x < page.rect.width * 0.4]
    right_xs = [x for x in sorted_x if x >= page.rect.width * 0.4]
    if left_xs and right_xs:
        return (max(left_xs) + min(right_xs)) / 2.0

    return page.rect.width / 2.0


# ──────────────────────────────────────────────── option extraction ──

def extract_options_from_block_text(block_text: str) -> Tuple[str, List[str]]:
    """
    Given the merged text of a question block, split into:
      - question stem (before the first option marker)
      - 4 option strings

    Handles both layouts:
      Horizontal: "(1) foo  (2) bar  (3) baz  (4) qux"  <- all on same y-line
      Vertical:   "(1) foo\n(2) bar\n(3) baz\n(4) qux"  <- each on own line

    Also strips trailing CCN tags from options.

    Bug fixes applied:
      1. Horizontal mode only fires when option markers are STRICTLY SEQUENTIAL
         (1,2,3,4 in order) — prevents "both (1) & (2) are correct" from being
         exploded into fake options.
      2. Once all 4 options are populated, continuation text is rejected so that
         answer-key data or next-question text cannot bleed into option 4.
      3. Stray brace artifacts (lone `{` / `}`) are stripped post-extraction.
    """
    lines = [ln.strip() for ln in block_text.split("\n") if ln.strip()]
    options = ["", "", "", ""]
    stem_lines = []
    current_opt = -1

    for ln in lines:
        # Skip standalone CCN tags
        if TAG_RE.match(ln):
            continue

        # Split line around option markers to detect horizontal/vertical layout
        parts = re.split(r"(\(\d\)|\([A-Da-d]\))", ln)
        opt_markers_in_line = [p for p in parts if re.match(r"^\(\d\)$|^\([A-Da-d]\)$", p)]

        if len(opt_markers_in_line) >= 2:
            # ── SEQUENTIAL CHECK ──────────────────────────────────────────────
            # Only treat as a horizontal option line if the markers form a
            # STRICTLY INCREASING SEQUENCE (e.g. 1,2,3,4).  If a line like
            # "(3) both (1) & (2) are correct  (4) ..." is encountered, the
            # marker order [3,1,2,4] is NOT sequential, so we fall through to
            # the single-marker path below instead.
            marker_values: List[int] = []
            for p in opt_markers_in_line:
                mv = re.match(r"^\((\d)\)$", p)
                if mv:
                    marker_values.append(int(mv.group(1)))
                else:
                    mv2 = re.match(r"^\(([A-Da-d])\)$", p)
                    if mv2:
                        marker_values.append(ord(mv2.group(1).upper()) - ord('A') + 1)

            is_sequential = bool(marker_values) and all(
                marker_values[k + 1] == marker_values[k] + 1
                for k in range(len(marker_values) - 1)
            )

            if is_sequential:
                # Horizontal options on one line: "(1) foo  (2) bar  (3) baz  (4) qux"
                pre = parts[0].strip()
                if pre and current_opt == -1:
                    stem_lines.append(pre)
                i = 1
                while i < len(parts):
                    marker_str = parts[i]
                    content = parts[i + 1].strip() if i + 1 < len(parts) else ""
                    content = re.sub(r"\s+[A-Z]{2,4}\d{3,4}$", "", content).strip()
                    m = re.match(r"^\((\d|[A-Da-d])\)$", marker_str)
                    if m:
                        c = m.group(1)
                        if c in "1234":
                            idx = int(c) - 1
                        elif c.upper() in "ABCD":
                            idx = ord(c.upper()) - ord("A")
                        else:
                            i += 2
                            continue
                        if 0 <= idx < 4:
                            options[idx] = content
                            current_opt = idx
                    i += 2
                continue
            # else: NOT sequential → fall through to single-marker check below

        # Check for single option marker at start of line: "(1) text"
        m = re.match(r"^\((\d|[A-Da-d])\)\s*(.*)", ln)
        if m:
            c = m.group(1)
            content = m.group(2).strip()
            content = re.sub(r"\s+[A-Z]{2,4}\d{3,4}$", "", content).strip()
            if c in "1234":
                idx = int(c) - 1
            elif c.upper() in "ABCD":
                idx = ord(c.upper()) - ord("A")
            else:
                # Not a valid option marker, treat as continuation/stem
                if current_opt >= 0:
                    options[current_opt] = (options[current_opt] + " " + ln).strip()
                else:
                    stem_lines.append(ln)
                continue
            if 0 <= idx < 4:
                options[idx] = content
                current_opt = idx
            continue

        # Plain text line
        if current_opt >= 0:
            # ── BLEED-IN GUARD ────────────────────────────────────────────────
            # If all 4 options are already populated, any further plain-text
            # continuation is almost certainly answer-key data or the start of
            # the next question bleeding in.  Reject it.
            all_filled = all(opt.strip() for opt in options)
            if all_filled:
                continue  # stop accumulating into option 4
            # Continuation of previous option
            options[current_opt] = (options[current_opt] + " " + ln).strip()
        else:
            stem_lines.append(ln)

    # Clean up stem: strip CCN tags and leading "N." question number
    stem_clean = []
    for ln in stem_lines:
        ln = re.sub(r"\s+[A-Z]{2,4}\d{3,4}$", "", ln).strip()
        if not TAG_RE.match(ln):
            stem_clean.append(ln)

    prompt = " ".join(stem_clean).strip()
    prompt = re.sub(r"^\d{1,3}\.\s*", "", prompt)

    return prompt, options




# ──────────────────────────────────────────────── question block detection ──

@dataclass
class QuestionBlock:
    num: int
    tag: str
    block_texts: List[str]      # all text blocks belonging to this question
    bbox_pt: List[float]        # [x0, y0, x1, y1] in PDF points
    page_num: int
    column: int                 # 1=left, 2=right


def _split_block_at_question_numbers(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Some PyMuPDF blocks contain multiple questions joined by their CCN tag:
      'CCN001\n2.\nThe number of ...\nCCN006\n3.\nThe number of ...'

    This function splits such a block into individual sub-blocks, one per question.
    Also strips leading topic-banner or watermark lines before the first question.
    Returns a list of block dicts with the same structure, each containing one question.
    """
    text = block["text"]

    # Find all positions of "N." question number lines within the text
    splits = list(re.finditer(r"(?:^|\n)(?:[A-Z]{2,4}\d{3,4}\s*\n\s*)?(\d{1,3})\.\s", text))

    if not splits:
        return [block]

    if len(splits) == 1:
        # Single question: strip anything before the question number (topic banner, watermarks)
        start = splits[0].start()
        if start > 0:
            sub_text = text[start:].strip()
            if sub_text:
                block = dict(block)  # don't mutate original
                block["text"] = sub_text
        return [block]

    sub_blocks = []
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        sub_text = text[start:end].strip()
        if not sub_text:
            continue
        frac = start / max(len(text), 1)
        est_y = block["y0"] + frac * (block["y1"] - block["y0"])
        sub_blocks.append({
            "x0": block["x0"],
            "y0": est_y,
            "x1": block["x1"],
            "y1": block["y1"],
            "text": sub_text,
            "bno": block["bno"]
        })

    return sub_blocks if sub_blocks else [block]


def extract_question_blocks_from_page(
    page: fitz.Page,
    page_num: int,
    col_divider_x: float,
    curr_exercise_key: str
) -> Tuple[List["QuestionBlock"], str]:
    """
    Extract ordered question blocks from a single page.

    Strategy:
    1. Get all PyMuPDF text blocks
    2. Split any blocks that contain multiple question numbers (CCN+Qnum pattern)
    3. Assign each sub-block to left or right column based on x0
    4. Within each column, walk blocks in y-order, grouping them per question number
    5. A new question starts when a block's first word matches "N." pattern
    """
    rich_blocks = get_text_blocks(page)
    page_w = page.rect.width
    page_h = page.rect.height
    new_exercise_key = curr_exercise_key

    # ── Filter and pre-process blocks ──
    processed = []
    for b in rich_blocks:
        x0, y0, x1, y1 = b["x0"], b["y0"], b["x1"], b["y1"]
        text = b["text"]
        block_dict = dict(b)

        # Check for exercise section headers (standalone blocks) FIRST
        sec = detect_exercise_section(text)
        if sec:
            new_exercise_key = sec[0]
            continue

        # Skip page-spanning topic banners at top of page (e.g. Chapter Titles)
        if y0 < page_h * 0.12 and is_topic_banner(text, page_w, x0, x1):
            continue

        # Split blocks that contain multiple question numbers
        sub = _split_block_at_question_numbers(block_dict)
        processed.extend(sub)



    # Sort: left col first, then right col; within each col by y0
    def sort_key(b):
        col = 0 if b["x0"] < col_divider_x else 1
        return (col, b["y0"])

    processed.sort(key=sort_key)

    # ── Group blocks into questions ──
    questions: List[QuestionBlock] = []
    current_q: Optional[QuestionBlock] = None

    for b in processed:
        text = b["text"]
        col = 1 if b["x0"] < col_divider_x else 2

        # Check if this block starts a new question (first non-tag word is "N.")
        lines_in_b = [ln.strip() for ln in text.split("\n") if ln.strip()]
        first_content_line = ""
        tag_from_prefix = ""
        # Skip a leading CCN tag line to find the actual first content line
        for ln in lines_in_b:
            if TAG_RE.match(ln):
                tag_from_prefix = ln
                continue
            first_content_line = ln
            break

        first_word = first_content_line.split()[0] if first_content_line.split() else ""
        qnum_match = QNUM_WORD_RE.match(first_word)

        if qnum_match:
            # Save previous question
            if current_q is not None:
                questions.append(current_q)

            qnum = int(qnum_match.group(1))

            # Determine tag
            tag = tag_from_prefix
            if not tag:
                tags_inline = TAG_RE.findall(first_content_line)
                if tags_inline:
                    tag = tags_inline[0]

            current_q = QuestionBlock(
                num=qnum,
                tag=tag,
                block_texts=[text],
                bbox_pt=[b["x0"], b["y0"], b["x1"], b["y1"]],
                page_num=page_num,
                column=col
            )
        else:
            # Continuation block
            if current_q is None:
                continue

            # Check if it's purely a standalone CCN tag
            if len(lines_in_b) == 1 and TAG_RE.match(lines_in_b[0]):
                current_q.tag = lines_in_b[0]
                continue

            current_q.block_texts.append(text)
            # Expand bbox
            current_q.bbox_pt[0] = min(current_q.bbox_pt[0], b["x0"])
            current_q.bbox_pt[1] = min(current_q.bbox_pt[1], b["y0"])
            current_q.bbox_pt[2] = max(current_q.bbox_pt[2], b["x1"])
            current_q.bbox_pt[3] = max(current_q.bbox_pt[3], b["y1"])

    if current_q is not None:
        questions.append(current_q)

    return questions, new_exercise_key


def merge_parsed_chunks(chunk_results: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Merges questions parsed from individual PDF chunks, handling boundary continuations.
    
    Only merges questions if a question is split across consecutive chunk boundaries
    (i.e., Chunk N ends with Q27 and Chunk N+1 starts with Q27).
    Prevents false merging of duplicate question numbers across different exercises/sections.
    """
    if not chunk_results:
        return []

    merged_list: List[Dict[str, Any]] = []

    for chunk in chunk_results:
        if not chunk:
            continue

        for idx, q in enumerate(chunk):
            if not isinstance(q, dict):
                continue

            q_num = q.get("n")
            if q_num is None:
                q_num = len(merged_list) + 1
                q["n"] = q_num

            # Check if this question is a continuation of the LAST question in merged_list
            # (i.e. Chunk N ended with Q27 and Chunk N+1 starts with Q27 as its first question)
            if idx == 0 and merged_list and merged_list[-1].get("n") == q_num:
                existing = merged_list[-1]

                # Merge diagram flag
                if q.get("d") is True:
                    existing["d"] = True

                # Merge prompt text
                new_q_text = q.get("q", "").strip()
                ex_q_text = existing.get("q", "").strip()
                if new_q_text and new_q_text not in ex_q_text:
                    existing["q"] = f"{ex_q_text}<br />{new_q_text}"

                # Merge options
                ex_opts = existing.get("o") or ["", "", "", ""]
                new_opts = q.get("o") or []

                valid_new_opts = [opt.strip() for opt in new_opts if isinstance(opt, str) and opt.strip()]

                filled_opts = list(ex_opts)
                for opt_str in valid_new_opts:
                    if opt_str in filled_opts:
                        continue
                    placed = False
                    for i_slot in range(len(filled_opts)):
                        if not filled_opts[i_slot].strip():
                            filled_opts[i_slot] = opt_str
                            placed = True
                            break
                    if not placed:
                        filled_opts.append(opt_str)

                existing["o"] = filled_opts

                # Merge solution
                if q.get("e") and not existing.get("e"):
                    existing["e"] = q["e"]
                elif q.get("e") and existing.get("e") and q["e"] not in existing["e"]:
                    existing["e"] = f"{existing['e']}<br />{q['e']}"

                if q.get("s") and not existing.get("s"):
                    existing["s"] = q["s"]

                if q.get("m") and not existing.get("m"):
                    existing["m"] = q["m"]

            else:
                # New question entry
                opts = q.get("o") or []
                while len(opts) < 4:
                    opts.append("")
                q["o"] = opts
                merged_list.append(q)

    return merged_list

