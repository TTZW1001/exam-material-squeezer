from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import (
    CONTACT_PROFILES as _CONTACT_PROFILES,
    IMAGE_JPG_PROFILES as _IMAGE_JPG_PROFILES,
    IMAGE_PNG_PROFILES as _IMAGE_PNG_PROFILES,
    RASTER_PROFILES as _RASTER_PROFILES,
    SMART_DPIS as _SMART_DPIS,
    TARGET_BYTES,
)
from .ghostscript import _CREATE_NO_WINDOW
from .image_ops import IMAGE_EXTENSIONS, compress_image_file
from .models import CancelledError, CompressionError, CompressionResult
from .pdf_builder import build_contact_sheet_pdf, build_pdf_from_jpegs


# ---------------------------------------------------------------------------
# File helpers


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        value = num_bytes / 1024
        return f"{value:.0f} KB" if value.is_integer() else f"{value:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        value = num_bytes / (1024 * 1024)
        return f"{value:.0f} MB" if value.is_integer() else f"{value:.2f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def _unique_path(
    directory: Path, base: str, suffix: str = ".pdf"
) -> Path:
    candidate = directory / f"{base}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}_{counter}{suffix}"
        counter += 1
    return candidate


def output_filename(source: Path, timestamp: str) -> str:
    return f"{source.stem}_压缩_{timestamp}"


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _friendly_gs_error(stderr: bytes) -> str:
    text = _decode(stderr)
    low = text.lower()
    if "password" in low or "encrypted" in low:
        return "PDF 已加密，暂不支持自动压缩"
    if "undefinedfilename" in low or "no such file" in low:
        return "Ghostscript 无法读取该文件"
    if "invalidfileaccess" in low or "invalidfile" in low:
        return "文件无法读取，可能已损坏或没有权限"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [line for line in lines if "ghostscript" not in line.lower()]
    if useful:
        return useful[-1][:180]
    return "Ghostscript 处理失败"


# ---------------------------------------------------------------------------
# Compressor


