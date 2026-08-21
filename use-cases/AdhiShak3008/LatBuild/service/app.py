"""
app.py
LatBuild — SuperDocs PDF Companion
Flask service for surgical PDF editing with layout preservation.

State is held in-memory keyed by browser session UUID.
All heavy processing (PDF analysis, SuperDocs calls, patching) happens
server-side. The API key never reaches the browser.

Routes:
  GET  /                      — upload page
  GET  /sections              — detected sections page
  GET  /edit                  — instruction entry page
  GET  /review                — before/after diff review
  GET  /report                — fidelity report + download

API:
  POST /api/upload            — upload PDF, analyze, return sections
  POST /api/transform         — start SuperDocs jobs for selected sections
  GET  /api/poll/<job_id>     — poll a SuperDocs job
  POST /api/approve           — submit per-change decisions
  POST /api/apply             — apply approved rewrites to PDF
  GET  /api/download          — download the modified PDF
  GET  /api/sections          — list detected sections (JSON)
"""

import io
import os
import uuid
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import (
    Flask, request, jsonify, render_template,
    session, send_file, redirect, url_for
)

import superdocs_client as sd
from pdf_analyzer import analyze_pdf
from flow_mapper import build_flow_map
from section_extractor import extract_section, build_superdocs_instruction
from docx_bridge import prose_to_docx, extract_text_from_docx, extract_text_from_html
from reflow_engine import plan_reflow, plan_multi_section_reflow
from pdf_patcher import apply_multiple_reflows
from fidelity_checker import check_fidelity

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

# ---------------------------------------------------------------------------
# In-memory state store
# ---------------------------------------------------------------------------
# State dict keys per session:
#   pdf_path          - path to uploaded PDF (tempfile)
#   pdf_filename      - original filename
#   flow_map          - DocumentFlowMap
#   selected_sections - list of section names chosen by user
#   instruction       - user's SuperDocs instruction string
#   jobs              - dict: section_name -> job dict
#                         job dict: session_id, job_id, status,
#                                   original_text, proposed_text,
#                                   pending_changes, approved
#   output_path       - path to patched PDF tempfile
#   fidelity_report   - FidelityReport.as_dict()
# ---------------------------------------------------------------------------
_state = {}
_state_lock = threading.Lock()


def _get_state():
    sid = session.get("sid")
    if not sid or sid not in _state:
        sid = str(uuid.uuid4())
        session["sid"] = sid
        with _state_lock:
            _state[sid] = {
                "pdf_path": None,
                "pdf_filename": None,
                "flow_map": None,
                "selected_sections": [],
                "instruction": "",
                "jobs": {},
                "output_path": None,
                "fidelity_report": None,
            }
    return _state[session["sid"]]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    state = _get_state()
    return render_template(
        "index.html",
        has_pdf=bool(state["pdf_path"]),
        pdf_filename=state["pdf_filename"],
    )


@app.route("/sections")
def sections_page():
    state = _get_state()
    if not state["flow_map"]:
        return redirect(url_for("index"))
    fm = state["flow_map"]
    editable = fm.editable_sections()
    return render_template(
        "sections.html",
        sections=editable,
        pdf_filename=state["pdf_filename"],
    )


@app.route("/edit")
def edit_page():
    state = _get_state()
    if not state["selected_sections"] or not state["flow_map"]:
        return redirect(url_for("sections_page"))
    fm = state["flow_map"]
    selected = [
        fm.get_section(name)
        for name in state["selected_sections"]
        if fm.get_section(name)
    ]
    return render_template(
        "edit.html",
        selected_sections=selected,
        instruction=state["instruction"],
    )


@app.route("/review")
def review_page():
    state = _get_state()
    if not state["jobs"]:
        return redirect(url_for("edit_page"))
    return render_template(
        "review.html",
        jobs=state["jobs"],
        selected_sections=state["selected_sections"],
    )


