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

import requests
from land_document_extractor import (
    extract_land_document,
    get_paddle_ocr_model,
    extract_land_document_from_lines,
    group_words_into_lines,
    OCRWord,
    OCRLine,
    normalize_space,
)


HTML_PAGE = Template("""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Land Document Extractor</title>
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
      max-width: 1180px;
      margin: 0 auto;
      padding: 40px 20px 56px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 24px;
      align-items: stretch;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }
    .intro { padding: 28px; }
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
    p { line-height: 1.6; color: var(--muted); }
    .upload {
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      justify-content: center;
    }
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
    button {
      border: 0;
      border-radius: 14px;
      padding: 14px 18px;
      background: linear-gradient(135deg, var(--accent), #9a3412);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    button:hover { filter: brightness(1.05); }
    .status {
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(20, 83, 45, 0.08);
      color: var(--accent-2);
      font-size: 14px;
    }
    .grid {
      margin-top: 24px;
      display: grid;
      grid-template-columns: 0.85fr 1.15fr;
      gap: 24px;
      align-items: start;
    }
    .preview, .output { padding: 22px; }
    .preview img {
      width: 100%;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: white;
    }
    pre {
      margin: 0;
      padding: 18px;
      overflow: auto;
      max-height: 72vh;
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 18px;
      font-size: 13px;
      line-height: 1.55;
    }
    .meta {
      display: grid;
      gap: 10px;
      margin-top: 14px;
      font-size: 14px;
      color: var(--muted);
    }
    .meta strong { color: var(--ink); }
    @media (max-width: 920px) {
      .hero, .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <section class="card intro">
        <span class="kicker">Land Document OCR</span>
        <h1>Upload a photo and get structured document data back.</h1>
        <p>
          This tool is tuned for Indian land and stamp-paper documents. Upload a scanned photo,
          and it will try to extract document details, parties, stamp information, and page-continuation flags.
        </p>
        <div class="meta">
          <div><strong>Best for:</strong> sale deeds, GPA documents, stamp papers, and similar scans.</div>
          <div><strong>Output:</strong> JSON you can plug into your site or API.</div>
        </div>
      </section>
      <section class="card upload">
        <form action="/extract" method="post" enctype="multipart/form-data">
          <div>
            <label for="document_image" style="font-weight: 700; display: block; margin-bottom: 6px;">Select Document Image:</label>
            <input type="file" name="document_image" id="document_image" accept="image/*" required>
          </div>
          
          <div style="display: grid; gap: 6px;">
            <label for="processing_mode" style="font-weight: 700;">Processing Mode:</label>
            <select name="processing_mode" id="processing_mode" style="padding: 10px; border-radius: 10px; border: 1px solid var(--border); background: white;">
              <option value="cpu" $cpu_selected>Local CPU</option>
              <option value="gpu" $gpu_selected>Google Colab GPU</option>
            </select>
          </div>
          
          <div id="colab_url_container" style="display: none; gap: 6px;">
            <label for="colab_url" style="font-weight: 700; display: block;">Colab OCR URL:</label>
            <input type="url" name="colab_url" id="colab_url" placeholder="https://xxxx.localtunnel.me" value="$colab_url_value" style="padding: 10px; border-radius: 10px; border: 1px solid var(--border); background: white; width: 100%;">
          </div>
          
          <button type="submit">Upload and Extract</button>
        </form>
        
        <script>
          const modeSelect = document.getElementById('processing_mode');
          const colabContainer = document.getElementById('colab_url_container');
          const colabInput = document.getElementById('colab_url');
          
          function toggleColabUrl() {
            if (modeSelect.value === 'gpu') {
              colabContainer.style.display = 'grid';
              colabInput.required = true;
            } else {
              colabContainer.style.display = 'none';
              colabInput.required = false;
            }
          }
          
          modeSelect.addEventListener('change', toggleColabUrl);
          toggleColabUrl();
        </script>
        
        <div class="status">Run this locally, then open <strong>http://127.0.0.1:8000</strong>.</div>
      </section>
    </div>
    <div class="grid">
      <section class="card preview">
        <h2>Preview</h2>
        <p>Uploaded image preview appears here after extraction.</p>
        $preview
      </section>
      <section class="card output">
        <h2>Extraction Result</h2>
        <p>$message</p>
        $timing_info
        <pre>$payload</pre>
      </section>
    </div>
  </div>
</body>
</html>
""")


