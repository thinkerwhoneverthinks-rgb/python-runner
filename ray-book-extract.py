"""Ray Book Extract Standalone & CLI Tool for Python Runner.

Downloads and decrypts PW / Streamfiles books with XOR-cipher reversing
and performs automatic watermark removal.

Supported Modes:
1. Single Book via Book ID: --book-id <id>
2. Cohort Batch: --cohort <12th jee | dropper jee | 12th neet | dropper neet>
3. Direct URL / asset_ref: --url <url>
"""

from __future__ import annotations
import sys
from pathlib import Path

# Add current directory to sys.path
sys.path.insert(0, str(Path(__file__).parent))

import ray_book_extractor as RBE

DEFAULT_TOKEN = RBE.DEFAULT_TOKEN
DEFAULT_TARGET_URL = "https://books.streamfiles.eu.org/viewer.php?asset_ref=2b7230684f67493064786e755571365070684a6153787a6538745141567a7a464b364e58747551614f49594b62655a483662373262436a563643384b78392b5043544b33383354455173584b6138454b30417a3239574c4e347754494f78764564414d336766414e4f426b2f6771775770687a466d336f662f49675632516570576177416d48685a6c71424e5044392f617a74366769426a726e63312f4c5a626463426a396e6965616d633d"

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ray Book Extractor & Watermark Remover")
    parser.add_argument("--url", default="", help="Target chapter URL or asset_ref")
    parser.add_argument("--book-id", default="", help="Extract full book via Book ID (e.g. 699eb4309a1240f7a2d435ba)")
    parser.add_argument("--cohort", default="", help="Extract cohort (e.g. 12th jee, dropper jee, 12th neet)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Authorization Bearer Token")
    parser.add_argument("--name", default="", help="Custom name for book or zip")
    parser.add_argument("--no-clean", action="store_true", help="Skip watermark removal")
    args = parser.parse_args()

    token = args.token or DEFAULT_TOKEN

    if args.cohort:
        print(f"🔍 Analysing Cohort '{args.cohort}'...")
        res = RBE.analyse_cohort(args.cohort, token)
        if not res.get("success"):
            print("❌ Error:", res.get("error"))
            sys.exit(1)
        print(f"✅ Discovered {res['total_books']} books with {res['total_chapters']} total chapters.")
        job = RBE.RayBookJob(
            mode="cohort",
            books=res["books"],
            cohort_name=res["cohort"],
            default_token=token,
            remove_watermarks=not args.no_clean
        )
        job.run()
    elif args.book_id:
        print(f"🔍 Analysing Book ID '{args.book_id}'...")
        res = RBE.analyse_book(args.book_id, token)
        if not res.get("success"):
            print("❌ Error:", res.get("error"))
            sys.exit(1)
        b = res["book"]
        book_title = args.name or b["title"]
        print(f"✅ Discovered Book: {book_title} ({b['total_chapters']} chapters)")
        b["title"] = book_title
        job = RBE.RayBookJob(
            mode="book",
            books=[b],
            zip_name=f"{RBE.sanitize_filename(book_title)}.zip",
            default_token=token,
            remove_watermarks=not args.no_clean
        )
        job.run()
    else:
        target_url = args.url or DEFAULT_TARGET_URL
        name = args.name or "Final_Extracted_Book"
        job = RBE.RayBookJob(
            mode="urls",
            items=[{"url": target_url, "custom_name": name, "token": token}],
            default_token=token,
            zip_name=f"{name}.zip",
            remove_watermarks=not args.no_clean
        )
        job.run()
