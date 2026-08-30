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

from land_document_extractor import extract_land_document, get_paddle_ocr_model


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
          <input type="file" name="document_image" accept="image/*" required>
          <button type="submit">Upload and Extract</button>
        </form>
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
        <pre>$payload</pre>
      </section>
    </div>
  </div>
</body>
</html>
""")


def render_page(payload: str = "{}", message: str = "Upload a file to see the extracted JSON.", preview: str = "") -> bytes:
    return HTML_PAGE.substitute(
        payload=html.escape(payload),
        message=html.escape(message),
        preview=preview or "<p>No image uploaded yet.</p>",
    ).encode("utf-8")


class LandExtractorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        page = render_page()
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

        for part in parts:
            if b"Content-Disposition" not in part or b"name=\"document_image\"" not in part:
                continue
            header_blob, _, file_blob = part.partition(b"\r\n\r\n")
            if not file_blob:
                continue
            disposition = header_blob.decode("utf-8", errors="ignore")
            match = re.search(r'filename="([^"]+)"', disposition)
            if match:
                filename = Path(match.group(1)).name
            uploaded = file_blob.rsplit(b"\r\n", 1)[0]
            break

        if not uploaded:
            self.send_error(HTTPStatus.BAD_REQUEST, "No file was uploaded")
            return

        suffix = Path(filename).suffix or ".png"
        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded)
                temp_path = tmp.name

            result = extract_land_document(temp_path)
            result.setdefault("profiling_ms", {})
            json_start = perf_counter()
            payload = json.dumps(result, indent=2, ensure_ascii=False)
            result["profiling_ms"]["json_generation_ms"] = round((perf_counter() - json_start) * 1000, 3)
            result["profiling_ms"]["total_processing_ms"] = round(
                result["profiling_ms"].get("pipeline_total_ms", 0.0) + result["profiling_ms"]["json_generation_ms"],
                3,
            )
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
            page = render_page(payload=error_payload, message="Extraction failed.")
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
