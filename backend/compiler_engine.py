"""Dependency-light William Shunn-style manuscript PDF compiler."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterable


def _pdf_text(value: str) -> bytes:
    # Helvetica/Courier use WinAnsi in this minimal compiler. Replacing only
    # unsupported glyphs keeps the generated PDF valid without a font package.
    safe = value.replace("\t", "    ").encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _page_stream(lines: list[str], page_number: int, total_pages: int) -> bytes:
    stream = [
        "BT",
        "/F1 12 Tf",
        "72 720 Td",
        "14 TL",
    ]
    for line in lines:
        stream.append(f"({_pdf_text(line).decode('latin-1')}) Tj")
        stream.append("T*")
    stream.extend(
        [
            "/F1 9 Tf",
            "0 -14 Td",
            f"(Page {page_number} of {total_pages}) Tj",
            "ET",
        ]
    )
    return "\n".join(stream).encode("latin-1")


def compile_shunn_pdf(
    scenes: Iterable[dict[str, str]], output_path: str | Path, title: str, author: str
) -> Path:
    lines: list[str] = [author, title, ""]
    for scene in scenes:
        lines.extend([f"SCENE {scene['scene_number']}: {scene['title']}", ""])
        for paragraph in scene["text"].split("\n"):
            if paragraph.strip():
                lines.extend(textwrap.wrap(paragraph.strip(), width=72) or [""])
            lines.append("")

    if not lines:
        lines = [author, title]
    page_size = 48
    pages = [lines[index : index + page_size] for index in range(0, len(lines), page_size)]
    pages = pages or [[]]

    objects: list[bytes] = []
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects.append(
        f"<< /Type /Catalog /Pages 2 0 R >>".encode("ascii")
    )
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    )

    for index, page_lines in enumerate(pages, start=1):
        content = _page_stream(page_lines, index, len(pages))
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {3 + len(pages) * 2} 0 R >> >> "
                f"/Contents {4 + (index - 1) * 2} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )

    font_object_number = 3 + len(pages) * 2
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(pdf)
    return destination
