"""DOCX Document Processor.
Extracts every structural element (headings, body paragraphs, bullet points, table cell paragraphs)
in strict, sequential XML document order with a detailed index map.
Ensures precise write-back into original XML nodes preserving formatting, list numbering, and layout integrity.
"""
from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Set, Tuple, Union

import docx
from docx.document import Document as DocxDocument
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

log = logging.getLogger("cat.doc_processor")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass
class DocumentSegment:
    """Represents an extracted translatable segment with strict structural indexing."""
    segment_index: int                # Strict sequential global index (0, 1, 2, ...)
    source_text: str                  # Stripped translatable text
    raw_text: str                     # Raw unstripped text
    element_type: str                 # "heading", "bullet", "table_cell", "paragraph"
    paragraph_index: int              # Sequential index of paragraph in body or cell
    table_index: Optional[int] = None # Table index in document (None if body)
    row_index: Optional[int] = None   # Row index in table (None if body)
    col_index: Optional[int] = None   # Column index in table (None if body)
    cell_index: Optional[int] = None  # Sequential cell index in table (None if body)
    cell_paragraph_index: Optional[int] = None # Paragraph index within the specific cell
    style_name: Optional[str] = None  # Paragraph style name (e.g. "Heading 1", "List Bullet")
    has_bullet: bool = False          # True if w:numPr list numbering is present
    metadata: Dict[str, Any] = field(default_factory=dict)


def is_translatable(text: str) -> bool:
    """Returns True if the text contains meaningful translatable content (letters or digits).
    Structural headings like 'Topic 1' or 'Introduction' return True.
    """
    stripped = text.strip()
    return bool(stripped and re.search(r"[\w\d]", stripped))


def detect_element_type(p: Paragraph, is_table: bool = False) -> Tuple[str, bool]:
    """Detects whether a paragraph is a heading, bullet item, table cell, or standard paragraph."""
    style_name = (p.style.name or "").lower() if p.style else ""
    # Check for OpenXML list numbering element (w:numPr)
    has_num_pr = False
    try:
        has_num_pr = bool(p._p.xpath("./w:pPr/w:numPr"))
    except Exception:
        pass

    has_bullet = has_num_pr or "bullet" in style_name or "list" in style_name

    if "heading" in style_name or "title" in style_name:
        return "heading", has_bullet
    if has_bullet:
        return "bullet", True
    if is_table:
        return "table_cell", has_bullet
    return "paragraph", False


def _iter_cell_paragraphs(
    cell: _Cell,
    doc: DocxDocument,
    table_idx: int,
    row_idx: int,
    col_idx: int,
    cell_idx: int,
) -> Iterator[Tuple[Paragraph, Dict[str, Any]]]:
    """Iterates through paragraphs and nested tables in a table cell in document order.
    Never flattens cell.paragraphs into a single string.
    """
    p_idx = 0
    for child in cell._tc.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            elem_type, has_bullet = detect_element_type(p, is_table=True)
            meta = {
                "element_type": elem_type,
                "paragraph_index": p_idx,
                "table_index": table_idx,
                "row_index": row_idx,
                "col_index": col_idx,
                "cell_index": cell_idx,
                "cell_paragraph_index": p_idx,
                "style_name": p.style.name if p.style else None,
                "has_bullet": has_bullet,
            }
            yield p, meta
            p_idx += 1
        elif tag == "tbl":
            # Nested table inside cell: recursively traverse in order
            nested_table = Table(child, doc)
            yield from _iter_table_elements(nested_table, doc, table_idx=table_idx)


