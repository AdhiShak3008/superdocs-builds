"""
docx_bridge.py
Convert plain text to/from .docx for the SuperDocs API.

SuperDocs requires a .docx upload. This module handles the minimal
conversion needed: prose → .docx for upload, .docx → prose after export.

Keeps the conversion simple and lossless for plain prose.
"""

import io
import re
from docx import Document
from docx.shared import Pt
from html.parser import HTMLParser


def prose_to_docx(text: str, title: str = "Section") -> bytes:
    """
    Convert plain text to a minimal .docx file.
    Paragraphs are separated by double newlines.
    Returns raw bytes of the .docx file.
    """
    doc = Document()

    # Remove default empty paragraph
    for para in doc.paragraphs:
        p = para._element
        p.getparent().remove(p)

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else ["(empty)"]

    for para_text in paragraphs:
        para = doc.add_paragraph(para_text)
        para.style.font.size = Pt(11)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return plain text."""
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip_tags = {"script", "style"}
        self._in_skip = False

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._in_skip = True
        if tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._in_skip = False

    def handle_data(self, data):
        if not self._in_skip:
            self._parts.append(data)

    def get_text(self):
        return "".join(self._parts)


def extract_text_from_docx(docx_bytes: bytes) -> str:
    """
    Extract plain text from .docx bytes.
    Returns paragraphs joined with double newlines.
    """
    buf = io.BytesIO(docx_bytes)
    doc = Document(buf)
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def extract_text_from_html(html: str) -> str:
    """
    Extract plain text from SuperDocs' returned HTML.
    Used when we want to pull rewritten prose from the job result HTML.
    """
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    raw = extractor.get_text()

    # Normalise whitespace
    lines = [line.strip() for line in raw.split("\n")]
    # Collapse multiple blank lines into paragraph breaks
    cleaned = []
    blank_count = 0
    for line in lines:
        if not line:
            blank_count += 1
        else:
            if blank_count > 0:
                cleaned.append("")
            blank_count = 0
            cleaned.append(line)

    return "\n\n".join(
        para for para in "\n".join(cleaned).split("\n\n") if para.strip()
    )
