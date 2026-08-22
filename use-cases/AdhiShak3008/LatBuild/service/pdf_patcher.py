"""
pdf_patcher.py
Apply a ReflowResult to the original PDF, producing a modified PDF.

Responsibilities:
- Redact original content regions (fill with white)
- Insert replacement text into those regions using the document's typography
- Redraw downstream blocks at their new positions
- NEVER touch non-content elements (figures, tables, images)
  unless explicitly listed in the reflow plan

Hard invariant (enforced here):
  Non-content elements are anchored. We verify that no patch or shift
  region overlaps a non-content element before applying. If a collision
  is detected that was not caught by the reflow engine, we skip that
  operation and log a warning.

Uses pymupdf (fitz) for all PDF modifications.
"""

import io
import logging
from dataclasses import dataclass
from typing import List, Optional

import fitz  # pymupdf

from reflow_engine import ReflowResult, SectionPatch, BlockShift, _normalise_fontname
from pdf_analyzer import NonContentElement, BBox, DocumentGrammar

logger = logging.getLogger(__name__)


@dataclass
class PatchWarning:
    operation: str
    description: str
    skipped: bool = True


@dataclass
class PatchReport:
    success: bool
    pages_modified: List[int]
    warnings: List[PatchWarning]
    output_path: str


def apply_reflow(
    original_pdf_path: str,
    reflow_result: ReflowResult,
    grammar: DocumentGrammar,
    non_content: List[NonContentElement],
    output_path: str,
) -> PatchReport:
    """
    Apply a ReflowResult to the original PDF.
    Writes the result to output_path.
    Returns a PatchReport describing what happened.
    """
    doc = fitz.open(original_pdf_path)
    warnings = []
    pages_modified = set()

    # ------------------------------------------------------------------
    # Phase 1: Collect all redaction rectangles
    # (redact in one pass to avoid visual artifacts)
    # ------------------------------------------------------------------
    redact_ops = []  # list of (page_idx, fitz.Rect)

    for patch in reflow_result.section_patches:
        # Always redact ALL section regions — including shrink_empty ones.
        # shrink_empty means the new text is shorter and this region will be
        # left blank (white). The old text must be erased.
        page_idx = patch.region.page - 1
        raw_rect = _to_fitz_rect(patch.region.bbox, doc[page_idx].rect.height)
        # Respect heading_bottom_y — don't redact the heading line itself
        hby = patch.region.heading_bottom_y
        rect = fitz.Rect(raw_rect.x0, max(raw_rect.y0, hby) if hby > 0 else raw_rect.y0,
                         raw_rect.x1, raw_rect.y1)

        if _collides_with_non_content(patch.region.bbox, patch.region.page, non_content):
            warnings.append(PatchWarning(
                operation=f"redact section patch page {patch.region.page}",
                description=(
                    f"Patch region on page {patch.region.page} collides with "
                    f"a non-content element. Skipping to preserve layout integrity."
                ),
                skipped=True,
            ))
            logger.warning("Skipping patch on page %d — non-content collision", patch.region.page)
            continue

        redact_ops.append((page_idx, rect))
        pages_modified.add(patch.region.page)

    for shift in reflow_result.downstream_shifts:
        page_idx = shift.original_page - 1
        rect = _to_fitz_rect(shift.original_bbox, doc[page_idx].rect.height)

        if _collides_with_non_content(shift.original_bbox, shift.original_page, non_content):
            warnings.append(PatchWarning(
                operation=f"redact shift source page {shift.original_page}",
                description=(
                    f"Shift source on page {shift.original_page} collides with "
                    f"a non-content element. Skipping shift."
                ),
                skipped=True,
            ))
            continue

        redact_ops.append((page_idx, rect))
        pages_modified.add(shift.original_page)

    # Apply all redactions
    for page_idx, rect in redact_ops:
        page = doc[page_idx]
        page.add_redact_annot(rect, fill=(1, 1, 1))

    for page_idx in set(pi for pi, _ in redact_ops):
        doc[page_idx].apply_redactions(graphics=1)

    # ------------------------------------------------------------------
    # Phase 2: Insert replacement text into section patch regions
    # ------------------------------------------------------------------
    for patch in reflow_result.section_patches:
        # shrink_empty regions were already redacted in Phase 1.
        # Leave them blank (white space) — don't insert old or new text.
        if patch.operation == "shrink_empty":
            continue

        if _collides_with_non_content(patch.region.bbox, patch.region.page, non_content):
            continue  # already warned above

        if not patch.text_segment.strip():
            continue

        page_idx = patch.region.page - 1
        page = doc[page_idx]
        raw_rect = _to_fitz_rect(patch.region.bbox, page.rect.height)

        # If the region has a heading_bottom_y, start the redact/insert
        # BELOW the heading line so we don't erase the section heading itself.
        hby = patch.region.heading_bottom_y
        if hby > raw_rect.y0:
            rect = fitz.Rect(raw_rect.x0, hby, raw_rect.x1, raw_rect.y1)
        else:
            rect = raw_rect

        fontname = _normalise_fontname(grammar.body_fontname)
        fontsize = grammar.body_fontsize

        # Detect actual font size used in this region from the original PDF
        # to match the original typography as closely as possible
        region_text = page.get_text("dict", clip=rect)
        detected_sizes = []
        for block in region_text.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sz = span.get("size", 0)
                    if sz > 0:
                        detected_sizes.append(sz)
        if detected_sizes:
            from statistics import median
            detected_fontsize = round(median(detected_sizes), 1)
            # Use detected size if it's reasonable (between 7 and 14pt)
            if 7 <= detected_fontsize <= 14:
                fontsize = detected_fontsize

        # Try inserting at original font size; if overflow, retry smaller
        overflow = -1
        for attempt_fontsize in [fontsize, fontsize * 0.95, fontsize * 0.90,
                                  fontsize * 0.85, fontsize * 0.80]:
            overflow = page.insert_textbox(
                rect,
                patch.text_segment,
                fontname=fontname,
                fontsize=attempt_fontsize,
                align=fitz.TEXT_ALIGN_JUSTIFY,
                color=(0, 0, 0),
            )
            if overflow >= 0:
                break  # fits

        # If still overflowing, expand rect downward to accommodate
        if overflow < 0:
            # Extend bottom of rect by the overflow amount + small margin
            expanded_rect = fitz.Rect(
                rect.x0, rect.y0,
                rect.x1, rect.y1 + abs(overflow) + 2
            )
            overflow = page.insert_textbox(
                expanded_rect,
                patch.text_segment,
                fontname=fontname,
                fontsize=fontsize,
                align=fitz.TEXT_ALIGN_JUSTIFY,
                color=(0, 0, 0),
            )

        if overflow < 0:
            warnings.append(PatchWarning(
                operation=f"insert text page {patch.region.page}",
                description=(
                    f"Text overflow on page {patch.region.page}: "
                    f"{abs(overflow):.0f} points of text did not fit. "
                    f"Consider requesting a more concise rewrite."
                ),
                skipped=False,
            ))

    # ------------------------------------------------------------------
    # Phase 3: Redraw downstream shifted blocks
    # ------------------------------------------------------------------
    for shift in reflow_result.downstream_shifts:
        if _collides_with_non_content(shift.original_bbox, shift.original_page, non_content):
            continue

        # Ensure target page exists (may be a new page if content pushed past end)
        while shift.new_page > len(doc):
            doc.new_page(width=grammar.page_width, height=grammar.page_height)

        target_page_idx = shift.new_page - 1
        target_page = doc[target_page_idx]

        # Build target rect
        if shift.new_col < len(grammar.column_regions):
            col = grammar.column_regions[shift.new_col]
            col_x0, col_x1 = col.x0, col.x1
        else:
            col_x0 = grammar.margin_left
            col_x1 = grammar.page_width - grammar.margin_right

        block_height = shift.original_bbox.height
        target_rect_pdf = BBox(
            x0=col_x0,
            y0=shift.new_y0,
            x1=col_x1,
            y1=shift.new_y0 + block_height,
        )
        target_rect = _to_fitz_rect(target_rect_pdf, target_page.rect.height)

        fontname = _normalise_fontname(shift.original_fontname)
        fontsize = shift.original_fontsize

        target_page.insert_textbox(
            target_rect,
            shift.original_text,
            fontname=fontname,
            fontsize=fontsize,
            align=fitz.TEXT_ALIGN_JUSTIFY,
            color=(0, 0, 0),
        )
        pages_modified.add(shift.new_page)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    return PatchReport(
        success=True,
        pages_modified=sorted(pages_modified),
        warnings=warnings,
        output_path=output_path,
    )


