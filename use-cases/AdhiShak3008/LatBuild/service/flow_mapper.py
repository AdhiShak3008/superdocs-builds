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
        from text_normalizer import normalize_section_text
        raw = "\n\n".join(b.text for b in self.content_blocks).strip()
        return normalize_section_text(raw)

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
    # For Roman headings: "III. SYSTEM ARCHITECTURE The platform..." → keep CAPS words
    m_roman = re.match(r'^([IVX]+\.\s+[A-Z][A-Z\s\-]+?)(?=\s+[A-Z][a-z]|$)', t)
    if m_roman:
        candidate = m_roman.group(1).strip()
        if 2 <= len(candidate.split()) <= 8:
            t = candidate

    # For alpha headings: "D. DocPilot effective ranking..." → keep title-case words
    # Alpha heading words start with uppercase; body text transitions to lowercase
    m_alpha = re.match(r'^([A-Z]\.\s+)', t)
    if m_alpha and not m_roman:
        prefix = m_alpha.group(1)
        rest = t[len(prefix):]
        # Keep words that are capitalized (title-case heading words)
        # Stop at the first clearly lowercase word that isn't a short connector
        heading_words = []
        for word in rest.split():
            if word[0].isupper() or word in ('and', 'of', 'the', 'for', 'in', 'on', 'to', 'with'):
                heading_words.append(word)
            else:
                break
            # Stop after 5 heading words max
            if len(heading_words) >= 5:
                break
        if heading_words:
            t = prefix + " ".join(heading_words)

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

        # --- Step 1: find candidate heading blocks and their levels ---
        candidates = [
            (i, _heading_level(b.text))
            for i, b in enumerate(all_blocks)
            if b.is_heading and _heading_level(b.text) > 0
        ]

        if not candidates:
            return [Section(
                id="document", title="Document", level=1,
                parent_id=None, heading_block=None,
                content_blocks=all_blocks, flow_regions=[],
                is_editable=True,
            )]

        # --- Step 2: VALIDATE heading candidates ---
        # Apply document-structure constraints to reject false positives.
        heading_indices = self._validate_heading_candidates(candidates, all_blocks)

        if not heading_indices:
            return [Section(
                id="document", title="Document", level=1,
                parent_id=None, heading_block=None,
                content_blocks=all_blocks, flow_regions=[],
                is_editable=True,
            )]

        sections: List[Section] = []

        # --- Step 3: preamble (blocks before first heading) ---
        first_h_idx = heading_indices[0][0]
        if first_h_idx > 0:
            pre = all_blocks[:first_h_idx]
            sections.append(Section(
                id="preamble", title="Title / Preamble", level=1,
                parent_id=None, heading_block=None,
                content_blocks=pre, flow_regions=[],
                is_editable=False,
            ))

        # --- Step 4: build sections, respecting REFERENCES terminal ---
        current_l1_id: Optional[str] = None
        in_references = False

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

            # Check if this is the REFERENCES heading
            tl = title.lower().rstrip('.:—\u2014 ')
            if tl in ("references", "bibliography"):
                in_references = True

            # Once in REFERENCES, everything is non-editable and no subsections
            if in_references:
                if tl not in ("references", "bibliography"):
                    # This is bibliography content, NOT a real section.
                    # Absorb it into the REFERENCES section.
                    continue

            # Determine parent
            parent_id: Optional[str] = None
            if level == 2:
                parent_id = current_l1_id
            elif level == 1:
                current_l1_id = None

            sec_id = _make_id(title, parent_id)

            editable = tl not in NON_EDITABLE and not in_references

            # Keywords check
            if any(kw in tl for kw in ["keyword", "index term"]):
                editable = False

            s = Section(
                id=sec_id, title=title, level=level,
                parent_id=parent_id,
                heading_block=hblock,
                content_blocks=content,
                flow_regions=[],
                is_editable=editable,
            )
            sections.append(s)

            if level == 1:
                current_l1_id = sec_id

        # If REFERENCES was detected, absorb all post-REFERENCES content into it
        if in_references:
            refs_section = next((s for s in sections
                                 if s.title.lower().rstrip('.:—\u2014 ') in
                                 ("references", "bibliography")), None)
            if refs_section:
                refs_idx = next(i for i, (idx, _) in enumerate(heading_indices)
                                if all_blocks[idx] == refs_section.heading_block)
                refs_h_idx = heading_indices[refs_idx][0]
                # All blocks after REFERENCES heading belong to it
                refs_section.content_blocks = [
                    b for b in all_blocks[refs_h_idx + 1:]
                    if not self._is_caption(b)
                ]

        return sections

    # ------------------------------------------------------------------
    # Heading validation layer
    # ------------------------------------------------------------------

    def _validate_heading_candidates(
        self,
        candidates: List[tuple],
        all_blocks: List[TextBlock],
    ) -> List[tuple]:
        """
        Filter heading candidates using document-structure constraints.

        Rules enforced:
        1. Roman numerals must form a plausible monotone sequence.
           A candidate that goes backwards (e.g. IV. after V.) is rejected
           unless it's actually a lower number (restart).
        2. Bibliographic patterns ("V. Cormack, C.", "L. A. Clarke, ...")
           are rejected — they look like "Author Initial. Name".
        3. Alpha subsection candidates require an active Roman parent.
        4. "keyword-based ..." body text is rejected.
        5. After REFERENCES heading, all subsequent candidates are rejected.
        """
        if not candidates:
            return []

        validated = []
        max_roman_seen = 0
        references_seen = False
        has_active_l1_parent = False

        for (block_idx, level) in candidates:
            block = all_blocks[block_idx]
            title_raw = block.text.strip()
            title_clean = _clean_title(title_raw)
            tl = title_clean.lower().rstrip('.:—\u2014 ')

            # Rule 5: after REFERENCES, reject everything
            if references_seen:
                continue
            if tl in ("references", "bibliography"):
                references_seen = True
                validated.append((block_idx, level))
                continue

            # Rule 2: reject bibliographic patterns
            # Pattern: "X. Surname," where X is a single letter
            if self._is_bibliographic(title_raw):
                continue

            # Rule 4: reject body text starting with a known word
            # "keyword-based retrieval..." is body text
            if level == 1 and len(title_raw.split()) > 8:
                # Long text that matched heading — likely false positive
                continue

            # Rule 1: Roman numeral monotonicity
            if level == 1:
                roman_num = self._extract_roman_number(title_clean)
                if roman_num is not None:
                    # Allow forward progression or same number
                    if roman_num >= max_roman_seen:
                        max_roman_seen = roman_num
                    elif roman_num < max_roman_seen:
                        # Going backwards — reject unless it's I (restart)
                        if roman_num != 1:
                            continue
                        max_roman_seen = roman_num

            # Rule 3: alpha subsections need an active L1 parent
            if level == 2:
                if not has_active_l1_parent:
                    # No parent → reject unless this is a known top-level
                    if tl not in KNOWN_HEADINGS:
                        continue

            validated.append((block_idx, level))

            if level == 1:
                has_active_l1_parent = True

        return validated

    def _is_bibliographic(self, text: str) -> bool:
        """
        Detect bibliographic reference entries that look like headings.

        Critical: must NOT reject legitimate alpha subsections like
        "A. Platform Overview" or "B. PilotCore Framework".

        Distinction: subsections have a known structural pattern.
        Bibliography entries are names, have commas, quotation marks, years,
        or are very short (just an author initial + surname).
        """
        t = text.strip()

        # Contains quotation marks → bibliographic (paper titles)
        if '"' in t or '\u201c' in t or '\u201d' in t:
            return True

        # Contains a year in parentheses or after comma → bibliographic
        if re.search(r'[\(,]\s*\d{4}', t):
            return True

        # Pattern: "X. Surname, Initial." — has a comma after the first word
        if re.match(r'^[A-Z]\.\s+[A-Z][a-z]+\s*,', t):
            return True

        # Pattern: "X. Surname" (2-3 words, looks like an author name)
        words = t.split()
        if (len(words) == 2 and re.match(r'^[A-Z]\.\s+[A-Z][a-z]+$', t)):
            surname = words[1].lower()
            # Known heading/section words are NOT surnames
            _NOT_SURNAME = {
                "overview", "framework", "model", "pipeline", "analysis",
                "setup", "metrics", "results", "evaluation", "rationale",
                "decisions", "trade-offs", "limitations", "work", "ingestion",
                "extraction", "construction", "generation", "architecture",
                "implementation", "discussion", "introduction", "availability",
                "background", "related", "motivation", "summary", "design",
                "conclusion", "method", "methods", "system", "approach",
                "docpilot", "tracepilot", "gaugepilot", "pilotcore",
            }
            if surname not in _NOT_SURNAME and len(surname) <= 8:
                return True

        # "et al" → bibliographic
        if "et al" in t.lower():
            return True

        # "and" at end or mid with initials → multi-author reference
        if re.match(r'^[A-Z]\.\s+\S+\s+and\b', t):
            return True

        # "G. V. Cormack" — multiple initials
        if re.match(r'^[A-Z]\.\s+[A-Z]\.\s+[A-Z]', t):
            return True

        return False

    def _extract_roman_number(self, title: str) -> Optional[int]:
        """Extract the Roman numeral from a title like 'III. SYSTEM ARCHITECTURE'."""
        m = re.match(r'^([IVX]+)\.', title)
        if not m:
            return None
        roman = m.group(1)
        roman_map = {'I': 1, 'V': 5, 'X': 10}
        total = 0
        prev = 0
        for ch in reversed(roman):
            val = roman_map.get(ch, 0)
            if val < prev:
                total -= val
            else:
                total += val
            prev = val
        return total

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
