# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-10-28

### Added

- First public release.
- PDF / JPG / JPEG / PNG input support.
- PDF / JPG / PNG output with a user-defined size limit in KB or MB.
- Single-file, multi-file and recursive folder batch processing.
- Per-file removal, progress, cancellation and local processing log.
- Automatic skipping of already completed files when settings are unchanged.
- Hard-limit fallback pipeline: Ghostscript, page rasterization, contact-sheet overview.
- Portable Windows executable packaging.

### Notes

- PDF input requires Ghostscript.
- Image input requires Pillow (bundled in the portable executable).