@app.route("/report")
def report_page():
    state = _get_state()
    fidelity = state.get("fidelity_report")
    if not fidelity:
        return redirect(url_for("index"))
    return render_template(
        "report.html",
        report=fidelity,
        can_download=bool(state.get("output_path")),
        pdf_filename=state["pdf_filename"],
    )


# ---------------------------------------------------------------------------
# API: Upload
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Upload a PDF, analyze it, build the flow map.
    Returns the list of detected editable sections.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No filename."}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported."}), 400

    state = _get_state()

    # Clean up previous tempfile
    if state["pdf_path"] and os.path.exists(state["pdf_path"]):
        try:
            os.unlink(state["pdf_path"])
        except OSError:
            pass

    # Save to temp file (pdfplumber/pymupdf need a file path)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(f.read())
    tmp.close()

    try:
        doc = analyze_pdf(tmp.name)
        flow_map = build_flow_map(doc)
    except Exception as e:
        os.unlink(tmp.name)
        return jsonify({"error": f"PDF analysis failed: {str(e)}"}), 422

    state["pdf_path"] = tmp.name
    state["pdf_filename"] = f.filename
    state["flow_map"] = flow_map
    state["selected_sections"] = []
    state["jobs"] = {}
    state["output_path"] = None
    state["fidelity_report"] = None

    editable = flow_map.editable_sections()
    return jsonify({
        "ok": True,
        "filename": f.filename,
        "page_count": doc.page_count,
        "sections": [
            {
                "name": s.title,
                "id": s.id,
                "level": s.level,
                "parent_id": s.parent_id,
                "pages": s.pages,
                "word_count": s.word_count,
                "is_editable": s.is_editable,
            }
            for s in editable
        ],
    })


# ---------------------------------------------------------------------------
# API: Sections (JSON)
# ---------------------------------------------------------------------------

@app.route("/api/sections", methods=["GET"])
def api_sections():
    state = _get_state()
    if not state["flow_map"]:
        return jsonify({"error": "No PDF uploaded."}), 400
    fm = state["flow_map"]
    editable = fm.editable_sections()
    return jsonify({
        "sections": [
            {
                "name": s.title,
                "id": s.id,
                "level": s.level,
                "parent_id": s.parent_id,
                "pages": s.pages,
                "word_count": s.word_count,
            }
            for s in editable
        ]
    })


# ---------------------------------------------------------------------------
# API: Select sections + start transform
# ---------------------------------------------------------------------------

