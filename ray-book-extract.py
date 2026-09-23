"""Ray Book Extract Standalone & CLI Tool for Python Runner.

Downloads and decrypts PW / Streamfiles books with XOR-cipher reversing
and performs automatic watermark removal.
"""

from __future__ import annotations
import sys
from pathlib import Path

# Add current directory to sys.path
sys.path.insert(0, str(Path(__file__).parent))

import ray_book_extractor as RBE

# Default configuration for fast standalone runs
DEFAULT_TOKEN = "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3ODg4NjM1MTQsImV4cCI6MTc4OTQ2ODMxNC4zNjQsImRhdGEiOnsiX2lkIjoiNjQxNTFiNjM3NmIwODEwMTBjMzk4NWVhIiwidXNlcm5hbWUiOiI4MjI0ODE1Njk3IiwiZmlyc3ROYW1lIjoiQWJoYXkgQW5hbmQiLCJsYXN0TmFtZSI6IkFuYW5kIiwib3JnYW5pemF0aW9uIjp7Il9pZCI6IjVlYjM5M2VlOTVmYWI3NDY4YTc5ZDE4OSIsIndlYnNpdGUiOiJwaHlzaWNzd2FsbGFoLmNvbSIsIm5hbWUiOiJQaHlzaWNzd2FsbGFoIn0sImVtYWlsIjoicnVuaWFuYW5kMDM4QGdtYWlsLmNvbSIsInJvbGVzIjpbIjViMjdiZDk2NTg0MmY5NTBhNzc4YzZlZiJdLCJjb3VudHJ5R3JvdXAiOiJJTiIsInR5cGUiOiJVU0VSIn0sImp0aSI6IllKRUdlVkYxUktXSmNvYXlzNXdMVXdfNjQxNTFiNjM3NmIwODEwMTBjMzk4NWVhIn0.D4XthP4Kx8gOv-goCbeHG-4dUpTObl3p4nzMtDKWOzc"
DEFAULT_TARGET_URL = "https://books.streamfiles.eu.org/viewer.php?asset_ref=2b7230684f67493064786e755571365070684a6153787a6538745141567a7a464b364e58747551614f49594b62655a483662373262436a563643384b78392b5043544b33383354455173584b6138454b30417a3239574c4e347754494f78764564414d336766414e4f426b2f6771775770687a466d336f662f49675632516570576177416d48685a6c71424e5044392f617a74366769426a726e63312f4c5a626463426a396e6965616d633d"

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ray Book Extractor & Watermark Remover")
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="Target book URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Authorization Bearer Token")
    parser.add_argument("--name", default="Final_Extracted_Book", help="Custom name for book")
    parser.add_argument("--no-clean", action="store_true", help="Skip watermark removal")
    args = parser.parse_args()

    job = RBE.RayBookJob(
        items=[{"url": args.url, "custom_name": args.name, "token": args.token}],
        default_token=args.token,
        zip_name=f"{args.name}.zip",
        remove_watermarks=not args.no_clean
    )
    job.run()
