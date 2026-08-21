"""Quick section check — run from LatBuild/ folder with your PDF in demo/."""
import sys, os
sys.path.insert(0, 'service')
from pdf_analyzer import analyze_pdf
from flow_mapper import build_flow_map

pdf_path = None
for f in sorted(os.listdir('demo')):
    if f.endswith('.pdf'):
        pdf_path = os.path.join('demo', f)
        break

if not pdf_path:
    print("No PDF found in demo/ folder.")
    print("Copy your IEEE paper there: demo/paper.pdf")
    sys.exit(1)

print(f"Analyzing: {pdf_path}\n")
doc = analyze_pdf(pdf_path)
fm = build_flow_map(doc)

print(f"Pages: {doc.page_count}  |  Total blocks: {len(doc.all_blocks)}")
print(f"Sections detected: {len(fm.sections)}\n")
print(f"{'#':<3} {'Editable':<8} {'Section name':<35} {'Pages':<12} {'Words':<6}")
print("-" * 75)
for i, s in enumerate(fm.sections):
    editable = "YES" if s.is_editable else "no"
    pages_str = str(s.pages[0]) if len(s.pages) == 1 else f"{s.pages[0]}-{s.pages[-1]}" if s.pages else "?"
    print(f"{i+1:<3} {editable:<8} {s.name[:35]:<35} {pages_str:<12} {s.word_count:<6}")

print()
print("=== TEXT PREVIEW PER SECTION (first 200 chars) ===")
for s in fm.sections:
    print(f"\n--- {s.name} ---")
    print(s.full_text[:200].replace("\n", " "))
