"""Render the maintained Markdown guide as a five-page PDF (development only)."""

import html
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def inline(text):
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def flowchart():
    # A print-friendly rendering of the Mermaid workflow in TECHNICAL_GUIDE.md.
    nodes = [
        (5, 15, "VERIFY_ID", "3 PII types; rep needs consent"),
        (185, 15, "RESOLVE_INTENT", "hints → confirm claim"),
        (365, 15, "PROCESS_CASE", "authorized facts + AI"),
        (545, 15, "POST_PROCESS", "summary / next steps"),
        (5, 130, "Async human request · H", "AI or wait-only; each acked"),
        (245, 130, "Email enquiry · E", "verified; simulated reply"),
        (545, 130, "Send / Skip", "customer sends or skips"),
    ]
    boxes = "".join(
        f'<rect x="{x}" y="{y}" width="170" height="65" rx="8"/>'
        f'<text x="{x + 85}" y="{y + 26}" class="label">{label}</text>'
        f'<text x="{x + 85}" y="{y + 48}">{detail}</text>'
        for x, y, label, detail in nodes
    )
    return f"""<svg viewBox="0 0 725 240" role="img" aria-label="SOP workflow with human and email side paths">
      <defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7Z" fill="#426588"/></marker></defs>
      <style>rect{{fill:#eff5fa;stroke:#9bb0c4}}text{{font-size:11px;text-anchor:middle;fill:#18334d}}.label{{font-size:12px;font-weight:700}}.edge{{fill:none;stroke:#426588;stroke-width:1.5;marker-end:url(#arrow)}}</style>
      {boxes}
      <path class="edge" d="M175 47 H183 M355 47 H363 M535 47 H543 M630 80 V128"/>
      <path class="edge" stroke-dasharray="4 3" d="M90 80 V128 M270 80 V105 H330 V128"/>
      <path class="edge" d="M90 195 V218 H15 V92 H75 V82"/>
      <text x="252" y="235">Unverified: no claim reads; humans never bypass verification; new questions return to processing</text>
    </svg>"""


def render_section(source):
    lines = source.strip().splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            language = line[3:]
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            blocks.append(
                flowchart() if language == "mermaid" else "<pre>" + html.escape("\n".join(code)) + "</pre>"
            )
        elif line.startswith("| "):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [c.strip() for c in lines[index].strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", c) for c in cells):
                    tag = "th" if not rows else "td"
                    rows.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
                index += 1
            blocks.append("<table>" + "".join(rows) + "</table>")
            continue
        elif line.startswith("## "):
            blocks.append("<h2>" + inline(line[3:]) + "</h2>")
        elif line.startswith("# "):
            blocks.append("<h1>" + inline(line[2:]) + "</h1>")
        elif line.strip():
            blocks.append("<p>" + inline(line) + "</p>")
        index += 1
    return "\n".join(blocks)


def main():
    source = (DOCS / "TECHNICAL_GUIDE.md").read_text()
    intro, *sections = re.split(r"(?=^## )", source, flags=re.M)
    assert len(sections) == 5
    pages = []
    for index, section in enumerate(sections):
        body = render_section((intro if index == 0 else "") + section)
        pages.append(
            f'<section><div class="running">INSURANCE SOP · TECHNICAL GUIDE 1.7.5</div>{body}</section>'
        )
    document = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><title>Insurance SOP Technical Guide</title>
    <style>
    @page {size:A4;margin:13mm 15mm 15mm}
    * {box-sizing:border-box} body{margin:0;color:#1e293b;font:9.2pt/1.55 sans-serif}
    section{break-after:page} section:last-child{break-after:auto}
    .running{font-size:8pt;color:#64748b;letter-spacing:.5px;border-bottom:1px solid #cbd5e1;padding-bottom:7px}
    h1{font-size:20pt;line-height:1.3;margin:12px 0 8px} h2{font-size:15pt;color:#17486a;margin:12px 0}
    p{margin:8px 0} strong{color:#153954} code{font-size:.9em;overflow-wrap:anywhere}
    table{border-collapse:collapse;width:100%;font-size:8pt;line-height:1.4;margin:10px 0}
    th,td{border:1px solid #cbd5e1;padding:6px 7px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
    th{background:#eaf1f7} tr{break-inside:avoid} pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:10px;font-size:7.5pt}
    svg{width:100%;height:auto;margin:6px 0} svg text{font-family:sans-serif}
    </style>"""
        + "".join(pages)
        + "</html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(document)
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(DOCS / "TECHNICAL_GUIDE.pdf"),
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template='<div style="width:100%;text-align:center;font-size:8px;color:#64748b">Insurance SOP · <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
        )
        browser.close()
    print(DOCS / "TECHNICAL_GUIDE.pdf")


if __name__ == "__main__":
    main()