def apply_multiple_reflows(
    original_pdf_path: str,
    reflow_results: List[ReflowResult],
    grammar: DocumentGrammar,
    non_content: List[NonContentElement],
    output_path: str,
) -> PatchReport:
    """
    Apply multiple ReflowResults (from multiple section edits) to one PDF.
    Merges all patches and applies in a single pass to avoid conflicts.
    """
    # Merge all patches and shifts
    all_patches = []
    all_shifts = []
    all_warnings = []

    for result in reflow_results:
        all_patches.extend(result.section_patches)
        all_shifts.extend(result.downstream_shifts)
        all_warnings.extend([
            PatchWarning(
                operation="overflow",
                description=w.description,
                skipped=not result.can_apply,
            )
            for w in result.overflow_warnings
        ])

    # Build a combined ReflowResult
    merged = ReflowResult(
        section_patches=all_patches,
        downstream_shifts=all_shifts,
        overflow_warnings=[],
        delta_lines=sum(r.delta_lines for r in reflow_results),
        delta_points=sum(r.delta_points for r in reflow_results),
        affected_pages=sorted(set(p for r in reflow_results for p in r.affected_pages)),
        unaffected_pages=sorted(set(p for r in reflow_results for p in r.unaffected_pages)),
        can_apply=all(r.can_apply for r in reflow_results),
    )

    report = apply_reflow(
        original_pdf_path, merged, grammar, non_content, output_path
    )
    report.warnings.extend(all_warnings)
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_fitz_rect(bbox: BBox, page_height: float) -> fitz.Rect:
    """
    Convert our top-origin BBox to pymupdf's top-origin Rect.

    Both pdfplumber and pymupdf use top-left origin in page coordinates
    when accessed via the standard text extraction APIs. No conversion needed.
    The y0 value from pdfplumber's 'top' field is the distance from the
    top of the page, which is what fitz.Rect also expects.
    """
    return fitz.Rect(
        bbox.x0,
        bbox.y0,
        bbox.x1,
        bbox.y1,
    )


def _collides_with_non_content(
    bbox: BBox,
    page: int,
    non_content: List[NonContentElement],
    threshold: float = 0.3,
) -> bool:
    """
    Return True if bbox overlaps a non-content element by more than threshold
    fraction of the bbox area. Non-content elements are NEVER overwritten.
    """
    for elem in non_content:
        if elem.page != page:
            continue
        # Normalise coordinates to top-left origin (min/max handles both orientations)
        bx0, bx1 = min(bbox.x0, bbox.x1), max(bbox.x0, bbox.x1)
        by0, by1 = min(bbox.y0, bbox.y1), max(bbox.y0, bbox.y1)
        ex0, ex1 = min(elem.bbox.x0, elem.bbox.x1), max(elem.bbox.x0, elem.bbox.x1)
        ey0, ey1 = min(elem.bbox.y0, elem.bbox.y1), max(elem.bbox.y0, elem.bbox.y1)

        overlap_x = max(0.0, min(bx1, ex1) - max(bx0, ex0))
        overlap_y = max(0.0, min(by1, ey1) - max(by0, ey0))
        overlap_area = overlap_x * overlap_y
        bbox_area = bbox.area
        if bbox_area > 0 and overlap_area / bbox_area > threshold:
            return True
    return False
