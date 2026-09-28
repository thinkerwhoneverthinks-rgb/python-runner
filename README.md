# Extraction Runner Studio (Quizard, Ray Book & Allen Cropper)

A lightweight extraction and document processing suite featuring:

1. **Quizard Extractor**: Playwright-based test discovery, question extraction, answer key parsing, and syllabus formatting for Quizzy.
2. **Ray Book Batch Extractor**: Downloads encrypted books from Streamfiles, reverses XOR ciphers, and removes semi-transparent watermarks using template-based nanmedian synthesis.
3. **Allen Question Cropper**: Processes Allen NEET/JEE PDF modules, auto-detects topics and exercise sections (Conceptual, PYQ, Analytical, and Answer Keys), and slices question images without question numbers. Delivers topic-wise ZIP archives and/or Cloudinary uploads with formatted Quiz JSON.

---

## ⚡ Features

- **Triple-Engine Web Interface**: Seamless tab switching between **Quizard Extractor**, **Ray Book Extractor**, and **Allen Cropper**.
- **Mobile-First & Touch-Friendly**: Fully responsive layout designed for mobile phones (Android, iPhone) and desktop browsers with large tap targets and native file pickers.
- **Dual PDF Input Modes**:
  - **Upload PDF File**: Direct drag-and-drop or device file selection.
  - **Downloadable Link**: Paste direct PDF download URLs or Google Drive share links for server-side downloading.
- **Flexible Delivery Options**:
  - 📦 **ZIP Archive Only**: Downloads clean topic-wise ZIP archives of question crops.
  - ☁️ **Cloudinary + JSON Only**: Uploads cropped question images to Cloudinary and generates formatted Quiz JSON.
  - 🚀 **Both Simultaneously**: Get both the ZIP file and the Cloudinary JSON package in one run.
- **Manual Cloudinary Configuration**: Enter Cloudinary credentials directly into the web UI whenever you want to use the Cloudinary upload feature.
- **Theme Switcher (Dark & Light Mode)**: Toggle between sleek modern dark mode and clean high-contrast light mode with saved preferences across all screens.
- **Quizzy Practice Player (`quiz_player.html`)**: Instant practice player for exported Allen ZIP modules featuring instant answer reveal (no countdown timers), topic/subtopic navigator grid, dual One-by-One / All-Scroll modes, and 5-day IndexedDB local retention.
- **Live Streaming Terminals**: Real-time page-by-page progress bars, audit checks, and console logging.
- **Built-in Artifact Downloader**: Instant download buttons for ZIP and JSON files, plus a JSON clipboard copy button and live preview accordion.

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
   Open your browser at `http://localhost:8080` (or `http://<your-pc-ip>:8080` on mobile connected to the same Wi-Fi network).

---

## ✂️ Allen Cropper Usage

### Web Interface
1. Navigate to the **✂️ Allen Cropper** tab in the top navigation bar.
2. Select your input mode:
   - **Upload PDF File**: Choose an Allen module PDF from your device storage.
   - **Downloadable PDF Link**: Paste a direct link or Google Drive link.
3. (Optional) Provide a naming prefix for your output files (e.g. `allen 26`, `Mole Concept`).
4. Select your desired output format(s):
   - Check **Generate ZIP Archive** to get an organized ZIP.
   - Check **Cloudinary Upload & JSON** to upload images and generate structured Quiz JSON.
   - You can check **either or both**.
5. If uploading to Cloudinary, expand the Cloudinary settings panel and enter your credentials manually.
6. Click **🚀 Start Allen Cropping**.
7. Once finished, download the ZIP archive and/or JSON file directly from the results card.

---

## 🛠 Standalone CLI Usage

- **Allen Cropper**:
  ```bash
  python ../no_qn_code_copper.py "path/to/module.pdf" "output_folder" "prefix"
  ```

- **Quizard Extractor**:
  ```bash
  python quizard_extractor.py
  ```

- **Ray Book Extractor**:
  ```bash
  python ray-book-extract.py --url "<viewer_url>" --token "Bearer <token>" --name "MyBook"
  ```
