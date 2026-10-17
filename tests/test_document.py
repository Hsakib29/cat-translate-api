"""Tests for app.doc_processor and app.document modules.
Verifies:
1. Strict, sequential XML document order extraction: structural headings ("Topic X"),
   body paragraphs, and multi-paragraph table cells are interleaved correctly without index drift.
2. Every paragraph inside table cells (cell.paragraphs) is extracted with detailed metadata
   (paragraph_index, table_index, row_index, col_index, cell_paragraph_index).
3. Safe write-back precisely targets original XML nodes via index_to_target mapping,
   preserving bullet formatting (w:numPr), styles, and preventing mixed-language lines.
4. Merged cell deduplication and nested table recursive traversal.
"""
import io
import unittest

import docx
from docx.shared import Pt, RGBColor
from app.doc_processor import (
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
    reconstruct_docx_bilingual,
    update_paragraph_text,
)


class TestDocumentProcessing(unittest.TestCase):
    def _create_sample_docx_with_multi_paragraph_table(self) -> bytes:
        doc = docx.Document()
        doc.add_heading("Document Title", level=1)
        doc.add_paragraph("This is a lead body paragraph.")

        # Create a 2x2 table
        table = doc.add_table(rows=2, cols=2)

        # Cell (0, 0): Normal single-paragraph cell
        cell_00 = table.cell(0, 0)
        cell_00.paragraphs[0].text = "Header 1"

        # Cell (0, 1): Normal single-paragraph cell
        cell_01 = table.cell(0, 1)
        cell_01.paragraphs[0].text = "Header 2"

        # Cell (1, 0): Simple cell
        cell_10 = table.cell(1, 0)
        cell_10.paragraphs[0].text = "Feature item"

        # Cell (1, 1): Multi-paragraph cell with bullet points and bold/italic styles
        cell_11 = table.cell(1, 1)
        p1 = cell_11.paragraphs[0]
        p1.style = "List Bullet"
        r1 = p1.add_run("Bullet point A: High performance")
        r1.bold = True

        p2 = cell_11.add_paragraph(style="List Bullet")
        r2 = p2.add_run("Bullet point B: Robust error isolation")
        r2.italic = True

        p3 = cell_11.add_paragraph(style="List Bullet")
        r3 = p3.add_run("Bullet point C: Automated retry")

        # Heading and paragraph AFTER the table
        doc.add_heading("Topic 2: Advanced Processing", level=1)
        doc.add_paragraph("Concluding body paragraph after table.")

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def test_multi_paragraph_cells_not_skipped_or_flattened(self):
        """Verifies explicitly iterating cell.paragraphs yields all paragraphs without flattening."""
        docx_bytes = self._create_sample_docx_with_multi_paragraph_table()
        doc = docx.Document(io.BytesIO(docx_bytes))
        table = doc.tables[0]

        table_paras = list(iter_table_paragraphs(table))
        self.assertEqual(len(table_paras), 6)

        texts = [p.text for p in table_paras]
        self.assertIn("Bullet point A: High performance", texts)
        self.assertIn("Bullet point B: Robust error isolation", texts)
        self.assertIn("Bullet point C: Automated retry", texts)

    def test_strict_interleaved_document_order(self):
        """CRITICAL: Headings, body paragraphs, and table cells must be extracted in true XML document order.
        Headings after tables ('Topic 2') must never be desynchronized or swapped with table cells.
        """
        docx_bytes = self._create_sample_docx_with_multi_paragraph_table()
        indexed_segments = extract_indexed_segments(docx_bytes)

        # Expected 10 segments in strict document sequence:
        # 0: Heading: "Document Title"
        # 1: Body: "This is a lead body paragraph."
        # 2: Table Cell (0,0): "Header 1"
        # 3: Table Cell (0,1): "Header 2"
        # 4: Table Cell (1,0): "Feature item"
        # 5: Table Cell (1,1,0): "Bullet point A: High performance"
        # 6: Table Cell (1,1,1): "Bullet point B: Robust error isolation"
        # 7: Table Cell (1,1,2): "Bullet point C: Automated retry"
        # 8: Heading: "Topic 2: Advanced Processing"  <-- Must come AFTER table!
        # 9: Body: "Concluding body paragraph after table."
        self.assertEqual(len(indexed_segments), 10)

        # Verify sequential indices
        for expected_idx, seg in enumerate(indexed_segments):
            self.assertEqual(seg.segment_index, expected_idx)

        # Verify element types and positions
        self.assertEqual(indexed_segments[0].source_text, "Document Title")
        self.assertEqual(indexed_segments[0].element_type, "heading")
        self.assertIsNone(indexed_segments[0].table_index)

        self.assertEqual(indexed_segments[1].source_text, "This is a lead body paragraph.")
        self.assertEqual(indexed_segments[1].element_type, "paragraph")

        self.assertEqual(indexed_segments[2].source_text, "Header 1")
        self.assertEqual(indexed_segments[2].table_index, 0)
        self.assertEqual(indexed_segments[2].row_index, 0)
        self.assertEqual(indexed_segments[2].col_index, 0)

        self.assertEqual(indexed_segments[5].source_text, "Bullet point A: High performance")
        self.assertEqual(indexed_segments[5].cell_paragraph_index, 0)
        self.assertTrue(indexed_segments[5].has_bullet)

        self.assertEqual(indexed_segments[6].source_text, "Bullet point B: Robust error isolation")
        self.assertEqual(indexed_segments[6].cell_paragraph_index, 1)

        self.assertEqual(indexed_segments[7].source_text, "Bullet point C: Automated retry")
        self.assertEqual(indexed_segments[7].cell_paragraph_index, 2)

        # Heading AFTER table must be at index 8 with table_index=None
        self.assertEqual(indexed_segments[8].source_text, "Topic 2: Advanced Processing")
        self.assertEqual(indexed_segments[8].element_type, "heading")
        self.assertIsNone(indexed_segments[8].table_index)

        self.assertEqual(indexed_segments[9].source_text, "Concluding body paragraph after table.")
        self.assertIsNone(indexed_segments[9].table_index)

    def test_extract_segments_from_docx_individual_segments(self):
        """extract_segments_from_docx returns strings in exact sequential order."""
        docx_bytes = self._create_sample_docx_with_multi_paragraph_table()
        segments = extract_segments_from_docx(docx_bytes)

        self.assertEqual(len(segments), 10)
        self.assertEqual(segments[0], "Document Title")
        self.assertEqual(segments[1], "This is a lead body paragraph.")
        self.assertEqual(segments[2], "Header 1")
        self.assertEqual(segments[5], "Bullet point A: High performance")
        self.assertEqual(segments[7], "Bullet point C: Automated retry")
        self.assertEqual(segments[8], "Topic 2: Advanced Processing")
        self.assertEqual(segments[9], "Concluding body paragraph after table.")

    def test_safe_write_back_with_index_map(self):
        """Precise write-back using index_to_target guarantees zero desync, zero mixed-language lines."""
        docx_bytes = self._create_sample_docx_with_multi_paragraph_table()

        # Map each index 0..9 to a distinct Bengali translation
        index_translations = {
            0: "নথির শিরোনাম",
            1: "এটি একটি প্রাথমিক মূল অনুচ্ছেদ।",
            2: "শিরোনাম ১",
            3: "শিরোনাম ২",
            4: "বৈশিষ্ট্য উপাদান",
            5: "বুলেট ক: উচ্চ কর্মক্ষমতা",
            6: "বুলেট খ: ত্রুটি বিচ্ছিন্নতা",
            7: "বুলেট গ: স্বয়ংক্রিয় পুনঃচেষ্টা",
            8: "বিষয় ২: উন্নত প্রক্রিয়াকরণ",
            9: "টেবিলের পরে চূড়ান্ত অনুচ্ছেদ।",
        }

        reconstructed_bytes = reconstruct_docx(docx_bytes, translations={}, index_to_target=index_translations)
        reconstructed_doc = docx.Document(io.BytesIO(reconstructed_bytes))

        reconstructed_segments = extract_segments_from_docx(reconstructed_doc)
        self.assertEqual(len(reconstructed_segments), 10)

        # Verify Heading 1
        self.assertEqual(reconstructed_segments[0], "নথির শিরোনাম")
        # Verify Table cells
        self.assertEqual(reconstructed_segments[2], "শিরোনাম ১")
        self.assertEqual(reconstructed_segments[5], "বুলেট ক: উচ্চ কর্মক্ষমতা")
        self.assertEqual(reconstructed_segments[6], "বুলেট খ: ত্রুটি বিচ্ছিন্নতা")
        self.assertEqual(reconstructed_segments[7], "বুলেট গ: স্বয়ংক্রিয় পুনঃচেষ্টা")
        # Verify Heading 2 AFTER table is correctly translated (not swapped!)
        self.assertEqual(reconstructed_segments[8], "বিষয় ২: উন্নত প্রক্রিয়াকরণ")
        self.assertEqual(reconstructed_segments[9], "টেবিলের পরে চূড়ান্ত অনুচ্ছেদ।")

        # Verify formatting integrity on multi-paragraph cell
        multi_cell = reconstructed_doc.tables[0].cell(1, 1)
        self.assertEqual(len(multi_cell.paragraphs), 3)
        self.assertTrue(multi_cell.paragraphs[0].runs[0].bold)
        self.assertTrue(multi_cell.paragraphs[1].runs[0].italic)
        self.assertEqual(multi_cell.paragraphs[0].style.name, "List Bullet")

    def test_update_paragraph_text_preserves_formatting_and_runs(self):
        """Updating paragraph text retains run styling (bold, italic) and paragraph style."""
        doc = docx.Document()
        p = doc.add_paragraph(style="List Bullet")
        r = p.add_run("Original text")
        r.bold = True

        update_paragraph_text(p, "Translated text")

        self.assertEqual(p.text, "Translated text")
        self.assertEqual(p.style.name, "List Bullet")
        self.assertTrue(p.runs[0].bold)

    def test_merged_cells_deduplication(self):
        """Merged cells are not processed repeatedly."""
        doc = docx.Document()
        table = doc.add_table(rows=2, cols=2)
        cell_00 = table.cell(0, 0)
        cell_01 = table.cell(0, 1)
        cell_00.merge(cell_01)
        cell_00.paragraphs[0].text = "Merged Header"

        table.cell(1, 0).paragraphs[0].text = "Row 2 Col 1"
        table.cell(1, 1).paragraphs[0].text = "Row 2 Col 2"

        paras = list(iter_table_paragraphs(table))
        merged_count = sum(1 for p in paras if p.text == "Merged Header")
        self.assertEqual(merged_count, 1)
        self.assertEqual(len(paras), 3)

    def test_nested_tables_traversal(self):
        """Nested tables within cells are recursively processed in order."""
        doc = docx.Document()
        outer_table = doc.add_table(rows=1, cols=1)
        cell = outer_table.cell(0, 0)
        cell.paragraphs[0].text = "Outer Cell Para"

        nested_table = cell.add_table(rows=1, cols=1)
        nested_cell = nested_table.cell(0, 0)
        nested_cell.paragraphs[0].text = "Nested Cell Para 1"
        nested_cell.add_paragraph("Nested Cell Para 2")

        paras = list(iter_table_paragraphs(outer_table))
        texts = [p.text for p in paras if p.text.strip()]
        self.assertEqual(texts, ["Outer Cell Para", "Nested Cell Para 1", "Nested Cell Para 2"])

    def test_is_bilingual_table_detection(self):
        """Verifies accurate detection of bilingual tables (>=2 rows, >=50% target cells empty)."""
        doc = docx.Document()
        # Table 1: Standard bilingual table with empty column 1
        t1 = doc.add_table(rows=3, cols=2)
        t1.cell(0, 0).paragraphs[0].text = "Header English"
        t1.cell(0, 1).paragraphs[0].text = ""
        t1.cell(1, 0).paragraphs[0].text = "Section 1 English"
        t1.cell(1, 1).paragraphs[0].text = ""
        t1.cell(2, 0).paragraphs[0].text = "Section 2 English"
        t1.cell(2, 1).paragraphs[0].text = ""
        self.assertTrue(is_bilingual_table(t1, 0, 1))

        # Table 2: Monolingual / already populated table
        t2 = doc.add_table(rows=3, cols=2)
        t2.cell(0, 0).paragraphs[0].text = "Col A"
        t2.cell(0, 1).paragraphs[0].text = "Col B"
        t2.cell(1, 0).paragraphs[0].text = "Data 1"
        t2.cell(1, 1).paragraphs[0].text = "Data 2"
        t2.cell(2, 0).paragraphs[0].text = "Data 3"
        t2.cell(2, 1).paragraphs[0].text = "Data 4"
        self.assertFalse(is_bilingual_table(t2, 0, 1))

        # Table 3: Single-row table cannot be bilingual
        t3 = doc.add_table(rows=1, cols=2)
        t3.cell(0, 0).paragraphs[0].text = "English"
        t3.cell(0, 1).paragraphs[0].text = ""
        self.assertFalse(is_bilingual_table(t3, 0, 1))

    def test_reconstruct_docx_bilingual_preserves_c0_and_clones_run_styles(self):
        """Verifies reconstruct_docx_bilingual:
        1. Leaves Column 0 (English) completely intact.
        2. Populates Column 1 (Target) with translated text.
        3. Clones run styling (bold, italic, font size, color) from C0 to C1.
        4. Preserves multiline strings using <w:br/> line breaks.
        5. Localizes digits to Bengali numerals.
        """
        doc = docx.Document()
        table = doc.add_table(rows=2, cols=2)

        # Row 0, Cell 0: Styled run with bold and font size
        p0 = table.cell(0, 0).paragraphs[0]
        r0 = p0.add_run("Executive Summary")
        r0.bold = True
        r0.font.name = "Calibri"
        r0.font.size = Pt(14)
        r0.font.color.rgb = RGBColor(31, 78, 121)

        # Row 0, Cell 1: Empty target
        table.cell(0, 1).paragraphs[0].text = ""

        # Row 1, Cell 0: Multiline source text
        p1 = table.cell(1, 0).paragraphs[0]
        r1 = p1.add_run("Paragraph 1\nParagraph 2 with 2026 data")
        r1.italic = True

        # Row 1, Cell 1: Empty target
        table.cell(1, 1).paragraphs[0].text = ""

        buf = io.BytesIO()
        doc.save(buf)
        docx_bytes = buf.getvalue()

        # Reconstruct with bilingual writer
        index_to_target = {
            0: "নির্বাহী সারসংক্ষেপ",
            1: "অনুচ্ছেদ ১\nঅনুচ্ছেদ ২ এবং ২০২৬ এর তথ্য",
        }
        rebuilt_bytes = reconstruct_docx_bilingual(
            docx_bytes,
            index_to_target=index_to_target,
            source_col=0,
            target_col=1,
            tl="bn",
        )

        rebuilt_doc = docx.Document(io.BytesIO(rebuilt_bytes))
        rebuilt_table = rebuilt_doc.tables[0]

        # Verify C0 remains unmodified
        self.assertEqual(rebuilt_table.cell(0, 0).text, "Executive Summary")
        self.assertEqual(rebuilt_table.cell(1, 0).text, "Paragraph 1\nParagraph 2 with 2026 data")

        # Verify C1 translations
        self.assertEqual(rebuilt_table.cell(0, 1).text, "নির্বাহী সারসংক্ষেপ")
        self.assertEqual(
            rebuilt_table.cell(1, 1).text,
            "অনুচ্ছেদ ১\nঅনুচ্ছেদ ২ এবং ২০২৬ এর তথ্য",
        )

        # Verify C1 run styles cloned from C0
        tgt_run0 = rebuilt_table.cell(0, 1).paragraphs[0].runs[0]
        self.assertTrue(tgt_run0.bold)
        self.assertEqual(tgt_run0.font.size, Pt(14))
        self.assertEqual(tgt_run0.font.color.rgb, RGBColor(31, 78, 121))

        # Verify C1 line break tag <w:br/>
        tgt_r1 = rebuilt_table.cell(1, 1).paragraphs[0].runs[0]._r
        self.assertGreaterEqual(len(tgt_r1.xpath("./w:br")), 1)

    def test_reconstruct_docx_auto_detects_bilingual(self):
        """reconstruct_docx automatically routes to bilingual reconstruction when an empty C1 table is detected."""
        doc = docx.Document()
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).paragraphs[0].text = "Topic Introduction"
        table.cell(0, 1).paragraphs[0].text = ""
        table.cell(1, 0).paragraphs[0].text = "Detailed Analysis"
        table.cell(1, 1).paragraphs[0].text = ""

        buf = io.BytesIO()
        doc.save(buf)
        docx_bytes = buf.getvalue()

        rebuilt_bytes = reconstruct_docx(
            docx_bytes,
            translations={},
            index_to_target={
                0: "বিষয়ের ভূমিকা",
                1: "বিস্তারিত বিশ্লেষণ",
            },
            tl="bn",
        )

        rebuilt_doc = docx.Document(io.BytesIO(rebuilt_bytes))
        rebuilt_table = rebuilt_doc.tables[0]
        self.assertEqual(rebuilt_table.cell(0, 0).text, "Topic Introduction")
        self.assertEqual(rebuilt_table.cell(0, 1).text, "বিষয়ের ভূমিকা")
        self.assertEqual(rebuilt_table.cell(1, 0).text, "Detailed Analysis")
        self.assertEqual(rebuilt_table.cell(1, 1).text, "বিস্তারিত বিশ্লেষণ")


if __name__ == "__main__":
    unittest.main()
