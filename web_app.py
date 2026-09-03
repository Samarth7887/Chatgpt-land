import base64
import html
import json
import mimetypes
import os
import re
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from time import perf_counter
from urllib.parse import parse_qs, urlparse
from datetime import datetime

import requests
from land_document_extractor import (
    OCRLine,
    OCRWord,
    extract_land_document,
    extract_land_document_from_lines,
    get_paddle_ocr_model,
    group_words_into_lines,
    normalize_space,
)
import verification_service
import socket

def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

# =====================================================================
# Kaggle / Colab OCR Tunnel Configuration
# =====================================================================
COLAB_OCR_URL = "https://union-remember-modems-joseph.trycloudflare.com"


def get_colab_url() -> str:
    env_val = os.environ.get("COLAB_OCR_URL")
    if env_val:
        return env_val.strip()

    txt_path = Path(__file__).parent / "colab_url.txt"
    if txt_path.exists():
        try:
            return txt_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return COLAB_OCR_URL.strip()


def extract_pdf_pages_to_images(uploaded_bytes: bytes) -> list[str]:
    """Converts a PDF file into a list of individual PNG temp file paths for per-page processing."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(uploaded_bytes)
    page_paths = []
    for page in pdf:
        pil_img = page.render(scale=2.5).to_pil()
        out_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        pil_img.save(out_file.name, format="PNG")
        out_file.close()
        page_paths.append(out_file.name)
    return page_paths


def process_uploaded_file(uploaded_bytes: bytes, filename: str) -> str:
    """Saves uploaded bytes to a temp file. If PDF, converts PDF pages into a single stacked PNG image."""
    is_pdf = filename.lower().endswith(".pdf") or uploaded_bytes.startswith(b"%PDF")
    if is_pdf:
        out_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        out_file.write(uploaded_bytes)
        out_file.close()
        return out_file.name
    else:
        suffix = Path(filename).suffix or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded_bytes)
            return tmp.name


# HTML Template for main app and verification console
HTML_PAGE = Template("""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Land Document Extractor & Verification Office</title>
  <style>
    :root {
      --bg: #f4efe6;
      --panel: #fffaf2;
      --ink: #1f2937;
      --muted: #6b7280;
      --accent: #7c2d12;
      --accent-2: #14532d;
      --border: #dccfb8;
      --shadow: 0 20px 60px rgba(31, 41, 55, 0.12);
      --status-extracted: #3b82f6;
      --status-needs-review: #f59e0b;
      --status-ready: #10b981;
      --status-approved: #047857;
      --status-rejected: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(124, 45, 18, 0.08), transparent 35%),
        radial-gradient(circle at top right, rgba(20, 83, 45, 0.08), transparent 28%),
        var(--bg);
      color: var(--ink);
      min-height: 100vh;
    }
    .wrap {
      max-width: 1280px;
      margin: 0 auto;
      padding: 40px 20px 56px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 24px;
      align-items: stretch;
      margin-bottom: 24px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 28px;
    }
    .kicker {
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(124, 45, 18, 0.08);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    h1 {
      margin: 16px 0 10px;
      font-size: clamp(2rem, 4vw, 3.6rem);
      line-height: 1.02;
    }
    h2 {
      margin-top: 0;
      font-size: 1.8rem;
      border-bottom: 1.5px solid var(--border);
      padding-bottom: 8px;
    }
    p { line-height: 1.6; color: var(--muted); }
    form {
      display: grid;
      gap: 14px;
    }
    input[type=file] {
      width: 100%;
      padding: 18px;
      border: 1px dashed var(--border);
      border-radius: 16px;
      background: #fff;
      color: var(--muted);
    }
    .btn {
      border: 0;
      border-radius: 14px;
      padding: 14px 18px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
    }
    .btn-primary {
      background: linear-gradient(135deg, var(--accent), #9a3412);
      color: white;
    }
    .btn-secondary {
      background: #e2e8f0;
      color: #1f2937;
      border: 1px solid #cbd5e1;
    }
    .btn-success {
      background: linear-gradient(135deg, var(--accent-2), #15803d);
      color: white;
    }
    .btn-danger {
      background: linear-gradient(135deg, #dc2626, #b91c1c);
      color: white;
    }
    .btn:hover { filter: brightness(1.05); }
    .btn:disabled {
      background: #e2e8f0;
      color: #94a3b8;
      cursor: not-allowed;
      border: 1px solid #cbd5e1;
    }
    .status-banner {
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(20, 83, 45, 0.08);
      color: var(--accent-2);
      font-size: 14px;
      margin-top: 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: 0.8fr 1.2fr;
      gap: 24px;
      align-items: start;
    }
    .preview, .output { padding: 24px; }
    .preview img {
      width: 100%;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: white;
    }
    
    /* Verification Console Layout */
    .console-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 20px;
    }
    .badge {
      padding: 8px 16px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 14px;
      text-transform: uppercase;
    }
    .badge-extracted { background: #dbeafe; color: #1e40af; border: 1.5px solid #bfdbfe; }
    .badge-needs_review { background: #fef3c7; color: #92400e; border: 1.5px solid #fde68a; }
    .badge-ready_for_approval { background: #d1fae5; color: #065f46; border: 1.5px solid #a7f3d0; }
    .badge-approved { background: #d1fae5; color: #065f46; border: 2.5px solid var(--status-approved); }
    .badge-rejected { background: #fee2e2; color: #991b1b; border: 2.5px solid var(--status-rejected); }
    
    .checklist {
      display: grid;
      gap: 12px;
      margin-bottom: 24px;
    }
    .check-item {
      padding: 14px;
      border-radius: 14px;
      border: 1.5px solid var(--border);
      background: rgba(255,255,255,0.6);
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 12px;
      align-items: start;
    }
    .check-status-icon {
      font-size: 20px;
      font-weight: bold;
    }
    .status-PASS { color: #15803d; }
    .status-WARNING { color: #b45309; }
    .status-FAIL { color: #b91c1c; }
    
    .check-title {
      font-weight: 700;
      margin: 0;
      font-size: 15px;
    }
    .check-desc {
      font-size: 13px;
      color: var(--muted);
      margin: 4px 0 0 0;
    }
    
    .editor-group {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-bottom: 12px;
    }
    .editor-field {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .editor-field label {
      font-weight: 700;
      font-size: 14px;
    }
    .editor-field input, .editor-field select, .editor-field textarea {
      padding: 10px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: white;
      color: var(--ink);
      font-family: inherit;
    }
    .editor-field input:disabled, .editor-field select:disabled, .editor-field textarea:disabled {
      background: #f1f5f9;
      color: #64748b;
      cursor: not-allowed;
    }
    
    .action-panel {
      display: flex;
      gap: 12px;
      margin-top: 20px;
      border-top: 1px solid var(--border);
      padding-top: 20px;
    }
    
    .certificate-card {
      background: #fafaf9;
      border: 3px double var(--accent-2);
      border-radius: 18px;
      padding: 24px;
      margin-top: 24px;
      position: relative;
    }
    .cert-seal {
      position: absolute;
      top: 20px;
      right: 20px;
      border: 3px solid var(--accent-2);
      color: var(--accent-2);
      padding: 8px 16px;
      border-radius: 8px;
      transform: rotate(15deg);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 14px;
    }
    .cert-field {
      margin-bottom: 8px;
      font-size: 14px;
    }
    .cert-field strong {
      display: inline-block;
      width: 180px;
      color: var(--ink);
    }
    .signature-box {
      background: #f1f5f9;
      padding: 12px;
      border-radius: 10px;
      font-family: monospace;
      font-size: 12px;
      word-break: break-all;
      border: 1px solid #cbd5e1;
      margin-top: 10px;
      max-height: 80px;
      overflow-y: auto;
    }
    
    .qr-container {
      display: flex;
      gap: 16px;
      align-items: center;
      margin-top: 18px;
      background: #fff;
      padding: 14px;
      border-radius: 14px;
      border: 1px dashed var(--border);
    }
    .qr-code-canvas {
      background: white;
      padding: 4px;
      border: 1px solid #ddd;
    }
    
    pre {
      margin: 0;
      padding: 18px;
      overflow: auto;
      max-height: 40vh;
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 18px;
      font-size: 13px;
      line-height: 1.55;
    }
    @media (max-width: 920px) {
      .hero, .grid, .editor-group { grid-template-columns: 1fr; }
    }
    
    /* Workflow Progress Indicator */
    .steps-container {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      background: rgba(31, 41, 55, 0.03);
      padding: 12px 16px;
      border-radius: 16px;
      border: 1px solid var(--border);
    }
    .step-item {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex: 1;
      position: relative;
      text-align: center;
    }
    .step-item:not(:last-child)::after {
      content: "";
      position: absolute;
      top: 14px;
      left: 50%;
      width: 100%;
      height: 2px;
      background: var(--border);
      z-index: 1;
    }
    .step-dot {
      width: 30px;
      height: 30px;
      border-radius: 50%;
      background: #e2e8f0;
      border: 2px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      font-weight: 700;
      z-index: 2;
      color: var(--muted);
    }
    .step-label {
      font-size: 11px;
      font-weight: 700;
      margin-top: 6px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .step-item.active .step-dot {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }
    .step-item.active .step-label {
      color: var(--accent);
    }
    .step-item.completed .step-dot {
      background: var(--accent-2);
      color: white;
      border-color: var(--accent-2);
    }
    .step-item.completed .step-label {
      color: var(--accent-2);
    }
    .step-item.rejected .step-dot {
      background: var(--status-rejected);
      color: white;
      border-color: var(--status-rejected);
    }
    .step-item.rejected .step-label {
      color: var(--status-rejected);
    }
    @media (max-width: 768px) {
      .steps-container {
        flex-direction: column;
        gap: 12px;
        align-items: flex-start;
      }
      .step-item {
        flex-direction: row;
        gap: 12px;
        text-align: left;
        width: 100%;
      }
      .step-item:not(:last-child)::after {
        display: none;
      }
      .step-label {
        margin-top: 0;
      }
      .action-panel {
        flex-direction: column;
        align-items: stretch;
      }
      .action-panel div {
        flex-direction: column;
        align-items: stretch;
        width: 100%;
      }
      .action-panel div input {
        width: 100%;
      }
      .action-panel div button {
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <!-- HERO SECTION -->
    <div class="hero">
      <section class="card intro">
        <span class="kicker">Government Registry Desk</span>
        <h1>Land Record Registry Office</h1>
        <p>
          State-level document verification engine. Process scans locally, execute cryptographic validation checks, correct extraction anomalies, and certify documents with official RSA-PSS seals.
        </p>
        <div style="margin-top: 10px;">
          <a href="/" class="btn btn-secondary">New Upload</a>
        </div>
      </section>
      
      <section class="card upload">
        <form action="/extract" method="post" enctype="multipart/form-data">
          <div>
            <label for="document_image" style="font-weight: 700; display: block; margin-bottom: 6px;">Select Scan Copy (Image or PDF):</label>
            <input type="file" name="document_image" id="document_image" accept="image/*,.pdf,application/pdf" required>
          </div>
          
          <div style="display: grid; gap: 6px;">
            <label for="processing_mode" style="font-weight: 700;">Processing Mode:</label>
            <select name="processing_mode" id="processing_mode" style="padding: 10px; border-radius: 10px; border: 1px solid var(--border); background: white; font-weight: 600;">
              <option value="gpu" $gpu_selected>⚡ Kaggle / Colab GPU Remote Tunnel (from colab_url.txt)</option>
              <option value="cpu" $cpu_selected>🐢 Local CPU (Pre-loaded PaddleOCR)</option>
            </select>
          </div>
          
          <button type="submit" class="btn btn-primary">Process Extraction</button>
        </form>
        <div class="status-banner">System is active locally. Key pair generated and managed at <strong>verification_keys/</strong>.</div>
      </section>
    </div>

    <!-- MAIN GRID SECTION -->
    <div class="grid">
      <section class="card preview">
        <h2>DOCUMENT PREVIEW</h2>
        $preview
      </section>

      <section class="card output">
        <!-- HEADER STATE -->
        <div class="console-header">
          <h2>Verification Console</h2>
          $badge_markup
        </div>
        
        $steps_markup
        $message_markup
        $timing_info
        
        $console_markup
      </section>
    </div>

    <!-- RAW JSON SECTION -->
    $raw_json_markup
  </div>

  <!-- OFFLINE CLIENT-SIDE QR GENERATION ENGINE -->
  <script>
    // Lightweight completely self-contained QR Code generator in JS (QRCode library wrapper)
    // Supports drawing QR codes onto HTML5 Canvas elements locally.
    (function(){
      var QRMode = { MODE_NUMBER: 1, MODE_ALPHA_NUM: 2, MODE_8BIT_BYTE: 4, MODE_KANJI: 8 };
      var QRErrorCorrectLevel = { L: 1, M: 0, Q: 3, H: 2 };
      var QRMaskPattern = { PATTERN000: 0, PATTERN001: 1, PATTERN010: 2, PATTERN011: 3, PATTERN100: 4, PATTERN101: 5, PATTERN110: 6, PATTERN111: 7 };
      var QRUtil = {
        PATTERN_POSITION_TABLE: [
          [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62],
          [6, 26, 46, 66], [6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78], [6, 30, 56, 82], [6, 30, 58, 86], [6, 34, 62, 90], [6, 28, 50, 72, 94],
          [6, 26, 50, 74, 98], [6, 30, 54, 78, 102], [6, 28, 54, 80, 106], [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
          [6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126], [6, 26, 52, 78, 104, 130], [6, 30, 56, 82, 108, 134], [6, 34, 60, 86, 112, 138],
          [6, 30, 58, 86, 114, 142], [6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150], [6, 24, 50, 76, 102, 128, 154], [6, 28, 54, 80, 106, 132, 158],
          [6, 32, 58, 84, 110, 136, 162], [6, 26, 54, 82, 110, 138, 166, 194], [6, 30, 58, 86, 114, 142, 170, 198]
        ],
        G15: (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0),
        G18: (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | (1 << 0),
        G15_MASK: (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1) | (1 << 0),
        getBchTypeInfo: function(data) { var d = data << 10; while (QRUtil.getBchDigit(d) - QRUtil.getBchDigit(QRUtil.G15) >= 0) { d ^= (QRUtil.G15 << (QRUtil.getBchDigit(d) - QRUtil.getBchDigit(QRUtil.G15))); } return ( (data << 10) | d) ^ QRUtil.G15_MASK; },
        getBchTypeNumber: function(data) { var d = data << 12; while (QRUtil.getBchDigit(d) - QRUtil.getBchDigit(QRUtil.G18) >= 0) { d ^= (QRUtil.G18 << (QRUtil.getBchDigit(d) - QRUtil.getBchDigit(QRUtil.G18))); } return (data << 12) | d; },
        getBchDigit: function(data) { var digit = 0; while (data != 0) { digit++; data >>>= 1; } return digit; },
        getPatternPositionTable: function(typeNumber) { return QRUtil.PATTERN_POSITION_TABLE[typeNumber - 1]; },
        getMask: function(maskPattern, i, j) {
          switch (maskPattern) {
            case QRMaskPattern.PATTERN000 : return (i + j) % 2 == 0;
            case QRMaskPattern.PATTERN001 : return i % 2 == 0;
            case QRMaskPattern.PATTERN010 : return j % 3 == 0;
            case QRMaskPattern.PATTERN011 : return (i + j) % 3 == 0;
            case QRMaskPattern.PATTERN100 : return (Math.floor(i / 2) + Math.floor(j / 3) ) % 2 == 0;
            case QRMaskPattern.PATTERN101 : return (i * j) % 2 + (i * j) % 3 == 0;
            case QRMaskPattern.PATTERN110 : return ( (i * j) % 2 + (i * j) % 3) % 2 == 0;
            case QRMaskPattern.PATTERN111 : return ( (i * j) % 3 + (i + j) % 2) % 2 == 0;
            default : throw new Error("bad maskPattern:" + maskPattern);
          }
        },
        getErrorCorrectPolynomial: function(errorCorrectLength) { var a = new QRPolynomial([1], 0); for (var i = 0; i < errorCorrectLength; i++) { a = a.multiply(new QRPolynomial([1, QRMath.gexp(i)], 0) ); } return a; },
        getLengthInBits: function(mode, type) {
          if (1 <= type && type < 10) {
            switch (mode) {
              case QRMode.MODE_NUMBER: return 10;
              case QRMode.MODE_ALPHA_NUM: return 9;
              case QRMode.MODE_8BIT_BYTE: return 8;
              case QRMode.MODE_KANJI: return 8;
              default: throw new Error("mode:" + mode);
            }
          } else if (type < 27) {
            switch (mode) {
              case QRMode.MODE_NUMBER: return 12;
              case QRMode.MODE_ALPHA_NUM: return 11;
              case QRMode.MODE_8BIT_BYTE: return 16;
              case QRMode.MODE_KANJI: return 10;
              default: throw new Error("mode:" + mode);
            }
          } else if (type < 41) {
            switch (mode) {
              case QRMode.MODE_NUMBER: return 14;
              case QRMode.MODE_ALPHA_NUM: return 13;
              case QRMode.MODE_8BIT_BYTE: return 16;
              case QRMode.MODE_KANJI: return 12;
              default: throw new Error("mode:" + mode);
            }
          } else {
            throw new Error("type:" + type);
          }
        },
        getLostPoint: function(qrCode) {
          var moduleCount = qrCode.getModuleCount();
          var lostPoint = 0;
          for (var row = 0; row < moduleCount; row++) {
            for (var col = 0; col < moduleCount; col++) {
              var sameColorCount = 0;
              var dark = qrCode.isDark(row, col);
              for (var r = -1; r <= 1; r++) {
                if (row + r < 0 || moduleCount <= row + r) { continue; }
                for (var c = -1; c <= 1; c++) {
                  if (col + c < 0 || moduleCount <= col + c) { continue; }
                  if (r == 0 && c == 0) { continue; }
                  if (dark == qrCode.isDark(row + r, col + c) ) { sameColorCount++; }
                }
              }
              if (sameColorCount > 5) { lostPoint += (3 + sameColorCount - 5); }
            }
          }
          for (var row = 0; row < moduleCount - 1; row++) {
            for (var col = 0; col < moduleCount - 1; col++) {
              var count = 0;
              if (qrCode.isDark(row, col) ) count++;
              if (qrCode.isDark(row + 1, col) ) count++;
              if (qrCode.isDark(row, col + 1) ) count++;
              if (qrCode.isDark(row + 1, col + 1) ) count++;
              if (count == 0 || count == 4) { lostPoint += 3; }
            }
          }
          for (var row = 0; row < moduleCount; row++) {
            for (var col = 0; col < moduleCount - 6; col++) {
              if (qrCode.isDark(row, col) && !qrCode.isDark(row, col + 1) && qrCode.isDark(row, col + 2) && qrCode.isDark(row, col + 3) && qrCode.isDark(row, col + 4) && !qrCode.isDark(row, col + 5) && qrCode.isDark(row, col + 6) ) {
                lostPoint += 40;
              }
            }
          }
          for (var col = 0; col < moduleCount; col++) {
            for (var row = 0; row < moduleCount - 6; row++) {
              if (qrCode.isDark(row, col) && !qrCode.isDark(row + 1, col) && qrCode.isDark(row + 2, col) && qrCode.isDark(row + 3, col) && qrCode.isDark(row + 4, col) && !qrCode.isDark(row + 5, col) && qrCode.isDark(row + 6, col) ) {
                lostPoint += 40;
              }
            }
          }
          var darkCount = 0;
          for (var col = 0; col < moduleCount; col++) {
            for (var row = 0; row < moduleCount; row++) {
              if (qrCode.isDark(row, col) ) { darkCount++; }
            }
          }
          var ratio = Math.abs(100 * darkCount / moduleCount / moduleCount - 50) / 5;
          lostPoint += ratio * 10;
          return lostPoint;
        }
      };
      var QRMath = {
        glog: function(n) { if (n < 1) { throw new Error("glog(" + n + ")"); } return QRMath.LOG_TABLE[n]; },
        gexp: function(n) { while (n < 0) { n += 255; } while (n >= 255) { n -= 255; } return QRMath.EXP_TABLE[n]; },
        EXP_TABLE: new Array(256),
        LOG_TABLE: new Array(256)
      };
      for (var i = 0; i < 8; i++) { QRMath.EXP_TABLE[i] = 1 << i; }
      for (var i = 8; i < 256; i++) { QRMath.EXP_TABLE[i] = QRMath.EXP_TABLE[i - 4] ^ QRMath.EXP_TABLE[i - 5] ^ QRMath.EXP_TABLE[i - 6] ^ QRMath.EXP_TABLE[i - 8]; }
      for (var i = 0; i < 255; i++) { QRMath.LOG_TABLE[QRMath.EXP_TABLE[i] ] = i; }
      
      function QRPolynomial(num, shift) {
        if (num.length == undefined) { throw new Error(num.length + "/" + shift); }
        var offset = 0;
        while (offset < num.length && num[offset] == 0) { offset++; }
        this.num = new Array(num.length - offset + shift);
        for (var i = 0; i < num.length - offset; i++) { this.num[i] = num[i + offset]; }
        for (var i = num.length - offset; i < this.num.length; i++) { this.num[i] = 0; }
      }
      QRPolynomial.prototype = {
        get: function(index) { return this.num[index]; },
        getLength: function() { return this.num.length; },
        multiply: function(e) {
          var num = new Array(this.getLength() + e.getLength() - 1);
          for (var i = 0; i < this.getLength(); i++) {
            for (var j = 0; j < e.getLength(); j++) {
              num[i + j] ^= QRMath.gexp(QRMath.glog(this.get(i) ) + QRMath.glog(e.get(j) ) );
            }
          }
          return new QRPolynomial(num, 0);
        },
        mod: function(e) {
          if (this.getLength() - e.getLength() < 0) { return this; }
          var ratio = QRMath.glog(this.get(0) ) - QRMath.glog(e.get(0) );
          var num = new Array(this.getLength() );
          for (var i = 0; i < this.getLength(); i++) { num[i] = this.get(i); }
          for (var i = 0; i < e.getLength(); i++) { num[i] ^= QRMath.gexp(QRMath.glog(e.get(i) ) + ratio); }
          return new QRPolynomial(num, 0).mod(e);
        }
      };
      
      var QRRSBlock = {
        RS_BLOCK_TABLE: [
          [1, 26, 19], [1, 26, 16], [1, 26, 13], [1, 26, 9], [1, 44, 34], [1, 44, 28], [1, 44, 22], [1, 44, 16],
          [1, 70, 55], [1, 70, 44], [2, 35, 17], [2, 35, 13], [1, 95, 80], [2, 47, 32], [2, 48, 24], [2, 48, 18],
          [1, 134, 108], [2, 67, 43], [2, 33, 15, 2, 34, 16], [2, 33, 11, 4, 34, 12], [2, 86, 68], [4, 43, 27], [4, 43, 19, 1, 44, 20], [4, 43, 15, 2, 44, 16],
          [2, 98, 78], [4, 49, 31], [2, 32, 14, 4, 33, 15], [4, 39, 13, 1, 40, 14], [2, 121, 97], [2, 60, 38, 2, 61, 39], [4, 40, 18, 2, 41, 19], [4, 40, 14, 2, 41, 15],
          [2, 146, 116], [3, 58, 36, 2, 59, 37], [4, 36, 16, 4, 37, 17], [4, 36, 12, 4, 37, 13], [2, 86, 68, 2, 87, 69], [4, 69, 43, 1, 70, 44], [6, 43, 19, 2, 44, 20], [6, 43, 15, 2, 44, 16],
          [4, 101, 80], [1, 80, 50, 4, 81, 51], [4, 50, 22, 4, 51, 23], [4, 50, 15, 4, 51, 16]
        ],
        getRSBlocks: function(typeNumber, errorCorrectLevel) {
          var list = QRRSBlock.getRsBlockTable(typeNumber, errorCorrectLevel);
          if (list == undefined) { throw new Error("bad rs block table for type:" + typeNumber + "/errorCorrectLevel:" + errorCorrectLevel); }
          var length = list.length / 3;
          var blocks = [];
          for (var i = 0; i < length; i++) {
            var count = list[i * 3 + 0];
            var totalCount = list[i * 3 + 1];
            var dataCount = list[i * 3 + 2];
            for (var j = 0; j < count; j++) { blocks.push(new QRRSBlock(totalCount, dataCount) ); }
          }
          return blocks;
        },
        getRsBlockTable: function(typeNumber, errorCorrectLevel) {
          switch (errorCorrectLevel) {
            case QRErrorCorrectLevel.L : return QRRSBlock.RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 0];
            case QRErrorCorrectLevel.M : return QRRSBlock.RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 1];
            case QRErrorCorrectLevel.Q : return QRRSBlock.RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 2];
            case QRErrorCorrectLevel.H : return QRRSBlock.RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 3];
            default : return undefined;
          }
        }
      };
      function QRRSBlock(totalCount, dataCount) { this.totalCount = totalCount; this.dataCount = dataCount; }
      
      function QRBitBuffer() { this.buffer = []; this.length = 0; }
      QRBitBuffer.prototype = {
        get: function(index) { var bufIndex = Math.floor(index / 8); return ( (this.buffer[bufIndex] >>> (7 - index % 8) ) & 1) == 1; },
        put: function(num, length) { for (var i = 0; i < length; i++) { this.putBit( ( (num >>> (length - i - 1) ) & 1) == 1); } },
        getLengthInBits: function() { return this.length; },
        putBit: function(bit) { var bufIndex = Math.floor(this.length / 8); if (this.buffer.length <= bufIndex) { this.buffer.push(0); } if (bit) { this.buffer[bufIndex] |= (0x80 >>> (this.length % 8) ); } this.length++; }
      };

      function QRCodeModel(typeNumber, errorCorrectLevel) {
        this.typeNumber = typeNumber;
        this.errorCorrectLevel = errorCorrectLevel;
        this.modules = null;
        this.moduleCount = 0;
        this.dataCache = null;
        this.dataList = [];
      }
      QRCodeModel.prototype = {
        addData: function(data) { var newData = new QR8bitByte(data); this.dataList.push(newData); this.dataCache = null; },
        isDark: function(row, col) { if (row < 0 || this.moduleCount <= row || col < 0 || this.moduleCount <= col) { throw new Error(row + "," + col); } return this.modules[row][col]; },
        getModuleCount: function() { return this.moduleCount; },
        make: function() { this.makeImpl(false, this.getBestMaskPattern() ); },
        makeImpl: function(test, maskPattern) {
          this.moduleCount = this.typeNumber * 4 + 17;
          this.modules = new Array(this.moduleCount);
          for (var row = 0; row < this.moduleCount; row++) { this.modules[row] = new Array(this.moduleCount); for (var col = 0; col < this.moduleCount; col++) { this.modules[row][col] = null; } }
          this.setupPositionFinderPattern(0, 0);
          this.setupPositionFinderPattern(this.moduleCount - 7, 0);
          this.setupPositionFinderPattern(0, this.moduleCount - 7);
          this.setupPositionAdjustPattern();
          this.setupTimingPattern();
          this.setupTypeInfo(test, maskPattern);
          if (this.typeNumber >= 7) { this.setupTypeNumber(test); }
          if (this.dataCache == null) { this.dataCache = QRCodeModel.createData(this.typeNumber, this.errorCorrectLevel, this.dataList); }
          this.mapData(this.dataCache, maskPattern);
        },
        setupPositionFinderPattern: function(row, col) {
          for (var r = -1; r <= 7; r++) {
            if (row + r <= -1 || this.moduleCount <= row + r) continue;
            for (var c = -1; c <= 7; c++) {
              if (col + c <= -1 || this.moduleCount <= col + c) continue;
              if ( (0 <= r && r <= 6 && (c == 0 || c == 6) ) || (0 <= c && c <= 6 && (r == 0 || r == 6) ) || (2 <= r && r <= 4 && 2 <= c && c <= 4) ) {
                this.modules[row + r][col + c] = true;
              } else {
                this.modules[row + r][col + c] = false;
              }
            }
          }
        },
        getBestMaskPattern: function() {
          var minLostPoint = 0;
          var pattern = 0;
          for (var i = 0; i < 8; i++) {
            this.makeImpl(true, i);
            var lostPoint = QRUtil.getLostPoint(this);
            if (i == 0 || minLostPoint > lostPoint) { minLostPoint = lostPoint; pattern = i; }
          }
          return pattern;
        },
        setupTimingPattern: function() {
          for (var r = 8; r < this.moduleCount - 8; r++) { if (this.modules[r][6] != null) { continue; } this.modules[r][6] = (r % 2 == 0); }
          for (var c = 8; c < this.moduleCount - 8; c++) { if (this.modules[6][c] != null) { continue; } this.modules[6][c] = (c % 2 == 0); }
        },
        setupPositionAdjustPattern: function() {
          var pos = QRUtil.getPatternPositionTable(this.typeNumber);
          for (var i = 0; i < pos.length; i++) {
            for (var j = 0; j < pos.length; j++) {
              var row = pos[i];
              var col = pos[j];
              if (this.modules[row][col] != null) { continue; }
              for (var r = -2; r <= 2; r++) {
                for (var c = -2; c <= 2; c++) {
                  if (Math.abs(r) == 2 || Math.abs(c) == 2 || (r == 0 && c == 0) ) {
                    this.modules[row + r][col + c] = true;
                  } else {
                    this.modules[row + r][col + c] = false;
                  }
                }
              }
            }
          }
        },
        setupTypeNumber: function(test) {
          var bits = QRUtil.getBchTypeNumber(this.typeNumber);
          for (var i = 0; i < 18; i++) {
            var mod = (!test && ( (bits >> i) & 1) == 1);
            this.modules[Math.floor(i / 3)][i % 3 + this.moduleCount - 8 - 3] = mod;
            this.modules[i % 3 + this.moduleCount - 8 - 3][Math.floor(i / 3)] = mod;
          }
        },
        setupTypeInfo: function(test, maskPattern) {
          var data = (this.errorCorrectLevel << 3) | maskPattern;
          var bits = QRUtil.getBchTypeInfo(data);
          for (var i = 0; i < 15; i++) {
            var mod = (!test && ( (bits >> i) & 1) == 1);
            if (i < 6) {
              this.modules[i][8] = mod;
            } else if (i < 8) {
              this.modules[i + 1][8] = mod;
            } else {
              this.modules[this.moduleCount - 15 + i][8] = mod;
            }
            if (i < 8) {
              this.modules[8][this.moduleCount - i - 1] = mod;
            } else if (i < 9) {
              this.modules[8][15 - i - 1 + 1] = mod;
            } else {
              this.modules[8][15 - i - 1] = mod;
            }
          }
          this.modules[this.moduleCount - 8][8] = !test;
        },
        mapData: function(data, maskPattern) {
          var inc = -1;
          var row = this.moduleCount - 1;
          var bitIndex = 0;
          var byteIndex = 0;
          for (var col = this.moduleCount - 1; col > 0; col -= 2) {
            if (col == 6) col--;
            while (true) {
              for (var c = 0; c < 2; c++) {
                var currentCol = col - c;
                if (this.modules[row][currentCol] == null) {
                  var dark = false;
                  if (bitIndex < data.length) { dark = ( ( (data[bitIndex] >>> (7 - byteIndex) ) & 1) == 1); }
                  var mask = QRUtil.getMask(maskPattern, row, currentCol);
                  if (mask) { dark = !dark; }
                  this.modules[row][currentCol] = dark;
                  byteIndex++;
                  if (byteIndex == 8) { byteIndex = 0; bitIndex++; }
                }
              }
              row += inc;
              if (row < 0 || this.moduleCount <= row) { row -= inc; inc = -inc; break; }
            }
          }
        }
      };
      QRCodeModel.createData = function(typeNumber, errorCorrectLevel, dataList) {
        var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, errorCorrectLevel);
        var buffer = new QRBitBuffer();
        for (var i = 0; i < dataList.length; i++) {
          var data = dataList[i];
          buffer.put(data.mode, 4);
          buffer.put(data.getLength(), QRUtil.getLengthInBits(data.mode, typeNumber) );
          data.write(buffer);
        }
        var totalDataCount = 0;
        for (var i = 0; i < rsBlocks.length; i++) { totalDataCount += rsBlocks[i].dataCount; }
        if (buffer.getLengthInBits() > totalDataCount * 8) {
          throw new Error("code length overflow. (" + buffer.getLengthInBits() + ">" + (totalDataCount * 8) + ")");
        }
        if (buffer.getLengthInBits() + 4 <= totalDataCount * 8) { buffer.put(0, 4); }
        while (buffer.getLengthInBits() % 8 != 0) { buffer.putBit(false); }
        while (true) {
          if (buffer.getLengthInBits() >= totalDataCount * 8) { break; }
          buffer.put(QRCodeModel.PAD0, 8);
          if (buffer.getLengthInBits() >= totalDataCount * 8) { break; }
          buffer.put(QRCodeModel.PAD1, 8);
        }
        return QRCodeModel.createBytes(buffer, rsBlocks);
      };
      QRCodeModel.createBytes = function(buffer, rsBlocks) {
        var offset = 0;
        var maxDcCount = 0;
        var maxEcCount = 0;
        var dcdata = new Array(rsBlocks.length);
        var ecdata = new Array(rsBlocks.length);
        for (var r = 0; r < rsBlocks.length; r++) {
          var dcCount = rsBlocks[r].dataCount;
          var ecCount = rsBlocks[r].totalCount - dcCount;
          maxDcCount = Math.max(maxDcCount, dcCount);
          maxEcCount = Math.max(maxEcCount, ecCount);
          dcdata[r] = new Array(dcCount);
          for (var i = 0; i < dcdata[r].length; i++) { dcdata[r][i] = 0xff & buffer.buffer[i + offset]; }
          offset += dcCount;
          var rsPoly = QRUtil.getErrorCorrectPolynomial(ecCount);
          var rawPoly = new QRPolynomial(dcdata[r], rsPoly.getLength() - 1);
          var modPoly = rawPoly.mod(rsPoly);
          ecdata[r] = new Array(rsPoly.getLength() - 1);
          for (var i = 0; i < ecdata[r].length; i++) {
            var modIndex = i + modPoly.getLength() - ecdata[r].length;
            ecdata[r][i] = (modIndex >= 0) ? modPoly.get(modIndex) : 0;
          }
        }
        var totalCodeCount = 0;
        for (var i = 0; i < rsBlocks.length; i++) { totalCodeCount += rsBlocks[i].totalCount; }
        var data = new Array(totalCodeCount);
        var idx = 0;
        for (var i = 0; i < maxDcCount; i++) { for (var r = 0; r < rsBlocks.length; r++) { if (i < dcdata[r].length) { data[idx++] = dcdata[r][i]; } } }
        for (var i = 0; i < maxEcCount; i++) { for (var r = 0; r < rsBlocks.length; r++) { if (i < ecdata[r].length) { data[idx++] = ecdata[r][i]; } } }
        return data;
      };
      QRCodeModel.PAD0 = 0xEC;
      QRCodeModel.PAD1 = 0x11;
      
      function QR8bitByte(data) { this.mode = QRMode.MODE_8BIT_BYTE; this.data = data; }
      QR8bitByte.prototype = {
        getLength: function() { return this.data.length; },
        write: function(buffer) { for (var i = 0; i < this.data.length; i++) { buffer.put(this.data.charCodeAt(i), 8); } }
      };

      // Expose to window namespace
      window.QRCodeLib = { QRCodeModel: QRCodeModel, QRErrorCorrectLevel: QRErrorCorrectLevel };
    })();

    // Function to draw QR code on the canvas element
    function drawQRCode(canvasId, text) {
      var canvas = document.getElementById(canvasId);
      if (!canvas) return;
      var ctx = canvas.getContext('2d');
      
      var qr = null;
      for (var type = 1; type <= 11; type++) {
        try {
          qr = new QRCodeLib.QRCodeModel(type, QRCodeLib.QRErrorCorrectLevel.M);
          qr.addData(text);
          qr.make();
          break;
        } catch (e) {
          if (type === 11) {
            console.error("QR Code generation overflow:", e);
            return;
          }
        }
      }
      
      var moduleCount = qr.getModuleCount();
      var size = 200;
      canvas.width = size;
      canvas.height = size;
      var cellSize = size / moduleCount;
      
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, size, size);
      
      ctx.fillStyle = '#000000';
      for (var row = 0; row < moduleCount; row++) {
        for (var col = 0; col < moduleCount; col++) {
          if (qr.isDark(row, col)) {
            ctx.fillRect(
              Math.floor(col * cellSize),
              Math.floor(row * cellSize),
              Math.ceil(cellSize),
              Math.ceil(cellSize)
            );
          }
        }
      }
    }
  </script>
</body>
</html>
""")


def render_page(
    payload: str = "{}",
    message: str = "Upload a file to see the extracted JSON.",
    preview: str = "",
    cpu_selected: str = "",
    gpu_selected: str = "",
    colab_url_value: str = "",
    timing_info: str = "",
    active_record: dict = None,
    host_name: str = "localhost:8001",
) -> bytes:
    colab_val = colab_url_value or get_colab_url()
    if not cpu_selected and not gpu_selected:
        if colab_val:
            gpu_selected = "selected"
            cpu_selected = ""
        else:
            cpu_selected = "selected"
            gpu_selected = ""
    # 1. Determine Badge markup
    badge_markup = ""
    if active_record:
        status = active_record.get("status", "EXTRACTED")
        badge_markup = f'<span class="badge badge-{status.lower()}">{status.replace("_", " ")}</span>'

    # 2. Determine Message markup
    message_markup = f"<p>{message}</p>" if message else ""

    # 3. Build Console UI markup
    console_markup = ""
    if active_record:
        rec_id = active_record["verification_id"]
        status = active_record["status"]
        payload_data = active_record["document_payload"]
        checks = active_record["checks"]
        rejection_reason = active_record.get("rejection_reason", "")

        is_immutable = status in {"APPROVED"}

        # Render layout based on status
        if status == "APPROVED":
            sig = active_record.get("signature", "")
            pub_key = active_record.get("public_key", "")
            sig_valid = False
            if sig and pub_key:
                sig_valid = verification_service.verify_document_signature(
                    payload_data, sig, pub_key
                )
            sig_result_text = (
                '<div style="color: #16a34a; font-weight: bold; font-size: 16px; margin-top: 8px;">✓ DOCUMENT SIGNATURE VALID</div>'
                if sig_valid
                else '<div style="color: #dc2626; font-weight: bold; font-size: 16px; margin-top: 8px;">✗ DOCUMENT SIGNATURE INVALID</div>'
            )

            prop = payload_data.get("property", {}) or {}
            parties = payload_data.get("parties", []) or []
            parties_html = ""
            for p in parties:
                if isinstance(p, dict):
                    parties_html += f"<li>{html.escape(p.get('name') or '')} ({html.escape(p.get('role') or '')})</li>"

            console_markup = f"""
            <div style="background: #fafaf9; border: 3px double var(--accent-2); border-radius: 20px; padding: 30px; position: relative; box-shadow: 0 10px 30px rgba(0,0,0,0.05);">
              <div class="cert-seal">Official Seal</div>
              <h2 style="color: var(--accent-2); border-bottom: 2px solid var(--accent-2); padding-bottom: 10px; margin-bottom: 20px;">✓ DOCUMENT APPROVED</h2>
              
              <div style="display: grid; gap: 12px; margin-bottom: 20px; font-size: 14px;">
                <div class="cert-field"><strong>Verification ID:</strong> <span style="font-family: monospace;">{rec_id}</span></div>
                <div class="cert-field"><strong>Approval Timestamp:</strong> {active_record.get('approved_at', '')}</div>
                <div class="cert-field"><strong>Document Type:</strong> {html.escape(payload_data.get('document_type') or '')}</div>
                <div class="cert-field"><strong>Document Number:</strong> {html.escape(payload_data.get('document_number') or '')}</div>
                <div class="cert-field"><strong>Property Survey:</strong> {html.escape(prop.get('survey_number') or '')}</div>
                <div class="cert-field"><strong>Property Area:</strong> {html.escape(str(prop.get('area') or ''))}</div>
                <div class="cert-field"><strong>Village:</strong> {html.escape(prop.get('village') or '')}</div>
                <div class="cert-field"><strong>District:</strong> {html.escape(prop.get('district') or '')}</div>
                <div class="cert-field"><strong>Parties Involved:</strong>
                  <ul style="margin: 4px 0 0 20px; padding: 0;">{parties_html}</ul>
                </div>
                <div class="cert-field"><strong>Document Date:</strong> {html.escape(payload_data.get('document_date') or '')}</div>
                <div class="cert-field"><strong>Execution Date:</strong> {html.escape(payload_data.get('execution_date') or '')}</div>
              </div>

              <h3 style="color: var(--accent); border-top: 1px solid var(--border); padding-top: 15px; margin-top: 20px; font-size: 16px;">CRYPTOGRAPHIC SECURITY</h3>
              <div style="margin-bottom: 10px; font-size: 14px;"><strong>Algorithm:</strong> RSA-PSS / SHA-256</div>
              <div style="margin-bottom: 10px; font-size: 14px;"><strong>Signature Status:</strong> {sig_result_text}</div>
              <div style="font-size: 14px; margin-bottom: 6px;"><strong>Digital Seal Signature:</strong></div>
              <div class="signature-box">{sig}</div>

              <div class="qr-container" style="flex-direction: column; align-items: center; text-align: center; padding: 20px;">
                <h3 style="margin-top: 0; color: var(--accent); font-size: 16px;">SCAN TO VERIFY</h3>
                <canvas id="qrCanvas" class="qr-code-canvas"></canvas>
                <div style="margin-top: 10px;">
                  <p style="margin: 0; font-size: 14px; color: var(--muted);">Scan this QR code from a device connected to the same local network.</p>
                  <p style="margin: 6px 0 0 0; font-size: 13px; font-family: monospace; font-weight: bold; background: #e2e8f0; padding: 6px 12px; border-radius: 6px; word-break: break-all;">http://{host_name}/?verification_id={rec_id}</p>
                  <p style="margin: 10px 0 0 0; font-size: 12px; color: var(--muted); font-style: italic;">
                    Verification requires this application to be running and the device must be able to reach this computer over the local network.
                  </p>
                </div>
              </div>

              <div style="margin-top: 25px; text-align: center; display: flex; gap: 12px; justify-content: center;">
                <a href="/?verification_id={rec_id}" class="btn btn-secondary">Verify Again</a>
                <a href="/" class="btn btn-primary">Process New Document</a>
              </div>

              <script>
                setTimeout(function() {{
                  drawQRCode('qrCanvas', 'http://{host_name}/?verification_id={rec_id}');
                }}, 100);
              </script>
            </div>
            """

        elif status == "REJECTED":
            console_markup = f"""
            <div style="background: #fff5f5; border: 2px solid var(--status-rejected); border-radius: 20px; padding: 30px; box-shadow: 0 10px 30px rgba(0,0,0,0.05);">
              <h2 style="color: var(--status-rejected); border-bottom: 2px solid var(--status-rejected); padding-bottom: 10px; margin-bottom: 20px;">✗ DOCUMENT REJECTED</h2>
              
              <div style="background: #fee2e2; border: 1.5px solid #fca5a5; padding: 16px; border-radius: 14px; margin-bottom: 20px; color: #991b1b;">
                <strong>Rejection Reason:</strong>
                <p style="margin: 6px 0 0 0; font-size: 14px;">{html.escape(rejection_reason or 'No reason provided.')}</p>
              </div>

              <div style="display: grid; gap: 12px; margin-bottom: 20px; font-size: 14px;">
                <div class="cert-field"><strong>Verification ID:</strong> <span style="font-family: monospace;">{rec_id}</span></div>
                <div class="cert-field"><strong>Rejected At:</strong> {active_record.get('rejected_at', '')}</div>
              </div>
              
              <div style="padding: 12px; background: #f3f4f6; border-radius: 10px; font-size: 14px; color: var(--muted); margin-bottom: 20px; text-align: center;">
                This document was not cryptographically certified.
              </div>

              <div style="margin-top: 25px; text-align: center; display: flex; gap: 12px; justify-content: center;">
                <a href="/" class="btn btn-primary">Process New Document</a>
              </div>
            </div>
            """

        else:
            # Build automated checklist output
            check_items_html = ""
            for c in checks:
                chk_status = c.get("status", "PASS")
                icon = "✓" if chk_status == "PASS" else "⚠" if chk_status == "WARNING" else "✗"
                check_items_html += f"""
                <div class="check-item">
                  <span class="check-status-icon status-{chk_status}">{icon}</span>
                  <div>
                    <p class="check-title">{html.escape(c.get('name', ''))} ({chk_status})</p>
                    <p class="check-desc">{html.escape(c.get('message', ''))}</p>
                  </div>
                </div>
                """

            # Form fields setup
            prop = payload_data.get("property", {}) or {}
            stamp = payload_data.get("stamp_information", {}) or {}
            parties = payload_data.get("parties", []) or []

            parties_json_str = json.dumps(parties)
            disabled_attr = "disabled" if is_immutable else ""

            # Rerun verification checks to detect critical failures
            has_critical_fail = any(
                c.get("status") == "FAIL" and c.get("severity") == "critical"
                for c in checks
            )

            if has_critical_fail:
                approval_warning = f"""
                <div style="background: #fef2f2; border: 1.5px solid #fca5a5; padding: 12px; border-radius: 10px; color: #991b1b; font-size: 13px; font-weight: bold; margin-top: 15px; margin-bottom: 15px; width: 100%;">
                  ⚠ Officer Approve is disabled because critical validation checks failed. Please correct details and Save Corrections.
                </div>
                """
                approve_button_disabled = "disabled"
            else:
                approval_warning = f"""
                <div style="background: #f0fdf4; border: 1.5px solid #bbf7d0; padding: 12px; border-radius: 10px; color: #166534; font-size: 13px; margin-top: 15px; margin-bottom: 15px; width: 100%;">
                  Officer approval will permanently certify the reviewed document facts.
                </div>
                """
                approve_button_disabled = ""

            # Action panel controls
            action_buttons = ""
            if not is_immutable:
                action_buttons = f"""
                <div style="width: 100%; display: flex; flex-direction: column; gap: 8px;">
                  <button type="submit" name="action" value="correct" class="btn btn-secondary" style="align-self: flex-start;">Save Corrections</button>
                  {approval_warning}
                  <div style="display: flex; flex-wrap: wrap; gap: 12px; align-items: center; width: 100%;">
                    <button type="submit" name="action" value="approve" class="btn btn-success" {approve_button_disabled}>Officer Approve</button>
                    <div style="flex-grow: 1;"></div>
                    <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                      <input type="text" name="rejection_reason" id="rejection_reason" placeholder="Rejection Reason" style="padding: 10px; border-radius: 10px; border: 1px solid var(--border);">
                      <button type="submit" name="action" value="reject" class="btn btn-danger" onclick="if(!document.getElementById('rejection_reason').value.trim()) {{ alert('Please provide a rejection reason.'); return false; }}">Officer Reject</button>
                    </div>
                  </div>
                </div>
                """

            blocked_banner_markup = ""
            if message and "Approval refused" in message:
                blocked_banner_markup = f"""
                <div style="background: #fee2e2; border: 1.5px solid #fca5a5; padding: 16px; border-radius: 14px; margin-bottom: 20px; color: #991b1b; font-weight: bold; font-size: 15px;">
                  Approval blocked — please resolve the following checks:
                </div>
                """

            console_markup = f"""
            <div style="background: rgba(31, 41, 55, 0.02); padding: 20px; border-radius: 18px; border: 1.5px solid var(--border); margin-bottom: 24px;">
              <div style="font-size: 14px; margin-bottom: 16px; color: var(--muted);">
                <strong>Verification ID:</strong> <span style="font-family: monospace;">{rec_id}</span>
              </div>
              
              {blocked_banner_markup}

              <!-- STEP 1: AUTOMATED VERIFICATION -->
              <h3 style="margin-top: 0; margin-bottom: 12px; color: var(--accent);">Stage 1 — Automated Verification</h3>
              <div class="checklist">
                {check_items_html}
              </div>

              <!-- STEP 2: CLERK REVIEW & CORRECTION -->
              <h3 style="margin-bottom: 4px; color: var(--accent);">Stage 2 — Clerk Review (Correct Fields)</h3>
              <p style="margin: 0 0 16px 0; font-size: 13px; color: var(--muted); font-style: italic;">
                Review the extracted information and correct any OCR errors before approval.
              </p>
              <form action="/extract" method="post" enctype="multipart/form-data">
                <input type="hidden" name="verification_id" value="{rec_id}">
                
                <div class="editor-group">
                  <div class="editor-field">
                    <label>Document Type:</label>
                    <input type="text" name="document_type" value="{html.escape(str(payload_data.get('document_type') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Document Number:</label>
                    <input type="text" name="document_number" value="{html.escape(str(payload_data.get('document_number') or ''))}" {disabled_attr}>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Survey Number:</label>
                    <input type="text" name="survey_number" value="{html.escape(str(prop.get('survey_number') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Sub-Survey Number:</label>
                    <input type="text" name="sub_survey_number" value="{html.escape(str(prop.get('sub_survey_number') or ''))}" {disabled_attr}>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Property Area:</label>
                    <input type="text" name="area" value="{html.escape(str(prop.get('area') if prop.get('area') is not None else ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Village:</label>
                    <input type="text" name="village" value="{html.escape(str(prop.get('village') or ''))}" {disabled_attr}>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Mandal:</label>
                    <input type="text" name="mandal" value="{html.escape(str(prop.get('mandal') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>District:</label>
                    <input type="text" name="district" value="{html.escape(str(prop.get('district') or ''))}" {disabled_attr}>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Stamp Serial Number:</label>
                    <input type="text" name="stamp_number" value="{html.escape(str(stamp.get('stamp_number') or payload_data.get('stamp_number') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Stamp Value:</label>
                    <input type="text" name="stamp_value" value="{html.escape(str(stamp.get('stamp_value') if stamp.get('stamp_value') is not None else (payload_data.get('stamp_value') if payload_data.get('stamp_value') is not None else '')))}" {disabled_attr}>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Stamp Sold To:</label>
                    <input type="text" name="sold_to" value="{html.escape(str(stamp.get('sold_to') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Parties List (JSON):</label>
                    <textarea name="parties_json" rows="3" style="font-family: monospace; font-size: 12px;" {disabled_attr}>{html.escape(parties_json_str)}</textarea>
                  </div>
                </div>

                <div class="editor-group">
                  <div class="editor-field">
                    <label>Document Date:</label>
                    <input type="text" name="document_date" value="{html.escape(str(payload_data.get('document_date') or ''))}" {disabled_attr}>
                  </div>
                  <div class="editor-field">
                    <label>Execution Date:</label>
                    <input type="text" name="execution_date" value="{html.escape(str(payload_data.get('execution_date') or ''))}" {disabled_attr}>
                  </div>
                </div>

                <div class="action-panel">
                  {action_buttons}
                </div>
              </form>
            </div>
            """

    raw_payload_section = f"""
    <div class="card" style="margin-top: 24px; padding: 20px;">
      <h3 style="margin-top: 0; color: var(--muted); font-size: 16px;">Raw Document JSON</h3>
      <pre style="max-height: 250px;">{html.escape(payload)}</pre>
    </div>
    """

    steps_markup = ""
    if active_record:
        status = active_record.get("status", "EXTRACTED")
        s1 = "completed"
        s2 = "pending"
        s3 = "pending"
        s4 = "pending"
        s5 = "pending"
        
        if status == "EXTRACTED":
            s1 = "active"
        elif status == "NEEDS_REVIEW":
            s2 = "active"
        elif status in {"READY_FOR_APPROVAL", "READY"}:
            s2 = "completed"
            s3 = "completed"
            s4 = "active"
        elif status == "APPROVED":
            s2 = "completed"
            s3 = "completed"
            s4 = "completed"
            s5 = "completed"
        elif status == "REJECTED":
            s2 = "completed"
            s3 = "completed"
            s4 = "rejected"
            s5 = "rejected"

        s5_label = "Certified"
        if status == "REJECTED":
            s5_label = "Rejected"
            
        steps_markup = f"""
        <div class="steps-container">
          <div class="step-item {s1}">
            <div class="step-dot">1</div>
            <div class="step-label">Extracted</div>
          </div>
          <div class="step-item {s2}">
            <div class="step-dot">2</div>
            <div class="step-label">Clerk Review</div>
          </div>
          <div class="step-item {s3}">
            <div class="step-dot">3</div>
            <div class="step-label">Automated Verify</div>
          </div>
          <div class="step-item {s4}">
            <div class="step-dot">4</div>
            <div class="step-label">Officer Decision</div>
          </div>
          <div class="step-item {s5}">
            <div class="step-dot">5</div>
            <div class="step-label">{s5_label}</div>
          </div>
        </div>
        """

    return HTML_PAGE.substitute(
        payload=payload,
        message=message,
        preview=preview or "<p>No image uploaded yet.</p>",
        cpu_selected=cpu_selected,
        gpu_selected=gpu_selected,
        colab_url_value=colab_url_value or os.environ.get("COLAB_OCR_URL", ""),
        timing_info=timing_info,
        badge_markup=badge_markup,
        message_markup=message_markup,
        console_markup=console_markup,
        steps_markup=steps_markup,
        raw_json_markup=raw_payload_section,
    ).encode("utf-8")


def render_verification_view(record: dict, sig_valid: bool) -> bytes:
    """Renders a simple standalone landing page for public verification checks."""
    rec_id = record["verification_id"]
    status = record["status"]
    payload_data = record["document_payload"]
    prop = payload_data.get("property", {}) or {}
    parties = payload_data.get("parties", []) or []

    sig_html = (
        '<div style="color: #16a34a; font-weight: bold; font-size: 20px; border: 2px solid #16a34a; padding: 12px; border-radius: 12px; background: #f0fdf4; margin-bottom: 20px; text-align: center;">✓ DOCUMENT SIGNATURE VALID</div>'
        if sig_valid
        else '<div style="color: #dc2626; font-weight: bold; font-size: 20px; border: 2px solid #dc2626; padding: 12px; border-radius: 12px; background: #fef2f2; margin-bottom: 20px; text-align: center;">✗ DOCUMENT SIGNATURE INVALID</div>'
    )

    parties_html = ""
    for p in parties:
        if isinstance(p, dict):
            parties_html += f"<li>{html.escape(p.get('name') or '')} ({html.escape(p.get('role') or '')})</li>"

    page_html = f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Registry Land Record Verification</title>
      <style>
        body {{
          background: #f4efe6;
          color: #1f2937;
          font-family: system-ui, sans-serif;
          padding: 40px 20px;
        }}
        .card {{
          max-width: 680px;
          margin: 0 auto;
          background: #fffaf2;
          border: 1px solid #dccfb8;
          border-radius: 20px;
          padding: 30px;
          box-shadow: 0 10px 30px rgba(0,0,0,0.05);
        }}
        h1 {{ color: #7c2d12; border-bottom: 2px solid #dccfb8; padding-bottom: 10px; margin-top: 0; text-align: center; }}
        h2 {{ color: #14532d; font-size: 18px; border-bottom: 1px solid #cbd5e1; padding-bottom: 6px; margin-top: 24px; }}
        .field {{ margin-bottom: 12px; font-size: 14px; line-height: 1.6; }}
        .field strong {{ display: inline-block; width: 180px; color: #4b5563; }}
        .sig {{ font-family: monospace; font-size: 11px; word-break: break-all; background: #e2e8f0; padding: 10px; border-radius: 8px; max-height: 80px; overflow-y: auto; }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 999px; font-weight: bold; text-transform: uppercase; font-size: 12px; }}
        .badge-approved {{ background: #d1fae5; color: #065f46; border: 1.5px solid #a7f3d0; }}
        .badge-rejected {{ background: #fee2e2; color: #991b1b; border: 1.5px solid #fca5a5; }}
        .btn {{ display: inline-block; padding: 10px 20px; background: #7c2d12; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; margin-top: 20px; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>DOCUMENT VERIFICATION</h1>
        
        <div class="field" style="margin-bottom: 20px; font-size: 15px; color: #4b5563;">
          <strong>Verification ID:</strong> <span style="font-family: monospace; font-weight: bold; color: #1f2937;">{rec_id}</span>
        </div>

        <div style="margin-bottom: 24px;">
          <h2 style="color: #7c2d12; margin-top: 0;">Signature Status</h2>
          {sig_html}
        </div>

        <div style="border-top: 2px solid #dccfb8; padding-top: 10px;">
          <h2>DOCUMENT FACTS</h2>
          <div class="field"><strong>Status:</strong> <span class="badge badge-{status.lower()}">{status}</span></div>
          <div class="field"><strong>Approved Timestamp:</strong> {record.get('approved_at', 'N/A')}</div>
          <div class="field"><strong>Document Type:</strong> {html.escape(payload_data.get('document_type') or '')}</div>
          <div class="field"><strong>Document Number:</strong> {html.escape(payload_data.get('document_number') or '')}</div>
          <div class="field"><strong>Property Survey:</strong> {html.escape(prop.get('survey_number') or '')}</div>
          <div class="field"><strong>Area:</strong> {html.escape(str(prop.get('area') or ''))}</div>
          <div class="field"><strong>Village:</strong> {html.escape(prop.get('village') or '')}</div>
          <div class="field"><strong>District:</strong> {html.escape(prop.get('district') or '')}</div>
          <div class="field"><strong>Parties Involved:</strong>
            <ul style="margin: 4px 0 0 20px; padding: 0;">{parties_html}</ul>
          </div>
          <div class="field"><strong>Document Date:</strong> {html.escape(payload_data.get('document_date') or '')}</div>
          <div class="field"><strong>Execution Date:</strong> {html.escape(payload_data.get('execution_date') or '')}</div>
        </div>

        <div style="margin-top: 24px; border-top: 1px dashed #cbd5e1; padding-top: 15px;">
          <div class="field"><strong>Digital Signature Seal:</strong></div>
          <div class="sig">{record.get('signature', 'None')}</div>
        </div>

        <div style="margin-top: 20px; text-align: center;">
          <a href="/" class="btn">← Go to Main Registry Office</a>
        </div>
      </div>
    </body>
    </html>
    """
    return page_html.encode("utf-8")


class LandExtractorHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        # Standard check
        if parsed.path not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        # Verification view routing
        verification_id = query_params.get("verification_id", [None])[0]
        if verification_id:
            record = verification_service.get_record(verification_id)
            if record:
                # Dynamic Signature Validation check
                pub_key = record.get(
                    "public_key"
                ) or verification_service.get_public_verification_key()
                signature = record.get("signature")
                payload = record.get("document_payload")
                status = record.get("status")

                sig_valid = False
                if signature and pub_key and payload:
                    sig_valid = verification_service.verify_document_signature(
                        payload, signature, pub_key
                    )

                page = render_verification_view(record, sig_valid)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Verification record not found")
                return

        host_name = self.headers.get("Host", f"localhost:{self.server.server_address[1]}")
        page = render_page(colab_url_value=get_colab_url(), host_name=host_name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if self.path != "/extract":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected multipart upload")
            return

        boundary_token = "boundary="
        if boundary_token not in content_type:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing upload boundary")
            return

        boundary = content_type.split(boundary_token, 1)[1].encode("utf-8")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        parts = body.split(b"--" + boundary)
        uploaded = None
        filename = "uploaded_image"
        colab_url = get_colab_url()
        processing_mode = "gpu" if colab_url else "cpu"

        # Action fields parsing
        action = None
        verification_id = None
        rejection_reason = ""
        form_fields = {}

        for part in parts:
            if b"Content-Disposition" not in part:
                continue

            header_blob, _, val_blob = part.partition(b"\r\n\r\n")
            val = val_blob.rsplit(b"\r\n", 1)[0]
            header_str = header_blob.decode("utf-8", errors="ignore")

            if 'name="document_image"' in header_str:
                if not val:
                    continue
                match = re.search(r'filename="([^"]+)"', header_str)
                if match:
                    filename = Path(match.group(1)).name
                uploaded = val
            elif 'name="processing_mode"' in header_str:
                mode_str = val.decode("utf-8", errors="ignore").strip()
                if mode_str:
                    processing_mode = mode_str
            elif 'name="colab_url"' in header_str:
                url_str = val.decode("utf-8", errors="ignore").strip()
                if url_str:
                    colab_url = url_str.rstrip("/")
                    try:
                        (Path(__file__).parent / "colab_url.txt").write_text(colab_url, encoding="utf-8")
                    except Exception:
                        pass
            elif 'name="action"' in header_str:
                action = val.decode("utf-8", errors="ignore").strip()
            elif 'name="verification_id"' in header_str:
                verification_id = val.decode("utf-8", errors="ignore").strip()
            elif 'name="rejection_reason"' in header_str:
                rejection_reason = val.decode("utf-8", errors="ignore").strip()
            else:
                match = re.search(r'name="([^"]+)"', header_str)
                if match:
                    field_name = match.group(1)
                    form_fields[field_name] = val.decode(
                        "utf-8", errors="ignore"
                    ).strip()

        # Handle postback actions (Approve / Reject / Save Corrections)
        if action and verification_id:
            record = verification_service.get_record(verification_id)
            if not record:
                self.send_error(HTTPStatus.NOT_FOUND, "Verification record not found")
                return

            if record.get("status") == "APPROVED":
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Immutable approved records cannot be modified.",
                )
                return

            # Apply clerk corrections
            payload = record.get("document_payload", {})

            if "document_type" in form_fields:
                payload["document_type"] = form_fields["document_type"]
            if "document_number" in form_fields:
                payload["document_number"] = form_fields["document_number"]

            payload.setdefault("property", {})
            if "area" in form_fields:
                payload["property"]["area"] = form_fields["area"]
            if "survey_number" in form_fields:
                payload["property"]["survey_number"] = form_fields["survey_number"]
            if "sub_survey_number" in form_fields:
                payload["property"]["sub_survey_number"] = form_fields["sub_survey_number"]
            if "village" in form_fields:
                payload["property"]["village"] = form_fields["village"]
            if "mandal" in form_fields:
                payload["property"]["mandal"] = form_fields["mandal"]
            if "district" in form_fields:
                payload["property"]["district"] = form_fields["district"]

            payload.setdefault("stamp_information", {})
            if "stamp_number" in form_fields:
                payload["stamp_information"]["stamp_number"] = form_fields["stamp_number"]
                payload["stamp_number"] = form_fields["stamp_number"]
            if "stamp_value" in form_fields:
                payload["stamp_information"]["stamp_value"] = form_fields["stamp_value"]
                payload["stamp_value"] = form_fields["stamp_value"]
            if "sold_to" in form_fields:
                payload["stamp_information"]["sold_to"] = form_fields["sold_to"]

            if "document_date" in form_fields:
                payload["document_date"] = form_fields["document_date"]
            if "execution_date" in form_fields:
                payload["execution_date"] = form_fields["execution_date"]

            if "parties_json" in form_fields:
                try:
                    payload["parties"] = json.loads(form_fields["parties_json"])
                except Exception:
                    pass

            # Recompute automated validation checks
            checks = verification_service.run_verification_checks(payload)
            status = verification_service.calculate_overall_status(checks)

            record["document_payload"] = payload
            record["checks"] = checks
            record["status"] = status

            message = ""
            if action == "approve":
                has_critical_fail = any(
                    c.get("status") == "FAIL" and c.get("severity") == "critical"
                    for c in checks
                )
                if has_critical_fail:
                    message = "Approval refused: critical automated checks failed."
                    verification_service.save_record(record)
                else:
                    sig = verification_service.sign_document(payload)
                    pub_key = verification_service.get_public_verification_key()

                    record["status"] = "APPROVED"
                    record["signature"] = sig
                    record["public_key"] = pub_key
                    record["approved_at"] = datetime.utcnow().strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    verification_service.save_record(record)
                    message = "Document approved and signed successfully."
            elif action == "reject":
                if not rejection_reason.strip():
                    message = "Rejection failed: a rejection reason is required."
                    record["status"] = verification_service.calculate_overall_status(checks)
                    verification_service.save_record(record)
                else:
                    record["status"] = "REJECTED"
                    record["rejection_reason"] = rejection_reason
                    record["rejected_at"] = datetime.utcnow().strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    verification_service.save_record(record)
                    message = "Document was rejected by the officer."
            elif action == "correct":
                verification_service.save_record(record)
                message = "Clerk review corrections saved successfully."

            payload_str = json.dumps(record, indent=2, ensure_ascii=False)
            host_name = self.headers.get("Host", f"localhost:{self.server.server_address[1]}")
            page = render_page(
                payload=payload_str,
                message=message,
                preview=f"<p><strong>Active Verification Record:</strong> {verification_id}</p>",
                cpu_selected="",
                gpu_selected="",
                colab_url_value=colab_url,
                timing_info="",
                active_record=record,
                host_name=host_name,
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if not uploaded:
            self.send_error(HTTPStatus.BAD_REQUEST, "No file was uploaded")
            return

        temp_path = None
        cpu_sel = "selected" if processing_mode == "cpu" else ""
        gpu_sel = "selected" if processing_mode == "gpu" else ""

        try:
            temp_path = process_uploaded_file(uploaded, filename)

            timing_info = ""

            if processing_mode == "gpu":
                t_total_start = perf_counter()
                try:
                    status_url = f"{colab_url.rstrip('/')}/status"
                    status_resp = requests.get(status_url, timeout=5)
                    if status_resp.status_code != 200:
                        raise ValueError(
                            f"Cloud GPU status check returned status code {status_resp.status_code}"
                        )
                    gpu_name = status_resp.json().get("gpu_name", "NVIDIA GPU")
                except Exception as e:
                    raise ConnectionError(
                        f"Cloud GPU is unavailable at this URL. Details: {e}"
                    )

                ocr_url = f"{colab_url.rstrip('/')}/ocr"
                is_pdf_upload = filename.lower().endswith(".pdf") or uploaded.startswith(b"%PDF")

                if is_pdf_upload:
                    page_img_paths = extract_pdf_pages_to_images(uploaded)
                    all_lines = []
                    all_raw_texts = []
                    total_ocr_time_ms = 0.0
                    t_net_start = perf_counter()

                    for page_idx, p_path in enumerate(page_img_paths, start=1):
                        with open(p_path, "rb") as f:
                            ocr_resp = requests.post(ocr_url, files={"image": f}, timeout=45)

                        if ocr_resp.status_code != 200:
                            try:
                                err_msg = ocr_resp.json().get("error", ocr_resp.text)
                            except Exception:
                                err_msg = ocr_resp.text
                            raise ValueError(
                                f"Cloud GPU OCR failed on Page {page_idx} with status {ocr_resp.status_code}: {err_msg}"
                            )

                        gpu_result = ocr_resp.json()
                        total_ocr_time_ms += gpu_result.get("ocr_time_ms", 0.0)
                        gpu_name = gpu_result.get("gpu_name", gpu_name)

                        p_words = []
                        for text, score, poly in zip(
                            gpu_result["rec_texts"],
                            gpu_result["rec_scores"],
                            gpu_result["rec_polys"],
                        ):
                            cleaned = normalize_space(str(text))
                            if not cleaned:
                                continue
                            p_words.append(
                                OCRWord(
                                    text=cleaned,
                                    score=float(score),
                                    points=[[int(pt[0]), int(pt[1])] for pt in poly],
                                )
                            )

                        p_lines = group_words_into_lines(p_words)
                        # Read the page image to get dimensions for y_rel calculation
                        from PIL import Image as _PILImage
                        _pimg = _PILImage.open(p_path)
                        _pw, _ph = _pimg.size
                        for l in p_lines:
                            l.page_num = page_idx
                            l.page_height = _ph
                            l.page_width = _pw
                        all_lines.extend(p_lines)
                        p_raw = "\n".join(l.text for l in p_lines)
                        all_raw_texts.append(f"--- PAGE {page_idx} ---\n{p_raw}")

                    t_net_end = perf_counter()
                    total_request_time = (t_net_end - t_net_start) * 1000
                    ocr_time_ms = total_ocr_time_ms
                    network_time_ms = max(0.0, total_request_time - ocr_time_ms)
                    lines = all_lines
                    raw_text = "\n\n".join(all_raw_texts)
                else:
                    t_net_start = perf_counter()
                    with open(temp_path, "rb") as f:
                        ocr_resp = requests.post(ocr_url, files={"image": f}, timeout=45)
                    t_net_end = perf_counter()
                    total_request_time = (t_net_end - t_net_start) * 1000

                    if ocr_resp.status_code != 200:
                        try:
                            err_msg = ocr_resp.json().get("error", ocr_resp.text)
                        except Exception:
                            err_msg = ocr_resp.text
                        raise ValueError(
                            f"Cloud GPU OCR failed with status {ocr_resp.status_code}: {err_msg}"
                        )

                    gpu_result = ocr_resp.json()
                    ocr_time_ms = gpu_result.get("ocr_time_ms", 0.0)
                    gpu_name = gpu_result.get("gpu_name", gpu_name)
                    network_time_ms = max(0.0, total_request_time - ocr_time_ms)

                    words = []
                    for text, score, poly in zip(
                        gpu_result["rec_texts"],
                        gpu_result["rec_scores"],
                        gpu_result["rec_polys"],
                    ):
                        cleaned = normalize_space(str(text))
                        if not cleaned:
                            continue
                        words.append(
                            OCRWord(
                                text=cleaned,
                                score=float(score),
                                points=[[int(pt[0]), int(pt[1])] for pt in poly],
                            )
                        )

                    lines = group_words_into_lines(words)
                    # Estimate page height from max y coordinate of detected words
                    _est_h = max((w.y_max for w in words), default=2000) + 100 if words else 2000
                    _est_w = max((w.x_max for w in words), default=1500) + 100 if words else 1500
                    for l in lines:
                        l.page_num = 1
                        l.page_height = _est_h
                        l.page_width = _est_w
                    raw_text = "\n".join(l.text for l in lines)

                gpu_ocr_timings = {
                    "model_initialization_ms": 0.0,
                    "model_access_ms": 0.0,
                    "image_reading_ms": 0.0,
                    "ocr_inference_ms": ocr_time_ms,
                    "ocr_word_parsing_ms": 0.0,
                    "line_grouping_ms": 0.0,
                    "ocr_text_join_ms": 0.0,
                    "ocr_total_ms": ocr_time_ms,
                }

                result = extract_land_document_from_lines(
                    lines, raw_text, temp_path, timings=gpu_ocr_timings
                )

                total_time_ms = (perf_counter() - t_total_start) * 1000
                result.setdefault("profiling_ms", {})
                result["profiling_ms"]["pipeline_total_ms"] = round(
                    total_time_ms, 3
                )

                timing_info = f"""
                <div style="background: rgba(31, 41, 55, 0.05); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid var(--border); font-size: 14px; display: grid; gap: 8px;">
                  <div><strong>Processing Mode:</strong> Kaggle / Colab GPU</div>
                  <div><strong>GPU Status:</strong> <span style="color: #14532d; font-weight: bold;">✓ Connected</span></div>
                  <div><strong>GPU Hardware:</strong> {gpu_name}</div>
                  <div><strong>OCR Inference Time:</strong> {ocr_time_ms:.2f} ms</div>
                  <div><strong>Network Transit Time:</strong> {network_time_ms:.2f} ms</div>
                  <div><strong>Total Processing Time:</strong> {total_time_ms:.2f} ms</div>
                </div>
                """
            else:
                t_total_start = perf_counter()
                result = extract_land_document(temp_path)
                total_time_ms = (perf_counter() - t_total_start) * 1000

                ocr_time_ms = result.get("profiling_ms", {}).get(
                    "ocr_total_ms", 0.0
                )

                timing_info = f"""
                <div style="background: rgba(31, 41, 55, 0.05); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid var(--border); font-size: 14px; display: grid; gap: 8px;">
                  <div><strong>Processing Mode:</strong> Local CPU</div>
                  <div><strong>OCR Inference Time:</strong> {ocr_time_ms:.2f} ms</div>
                  <div><strong>Total Processing Time:</strong> {total_time_ms:.2f} ms</div>
                </div>
                """

            # Create local verification record instantly
            record = verification_service.create_verification_record(result)
            verification_service.save_record(record)

            from semantic_extractor import clean_user_facing_schema
            user_facing_result = clean_user_facing_schema(result)
            payload = json.dumps(user_facing_result, indent=2, ensure_ascii=False)
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            image_data = base64.b64encode(uploaded).decode("ascii")
            preview = f'<img src="data:{mime_type};base64,{image_data}" alt="Uploaded image preview">'
            message = f"Processed {html.escape(filename)} successfully. Verification record created."

            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

            host_name = self.headers.get("Host", f"localhost:{self.server.server_address[1]}")
            page = render_page(
                payload=payload,
                message=message,
                preview=f"<p><strong>{html.escape(filename)}</strong></p>{preview}",
                cpu_selected=cpu_sel,
                gpu_selected=gpu_sel,
                colab_url_value=colab_url,
                timing_info=timing_info,
                active_record=record,
                host_name=host_name,
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        except Exception as exc:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            error_payload = json.dumps({"error": str(exc)}, indent=2)

            timing_info = (
                f"""
            <div style="background: rgba(220, 38, 38, 0.08); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid #fecaca; font-size: 14px; display: grid; gap: 8px; color: #991b1b;">
              <div><strong>Processing Mode:</strong> Kaggle / Colab GPU</div>
              <div><strong>GPU Status:</strong> <span style="font-weight: bold;">✗ Unavailable</span></div>
              <div><strong>Error Details:</strong> {html.escape(str(exc))}</div>
            </div>
            """
                if processing_mode == "gpu"
                else ""
            )

            host_name = self.headers.get("Host", f"localhost:{self.server.server_address[1]}")
            page = render_page(
                payload=error_payload,
                message="Extraction failed.",
                cpu_selected=cpu_sel,
                gpu_selected=gpu_sel,
                colab_url_value=colab_url,
                timing_info=timing_info,
                host_name=host_name,
            )
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    def log_message(self, format, *args):
        return


def main() -> int:
    print("Pre-loading PaddleOCR models (this may take a moment)...")
    try:
        get_paddle_ocr_model()
        print("PaddleOCR models pre-loaded successfully!")
    except Exception as exc:
        print(
            f"Warning: local PaddleOCR preload failed, starting server anyway: {exc}"
        )
    port = int(os.environ.get("PORT", 8001))
    server = ThreadingHTTPServer(("0.0.0.0", port), LandExtractorHandler)
    lan_ip = get_lan_ip()
    print(f"Land extractor web app running locally at http://localhost:{port}")
    print(f"Accessible on your local network/LAN at http://{lan_ip}:{port}")
    while True:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Server exception recovered: {exc}")
            import time, traceback
            traceback.print_exc()
            time.sleep(1)
    try:
        server.server_close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