def _iter_table_elements(
    table: Table,
    doc: DocxDocument,
    table_idx: int,
    source_col: Optional[int] = None,
) -> Iterator[Tuple[Paragraph, Dict[str, Any]]]:
    """Iterates through all cells and paragraphs in a table row-by-row and cell-by-cell.
    Deduplicates merged cells (_tc) so merged headers/cells are never processed twice.
    When `source_col` is set, only yields paragraphs from that specific column index
    (used for bilingual two-column extraction where only C0 is the source).
    """
    seen_cells: Set[Any] = set()
    cell_counter = 0

    for row_idx, row in enumerate(table.rows):
        for col_idx, cell in enumerate(row.cells):
            # Deduplicate horizontally or vertically merged cells
            if cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)
            cell_counter += 1

            # In bilingual mode, only extract from the designated source column
            if source_col is not None and col_idx != source_col:
                continue

            yield from _iter_cell_paragraphs(
                cell,
                doc,
                table_idx=table_idx,
                row_idx=row_idx,
                col_idx=col_idx,
                cell_idx=cell_counter - 1,
            )


def iter_document_elements(
    doc: DocxDocument,
    source_col: Optional[int] = None,
) -> Iterator[Tuple[Paragraph, Dict[str, Any]]]:
    """Traverses all document elements in strict, sequential XML document order.
    Interleaves body paragraphs (headings, bullet points, text) and tables exactly as they appear in the file.
    When `source_col` is set, table extraction is restricted to that column (bilingual mode).
    """
    body = doc._body._body
    table_counter = 0
    body_p_counter = 0

    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            elem_type, has_bullet = detect_element_type(p, is_table=False)
            meta = {
                "element_type": elem_type,
                "paragraph_index": body_p_counter,
                "table_index": None,
                "row_index": None,
                "col_index": None,
                "cell_index": None,
                "cell_paragraph_index": None,
                "style_name": p.style.name if p.style else None,
                "has_bullet": has_bullet,
            }
            yield p, meta
            body_p_counter += 1

        elif tag == "tbl":
            table = Table(child, doc)
            yield from _iter_table_elements(table, doc, table_idx=table_counter, source_col=source_col)
            table_counter += 1


def extract_indexed_segments(
    doc_or_bytes: Union[DocxDocument, bytes, BinaryIO],
    source_col: Optional[int] = None,
) -> List[DocumentSegment]:
    """Extracts every single translatable structural element—including headings,
    bullet points, and individual paragraphs inside table cells—with a strict,
    sequential index map (segment_index, paragraph_index, table_index, cell_index).

    Args:
        doc_or_bytes: DOCX document, raw bytes, or a file-like object.
        source_col: If set, only paragraphs in this table column index are extracted
                    (bilingual two-column mode where col 0 is English source).
    """
    if isinstance(doc_or_bytes, bytes):
        doc = docx.Document(io.BytesIO(doc_or_bytes))
    elif isinstance(doc_or_bytes, (io.BytesIO, io.BufferedReader)):
        doc = docx.Document(doc_or_bytes)
    else:
        doc = doc_or_bytes

    segments: List[DocumentSegment] = []
    global_index = 0

    for p, meta in iter_document_elements(doc, source_col=source_col):
        raw = p.text
        source = raw.strip()
        if is_translatable(source):
            seg = DocumentSegment(
                segment_index=global_index,
                source_text=source,
                raw_text=raw,
                element_type=meta["element_type"],
                paragraph_index=meta["paragraph_index"],
                table_index=meta["table_index"],
                row_index=meta["row_index"],
                col_index=meta["col_index"],
                cell_index=meta["cell_index"],
                cell_paragraph_index=meta["cell_paragraph_index"],
                style_name=meta["style_name"],
                has_bullet=meta["has_bullet"],
                metadata=meta,
            )
            segments.append(seg)
            global_index += 1

    return segments


def extract_segments_from_docx(
    doc_or_bytes: Union[DocxDocument, bytes, BinaryIO],
    source_col: Optional[int] = None,
) -> List[str]:
    """Extracts translatable text segments in exact sequential document order."""
    return [seg.source_text for seg in extract_indexed_segments(doc_or_bytes, source_col=source_col)]


