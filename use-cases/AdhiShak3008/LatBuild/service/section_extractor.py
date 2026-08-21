"""
section_extractor.py
Extract clean prose from a section for sending to SuperDocs.

Responsibilities:
- Produce clean plain text from a Section's content blocks
- Report word count and estimated character budget
- Preserve non-content element positions (figures, tables never touched)

SuperDocs receives ONLY clean prose. It has no idea the text came from a PDF,
which page it came from, or what columns it occupied. That is intentional.
"""

from dataclasses import dataclass
from typing import List

from flow_mapper import Section, DocumentFlowMap
from pdf_analyzer import NonContentElement, DocumentGrammar


@dataclass
class ExtractedSection:
    section_name: str
    clean_text: str
    word_count: int
    char_count: int
    estimated_lines: int          # how many lines the original occupied
    estimated_height_pts: float   # total vertical space in points


def extract_section(section: Section, grammar: DocumentGrammar) -> ExtractedSection:
    """
    Extract clean prose from a section.
    Returns the text SuperDocs will receive.
    """
    # Join paragraph blocks with double newline to preserve paragraph structure
    paragraphs = []
    for block in section.content_blocks:
        text = block.text.strip()
        if text:
            paragraphs.append(text)

    clean_text = "\n\n".join(paragraphs)

    word_count = len(clean_text.split()) if clean_text else 0
    char_count = len(clean_text)

    # Estimate lines and height from flow regions
    total_height = sum(r.bbox.height for r in section.flow_regions)
    estimated_lines = max(1, int(total_height / grammar.body_line_height))

    return ExtractedSection(
        section_name=section.name,
        clean_text=clean_text,
        word_count=word_count,
        char_count=char_count,
        estimated_lines=estimated_lines,
        estimated_height_pts=total_height,
    )


def extract_multiple_sections(
    sections: List[Section], grammar: DocumentGrammar
) -> List[ExtractedSection]:
    """Extract multiple sections independently."""
    return [extract_section(s, grammar) for s in sections]


def build_superdocs_instruction(
    user_instruction: str,
    extracted: ExtractedSection,
    include_word_hint: bool = True,
) -> str:
    """
    Build the full instruction string sent to SuperDocs.

    The word count is a hint, not a hard constraint —
    the reflow engine handles whatever length SuperDocs returns.
    """
    parts = [
        "IMPORTANT: Preserve all citation markers exactly as they appear, "
        "such as [1], [2], (Smith et al., 2020), \\cite{key}, etc. "
        "Do not add, remove, or alter any citation. "
        "Preserve all technical terminology exactly. "
        "Do not add new claims, results, or references that are not in the original text.",
        "",
        user_instruction,
    ]

    if include_word_hint and extracted.word_count > 0:
        parts.append(
            f"\nNote: The original text is approximately {extracted.word_count} words. "
            "You may write more or less as needed — the layout engine will handle "
            "reflowing the result. Focus on quality over length matching."
        )

    return "\n".join(parts)