class MaterialCompressor:
    def __init__(
        self,
        gs_path: str,
        target_bytes: int = TARGET_BYTES,
        progress: Optional[Callable[[float, str], None]] = None,
        log: Optional[Callable[[str, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.gs_path = gs_path
        self.target_bytes = target_bytes
        self._progress = progress
        self._log = log
        self._cancel = cancel_event or threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

    # -- public ---------------------------------------------------------

    @property
    def _target_label(self) -> str:
        return human_size(self.target_bytes)

    def cancel(self) -> None:
        self._cancel.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def compress_file(
        self,
        source: Path,
        output_root: Path,
        timestamp: str,
        relative_parent: Path = Path("."),
    ) -> CompressionResult:
        source = source.resolve()
        output_root = output_root.resolve()
        self._check_cancel()

        if not source.is_file():
            raise CompressionError("文件不存在")
        try:
            with source.open("rb") as handle:
                if b"%PDF-" not in handle.read(1024):
                    raise CompressionError("不是有效的 PDF 文件")
        except OSError as exc:
            raise CompressionError(f"无法读取文件：{exc}") from exc

        target_dir = output_root / relative_parent
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CompressionError(f"无法创建输出文件夹：{exc}") from exc

        destination = _unique_path(
            target_dir, output_filename(source, timestamp)
        )
        original_size = source.stat().st_size

        # Already within budget: keep the original bytes and quality.
        if original_size < self.target_bytes:
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                raise CompressionError(f"无法写入输出文件：{exc}") from exc
            self._emit_progress(
                1.0, f"原文件已低于 {self._target_label}，直接保留"
            )
            return CompressionResult(
                source, destination, original_size, destination.stat().st_size,
                "原文件已达标", outputs=(destination,),
                total_output_size=destination.stat().st_size,
            )

        workdir = Path(tempfile.mkdtemp(prefix="exam_squeezer_"))
        chosen: Optional[Path] = None
        method = ""
        try:
            self._emit_progress(0.03, "正在分析页面…")
            page_count = self._page_count(source)
            if page_count:
                self._log_message("info", f"共 {page_count} 页")

            # Pass 1: let Ghostscript recompress images/fonts.  Text and
            # vectors stay sharp; only rasters are downsampled.
            previous_size = original_size
            for index, dpi in enumerate(_SMART_DPIS):
                self._check_cancel()
                self._emit_progress(
                    0.08 + 0.42 * index / len(_SMART_DPIS),
                    f"智能压缩 · {dpi} DPI",
                )
                attempt = workdir / f"smart_{dpi}.pdf"
                self._run_smart(source, attempt, dpi)
                if not self._is_good_pdf(attempt):
                    self._log_message("warn", f"{dpi} DPI 未生成有效文件")
                    continue
                size = attempt.stat().st_size
                self._log_message(
                    "muted", f"智能压缩 {dpi} DPI → {human_size(size)}"
                )
                if size < self.target_bytes:
                    chosen = attempt
                    method = "智能压缩"
                    break
                # When Ghostscript can barely shrink a vector/text-heavy PDF,
                # lower image resolutions will not help either.  Go straight
                # to raster mode instead of wasting minutes on six passes.
                gain_limit = 0.92 if index == 0 else 0.93
                if size > previous_size * gain_limit:
                    self._log_message(
                        "muted", "智能压缩收益有限，切换到页面栅格化"
                    )
                    break
                previous_size = size

            # Pass 2: rasterise every page and rebuild a lean image PDF.  This
            # is the "whatever it takes" mode and always wins eventually for
            # ordinary documents.
            if chosen is None:
                chosen, method, actual_pages = self._raster_fallback(
                    source, workdir, page_count
                )
                if chosen is None and actual_pages:
                    chosen, method = self._contact_sheet_fallback(
                        source, workdir, actual_pages
                    )

            self._check_cancel()
            if chosen is None or not self._is_good_pdf(chosen):
                raise CompressionError("未能生成有效的压缩文件")
            final_size = chosen.stat().st_size
            if final_size >= self.target_bytes:
                raise CompressionError(
                    f"页数或内容过于复杂，无法压到 {self._target_label} 以下"
                )
            try:
                shutil.copy2(chosen, destination)
            except OSError as exc:
                raise CompressionError(f"无法写入输出文件：{exc}") from exc
            self._emit_progress(1.0, "压缩完成")
            return CompressionResult(
                source, destination, original_size, destination.stat().st_size,
                method, outputs=(destination,),
                total_output_size=destination.stat().st_size,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def compress_any(
        self,
        source: Path,
        output_root: Path,
        timestamp: str,
        output_format: str,
        relative_parent: Path = Path("."),
    ) -> CompressionResult:
        """Dispatch PDF and image inputs to the right pipeline."""
        suffix = source.suffix.lower()
        if suffix == ".pdf":
            if output_format.upper() == "PDF":
                return self.compress_file(
                    source, output_root, timestamp, relative_parent
                )
            return self.compress_to_images(
                source, output_root, timestamp, output_format.lower(),
                relative_parent,
            )
        if suffix in IMAGE_EXTENSIONS:
            return self.compress_image(
                source, output_root, timestamp, output_format.lower(),
                relative_parent,
            )
        raise CompressionError(f"不支持的文件类型：{suffix or '未知'}")

    def compress_image(
        self,
        source: Path,
        output_root: Path,
        timestamp: str,
        output_format: str,
        relative_parent: Path = Path("."),
    ) -> CompressionResult:
        """Compress one JPG/PNG into JPG, PNG or a one-page PDF."""
        fmt = output_format.lower().lstrip(".")
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in ("jpg", "png", "pdf"):
            raise CompressionError("不支持的图片输出格式")
        if source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise CompressionError("不是受支持的图片文件")

        source = source.resolve()
        output_root = output_root.resolve()
        self._check_cancel()
        if not source.is_file():
            raise CompressionError("文件不存在")

        target_dir = output_root / relative_parent
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CompressionError(f"无法创建输出文件夹：{exc}") from exc

        ext = ".pdf" if fmt == "pdf" else (".jpg" if fmt == "jpg" else ".png")
        destination = _unique_path(
            target_dir, output_filename(source, timestamp), ext
        )
        original_size = source.stat().st_size

        source_suffix = source.suffix.lower()
        same_format = (
            (fmt == "jpg" and source_suffix in (".jpg", ".jpeg"))
            or (fmt == "png" and source_suffix == ".png")
        )
        if same_format and original_size < self.target_bytes:
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                raise CompressionError(f"无法写入输出文件：{exc}") from exc
            self._emit_progress(
                1.0, f"原文件已低于 {self._target_label}，直接保留"
            )
            return CompressionResult(
                source, destination, original_size, destination.stat().st_size,
                "原文件已达标", outputs=(destination,),
                total_output_size=destination.stat().st_size,
            )

        self._emit_progress(0.04, "处理图片…")
        final_size, method = compress_image_file(
            source,
            destination,
            self.target_bytes,
            fmt,
            progress=self._emit_progress,
            cancel=self._check_cancel,
        )
        self._emit_progress(1.0, "图片处理完成")
        return CompressionResult(
            source,
            destination,
            original_size,
            final_size,
            method,
            outputs=(destination,),
            pages=1,
            total_output_size=final_size,
        )

    def compress_to_images(
        self,
        source: Path,
        output_root: Path,
        timestamp: str,
        image_format: str,
        relative_parent: Path = Path("."),
    ) -> CompressionResult:
        """Convert each PDF page to a JPG/PNG that is strictly below target.

        Multi-page PDFs deliberately produce one image per page instead of
        silently dropping pages.  Every image gets its own size check.
        """
        fmt = image_format.lower().lstrip(".")
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in ("jpg", "png"):
            raise CompressionError("不支持的图片格式")

        source = source.resolve()
        output_root = output_root.resolve()
        self._check_cancel()

        if not source.is_file():
            raise CompressionError("文件不存在")
        try:
            with source.open("rb") as handle:
                if b"%PDF-" not in handle.read(1024):
                    raise CompressionError("不是有效的 PDF 文件")
        except OSError as exc:
            raise CompressionError(f"无法读取文件：{exc}") from exc

        target_dir = output_root / relative_parent
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CompressionError(f"无法创建输出文件夹：{exc}") from exc

        original_size = source.stat().st_size
        ext = ".jpg" if fmt == "jpg" else ".png"
        workdir = Path(tempfile.mkdtemp(prefix="exam_squeezer_img_"))
        try:
            self._emit_progress(0.03, "正在分析页面…")
            page_count = self._page_count(source)
            if page_count:
                self._log_message("info", f"共 {page_count} 页，输出 {fmt.upper()}")

            profiles = list(
                _IMAGE_JPG_PROFILES if fmt == "jpg" else _IMAGE_PNG_PROFILES
            )
            if page_count:
                # Rendering hundreds of pages at 160 DPI just to discover that
                # page one is too large is wasteful; start lower for big files.
                if page_count > 300:
                    dpi_limit = 40
                elif page_count > 100:
                    dpi_limit = 58
                elif page_count > 40:
                    dpi_limit = 84
                elif page_count > 10:
                    dpi_limit = 120
                else:
                    dpi_limit = 160
                profiles = [p for p in profiles if p[0] <= dpi_limit]

            total = len(profiles)
            for index, (dpi, quality) in enumerate(profiles):
                self._check_cancel()
                self._emit_progress(
                    0.07 + 0.9 * index / max(1, total),
                    f"{fmt.upper()} 压缩 · {dpi} DPI",
                )
                render_dir = workdir / f"images_{dpi}"
                images = self._render_images(
                    source, render_dir, dpi, quality, fmt
                )
                if not images:
                    raise CompressionError("渲染页面时没有生成图片")
                if page_count and len(images) != page_count:
                    raise CompressionError(
                        f"页面渲染不完整（{len(images)}/{page_count} 页）"
                    )
                page_count = page_count or len(images)
                largest = max(path.stat().st_size for path in images)
                self._log_message(
                    "muted",
                    f"{fmt.upper()} {dpi} DPI"
                    + (f" · Q{quality}" if fmt == "jpg" else "")
                    + f" → 最大 {human_size(largest)}（{len(images)} 张）",
                )
                if largest >= self.target_bytes:
                    shutil.rmtree(render_dir, ignore_errors=True)
                    continue

                base = output_filename(source, timestamp)
                outputs: list[Path] = []
                try:
                    for page_index, image_path in enumerate(images, start=1):
                        page_suffix = (
                            f"_第{page_index:02d}页" if len(images) > 1 else ""
                        )
                        destination = _unique_path(
                            target_dir, base + page_suffix, ext
                        )
                        shutil.copy2(image_path, destination)
                        outputs.append(destination)
                except OSError as exc:
                    for produced in outputs:
                        try:
                            produced.unlink()
                        except OSError:
                            pass
                    raise CompressionError(f"无法写入输出文件：{exc}") from exc

                total_size = sum(path.stat().st_size for path in outputs)
                self._emit_progress(1.0, "图片压缩完成")
                return CompressionResult(
                    source,
                    outputs[0],
                    original_size,
                    largest,
                    f"{fmt.upper()} 逐页（{dpi} DPI）",
                    outputs=tuple(outputs),
                    pages=len(outputs),
                    total_output_size=total_size,
                )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        raise CompressionError(
            f"未能把 {fmt.upper()} 压到 {self._target_label} 以下"
        )

    # -- internals ------------------------------------------------------

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise CancelledError("已停止")

    def _emit_progress(self, fraction: float, text: str) -> None:
        if self._progress:
            self._progress(max(0.0, min(1.0, fraction)), text)

    def _log_message(self, level: str, text: str) -> None:
        if self._log:
            self._log(level, text)

    def _run(
        self, args: list[str], timeout: int = 300, capture_stdout: bool = False
    ) -> tuple[bytes, bytes]:
        self._check_cancel()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise CompressionError(f"无法启动 Ghostscript：{exc}") from exc
        with self._proc_lock:
            self._proc = proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise CompressionError("处理超时，文件可能过于复杂")
        finally:
            with self._proc_lock:
                self._proc = None
        self._check_cancel()
        if proc.returncode != 0:
            raise CompressionError(_friendly_gs_error(err))
        return out or b"", err or b""

    def _page_count(self, source: Path) -> Optional[int]:
        path = str(source).replace("\\", "/")
        path = path.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        code = f"({path}) (r) file runpdfbegin pdfpagecount = quit"
        args = [
            self.gs_path, "-q", "-dNODISPLAY", "-dNOSAFER",
            "-c", code,
        ]
        try:
            out, _ = self._run(args, timeout=25, capture_stdout=True)
            text = _decode(out).strip()
            for token in reversed(text.split()):
                if token.isdigit():
                    value = int(token)
                    if 0 < value < 1_000_000:
                        return value
        except (CompressionError, CancelledError) as exc:
            if isinstance(exc, CancelledError):
                raise
        return None

    def _run_smart(self, source: Path, output: Path, dpi: int) -> None:
        mono = min(300, max(120, dpi * 3))
        args = [
            self.gs_path,
            "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.5",
            "-dPDFSETTINGS=/screen",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            "-dSubsetFonts=true",
            "-dEmbedAllFonts=true",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageDownsampleType=/Bicubic",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dMonoImageDownsampleType=/Subsample",
            "-dColorImageDownsampleThreshold=1.0",
            "-dGrayImageDownsampleThreshold=1.0",
            "-dMonoImageDownsampleThreshold=1.0",
            "-dAutoFilterColorImages=false",
            "-dColorImageFilter=/DCTEncode",
            "-dAutoFilterGrayImages=false",
            "-dGrayImageFilter=/DCTEncode",
            f"-dColorImageResolution={dpi}",
            f"-dGrayImageResolution={dpi}",
            f"-dMonoImageResolution={mono}",
            f"-sOutputFile={output}",
            str(source),
        ]
        self._run(args)

    def _render_jpegs(
        self, source: Path, folder: Path, dpi: int, quality: int
    ) -> list[Path]:
        for old in folder.glob("page-*.jpg"):
            try:
                old.unlink()
            except OSError:
                pass
        pattern = folder / "page-%06d.jpg"
        args = [
            self.gs_path,
            "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
            "-sDEVICE=jpeg",
            f"-r{dpi}",
            f"-dJPEGQ={quality}",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            "-dUseCropBox",
            f"-sOutputFile={pattern}",
            str(source),
        ]
        self._run(args)
        return sorted(folder.glob("page-*.jpg"))

    def _render_images(
        self,
        source: Path,
        folder: Path,
        dpi: int,
        quality: int,
        image_format: str,
    ) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if image_format == "jpg" else "png"
        for old in folder.glob("page-*.*"):
            try:
                old.unlink()
            except OSError:
                pass
        pattern = folder / f"page-%06d.{ext}"
        args = [
            self.gs_path,
            "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
            f"-sDEVICE={'jpeg' if ext == 'jpg' else 'png256'}",
            f"-r{dpi}",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            "-dUseCropBox",
        ]
        if ext == "jpg":
            args.append(f"-dJPEGQ={quality}")
        args.extend([f"-sOutputFile={pattern}", str(source)])
        self._run(args)
        return sorted(folder.glob(f"page-*.{ext}"))

    def _raster_fallback(
        self, source: Path, workdir: Path, page_count: Optional[int]
    ) -> tuple[Optional[Path], str, int]:
        profiles = list(_RASTER_PROFILES)
        if page_count:
            if page_count <= 3:
                start = 0
            elif page_count <= 12:
                start = 1
            elif page_count <= 40:
                start = 2
            elif page_count <= 100:
                start = 4
            elif page_count <= 250:
                start = 6
            elif page_count <= 600:
                start = 9
            else:
                start = 10
            profiles = profiles[start:]

        actual_pages = page_count or 0
        total = len(profiles)
        for index, (dpi, quality) in enumerate(profiles):
            self._check_cancel()
            self._emit_progress(
                0.52 + 0.44 * index / max(1, total),
                f"极限压缩 · {dpi} DPI",
            )
            raster_dir = workdir / f"raster_{dpi}"
            raster_dir.mkdir(parents=True, exist_ok=True)
            jpegs = self._render_jpegs(source, raster_dir, dpi, quality)
            if not jpegs:
                continue
            actual_pages = len(jpegs)
            if page_count and actual_pages != page_count:
                raise CompressionError(
                    f"页面渲染不完整（{actual_pages}/{page_count} 页）"
                )
            output = workdir / f"raster_{dpi}.pdf"
            build_pdf_from_jpegs(jpegs, output, dpi)
            if output.is_file():
                size = output.stat().st_size
                self._log_message(
                    "muted",
                    f"极限压缩 {dpi} DPI · Q{quality} → {human_size(size)}",
                )
                if size < self.target_bytes:
                    return output, f"页面栅格化（{dpi} DPI）", actual_pages
            # Free disk space as we descend.
            shutil.rmtree(raster_dir, ignore_errors=True)
        return None, "", actual_pages

    def _contact_sheet_fallback(
        self, source: Path, workdir: Path, page_count: int
    ) -> tuple[Optional[Path], str]:
        """Last resort: put every page thumbnail on a single jumbo page."""
        self._check_cancel()
        self._log_message(
            "warn",
            f"常规压缩未能达标，将 {page_count} 页拼成一张缩略图总览…",
        )
        self._emit_progress(0.96, "生成缩略图总览…")

        if page_count <= 500:
            thumb_dpi = 30
        elif page_count <= 1500:
            thumb_dpi = 20
        else:
            thumb_dpi = 12
        pages_dir = workdir / "contact_pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        jpegs = self._render_jpegs(source, pages_dir, thumb_dpi, 60)
        if not jpegs:
            raise CompressionError("无法渲染页面缩略图")
        if len(jpegs) != page_count:
            raise CompressionError(
                f"页面渲染不完整（{len(jpegs)}/{page_count} 页）"
            )

        grid = workdir / "contact_grid.pdf"
        build_contact_sheet_pdf(jpegs, grid)
        self._log_message(
            "warn", f"已把 {page_count} 页拼成 1 页总览，正在压缩总览图…"
        )

        total = len(_CONTACT_PROFILES)
        for index, (dpi, quality) in enumerate(_CONTACT_PROFILES):
            self._check_cancel()
            self._emit_progress(
                0.965 + 0.03 * index / max(1, total),
                f"压缩总览图 · {dpi} DPI",
            )
            render_dir = workdir / f"contact_render_{dpi}"
            render_dir.mkdir(parents=True, exist_ok=True)
            rendered = self._render_jpegs(grid, render_dir, dpi, quality)
            if len(rendered) != 1:
                continue
            output = workdir / f"contact_{dpi}.pdf"
            build_pdf_from_jpegs(rendered, output, dpi)
            if output.is_file():
                size = output.stat().st_size
                self._log_message(
                    "muted",
                    f"缩略图总览 {dpi} DPI · Q{quality} → {human_size(size)}",
                )
                if size < self.target_bytes:
                    # Put every thumbnail page into the output name for clarity.
                    return (
                        output,
                        f"缩略图总览（{page_count} 页 → 1 页，{dpi} DPI）",
                    )
            shutil.rmtree(render_dir, ignore_errors=True)
        raise CompressionError(
            f"缩略图总览仍无法压到 {self._target_label} 以下"
        )

    @staticmethod
    def _is_good_pdf(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size < 64:
                return False
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return False
                handle.seek(max(0, path.stat().st_size - 1024))
                return b"%%EOF" in handle.read()
        except OSError:
            return False


# Backward-compatible name; the engine now handles PDF and image inputs.
PDFCompressor = MaterialCompressor
