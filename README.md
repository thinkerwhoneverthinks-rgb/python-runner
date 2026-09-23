# Extraction Runner Studio (Quizard & Ray Book Extractor)

A lightweight, non-AI extraction suite featuring:
1. **Quizard Extractor**: Playwright-based test discovery, question extraction, answer key parsing, and syllabus formatting for Quizzy.
2. **Ray Book Batch Extractor**: Downloads up to 30 encrypted books from PW & Streamfiles, reverses XOR ciphers, and removes semi-transparent watermarks using template-based nanmedian synthesis.

---

## ⚡ Features

- **Dual-Engine Web Interface**: Switch seamlessly between Quizard Extractor and Ray Book Extractor.
- **Batch Processing for up to 30 Books**: Enter Target URLs and custom names, with quick Bulk Paste support.
- **Watermark Removal**: Automatic repeating semi-transparent watermark inversion.
- **Flexible Downloads**: Download individual books with custom names, or download a consolidated ZIP archive.
- **Live Streaming Terminals**: Real-time console logs and progress bars.
- **Cloud & Local Execution**: Run locally with `python app.py` or deploy via GitHub Actions with Cloudflare tunnel.

---

## 🚀 Quick Start (Local)

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Launch Web Server**:
   ```bash
   python app.py --port 8080
   ```
   Open your browser at `http://localhost:8080`.

---

## 🛠 Standalone CLI Usage

- **Quizard Extractor**:
  ```bash
  python quizard_extractor.py
  ```

- **Ray Book Extractor**:
  ```bash
  python ray-book-extract.py --url "<viewer_url>" --token "Bearer <token>" --name "MyBook"
  ```
