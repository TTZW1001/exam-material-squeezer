"""Backward-compatible facade for code that imports ``compressor``.

New code should import from :mod:`core` instead.
"""

from core import (  # noqa: F401
    TARGET_BYTES,
    TARGET_LABEL,
    CancelledError,
    CompressionError,
    CompressionResult,
    MaterialCompressor,
    PDFCompressor,
    build_contact_sheet_pdf,
    build_pdf_from_jpegs,
    find_ghostscript,
    ghostscript_version,
    human_size,
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
