"""DOCX Document Parser and Reconstructor.
Extracts individual translatable segments from body paragraphs and table cell paragraphs.
Preserves multi-paragraph cells, bullet lists (w:numPr), runs, styles, and document hierarchy.
"""
from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Set, Union

import docx
from docx.document import Document as DocxDocument
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

log = logging.getLogger("cat.document")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def iter_table_paragraphs(table: Table) -> Iterator[Paragraph]:
    """Iterates through all paragraphs in a table row-by-row and cell-by-cell.
    Handles multi-paragraph cells explicitly without flattening cell.text or skipping cells.
    Deduplicates merged cells and recursively visits nested tables.
    """
    seen_cells: Set[Any] = set()

    for row in table.rows:
        for cell in row.cells:
            # Merged cells (vMerge / gridSpan) share the underlying _tc XML element.
            if cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)

            # 1. Explicitly iterate cell.paragraphs instead of flattening cell.text
            for paragraph in cell.paragraphs:
                yield paragraph

            # 2. Recursively traverse any nested tables inside the cell
            for nested_table in cell.tables:
                yield from iter_table_paragraphs(nested_table)


def iter_document_paragraphs(doc: DocxDocument) -> Iterator[Paragraph]:
    """Yields all paragraphs in document order:
    1. Body paragraphs
    2. Table cell paragraphs (explicitly iterating cell.paragraphs for every cell in table.rows)
    3. Header & footer paragraphs (if present)
    """
    # 1. Body paragraphs
    for p in doc.paragraphs:
        yield p

    # 2. Tables and nested tables
    for table in doc.tables:
        yield from iter_table_paragraphs(table)

    # 3. Section headers and footers
    for section in doc.sections:
        for hf_group in (
            (section.header, section.first_page_header, section.even_page_header),
            (section.footer, section.first_page_footer, section.even_page_footer),
        ):
            for hf in hf_group:
                if hf and not hf.is_linked_to_previous:
                    for p in hf.paragraphs:
                        yield p
                    for table in hf.tables:
                        yield from iter_table_paragraphs(table)


def is_translatable(text: str) -> bool:
    """Returns True if the text contains meaningful translatable content (letters or digits)."""
    stripped = text.strip()
    return bool(stripped and re.search(r"[\w\d]", stripped))


def extract_segments_from_docx(doc_or_bytes: Union[DocxDocument, bytes, BinaryIO]) -> List[str]:
    """Extracts translatable segments from a DOCX document.
    Each paragraph inside table cells is extracted as an individual translatable segment
    rather than treating the whole cell as a single block that fails or truncates.
    """
    if isinstance(doc_or_bytes, bytes):
        doc = docx.Document(io.BytesIO(doc_or_bytes))
    elif isinstance(doc_or_bytes, (io.BytesIO, io.BufferedReader)):
        doc = docx.Document(doc_or_bytes)
    else:
        doc = doc_or_bytes

    segments: List[str] = []
    for p in iter_document_paragraphs(doc):
        text = p.text.strip()
        if is_translatable(text):
            segments.append(text)

    return segments


def update_paragraph_text(p: Paragraph, translated_text: str) -> None:
    """Updates a paragraph's text while preserving its formatting, run styles,
    and bullet list attributes (w:numPr).

    - Does NOT delete or overwrite the paragraph element or its pPr properties.
    - Updates the primary text run (preserving font, bold, italic, size, color).
    - Clears secondary text runs so old text is replaced cleanly without duplicating.
    """
    runs = p.runs
    if not runs:
        # Paragraph has no runs; add one
        p.add_run(translated_text)
        return

    # Assign translated text to the first run to keep its run-level styling
    runs[0].text = translated_text

    # Clear subsequent runs so trailing old text is removed without corrupting the XML
    for r in runs[1:]:
        r.text = ""


def reconstruct_docx(
    docx_bytes: bytes,
    translations: Dict[str, str],
    index_to_target: Optional[Dict[int, str]] = None,
) -> bytes:
    """Replaces text in a DOCX document with translated text.
    Handles multi-paragraph table cells explicitly via cell.paragraphs,
    preserving list bullets, styles, runs, and document structure.
    If index_to_target is provided, maps each translatable paragraph 1-to-1 by document index.
    """
    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        seg_index = 0
        for p in iter_document_paragraphs(doc):
            source = p.text.strip()
            if not is_translatable(source):
                continue
            if index_to_target is not None and seg_index in index_to_target:
                update_paragraph_text(p, index_to_target[seg_index])
            elif source in translations:
                update_paragraph_text(p, translations[source])
            seg_index += 1

        out_buf = io.BytesIO()
        doc.save(out_buf)
        return out_buf.getvalue()

    except Exception as exc:
        log.warning("python-docx reconstruction failed (%s), falling back to XML stream replacement", exc)
        return _reconstruct_docx_xml_fallback(docx_bytes, translations, index_to_target=index_to_target)


def _reconstruct_docx_xml_fallback(
    docx_bytes: bytes,
    translations: Dict[str, str],
    index_to_target: Optional[Dict[int, str]] = None,
) -> bytes:
    """Fallback XML rebuilder for minimal non-OPC zip archives or corrupted packages."""
    in_buf = io.BytesIO(docx_bytes)
    out_buf = io.BytesIO()

    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                try:
                    tree = ET.fromstring(data)
                    seg_index = 0
                    for p in tree.iter(f"{{{W_NS}}}p"):
                        p_texts = [node.text for node in p.iter(f"{{{W_NS}}}t") if node.text]
                        combined = "".join(p_texts).strip()
                        if not is_translatable(combined):
                            continue
                        target = None
                        if index_to_target is not None and seg_index in index_to_target:
                            target = index_to_target[seg_index]
                        elif combined in translations:
                            target = translations[combined]
                        if target is not None:
                            t_nodes = list(p.iter(f"{{{W_NS}}}t"))
                            if t_nodes:
                                t_nodes[0].text = target
                                for remaining in t_nodes[1:]:
                                    remaining.text = ""
                        seg_index += 1
                    data = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
                except Exception as xml_exc:
                    log.warning("Fallback XML reconstruction warning: %s", xml_exc)
            zout.writestr(item, data)

    return out_buf.getvalue()
