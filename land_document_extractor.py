import argparse
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import cv2
import numpy as np


os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")

if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


_PADDLE_OCR_MODEL = None
_PADDLE_OCR_INIT_LOCK = threading.Lock()
_PADDLE_OCR_PREDICT_LOCK = threading.Lock()
_PADDLE_OCR_INIT_MS: float | None = None


MONTH_LOOKUP = {
    "JANUARY": "01",
    "FEBRUARY": "02",
    "MARCH": "03",
    "APRIL": "04",
    "MAY": "05",
    "JUNE": "06",
    "JULY": "07",
    "AUGUST": "08",
    "SEPTEMBER": "09",
    "OCTOBER": "10",
    "NOVEMBER": "11",
    "DECEMBER": "12",
}


@dataclass
class OCRWord:
    text: str
    score: float
    points: list[list[int]]

    @property
    def x_min(self) -> int:
        return min(point[0] for point in self.points)

    @property
    def x_max(self) -> int:
        return max(point[0] for point in self.points)

    @property
    def y_min(self) -> int:
        return min(point[1] for point in self.points)

    @property
    def y_max(self) -> int:
        return max(point[1] for point in self.points)

    @property
    def y_center(self) -> float:
        return (self.y_min + self.y_max) / 2


@dataclass
class OCRLine:
    text: str
    score: float
    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @property
    def y_center(self) -> float:
        return (self.y_min + self.y_max) / 2


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_upper(value: str) -> str:
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.upper().strip()


def clean_field(value: str | None) -> str | None:
    if not value:
        return None
    value = normalize_space(value)
    value = value.strip(" ,.;:-")
    if not value:
        return None
    return value.title()


def clean_address(value: str | None) -> str | None:
    if not value:
        return None
    value = normalize_space(value)
    value = re.sub(r"\b(?:AADH?A?R|AADHAAR|ADHAR|AADAHAR)\b.*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\(.*", "", value)
    value = value.replace("H. No.", "H.No.")
    value = value.replace("H.No..", "H.No.")
    value = value.replace("Hospitai", "Hospital")
    value = value.strip(" ,.;:-")
    return clean_field(value)


