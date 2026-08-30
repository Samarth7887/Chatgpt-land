# Land Document Extractor

This project extracts structured JSON from uploaded land-document images such as sale agreements and GPA documents on Indian stamp paper.

## What it does

- Runs OCR with PaddleOCR.
- Reconstructs OCR words into readable lines.
- Extracts key fields like document number, dates, stamp details, parties, and continuation markers.
- Returns JSON shaped for downstream website or API use.

## Main files

- `land_document_extractor.py`: production extraction pipeline and CLI entry point.
- `run_ocr.py`: thin wrapper that runs the extractor.
- `web_app.py`: local upload website for drag-and-drop testing in the browser.
- `test_paddle.py`: OCR engine smoke test.
- `test_land_extractor.py`: parser regression test using a sample legal-document text fixture.

## Usage

Run on a document image:

```powershell
.\.venv\Scripts\python.exe run_ocr.py sample_document.png
```

Write only to stdout:

```powershell
.\.venv\Scripts\python.exe run_ocr.py sample_document.png --stdout-only
```

Run the local website:

```powershell
.\.venv\Scripts\python.exe web_app.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Output

The extractor writes `land_document_output.json` by default and includes:

- document identity fields
- party details
- property placeholder fields
- stamp information
- document feature flags
- OCR debug lines for inspection

## Notes

- The current parser is tuned for stamp-paper property documents similar to the sample you provided.
- OCR model files must be available to PaddleOCR on the machine where this runs.
- Property fields stay `null` when the first page explicitly continues to later pages.
