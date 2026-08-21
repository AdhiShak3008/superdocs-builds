"""
pdf_analyzer.py
Analyze a PDF and extract its visual grammar, layout, and content structure.

Responsibilities:
- Detect page dimensions and margins
- Detect column layout (single vs multi-column)
- Extract all text blocks with bounding boxes, fonts, reading order
- Detect non-content elements (figures, tables, images)
- Extract DocumentGrammar: the PDF's visual rules

Uses pdfplumber for reading. All coordinates are in PDF points (1/72 inch).
PDF coordinate origin is bottom-left; we normalise to top-left for consistency.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
import pdfplumber


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BBox:
    x0: float
    y0: float  # top (normalised)
    x1: float
    y1: float  # bottom (normalised)

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return abs(self.y1 - self.y0)

    @property
    def area(self):
        return self.width * self.height

    def as_tuple(self):
        return (self.x0, self.y0, self.x1, self.y1)

    def overlaps(self, other: "BBox") -> bool:
        self_y0  = min(self.y0, self.y1)
        self_y1  = max(self.y0, self.y1)
        other_y0 = min(other.y0, other.y1)
        other_y1 = max(other.y0, other.y1)
        return not (
            self.x1 <= other.x0 or other.x1 <= self.x0 or
            self_y1 <= other_y0 or other_y1 <= self_y0
        )


@dataclass
class TextBlock:
    text: str
    bbox: BBox
    page: int                  # 1-indexed
    column: int                # 0-indexed column within page
    fontname: str
    fontsize: float
    is_bold: bool
    is_heading: bool
    reading_order: int         # global across entire document
    line_height: float = 0.0
    paragraph_spacing: float = 0.0


@dataclass
class NonContentElement:
    """Figures, tables, images, headers, footers — never modified by patcher."""
    type: str                  # "figure", "table", "image", "header", "footer"
    page: int
    bbox: BBox
    anchor: str = "absolute"  # always absolute for MVP


@dataclass
class ColumnRegion:
    index: int
    x0: float
    x1: float
    width: float


@dataclass
class PageLayout:
    page_number: int           # 1-indexed
    width: float
    height: float
    columns: List[ColumnRegion]
    text_blocks: List[TextBlock]
    non_content: List[NonContentElement]
    margin_top: float
    margin_bottom: float
    margin_left: float
    margin_right: float


@dataclass
class DocumentGrammar:
    """
    The visual rules of the document — extracted once from the PDF.
    Used by the reflow engine to reproduce typography.
    """
    page_width: float
    page_height: float
    margin_top: float
    margin_bottom: float
    margin_left: float
    margin_right: float
    column_count: int
    column_regions: List[ColumnRegion]   # per-column geometry (consistent across pages)
    column_gap: float
    body_fontname: str
    body_fontsize: float
    body_line_height: float
    paragraph_spacing: float
    heading_styles: Dict[str, dict]      # level -> {fontname, fontsize, is_bold}


@dataclass
class AnalyzedDocument:
    path: str
    page_count: int
    grammar: DocumentGrammar
    pages: List[PageLayout]
    all_blocks: List[TextBlock]          # all blocks in reading order
    non_content: List[NonContentElement]


# ---------------------------------------------------------------------------
# Known section headings that may not have Roman numeral prefixes
# ---------------------------------------------------------------------------

KNOWN_HEADINGS = {
    "abstract", "introduction", "conclusion", "conclusions",
    "references", "acknowledgment", "acknowledgments", "acknowledgement",
    "acknowledgements", "related work", "background", "motivation",
    "overview", "discussion", "future work", "appendix",
}


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class PDFAnalyzer:
    """
    Analyze a PDF file and return a complete AnalyzedDocument.
    """

    def analyze(self, pdf_path: str) -> AnalyzedDocument:
        pages = []
        all_blocks = []
        all_non_content = []
        reading_order_counter = [0]

        with pdfplumber.open(pdf_path) as pdf:
            # Use first content page for grammar extraction
            grammar = self._extract_grammar(pdf)

            for page_obj in pdf.pages:
                page_num = page_obj.page_number  # 1-indexed
                pw = float(page_obj.width)
                ph = float(page_obj.height)

                columns = self._detect_columns(page_obj, grammar)
                margins = self._estimate_margins(page_obj, grammar)

                non_content = self._extract_non_content(page_obj, page_num, ph)
                blocks = self._extract_text_blocks(
                    page_obj, page_num, ph, columns,
                    grammar, reading_order_counter, non_content
                )

                layout = PageLayout(
                    page_number=page_num,
                    width=pw,
                    height=ph,
                    columns=columns,
                    text_blocks=blocks,
                    non_content=non_content,
                    margin_top=margins["top"],
                    margin_bottom=margins["bottom"],
                    margin_left=margins["left"],
                    margin_right=margins["right"],
                )
                pages.append(layout)
                all_blocks.extend(blocks)
                all_non_content.extend(non_content)

        return AnalyzedDocument(
            path=pdf_path,
            page_count=len(pages),
            grammar=grammar,
            pages=pages,
            all_blocks=all_blocks,
            non_content=all_non_content,
        )

    # ------------------------------------------------------------------
    # Grammar extraction
    # ------------------------------------------------------------------

    def _extract_grammar(self, pdf) -> DocumentGrammar:
        """
        Extract document-wide visual rules from the PDF.
        Uses the first content page (usually page 1 or 2).
        """
        # Gather font stats across all pages
        font_sizes = {}
        all_words = []

        for page_obj in pdf.pages[:min(4, len(pdf.pages))]:
            words = page_obj.extract_words(
                extra_attrs=["fontname", "size"],
                use_text_flow=True,
            )
            for w in words:
                size = round(float(w.get("size", 10)), 1)
                font_sizes[size] = font_sizes.get(size, 0) + 1
            all_words.extend(words)

        # Body font size = most common size
        body_fontsize = max(font_sizes, key=font_sizes.get) if font_sizes else 10.0

        # Body fontname = most common fontname at body size
        fontname_counts = {}
        for w in all_words:
            if round(float(w.get("size", 10)), 1) == body_fontsize:
                fn = w.get("fontname", "")
                fontname_counts[fn] = fontname_counts.get(fn, 0) + 1
        body_fontname = max(fontname_counts, key=fontname_counts.get) if fontname_counts else "Times-Roman"

        # Line height: median y-gap between consecutive words on same column
        line_height = body_fontsize * 1.2  # sensible default
        line_gaps = []
        for page_obj in pdf.pages[:2]:
            words = page_obj.extract_words(extra_attrs=["fontname", "size"])
            sorted_words = sorted(words, key=lambda w: (round(w["x0"] / 200), w["top"]))
            for i in range(1, len(sorted_words)):
                gap = sorted_words[i]["top"] - sorted_words[i - 1]["bottom"]
                if 0 < gap < body_fontsize * 3:
                    line_gaps.append(gap)
        if line_gaps:
            line_gaps.sort()
            line_height = body_fontsize + line_gaps[len(line_gaps) // 2]

        # Column detection on first page
        first_page = pdf.pages[0]
        pw = float(first_page.width)
        ph = float(first_page.height)

        col_regions, col_gap = self._detect_columns_from_words(
            first_page.extract_words(extra_attrs=["fontname", "size"]),
            pw, ph
        )

        # Margins: estimate from text extents
        margin_left = col_regions[0].x0 if col_regions else pw * 0.1
        margin_right = pw - col_regions[-1].x1 if col_regions else pw * 0.1

        # Paragraph spacing: approximately 1 line height
        paragraph_spacing = line_height * 0.8

        # Heading styles: collect fonts larger than body
        heading_styles = {}
        for page_obj in pdf.pages[:4]:
            words = page_obj.extract_words(extra_attrs=["fontname", "size"])
            for w in words:
                size = round(float(w.get("size", 10)), 1)
                fn = w.get("fontname", "")
                if size > body_fontsize + 1:
                    level = "h1" if size > body_fontsize + 3 else "h2"
                    if level not in heading_styles:
                        heading_styles[level] = {
                            "fontname": fn,
                            "fontsize": size,
                            "is_bold": "bold" in fn.lower() or "Bold" in fn,
                        }

        # Top/bottom margins from text extents
        all_tops = []
        all_bottoms = []
        for page_obj in pdf.pages[:2]:
            words = page_obj.extract_words()
            if words:
                all_tops.append(min(w["top"] for w in words))
                all_bottoms.append(max(w["bottom"] for w in words))
        margin_top = min(all_tops) if all_tops else ph * 0.1
        margin_bottom = ph - max(all_bottoms) if all_bottoms else ph * 0.1

        return DocumentGrammar(
            page_width=pw,
            page_height=ph,
            margin_top=margin_top,
            margin_bottom=margin_bottom,
            margin_left=margin_left,
            margin_right=margin_right,
            column_count=len(col_regions),
            column_regions=col_regions,
            column_gap=col_gap,
            body_fontname=body_fontname,
            body_fontsize=body_fontsize,
            body_line_height=line_height,
            paragraph_spacing=paragraph_spacing,
            heading_styles=heading_styles,
        )

    # ------------------------------------------------------------------
    # Column detection
    # ------------------------------------------------------------------

    def _detect_columns_from_words(
        self, words: list, page_width: float, page_height: float
    ) -> Tuple[List[ColumnRegion], float]:
        """
        Detect columns by building an x-coordinate histogram.
        Returns (column_regions, column_gap).
        """
        if not words:
            return [ColumnRegion(0, page_width * 0.08, page_width * 0.92, page_width * 0.84)], 0.0

        # Exclude header/footer zone (top 10% and bottom 10%)
        content_words = [
            w for w in words
            if page_height * 0.08 < w["top"] < page_height * 0.92
        ]
        if not content_words:
            content_words = words

        # Build x0 histogram in 5-point buckets
        bucket_size = 5.0
        hist = {}
        for w in content_words:
            bucket = int(w["x0"] / bucket_size) * bucket_size
            hist[bucket] = hist.get(bucket, 0) + 1

        if not hist:
            return [ColumnRegion(0, page_width * 0.08, page_width * 0.92, page_width * 0.84)], 0.0

        # Find the gap zone (low-density region between potential columns)
        max_x = max(hist.keys())
        min_x = min(hist.keys())
        total_range = max_x - min_x

        if total_range < page_width * 0.3:
            # Looks single-column
            x0 = min(w["x0"] for w in content_words)
            x1 = max(w["x1"] for w in content_words)
            return [ColumnRegion(0, x0, x1, x1 - x0)], 0.0

        # Look for a zero-density gap in the middle portion of the page.
        # Real two-column layout has a physical gutter with NO text x0 values.
        mid_start = page_width * 0.25
        mid_end = page_width * 0.75
        occupied_xs = sorted(k for k in hist.keys() if mid_start <= k <= mid_end)

        # Find longest run of empty buckets (gap) in the middle zone
        if occupied_xs:
            best_gap_center = None
            best_gap_width = 0
            prev_x = occupied_xs[0]
            for x in occupied_xs[1:]:
                gap_w = x - prev_x
                if gap_w > best_gap_width:
                    best_gap_width = gap_w
                    best_gap_center = (prev_x + x) / 2
                prev_x = x

            # Only split if there is a meaningful gap (> 10% of page width)
            if best_gap_center and best_gap_width > page_width * 0.10:
                # Two-column: gap found in middle
                gap_center = best_gap_center

                left_words = [w for w in content_words if w["x1"] <= gap_center]
                right_words = [w for w in content_words if w["x0"] >= gap_center]

                if left_words and right_words:
                    left_x0 = min(w["x0"] for w in left_words)
                    left_x1 = max(w["x1"] for w in left_words)
                    right_x0 = min(w["x0"] for w in right_words)
                    right_x1 = max(w["x1"] for w in right_words)
                    col_gap = right_x0 - left_x1

                    return [
                        ColumnRegion(0, left_x0, left_x1, left_x1 - left_x0),
                        ColumnRegion(1, right_x0, right_x1, right_x1 - right_x0),
                    ], col_gap

        # Default: single column
        x0 = min(w["x0"] for w in content_words)
        x1 = max(w["x1"] for w in content_words)
        return [ColumnRegion(0, x0, x1, x1 - x0)], 0.0

    def _detect_columns(self, page_obj, grammar: DocumentGrammar) -> List[ColumnRegion]:
        """Use grammar's column regions (consistent across pages)."""
        return grammar.column_regions

    def _estimate_margins(self, page_obj, grammar: DocumentGrammar) -> dict:
        return {
            "top": grammar.margin_top,
            "bottom": grammar.margin_bottom,
            "left": grammar.margin_left,
            "right": grammar.margin_right,
        }

    # ------------------------------------------------------------------
    # Non-content element extraction
    # ------------------------------------------------------------------

    def _extract_non_content(
        self, page_obj, page_num: int, page_height: float
    ) -> List[NonContentElement]:
        elements = []

        # Images
        for img in page_obj.images:
            bbox = BBox(
                x0=float(img["x0"]),
                y0=float(img["top"]),
                x1=float(img["x1"]),
                y1=float(img["bottom"]),
            )
            elements.append(NonContentElement(
                type="image",
                page=page_num,
                bbox=bbox,
            ))

        # Tables
        for table in page_obj.find_tables():
            bbox_raw = table.bbox  # (x0, top, x1, bottom)
            bbox = BBox(
                x0=float(bbox_raw[0]),
                y0=float(bbox_raw[1]),
                x1=float(bbox_raw[2]),
                y1=float(bbox_raw[3]),
            )
            elements.append(NonContentElement(
                type="table",
                page=page_num,
                bbox=bbox,
            ))

        return elements

    # ------------------------------------------------------------------
    # Text block extraction
    # ------------------------------------------------------------------

    def _extract_text_blocks(
        self,
        page_obj,
        page_num: int,
        page_height: float,
        columns: List[ColumnRegion],
        grammar: DocumentGrammar,
        reading_order_counter: list,
        non_content: List[NonContentElement],
    ) -> List[TextBlock]:
        """
        Extract text blocks grouped by paragraph-level proximity.
        Each block has its bounding box, font info, column, and reading order.
        """
        words = page_obj.extract_words(
            extra_attrs=["fontname", "size"],
            use_text_flow=True,
            keep_blank_chars=False,
        )
        if not words:
            return []

        # Group words into lines (same y-position ± tolerance)
        lines = self._group_words_into_lines(words, tolerance=3.0)

        # Group lines into paragraphs (gap > 1.5x line height = new paragraph)
        paragraphs = self._group_lines_into_paragraphs(lines, grammar.body_line_height)

        blocks = []
        for para in paragraphs:
            if not para:
                continue

            all_words_in_para = [w for line in para for w in line]
            text = " ".join(w["text"] for w in all_words_in_para)
            text = text.strip()
            if not text:
                continue

            x0 = min(w["x0"] for w in all_words_in_para)
            y0 = min(w["top"] for w in all_words_in_para)
            x1 = max(w["x1"] for w in all_words_in_para)
            y1 = max(w["bottom"] for w in all_words_in_para)

            bbox = BBox(x0=x0, y0=y0, x1=x1, y1=y1)

            # Skip if entirely overlapping a non-content element
            if self._overlaps_non_content(bbox, non_content):
                continue

            # Font info from most common font in paragraph
            fontnames = [w.get("fontname", "") for w in all_words_in_para]
            sizes = [float(w.get("size", grammar.body_fontsize)) for w in all_words_in_para]
            fontname = max(set(fontnames), key=fontnames.count)
            fontsize = round(sum(sizes) / len(sizes), 1)

            is_bold = "bold" in fontname.lower() or "Bold" in fontname
            is_heading = self._classify_heading(text, fontsize, is_bold, grammar)

            col = self._assign_column(x0, x1, columns)

            block = TextBlock(
                text=text,
                bbox=bbox,
                page=page_num,
                column=col,
                fontname=fontname,
                fontsize=fontsize,
                is_bold=is_bold,
                is_heading=is_heading,
                reading_order=reading_order_counter[0],
                line_height=grammar.body_line_height,
            )
            reading_order_counter[0] += 1
            blocks.append(block)

        # Sort blocks by reading order: column-major (left col first, then right)
        blocks.sort(key=lambda b: (b.column, b.bbox.y0))

        # Re-assign reading order after sorting
        for i, block in enumerate(blocks):
            block.reading_order = reading_order_counter[0] - len(blocks) + i

        return blocks

    def _group_words_into_lines(self, words: list, tolerance: float = 3.0) -> list:
        """Group words that share approximately the same baseline."""
        if not words:
            return []
        sorted_words = sorted(words, key=lambda w: w["top"])
        lines = []
        current_line = [sorted_words[0]]
        current_top = sorted_words[0]["top"]

        for word in sorted_words[1:]:
            if abs(word["top"] - current_top) <= tolerance:
                current_line.append(word)
            else:
                lines.append(sorted(current_line, key=lambda w: w["x0"]))
                current_line = [word]
                current_top = word["top"]
        if current_line:
            lines.append(sorted(current_line, key=lambda w: w["x0"]))
        return lines

    def _group_lines_into_paragraphs(self, lines: list, line_height: float) -> list:
        """Group consecutive lines into paragraphs based on vertical gap."""
        if not lines:
            return []
        paragraphs = []
        current_para = [lines[0]]
        threshold = line_height * 1.6

        for line in lines[1:]:
            prev_bottom = max(w["bottom"] for w in current_para[-1])
            curr_top = min(w["top"] for w in line)
            gap = curr_top - prev_bottom
            if gap > threshold:
                paragraphs.append(current_para)
                current_para = [line]
            else:
                current_para.append(line)
        if current_para:
            paragraphs.append(current_para)
        return paragraphs

    def _classify_heading(
        self, text: str, fontsize: float, is_bold: bool, grammar: DocumentGrammar
    ) -> bool:
        """Determine if a text block is a section heading."""
        text_stripped = text.strip()
        text_lower = text_stripped.lower()

        # Roman numeral prefix: I. II. III. IV. V. etc.
        if re.match(r'^[IVX]+\.\s+\S', text_stripped):
            return True

        # Known heading words
        if text_lower in KNOWN_HEADINGS:
            return True
        for kh in KNOWN_HEADINGS:
            if text_lower.startswith(kh + " ") or text_lower.startswith(kh + "\n"):
                return True

        # Larger than body text and bold
        if fontsize > grammar.body_fontsize + 1.5 and is_bold:
            return True

        # ALL CAPS short text (likely a heading)
        if text_stripped.isupper() and len(text_stripped.split()) <= 8 and len(text_stripped) > 2:
            return True

        return False

    def _assign_column(self, x0: float, x1: float, columns: List[ColumnRegion]) -> int:
        """Assign a text block to a column based on its x0 position."""
        center = (x0 + x1) / 2
        best_col = 0
        best_dist = float("inf")
        for col in columns:
            col_center = (col.x0 + col.x1) / 2
            dist = abs(center - col_center)
            if dist < best_dist:
                best_dist = dist
                best_col = col.index
        return best_col

    def _overlaps_non_content(
        self, bbox: BBox, non_content: List[NonContentElement]
    ) -> bool:
        """Return True if bbox substantially overlaps a non-content element."""
        for elem in non_content:
            if bbox.overlaps(elem.bbox):
                overlap_x = max(0, min(bbox.x1, elem.bbox.x1) - max(bbox.x0, elem.bbox.x0))
                overlap_y = max(0, min(bbox.y1, elem.bbox.y1) - max(bbox.y0, elem.bbox.y0))
                overlap_area = overlap_x * overlap_y
                if overlap_area > bbox.area * 0.5:
                    return True
        return False


def analyze_pdf(pdf_path: str) -> AnalyzedDocument:
    """Convenience function."""
    return PDFAnalyzer().analyze(pdf_path)