@app.route("/api/transform", methods=["POST"])
def api_transform():
    """
    Start SuperDocs editing jobs for the selected sections.
    Each section gets its own independent SuperDocs session.
    Jobs run in parallel via ThreadPoolExecutor.
    """
    data = request.get_json() or {}
    section_names = data.get("sections", [])
    instruction = data.get("instruction", "").strip()

    if not section_names:
        return jsonify({"error": "No sections selected."}), 400
    if not instruction:
        return jsonify({"error": "Instruction is required."}), 400

    state = _get_state()
    if not state["flow_map"]:
        return jsonify({"error": "No PDF uploaded."}), 400

    fm = state["flow_map"]
    state["selected_sections"] = section_names
    state["instruction"] = instruction
    state["jobs"] = {}
    state["output_path"] = None
    state["fidelity_report"] = None

    sections_to_edit = []
    for name in section_names:
        sec = fm.get_section(name)
        if not sec:
            return jsonify({"error": f"Section '{name}' not found."}), 400
        if not sec.is_editable:
            return jsonify({"error": f"Section '{name}' is not editable."}), 400
        sections_to_edit.append(sec)

    # Start one SuperDocs job per section (parallel)
    def start_job(section):
        extracted = extract_section(section, fm.grammar)
        full_instruction = build_superdocs_instruction(instruction, extracted)
        docx_bytes = prose_to_docx(extracted.clean_text, title=section.title)
        conv_session_id = f"latbuild-{uuid.uuid4().hex[:12]}"

        try:
            upload_result = sd.upload_document(docx_bytes, "section.docx", conv_session_id)
            job_id = sd.start_transformation(
                conv_session_id, upload_result["html"], full_instruction
            )
            return section.title, {
                "session_id": conv_session_id,
                "job_id": job_id,
                "status": "in_progress",
                "original_text": extracted.clean_text,
                "proposed_text": None,
                "pending_changes": [],
                "approved": False,
                "error": None,
            }
        except sd.SuperDocsError as e:
            return section.title, {
                "session_id": conv_session_id,
                "job_id": None,
                "status": "failed",
                "original_text": extracted.clean_text,
                "proposed_text": None,
                "pending_changes": [],
                "approved": False,
                "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=min(4, len(sections_to_edit))) as executor:
        futures = {executor.submit(start_job, s): s for s in sections_to_edit}
        for future in as_completed(futures):
            name, job = future.result()
            state["jobs"][name] = job

    return jsonify({
        "ok": True,
        "jobs": {
            name: {"job_id": j["job_id"], "status": j["status"]}
            for name, j in state["jobs"].items()
        }
    })


# ---------------------------------------------------------------------------
# API: Poll
# ---------------------------------------------------------------------------

@app.route("/api/poll/<job_id>", methods=["GET"])
def api_poll(job_id):
    """Poll a single SuperDocs job."""
    state = _get_state()

    # Find which section this job belongs to
    section_name = None
    for name, job in state["jobs"].items():
        if job.get("job_id") == job_id:
            section_name = name
            break

    if not section_name:
        return jsonify({"error": "Job not found."}), 404

    job = state["jobs"][section_name]
    if job["status"] in ("failed", "cancelled"):
        return jsonify({"status": job["status"], "error": job.get("error")})

    try:
        job_state = sd.poll_job(job_id)
    except sd.SuperDocsError as e:
        return jsonify({"error": str(e)}), 502

    status = job_state["status"]
    job["status"] = status

    if status == "awaiting_approval":
        job["pending_changes"] = job_state["pending_changes"]
        return jsonify({
            "status": "awaiting_approval",
            "section": section_name,
            "pending_changes": job_state["pending_changes"],
        })

    if status == "completed":
        result = job_state.get("result") or {}
        final_html = sd.get_final_html(result) or ""

        # Extract the rewritten text from the result
        if final_html:
            proposed_text = extract_text_from_html(final_html)
        else:
            # Fall back to export
            try:
                docx_bytes = sd.export_document(job["session_id"])
                proposed_text = extract_text_from_docx(docx_bytes)
            except sd.SuperDocsError:
                proposed_text = job["original_text"]

        job["proposed_text"] = proposed_text
        return jsonify({
            "status": "completed",
            "section": section_name,
            "proposed_text": proposed_text,
        })

    if status == "failed":
        job["error"] = job_state.get("error", "Unknown error")
        return jsonify({
            "status": "failed",
            "section": section_name,
            "error": job["error"],
        })

    return jsonify({"status": status, "section": section_name})


# ---------------------------------------------------------------------------
# API: Approve changes
# ---------------------------------------------------------------------------

@app.route("/api/approve", methods=["POST"])
def api_approve():
    """Submit per-change decisions for a SuperDocs job."""
    data = request.get_json() or {}
    job_id = data.get("job_id")
    session_id = data.get("session_id")
    decisions = data.get("decisions", [])

    if not job_id or not session_id:
        return jsonify({"error": "job_id and session_id are required."}), 400

    try:
        sd.approve_changes(session_id, job_id, decisions)
    except sd.SuperDocsError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Apply — patch the PDF
# ---------------------------------------------------------------------------

@app.route("/api/apply", methods=["POST"])
def api_apply():
    """
    Apply approved rewrites to the original PDF.
    Runs the reflow engine and patcher.
    Returns the fidelity report.
    """
    data = request.get_json() or {}
    # approved_texts: dict of section_name -> approved_text
    # If not provided, uses the proposed_text from each job
    approved_texts = data.get("approved_texts", {})

    state = _get_state()
    if not state["pdf_path"]:
        return jsonify({"error": "No PDF uploaded."}), 400
    if not state["jobs"]:
        return jsonify({"error": "No editing jobs found."}), 400

    fm = state["flow_map"]

    # Collect section + replacement pairs
    edits = []
    for section_name, job in state["jobs"].items():
        if not job.get("approved", True):
            continue  # Skip rejected sections
        replacement = approved_texts.get(section_name) or job.get("proposed_text")
        if not replacement:
            continue
        section = fm.get_section(section_name)
        if section:
            edits.append((section, replacement))

    if not edits:
        return jsonify({"error": "No approved edits to apply."}), 400

    # Plan reflow for all sections
    reflow_results = plan_multi_section_reflow(edits, fm)

    # Check for unresolvable overflows
    unresolvable = [
        r for r in reflow_results if not r.can_apply
    ]
    if unresolvable:
        warnings = []
        for r in unresolvable:
            for w in r.overflow_warnings:
                warnings.append(w.description)
        return jsonify({
            "error": "Some edits cannot be applied due to overflow.",
            "warnings": warnings,
        }), 422

    # Write output to temp file
    if state["output_path"] and os.path.exists(state["output_path"]):
        try:
            os.unlink(state["output_path"])
        except OSError:
            pass

    out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    out_tmp.close()
    output_path = out_tmp.name

    try:
        patch_report = apply_multiple_reflows(
            original_pdf_path=state["pdf_path"],
            reflow_results=reflow_results,
            grammar=fm.grammar,
            non_content=fm.non_content,
            output_path=output_path,
        )
    except Exception as e:
        return jsonify({"error": f"PDF patching failed: {str(e)}"}), 500

    state["output_path"] = output_path

    # Run fidelity check
    # Merge reflow results into one for the checker
    from reflow_engine import ReflowResult
    merged_affected = sorted(set(p for r in reflow_results for p in r.affected_pages))
    merged_result = ReflowResult(
        section_patches=[p for r in reflow_results for p in r.section_patches],
        downstream_shifts=[s for r in reflow_results for s in r.downstream_shifts],
        overflow_warnings=[w for r in reflow_results for w in r.overflow_warnings],
        delta_lines=sum(r.delta_lines for r in reflow_results),
        delta_points=sum(r.delta_points for r in reflow_results),
        affected_pages=merged_affected,
        unaffected_pages=[],
        can_apply=True,
    )

    try:
        fidelity = check_fidelity(
            original_path=state["pdf_path"],
            modified_path=output_path,
            reflow_result=merged_result,
            selected_section_names=[e[0].title for e in edits],
            non_content=fm.non_content,
        )
        state["fidelity_report"] = fidelity.as_dict()
    except Exception as e:
        # Fidelity check failure does not block the download
        state["fidelity_report"] = {
            "pass": None,
            "summary": f"Fidelity check could not complete: {str(e)}",
            "selected_sections": [e[0].title for e in edits],
            "pages_total": 0,
            "pages_modified": merged_affected,
            "non_target_pages_changed": [],
            "figures_original": 0,
            "figures_modified": 0,
            "tables_original": 0,
            "tables_modified": 0,
            "overflow_detected": False,
            "page_count_changed": False,
        }

    patch_warnings = [w.description for w in patch_report.warnings]

    return jsonify({
        "ok": True,
        "pages_modified": patch_report.pages_modified,
        "patch_warnings": patch_warnings,
        "fidelity": state["fidelity_report"],
    })


# ---------------------------------------------------------------------------
# API: Download
# ---------------------------------------------------------------------------

@app.route("/api/download", methods=["GET"])
def api_download():
    """Download the modified PDF."""
    state = _get_state()
    if not state["output_path"] or not os.path.exists(state["output_path"]):
        return jsonify({"error": "No modified PDF available."}), 404

    original_name = state["pdf_filename"] or "document.pdf"
    stem = original_name.replace(".pdf", "")
    download_name = f"{stem}_latbuild.pdf"

    return send_file(
        state["output_path"],
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
