"""Core compression engine, Ghostscript adapter and PDF writer."""

from .config import TARGET_BYTES, TARGET_LABEL
from .engine import MaterialCompressor, PDFCompressor, human_size
from .ghostscript import find_ghostscript, ghostscript_version
from .models import CancelledError, CompressionError, CompressionResult
from .pdf_builder import (
    build_contact_sheet_pdf,
    build_pdf_from_jpegs,
    jpeg_info,
)

__all__ = [
    "TARGET_BYTES",
    "TARGET_LABEL",
    "CancelledError",
    "CompressionError",
    "CompressionResult",
    "MaterialCompressor",
    "PDFCompressor",
    "build_contact_sheet_pdf",
    "build_pdf_from_jpegs",
    "find_ghostscript",
    "ghostscript_version",
    "human_size",
    "jpeg_info",
]
