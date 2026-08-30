"""Cropper Bridge module.

Integrates with the high-resolution cropper engine (no_qn_code_copper.py)
to extract visual crops for questions that contain diagrams (d: true) without
aggressive content trimming or altering the underlying cropping engine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from PIL import Image

# Ensure original cropper folder is accessible
_CROPPER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cropper"))
if _CROPPER_DIR not in sys.path:
    sys.path.insert(0, _CROPPER_DIR)

try:
    import no_qn_code_copper as orig_cropper
except ImportError:
    orig_cropper = None


def extract_q_num_from_filename(filename: str) -> Optional[int]:
    """Extracts integer number from names like 'q14.png' or 'q_14.png'."""
    m = re.search(r'q_?(\d+)', filename, re.I)
    return int(m.group(1)) if m else None


def run_cropper_engine(
    pdf_path: Path,
    output_crops_dir: Path,
    diagram_q_nums: Optional[Set[int]] = None
) -> Dict[int, Dict[str, Any]]:
    """Runs the original cropper engine to generate exact crops.
    
    Copies and organizes the crops into output_crops_dir.
    If diagram_q_nums is provided, retains crops for those questions.
    
    Returns:
        Mapping of q_num -> {
            "crop_path": str,
            "image_filename": str,
            "relative_crop_path": str,
            "topic": str,
            "section": str,
            "subtopic": str
        }
    """
    output_crops_dir.mkdir(parents=True, exist_ok=True)
    crops_map: Dict[int, Dict[str, Any]] = {}
    
    if orig_cropper is None:
        print("[Cropper Bridge] Warning: no_qn_code_copper module not found.")
        return crops_map

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_out = Path(temp_dir) / "cropper_run"
        try:
            # Run the cropper engine
            orig_cropper.process_pdf(str(pdf_path), str(temp_out), prefix="")
        except Exception as e:
            print(f"[Cropper Bridge] Cropper run note: {e}")

        # Traverse the generated directory structure
        for root, dirs, files in os.walk(temp_out):
            if "answer key" in root.lower():
                continue
            
            rel_dir = os.path.relpath(root, temp_out)
            parts = rel_dir.replace("\\", "/").split("/")
            
            topic = parts[0] if len(parts) > 0 and parts[0] != "." else "General"
            section = parts[1] if len(parts) > 1 else "Conceptual Questions"
            subtopic = parts[2] if len(parts) > 2 else "General"

            for file in files:
                if file.lower().endswith((".png", ".jpg", ".jpeg")) and file.lower().startswith("q"):
                    q_num = extract_q_num_from_filename(file)
                    if q_num is None:
                        continue

                    # Copy to output crops dir
                    src_file = Path(root) / file
                    dest_filename = f"q_{q_num}.png"
                    dest_file = output_crops_dir / dest_filename
                    
                    try:
                        shutil.copy2(src_file, dest_file)
                        crops_map[q_num] = {
                            "crop_path": str(dest_file),
                            "image_filename": dest_filename,
                            "relative_crop_path": f"crops/{dest_filename}",
                            "topic": topic,
                            "section": section,
                            "subtopic": subtopic
                        }
                    except Exception as copy_err:
                        print(f"[Cropper Bridge] Error copying crop {file}: {copy_err}")

    return crops_map
