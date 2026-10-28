from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Iterable

from .models import CompressionError


# ---------------------------------------------------------------------------
# Minimal JPEG -> PDF writer (for the raster fallback)


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(data: bytes) -> tuple[int, int, int]:
    """Return (width, height, colour components) for a JPEG byte string."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise CompressionError("栅格化输出不是有效的 JPEG")
    i = 2
    while i + 4 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xDA:  # start of scan; no frame header after this
            break
        if i + 2 > len(data):
            break
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2:
            break
        if marker in _SOF_MARKERS and i + 7 <= len(data):
            height = int.from_bytes(data[i + 3:i + 5], "big")
            width = int.from_bytes(data[i + 5:i + 7], "big")
            components = data[i + 7]
            if width and height:
                return width, height, components
        i += length
    raise CompressionError("无法读取栅格化图片的尺寸")


class _PdfBuilder:
    """Tiny PDF 1.4 writer that wraps JPEG pages without re-encoding them."""

    def __init__(self) -> None:
        self.objects: list[Optional[bytes]] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def put(self, object_id: int, body: bytes) -> None:
        self.objects[object_id - 1] = body

    def add(self, body: bytes) -> int:
        oid = self.reserve()
        self.put(oid, body)
        return oid

    def stream(self, dictionary: str, payload: bytes) -> bytes:
        return (
            dictionary.encode("ascii")
            + b"\nstream\n"
            + payload
            + b"\nendstream"
        )

    def write(self, path: Path) -> None:
        if any(obj is None for obj in self.objects):
            raise CompressionError("内部 PDF 结构生成失败")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, body in enumerate(self.objects, start=1):
            offsets.append(len(out))
            out.extend(f"{index} 0 obj\n".encode("ascii"))
            out.extend(body)  # type: ignore[arg-type]
            out.extend(b"\nendobj\n")
        xref = len(out)
        out.extend(f"xref\n0 {len(self.objects) + 1}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        out.extend(
            (f"trailer\n<< /Size {len(self.objects) + 1} /Root 1 0 R "
             f"/Info 3 0 R >>\n"
             f"startxref\n{xref}\n%%EOF\n").encode("ascii")
        )
        path.write_bytes(bytes(out))


def build_pdf_from_jpegs(jpegs: Iterable[Path], output: Path, dpi: int) -> int:
    """Combine JPEG pages into a compact PDF.  Returns the page count."""
    builder = _PdfBuilder()
    catalog_id = builder.reserve()
    pages_id = builder.reserve()
    info_id = builder.reserve()

    image_cache: dict[bytes, int] = {}
    pages: list[tuple[int, int, int, int, int]] = []  # page, image, content, w, h
    page_count = 0

    for jpeg_path in jpegs:
        data = jpeg_path.read_bytes()
        width, height, components = jpeg_info(data)
        if components == 1:
            colorspace = "/DeviceGray"
        elif components == 3:
            colorspace = "/DeviceRGB"
        elif components == 4:
            colorspace = "/DeviceCMYK"
        else:
            raise CompressionError("不支持的 JPEG 颜色格式")

        digest = hashlib.sha1(data).digest()
        image_id = image_cache.get(digest)
        if image_id is None:
            image_dict = (
                f"<< /Type /XObject /Subtype /Image /Width {width} "
                f"/Height {height} /ColorSpace {colorspace} "
                f"/BitsPerComponent 8 /Filter /DCTDecode /Interpolate true "
                f"/Length {len(data)} >>"
            )
            image_id = builder.add(builder.stream(image_dict, data))
            image_cache[digest] = image_id

        width_pt = max(1.0, width * 72.0 / dpi)
        height_pt = max(1.0, height * 72.0 / dpi)
        content = (
            f"q\n{width_pt:.2f} 0 0 {height_pt:.2f} 0 0 cm\n/Im0 Do\nQ\n"
        ).encode("ascii")
        content_id = builder.add(
            builder.stream(f"<< /Length {len(content)} >>", content)
        )

        page_id = builder.reserve()
        pages.append((page_id, image_id, content_id, width, height))
        page_count += 1

    if not page_count:
        raise CompressionError("没有可写入的页面")

    kids = " ".join(f"{p[0]} 0 R" for p in pages)
    builder.put(
        catalog_id,
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"),
    )
    builder.put(
        pages_id,
        f"<< /Type /Pages /Count {page_count} /Kids [ {kids} ] >>".encode("ascii"),
    )
    builder.put(
        info_id,
        f"<< /Producer (PDF Squeezer) /CreationDate (D:{time.strftime('%Y%m%d%H%M%S')}) >>".encode("ascii"),
    )

    for page_id, image_id, content_id, width, height in pages:
        width_pt = max(1.0, width * 72.0 / dpi)
        height_pt = max(1.0, height * 72.0 / dpi)
        page = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {width_pt:.2f} {height_pt:.2f}] "
            f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        builder.put(page_id, page)

    builder.write(output)
    return page_count


def build_contact_sheet_pdf(
    jpegs: Iterable[Path], output: Path
) -> tuple[int, int]:
    """Tile every page thumbnail onto one (possibly very large) PDF page.

    This is the final fallback for documents with hundreds of pages: the page
    count collapses to one, so a low-resolution JPEG of the contact sheet fits
    comfortably below the size limit.
    """
    pages = list(jpegs)
    if not pages:
        raise CompressionError("没有可生成缩略图的页面")

    columns = max(1, math.ceil(math.sqrt(len(pages) / 1.35)))
    rows = math.ceil(len(pages) / columns)

    cell_w, cell_h, gap, margin = 56.0, 74.0, 4.0, 12.0
    page_w = columns * (cell_w + gap) + margin * 2
    page_h = rows * (cell_h + gap) + margin * 2
    if max(page_w, page_h) > 14000:
        factor = 14000.0 / max(page_w, page_h)
        cell_w *= factor
        cell_h *= factor
        gap *= factor
        margin *= factor
        page_w = columns * (cell_w + gap) + margin * 2
        page_h = rows * (cell_h + gap) + margin * 2

    builder = _PdfBuilder()
    catalog_id = builder.reserve()
    pages_id = builder.reserve()
    info_id = builder.reserve()

    images: list[tuple[int, int, int]] = []  # image object, width px, height px
    for jpeg_path in pages:
        data = jpeg_path.read_bytes()
        width, height, components = jpeg_info(data)
        if components == 1:
            colorspace = "/DeviceGray"
        elif components == 3:
            colorspace = "/DeviceRGB"
        elif components == 4:
            colorspace = "/DeviceCMYK"
        else:
            raise CompressionError("不支持的 JPEG 颜色格式")
        image_dict = (
            f"<< /Type /XObject /Subtype /Image /Width {width} "
            f"/Height {height} /ColorSpace {colorspace} "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Interpolate true "
            f"/Length {len(data)} >>"
        )
        image_id = builder.add(builder.stream(image_dict, data))
        images.append((image_id, width, height))

    ops: list[str] = []
    for index, (image_id, width, height) in enumerate(images):
        row = index // columns
        column = index % columns
        cell_x = margin + column * (cell_w + gap)
        cell_top = page_h - margin - row * (cell_h + gap)
        cell_y = cell_top - cell_h
        aspect = width / height if height else 0.707
        cell_aspect = cell_w / cell_h
        if aspect >= cell_aspect:
            draw_w = cell_w
            draw_h = cell_w / aspect
        else:
            draw_h = cell_h
            draw_w = cell_h * aspect
        x = cell_x + (cell_w - draw_w) / 2
        y = cell_y + (cell_h - draw_h) / 2
        # A hairline keeps white pages visually separated in the grid.
        ops.append(
            f"q 0.82 w 0.82 0.82 0.82 RG {cell_x:.2f} {cell_y:.2f} "
            f"{cell_w:.2f} {cell_h:.2f} re S Q"
        )
        ops.append(
            f"q {draw_w:.2f} 0 0 {draw_h:.2f} {x:.2f} {y:.2f} cm "
            f"/Im{index} Do Q"
        )
    content = ("\n".join(ops) + "\n").encode("ascii")
    content_id = builder.add(
        builder.stream(f"<< /Length {len(content)} >>", content)
    )
    page_id = builder.reserve()

    resources = " ".join(
        f"/Im{index} {image_id} 0 R"
        for index, (image_id, _width, _height) in enumerate(images)
    )
    page = (
        f"<< /Type /Page /Parent {pages_id} 0 R "
        f"/MediaBox [0 0 {page_w:.2f} {page_h:.2f}] "
        f"/Resources << /XObject << {resources} >> >> "
        f"/Contents {content_id} 0 R >>"
    ).encode("ascii")
    builder.put(page_id, page)
    builder.put(
        catalog_id,
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"),
    )
    builder.put(
        pages_id,
        f"<< /Type /Pages /Count 1 /Kids [ {page_id} 0 R ] >>".encode("ascii"),
    )
    builder.put(
        info_id,
        f"<< /Producer (PDF Squeezer) /CreationDate (D:{time.strftime('%Y%m%d%H%M%S')}) >>".encode("ascii"),
    )
    builder.write(output)
    return len(images), columns * rows
