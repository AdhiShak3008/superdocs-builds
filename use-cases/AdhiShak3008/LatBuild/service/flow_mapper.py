"""
flow_mapper.py
Build the DocumentFlowMap from an AnalyzedDocument.

Responsibilities:
- Detect section boundaries from heading blocks
- Group content blocks into sections
- Build FlowRegions (physical containers) for each section
- Establish reading order across pages and columns
- Produce DocumentFlowMap: the single source of truth for the entire document

The DocumentFlowMap is the foundation of everything downstream.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from pdf_analyzer import (
    AnalyzedDocument, TextBlock, NonContentElement,
    BBox, ColumnRegion, DocumentGrammar, KNOWN_HEADINGS
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FlowRegion:
    """
    A physical container on a specific page/column through which content flows.
    """
    page: int
    column: int
    bbox: BBox
    column_width: float

    def capacity_lines(self, line_height: float) -> int:
        """Approximate number of lines that fit in this region."""
        return max(1, int(self.bbox.height / line_height))

    def capacity_chars(self, line_height: float, chars_per_line: int) -> int:
        return self.capacity_lines(line_height) * chars_per_line


@dataclass
class Section:
    """
    A semantic section of the document.
    Contains an ordered list of text blocks and the flow regions they occupy.
    """
    name: str                              # e.g. "III. METHODOLOGY"
    heading_block: Optional[TextBlock]     # the heading block itself
    content_blocks: List[TextBlock]        # ordered content blocks (not heading)
    flow_regions: List[FlowRegion]         # ordered physical regions
    is_editable: bool = True               # References = False by default

    @property
    def full_text(self) -> str:
        return "\n\n".join(b.text for b in self.content_blocks).strip()

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    @property
    def pages(self) -> List[int]:
        return sorted(set(b.page for b in self.content_blocks))

    @property
    def reading_start(self) -> int:
        if self.heading_block:
            return self.heading_block.reading_order
        if self.content_blocks:
            return self.content_blocks[0].reading_order
        return 0

    @property
    def reading_end(self) -> int:
        if self.content_blocks:
            return self.content_blocks[-1].reading_order
        return self.reading_start


@dataclass
class DocumentFlowMap:
    """
    The complete semantic + physical map of the document.
    Built once at upload time. Used by all downstream components.
    """
    grammar: DocumentGrammar
    sections: List[Section]               # in reading order
    all_blocks: List[TextBlock]           # all blocks, reading order
    non_content: List[NonContentElement]  # anchored elements — never modified

    def get_section(self, name: str) -> Optional[Section]:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def sections_after(self, section: Section) -> List[Section]:
        """All sections that come after the given section in reading order."""
        found = False
        result = []
        for s in self.sections:
            if found:
                result.append(s)
            if s.name == section.name:
                found = True
        return result

    def editable_sections(self) -> List[Section]:
        return [s for s in self.sections if s.is_editable]


# ---------------------------------------------------------------------------
# Flow Mapper
# ---------------------------------------------------------------------------

NON_EDITABLE_SECTIONS = {
    "references", "bibliography",
}


class FlowMapper:
    """
    Takes an AnalyzedDocument and produces a DocumentFlowMap.
    """

    def build(self, doc: AnalyzedDocument) -> DocumentFlowMap:
        all_blocks = doc.all_blocks

        # Step 1: Identify heading blocks and section boundaries
        sections = self._detect_sections(all_blocks, doc.grammar)

        # Step 2: Build flow regions for each section
        for section in sections:
            section.flow_regions = self._build_flow_regions(
                section, doc.grammar
            )

        return DocumentFlowMap(
            grammar=doc.grammar,
            sections=sections,
            all_blocks=all_blocks,
            non_content=doc.non_content,
        )

    # ------------------------------------------------------------------
    # Section detection
    # ------------------------------------------------------------------

    def _detect_sections(
        self, all_blocks: List[TextBlock], grammar: DocumentGrammar
    ) -> List[Section]:
        """
        Find section boundaries by locating heading blocks.
        Everything between two consecutive headings belongs to the first section.
        """
        if not all_blocks:
            return []

        # Identify heading blocks
        heading_indices = [
            i for i, b in enumerate(all_blocks) if b.is_heading
        ]

        if not heading_indices:
            # No headings detected — treat entire document as one section
            return [Section(
                name="Document",
                heading_block=None,
                content_blocks=all_blocks,
                flow_regions=[],
                is_editable=True,
            )]

        sections = []

        # Content before first heading (e.g. title, authors) — not editable
        if heading_indices[0] > 0:
            pre_blocks = all_blocks[:heading_indices[0]]
            sections.append(Section(
                name="Title / Preamble",
                heading_block=None,
                content_blocks=pre_blocks,
                flow_regions=[],
                is_editable=False,
            ))

        for idx, heading_idx in enumerate(heading_indices):
            heading_block = all_blocks[heading_idx]
            section_name = self._normalise_section_name(heading_block.text)

            # Content blocks: from after this heading to before next heading
            next_heading_idx = (
                heading_indices[idx + 1]
                if idx + 1 < len(heading_indices)
                else len(all_blocks)
            )
            content_blocks = all_blocks[heading_idx + 1:next_heading_idx]

            # Filter out blocks that are clearly captions or labels
            content_blocks = [
                b for b in content_blocks
                if not self._is_caption(b)
            ]

            is_editable = section_name.lower().rstrip(".") not in NON_EDITABLE_SECTIONS

            sections.append(Section(
                name=section_name,
                heading_block=heading_block,
                content_blocks=content_blocks,
                flow_regions=[],
                is_editable=is_editable,
            ))

        return sections

    def _normalise_section_name(self, text: str) -> str:
        """Clean up a heading block's text to a canonical section name."""
        # Remove leading/trailing whitespace and normalise internal spaces
        name = " ".join(text.split())
        # Capitalise known single-word headings
        lower = name.lower()
        for kh in KNOWN_HEADINGS:
            if lower == kh:
                return name.title()
        return name

    def _is_caption(self, block: TextBlock) -> bool:
        """Heuristic: block is a figure/table caption."""
        text = block.text.strip()
        return bool(re.match(
            r'^(fig(?:ure)?\.?\s*\d+|table\s*\d+|fig\.|tab\.)',
            text, re.IGNORECASE
        ))

    # ------------------------------------------------------------------
    # Flow region construction
    # ------------------------------------------------------------------

    def _build_flow_regions(
        self, section: Section, grammar: DocumentGrammar
    ) -> List[FlowRegion]:
        """
        Build an ordered list of FlowRegions from the section's content blocks.
        Consecutive blocks on the same page/column are merged into one region.
        """
        if not section.content_blocks and section.heading_block:
            # Section exists but has no content blocks detected
            hb = section.heading_block
            col = grammar.column_regions[hb.column] if hb.column < len(grammar.column_regions) else grammar.column_regions[0]
            return [FlowRegion(
                page=hb.page,
                column=hb.column,
                bbox=BBox(
                    x0=col.x0,
                    y0=hb.bbox.y1,
                    x1=col.x1,
                    y1=hb.bbox.y1 + grammar.body_line_height * 3,
                ),
                column_width=col.width,
            )]

        blocks = section.content_blocks
        if not blocks:
            return []

        regions = []
        current_page = blocks[0].page
        current_col = blocks[0].column
        region_y0 = blocks[0].bbox.y0
        region_y1 = blocks[0].bbox.y1

        def col_width_for(col_idx):
            if col_idx < len(grammar.column_regions):
                return grammar.column_regions[col_idx].width
            return grammar.column_regions[0].width

        def col_x0_for(col_idx):
            if col_idx < len(grammar.column_regions):
                return grammar.column_regions[col_idx].x0
            return grammar.column_regions[0].x0

        def col_x1_for(col_idx):
            if col_idx < len(grammar.column_regions):
                return grammar.column_regions[col_idx].x1
            return grammar.column_regions[0].x1

        def flush_region():
            regions.append(FlowRegion(
                page=current_page,
                column=current_col,
                bbox=BBox(
                    x0=col_x0_for(current_col),
                    y0=region_y0,
                    x1=col_x1_for(current_col),
                    y1=region_y1,
                ),
                column_width=col_width_for(current_col),
            ))

        for block in blocks[1:]:
            same_page = block.page == current_page
            same_col = block.column == current_col

            if same_page and same_col:
                # Extend current region
                region_y1 = max(region_y1, block.bbox.y1)
            else:
                # New page or column — flush current region
                flush_region()
                current_page = block.page
                current_col = block.column
                region_y0 = block.bbox.y0
                region_y1 = block.bbox.y1

        flush_region()
        return regions


def build_flow_map(doc: AnalyzedDocument) -> DocumentFlowMap:
    """Convenience function."""
    return FlowMapper().build(doc)
