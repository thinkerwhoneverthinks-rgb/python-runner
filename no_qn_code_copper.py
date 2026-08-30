import fitz
import numpy as np
import cv2
import re
import os
import sys
import shutil
import zipfile
import json
from collections import defaultdict
from PIL import Image

ZOOM = 300 / 72.0

GENERIC_HEADER_WORDS = {
    "NEET : PHYSICS", "NEET : CHEMISTRY", "NEET : BIOLOGY",
    "NEET: PHYSICS", "NEET: CHEMISTRY", "NEET: BIOLOGY",
    "PRE-MEDICAL : PHYSICS", "PRE-MEDICAL : CHEMISTRY", "PRE-MEDICAL : BIOLOGY",
    "PRE-MEDICAL: PHYSICS", "PRE-MEDICAL: CHEMISTRY", "PRE-MEDICAL: BIOLOGY",
    "WWW.ALLEN.IN", "ALLEN", "PHYSICS", "CHEMISTRY", "BIOLOGY"
}

EXERCISE_PATTERNS = [
    (re.compile(r"EXERCISE\s*[-–—]?\s*I(?!I)", re.I), "conceptual"),
    (re.compile(r"EXERCISE\s*[-–—]?\s*II(?!I)", re.I), "pyq"),
    (re.compile(r"EXERCISE\s*[-–—]?\s*III\b", re.I), "analytical"),
]

SECTION_FOLDER = {
    "conceptual": "Conceptual Questions",
    "pyq": "PYQ",
    "analytical": "Analytical Questions",
}

ANSWER_KEY_RE = re.compile(r"ANSWER\s*KEY", re.I)
PARAGRAPH_RE = re.compile(r"Paragraph\s+for\s+Question\s+Nos?\.?\s*(\d+)\s*to\s*(\d+)", re.I)
QNUM_RE = re.compile(r"^(\d{1,3})\.$")
TAG_RE = re.compile(r"^[A-Z]{3}\d{3}$")   # ALLEN question codes: PML042, CCN043, ...
PAGEBRACKET_RE = re.compile(r"^\[\s*\d+\s*\]$")
YEAR_RE = re.compile(r"(19|20)\d{2}")


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] if name else "Untitled"


def get_lines(page):
    """Group words into visual lines with bbox + concatenated text."""
    words = page.get_text("words")
    groups = defaultdict(list)
    for w in words:
        x0, y0, x1, y1, text, block, line, wno = w
        groups[(block, line)].append(w)
    lines = []
    for key, ws in groups.items():
        ws.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in ws)
        x0 = min(w[0] for w in ws)
        y0 = min(w[1] for w in ws)
        x1 = max(w[2] for w in ws)
        y1 = max(w[3] for w in ws)
        lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text, "words": ws})
    lines.sort(key=lambda l: l["y0"])
    return lines


