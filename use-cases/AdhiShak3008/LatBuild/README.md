# LatBuild — SuperDocs PDF Companion

A SuperDocs-powered surgical PDF editing tool that lets you rewrite any section of an existing PDF while preserving the original document's visual layout, columns, fonts, figures, and structure.

**Built for:** The SuperDocs Engineer Task — Round 2  
**Author:** AdhiShak3008

---

## The Problem

When you upload an 11-page IEEE paper to SuperDocs and ask it to rewrite the Abstract, the AI editing works correctly — but the exported document loses the IEEE two-column layout and becomes a 24-page wall of prose.

**The semantic edit succeeds. Document fidelity fails.**

LatBuild solves this by keeping SuperDocs responsible for the text transformation and keeping the application responsible for document surgery.

```
SuperDocs sees:   pure text
SuperDocs returns: pure text
LatBuild does:    everything else
```

---

## How It Works

```
Upload PDF
  ↓
PDF Analyzer (pdfplumber)
  ↓ extracts: columns, margins, fonts, headings, text blocks
DocumentFlowMap
  ↓ maps: sections → ordered text blocks → flow regions
User selects sections
  ↓
Section Extractor → clean prose per section
  ↓
SuperDocs API (upload → chat → approve → export)
  ↓ returns: rewritten prose
Reflow Engine
  ↓ measures replacement, distributes across flow regions
  ↓ plans downstream shifts if section grew/shrank
PDF Patcher (pymupdf)
  ↓ redacts original regions, inserts replacement text
  ↓ redraws shifted downstream content
  ↓ NEVER touches figures, tables, or non-content elements
Fidelity Checker
  ↓ pixel-compares original vs modified
  ↓ verifies changes localized to selected sections
Download modified PDF
```

---

## What Makes This Different

**SuperDocs does NOT edit the PDF.**

SuperDocs receives plain text extracted from the selected section. It returns rewritten plain text. It has no knowledge of PDF coordinates, columns, fonts, or layout.

LatBuild's reflow engine takes the rewritten text and:
1. Measures how many lines it needs at the section's typography
2. Fills the original flow regions sequentially
3. If the replacement is longer, extends into downstream space and shifts subsequent sections accordingly
4. If the replacement is shorter, the remaining region is cleared (empty space at section end)
5. Detects and refuses to overwrite figures, tables, or images (hard invariant)

---

## Architecture

```
service/
├── pdf_analyzer.py       — pdfplumber: layout, grammar, text blocks
├── flow_mapper.py        — sections, FlowRegions, DocumentFlowMap
├── section_extractor.py  — clean prose extraction, word budget
├── docx_bridge.py        — prose ↔ .docx (for SuperDocs API)
├── superdocs_client.py   — SuperDocs REST API (unchanged from preprint app)
├── reflow_engine.py      — text measurement, flow distribution, shift planning
├── pdf_patcher.py        — pymupdf: redact + redraw, non-content invariant
├── fidelity_checker.py   — pixel diff, structural validation, report
└── app.py                — Flask: all routes and orchestration
```

**Key data structures:**

- `DocumentGrammar` — the PDF's visual rules (fonts, margins, columns, line height)
- `DocumentFlowMap` — the entire document as ordered sections → flow regions
- `FlowRegion` — a physical container (page + column + bbox) that content flows through
- `ReflowResult` — the complete plan: patches, downstream shifts, overflow warnings
- `FidelityReport` — pixel-level validation of what changed and what didn't

---

## SuperDocs Integration

| Operation | Endpoint | Purpose |
|---|---|---|
| Upload section | `POST /v1/documents/upload` | Send section prose as .docx |
| Start edit | `POST /v1/chat/async` | Send user instruction |
| Poll | `GET /v1/jobs/{job_id}` | Wait for rewrite |
| Approve | `POST /v1/chat/{session_id}/approve` | Accept proposed changes |
| Export | `POST /v1/documents/export` | Get rewritten prose back as .docx |

Multiple sections are processed in parallel using `ThreadPoolExecutor`. Each section gets its own independent SuperDocs session.

**The SuperDocs export is NOT the final PDF.** It is used only to extract the rewritten text. LatBuild applies that text to the original PDF.

---

## Setup

### Prerequisites
- Python 3.10+
- SuperDocs API key from [use.superdocs.app](https://use.superdocs.app)

### Install

```powershell
cd use-cases/AdhiShak3008/LatBuild/service
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

### Configure

```powershell
copy .env.example .env
# Edit .env and set SUPERDOCS_API_KEY=your-key-here
```

### Run

```powershell
# Windows PowerShell:
$env:SUPERDOCS_API_KEY = "your-key-here"
python app.py

# macOS/Linux:
export SUPERDOCS_API_KEY=your-key-here
python app.py
```

Open [http://localhost:5001](http://localhost:5001)

---

## Browser Extension

The Chrome/Edge extension provides a one-click launcher for the service.

**Install:**
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click "Load unpacked"
4. Select the `extension/` folder

The extension opens the service UI in a new tab. All PDF processing happens locally in the service — the extension is a thin launcher only.

---

## Running Tests

Tests run without a live SuperDocs API key or a real PDF.

```powershell
cd use-cases/AdhiShak3008/LatBuild/service
pytest ../tests/ -v
```

Test coverage:
- `test_pdf_analyzer.py` — column detection, heading classification, BBox geometry
- `test_flow_mapper.py` — section detection, flow region building, DocumentFlowMap
- `test_reflow_engine.py` — text measurement, region filling, overflow, non-content invariant
- `test_section_extractor.py` — prose extraction, instruction building
- `test_docx_bridge.py` — prose ↔ .docx roundtrip, HTML text extraction

---

## Supported PDFs

- Digitally generated PDFs with selectable text
- Single and two-column layouts
- Academic papers (IEEE, ACM, Springer, etc.)
- Reports, manuals, business documents

**Not supported (explicit):**
- Scanned PDFs (no selectable text layer)
- Encrypted/password-protected PDFs
- PDFs with equations as images (not selectable text)
- Right-to-left scripts

---

## Known Limitations

1. **Font matching**: Replacement text uses standard fonts (Times-Roman, Helvetica, Courier). If the original PDF uses a proprietary embedded font, character metrics will differ slightly from the original.
2. **Reflow across page boundaries**: If a section's replacement text is dramatically longer and pushes content past the last page, a new page is added with standard margins.
3. **Figures anchored**: Inline figures that flow with text are treated as absolute-position elements. If text grows past a figure, the figure stays and the overflow is reported.
4. **Single-file PDFs**: Multi-file LaTeX projects must be compiled to a single PDF first.
5. **In-memory state**: Session state is lost on server restart (appropriate for local use).

---

## Screenshot

Upload page → section detection → select sections → enter instruction → review before/after per section → approve → fidelity report → download.

Built for the SuperDocs Engineer Task, Round 2.
