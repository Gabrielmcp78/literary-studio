"""Export a Literary Studio Markdown package as a Google Docs-compatible DOCX."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping


class GoogleDocsExportUnavailable(RuntimeError):
    """Raised when the local document conversion tool is not installed."""


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)
ET.register_namespace("ct", CT_NS)


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{R_NS}}}{tag}"


def _relationship(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _content_type(tag: str) -> str:
    return f"{{{CT_NS}}}{tag}"


def _footer_text(provenance: Mapping[str, Any]) -> str:
    generated_at = str(provenance.get("generated_at") or "unknown time")
    generated_at = generated_at.replace("T", " ").replace("+00:00", " UTC")
    if "." in generated_at:
        generated_at = generated_at.split(".", 1)[0] + (" UTC" if "UTC" not in generated_at else "")
    author = str(provenance.get("author") or "Gabriel McPherson")
    branch = str(provenance.get("branch") or "detached")
    commit = str(provenance.get("commit_sha") or "unknown")[:12]
    return f"{author} | {generated_at} | Git {branch} @ {commit} | Page "


def _page_field(parent: ET.Element) -> None:
    run = ET.SubElement(parent, _w("r"))
    begin = ET.SubElement(run, _w("fldChar"))
    begin.set(_w("fldCharType"), "begin")
    instruction_run = ET.SubElement(parent, _w("r"))
    instruction = ET.SubElement(instruction_run, _w("instrText"))
    instruction.set(f"{{{XML_NS}}}space", "preserve")
    instruction.text = " PAGE "
    separate = ET.SubElement(parent, _w("r"))
    separate_char = ET.SubElement(separate, _w("fldChar"))
    separate_char.set(_w("fldCharType"), "separate")
    result = ET.SubElement(parent, _w("r"))
    result_text = ET.SubElement(result, _w("t"))
    result_text.text = "1"
    end = ET.SubElement(parent, _w("r"))
    end_char = ET.SubElement(end, _w("fldChar"))
    end_char.set(_w("fldCharType"), "end")


def _add_docx_provenance_footer(
    destination: Path,
    provenance: Mapping[str, Any],
    page_start: int = 1,
) -> None:
    """Stamp the generated DOCX with a small footer and absolute page start."""
    with zipfile.ZipFile(destination, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    document_name = "word/document.xml"
    rels_name = "word/_rels/document.xml.rels"
    content_types_name = "[Content_Types].xml"
    if not all(name in members for name in (document_name, rels_name, content_types_name)):
        return

    document = ET.fromstring(members[document_name])
    relationships = ET.fromstring(members[rels_name])
    content_types = ET.fromstring(members[content_types_name])
    existing_targets = {rel.attrib.get("Target") for rel in relationships.findall(_relationship("Relationship"))}
    footer_index = 1
    while f"footer{footer_index}.xml" in existing_targets:
        footer_index += 1
    footer_name = f"word/footer{footer_index}.xml"
    rel_ids = {
        rel.attrib.get("Id", "")
        for rel in relationships.findall(_relationship("Relationship"))
    }
    rel_index = 1
    relationship_id = f"rId{rel_index}"
    while relationship_id in rel_ids:
        rel_index += 1
        relationship_id = f"rId{rel_index}"

    footer = ET.Element(_w("ftr"))
    paragraph = ET.SubElement(footer, _w("p"))
    paragraph_properties = ET.SubElement(paragraph, _w("pPr"))
    alignment = ET.SubElement(paragraph_properties, _w("jc"))
    alignment.set(_w("val"), "center")
    run = ET.SubElement(paragraph, _w("r"))
    run_properties = ET.SubElement(run, _w("rPr"))
    size = ET.SubElement(run_properties, _w("sz"))
    size.set(_w("val"), "12")
    color = ET.SubElement(run_properties, _w("color"))
    color.set(_w("val"), "777777")
    text = ET.SubElement(run, _w("t"))
    text.set(f"{{{XML_NS}}}space", "preserve")
    text.text = _footer_text(provenance)
    _page_field(paragraph)

    relationships.append(
        ET.Element(
            _relationship("Relationship"),
            {
                "Id": relationship_id,
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
                "Target": footer_name.removeprefix("word/"),
            },
        )
    )
    content_types.append(
        ET.Element(
            _content_type("Override"),
            {
                "PartName": f"/{footer_name}",
                "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
            },
        )
    )

    for sect_pr in document.iter(_w("sectPr")):
        footer_reference = ET.Element(_w("footerReference"), {_w("type"): "default", _r("id"): relationship_id})
        sect_pr.insert(0, footer_reference)
        page_numbering = sect_pr.find(_w("pgNumType"))
        if page_numbering is None:
            page_numbering = ET.SubElement(sect_pr, _w("pgNumType"))
        page_numbering.set(_w("start"), str(max(1, int(page_start))))

    members[document_name] = ET.tostring(document, encoding="utf-8", xml_declaration=True)
    members[rels_name] = ET.tostring(relationships, encoding="utf-8", xml_declaration=True)
    members[content_types_name] = ET.tostring(content_types, encoding="utf-8", xml_declaration=True)
    members[footer_name] = ET.tostring(footer, encoding="utf-8", xml_declaration=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_markdown_to_docx(
    markdown: str,
    output_path: str | Path,
    title: str,
    provenance: Mapping[str, Any] | None = None,
    page_start: int = 1,
) -> Path:
    """Convert Markdown to DOCX using Pandoc without touching manuscript sources.

    The resulting DOCX is intentionally a generated artifact. Google Drive can
    import it and open it as a native Google Doc; no Google credentials or
    network write are required by this local export.
    """
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise GoogleDocsExportUnavailable(
            "Pandoc is required for Google Docs export. Install it and restart the backend."
        )

    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                pandoc,
                "--from=gfm",
                "--to=docx",
                "--standalone",
                f"--metadata=title:{title}",
                "--output",
                str(destination),
            ],
            input=markdown,
            text=True,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GoogleDocsExportUnavailable(
            f"Pandoc could not create the Google Docs export: {detail.strip()}"
        ) from exc
    if provenance:
        _add_docx_provenance_footer(destination, provenance, page_start=page_start)
    return destination