def smart_number(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", "", value)
    return value.replace("|", "/")


def format_relation(val: str | None) -> str | None:
    if not val:
        return None
    val = normalize_space(val)
    # Convert to title case first
    val = val.title()
    # Replace S/O, W/O, D/O variations
    val = re.sub(r"\bS/O\.?", "S/o", val, flags=re.IGNORECASE)
    val = re.sub(r"\bW/O\.?", "W/o", val, flags=re.IGNORECASE)
    val = re.sub(r"\bD/O\.?", "D/o", val, flags=re.IGNORECASE)
    # Clean up spacing around slashes and punctuation
    val = re.sub(r"\s+", " ", val).strip(" ,.;:-")
    return val


def assemble_address(block_text: str, name: str, relation: str, age_str: str, occup_str: str) -> str | None:
    rem = block_text
    if name:
        rem = re.sub(re.escape(name), "", rem, flags=re.IGNORECASE)
    if relation:
        rem = re.sub(re.escape(relation), "", rem, flags=re.IGNORECASE)
    if age_str:
        rem = re.sub(re.escape(age_str), "", rem, flags=re.IGNORECASE)
    if occup_str:
        rem = re.sub(re.escape(occup_str), "", rem, flags=re.IGNORECASE)
    
    markers = [
        r"\bIN\s*FAVOUR\s*OF\b",
        r"\bHEREINAFTER\s*CALLED\b",
        r"\bTHE\s+VENDORS?\b",
        r"\bTHE\s+VENDEES?\b",
        r"\bPRINCIPALS?\b",
        r"\bATTORNEYS?\b",
        r"\bVENDOR/PRINCIPAL\b",
        r"\bVENDEE/ATTORNEY\b",
        r"\bVENDEES/\s*ATTORNEYS\b",
        r"\(HEREINAFTER CALLED.*?\)",
        r"\bOccup(?:ation)?\b",
        r"\bAge\b",
        r"\bR/o\.?\b",
        r"^\s*\d+\s*[\]\)\.]",
        r"^\s*I\s*[,:\-\]\)]",
    ]
    for m in markers:
        rem = re.sub(m, "", rem, flags=re.IGNORECASE)
    
    rem = re.sub(
        r"\b(?:[a-zA-Z0-9]{2,4})?\s*(?:AADH?A?R|ADHAR|AADHAAR|UID)\s*(?:CARD)?\s*(?:NO\.?)?[:\s\-]*([X\d\s]{4,15})\b",
        "",
        rem,
        flags=re.IGNORECASE
    )
    
    rem = re.sub(r"\bH\.\s*No\.", "H_NO", rem, flags=re.IGNORECASE)
    rem = re.sub(r"\bNo\.", "NO_", rem, flags=re.IGNORECASE)
    chunks = re.split(r"[\.;]+", rem)
    cleaned_chunks = []
    for chunk in chunks:
        c = chunk.strip(" ,;:-")
        c = re.sub(r"\bH_NO\b", "H.No.", c, flags=re.IGNORECASE)
        c = re.sub(r"\bNO_\b", "No.", c, flags=re.IGNORECASE)
        c = normalize_space(c)
        if len(c) > 3:
            c = re.sub(r"^[a-zA-Z]\d{2,3}\s+", "", c, flags=re.IGNORECASE)
            c = re.sub(r"^[^a-zA-Z0-9]+", "", c)
            c = re.sub(r"[^a-zA-Z0-9]+$", "", c)
            if re.search(r"\b[a-zA-Z]{3,}\b", c) or re.search(r"\b\d{3,}\b", c):
                cleaned_chunks.append(c)
    
    if not cleaned_chunks:
        return None
    
    def chunk_key(c: str) -> int:
        cu = c.upper()
        if "H.NO" in cu or "HNO" in cu:
            return 0
        if re.search(r"\b\d{6}\b", cu) or "PIN CODE" in cu:
            return 2
        return 1
    
    sorted_chunks = sorted(cleaned_chunks, key=chunk_key)
    assembled = ", ".join(sorted_chunks)
    return clean_address(assembled)



def clean_ocr_noise(value: str) -> str:
    value = normalize_upper(value)
    value = value.replace("SCANNED", " ")
    value = value.replace("\\", "/")
    value = value.replace("|", "/")
    value = value.replace("O", "0")
    value = value.replace("I", "1")
    return normalize_space(value)


def parse_date_token(token: str | None) -> str | None:
    if not token:
        return None
    token = normalize_space(token)
    match = re.search(r"(\d{1,2})\D+(\d{1,2})\D+(\d{2,4})", token)
    if not match:
        return None
    day, month, year = match.groups()
    if len(year) == 2:
        year = f"20{year}"
    return f"{int(day):02d}-{int(month):02d}-{year}"


def _date_from_labeled_text(text: str, labels: tuple[str, ...]) -> str | None:
    upper = normalize_upper(text)
    for label in labels:
        pattern = re.compile(
            rf"\b{label}\b[^\d]{{0,20}}(\d{{1,2}}\D+\d{{1,2}}\D+\d{{2,4}})",
            flags=re.IGNORECASE,
        )
        match = pattern.search(upper)
        if match:
            parsed = parse_date_token(match.group(1))
            if parsed:
                return parsed
    return None


def parse_execution_date(text: str) -> str | None:
    pattern = re.compile(
        r"EXECUT(?:ED|ION)?\s+ON\s+THIS\s+(\d{1,2})(?:ST|ND|RD|TH)?\s+DAY\s+OF\s+([A-Z]+)[\s\-/,]+(\d{4})",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTH_LOOKUP.get(month_name.upper())
    if not month:
        return None
    return f"{int(day):02d}-{month}-{year}"





def extract_stamp_number_from_text(text: str) -> str | None:
    prefixed_patterns = [
        r"\b(?!(?:NO|SC|DOC|LIC|ACK|CASH|CELL|SI|RL|STAMP|TELANGANA|INDIA)\b)([A-Z]{1,3})\s*(\d{5,10})\b",
    ]
    for pattern in prefixed_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            prefix, number = match.groups()
            return f"{prefix.upper()} {number}"

    candidate = extract_pattern(
        text,
        [
            r"\bSTAMP\s*(?:NO\.?|NUMBER)?[:\s]*([0-9]{5,10})",
            r"\b([0-9]{6,10})\b(?=.*STAMP VENDOR)",
            r"\b([0-9]{6,10})\b",
        ],
    )
    return smart_number(candidate)


def extract_document_number_from_text(text: str) -> str | None:
    pattern = re.compile(
        r"\b(?:D|DOC(?:UMENT)?)\.?\s*NO\.?\s*[:\-]?\s*([0-9]{1,6})\s*[\/\s]\s*([0-9]{2,4})\b",
        re.IGNORECASE
    )
    match = pattern.search(text)
    if match:
        num, year = match.groups()
        return f"{num}/{year}"

    candidate = extract_pattern(
        text,
        [
            r"\bD\.?\s*NO\.?\s*[:\-]?\s*([0-9]{1,6}\s*/\s*[0-9]{2,4})",
            r"\bDOC(?:UMENT)?\s*NO\.?\s*[:\-]?\s*([0-9]{1,6}\s*/\s*[0-9]{2,4})",
            r"\bD\.?\s*NO\.?\s*[:\-]?\s*([0-9]{1,6}\s+[0-9]{2,4})",
        ],
    )
    if candidate:
        parts = re.split(r"[\/\s]+", candidate.strip())
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return smart_number(candidate)
    return None


def detect_languages(raw_text: str) -> list[str]:
    languages = ["English"]
    raw_upper = raw_text.upper()
    if re.search(r"[\u0C00-\u0C7F]", raw_text) or "TELANGANA" in raw_upper or "ANDHRA PRADESH" in raw_upper:
        languages.append("Telugu")
    return languages


def extract_pattern(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_space(match.group(1))
    return None


def group_words_into_lines(words: list[OCRWord]) -> list[OCRLine]:
    if not words:
        return []

    words = sorted(words, key=lambda item: (item.y_center, item.x_min))
    median_height = int(np.median([max(1, item.y_max - item.y_min) for item in words]))
    y_tolerance = max(12, int(median_height * 0.8))

    buckets: list[list[OCRWord]] = []
    for word in words:
        placed = False
        for bucket in buckets:
            bucket_center = sum(item.y_center for item in bucket) / len(bucket)
            if abs(word.y_center - bucket_center) <= y_tolerance:
                bucket.append(word)
                placed = True
                break
        if not placed:
            buckets.append([word])

    lines: list[OCRLine] = []
    for bucket in buckets:
        ordered = sorted(bucket, key=lambda item: item.x_min)
        line_text = normalize_space(" ".join(item.text for item in ordered))
        if not line_text:
            continue
        lines.append(
            OCRLine(
                text=line_text,
                score=sum(item.score for item in ordered) / len(ordered),
                x_min=min(item.x_min for item in ordered),
                y_min=min(item.y_min for item in ordered),
                x_max=max(item.x_max for item in ordered),
                y_max=max(item.y_max for item in ordered),
            )
        )

    return sorted(lines, key=lambda item: (item.y_center, item.x_min))


def _party_role_from_text(text: str, fallback_index: int) -> str:
    upper = normalize_upper(text)
    if "VENDOR/PRINCIPAL" in upper:
        return "Vendor/Principal"
    if "VENDEE" in upper or "ATTORNEY" in upper:
        return "Vendee/Attorney"
    if fallback_index == 0:
        return "Vendor/Principal"
    return "Vendee/Attorney"


def _is_party_start(text: str) -> bool:
    upper = normalize_upper(text)
    if re.search(r"\b(?:1|2|3|4|5|6|7|8|9)\s*[\]\)\.]\s*[A-Z]", upper):
        return True
    if re.match(r"^\s*I\s*[,:\-\]\)]\s*[A-Z]", upper):
        return True
    if re.search(r"^[A-Z][A-Z\s\.'-]{2,},\s*(?:S/O|W/O|D/O)\b", upper):
        return True
    return False


def _extract_party_from_block(block_text: str, role: str) -> dict[str, Any] | None:
    normalized = normalize_space(block_text.replace("INFAVOUR", "IN FAVOUR"))
    normalized = re.sub(r"\bIN FAVOUR OF\b", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(HEREINAFTER CALLED THE [^)]+?)\b", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bWHICH TERM AND EXPRESSION\b.*", " ", normalized, flags=re.IGNORECASE)
    normalized = normalize_space(normalized)
    if not normalized:
        return None

    marker_match = re.search(r"(?:^|\s)(?:I\s*[,:\-\]\)]|\d+\s*[\]\)\.])\s*[A-Z]", normalized)
    if marker_match:
        normalized = normalize_space(normalized[marker_match.start():])

    # 1. Detect Aadhaar
    has_aadhaar = False
    aadhaar_match = re.search(
        r"\b(?:AADH?A?R|ADHAR|AADHAAR|UID)\s*(?:CARD)?\s*(?:NO\.?)?[:\s\-]*([X\d\s]{4,15})\b",
        normalized,
        re.IGNORECASE
    )
    if aadhaar_match or "Aadhaar" in normalized or "Aadhar" in normalized or "Adhar" in normalized:
        has_aadhaar = True

    # 2. Name
    name_match = re.search(
        r"^(?:I|[0-9]+\s*[\]\)\.]?)\s*,?\s*(?P<name>[A-Z][A-Z\s\.'-]+?)(?=,\s*(?:S/O|W/O|D/O)\b|,\s*AGE\b|,\s*OCCUP\b|,\s*R/O\b|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not name_match:
        name_match = re.search(
            r"^(?P<name>[A-Z][A-Z\s\.'-]+?)(?=,\s*(?:S/O|W/O|D/O)\b|,\s*AGE\b|,\s*OCCUP\b|,\s*R/O\b|$)",
            normalized,
            flags=re.IGNORECASE,
        )
    if not name_match:
        return None
    name = clean_field(name_match.group("name"))

    # 3. Relation
    relation_match = re.search(r"\b(?P<relation>(?:S/O|W/O|D/O)\.?\s*[^,]+)", normalized, flags=re.IGNORECASE)
    relation = format_relation(relation_match.group("relation")) if relation_match else None

    # 4. Age
    age_match = re.search(r"\bAGE[:\.\s]*(?P<age>\d{1,3})(?:\s*YEARS?)?\b", normalized, flags=re.IGNORECASE)
    age = int(age_match.group("age")) if age_match else None

    # 5. Occupation
    occupation_match = re.search(r"\bOCCUP(?:ATION)?[:\.\s]*(?P<occupation>[^,]+)", normalized, flags=re.IGNORECASE)
    occupation = clean_field(occupation_match.group("occupation")) if occupation_match else None

    # 6. Present District
    present_district = None
    present_match = re.search(r"PRESENT(?:LY)?\s+([A-Z][A-Z\s]+?\sDISTRICT)", normalized, flags=re.IGNORECASE)
    if present_match:
        present_district = clean_field(present_match.group(1))
        # Remove present district from the text before address extraction
        normalized = re.sub(
            r",?\s*PRESENT(?:LY)?\s+[A-Z][A-Z\s]+?\sDISTRICT",
            "",
            normalized,
            flags=re.IGNORECASE,
        )

    # 7. Extract/Assemble Address
    age_str = age_match.group(0) if age_match else ""
    occup_str = occupation_match.group(0) if occupation_match else ""
    rel_str = relation_match.group(0) if relation_match else ""
    
    address = assemble_address(normalized, name, rel_str, age_str, occup_str)

    party = {
        "name": name,
        "relation": relation,
        "age": age,
        "occupation": occupation,
        "address": address,
        "role": role,
    }
    if has_aadhaar:
        party["aadhaar"] = "[MASKED]"
    if present_district:
        party["present_district"] = present_district
    return party


def parse_party_blocks(lines: list[OCRLine]) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []
    current_block: list[str] = []
    current_role: str | None = None

    def flush_block() -> None:
        nonlocal current_block, current_role
        if not current_block:
            current_role = None
            return
        block_text = " ".join(current_block)
        role = current_role or _party_role_from_text(block_text, len(parties))
        party = _extract_party_from_block(block_text, role)
        if party and party.get("name") and party["name"] not in {item["name"] for item in parties}:
            parties.append(party)
        current_block = []
        current_role = None

    for line in lines:
        text = normalize_space(line.text)
        upper = normalize_upper(text)

        boundary_marker = any(
            marker in upper
            for marker in (
                "HEREINAFTER CALLED THE VENDOR/PRINCIPAL",
                "HEREINAFTER CALLED THE VENDEES",
                "HEREINAFTER CALLED THE VENDEE",
                "WHICH TERM AND EXPRESSION",
            )
        )
        start_marker = _is_party_start(text)

        if start_marker and current_block:
            flush_block()

        if start_marker:
            current_role = _party_role_from_text(text, len(parties))
            current_block = [text]
            continue

        if current_block and not boundary_marker:
            current_block.append(text)
            continue

        if boundary_marker:
            flush_block()

    flush_block()

    structured_parties = []
    for idx, party in enumerate(parties):
        p_dict = {
            "party_number": idx + 1,
            "name": party["name"],
            "relation": party["relation"],
            "age": party["age"],
            "occupation": party["occupation"],
            "address": party["address"],
            "role": party["role"],
        }
        if "aadhaar" in party:
            p_dict["aadhaar"] = party["aadhaar"]
        if "present_district" in party:
            p_dict["present_district"] = party["present_district"]
        structured_parties.append(p_dict)

    return structured_parties


def detect_signature(image: np.ndarray, lines: list[OCRLine] | None = None) -> bool:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bottom_band = gray[int(gray.shape[0] * 0.75) :, :]
    _, thresh = cv2.threshold(bottom_band, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ink_ratio = float(np.mean(thresh > 0))

    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if area < 80 or area > 15000:
            continue
        if width < 30 or height < 6:
            continue
        aspect_ratio = width / max(1, height)
        if aspect_ratio >= 2.0:
            return True

    if lines:
        image_height = gray.shape[0]
        bottom_lines = [line for line in lines if line.y_center >= image_height * 0.75]
        if bottom_lines:
            combined = normalize_space(" ".join(line.text for line in bottom_lines))
            if any(kw in combined.upper() for kw in ["SIGN", "SIGNATURE", "THUMB", "LTI", "MARK"]):
                return True
            if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", combined) or re.search(r"\b[A-Z]\.\s*[A-Z][a-z]+\b", combined):
                if ink_ratio > 0.005:
                    return True
            short_bottom_lines = sum(1 for line in bottom_lines if len(normalize_space(line.text)) <= 80)
            low_confidence_bottom_lines = sum(1 for line in bottom_lines if line.score < 0.94)
            if short_bottom_lines and low_confidence_bottom_lines:
                if re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+", combined) or re.search(r"\b[A-Z]\.\s*[A-Z][a-z]+", combined):
                    return True
            if ink_ratio > 0.008 and low_confidence_bottom_lines and short_bottom_lines:
                return True

    return False


def detect_handwriting(lines: list[OCRLine], image_height: int) -> bool:
    if not lines:
        return False
    top_zone = image_height * 0.14
    bottom_zone = image_height * 0.18

    for line in lines:
        upper = normalize_upper(line.text)
        if line.y_center <= top_zone and re.search(r"\d", upper):
            return True
        if line.y_center >= image_height - bottom_zone and line.score < 0.93:
            return True
    return False


def infer_state(text: str) -> str | None:
    for state in ("TELANGANA", "ANDHRA PRADESH", "KARNATAKA", "MAHARASHTRA"):
        if state in text:
            return state.title()
    return None


def infer_document_type(text: str) -> str | None:
    if "AGREEMENT OF SALE-CUM-GENERAL POWER OF ATTORNEY" in text:
        return "Agreement of Sale-cum-General Power of Attorney"
    if "GENERAL POWER OF ATTORNEY" in text:
        return "General Power of Attorney"
    if "SALE DEED" in text:
        return "Sale Deed"
    return None


def infer_document_category(document_type: str | None) -> str | None:
    if not document_type:
        return None
    mapping = {
        "Agreement of Sale-cum-General Power of Attorney": "Property Transaction Document",
        "General Power of Attorney": "Property Authorization Document",
        "Sale Deed": "Property Transaction Document",
    }
    return mapping.get(document_type, "Property Document")


def build_important_notes(
    text: str,
    property_status: str,
    pii_detected: bool,
    handwritten_detected: bool = False,
    signature_detected: bool = False,
) -> list[str]:
    notes: list[str] = []
    if "CONTD" in text or "CONTINUES" in text:
        notes.append("This is page 1 of a multi-page document.")
    if property_status in ("CONTINUES_ON_NEXT_PAGE", "NOT_FOUND_ON_PAGE"):
        notes.append("Property details are not present on this page.")
    if "CONTD" in text or "2/P" in text:
        notes.append("Document explicitly contains a continuation marker.")
    if property_status == "CONTINUES_ON_NEXT_PAGE":
        notes.append("Do not infer survey numbers, boundaries, area, or ownership details from this page.")
    if pii_detected:
        notes.append("Aadhaar/identity numbers are present and should be masked in user-facing output.")
    if handwritten_detected:
        notes.append("The document contains both printed and handwritten content.")
    if signature_detected:
        notes.append("Signatures are visible at the bottom of the page.")
    return notes


def extract_stamp_value(text: str) -> str | None:
    value = extract_pattern(
        text,
        [
            r"\bRS\.?\s*([0-9]{1,5})\b",
            r"\b([0-9]{1,5})\s*RUPEES\b",
        ],
    )
    if not value:
        return None
    return f"Rs.{value}"


def extract_stamp_number(text: str) -> str | None:
    return extract_stamp_number_from_text(text)


def extract_document_number(text: str) -> str | None:
    return extract_document_number_from_text(text)


def extract_document_number_from_top_lines(lines: list[OCRLine], image_height: int) -> str | None:
    top_lines = [
        line for line in lines
        if line.y_center <= image_height * 0.25
    ]
    if not top_lines:
        return None

    candidates: list[str] = []
    for line in sorted(top_lines, key=lambda item: (item.y_center, item.x_min)):
        cleaned = clean_ocr_noise(line.text)
        candidates.append(cleaned)

        direct = extract_document_number(cleaned)
        if direct:
            return direct

        if any(marker in cleaned for marker in ("D.NO", "D NO", "NO:", "NO.")):
            digits = re.findall(r"\d{2,6}", cleaned)
            if len(digits) >= 2:
                year = digits[-1]
                number = digits[-2]
                if len(year) == 4 and len(number) <= 6:
                    return f"{number}/{year}"

        loose = re.search(r"[:\s]([0-9]{2,6})\s+([0-9]{4})\b", cleaned)
        if loose:
            return f"{loose.group(1)}/{loose.group(2)}"

    merged = clean_ocr_noise(" ".join(candidates))
    digits = re.findall(r"\d{2,6}", merged)
    if len(digits) >= 2:
        year = next((item for item in reversed(digits) if len(item) == 4), None)
        if year:
            year_index = digits.index(year)
            if year_index > 0:
                number = digits[year_index - 1]
                return f"{number}/{year}"
    return None


def extract_serial_number(text: str) -> str | None:
    candidate = extract_pattern(
        text,
        [
            r"\bS\.?\s*C\.?\s*NO\.?\s*[:\-]?\s*([A-Z]?\s*\d{3,10})\b",
            r"\bSERIAL\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*([A-Z]?\s*\d{3,10})\b",
            r"\bSC\s*NO\.?\s*[:\-]?\s*([A-Z]?\s*\d{3,10})\b",
            r"\bS[IL]['\s]*NO\.?\s*[:\-]?\s*([0-9]{1,6})",
        ],
    )
    if candidate:
        return normalize_space(candidate).upper() if re.search(r"[A-Z]", candidate) else normalize_space(candidate)
    return None


def extract_document_date(text: str) -> str | None:
    explicit = _date_from_labeled_text(text, ("DT", "DATE"))
    if explicit:
        return explicit
    return None


def build_words_from_paddle(result: dict[str, Any]) -> list[OCRWord]:
    words: list[OCRWord] = []
    for text, score, poly in zip(result["rec_texts"], result["rec_scores"], result["rec_polys"]):
        cleaned = normalize_space(str(text))
        if not cleaned:
            continue
        words.append(
            OCRWord(
                text=cleaned,
                score=float(score),
                points=[[int(point[0]), int(point[1])] for point in poly.tolist()],
            )
        )
    return words


def get_paddle_ocr_model() -> Any:
    global _PADDLE_OCR_MODEL, _PADDLE_OCR_INIT_MS
    if _PADDLE_OCR_MODEL is not None:
        return _PADDLE_OCR_MODEL

    with _PADDLE_OCR_INIT_LOCK:
        if _PADDLE_OCR_MODEL is not None:
            return _PADDLE_OCR_MODEL

        from paddleocr import PaddleOCR

        start = perf_counter()
        model = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        _PADDLE_OCR_MODEL = model
        _PADDLE_OCR_INIT_MS = (perf_counter() - start) * 1000
        return model


def _run_paddle_ocr_impl(image_path: str) -> tuple[list[OCRLine], str, dict[str, float]]:
    timings: dict[str, float] = {}
    t0 = perf_counter()
    ocr = get_paddle_ocr_model()
    if _PADDLE_OCR_INIT_MS is not None:
        timings["model_initialization_ms"] = _PADDLE_OCR_INIT_MS
    else:
        timings["model_initialization_ms"] = 0.0
    timings["model_access_ms"] = (perf_counter() - t0) * 1000

    # Robust image reading to bypass paddlex internal file reader bugs
    t_read = perf_counter()
    image = cv2.imread(image_path)
    if image is None:
        try:
            image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            pass
    if image is None:
        raise ValueError(f"Unable to read image file: {image_path}")
    timings["image_reading_ms"] = (perf_counter() - t_read) * 1000

    with _PADDLE_OCR_PREDICT_LOCK:
        t0 = perf_counter()
        result = ocr.predict(image)[0]
        timings["ocr_inference_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    words = build_words_from_paddle(result)
    timings["ocr_word_parsing_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    lines = group_words_into_lines(words)
    timings["line_grouping_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    raw_text = "\n".join(line.text for line in lines)
    timings["ocr_text_join_ms"] = (perf_counter() - t0) * 1000
    timings["ocr_total_ms"] = timings["ocr_inference_ms"] + timings["ocr_word_parsing_ms"] + timings["line_grouping_ms"] + timings["ocr_text_join_ms"]
    return lines, raw_text, timings


def _extract_stamp_metadata(lines: list[OCRLine], full_text: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "acknowledgement_number": None,
        "si_number": None,
        "cash_number": None,
        "sold_to": None,
        "sold_to_relation": None,
        "sold_to_residence": None,
        "for_whom": None,
        "license_number": None,
        "rl_number": None,
        "vendor_address": None,
        "vendor_phone": None,
    }

    source_text = normalize_space(full_text)

    ack = extract_pattern(source_text, [r"\bACK\.?\s*(?:NO\.?)?[:\s\-]*([0-9]{2,10})\b"])
    si = extract_pattern(source_text, [r"\bSI\s*(?:NO\.?)?[:\s\-\.]*([0-9][0-9\.\-\s]{0,10})"])
    cash = extract_pattern(source_text, [r"\bCASH\.?\s*(?:NO\.?)?[:\s\-]*([0-9]{1,10})\b"])
    sold_to = extract_pattern(
        source_text,
        [
            r"\bSOLD\s*TO\.?[^\w]{0,10}([A-Z][A-Z\.\s]+?)(?=,\s*S/O\b|\s+S/O\b|\s+R/O\b|,|$)",
        ],
    )
    sold_to_relation = extract_pattern(source_text, [r"\b(S/O\.?\s*[A-Z][A-Z\s\.]+?)(?=,|\bR/O\b|$)"])
    residence = extract_pattern(source_text, [r"\bR/O\.?\s*([A-Z0-9][A-Z0-9\s\-/\.]+?)(?=,|\bLIC\.?\b|\bR\.L\.?\b|$)"])
    for_whom = extract_pattern(source_text, [r"\bFOR\s*WHOM\.?\s*([^:\-\n]+?)(?=Cell:|Lic\b|$)"])
    license_number = extract_pattern(source_text, [r"\bLIC\.?\s*(?:NO\.?)?[:\s\-]*([0-9]{2,4}(?:-[0-9]{2,4}){2,3}/[0-9]{2,4})\b"])
    rl_number = extract_pattern(source_text, [r"\bR\.?\s*L\.?\s*(?:NO\.?)?[:\s\-]*([0-9]{2,4}(?:-[0-9]{2,4}){2,3}/[0-9]{2,4})\b"])

    # vendor address & phone from stamp header
    header_part = full_text.split("AGREEMENT OF SALE")[0]
    vendor_addr_match = re.search(r"\b(H\.?No\..*?)(?=Cell:|For Whom|Lic\.? No|$)", header_part, re.IGNORECASE | re.DOTALL)
    vendor_address = clean_address(vendor_addr_match.group(1)) if vendor_addr_match else None
    has_phone = re.search(r"\b(?:Cell|Phone|Mobile)\b", header_part, re.IGNORECASE) is not None

    metadata["acknowledgement_number"] = smart_number(ack)
    metadata["si_number"] = re.sub(r"\D", "", si) if si else None
    metadata["cash_number"] = smart_number(cash)
    metadata["sold_to"] = clean_field(sold_to)
    metadata["sold_to_relation"] = format_relation(sold_to_relation)
    metadata["sold_to_residence"] = clean_field(residence)
    metadata["for_whom"] = clean_field(for_whom)
    metadata["license_number"] = smart_number(license_number)
    metadata["rl_number"] = smart_number(rl_number)
    metadata["vendor_address"] = vendor_address
    metadata["vendor_phone"] = "[MASKED]" if has_phone else None
    return metadata


def run_paddle_ocr(image_path: str) -> tuple[list[OCRLine], str, dict[str, float]]:
    return _run_paddle_ocr_impl(image_path)


def extract_land_document_from_lines(
    lines: list[OCRLine],
    raw_text: str,
    image_path: str,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    pipeline_timings = dict(timings or {})
    t0 = perf_counter()
    full_text = normalize_upper(raw_text)
    pipeline_timings["text_normalization_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Unable to read image: {image_path}")
    pipeline_timings["image_loading_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    document_type = infer_document_type(full_text)
    document_category = infer_document_category(document_type)
    state = infer_state(full_text)
    document_number = extract_document_number(full_text) or extract_document_number_from_top_lines(lines, image.shape[0])
    serial_number = extract_serial_number(full_text)
    stamp_number = extract_stamp_number(full_text)
    stamp_value = extract_stamp_value(full_text)
    document_date = extract_document_date(full_text)
    execution_date = parse_execution_date(full_text)
    stamp_metadata = _extract_stamp_metadata(lines, full_text)
    pipeline_timings["field_extraction_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    parties = parse_party_blocks(lines)
    pipeline_timings["party_extraction_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    continuation_detected = "CONTD" in full_text or "2/P" in full_text or "NEXT PAGE" in full_text
    pii_detected = any(token in full_text for token in ("AADHAR", "AADHAAR", "ADHAR", "UID"))

    property_status = "CONTINUES_ON_NEXT_PAGE" if continuation_detected else "NOT_FOUND_ON_PAGE"
    languages = detect_languages(raw_text)
    signature_detected = detect_signature(image, lines)
    handwritten_text_detected = detect_handwriting(lines, image.shape[0])
    stamp_detected = bool(stamp_value or "NON JUDICIAL" in full_text or "STAMP VENDOR" in full_text)
    pipeline_timings["feature_detection_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    stamp_vendor = extract_pattern(
        full_text,
        [
            r"\b([A-Z][A-Z\s]+)\s+LICENSED STAMP VENDOR\b",
        ],
    )
    pipeline_timings["stamp_vendor_extraction_ms"] = (perf_counter() - t0) * 1000

    t0 = perf_counter()
    output = {
        "document_type": document_type,
        "document_category": document_category,
        "state": state,
        "document_number": document_number,
        "serial_number": serial_number,
        "stamp_number": stamp_number,
        "stamp_value": stamp_value,
        "document_date": document_date,
        "execution_date": execution_date,
        "parties": parties,
        "property": {
            "survey_number": None,
            "sub_survey_number": None,
            "khata_number": None,
            "patta_number": None,
            "area": None,
            "boundaries": None,
            "village": None,
            "mandal": None,
            "district": None,
            "status": property_status,
        },
        "stamp_information": {
            "stamp_vendor": clean_field(stamp_vendor),
            "stamp_vendor_type": "Licensed Stamp Vendor"
            if ("LICENSED STAMP VENDOR" in full_text or "LICENCED STAMP VENDOR" in full_text)
            else None,
            "stamp_number": stamp_number,
            "stamp_value": stamp_value,
            **stamp_metadata,
        },
        "document_features": {
            "languages": languages,
            "printed_text_detected": bool(lines),
            "handwritten_text_detected": handwritten_text_detected,
            "signature_detected": signature_detected,
            "stamp_detected": stamp_detected,
            "multi_page_document": continuation_detected,
            "continuation_detected": continuation_detected,
            "pii_detected": pii_detected,
        },
        "important_notes": build_important_notes(
            full_text,
            property_status,
            pii_detected,
            handwritten_detected=handwritten_text_detected,
            signature_detected=signature_detected,
        ),
        "ocr_debug": {
            "source_image": str(Path(image_path).resolve()),
            "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "line_count": len(lines),
            "lines": [line.text for line in lines],
        },
    }
    pipeline_timings["json_object_build_ms"] = (perf_counter() - t0) * 1000
    pipeline_timings["core_pipeline_ms"] = (
        pipeline_timings.get("ocr_total_ms", 0.0)
        + pipeline_timings.get("text_normalization_ms", 0.0)
        + pipeline_timings.get("image_loading_ms", 0.0)
        + pipeline_timings.get("field_extraction_ms", 0.0)
        + pipeline_timings.get("party_extraction_ms", 0.0)
        + pipeline_timings.get("feature_detection_ms", 0.0)
        + pipeline_timings.get("stamp_vendor_extraction_ms", 0.0)
        + pipeline_timings.get("json_object_build_ms", 0.0)
    )
    output["profiling_ms"] = {key: round(value, 3) for key, value in pipeline_timings.items()}

    return output


def extract_land_document(image_path: str) -> dict[str, Any]:
    t0 = perf_counter()
    lines, raw_text, ocr_timings = run_paddle_ocr(image_path)
    result = extract_land_document_from_lines(lines, raw_text, image_path, timings=ocr_timings)
    result.setdefault("profiling_ms", {})
    result["profiling_ms"]["pipeline_total_ms"] = round((perf_counter() - t0) * 1000, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract structured data from land document images.")
    parser.add_argument("image_path", help="Path to the uploaded land document image")
    parser.add_argument(
        "--output",
        default="land_document_output.json",
        help="Where to write the extracted JSON output",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print the extracted JSON without writing a file",
    )
    args = parser.parse_args()

    result = extract_land_document(args.image_path)
    result.setdefault("profiling_ms", {})
    json_start = perf_counter()
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    result["profiling_ms"]["json_generation_ms"] = round((perf_counter() - json_start) * 1000, 3)
    result["profiling_ms"]["total_processing_ms"] = round(
        result["profiling_ms"].get("pipeline_total_ms", 0.0) + result["profiling_ms"]["json_generation_ms"],
        3,
    )
    payload = json.dumps(result, indent=2, ensure_ascii=False)

    if not args.stdout_only:
        output_path = Path(args.output)
        output_path.write_text(payload, encoding="utf-8")
        print(f"Saved structured output to {output_path.resolve()}")

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
