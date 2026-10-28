"""Image input support (JPG/PNG → JPG/PNG/PDF) with hard size limits.

Pillow is optional at import time so that PDF-only workflows still work if the
dependency is missing; image inputs will show a clear install hint instead.
"""

from __future__ import annotations

import io
import math
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .models import CompressionError
from .pdf_builder import build_pdf_from_jpegs

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - depends on the user's environment
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# quality values tried at every scale before shrinking further
_JPEG_QUALITIES = (88, 78, 68, 58, 48, 38, 30, 23, 17, 12, 8, 5, 3, 1)
_PNG_COLOR_STEPS = (256, 128, 64, 32, 16, 8)


def _require_pillow() -> None:
    if Image is None or ImageOps is None:
        raise CompressionError(
            "处理 JPG/PNG 输入需要 Pillow。请运行：pip install Pillow"
        )


def _load_rgb_image(source: Path):
    _require_pillow()
    try:
        image = Image.open(source)
        image.load()
    except Exception as exc:
        raise CompressionError(f"无法读取图片：{exc}") from exc
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass

    dpi = _image_dpi(image)
    # Documents should not get black/odd backgrounds when a transparent PNG is
    # flattened into JPG or PDF.
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    return image, dpi


def _image_dpi(image) -> int:
    value = image.info.get("dpi")
    if isinstance(value, (tuple, list)) and value:
        try:
            dpi = float(value[0])
            if 24 <= dpi <= 1200:
                return int(round(dpi))
        except (TypeError, ValueError):
            pass
    return 96


def _initial_scale(width: int, height: int, target_bytes: int, fmt: str) -> float:
    pixels = max(1, width * height)
    if fmt == "jpg":
        budget = max(1024, target_bytes) * 8
    elif fmt == "png":
        budget = max(1024, target_bytes) * 3
    else:  # jpeg for PDF embedding
        budget = max(1024, target_bytes - 8192) * 8
    return min(1.0, max(0.002, math.sqrt(budget / pixels)))


def _scale_steps(start: float):
    steps: list[float] = []
    value = start
    while value > 0.0015:
        rounded = round(value, 4)
        if not steps or rounded != steps[-1]:
            steps.append(rounded)
        value *= 0.8
    if not steps or steps[-1] > 0.001:
        steps.append(0.001)
    return steps


def _resize(image, scale: float):
    if scale >= 0.999:
        return image
    width = max(1, int(round(image.width * scale)))
    height = max(1, int(round(image.height * scale)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _jpeg_bytes(image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(
        buffer, format="JPEG", quality=quality, optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


def _png_bytes(image, colors: int) -> bytes:
    buffer = io.BytesIO()
    palette = image.convert(
        "P", palette=Image.Palette.ADAPTIVE, colors=colors
    )
    palette.save(buffer, format="PNG", optimize=True, compress_level=9)
    return buffer.getvalue()


def compress_image_file(
    source: Path,
    destination: Path,
    target_bytes: int,
    output_format: str,
    progress: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], None]] = None,
) -> tuple[int, str]:
    """Write one compressed image/PDF under ``target_bytes``.

    Returns ``(final_size, method_label)``.  ``destination`` is only written
    after a candidate has passed the size check.
    """
    fmt = output_format.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("jpg", "png", "pdf"):
        raise CompressionError("不支持的图片输出格式")

    _require_pillow()
    image, dpi = _load_rgb_image(source)
    try:
        start_scale = _initial_scale(
            image.width, image.height, target_bytes, fmt
        )
        scales = _scale_steps(start_scale)
        jpeg_target = max(
            1024, target_bytes - 8192
        ) if fmt == "pdf" else target_bytes

        with tempfile.TemporaryDirectory(prefix="exam_squeezer_img_") as temp_dir:
            temp = Path(temp_dir)
            total = len(scales)
            for scale_index, scale in enumerate(scales):
                if cancel:
                    cancel()
                if progress:
                    progress(
                        0.08 + 0.88 * scale_index / max(1, total),
                        f"{fmt.upper()} 处理中 · 长边 {max(1, int(image.width * scale))} px",
                    )
                resized = _resize(image, scale)
                side = max(resized.size)

                if fmt in ("jpg", "pdf"):
                    for quality in _JPEG_QUALITIES:
                        data = _jpeg_bytes(resized, quality)
                        if fmt == "jpg":
                            if len(data) < target_bytes:
                                destination.write_bytes(data)
                                return len(data), (
                                    f"JPG 重编码（长边 {side}px · Q{quality}）"
                                )
                            continue

                        # PDF wrapper: leave room for one page object and xref.
                        if len(data) >= jpeg_target:
                            continue
                        jpeg_path = temp / "page.jpg"
                        pdf_path = temp / "page.pdf"
                        jpeg_path.write_bytes(data)
                        build_pdf_from_jpegs([jpeg_path], pdf_path, dpi)
                        size = pdf_path.stat().st_size
                        if size < target_bytes:
                            shutil.copyfile(pdf_path, destination)
                            return size, f"图片转 PDF（长边 {side}px）"
                else:
                    for colors in _PNG_COLOR_STEPS:
                        data = _png_bytes(resized, colors)
                        if len(data) < target_bytes:
                            destination.write_bytes(data)
                            return len(data), (
                                f"PNG 缩放（长边 {side}px · {colors}色）"
                            )
        raise CompressionError("图片在最低设置下仍无法达到目标大小")
    finally:
        image.close()