def render_page(
    payload: str = "{}",
    message: str = "Upload a file to see the extracted JSON.",
    preview: str = "",
    cpu_selected: str = "selected",
    gpu_selected: str = "",
    colab_url_value: str = "",
    timing_info: str = "",
) -> bytes:
    return HTML_PAGE.substitute(
        payload=html.escape(payload),
        message=html.escape(message),
        preview=preview or "<p>No image uploaded yet.</p>",
        cpu_selected=cpu_selected,
        gpu_selected=gpu_selected,
        colab_url_value=colab_url_value or os.environ.get("COLAB_OCR_URL", ""),
        timing_info=timing_info,
    ).encode("utf-8")


class LandExtractorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        page = render_page(colab_url_value=os.environ.get("COLAB_OCR_URL", ""))
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
        processing_mode = "cpu"
        colab_url = ""

        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            
            if b"name=\"document_image\"" in part:
                header_blob, _, file_blob = part.partition(b"\r\n\r\n")
                if not file_blob:
                    continue
                disposition = header_blob.decode("utf-8", errors="ignore")
                match = re.search(r'filename="([^"]+)"', disposition)
                if match:
                    filename = Path(match.group(1)).name
                uploaded = file_blob.rsplit(b"\r\n", 1)[0]
                
            elif b"name=\"processing_mode\"" in part:
                _, _, val_blob = part.partition(b"\r\n\r\n")
                processing_mode = val_blob.rsplit(b"\r\n", 1)[0].decode("utf-8").strip()
                
            elif b"name=\"colab_url\"" in part:
                _, _, val_blob = part.partition(b"\r\n\r\n")
                colab_url = val_blob.rsplit(b"\r\n", 1)[0].decode("utf-8").strip()

        if not uploaded:
            self.send_error(HTTPStatus.BAD_REQUEST, "No file was uploaded")
            return

        suffix = Path(filename).suffix or ".png"
        temp_path = None
        cpu_sel = "selected" if processing_mode == "cpu" else ""
        gpu_sel = "selected" if processing_mode == "gpu" else ""

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded)
                temp_path = tmp.name

            timing_info = ""

            if processing_mode == "gpu":
                # Google Colab GPU OCR Path
                t_total_start = perf_counter()
                
                # Check status first
                try:
                    status_url = f"{colab_url.rstrip('/')}/status"
                    status_resp = requests.get(status_url, timeout=5)
                    if status_resp.status_code != 200:
                        raise ValueError(f"Colab status check returned status code {status_resp.status_code}")
                    gpu_name = status_resp.json().get("gpu_name", "NVIDIA GPU")
                except Exception as e:
                    raise ConnectionError(f"Google Colab GPU is unavailable at this URL. Details: {e}")
                
                # Post image to Colab
                ocr_url = f"{colab_url.rstrip('/')}/ocr"
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
                    raise ValueError(f"Colab OCR failed with status {ocr_resp.status_code}: {err_msg}")
                
                gpu_result = ocr_resp.json()
                ocr_time_ms = gpu_result.get("ocr_time_ms", 0.0)
                gpu_name = gpu_result.get("gpu_name", gpu_name)
                network_time_ms = max(0.0, total_request_time - ocr_time_ms)
                
                # Translate PaddleOCR 3.x results to local OCRLine structures
                words = []
                for text, score, poly in zip(gpu_result["rec_texts"], gpu_result["rec_scores"], gpu_result["rec_polys"]):
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
                raw_text = "\n".join(l.text for l in lines)
                
                # Run existing local extraction parser
                gpu_ocr_timings = {
                    "model_initialization_ms": 0.0,
                    "model_access_ms": 0.0,
                    "image_reading_ms": 0.0,
                    "ocr_inference_ms": ocr_time_ms,
                    "ocr_word_parsing_ms": 0.0,
                    "line_grouping_ms": 0.0,
                    "ocr_text_join_ms": 0.0,
                    "ocr_total_ms": ocr_time_ms
                }
                
                result = extract_land_document_from_lines(lines, raw_text, temp_path, timings=gpu_ocr_timings)
                
                total_time_ms = (perf_counter() - t_total_start) * 1000
                result.setdefault("profiling_ms", {})
                result["profiling_ms"]["pipeline_total_ms"] = round(total_time_ms, 3)
                
                timing_info = f"""
                <div style="background: rgba(31, 41, 55, 0.05); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid var(--border); font-size: 14px; display: grid; gap: 8px;">
                  <div><strong>Processing Mode:</strong> Google Colab GPU</div>
                  <div><strong>GPU Status:</strong> <span style="color: #14532d; font-weight: bold;">✓ Connected</span></div>
                  <div><strong>GPU Hardware:</strong> {gpu_name}</div>
                  <div><strong>OCR Inference Time:</strong> {ocr_time_ms:.2f} ms</div>
                  <div><strong>Network Transit Time:</strong> {network_time_ms:.2f} ms</div>
                  <div><strong>Total Processing Time:</strong> {total_time_ms:.2f} ms</div>
                </div>
                """
            else:
                # Local CPU Path (untouched logic)
                t_total_start = perf_counter()
                result = extract_land_document(temp_path)
                total_time_ms = (perf_counter() - t_total_start) * 1000
                
                ocr_time_ms = result.get("profiling_ms", {}).get("ocr_total_ms", 0.0)
                
                timing_info = f"""
                <div style="background: rgba(31, 41, 55, 0.05); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid var(--border); font-size: 14px; display: grid; gap: 8px;">
                  <div><strong>Processing Mode:</strong> Local CPU</div>
                  <div><strong>OCR Inference Time:</strong> {ocr_time_ms:.2f} ms</div>
                  <div><strong>Total Processing Time:</strong> {total_time_ms:.2f} ms</div>
                </div>
                """

            payload = json.dumps(result, indent=2, ensure_ascii=False)
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            image_data = base64.b64encode(uploaded).decode("ascii")
            preview = f'<img src="data:{mime_type};base64,{image_data}" alt="Uploaded image preview">'
            message = f"Processed {html.escape(filename)} successfully."

            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

            page = render_page(
                payload=payload,
                message=message,
                preview=f'<p><strong>{html.escape(filename)}</strong></p>{preview}',
                cpu_selected=cpu_sel,
                gpu_selected=gpu_sel,
                colab_url_value=colab_url,
                timing_info=timing_info,
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
            
            timing_info = f"""
            <div style="background: rgba(220, 38, 38, 0.08); padding: 16px; border-radius: 14px; margin-bottom: 16px; border: 1px solid #fecaca; font-size: 14px; display: grid; gap: 8px; color: #991b1b;">
              <div><strong>Processing Mode:</strong> Google Colab GPU</div>
              <div><strong>GPU Status:</strong> <span style="font-weight: bold;">✗ Unavailable</span></div>
              <div><strong>Error Details:</strong> {html.escape(str(exc))}</div>
            </div>
            """ if processing_mode == "gpu" else ""
            
            page = render_page(
                payload=error_payload,
                message="Extraction failed.",
                cpu_selected=cpu_sel,
                gpu_selected=gpu_sel,
                colab_url_value=colab_url,
                timing_info=timing_info
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
    get_paddle_ocr_model()
    print("PaddleOCR models pre-loaded successfully!")
    server = ThreadingHTTPServer(("127.0.0.1", 8000), LandExtractorHandler)
    print("Land extractor web app running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
