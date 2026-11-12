"""Upload validation and text extraction for PDFs and images."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pymupdf
from PIL import Image, UnidentifiedImageError

import config
import ocr_engine
from ocr_engine import OCRError, OCRUnavailableError

logger = logging.getLogger(__name__)

_MAGIC = {
    "pdf": (b"%PDF",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
}


class DocumentError(Exception):
    """A user-facing problem with the uploaded document."""


@dataclass
class ExtractionResult:
    text: str
    method: str  # "pdf-text", "pdf-ocr", "pdf-mixed", "image-ocr"
    pages: int = 0
    ocr_pages: int = 0
    warnings: List[str] = field(default_factory=list)


def get_extension(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def validate_upload(filename: str, data: bytes) -> str:
    """Validate extension, size and file signature. Returns the extension."""
    ext = get_extension(filename)
    if ext not in config.ALLOWED_EXTENSIONS:
        allowed = ", ".join(e.upper() for e in config.ALLOWED_EXTENSIONS)
        raise DocumentError(f"Unsupported file type '.{ext or '?'}'. Allowed types: {allowed}.")
    if not data:
        raise DocumentError("The uploaded file is empty.")
    if len(data) > config.MAX_FILE_SIZE_BYTES:
        size_mb = len(data) / (1024 * 1024)
        raise DocumentError(
            f"File is too large ({size_mb:.1f} MB). The maximum size is {config.MAX_FILE_SIZE_MB} MB."
        )
    if not any(data.startswith(sig) for sig in _MAGIC[ext]):
        raise DocumentError(
            f"The file content does not look like a valid {ext.upper()} file. "
            "It may be corrupted or have the wrong extension."
        )
    return ext


def _page_to_image(page: "pymupdf.Page") -> Image.Image:
    pix = page.get_pixmap(dpi=config.OCR_DPI)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def extract_pdf(data: bytes) -> ExtractionResult:
    """Extract text per page; fall back to OCR for pages with too little text."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise DocumentError("The PDF could not be opened. It may be corrupted.") from exc

    with doc:
        if doc.needs_pass:
            raise DocumentError("The PDF is password-protected. Please upload an unlocked copy.")
        if doc.page_count == 0:
            raise DocumentError("The PDF contains no pages.")

        warnings: List[str] = []
        page_count = doc.page_count
        if page_count > config.MAX_PDF_PAGES:
            warnings.append(
                f"Only the first {config.MAX_PDF_PAGES} of {page_count} pages were processed."
            )
            page_count = config.MAX_PDF_PAGES

        chunks: List[str] = []
        ocr_pages = 0
        ocr_failed = False
        for index in range(page_count):
            try:
                page = doc[index]
                text = page.get_text("text").strip()
            except Exception:
                warnings.append(f"Page {index + 1} could not be read.")
                continue

            if len(text) < config.MIN_CHARS_PER_PAGE and not ocr_failed:
                if ocr_pages >= config.MAX_OCR_PAGES:
                    warnings.append("OCR page limit reached; remaining scanned pages were skipped.")
                else:
                    try:
                        ocr_text = ocr_engine.ocr_image(_page_to_image(page))
                        ocr_pages += 1
                        if len(ocr_text) > len(text):
                            text = ocr_text
                    except OCRUnavailableError as exc:
                        ocr_failed = True
                        warnings.append(f"Scanned page(s) detected but OCR is unavailable: {exc}")
                    except OCRError as exc:
                        warnings.append(f"OCR failed on page {index + 1}: {exc}")
            if text:
                chunks.append(text)

    full_text = "\n\n".join(chunks).strip()
    if not full_text:
        raise DocumentError(
            "No readable text was found in this PDF. "
            + (warnings[0] if warnings else "Try uploading a clearer scan or an image of the notice.")
        )
    if ocr_pages == 0:
        method = "pdf-text"
    elif ocr_pages >= page_count:
        method = "pdf-ocr"
    else:
        method = "pdf-mixed"
    return ExtractionResult(full_text, method, page_count, ocr_pages, warnings)


def extract_image(data: bytes) -> ExtractionResult:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise DocumentError("The image could not be read. It may be corrupted.") from exc

    try:
        text = ocr_engine.ocr_image(image)
    except OCRUnavailableError as exc:
        raise DocumentError(str(exc)) from exc
    except OCRError as exc:
        raise DocumentError(str(exc)) from exc
    if not text:
        raise DocumentError(
            "No text could be recognised in this image. Try a sharper, higher-resolution photo."
        )
    return ExtractionResult(text, "image-ocr", pages=1, ocr_pages=1)


def process_document(filename: str, data: bytes) -> ExtractionResult:
    """Validate an upload and extract its text."""
    ext = validate_upload(filename, data)
    result = extract_pdf(data) if ext == "pdf" else extract_image(data)
    logger.info("Extracted document: method=%s pages=%d chars=%d", result.method, result.pages, len(result.text))
    return result
