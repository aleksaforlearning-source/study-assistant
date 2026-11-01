import difflib
import tempfile
from collections import Counter

import pymupdf
import pymupdf4llm

MIN_TEXT_LENGTH = 300
DRAWINGS_THRESHOLD = 30
DUPLICATE_RATIO = 0.9
BOILERPLATE_THRESHOLD = 0.6
PICTURE_MARKER = "<!-- Start of picture text -->"


def find_boilerplate(texts: list[str]) -> set[str]:
    counter = Counter()
    for text in texts:
        unique_lines = {line.strip() for line in text.split("\n") if line.strip()}
        counter.update(unique_lines)

    min_count = len(texts) * BOILERPLATE_THRESHOLD
    return {line for line, count in counter.items() if count >= min_count}


def strip_boilerplate(text: str, boilerplate: set[str]) -> str:
    lines = [l for l in text.split("\n") if l.strip() not in boilerplate]
    return "\n".join(lines).strip()


def parse_pdf(file_bytes: bytes) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    pages = pymupdf4llm.to_markdown(tmp_path, page_chunks=True, use_ocr=False)
    raw_texts = [p["text"] for p in pages]
    boilerplate = find_boilerplate(raw_texts)

    doc = pymupdf.open(tmp_path)
    result = []
    previous_text = ""

    for index, raw in enumerate(raw_texts):
        text = strip_boilerplate(raw, boilerplate)
        page = doc[index]
        drawings = len(page.get_drawings())
        has_picture = PICTURE_MARKER in text

        # A page that is only a picture or a diagram is kept with no text: the
        # vision worker describes it later. A page with nothing on it is skipped.
        # (Images count only here: a logo on every slide must not make short slides weak.)
        if not text and not page.get_images() and drawings < DRAWINGS_THRESHOLD:
            continue

        if previous_text and text:
            ratio = difflib.SequenceMatcher(None, previous_text, text).ratio()
            if ratio > DUPLICATE_RATIO:
                previous_text = text
                continue

        # Weak: mostly a picture, so the text layer says little. These go to the vision worker.
        is_weak = not text or len(text) < MIN_TEXT_LENGTH and (
            drawings >= DRAWINGS_THRESHOLD or has_picture
        )

        result.append({
            "page_number": index + 1,
            "text": text,
            "drawings_count": drawings,
            "is_weak": is_weak,
        })
        previous_text = text

    doc.close()
    return result