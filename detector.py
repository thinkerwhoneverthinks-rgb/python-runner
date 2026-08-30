"""Detector for 3-tier hybrid question classification.

Tier 1  – 'text'          Clean text; KaTeX / mhchem handle sub/superscripts (0 API cost).
Tier 2  – 'math_formula'  Complex math / garbled fractions / Avogadro patterns → single
                          batch Gemini call.
Tier 3  – 'diagram_visual' Vector drawings > 3 paths, raster images, circuit keywords →
                          full high-res visual crop (0 API cost).

Key improvements over previous version:
  • Garbled-fraction detection:  if any option is suspiciously short / numeric-only it
    almost certainly came from a mis-parsed visual fraction and must go to AI.
  • N_A / Avogadro detection:   N_{A}, N_A, or Avogadro constant mentions → Tier 2
    (KaTeX can't handle the complex surrounding expressions reliably).
  • Negative-exponent detection: 10^{-N} expressions → Tier 2 for clean LaTeX.
  • Abnormally long option 4:   if the last option is 3× longer than the median of
    the others, answer-key bleed-in probably occurred → Tier 2.
  • Diagram / visual check now also picks up reaction-arrow keywords.
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Dict, List, Optional
import fitz

# ─── Heavy math symbol set ───────────────────────────────────────────────────
HEAVY_MATH_SYMBOLS = {
    "∫", "∬", "∭", "∮", "∑", "∏", "√", "∛", "∜", "∝", "∞", "∠", "⊥", "∆", "∇",
    "∂", "≠", "≤", "≥", "≈", "≡", "⊂", "⊃", "⊆", "⊇", "∈", "∉", "∀", "∃", "∄",
    "∅", "∧", "∨", "∩", "∪", "⇒", "↔", "⇔",
}

# ─── Complex math regex patterns ────────────────────────────────────────────
COMPLEX_MATH_PATTERNS = [
    re.compile(r"\b(sin|cos|tan|cot|sec|cosec|lim|det|matrix|log|ln)\b", re.I),
    re.compile(r"[\uE000-\uF8FF]"),          # unmapped MathType / Symbol fonts
]

# ─── Avogadro / N_A patterns ────────────────────────────────────────────────
# Matches: N_A, N_{A}, N_A, NA (standalone), 6.02 × 10 style expressions in options
AVOGADRO_PATTERNS = [
    re.compile(r"\bN_\{A\}|\bN_A\b", re.I),
    re.compile(r"6\.02\s*[×x]\s*10", re.I),
    re.compile(r"\bAvogadro\b", re.I),
]

# ─── Negative / fractional exponent patterns ─────────────────────────────────
NEGATIVE_EXP_RE = re.compile(r"10\^?\{?-\d+", re.I)

# ─── Garbled fraction heuristics ────────────────────────────────────────────
# An option that is ONLY a short numeric string is almost certainly a denominator
# that was visually split from its numerator by PyMuPDF.
_GARBLED_LONE_NUMBER = re.compile(r"^\d+(\.\d+)?\s*$")
_GARBLED_BARE_MULTIPLY = re.compile(r"^\d[\d\s.]*[×xX]\s*$")   # e.g. "3 ×"
_GARBLED_SPACED_NUMS = re.compile(r"^[\d\s.]+$")                # e.g. "23 6.02 10"
# Option text with 4+ token groups that are mostly digits/operators – junk bleed-in
_GARBLED_JUNK_SEQUENCE = re.compile(r"(\b\d+[\d.\s]*[×x]\s*\d+\b.*){2,}", re.I)
# Single uppercase letter — almost always a fraction numerator (V, W, T…) split from its denominator
_GARBLED_SINGLE_LETTER = re.compile(r"^[A-Z]$")
# Letter immediately followed by digit(s) with NO operator — concatenation artifact (W1, T3, V22400)
_GARBLED_LETTER_DIGIT_CONCAT = re.compile(r"^[A-Z]\d+$")


# ─── Diagram keywords ───────────────────────────────────────────────────────
DIAGRAM_KEYWORDS = [
    "figure", "diagram", "circuit", "graph",
    "shown in the structure", "following structure",
    "following reaction scheme", "resonance structure",
    "chair conformation", "given diagram", "given circuit",
    "shown below", "in the figure", "reaction mechanism",
    "given below", "structural formula",
]


# ─────────────────────────────────────────────────────────────────────────────
def _detect_garbled_options(options_text: List[str]) -> Optional[str]:
    """
    Returns a reason string if the options look garbled (e.g. visual fractions
    were mis-parsed by PyMuPDF), or None if options look fine.

    Garbled indicators:
    1. A bare number (≤6 chars) NOT consistent with all other options
       being numbers too → likely a denominator separated from its numerator.
    2. An option ends with a stray '×' → fraction numerator leaked.
    3. Only space-separated digits → scrambled fraction/Avogadro.
    4. Single uppercase letter (V, W, T) → fraction numerator split from denominator.
    5. Letter+digit concat (W1, T22400) → numerator+denominator ran together.
    6. Last option disproportionately long → answer-key data bled in.
    """
    valid = [o.strip() for o in options_text if o.strip()]
    if not valid:
        return "All options are empty"

    # ── Pre-compute: how many options are purely short numbers?
    # If MOST options are short numbers it's a valid numeric-answer question
    # (e.g. x = 7, 4, 5, 6) — do NOT flag those as garbled.
    short_numeric_count = sum(
        1 for o in valid
        if _GARBLED_LONE_NUMBER.match(o) and len(o) <= 6
    )
    all_options_are_numbers = short_numeric_count >= max(2, len(valid) - 1)

    for i, opt in enumerate(valid):
        # 1. Lone number — only flag if other options are NOT all numbers
        if _GARBLED_LONE_NUMBER.match(opt) and len(opt) <= 6 and not all_options_are_numbers:
            return f"Option {i+1} is a bare number (likely a mis-parsed fraction denominator): '{opt}'"

        # 2. Stray trailing multiply sign
        if _GARBLED_BARE_MULTIPLY.match(opt):
            return f"Option {i+1} ends with stray multiply sign: '{opt}'"

        # 3. All-numeric space-separated sequence
        if _GARBLED_SPACED_NUMS.match(opt) and len(opt.split()) >= 3:
            return f"Option {i+1} looks like garbled numeric sequence: '{opt}'"

        # 4. Repeated numeric/operator junk (bleed-in)
        if _GARBLED_JUNK_SEQUENCE.search(opt):
            return f"Option {i+1} contains repeated numeric/operator junk: '{opt[:60]}'"

        # 5. Single uppercase letter → fraction numerator that lost its denominator
        if _GARBLED_SINGLE_LETTER.match(opt):
            return f"Option {i+1} is a lone letter (fraction numerator split from denominator): '{opt}'"

        # 6. Letter+digit concatenation → numerator+denominator run together (e.g. W1, V22400)
        if _GARBLED_LETTER_DIGIT_CONCAT.match(opt):
            return f"Option {i+1} looks like letter+digit concatenation artifact: '{opt}'"

    # Check option length disproportion (answer-key bleed into last option)
    if len(valid) >= 3:
        lengths = [len(o) for o in valid]
        median_len = sorted(lengths)[len(lengths) // 2]
        last_len = lengths[-1]
        if median_len > 0 and last_len > median_len * 3 and last_len >= 30:
            return f"Last option is suspiciously long ({last_len} chars vs median {median_len}) — possible answer-key bleed-in"

    return None


# ─────────────────────────────────────────────────────────────────────────────
def classify_question(
    question_text: str,
    options_text: List[str],
    page: Optional[fitz.Page] = None,
    bbox_pt: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """
    Returns a classification dict with keys:
        tier          : 'text' | 'math_formula' | 'diagram_visual'
        needs_crop    : bool
        needs_ai      : bool
        reasons       : List[str]
        default_mode  : 'text' | 'crop' | 'ai_text'
    """
    full_text = question_text + " " + " ".join(options_text)
    lower_text = full_text.lower()

    diagram_reasons: List[str] = []
    math_reasons: List[str] = []

    # ── Tier 3 checks: Diagram / Visual ──────────────────────────────────────
    if page is not None and bbox_pt is not None and len(bbox_pt) == 4:
        try:
            rect = fitz.Rect(bbox_pt[0], bbox_pt[1], bbox_pt[2], bbox_pt[3])

            images = page.get_images()
            if images:
                for img_info in images:
                    xref = img_info[0]
                    for img_rect in page.get_image_rects(xref):
                        if img_rect.intersects(rect):
                            # ── SIZE THRESHOLD ─────────────────────────────────
                            # Tiny images (<= 2500 pt²) are inline formula glyphs,
                            # e.g. isotope notation ¹²₆C, not actual diagrams.
                            # Only count images large enough to be real figures.
                            img_area = img_rect.width * img_rect.height
                            if img_area > 2500:
                                diagram_reasons.append("Contains embedded raster image")
                            break

            drawings = page.get_drawings()
            drawings_in_bbox = [d for d in drawings if fitz.Rect(d["rect"]).intersects(rect)]
            if len(drawings_in_bbox) > 3:
                diagram_reasons.append(f"Contains {len(drawings_in_bbox)} vector graphics/drawings")
        except Exception:
            pass

    for kw in DIAGRAM_KEYWORDS:
        if kw in lower_text:
            diagram_reasons.append(f"Mentions visual keyword: '{kw}'")
            break

    if diagram_reasons:
        return {
            "tier": "diagram_visual",
            "needs_crop": True,
            "needs_ai": False,
            "reasons": diagram_reasons,
            "default_mode": "crop",
        }

    # ── Tier 2 checks: Math / Formula / Garbled ───────────────────────────────

    # 1. Private-Use-Area unmapped math font glyphs
    pua_chars = [c for c in full_text if 0xE000 <= ord(c) <= 0xF8FF]
    if pua_chars:
        math_reasons.append(f"Contains {len(pua_chars)} unmapped custom font glyph(s)")

    # 2. Heavy calculus / matrix symbols
    heavy_chars = [c for c in full_text if c in HEAVY_MATH_SYMBOLS]
    if heavy_chars:
        math_reasons.append(f"Contains heavy math operators: {''.join(sorted(set(heavy_chars)))}")

    # 3. Complex math patterns (trig, limits, …)
    for pat in COMPLEX_MATH_PATTERNS:
        if pat.search(full_text):
            math_reasons.append(f"Matches complex math structure: {pat.pattern[:40]}")
            break

    # 4. Avogadro / N_A patterns
    for pat in AVOGADRO_PATTERNS:
        if pat.search(full_text):
            math_reasons.append("Contains Avogadro constant / N_A expression")
            break

    # 5. Negative or fractional exponents  (10^{-23} etc.)
    if NEGATIVE_EXP_RE.search(full_text):
        math_reasons.append("Contains negative or fractional exponent (10^{-N})")

    # 6. Garbled option detection
    garble_reason = _detect_garbled_options(options_text)
    if garble_reason:
        math_reasons.append(f"Options appear garbled: {garble_reason}")

    # 7. Fewer than 2 valid options
    valid_options = [o for o in options_text if o.strip()]
    if len(valid_options) < 2:
        math_reasons.append("Fewer than 2 valid options parsed from native text")

    if math_reasons:
        return {
            "tier": "math_formula",
            "needs_crop": False,
            "needs_ai": True,
            "reasons": math_reasons,
            "default_mode": "ai_text",
        }

    # ── Tier 1: Clean text ────────────────────────────────────────────────────
    return {
        "tier": "text",
        "needs_crop": False,
        "needs_ai": False,
        "reasons": ["Clean text and simple inline formulas"],
        "default_mode": "text",
    }


# Backwards-compatibility alias used by the old pipeline.py
def detect_question_complexity(
    question_text: str,
    options_text: List[str],
    page=None,
    bbox_pt=None,
):
    res = classify_question(question_text, options_text, page, bbox_pt)
    return {
        "needs_crop": res["needs_crop"],
        "needs_ai": res["needs_ai"],
        "tier": res["tier"],
        "reasons": res["reasons"],
    }
