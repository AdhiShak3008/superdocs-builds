"""
flow_mapper.py
Build a hierarchical DocumentFlowMap from an AnalyzedDocument.

Each section has:
  - id, title, level (1 = top-level, 2 = subsection)
  - parent_id (None for top-level)
  - heading_block
  - content_blocks (body text only, NOT the heading itself)
  - flow_regions
  - start_page, end_page, word_count
  - is_editable

Heading hierarchy:
  Level 1: Roman numeral  "I.", "II.", known top-level words
  Level 2: Alpha          "A.", "B.", "C."

Section boundaries:
  A section ends where the next section of equal or higher level begins.
  Level-2 sections are children of the immediately preceding level-1 section.

Layout provenance:
  Every block retains its page, column, bbox, and reading_order so the
  patcher can locate and redact exactly the right PDF region.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from pdf_analyzer import (
    AnalyzedDocument, TextBlock, NonContentElement,
    BBox, ColumnRegion, DocumentGrammar,
    KNOWN_HEADINGS, _ROMAN_HEADING_RE, _ALPHA_HEADING_RE, _KEYWORD_RE,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FlowRegion:
    """One physical rectangle on one page/column through which a section flows."""
    page: int
    column: int
    bbox: BBox
    column_width: float

    def capacity_lines(self, line_height: float) -> int:
        return max(1, int(self.bbox.height / line_height))


@dataclass
class Section:
    id: str                         # e.g. "abstract", "sec_I", "sec_I_A"
    title: str                      # clean display title
    level: int                      # 1 = top-level, 2 = subsection
    parent_id: Optional[str]        # None for level-1
    heading_block: Optional[TextBlock]
    content_blocks: List[TextBlock]  # body blocks only (no heading)
    flow_regions: List[FlowRegion]
    is_editable: bool = True

    @property
    def full_text(self) -> str:
        return "\n\n".join(b.text for b in self.content_blocks).strip()

    @property
    def word_count(self) -> int:
        return len(self.full_text.split()) if self.full_text else 0

    @property
    def pages(self) -> List[int]:
        ps = set()
        if self.heading_block:
            ps.add(self.heading_block.page)
        for b in self.content_blocks:
            ps.add(b.page)
        return sorted(ps)

    @property
    def start_page(self) -> int:
        return self.pages[0] if self.pages else 0

    @property
    def end_page(self) -> int:
        return self.pages[-1] if self.pages else 0

    @property
    def reading_start(self) -> int:
        if self.heading_block:
            return self.heading_block.reading_order
        return self.content_blocks[0].reading_order if self.content_blocks else 0

    @property
    def reading_end(self) -> int:
        return self.content_blocks[-1].reading_order if self.content_blocks else self.reading_start


@dataclass
class DocumentFlowMap:
    grammar: DocumentGrammar
    sections: List[Section]           # flat list in reading order
    all_blocks: List[TextBlock]
    non_content: List[NonContentElement]

    # ---- convenience accessors ----

    def get_section(self, title_or_id: str) -> Optional[Section]:
        for s in self.sections:
            if s.title == title_or_id or s.id == title_or_id:
                return s
        return None

    def editable_sections(self) -> List[Section]:
        return [s for s in self.sections if s.is_editable]

    def top_level_sections(self) -> List[Section]:
        return [s for s in self.sections if s.level == 1]

    def children_of(self, section: Section) -> List[Section]:
        return [s for s in self.sections
                if s.parent_id == section.id and s.level == section.level + 1]

    def sections_after(self, section: Section) -> List[Section]:
        found = False
        result = []
        for s in self.sections:
            if found:
                result.append(s)
            if s.id == section.id:
                found = True
        return result


# ---------------------------------------------------------------------------
# Non-editable section titles
# ---------------------------------------------------------------------------

NON_EDITABLE = {
    "references", "bibliography",
    "keywords", "index terms", "key words", "keyword",
}


# ---------------------------------------------------------------------------
# Heading level classifier
# ---------------------------------------------------------------------------

def _heading_level(text: str) -> int:
    """
    Return 1 for top-level IEEE headings, 2 for subsections, 0 for body text.
    """
    t  = text.strip()
    tn = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', t)
    tl = tn.lower().rstrip('.:—\u2014 ')

    # Roman numeral → level 1
    if _ROMAN_HEADING_RE.match(t) or _ROMAN_HEADING_RE.match(tn):
        return 1

    # Known top-level headings without Roman numeral (Abstract, References, etc.)
    TOP_WORDS = {
        "abstract", "introduction", "conclusion", "conclusions",
        "references", "acknowledgment", "acknowledgments",
        "acknowledgement", "acknowledgements",
        "related work", "background", "keywords", "index terms",
        "keyword", "key words", "summary", "appendix",
        "future work", "discussion",
    }
    if tl in TOP_WORDS:
        return 1
    if _KEYWORD_RE.match(tn):
        return 1

    # Alpha subsection → level 2
    if _ALPHA_HEADING_RE.match(t):
        return 2

    return 0


def _clean_title(text: str) -> str:
    """
    Clean a heading block's text to a canonical display title.

    Handles:
    - Letter-spacing artifacts: "I N T R O D U C T I O N" → "INTRODUCTION"
    - PDF space-removal ligatures: "RELATEDWORK" → "RELATED WORK"
    - Body text absorbed into heading line: truncate at heading boundary
    """
    # Step 1: collapse letter-spacing artifacts
    t = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', text.strip())
    t = " ".join(t.split())

    # Step 2: re-insert spaces in fused all-caps words using known patterns
    _FIXES = [
        (r'RELATEDWORK', 'RELATED WORK'),
        (r'SYSTEMARCHITECTURE', 'SYSTEM ARCHITECTURE'),
        (r'EXPERIMENTALEVALUATION(?:A\.?)?', 'EXPERIMENTAL EVALUATION'),
        (r'IMPLEMENTATIONAI-?', 'IMPLEMENTATION'),
    ]
    for pattern, replacement in _FIXES:
        t = re.sub(pattern, replacement, t, flags=re.IGNORECASE)

    # Step 3: truncate body text absorbed into heading line.
    # Keep only the heading portion: Roman/alpha prefix + CAPS words.
    # Stop when we hit mixed-case body text (e.g. "The platform is...")
    m_roman = re.match(r'^([IVX]+\.\s+[A-Z][A-Z\s\-]+?)(?=\s+[A-Z][a-z]|$)', t)
    m_alpha = re.match(r'^([A-Z]\.\s+[A-Z][A-Z\s\-]+?)(?=\s+[A-Z][a-z]|$)', t)
    m = m_roman or m_alpha
    if m:
        candidate = m.group(1).strip()
        if 2 <= len(candidate.split()) <= 8:
            t = candidate

    return " ".join(t.split())


def _make_id(title: str, parent_id: Optional[str] = None) -> str:
    """Create a stable slug-like ID from a title."""
    slug = re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')[:40]
    if parent_id:
        return f"{parent_id}__{slug}"
    return slug


# ---------------------------------------------------------------------------
# FlowMapper
# ---------------------------------------------------------------------------

class FlowMapper:

    def build(self, doc: AnalyzedDocument) -> DocumentFlowMap:
        sections = self._detect_sections(doc.all_blocks, doc.grammar)
        for s in sections:
            s.flow_regions = self._build_flow_regions(s, doc.grammar)
        return DocumentFlowMap(
            grammar=doc.grammar,
            sections=sections,
            all_blocks=doc.all_blocks,
            non_content=doc.non_content,
        )

    # ------------------------------------------------------------------
    # Section detection
    # ------------------------------------------------------------------

    def _detect_sections(
        self, all_blocks: List[TextBlock], grammar: DocumentGrammar
    ) -> List[Section]:
        if not all_blocks:
            return []

        # --- Step 1: find all heading blocks and their levels ---
        heading_indices = [
            (i, _heading_level(b.text))
            for i, b in enumerate(all_blocks)
            if b.is_heading and _heading_level(b.text) > 0
        ]

        if not heading_indices:
            # No headings — single section
            return [Section(
                id="document", title="Document", level=1,
                parent_id=None,
                heading_block=None,
                content_blocks=all_blocks,
                flow_regions=[],
                is_editable=True,
            )]

        sections: List[Section] = []

        # --- Step 2: preamble (blocks before first heading) ---
        first_h_idx = heading_indices[0][0]
        if first_h_idx > 0:
            pre = all_blocks[:first_h_idx]
            sections.append(Section(
                id="preamble", title="Title / Preamble", level=1,
                parent_id=None,
                heading_block=None,
                content_blocks=pre,
                flow_regions=[],
                is_editable=False,
            ))

        # --- Step 3: build each section from heading to next heading ---
        current_l1_id: Optional[str] = None

        for pos, (h_idx, level) in enumerate(heading_indices):
            hblock = all_blocks[h_idx]
            title  = _clean_title(hblock.text)

            # Content blocks: from h_idx+1 to next heading (any level)
            next_h_idx = (
                heading_indices[pos + 1][0]
                if pos + 1 < len(heading_indices)
                else len(all_blocks)
            )
            content = [
                b for b in all_blocks[h_idx + 1 : next_h_idx]
                if not self._is_caption(b)
            ]

            # Determine parent
            parent_id: Optional[str] = None
            if level == 2:
                parent_id = current_l1_id
            elif level == 1:
                current_l1_id = None  # will be set below

            # Keywords line: the entire text is in the heading block
            # Mark as non-editable metadata; word count from heading text
            tl_check = title.lower()
            if any(kw in tl_check for kw in ["keyword", "index term"]):
                editable = False

            sec_id = _make_id(title, parent_id)

            tl = title.lower().rstrip('.:—\u2014 ')
            editable = tl not in NON_EDITABLE

            s = Section(
                id=sec_id,
                title=title,
                level=level,
                parent_id=parent_id,
                heading_block=hblock,
                content_blocks=content,
                flow_regions=[],
                is_editable=editable,
            )
            sections.append(s)

            if level == 1:
                current_l1_id = sec_id

        return sections

    def _is_caption(self, block: TextBlock) -> bool:
        return bool(re.match(
            r'^(fig(?:ure)?\.?\s*\d|table\s*\d|fig\.|tab\.)',
            block.text.strip(), re.IGNORECASE
        ))

    # ------------------------------------------------------------------
    # Flow region building
    # ------------------------------------------------------------------

    def _build_flow_regions(
        self, section: Section, grammar: DocumentGrammar
    ) -> List[FlowRegion]:
        blocks = section.content_blocks
        if not blocks:
            if section.heading_block:
                hb = section.heading_block
                col_idx = hb.column
                col = (grammar.column_regions[col_idx]
                       if col_idx < len(grammar.column_regions)
                       else grammar.column_regions[0])
                return [FlowRegion(
                    page=hb.page, column=col_idx,
                    bbox=BBox(col.x0, hb.bbox.y1, col.x1,
                              hb.bbox.y1 + grammar.body_line_height * 3),
                    column_width=col.width,
                )]
            return []

        regions: List[FlowRegion] = []
        cur_page = blocks[0].page
        cur_col  = blocks[0].column
        reg_y0   = blocks[0].bbox.y0
        reg_y1   = blocks[0].bbox.y1

        def col_x0(ci):
            return grammar.column_regions[ci].x0 if ci < len(grammar.column_regions) \
                else grammar.column_regions[0].x0

        def col_x1(ci):
            return grammar.column_regions[ci].x1 if ci < len(grammar.column_regions) \
                else grammar.column_regions[0].x1

        def col_w(ci):
            return grammar.column_regions[ci].width if ci < len(grammar.column_regions) \
                else grammar.column_regions[0].width

        def flush():
            regions.append(FlowRegion(
                page=cur_page, column=cur_col,
                bbox=BBox(col_x0(cur_col), reg_y0, col_x1(cur_col), reg_y1),
                column_width=col_w(cur_col),
            ))

        for b in blocks[1:]:
            if b.page == cur_page and b.column == cur_col:
                reg_y1 = max(reg_y1, b.bbox.y1)
            else:
                flush()
                cur_page, cur_col = b.page, b.column
                reg_y0, reg_y1 = b.bbox.y0, b.bbox.y1

        flush()
        return regions


def build_flow_map(doc: AnalyzedDocument) -> DocumentFlowMap:
    return FlowMapper().build(doc)
