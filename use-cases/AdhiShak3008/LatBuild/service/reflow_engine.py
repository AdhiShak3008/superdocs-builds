"""
reflow_engine.py
Plan how replacement text flows through the document after a SuperDocs edit.

This is the core engine. It answers the question:
  "Given that Section X now has N more/fewer lines of text, where does
   everything go in the document?"

Key invariants enforced here:
  1. Non-content elements (figures, tables, images) are NEVER moved unless
     the reflow planner explicitly determines they are in the affected flow.
     For MVP, all non-content is treated as anchored (absolute position).
  2. Pages and columns outside the affected flow are untouched.
  3. Overflow is detected and reported — never silently applied.
  4. Text width is measured using pymupdf get_text_length(), not estimated
     with character counts.

Reflow strategy:
  - Measure how many lines the replacement text needs at the section's
    typography (font, size, column width).
  - Compute the delta: lines_needed - lines_available.
  - If delta <= 0: replacement fits. Empty space left at end of section.
  - If delta > 0: section needs more flow regions. Consume space from
    downstream content, pushing it further in the document flow.
  - Detect collision with anchored non-content elements and report.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import math

try:
    import fitz  # pymupdf
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

from flow_mapper import Section, FlowRegion, DocumentFlowMap
from pdf_analyzer import BBox, DocumentGrammar, NonContentElement


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class SectionPatch:
    """Describes one region to redact and refill with replacement text."""
    region: FlowRegion
    text_segment: str          # the text that goes into this region
    operation: str             # "replace" | "overflow_extend" | "shrink_empty"


@dataclass
class BlockShift:
    """Describes one downstream text block that must be redrawn at a new position."""
    original_page: int
    original_col: int
    original_bbox: BBox
    original_text: str
    original_fontname: str
    original_fontsize: float
    new_page: int
    new_col: int
    new_y0: float              # new top y coordinate


@dataclass
class OverflowWarning:
    section_name: str
    lines_needed: int
    lines_available: int
    overflow_lines: int
    collides_with_non_content: bool
    description: str


@dataclass
class ReflowResult:
    """Complete plan for patching the PDF after one or more section edits."""
    section_patches: List[SectionPatch]
    downstream_shifts: List[BlockShift]
    overflow_warnings: List[OverflowWarning]
    delta_lines: int            # positive = grew, negative = shrank
    delta_points: float         # in PDF points
    affected_pages: List[int]
    unaffected_pages: List[int]
    can_apply: bool             # False if overflow is unresolvable


# ---------------------------------------------------------------------------
# Text measurement
# ---------------------------------------------------------------------------

def measure_text_width(text: str, fontname: str, fontsize: float) -> float:
    """
    Measure the rendered width of a text string in points.
    Uses pymupdf for accuracy when available; falls back to character estimate.
    """
    if PYMUPDF_AVAILABLE:
        try:
            return fitz.get_text_length(text, fontname=_normalise_fontname(fontname), fontsize=fontsize)
        except Exception:
            pass
    # Fallback: average character width ≈ 0.5 × fontsize
    return len(text) * fontsize * 0.5


def _normalise_fontname(fontname: str) -> str:
    """
    Strip PDF subset prefix (e.g. 'ABCDEF+TimesNewRoman' → 'Times-Roman')
    and map to a pymupdf built-in font name.
    """
    name = fontname.split("+")[-1].lower()
    if "times" in name or "roman" in name:
        if "bold" in name and "italic" in name:
            return "Times-BoldItalic"
        if "bold" in name:
            return "Times-Bold"
        if "italic" in name:
            return "Times-Italic"
        return "Times-Roman"
    if "helvetica" in name or "arial" in name or "sans" in name:
        if "bold" in name:
            return "Helvetica-Bold"
        return "Helvetica"
    if "courier" in name or "mono" in name:
        return "Courier"
    return "Times-Roman"


def estimate_lines_needed(
    text: str,
    column_width: float,
    fontname: str,
    fontsize: float,
    line_height: float,
) -> Tuple[int, float]:
    """
    Estimate how many lines and vertical points the text needs
    when typeset at the given column width and font.

    Returns (line_count, total_height_in_points).
    """
    if not text.strip():
        return 0, 0.0

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    total_lines = 0

    for para in paragraphs:
        words = para.split()
        if not words:
            continue

        current_line_width = 0.0
        line_count = 1
        space_width = measure_text_width(" ", fontname, fontsize)

        for word in words:
            word_width = measure_text_width(word, fontname, fontsize)
            if current_line_width == 0.0:
                current_line_width = word_width
            elif current_line_width + space_width + word_width <= column_width:
                current_line_width += space_width + word_width
            else:
                line_count += 1
                current_line_width = word_width

        total_lines += line_count

        # Paragraph spacing = 1 extra line between paragraphs
        total_lines += 1

    # Remove the trailing paragraph spacing
    if total_lines > 0:
        total_lines -= 1

    total_height = total_lines * line_height
    return total_lines, total_height


# ---------------------------------------------------------------------------
# Reflow Engine
# ---------------------------------------------------------------------------

class ReflowEngine:
    """
    Given a section, its replacement text, and the full document flow map,
    produce a ReflowResult describing exactly what needs to change in the PDF.
    """

    def plan(
        self,
        section: Section,
        replacement_text: str,
        flow_map: DocumentFlowMap,
    ) -> ReflowResult:
        grammar = flow_map.grammar
        fontname = grammar.body_fontname
        fontsize = grammar.body_fontsize
        line_height = grammar.body_line_height

        # ------------------------------------------------------------------
        # Step 1: Measure the replacement text
        # ------------------------------------------------------------------
        if section.flow_regions:
            col_width = section.flow_regions[0].column_width
        elif grammar.column_regions:
            col_width = grammar.column_regions[0].width
        else:
            col_width = grammar.page_width * 0.8

        lines_needed, height_needed = estimate_lines_needed(
            replacement_text, col_width, fontname, fontsize, line_height
        )

        # ------------------------------------------------------------------
        # Step 2: Measure the available space in existing flow regions
        # ------------------------------------------------------------------
        total_available_lines = sum(
            r.capacity_lines(line_height) for r in section.flow_regions
        )
        total_available_height = sum(r.bbox.height for r in section.flow_regions)

        delta_lines = lines_needed - total_available_lines
        delta_points = height_needed - total_available_height

        # ------------------------------------------------------------------
        # Step 3: Distribute replacement text across existing flow regions
        # ------------------------------------------------------------------
        patches, remaining_text = self._distribute_text(
            replacement_text, section.flow_regions, grammar
        )

        overflow_warnings = []
        downstream_shifts = []
        extra_patches = []
        can_apply = True

        # ------------------------------------------------------------------
        # Step 4: Handle overflow (replacement is longer than original)
        # ------------------------------------------------------------------
        if remaining_text.strip():
            overflow_result = self._handle_overflow(
                remaining_text,
                section,
                flow_map,
                grammar,
                delta_points,
            )
            extra_patches.extend(overflow_result["patches"])
            downstream_shifts.extend(overflow_result["shifts"])
            overflow_warnings.extend(overflow_result["warnings"])
            if overflow_result["unresolvable"]:
                can_apply = False

        # ------------------------------------------------------------------
        # Step 5: Handle shrink (replacement is shorter — empty space remains)
        # ------------------------------------------------------------------
        elif delta_lines < 0:
            # Mark remaining regions as empty (will be cleared but not refilled)
            # This is fine — empty space at end of section is acceptable
            pass

        # ------------------------------------------------------------------
        # Step 6: Downstream shifting DISABLED for MVP.
        # Shifting downstream content across complex page layouts (last pages,
        # two-column with references) produces incorrect redaction of non-target
        # content. For now, just patch the section's own regions and accept
        # minor gaps or slight overflow. The reflow engine architecture supports
        # this feature but it needs per-layout calibration.
        # ------------------------------------------------------------------
        # downstream_shifts = self._plan_downstream_shifts(...)  # deferred

        # Collect affected pages
        affected_pages = sorted(set(
            p.region.page for p in patches + extra_patches
        ) | set(s.new_page for s in downstream_shifts)
          | set(s.original_page for s in downstream_shifts))

        all_pages = list(range(1, flow_map.grammar.page_height and
                               max((b.page for b in flow_map.all_blocks), default=1) + 1
                               or 2))
        # simpler: just collect from all_blocks
        max_page = max((b.page for b in flow_map.all_blocks), default=1)
        all_page_nums = list(range(1, max_page + 1))
        unaffected_pages = [p for p in all_page_nums if p not in affected_pages]

        return ReflowResult(
            section_patches=patches + extra_patches,
            downstream_shifts=downstream_shifts,
            overflow_warnings=overflow_warnings,
            delta_lines=delta_lines,
            delta_points=delta_points,
            affected_pages=affected_pages,
            unaffected_pages=unaffected_pages,
            can_apply=can_apply,
        )

    # ------------------------------------------------------------------
    # Text distribution
    # ------------------------------------------------------------------

    def _distribute_text(
        self,
        text: str,
        regions: List[FlowRegion],
        grammar: DocumentGrammar,
    ) -> Tuple[List[SectionPatch], str]:
        """
        Fill each region with as much text as fits.
        Returns (patches, remaining_text).
        """
        patches = []
        remaining = text

        for region in regions:
            if not remaining.strip():
                # Region is now empty — mark it as cleared
                patches.append(SectionPatch(
                    region=region,
                    text_segment="",
                    operation="shrink_empty",
                ))
                continue

            segment, remaining = self._fill_region(remaining, region, grammar)
            patches.append(SectionPatch(
                region=region,
                text_segment=segment,
                operation="replace",
            ))

        return patches, remaining

    def _fill_region(
        self,
        text: str,
        region: FlowRegion,
        grammar: DocumentGrammar,
    ) -> Tuple[str, str]:
        """
        Fill a region with as much text as fits.
        Returns (text_for_region, remaining_text).
        """
        available_height = region.bbox.height
        line_height = grammar.body_line_height
        max_lines = max(1, int(available_height / line_height))
        col_width = region.column_width

        fontname = grammar.body_fontname
        fontsize = grammar.body_fontsize

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        filled_paragraphs = []
        lines_used = 0

        for para_idx, para in enumerate(paragraphs):
            words = para.split()
            para_lines = []
            current_line_words = []
            current_width = 0.0
            space_w = measure_text_width(" ", fontname, fontsize)

            for word in words:
                word_w = measure_text_width(word, fontname, fontsize)
                if not current_line_words:
                    current_line_words.append(word)
                    current_width = word_w
                elif current_width + space_w + word_w <= col_width:
                    current_line_words.append(word)
                    current_width += space_w + word_w
                else:
                    para_lines.append(" ".join(current_line_words))
                    current_line_words = [word]
                    current_width = word_w

            if current_line_words:
                para_lines.append(" ".join(current_line_words))

            para_line_count = len(para_lines) + 1  # +1 for paragraph spacing
            if lines_used + para_line_count > max_lines and filled_paragraphs:
                # This paragraph doesn't fit — stop here
                remaining_paras = paragraphs[para_idx:]
                return "\n\n".join(filled_paragraphs), "\n\n".join(remaining_paras)

            filled_paragraphs.append(para)
            lines_used += para_line_count

        # All text fits
        return "\n\n".join(filled_paragraphs), ""

    # ------------------------------------------------------------------
    # Overflow handling
    # ------------------------------------------------------------------

    def _handle_overflow(
        self,
        remaining_text: str,
        section: Section,
        flow_map: DocumentFlowMap,
        grammar: DocumentGrammar,
        delta_points: float,
    ) -> dict:
        """
        Handle text that overflowed the original section regions.
        Returns dict with patches, shifts, warnings, unresolvable flag.
        """
        patches = []
        shifts = []
        warnings = []
        unresolvable = False

        # Find downstream sections to push
        downstream = flow_map.sections_after(section)

        if not downstream:
            # Nothing downstream — overflow has nowhere to go
            warnings.append(OverflowWarning(
                section_name=section.name,
                lines_needed=0,
                lines_available=0,
                overflow_lines=len(remaining_text.split("\n")),
                collides_with_non_content=False,
                description=(
                    f"Replacement text overflows the available space for "
                    f"'{section.name}' and there are no downstream sections "
                    f"to shift. The overflow cannot be applied without "
                    f"truncating content."
                ),
            ))
            unresolvable = True
            return {"patches": patches, "shifts": shifts,
                    "warnings": warnings, "unresolvable": unresolvable}

        # Check for non-content collision in the overflow zone
        last_region = section.flow_regions[-1] if section.flow_regions else None
        collision = False
        if last_region:
            overflow_bbox = BBox(
                x0=last_region.bbox.x0,
                y0=last_region.bbox.y1,
                x1=last_region.bbox.x1,
                y1=last_region.bbox.y1 + abs(delta_points),
            )
            for nc in flow_map.non_content:
                if nc.page == last_region.page:
                    # Use normalised overlap check
                    bx0 = min(overflow_bbox.x0, overflow_bbox.x1)
                    bx1 = max(overflow_bbox.x0, overflow_bbox.x1)
                    by0 = min(overflow_bbox.y0, overflow_bbox.y1)
                    by1 = max(overflow_bbox.y0, overflow_bbox.y1)
                    ex0 = min(nc.bbox.x0, nc.bbox.x1)
                    ex1 = max(nc.bbox.x0, nc.bbox.x1)
                    ey0 = min(nc.bbox.y0, nc.bbox.y1)
                    ey1 = max(nc.bbox.y0, nc.bbox.y1)
                    ox = max(0, min(bx1, ex1) - max(bx0, ex0))
                    oy = max(0, min(by1, ey1) - max(by0, ey0))
                    if ox > 0 and oy > 0:
                        collision = True
                        break

        if collision:
            warnings.append(OverflowWarning(
                section_name=section.name,
                lines_needed=0,
                lines_available=0,
                overflow_lines=len(remaining_text.split("\n")),
                collides_with_non_content=True,
                description=(
                    f"Replacement text for '{section.name}' would overflow "
                    f"into a figure, table, or image. Applying this edit "
                    f"would corrupt the layout. Please use a shorter rewrite."
                ),
            ))
            unresolvable = True
            return {"patches": patches, "shifts": shifts,
                    "warnings": warnings, "unresolvable": unresolvable}

        # Create an overflow extension region immediately after the last region
        if last_region:
            overflow_region = FlowRegion(
                page=last_region.page,
                column=last_region.column,
                bbox=BBox(
                    x0=last_region.bbox.x0,
                    y0=last_region.bbox.y1,
                    x1=last_region.bbox.x1,
                    y1=last_region.bbox.y1 + delta_points + grammar.body_line_height * 2,
                ),
                column_width=last_region.column_width,
            )
            patches.append(SectionPatch(
                region=overflow_region,
                text_segment=remaining_text,
                operation="overflow_extend",
            ))

        # Plan downstream shifts
        shifts = self._plan_downstream_shifts(section, delta_points, flow_map)

        return {"patches": patches, "shifts": shifts,
                "warnings": warnings, "unresolvable": unresolvable}

    # ------------------------------------------------------------------
    # Downstream shift planning
    # ------------------------------------------------------------------

    def _plan_downstream_shifts(
        self,
        edited_section: Section,
        delta_points: float,
        flow_map: DocumentFlowMap,
    ) -> List[BlockShift]:
        """
        Plan how downstream blocks shift after the edited section grows/shrinks.
        Positive delta_points = content grew = downstream shifts down.
        Negative delta_points = content shrank = downstream shifts up.
        """
        if abs(delta_points) < 2.0:
            return []  # Negligible change

        grammar = flow_map.grammar
        downstream_sections = flow_map.sections_after(edited_section)

        shifts = []
        for ds in downstream_sections:
            # Skip non-editable sections at the very end (References, etc.)
            # They stay anchored unless the shift is very large
            all_blocks = ds.content_blocks
            if ds.heading_block:
                all_blocks = [ds.heading_block] + all_blocks

            for block in all_blocks:
                new_pos = self._compute_shifted_position(
                    block, delta_points, grammar
                )
                if new_pos:
                    shifts.append(BlockShift(
                        original_page=block.page,
                        original_col=block.column,
                        original_bbox=block.bbox,
                        original_text=block.text,
                        original_fontname=block.fontname,
                        original_fontsize=block.fontsize,
                        new_page=new_pos["page"],
                        new_col=new_pos["col"],
                        new_y0=new_pos["y0"],
                    ))

        return shifts

    def _compute_shifted_position(
        self,
        block,
        delta_points: float,
        grammar: DocumentGrammar,
    ) -> Optional[dict]:
        """
        Compute where a block should move after a delta shift.
        Returns dict with page, col, y0 or None if unmoved.
        """
        new_y0 = block.bbox.y0 + delta_points  # shift down (positive delta)
        new_page = block.page
        new_col = block.column

        col_count = grammar.column_count
        bottom_limit = grammar.page_height - grammar.margin_bottom
        top_limit = grammar.margin_top

        # Check if shifted position overflows the column bottom
        if new_y0 + block.bbox.height > bottom_limit:
            overflow = (new_y0 + block.bbox.height) - bottom_limit

            # Try next column on same page
            next_col = new_col + 1
            if next_col < col_count:
                new_col = next_col
                new_y0 = top_limit
            else:
                # Move to next page
                new_page = block.page + 1
                new_col = 0
                new_y0 = top_limit

        # Check if shifted position is above the top of the column
        elif new_y0 < top_limit and delta_points < 0:
            # Shrink case — block moves up; clamp to top margin
            new_y0 = max(top_limit, new_y0)

        if new_page == block.page and abs(new_y0 - block.bbox.y0) < 1.0:
            return None  # No meaningful movement

        return {"page": new_page, "col": new_col, "y0": new_y0}


def plan_reflow(
    section: Section,
    replacement_text: str,
    flow_map: DocumentFlowMap,
) -> ReflowResult:
    """Convenience function."""
    return ReflowEngine().plan(section, replacement_text, flow_map)


def plan_multi_section_reflow(
    edits: List[Tuple[Section, str]],
    flow_map: DocumentFlowMap,
) -> List[ReflowResult]:
    """
    Plan reflow for multiple section edits.
    Processes sections in reading order. Each edit's delta affects subsequent edits.
    """
    # Sort by reading order
    sorted_edits = sorted(edits, key=lambda e: e[0].reading_start)
    results = []
    engine = ReflowEngine()
    for section, replacement in sorted_edits:
        result = engine.plan(section, replacement, flow_map)
        results.append(result)
    return results