def update_paragraph_text(p: Paragraph, translated_text: str) -> None:
    """Updates a paragraph's text while strictly preserving its formatting,
    run styles, and bullet list attributes (w:numPr).

    - Does NOT delete or replace the paragraph XML node (<w:p>) or properties (<w:pPr>).
    - Preserves bullet points, outline levels, indentation, and paragraph styles.
    - Updates the primary run to retain font, size, bold, italic, and color.
    - Clears trailing text runs without destroying inline drawings or images.
    """
    runs = p.runs
    if not runs:
        p.add_run(translated_text)
        return

    # Update text in the primary run to keep its run styling
    runs[0].text = translated_text

    # Clear secondary runs so old text doesn't trail, but preserve drawing objects
    for r in runs[1:]:
        has_drawing = bool(r._r.xpath("./w:drawing"))
        if not has_drawing:
            r.text = ""
        else:
            for t in r._r.xpath("./w:t"):
                t.text = ""


def _clone_paragraph_format(src_p: Paragraph, dst_p: Paragraph) -> None:
    """Clones paragraph-level formatting (w:pPr) from the source paragraph into
    the destination paragraph without overwriting its text content.
    Used to ensure C1 target paragraphs visually match C0 source paragraphs
    (indentation, numbering, list style, bold, etc.).
    """
    import copy

    src_xml = src_p._p
    dst_xml = dst_p._p

    # Clone paragraph properties (indentation, numbering, style, etc.)
    src_pPr = src_xml.find(f"{{{W_NS}}}pPr")
    if src_pPr is not None:
        dst_pPr = dst_xml.find(f"{{{W_NS}}}pPr")
        if dst_pPr is not None:
            dst_xml.remove(dst_pPr)
        dst_xml.insert(0, copy.deepcopy(src_pPr))


def reconstruct_docx_bilingual(
    docx_bytes: bytes,
    index_to_target: Dict[int, str],
    source_col: int = 0,
    target_col: int = 1,
) -> bytes:
    """Reconstructs a two-column bilingual DOCX document.

    For every row in every table:
    - Column `source_col` (C0) is kept **strictly unchanged** as the English source.
    - Translations from `index_to_target` are written **only into column `target_col`** (C1),
      paragraph-by-paragraph, synchronized 1:1 with C0 paragraph counts.
    - C1 paragraph count is synchronized to match C0 (extra paragraphs trimmed,
      missing paragraphs added with formatting cloned from the corresponding C0 paragraph).
    - Formatting (w:pPr, w:rPr) from each C0 paragraph is cloned into the matching C1 paragraph.

    Args:
        docx_bytes:      Raw DOCX bytes.
        index_to_target: Mapping of sequential segment index -> translated text.
                         Indexes are based on `extract_indexed_segments(source_col=source_col)`.
        source_col:      Column index of the source (English) column. Default 0.
        target_col:      Column index to write translations into. Default 1.

    Returns:
        Rebuilt DOCX bytes with translations in target column.
    """
    import copy

    doc = docx.Document(io.BytesIO(docx_bytes))
    seg_index = 0

    body = doc._body._body
    table_counter = 0

    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag != "tbl":
            continue

        table = Table(child, doc)
        seen_rows: Set[Any] = set()

        for row in table.rows:
            # Deduplicate vertically-merged row references
            if id(row) in seen_rows:
                continue
            seen_rows.add(id(row))

            num_cols = len(row.cells)
            if num_cols <= max(source_col, target_col):
                # Not a two-column row — skip
                continue

            src_cell = row.cells[source_col]
            tgt_cell = row.cells[target_col]

            # Skip if source and target are the same merged cell
            if src_cell._tc is tgt_cell._tc:
                continue

            src_paragraphs = src_cell.paragraphs
            tgt_paragraphs = tgt_cell.paragraphs

            # --- Synchronize C1 paragraph count to match C0 ---
            src_count = len(src_paragraphs)
            tgt_count = len(tgt_paragraphs)

            if src_count == 0:
                continue

            # Add missing paragraphs to C1 by cloning the last C1 paragraph
            while len(tgt_cell.paragraphs) < src_count:
                last_p = tgt_cell.paragraphs[-1]._p
                new_p = copy.deepcopy(last_p)
                # Clear text nodes in clone
                for t in new_p.iter(f"{{{W_NS}}}t"):
                    t.text = ""
                last_p.addnext(new_p)

            # Re-read after sync
            tgt_paragraphs = tgt_cell.paragraphs

            # Remove excess C1 paragraphs (keep at least 1)
            while len(tgt_cell.paragraphs) > src_count and len(tgt_cell.paragraphs) > 1:
                excess = tgt_cell.paragraphs[-1]
                tgt_cell._tc.remove(excess._p)

            tgt_paragraphs = tgt_cell.paragraphs

            # --- Write translations into each C1 paragraph ---
            for para_idx, src_p in enumerate(src_paragraphs):
                source_text = src_p.text.strip()
                if not is_translatable(source_text):
                    # Non-translatable paragraphs are NOT counted in extract_indexed_segments;
                    # do NOT increment seg_index here to keep indexes aligned.
                    continue

                target_text: Optional[str] = index_to_target.get(seg_index)
                seg_index += 1

                if target_text is None:
                    continue

                if para_idx < len(tgt_paragraphs):
                    tgt_p = tgt_paragraphs[para_idx]
                    # Clone paragraph formatting from C0 into C1
                    _clone_paragraph_format(src_p, tgt_p)
                    update_paragraph_text(tgt_p, target_text)

        table_counter += 1

    out_buf = io.BytesIO()
    doc.save(out_buf)
    return out_buf.getvalue()


