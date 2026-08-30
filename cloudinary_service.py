"""Cloudinary Upload Service with Zero-Transformation-Credit Local Optimization.

Pre-optimizes images locally (WebP conversion + max-width scaling to 1000px)
before uploading directly to Cloudinary so that 0 Cloudinary Transformation
credits are consumed while maintaining crisp diagram readability.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image

try:
    import cloudinary
    import cloudinary.uploader
    HAS_CLOUDINARY = True
except ImportError:
    HAS_CLOUDINARY = False


def sanitize_path(path: str) -> str:
    """Sanitizes folder paths for Cloudinary folder names."""
    replacements = {
        '&': 'and', '#': '', '%': '', '@': '', '!': '',
        '+': 'plus', '=': '', '{': '', '}': '', '[': '', ']': '',
        '|': '', '\\': '/', ';': '', ':': '', '"': '', "'": '',
        '<': '', '>': '', '?': '', '^': '', '~': '', '`': ''
    }
    for char, replacement in replacements.items():
        path = path.replace(char, replacement)
    # Remove leading/trailing slashes and collapse multiple slashes
    return re.sub(r'/+', '/', path).strip('/')


def optimize_image_locally(
    input_path: Path,
    output_path: Path,
    max_width: int = 1000,
    quality: int = 85
) -> bool:
    """Resizes image to max_width using Lanczos and saves as high-quality WebP.
    
    This reduces file size by ~80% locally so that 0 transformation credits
    are billed on Cloudinary.
    """
    try:
        with Image.open(input_path) as im:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            
            w, h = im.size
            if w > max_width:
                new_w = max_width
                new_h = int(h * (max_width / w))
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            
            im.save(str(output_path), "WEBP", quality=quality, method=6)
            return True
    except Exception as e:
        print(f"[Image Optimization Error] {input_path}: {e}")
        return False


def upload_images_to_cloudinary(
    credentials: Dict[str, str],
    crops_to_upload: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, str]:
    """Uploads a list of crop items to Cloudinary and returns mapping of {crop_path: secure_url}.
    
    Args:
        credentials: dict with keys "cloud_name", "api_key", "api_secret", and optional "base_folder"
        crops_to_upload: list of dicts with {"crop_path": str, "folder_suffix": str, "public_id": str}
        progress_callback: callback reporting (current_step, total_steps, message)
        
    Returns:
        Dict mapping crop_path -> Cloudinary secure_url
    """
    if not HAS_CLOUDINARY:
        raise ImportError("cloudinary package is not installed.")

    cloud_name = credentials.get("cloud_name", "").strip()
    api_key = credentials.get("api_key", "").strip()
    api_secret = credentials.get("api_secret", "").strip()
    base_folder = credentials.get("base_folder", "quiz_app").strip()

    if not cloud_name or not api_key or not api_secret:
        raise ValueError("Cloudinary Cloud Name, API Key, and API Secret are all required.")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True
    )

    url_map: Dict[str, str] = {}
    total = len(crops_to_upload)

    with tempfile.TemporaryDirectory() as temp_dir:
        for idx, item in enumerate(crops_to_upload):
            crop_path_str = item.get("crop_path", "")
            if not crop_path_str or not os.path.exists(crop_path_str):
                continue

            crop_file = Path(crop_path_str)
            folder_suffix = sanitize_path(item.get("folder_suffix", ""))
            full_folder = sanitize_path(f"{base_folder}/{folder_suffix}")
            public_id = item.get("public_id", crop_file.stem)

            if progress_callback:
                progress_callback(idx + 1, total, f"Optimizing & Uploading {crop_file.name} ({idx+1}/{total})...")

            # 1. Pre-optimize locally to WebP (0 Cloudinary transformations!)
            opt_webp_path = Path(temp_dir) / f"{public_id}.webp"
            ok = optimize_image_locally(crop_file, opt_webp_path, max_width=1000, quality=85)
            upload_source = opt_webp_path if ok and opt_webp_path.exists() else crop_file

            # 2. Upload to Cloudinary with retry
            max_retries = 3
            uploaded_url = None
            for attempt in range(1, max_retries + 1):
                try:
                    res = cloudinary.uploader.upload(
                        str(upload_source),
                        folder=full_folder,
                        public_id=public_id,
                        unique_filename=False,
                        overwrite=True
                    )
                    uploaded_url = res.get("secure_url")
                    break
                except Exception as upload_err:
                    print(f"Cloudinary upload attempt {attempt} failed for {crop_file.name}: {upload_err}")
                    if attempt < max_retries:
                        time.sleep(1.5 * attempt)

            if uploaded_url:
                url_map[crop_path_str] = uploaded_url
                url_map[crop_file.name] = uploaded_url

    return url_map
