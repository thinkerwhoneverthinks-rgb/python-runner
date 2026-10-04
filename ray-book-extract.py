"""Ray Book Extract Standalone & CLI Tool for Python Runner.

Downloads and decrypts PW / Streamfiles books with XOR-cipher reversing
and performs automatic watermark removal.

Supported Modes:
1. Single/Multiple Books via Book ID: --book-id <id1,id2>
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
    parser.add_argument("--book-id", default="", help="Extract full book(s) via Book ID (comma-separated for multiple)")
    parser.add_argument("--cohort", default="", help="Extract cohort (e.g. 12th jee, dropper jee, 12th neet)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Authorization Bearer Token")
    parser.add_argument("--name", default="", help="Custom name for book or master zip")
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
        book_ids = [bid.strip() for bid in args.book_id.split(",") if bid.strip()]
        valid_books = []
        
        for bid in book_ids:
            print(f"🔍 Analysing Book ID '{bid}'...")
            res = RBE.analyse_book(bid, token)
            if not res.get("success"):
                print(f"❌ Error fetching ID {bid}:", res.get("error"))
            else:
                b = res["book"]
                # Only apply custom name to the book if there is exactly 1 ID being extracted
                book_title = args.name if (args.name and len(book_ids) == 1) else b["title"]
                b["title"] = book_title
                print(f"✅ Discovered Book: {book_title} ({b['total_chapters']} chapters)")
                valid_books.append(b)
                
        if not valid_books:
            print("❌ No valid books could be extracted. Exiting.")
            sys.exit(1)
            
        # Determine master ZIP name based on number of valid books
        if len(valid_books) == 1:
            master_zip_name = f"{RBE.sanitize_filename(valid_books[0]['title'])}.zip"
        else:
            # If multiple books, use args.name for the master zip if provided, else default
            raw_master = args.name if args.name else "Multiple_Extracted_Books"
            master_zip_name = f"{RBE.sanitize_filename(raw_master)}.zip"

        job = RBE.RayBookJob(
            mode="book",
            books=valid_books,
            zip_name=master_zip_name,
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