def reconstruct_docx(
    docx_bytes: bytes,
    translations: Dict[str, str],
    index_to_target: Optional[Dict[int, str]] = None,
    bilingual_target_col: Optional[int] = None,
    bilingual_source_col: int = 0,
) -> bytes:
    """Replaces text in a DOCX document with translated text.
    Uses the strict sequential index map to map translations precisely back to their
    original XML nodes, ensuring formatting, bullet structures, and layout integrity are fully preserved.

    When `bilingual_target_col` is set, dispatches to `reconstruct_docx_bilingual` which
    writes translations only into the target column (e.g. C1), never touching C0 source text.
    """
    if bilingual_target_col is not None and index_to_target:
        try:
            return reconstruct_docx_bilingual(
                docx_bytes,
                index_to_target,
                source_col=bilingual_source_col,
                target_col=bilingual_target_col,
            )
        except Exception as exc:
            log.warning(
                "Bilingual reconstruction failed (%s), falling back to standard reconstruct.", exc
            )

    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        seg_index = 0

        for p, meta in iter_document_elements(doc):
            source = p.text.strip()
            if not is_translatable(source):
                continue

            target_text: Optional[str] = None
            # 1. Primary: strict sequential index map matching exact database segment rows
            if index_to_target is not None and seg_index in index_to_target:
                target_text = index_to_target[seg_index]
            # 2. Secondary: direct source text lookup fallback
            elif source in translations:
                target_text = translations[source]

            if target_text is not None:
                update_paragraph_text(p, target_text)

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
    """Fallback XML rebuilder for minimal non-OPC zip archives or corrupted packages.
    Iterates all <w:p> nodes in XML document order to maintain index synchronization.
    """
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


# Compatibility helpers for existing codebase
def iter_table_paragraphs(table: Table) -> Iterator[Paragraph]:
    """Yields all paragraphs from a table in row/cell order without flattening."""
    seen_cells: Set[Any] = set()
    for row in table.rows:
        for cell in row.cells:
            if cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)
            for p in cell.paragraphs:
                yield p
            for nested in cell.tables:
                yield from iter_table_paragraphs(nested)


def iter_document_paragraphs(doc: DocxDocument) -> Iterator[Paragraph]:
    """Yields paragraphs in true XML document order (interleaving body paragraphs and tables)."""
    for p, _meta in iter_document_elements(doc):
        yield p
