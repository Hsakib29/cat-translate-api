"""Backward-compatibility wrapper for app.doc_processor.
All core document parsing and reconstruction logic lives in app.doc_processor.
"""
from __future__ import annotations

from .doc_processor import (
    DocumentSegment,
    detect_element_type,
    extract_indexed_segments,
    extract_segments_from_docx,
    is_bilingual_table,
    is_translatable,
    iter_document_elements,
    iter_document_paragraphs,
    iter_table_paragraphs,
    reconstruct_docx,
    update_paragraph_text,
)

__all__ = [
    "DocumentSegment",
    "detect_element_type",
    "extract_indexed_segments",
    "extract_segments_from_docx",
    "is_bilingual_table",
    "is_translatable",
    "iter_document_elements",
    "iter_document_paragraphs",
    "iter_table_paragraphs",
    "reconstruct_docx",
    "update_paragraph_text",
]
