# Allen Hybrid PDF Extractor & Review Studio (Cloud & Local)

This repository contains the complete **Allen Hybrid PDF Extractor & Review Studio**, equipped with:
- Playwright Web Scrapers (`DeepSeek`, `Perplexity`, `Qwen`).
- 10-Block Modular Prompt Management.
- Multi-Exercise PDF Section Scoping & Boundary Continuation Handler.
- Diagram Cropping & Cloudinary Direct WebP Uploader.
- KaTeX + mhchem Chemistry & SMILES 2D Molecular Drawer.
- GitHub Actions Automated Workflow with OpenVPN + Cloudflare Tunnel.

---

## 🚀 GitHub Actions Setup Guide

### 1. Upload Your VPN Configs
Place your ProtonVPN `.ovpn` files (e.g. `japan.ovpn`, `netherlands.ovpn`, `us.ovpn`) inside the `.github/vpn/` folder in your repo:
```text
.github/
└── vpn/
    ├── netherlands.ovpn
    ├── japan.ovpn
    └── us.ovpn
```

### 2. Configure GitHub Secrets
Go to your GitHub Repository -> **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**:
1. `OVPN_USERNAME`: Your OpenVPN / ProtonVPN username.
2. `OVPN_PASSWORD`: Your OpenVPN / ProtonVPN password.

### 3. Launch via GitHub Actions
1. Go to the **Actions** tab on your GitHub repository.
2. Click **Launch PDF Extractor UI** on the left menu.
3. Click **Run workflow** and select:
   - `app.py` (Recommended: Full Web UI + Review Studio + Cropper).
   - Or standalone scripts (`deepseek.py`, `perplexity.py`, `qwen.py`).
4. Click on the running job logs to find your public URL:
   ```text
   https://xxxx-xxxx-xxxx.trycloudflare.com
   ```
5. Open the link in your browser to parse PDFs and review in Studio from anywhere!