def render_page(page):
    mat = fitz.Matrix(ZOOM, ZOOM)
    pix = page.get_pixmap(matrix=mat)
    img = np.array(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    return img


def _ink_mask(raw):
    gray = cv2.cvtColor(raw, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    return thresh


# --- visual (ink-profile) question boundaries -------------------------------
# Question numbers are text, but chemical structures are images that sit ABOVE
# the number's baseline. Slicing on text coordinates alone therefore chops the
# top off a question and leaks the bottom of the next one's structure into it.
# We instead cut inside real horizontal whitespace found in the rendered page.

INK_GRAY_MAX = 200      # < this = ink (the light grey watermark is ~219-230)
INK_ROW_TOL = 2         # rows with <= this many ink pixels count as blank
GAP_MIN_PX = 10         # preferred separator height, at ZOOM (300 dpi)
GAP_MIN_PX_FALLBACK = 5 # used only when no preferred-size gap exists
GAP_SEARCH_PT = 130.0   # how far above a marker we look for a separator
TAG_DESCENDER_PT = 3.5  # empty descender space inside a tag's glyph box
CROP_PAD_PT = 4.0       # breathing room, never more than half the gap allows


def column_ink_rows(img_np, x_left, x_right):
    """Per-row ink flags for one column of the rendered page (PDF-pt -> px)."""
    h, w = img_np.shape[:2]
    xa = max(0, min(w, int(x_left * ZOOM)))
    xb = max(0, min(w, int(x_right * ZOOM)))
    if xb - xa < 2:
        return np.zeros(h, dtype=bool)
    gray = cv2.cvtColor(img_np[:, xa:xb], cv2.COLOR_RGB2GRAY)
    return (gray < INK_GRAY_MAX).sum(axis=1) > INK_ROW_TOL


def _scan_gap_above(inked, y_pt, lo_pt, min_gap_px):
    """Nearest blank run of >= min_gap_px rows above y_pt. Returns (top, bottom) in pt."""
    top_limit = max(0, int(max(lo_pt, y_pt - GAP_SEARCH_PT) * ZOOM))
    i = max(0, min(len(inked), int(y_pt * ZOOM)))
    while i > top_limit:
        if inked[i - 1]:
            i -= 1
            continue
        b = i
        a = i
        while a > top_limit and not inked[a - 1]:
            a -= 1
        if b - a >= min_gap_px:
            return a / ZOOM, b / ZOOM
        i = a
    return None


def gap_above(inked, y_pt, lo_pt):
    """(cut, top): where the previous block ends and this one visually starts."""
    g = _scan_gap_above(inked, y_pt, lo_pt, GAP_MIN_PX)
    if g is None:
        g = _scan_gap_above(inked, y_pt, lo_pt, GAP_MIN_PX_FALLBACK)
    if g is None:
        return y_pt, y_pt
    return g


def last_ink_before(inked, y_pt, lo_pt):
    """Bottom of the last inked row above y_pt - kills trailing dead space."""
    lo = max(0, int(lo_pt * ZOOM))
    i = max(0, min(len(inked), int(y_pt * ZOOM)))
    while i > lo and not inked[i - 1]:
        i -= 1
    return i / ZOOM


def crop_tight(img_np, x0, y0, x1, y1, pad_px=14, min_ink=0):
    h, w = img_np.shape[:2]
    x0 = max(0, int(x0)); y0 = max(0, int(y0))
    x1 = min(w, int(x1)); y1 = min(h, int(y1))
    if x1 <= x0 or y1 <= y0:
        return None
    raw = img_np[y0:y1, x0:x1]
    thresh = _ink_mask(raw)
    total_ink = int(cv2.countNonZero(thresh))
    if total_ink == 0:
        return None if min_ink else Image.fromarray(raw)
    if min_ink and total_ink < min_ink:
        return None

    # Ignore rows/columns holding only a speck or two, so one stray pixel from a
    # rule or watermark edge cannot pin a huge blank margin onto the crop.
    rows = (thresh > 0).sum(axis=1)
    cols = (thresh > 0).sum(axis=0)
    r_idx = np.flatnonzero(rows > INK_ROW_TOL)
    c_idx = np.flatnonzero(cols > INK_ROW_TOL)
    if len(r_idx) == 0 or len(c_idx) == 0:
        r_idx = np.flatnonzero(rows)
        c_idx = np.flatnonzero(cols)
    if len(r_idx) == 0 or len(c_idx) == 0:
        return None if min_ink else Image.fromarray(raw)

    y, hh = int(r_idx[0]), int(r_idx[-1] - r_idx[0] + 1)
    x, ww = int(c_idx[0]), int(c_idx[-1] - c_idx[0] + 1)
    xt = max(0, x - pad_px)
    yt = max(0, y - pad_px)
    wt = min(raw.shape[1] - xt, ww + 2 * pad_px)
    ht = min(raw.shape[0] - yt, hh + 2 * pad_px)
    final = raw[yt:yt + ht, xt:xt + wt]
    return Image.fromarray(final)


class Question:
    def __init__(self, topic, section, subtopic, num):
        self.topic = topic
        self.section = section
        self.subtopic = subtopic
        self.num = num
        self.image = None
        self.passage_image = None
        self.rects = []
        self.continuations = []

    def combined_image(self):
        parts = []
        if self.passage_image is not None:
            parts.append(self.passage_image)
        if self.image is not None:
            parts.append(self.image)
        parts.extend(self.continuations)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        gap = 14
        w = max(p.width for p in parts)
        h = sum(p.height for p in parts) + gap * (len(parts) - 1)
        canvas = Image.new("RGB", (w, h), "white")
        y = 0
        for p in parts:
            canvas.paste(p, (0, y))
            y += p.height + gap
        return canvas


BRACKET_NUM_RE = re.compile(r"^\[\s*(\d+)\s*\]$")


def extract_bracket_number(lines):
    for l in lines:
        m = BRACKET_NUM_RE.match(l["text"].strip())
        if m:
            return int(m.group(1))
    return None


def resolve_topics_per_page(doc, fallback_title="Unknown Topic"):
    n = len(doc)
    titles = [None] * n
    brackets = [None] * n
    for i in range(n):
        page = doc[i]
        lines = get_lines(page)
        titles[i] = detect_topic_title(lines, page.rect.height)
        brackets[i] = extract_bracket_number(lines)

    boundaries = [0]
    for i in range(1, n):
        if brackets[i] is not None and brackets[i - 1] is not None and brackets[i] < brackets[i - 1]:
            boundaries.append(i)
    boundaries.append(n)

    final_topic = [None] * n
    current = fallback_title
    for b in range(len(boundaries) - 1):
        start, end = boundaries[b], boundaries[b + 1]
        run_title = None
        for i in range(start, end):
            if titles[i]:
                run_title = titles[i]
                break
        if run_title:
            current = run_title
        for i in range(start, end):
            final_topic[i] = current
    return final_topic


def detect_topic_title(lines, page_h):
    if not lines:
        return None
    l = lines[0]
    if l["y0"] > 80:
        return None
    t = l["text"].strip()
    tu = t.upper()
    if not t or tu in GENERIC_HEADER_WORDS:
        return None
    if PAGEBRACKET_RE.match(t):
        return None
    if len(t) < 3:
        return None
    return t


def find_banners(lines, page_h):
    candidates = []
    for l in lines:
        for pat, section in EXERCISE_PATTERNS:
            if pat.search(l["text"]):
                candidates.append({"y0": l["y0"], "y1": l["y1"], "kind": "exercise", "section": section})
                break
        if ANSWER_KEY_RE.search(l["text"]):
            candidates.append({"y0": l["y0"], "y1": l["y1"], "kind": "answerkey", "section": None})

    if not candidates:
        return []

    candidates.sort(key=lambda c: c["y0"])
    groups = []
    used = [False] * len(candidates)
    for i, c in enumerate(candidates):
        if used[i]:
            continue
        group = [c]
        used[i] = True
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            if candidates[j]["y0"] - group[-1]["y0"] < 25:
                group.append(candidates[j])
                used[j] = True
        section = None
        is_answerkey = False
        y0 = min(g["y0"] for g in group)
        y1 = max(g["y1"] for g in group)
        for g in group:
            if g["kind"] == "exercise":
                section = g["section"]
            if g["kind"] == "answerkey":
                is_answerkey = True
        if section is None:
            continue
        groups.append({
            "y0": y0, "y1": y1, "section": section,
            "mode": "answerkey" if is_answerkey else "questions",
        })
    groups.sort(key=lambda g: g["y0"])
    return groups


OPTION_LINE_RE = re.compile(r"^\(\d\)")


def is_conceptual_subtopic(text):
    t = text.strip()
    if len(t) < 3 or len(t) > 45:
        return False
    if OPTION_LINE_RE.match(t):
        return False
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) < 4:
        return False
    if letters != letters.upper():
        return False
    if any(k in t.upper() for k in ["ANSWER", "EXERCISE", "PARAGRAPH", "OPTION", "COLUMN", "NEET", "ALLEN"]):
        return False
    if re.search(r"\d", t):
        return False
    return True


def is_pyq_subtopic(text):
    t = text.strip()
    if len(t) < 3 or len(t) > 45:
        return False
    if OPTION_LINE_RE.match(t):
        return False
    if any(k in t.upper() for k in ["ANSWER", "EXERCISE", "PARAGRAPH", "OPTION", "COLUMN"]):
        return False
    if YEAR_RE.search(t):
        return True
    if re.search(r"\bAIPMT\b|\bNEET\b", t, re.I):
        return True
    return False


COL_TOL = 0.60

def _group_word_rows(words, y0, y1):
    inside = []
    for w in words:
        wx0, wy0, wx1, wy1 = w[0], w[1], w[2], w[3]
        text = w[4].strip()
        if not text:
            continue
        cy = (wy0 + wy1) / 2
        if y0 - 2 <= wy0 and wy1 <= y1 + 2:
            inside.append({"x0": wx0, "y0": wy0, "x1": wx1, "y1": wy1,
                           "cx": (wx0 + wx1) / 2, "cy": cy, "text": text})
    if not inside:
        return []
    inside.sort(key=lambda w: w["cy"])

    rows = []
    cur_row = [inside[0]]
    for w in inside[1:]:
        if abs(w["cy"] - cur_row[-1]["cy"]) < 4:
            cur_row.append(w)
        else:
            rows.append(sorted(cur_row, key=lambda w: w["x0"]))
            cur_row = [w]
    rows.append(sorted(cur_row, key=lambda w: w["x0"]))
    return rows


def _is_label_word(text):
    t = text.strip().rstrip(".").lower()
    return t in ("question", "que", "q", "answer", "ans", "a")


def _extract_digit_words(row):
    digits = []
    for w in row:
        text = w["text"]
        if _is_label_word(text):
            continue
        only_digits = "".join(ch for ch in text if ch.isdigit())
        if not only_digits:
            continue
        if len(only_digits) == 1:
            digits.append({"ch": only_digits, "cx": w["cx"]})
        else:
            w_width = w["x1"] - w["x0"]
            for k, ch in enumerate(only_digits):
                frac = (k + 0.5) / len(only_digits)
                cx = w["x0"] + frac * w_width
                digits.append({"ch": ch, "cx": cx})
    return digits


def _cells_from_rows(q_digits, a_digits):
    if not a_digits or not q_digits:
        return {}
    anchors = [d["cx"] for d in a_digits]
    if len(anchors) == 1:
        num_str = "".join(d["ch"] for d in q_digits)
        if num_str.isdigit():
            return {int(num_str): int(a_digits[0]["ch"])}
        return {}

    pitch = (anchors[-1] - anchors[0]) / (len(anchors) - 1)
    buckets = {i: [] for i in range(len(anchors))}
    for d in q_digits:
        i = min(range(len(anchors)), key=lambda k: abs(anchors[k] - d["cx"]))
        if abs(anchors[i] - d["cx"]) <= pitch * COL_TOL:
            buckets[i].append(d)

    result = {}
    for i, digs in buckets.items():
        if not digs:
            continue
        num_str = "".join(d["ch"] for d in sorted(digs, key=lambda d: d["cx"]))
        if num_str.isdigit():
            result[int(num_str)] = int(a_digits[i]["ch"])
    return result


def parse_answer_key_spatial(raw_words, zone_y0, zone_y1):
    rows = _group_word_rows(raw_words, zone_y0, zone_y1)
    if len(rows) < 2:
        return {}

    digit_rows = []
    for row in rows:
        digits = _extract_digit_words(row)
        if digits:
            digit_rows.append(digits)

    if len(digit_rows) < 2:
        return {}

    result = {}
    for a, b in zip(digit_rows[0::2], digit_rows[1::2]):
        a_all_answers = all(d["ch"] in "1234" for d in a)
        b_all_answers = all(d["ch"] in "1234" for d in b)

        if not a_all_answers and b_all_answers:
            result.update(_cells_from_rows(a, b))
        elif a_all_answers and not b_all_answers:
            result.update(_cells_from_rows(b, a))
        elif b_all_answers:
            result.update(_cells_from_rows(a, b))

    return result


def parse_answer_key_text(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    result = {}
    q_row_re = re.compile(r"^(?:question|que\.?|q\.?)\s", re.I)
    a_row_re = re.compile(r"^(?:answer|ans\.?|a\.?)\s", re.I)
    i = 0
    while i < len(lines):
        if q_row_re.match(lines[i]):
            q_nums = re.findall(r"\d+", lines[i])
            if i + 1 < len(lines) and a_row_re.match(lines[i + 1]):
                a_vals = re.findall(r"\d+|[a-dA-D]", lines[i + 1])
                for q, a in zip(q_nums, a_vals):
                    try:
                        result[int(q)] = int(a) if a.isdigit() else a.upper()
                    except ValueError:
                        pass
                i += 2
                continue
        i += 1
    return result


def process_pdf(pdf_path, out_root, prefix=""):
    doc = fitz.open(pdf_path)
    fallback_name = os.path.splitext(os.path.basename(pdf_path))[0]
    topic_per_page = resolve_topics_per_page(doc, fallback_title=fallback_name)

    current_topic = topic_per_page[0] if topic_per_page else fallback_name
    current_section = None
    current_subtopic = {"conceptual": None, "pyq": "General", "analytical": None}
    section_counter = {"conceptual": 0, "pyq": 0, "analytical": 0}

    data = defaultdict(lambda: defaultdict(lambda: {"subtopics": defaultdict(list), "answerkeys": []}))
    pending_paragraph = {}
    last_question = None

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_w, page_h = page.rect.width, page.rect.height
        mid_x = page_w / 2.0
        lines = get_lines(page)
        if not lines:
            continue
        raw_words = page.get_text("words")
        page_tags = [(w[0], w[1], w[2], w[3], w[4].strip()) for w in raw_words
                     if TAG_RE.match(w[4].strip())]

        page_topic = topic_per_page[page_idx]
        if page_topic != current_topic:
            current_topic = page_topic
            for k in section_counter:
                section_counter[k] = 0
            current_subtopic = {"conceptual": None, "pyq": "General", "analytical": None}

        banners = find_banners(lines, page_h)

        filled = [d["rect"] for d in page.get_drawings()
                  if d.get("fill") and d["rect"].height < 40]
        banner_boxes = [r for r in filled if r.width > 400]
        heading_boxes = [r for r in filled if r.width > 120]
        for b in banners:
            for r in banner_boxes:
                if r.y0 <= b["y1"] and r.y1 >= b["y0"]:
                    b["y1"] = max(b["y1"], r.y1 + 2)

        footer_top = page_h
        for l in lines:
            if l["y0"] > page_h * 0.90 and re.search(r"ALLEN|WWW\.ALLEN", l["text"], re.I):
                footer_top = min(footer_top, l["y0"])

        header_bottom = 0.0
        if lines and lines[0]["y0"] < 80:
            header_bottom = lines[0]["y1"] + 5

        # Full-width hairlines are page furniture (header/footer rules and the
        # separators between question blocks) - never question content. Record
        # them so they can be wiped from the render: otherwise a single rule row
        # anchors the crop and drags a slab of dead space in with it.
        rule_bands = []
        for d in page.get_drawings():
            r = d["rect"]
            if r.width < 450 or r.height > 6:
                continue
            rule_bands.append((r.y0, r.y1))
            if r.y1 < page_h * 0.15:
                header_bottom = max(header_bottom, r.y1 + 2)
            elif r.y0 > page_h * 0.85:
                footer_top = min(footer_top, r.y0 - 2)

        img_np = None
        ink_rows = {}

        def ensure_img():
            nonlocal img_np
            if img_np is None:
                img_np = render_page(page)
                for ry0, ry1 in rule_bands:
                    a = max(0, int(ry0 * ZOOM) - 2)
                    b = min(img_np.shape[0], int(ry1 * ZOOM) + 3)
                    if b > a:
                        img_np[a:b, :] = 255
                # Tinted heading bands carry the exercise/subtopic label, which
                # is captured as folder structure and never belongs in a crop.
                for r in heading_boxes:
                    a = max(0, int(r.y0 * ZOOM) - 2)
                    b = min(img_np.shape[0], int(r.y1 * ZOOM) + 3)
                    c = max(0, int(r.x0 * ZOOM) - 2)
                    d = min(img_np.shape[1], int(r.x1 * ZOOM) + 3)
                    if b > a and d > c:
                        img_np[a:b, c:d] = 255
                # Profile the pristine render: question numbers get painted out
                # of img_np later, and that must not disturb the ink profile.
                ink_rows["left"] = column_ink_rows(img_np, 0, mid_x - 4)
                ink_rows["right"] = column_ink_rows(img_np, mid_x + 4, page_w)
            return img_np

        def ensure_ink(col_name):
            ensure_img()
            return ink_rows[col_name]

        zone_starts = [header_bottom] + [b["y1"] for b in banners]
        zone_ends = [b["y0"] for b in banners] + [footer_top]
        seq = []
        sect = current_section
        for b in banners:
            seq.append((sect, "questions"))
            sect = b["section"]
        seq.append((sect, banners[-1]["mode"] if banners else "questions"))
        current_section = sect

        real_zones = []
        for i in range(len(zone_starts)):
            sec, mode = seq[i]
            real_zones.append({"y0": zone_starts[i], "y1": zone_ends[i], "section": sec, "mode": mode})

        for zone in real_zones:
            if zone["section"] is None:
                continue
            if zone["y1"] - zone["y0"] < 5:
                continue

            if zone["mode"] == "answerkey":
                arr = ensure_img()
                topimg = crop_tight(arr, page_w * 0.03 * ZOOM, zone["y0"] * ZOOM,
                                     page_w * 0.97 * ZOOM, zone["y1"] * ZOOM, pad_px=8)
                if topimg is not None:
                    zone_text_lines = []
                    for l in lines:
                        if zone["y0"] <= l["y0"] < zone["y1"]:
                            zone_text_lines.append(l["text"])
                    zone_text = "\n".join(zone_text_lines)
                    data[current_topic][zone["section"]]["answerkeys"].append({
                        "image": topimg,
                        "text": zone_text,
                        "raw_words": raw_words,
                        "zone_y0": zone["y0"],
                        "zone_y1": zone["y1"],
                    })
                continue

            zone_lines = [l for l in lines if zone["y0"] <= l["y0"] < zone["y1"]]

            for col_name, col_lo, col_hi in (("left", 0, mid_x), ("right", mid_x, page_w)):
                col_lines = [l for l in zone_lines if col_lo <= l["x0"] < col_hi]
                if not col_lines:
                    continue

                q_candidates = []
                for w in raw_words:
                    wx0, wy0, wx1, wy1, wtext = w[0], w[1], w[2], w[3], w[4]
                    if not (zone["y0"] <= wy0 < zone["y1"]):
                        continue
                    if not (col_lo <= wx0 < col_hi):
                        continue
                    if w[7] != 0:
                        continue
                    mm = QNUM_RE.match(wtext.strip())
                    if mm:
                        q_candidates.append({"num": int(mm.group(1)), "y0": wy0, "y1": wy1, "x0": wx0, "x1": wx1})
                
                if q_candidates:
                    margin = min(c["x0"] for c in q_candidates)
                    q_candidates = [c for c in q_candidates if abs(c["x0"] - margin) < 8]
                q_candidates.sort(key=lambda c: c["y0"])
                filtered, last_num = [], 0
                for c in q_candidates:
                    if c["num"] > last_num:
                        filtered.append(c)
                        last_num = c["num"]
                q_candidates = filtered

                events = []
                for c in q_candidates:
                    events.append({"type": "question", "y0": c["y0"], "y1": c["y1"], "num": c["num"], "x0": c["x0"], "x1": c["x1"]})

                subtopic_events = []
                if zone["section"] == "conceptual":
                    for l in col_lines:
                        if l["text"].strip() == current_topic or l["text"].strip().upper() in GENERIC_HEADER_WORDS:
                            continue
                        if is_conceptual_subtopic(l["text"]):
                            subtopic_events.append({"type": "subtopic", "y0": l["y0"], "y1": l["y1"], "name": l["text"].strip()})
                elif zone["section"] == "pyq":
                    for l in col_lines:
                        if l["text"].strip() == current_topic or l["text"].strip().upper() in GENERIC_HEADER_WORDS:
                            continue
                        if is_pyq_subtopic(l["text"]):
                            subtopic_events.append({"type": "subtopic", "y0": l["y0"], "y1": l["y1"], "name": l["text"].strip()})
                subtopic_events.sort(key=lambda e: e["y0"])

                merged = []
                for ev in subtopic_events:
                    if merged and ev["y0"] - merged[-1]["y1"] < 20:
                        merged[-1]["name"] = merged[-1]["name"] + " " + ev["name"]
                        merged[-1]["y1"] = ev["y1"]
                    else:
                        merged.append(dict(ev))

                q_sorted = sorted(q_candidates, key=lambda c: c["y0"])
                for ev in merged:
                    nxt = next((q for q in q_sorted if q["y0"] > ev["y0"]), None)
                    if nxt is None or nxt["y0"] - ev["y1"] >= 30:
                        continue
                    # A subtopic heading sits on a pale tinted band. That tint is
                    # too light to register as ink, so grow the event over the
                    # whole band or its edges bleed into the neighbouring crops.
                    for r in heading_boxes:
                        if r.y0 <= ev["y1"] and r.y1 >= ev["y0"] and r.x0 < col_hi and r.x1 > col_lo:
                            ev["y0"] = min(ev["y0"], r.y0)
                            ev["y1"] = max(ev["y1"], min(r.y1, nxt["y0"]))
                    events.append(ev)

                for l in col_lines:
                    pm = PARAGRAPH_RE.search(l["text"])
                    if pm:
                        events.append({"type": "paragraph", "y0": l["y0"], "y1": l["y1"],
                                        "start": int(pm.group(1)), "end": int(pm.group(2))})

                events.sort(key=lambda e: e["y0"])

                x_left = col_lo if col_name == "left" else col_lo + 4
                x_right = col_hi - 4 if col_name == "left" else col_hi

                # Keep the zone's own bottom rule / banner edge out of the crops.
                zone_hi = max(zone["y0"], zone["y1"] - 3.0)

                # Snap every marker to the whitespace above it, so a question
                # keeps the structure that hangs above its number and does not
                # inherit the top of the next question's structure.
                inked = ensure_ink(col_name)
                # A question's code tag (CCN067) always closes that question, and
                # in the PYQ layout a year heading follows it only ~2pt below. Use
                # the tags as a floor so the search cannot cut above one and strand
                # it. TAG_DESCENDER_PT drops the glyph box's empty descender space.
                col_tag_bottoms = sorted(t[3] for t in page_tags
                                         if x_left <= t[0] and t[2] <= x_right
                                         and zone["y0"] <= t[1] < zone["y1"])
                floor_pt = zone["y0"]
                for ev in events:
                    tag_floor = max([tb - TAG_DESCENDER_PT for tb in col_tag_bottoms
                                     if tb - TAG_DESCENDER_PT <= ev["y0"]], default=floor_pt)
                    ev["vcut"], ev["vtop"] = gap_above(inked, ev["y0"],
                                                       max(floor_pt, tag_floor))
                    floor_pt = ev["y1"]

                can_attach = ((col_name == "right" or zone is real_zones[0])
                              and last_question is not None
                              and last_question.section == zone["section"])

                if not any(e["type"] == "question" for e in events):
                    col_tags = [t for t in page_tags
                                if x_left <= t[0] and t[2] <= x_right
                                and zone["y0"] <= t[1] < zone["y1"]]
                    if can_attach and col_tags:
                        end_y = max(t[3] for t in col_tags) + 3
                        arr = ensure_img()
                        tail = crop_tight(arr, x_left * ZOOM, zone["y0"] * ZOOM,
                                          x_right * ZOOM, end_y * ZOOM, min_ink=400)
                        if tail is not None:
                            last_question.continuations.append(tail)
                            last_question.rects.append((page_idx, x_left, zone["y0"], x_right, end_y))
                    continue

                if can_attach:
                    first_cut = events[0]["vcut"]
                    if first_cut - zone["y0"] > 5:
                        arr = ensure_img()
                        tail = crop_tight(arr, x_left * ZOOM, zone["y0"] * ZOOM,
                                          x_right * ZOOM, first_cut * ZOOM, min_ink=400)
                        if tail is not None:
                            last_question.continuations.append(tail)
                            last_question.rects.append((page_idx, x_left, zone["y0"], x_right, first_cut))

                def padded_top(e):
                    return max(e["vcut"], e["vtop"] - CROP_PAD_PT)

                def padded_bottom(e):
                    return min(e["vtop"], e["vcut"] + CROP_PAD_PT)

                prev_bottom = padded_top(events[0]) if events else zone["y0"]
                n_events = len(events)
                for i, ev in enumerate(events):
                    nxt = events[i + 1] if i + 1 < n_events else None
                    top = prev_bottom
                    if nxt is not None:
                        bottom = max(top, padded_bottom(nxt))
                        next_top = max(top, padded_top(nxt))
                    else:
                        # Last block in the column: stop just past its own last
                        # ink instead of running down to the zone boundary.
                        end = last_ink_before(inked, zone_hi, top)
                        bottom = max(top, min(zone_hi, end + CROP_PAD_PT))
                        next_top = bottom

                    if ev["type"] == "subtopic":
                        current_subtopic[zone["section"]] = ev["name"]
                        # Never let the heading bleed into the question below it.
                        prev_bottom = max(ev["y1"], next_top)
                        continue

                    if ev["type"] == "paragraph":
                        arr = ensure_img()
                        pimg = crop_tight(arr, x_left * ZOOM, top * ZOOM, x_right * ZOOM, bottom * ZOOM)
                        pending_paragraph[(page_idx, col_name)] = {
                            "start": ev["start"], "end": ev["end"], "img": pimg
                        }
                        prev_bottom = next_top
                        continue

                    if ev["type"] == "question":
                        num = ev["num"]
                        expected = section_counter[zone["section"]] + 1
                        if num != expected and not (num == 1 and section_counter[zone["section"]] == 0):
                            pass
                        section_counter[zone["section"]] = num

                        arr = ensure_img()

                        # --- NEW: Erase the question number to keep just the question text and options ---
                        px0, py0 = int(ev["x0"] * ZOOM), int(ev["y0"] * ZOOM)
                        px1, py1 = int(ev["x1"] * ZOOM), int(ev["y1"] * ZOOM)
                        # Paint a white bounding box cleanly over the number
                        cv2.rectangle(arr, (max(0, px0 - 4), max(0, py0 - 4)), (px1 + 4, py1 + 4), (255, 255, 255), -1)
                        # ---------------------------------------------------------------------------------

                        qimg = crop_tight(arr, x_left * ZOOM, top * ZOOM, x_right * ZOOM, bottom * ZOOM)

                        q = Question(current_topic, zone["section"], current_subtopic[zone["section"]], num)
                        q.image = qimg
                        q.rects.append((page_idx, x_left, top, x_right, bottom))

                        pp = pending_paragraph.get((page_idx, col_name))
                        if pp and pp["start"] <= num <= pp["end"]:
                            q.passage_image = pp["img"]
                            if num == pp["end"]:
                                pending_paragraph.pop((page_idx, col_name), None)

                        subtopic_key = current_subtopic[zone["section"]] or "General"
                        data[current_topic][zone["section"]]["subtopics"][subtopic_key].append(q)
                        last_question = q
                        prev_bottom = next_top

        print(f"Page {page_idx+1}/{len(doc)} done. topic={current_topic} section={current_section}")

    audit(doc, data)
    write_output(data, out_root, prefix)


def audit(doc, data):
    tags_by_page = {}
    total_tags = 0
    for i in range(len(doc)):
        found = []
        for w in doc[i].get_text("words"):
            if TAG_RE.match(w[4].strip()):
                found.append((w[0], w[1], w[2], w[3], w[4].strip()))
                total_tags += 1
        tags_by_page[i] = found

    if total_tags == 0:
        print("\n--- audit: no question code tags in document (skipped) ---")
        return

    problems = []
    for topic, sections in data.items():
        for section, content in sections.items():
            for subtopic, questions in content["subtopics"].items():
                for q in questions:
                    seen = set()
                    for (pi, x0, y0, x1, y1) in q.rects:
                        for (tx0, ty0, tx1, ty1, text) in tags_by_page.get(pi, []):
                            # Crops are trimmed to ink, so a glyph box can poke a
                            # couple of points past the rect - match on centre.
                            cx, cy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
                            if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                                seen.add(text)
                    if len(seen) != 1:
                        problems.append((topic, section, q.num, sorted(seen)))

    print("\n--- audit: one code tag per question crop ---")
    if not problems:
        print("OK: every question crop contains exactly one tag.")
    else:
        print(f"{len(problems)} question(s) need review:")
        for topic, section, num, seen in problems:
            print(f"  [{topic} / {section}] q{num}: tags={seen}")


def write_output(data, out_root, prefix=""):
    if os.path.exists(out_root):
        shutil.rmtree(out_root)
    os.makedirs(out_root, exist_ok=True)

    topic_dirs = []
    for topic, sections in data.items():
        topic_dir = os.path.join(out_root, sanitize(topic))
        os.makedirs(topic_dir, exist_ok=True)
        topic_dirs.append((topic, topic_dir))
        for section, content in sections.items():
            section_dir = os.path.join(topic_dir, SECTION_FOLDER.get(section, section))
            os.makedirs(section_dir, exist_ok=True)

            has_subtopics = section != "analytical" and len([k for k in content["subtopics"] if k != "General"]) > 0

            for subtopic, questions in content["subtopics"].items():
                if has_subtopics:
                    q_dir = os.path.join(section_dir, sanitize(subtopic))
                else:
                    q_dir = section_dir
                os.makedirs(q_dir, exist_ok=True)
                for q in sorted(questions, key=lambda x: x.num):
                    img = q.combined_image()
                    if img is None:
                        continue
                    img.save(os.path.join(q_dir, f"q{q.num}.png"))

            if content["answerkeys"]:
                ak_dir = os.path.join(section_dir, "Answer Key")
                os.makedirs(ak_dir, exist_ok=True)
                merged_answers = {} 
                for i, ak in enumerate(content["answerkeys"], 1):
                    suffix = "" if len(content["answerkeys"]) == 1 else f"_{i}"
                    ak["image"].save(os.path.join(ak_dir, f"answer_key{suffix}.png"))
                    parsed = {}
                    if "raw_words" in ak:
                        parsed = parse_answer_key_spatial(
                            ak["raw_words"], ak["zone_y0"], ak["zone_y1"])
                    if not parsed:
                        parsed = parse_answer_key_text(ak["text"])
                    if parsed:
                        merged_answers.update(parsed)
                        if len(content["answerkeys"]) > 1:
                            json_path = os.path.join(ak_dir, f"answer_key_{i}.json")
                            with open(json_path, "w", encoding="utf-8") as f:
                                json.dump(parsed, f, indent=2, ensure_ascii=False)
                            print(f"  -> Saved block JSON: {json_path} ({len(parsed)} entries)")
                    else:
                        print(f"  !! Warning: could not parse answer key block {i} for {SECTION_FOLDER.get(section, section)}")

                if merged_answers:
                    sorted_answers = {str(k): v for k, v in sorted(merged_answers.items())}
                    merged_path = os.path.join(ak_dir, "answer_key.json")
                    with open(merged_path, "w", encoding="utf-8") as f:
                        json.dump(sorted_answers, f, indent=2, ensure_ascii=False)
                    print(f"  -> Saved merged answer_key.json: {merged_path} ({len(sorted_answers)} entries)")
                else:
                    print(f"  !! Warning: no answers parsed for {SECTION_FOLDER.get(section, section)} — answer_key.json NOT created")

    zip_dir = os.path.dirname(os.path.abspath(out_root))
    print(f"\nDone. Output folder: {out_root}")
    for topic, topic_dir in topic_dirs:
        prefix_str = prefix.strip().replace(" ", "_")
        if prefix_str and not prefix_str.endswith("_"):
            prefix_str += "_"
        
        clean_topic = sanitize(topic).replace(" ", "_").replace(",", "")
        zip_path = os.path.join(zip_dir, prefix_str + clean_topic + ".zip")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        n = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(topic_dir):
                for f in sorted(files):
                    full = os.path.join(root, f)
                    zf.write(full, os.path.relpath(full, topic_dir))
                    n += 1
        print(f"Zip: {zip_path}  ({n} files)")


if __name__ == "__main__":
    pdf_file = sys.argv[1] if len(sys.argv) > 1 else "Complete Physics Module [Exercise] NEET 2026.pdf"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "question_bank_output"
    
    if len(sys.argv) > 3:
        prefix_arg = sys.argv[3]
    else:
        prefix_arg = input("Enter a prefix for the ZIP files (e.g. 'allen 23') or press Enter to skip: ").strip()
        
    process_pdf(pdf_file, out_dir, prefix_arg)