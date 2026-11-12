import pymupdf
import pytest

import config
import document_processor as dp
import ocr_engine
from ocr_engine import OCRError, OCRUnavailableError


# ---- PDF text extraction ----------------------------------------------------
def test_pdf_text_extraction(text_pdf_bytes, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("OCR must not run for text PDFs")
    monkeypatch.setattr(ocr_engine, "ocr_image", fail)
    result = dp.process_document("notice.pdf", text_pdf_bytes)
    assert result.method == "pdf-text"
    assert "registration closes" in result.text.lower()
    assert result.ocr_pages == 0 and result.pages == 1


# ---- OCR fallback -----------------------------------------------------------
def test_scanned_pdf_falls_back_to_ocr(scanned_pdf_bytes, monkeypatch):
    monkeypatch.setattr(ocr_engine, "ocr_image", lambda img, languages=None: "OCR recovered notice text")
    result = dp.process_document("scan.pdf", scanned_pdf_bytes)
    assert result.method == "pdf-ocr"
    assert result.text == "OCR recovered notice text"
    assert result.ocr_pages == 1


def test_scanned_pdf_without_tesseract_gives_clear_error(scanned_pdf_bytes, monkeypatch):
    def unavailable(img, languages=None):
        raise OCRUnavailableError("Tesseract OCR is not installed")
    monkeypatch.setattr(ocr_engine, "ocr_image", unavailable)
    with pytest.raises(dp.DocumentError, match="(?i)tesseract|ocr"):
        dp.process_document("scan.pdf", scanned_pdf_bytes)


def test_ocr_failure_on_page_is_reported(scanned_pdf_bytes, monkeypatch):
    def boom(img, languages=None):
        raise OCRError("OCR failed.")
    monkeypatch.setattr(ocr_engine, "ocr_image", boom)
    with pytest.raises(dp.DocumentError, match="No readable text"):
        dp.process_document("scan.pdf", scanned_pdf_bytes)


def test_image_uses_ocr(png_bytes, monkeypatch):
    monkeypatch.setattr(ocr_engine, "ocr_image", lambda img, languages=None: "Text from image")
    result = dp.process_document("photo.png", png_bytes)
    assert result.method == "image-ocr" and result.text == "Text from image"


def test_image_with_no_text_errors(png_bytes, monkeypatch):
    monkeypatch.setattr(ocr_engine, "ocr_image", lambda img, languages=None: "")
    with pytest.raises(dp.DocumentError, match="No text"):
        dp.process_document("photo.png", png_bytes)


def test_ocr_engine_reports_missing_tesseract(monkeypatch, png_bytes):
    from PIL import Image
    import io
    monkeypatch.setattr(ocr_engine, "locate_tesseract", lambda: None)
    assert ocr_engine.get_status().available is False
    with pytest.raises(OCRUnavailableError):
        ocr_engine.ocr_image(Image.open(io.BytesIO(png_bytes)))


# ---- Invalid files ----------------------------------------------------------
def test_rejects_unsupported_extension():
    with pytest.raises(dp.DocumentError, match="Unsupported file type"):
        dp.validate_upload("notes.exe", b"MZ....")


def test_rejects_empty_file():
    with pytest.raises(dp.DocumentError, match="empty"):
        dp.validate_upload("a.pdf", b"")


def test_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 100)
    with pytest.raises(dp.DocumentError, match="too large"):
        dp.validate_upload("a.pdf", b"%PDF" + b"0" * 200)


def test_rejects_wrong_signature():
    with pytest.raises(dp.DocumentError, match="does not look like"):
        dp.validate_upload("fake.png", b"%PDF-1.4 not an image")


def test_malformed_pdf():
    with pytest.raises(dp.DocumentError):
        dp.process_document("broken.pdf", b"%PDF-1.4 this is not really a pdf")


def test_corrupt_image():
    with pytest.raises(dp.DocumentError, match="could not be read"):
        dp.process_document("broken.png", b"\x89PNG\r\n\x1a\n" + b"garbage")


def test_password_protected_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secret")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    with pytest.raises(dp.DocumentError, match="password"):
        dp.process_document("locked.pdf", data)
