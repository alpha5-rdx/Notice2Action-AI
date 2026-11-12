"""Tesseract OCR wrapper with image pre-processing and clear error types."""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Optional

import pytesseract
from PIL import Image, ImageOps

import config

logger = logging.getLogger(__name__)


class OCRError(Exception):
    """Base class for OCR failures."""


class OCRUnavailableError(OCRError):
    """Tesseract is not installed or could not be located."""


@dataclass
class OCRStatus:
    available: bool
    path: Optional[str] = None
    version: Optional[str] = None
    languages: tuple = ()
    message: str = ""


def locate_tesseract() -> Optional[str]:
    """Find the tesseract executable (env override, PATH, then Windows defaults)."""
    candidates = [os.environ.get(config.TESSERACT_CMD_ENV_VAR)]
    candidates.append(shutil.which("tesseract"))
    candidates.extend(config.TESSERACT_WINDOWS_PATHS)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _configure() -> str:
    path = locate_tesseract()
    if path is None:
        raise OCRUnavailableError(
            "Tesseract OCR is not installed or not on PATH. Install it (see README) "
            "or set the TESSERACT_CMD environment variable."
        )
    pytesseract.pytesseract.tesseract_cmd = path
    return path


def get_status() -> OCRStatus:
    try:
        path = _configure()
        version = str(pytesseract.get_tesseract_version())
        languages = tuple(pytesseract.get_languages(config=""))
        return OCRStatus(True, path, version, languages, "Tesseract is ready.")
    except OCRUnavailableError as exc:
        return OCRStatus(False, message=str(exc))
    except Exception as exc:  # broken install, permissions, etc.
        return OCRStatus(False, message=f"Tesseract found but not working: {type(exc).__name__}")


def preprocess_image(image: Image.Image) -> Image.Image:
    """Normalise orientation, colour and contrast to improve OCR accuracy."""
    image = ImageOps.exif_transpose(image)
    image = image.convert("L")
    if max(image.size) < 1200:  # upscale small images
        scale = 1200 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(image)


def ocr_image(image: Image.Image, languages: Optional[str] = None) -> str:
    """Run OCR on a PIL image and return the extracted text."""
    _configure()
    languages = languages or config.get_ocr_languages()
    try:
        text = pytesseract.image_to_string(preprocess_image(image), lang=languages)
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRUnavailableError("Tesseract executable could not be started.") from exc
    except pytesseract.TesseractError as exc:
        # Do not include OCR content in logs; the message only describes the failure.
        logger.warning("Tesseract failed: %s", type(exc).__name__)
        raise OCRError(
            f"OCR failed. Check that the language pack(s) '{languages}' are installed."
        ) from exc
    except Exception as exc:
        logger.warning("Unexpected OCR failure: %s", type(exc).__name__)
        raise OCRError("OCR failed unexpectedly while reading the image.") from exc
    return text.strip()
