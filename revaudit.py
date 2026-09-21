#!/usr/bin/env python3
"""
revaudit.py - release / drawing / production-file audit for Onshape assemblies.

For each assembly part number it answers:
  * is every part's drawing released?
  * does any drawing lag its part revision?
  * does the BOM list one part number at two different revisions?
  * which BOM lines are obsolete or not revision-managed?
  * has the live workspace drifted from the released version?
  * does every sheet part have a DXF in the production folder?
  * does every tube part have a SAT file in the production folder?

Usage:
  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" ^
      --sat-dir "<share>\\SAT FILES" --open

Credentials come from the environment or a .env file beside this script:
  ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY, ONSHAPE_BASE_URL

Standard library only - no pip install required.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import config
from audit_core import FileIndex, audit_assembly
from onshape_client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError
from pdf_export import (export_assembly_drawings, export_assembly_step_file,
                        export_counts, zip_pdfs)

APP_NAME = "RevAudit"
AUTHOR = "Sergio Alexo"
AUTHOR_URL = "https://sergioalexo.com"

# Bumped whenever the saved report data changes shape in a way the renderer
# has to know about. Older files are still rendered - every lookup is a
# .get() - this just names the layout.
REPORT_FORMAT = 1


def credit_html(prefix=""):
    """Byline shown in the report footer and on the web UI."""
    return (
        f"{prefix}Developed by <a href='{AUTHOR_URL}' target='_blank' "
        f"rel='noopener noreferrer' style='color:var(--accent);"
        f"text-decoration:none'>{AUTHOR}</a>"
    )


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_dotenv(path):
    """Load KEY=VALUE lines from a .env file into os.environ (no overwrite)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#ffffff; --panel:#f7f8fa; --panel2:#eef0f4; --fg:#12151a; --muted:#5d6673;
  --line:#d9dee6; --accent:#2f6fed; --crit:#c02b2b; --crit-bg:#fdeaea;
  --warn:#9a6400; --warn-bg:#fdf3e0; --ok:#1c7c4a; --ok-bg:#e8f6ee;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1216; --panel:#171b21; --panel2:#1f242c; --fg:#e7ebf0; --muted:#98a2b0;
    --line:#2a313a; --accent:#6f9bff; --crit:#ff6b6b; --crit-bg:#2a1616;
    --warn:#e8b45c; --warn-bg:#2a2213; --ok:#5fd39a; --ok-bg:#12251b;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1216; --panel:#171b21; --panel2:#1f242c; --fg:#e7ebf0; --muted:#98a2b0;
  --line:#2a313a; --accent:#6f9bff; --crit:#ff6b6b; --crit-bg:#2a1616;
  --warn:#e8b45c; --warn-bg:#2a2213; --ok:#5fd39a; --ok-bg:#12251b;
}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 80px;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:20px;margin:40px 0 12px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:26px 0 8px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600}
.sub{color:var(--muted);margin:0 0 28px;font-size:13px}
.meta{display:flex;flex-wrap:wrap;gap:8px 20px;margin:10px 0 18px;font-size:13px;color:var(--muted)}
.meta b{color:var(--fg);font-weight:600}
.verdict{padding:14px 16px;border-radius:8px;margin:16px 0;font-weight:600;
  border:1px solid transparent}
.verdict.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok)}
.verdict.bad{background:var(--crit-bg);color:var(--crit);border-color:var(--crit)}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 4px}
.pill{padding:4px 11px;border-radius:999px;font-size:12.5px;font-weight:600;
  background:var(--panel2);border:1px solid var(--line)}
.pill.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn)}
.pill.crit{background:var(--crit-bg);color:var(--crit);border-color:var(--crit)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:10px 0 4px}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{background:var(--panel);font-weight:600;font-size:12px;text-transform:uppercase;
  letter-spacing:.04em;color:var(--muted);position:sticky;top:0}
tbody tr:hover{background:var(--panel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
code,.mono{font-family:ui-monospace,"Cascadia Code",Consolas,monospace;font-size:12.5px}
code{cursor:pointer;border-radius:3px;padding:0 2px;transition:background .12s}
code:hover{background:var(--panel2)}
code.copied{background:var(--ok-bg);color:var(--ok)}
code.copied::after{content:" copied";font-size:11px}
a.os{color:var(--accent);text-decoration:none;font-size:12px;margin-left:4px;
  opacity:.65;vertical-align:baseline}
a.os:hover{opacity:1;text-decoration:underline}
.meta a{color:var(--accent);text-decoration:none}
.meta a:hover{text-decoration:underline}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11.5px;
  font-weight:700;letter-spacing:.03em}
.tag.RELEASED{background:var(--ok-bg);color:var(--ok)}
.tag.OBSOLETE{background:var(--crit-bg);color:var(--crit)}
.tag.OTHER{background:var(--warn-bg);color:var(--warn)}
.none{color:var(--muted);font-style:italic;margin:6px 0}
.good{background:var(--ok-bg);color:var(--ok);border:1px solid var(--ok);
  border-radius:7px;padding:9px 13px;margin:8px 0;font-weight:600;font-size:13.5px}
.good::before{content:"✓  "}
td .miss{color:var(--crit);font-weight:700}
tr:has(.miss){background:var(--crit-bg)}
details{margin:14px 0;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);padding:0 14px}
summary{cursor:pointer;padding:11px 0;font-weight:600;font-size:14px}
details[open]{padding-bottom:10px}
.err{background:var(--crit-bg);color:var(--crit);padding:11px 14px;border-radius:8px;
  margin:10px 0;font-size:13.5px}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px}
"""


def pretty_configuration(encoded):
    """'HEIGHT=1.016+meter;List_7yos=STEEL' -> 'HEIGHT=1.016 meter, STEEL'.
    List_* keys are opaque Onshape ids, so only their values are shown."""
    parts = []
    for item in (encoded or "").split(";"):
        key, _, val = item.partition("=")
        val = val.replace("+", " ")
        parts.append(val if key.startswith("List_") else f"{key}={val}" if val else key)
    return ", ".join(p for p in parts if p)


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value))


def state_tag(state):
    cls = state if state in ("RELEASED", "OBSOLETE") else "OTHER"
    return f'<span class="tag {cls}">{esc(state or "-")}</span>'


def os_link(url, title="Open in Onshape", text="&#8599;"):
    """A small arrow that opens url in a new tab - the Onshape document at
    the exact version and configuration the audit looked at. Empty when the
    record carried no link (older saved reports, mock data)."""
    if not url:
        return ""
    return (f'<a class="os" href="{esc(url)}" target="_blank" rel="noopener noreferrer" '
            f'title="{esc(title)}">{text}</a>')


def pn_cell(pn, url=None, title="Open the part in Onshape", extra_class=""):
    """Part number as a click-to-copy token, followed by its Onshape link."""
    cls = f' class="{extra_class}"' if extra_class else ""
    return f"<code{cls}>{esc(pn)}</code>{os_link(url, title)}"


def rev_cell(rev, url=None, title="Open this revision in Onshape", bold=False):
    text = f"<b>{esc(rev)}</b>" if bold else esc(rev)
    return f"{text}{os_link(url, title)}"


def table(headers, rows, aligns=None):
    if not rows:
        return ""
    aligns = aligns or [""] * len(headers)
    out = ['<div class="scroll"><table><thead><tr>']
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for value, align in zip(row, aligns):
            cls = ' class="num"' if align == "num" else ""
            out.append(f"<td{cls}>{value}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def section(title, body, empty_note=None):
    """empty_note is always a clean/passing result, so it renders in green."""
    if not body:
        if not empty_note:
            return ""
        return f"<h3>{esc(title)}</h3><p class=\"good\">{esc(empty_note)}</p>"
    return f"<h3>{esc(title)}</h3>{body}"


def render_assembly(result):
    out = []
    pn = result["partNumber"]
    out.append(f'<h2 id="{esc(pn)}">{esc(pn)}</h2>')

    for err in result.get("errors", []):
        out.append(f'<div class="err">{esc(err)}</div>')

    asm = result.get("assembly")
    if not asm:
        return "".join(out)

    dwg = asm["drawingRevision"]
    out.append(
        '<div class="meta">'
        f'<span>Document <b>{esc(asm["documentName"])}</b></span>'
        f'<span>Assembly rev <b>{esc(asm["revision"])}</b>'
        f'{os_link(asm.get("url"), "Open the released assembly in Onshape")}</span>'
        f'<span>Released <b>{esc(asm["releaseDate"] or "-")}</b></span>'
        f'<span>Assembly drawing <b>{esc(dwg or "none")}</b>'
        f'{os_link(asm.get("drawingUrl"), "Open the assembly drawing in Onshape")}</span>'
        f'<span>Source <b>{esc(result.get("bomSource", ""))}</b></span>'
        + (f'<span>Configuration <b>{esc(pretty_configuration(asm["configuration"]))}</b></span>'
           if asm.get("configuration") else "")
        + "</div>"
    )

    f = result.get("findings", {})
    statuses = result.get("partStatuses", {})
    total_parts = len(statuses)
    no_drawing = f.get("noDrawing", [])
    with_drawing = total_parts - len(no_drawing)

    if total_parts:
        if no_drawing:
            out.append(
                f'<div class="verdict bad">{with_drawing} of {total_parts} parts have a '
                f'released drawing &mdash; {len(no_drawing)} have none.</div>'
            )
        else:
            out.append(
                f'<div class="verdict ok">All {total_parts} parts have a released '
                "drawing.</div>"
            )

    counts = result.get("stateCounts", {})
    pills = []
    for key in ("RELEASED", "OBSOLETE"):
        if counts.get(key):
            cls = "ok" if key == "RELEASED" else "crit"
            pills.append(f'<span class="pill {cls}">{counts[key]} {key.lower()}</span>')
    for key, val in sorted(counts.items()):
        if key not in ("RELEASED", "OBSOLETE"):
            pills.append(f'<span class="pill warn">{val} {esc(key.lower())}</span>')
    rows = result.get("rows", [])
    pills.append(f'<span class="pill">{len(rows)} BOM lines</span>')
    pills.append(f'<span class="pill">{total_parts} unique parts</span>')
    out.append('<div class="pills">' + "".join(pills) + "</div>")

    # 1. no drawing
    body = table(
        ["Part", "Name", "Material", "Qty", "Part rev", "Source document"],
        [
            [pn_cell(e["pn"], e.get("partUrl")), esc(e["name"]), esc(e["material"]),
             esc(e["qty"]), rev_cell(e["partRev"], e.get("partUrl")), esc(e["sourceDoc"])]
            for e in no_drawing
        ],
        ["", "", "", "num", "", ""],
    )
    out.append(section("Parts with no drawing at all", body,
                       "None - every part has a released drawing."))

    # 2. drawing behind part
    behind = f.get("drawingBehindPart", [])
    body = table(
        ["Part", "Name", "Part rev", "Newest drawing rev", "Qty"],
        [
            [pn_cell(e["pn"], e.get("partUrl")), esc(e["name"]),
             rev_cell(e["partRev"], e.get("partUrl"), "Open the part revision in Onshape"),
             rev_cell(e["drawingRev"], e.get("drawingUrl"),
                      "Open the drawing revision in Onshape", bold=True),
             esc(e["qty"])]
            for e in behind
        ],
        ["", "", "", "", "num"],
    )
    out.append(section("Drawing older than the part it documents", body,
                       "None - no drawing lags its part revision."))

    # 3. duplicate part numbers
    dups = [d for d in f.get("duplicatePartNumbers", []) if d["mixedRevisions"]]
    dup_rows = []
    for d in dups:
        detail = " &nbsp;/&nbsp; ".join(
            f'item {esc(l["item"])}: rev <b>{esc(l["rev"])}</b>'
            f'{os_link(l.get("url"), "Open this BOM line in Onshape")} '
            f'&times;{esc(l["qty"])} {state_tag(l["state"])}'
            for l in d["lines"]
        )
        total = sum((l["qty"] or 0) for l in d["lines"])
        first_url = next((l.get("url") for l in d["lines"] if l.get("url")), None)
        dup_rows.append([pn_cell(d["pn"], first_url, "Open in Onshape"), detail, esc(total)])
    out.append(section(
        "Same part number at two or more revisions",
        table(["Part", "Lines", "Total qty"], dup_rows, ["", "", "num"]),
        "None - every part number appears at a single revision.",
    ))

    # 4. obsolete lines
    obs = f.get("obsoleteLines", [])
    orphans = [o for o in obs if not o["supersededInBom"]]
    body = table(
        ["Item", "Part", "Name", "Rev", "Qty", "Superseded in this BOM?"],
        [
            [esc(o["item"]), pn_cell(o["pn"], o.get("url"), "Open this BOM line in Onshape"),
             esc(o["name"]), esc(o["rev"]), esc(o["qty"]),
             "yes" if o["supersededInBom"] else "<b>no</b>"]
            for o in obs
        ],
        ["num", "", "", "", "num", ""],
    )
    title = f"Obsolete BOM lines ({len(orphans)} with no newer line in this BOM)"
    out.append(section(title, body, "None - no obsolete lines."))

    # 5. BOM rev stale
    stale = f.get("bomRevStale", [])
    body = table(
        ["Part", "Name", "Rev in BOM", "Current part rev", "Qty"],
        [
            [pn_cell(e["pn"], e.get("partUrl")), esc(e["name"]),
             esc(", ".join(e.get("bomRevs") or [])),
             rev_cell(e["partRev"], e.get("partUrl"), "Open the current part revision",
                      bold=True),
             esc(e["qty"])]
            for e in stale
        ],
        ["", "", "", "", "num"],
    )
    out.append(section("BOM pins a revision older than the current part", body,
                       "None - the BOM references current part revisions."))

    # 6. not revision managed
    unmanaged = f.get("notRevisionManaged", [])
    body = table(
        ["Item", "Part", "Name", "State", "Qty"],
        [
            [esc(u["item"]), pn_cell(u["pn"], u.get("url"), "Open this BOM line in Onshape"),
             esc(u["name"]), state_tag(u["state"]), esc(u["qty"])]
            for u in unmanaged
        ],
        ["num", "", "", "", "num"],
    )
    out.append(section("Lines with no released revision", body,
                       "None - every line is revision-managed."))

    # 7. workspace drift
    drift = f.get("workspaceDrift")
    if drift:
        if drift["drifted"]:
            ws = ", ".join(f"{v} {k.lower()}" for k, v in sorted(drift["workspaceCounts"].items()))
            vs = ", ".join(f"{v} {k.lower()}" for k, v in sorted(drift["versionCounts"].items()))
            body = (
                f'<p>The live workspace differs from the released version.</p>'
                f'<div class="scroll"><table><thead><tr><th>Source</th><th>Lines</th>'
                f'<th>States</th></tr></thead><tbody>'
                f'<tr><td>Released Rev {esc(result["assembly"]["revision"])}</td>'
                f'<td class="num">{drift["versionLines"]}</td><td>{esc(vs)}</td></tr>'
                f'<tr><td>Main workspace</td><td class="num">{drift["workspaceLines"]}</td>'
                f'<td>{esc(ws)}</td></tr></tbody></table></div>'
            )
        else:
            body = ('<p class="good">The live workspace matches the released version '
                    "line for line.</p>")
        out.append(f"<h3>Workspace vs released version</h3>{body}")

    # 8. production-file coverage - DXF, SAT, or whatever else was checked
    for check_name, fc in (f.get("files") or {}).items():
        out.append(f"<h3>{esc(check_name)} coverage</h3>")
        stale_note = ""
        if fc.get("source") == "uploaded index" and fc.get("generated"):
            stale_note = f'<span>Index generated <b>{esc(fc["generated"])}</b></span>'
        out.append(
            f'<div class="meta"><span>Folder <b>{esc(fc["folder"])}</b></span>'
            f'<span>Files indexed <b>{fc["filesIndexed"]}</b></span>'
            f'<span>Parts expecting {esc(check_name)} <b>{fc["expected"]}</b></span>'
            f'<span>Material pattern <code>{esc(fc["materialPattern"])}</code></span>'
            f'{stale_note}</div>'
        )
        if fc["missing"]:
            n = len(fc["missing"])
            plural = "s" if n != 1 else ""
            verb = "are" if n != 1 else "is"
            out.append(f'<div class="verdict bad">{n} part{plural} {verb} missing '
                       f'a {esc(check_name)} file.</div>')
            out.append(table(
                ["Part", "Name", "Material", "Qty"],
                [
                    [pn_cell(m["pn"], m.get("partUrl"), extra_class="miss"),
                     f'<span class="miss">{esc(m["name"])}</span>',
                     esc(m["material"]), esc(m["qty"])]
                    for m in fc["missing"]
                ],
                ["", "", "", "num"],
            ))
        else:
            out.append(f'<p class="good">Every part expecting {esc(check_name)} has one.</p>')
        if fc.get("multiple"):
            rows_m = [
                [pn_cell(m["pn"], m.get("partUrl")), esc(m["name"]),
                 f'<b>{len(m["files"])}</b>',
                 " &nbsp;".join(f"<code>{esc(fn)}</code>" for fn in m["files"])]
                for m in fc["multiple"]
            ]
            out.append(
                f"<details open><summary>Parts with more than one {esc(check_name)} "
                f"file ({len(fc['multiple'])}) &mdash; check the extras are variants, "
                "not stale duplicates</summary>"
                + table(["Part", "Name", "Files", ""], rows_m, ["", "", "num", ""])
                + "</details>"
            )
        if fc["unexpected"]:
            rows_u = [
                [pn_cell(u["pn"], u.get("partUrl")), esc(u["material"]),
                 esc(", ".join(u["files"][:4]))]
                for u in fc["unexpected"]
            ]
            out.append(
                f"<details><summary>Parts with a {esc(check_name)} file that were not "
                f"expected to need one ({len(fc['unexpected'])})</summary>"
                + table(["Part", "Material", "Files"], rows_u) + "</details>"
            )

    # 9. drawing PDF export, if requested
    export = result.get("drawingExport")
    if export:
        entries = export.get("entries", [])
        exported, skipped, n_failed = export_counts(entries)
        failed = [e for e in entries if e.get("error")]
        out.append("<h3>Drawing PDF export</h3>")
        if exported or skipped:
            cls = "bad" if failed else "ok"
            bits = []
            if exported:
                bits.append(f"{exported} drawing PDF{'s' if exported != 1 else ''} exported")
            if skipped:
                bits.append(f"{skipped} already in the folder at the current revision "
                            "(not fetched again)")
            if failed:
                bits.append(f"{n_failed} failed")
            out.append(f'<div class="verdict {cls}">{", ".join(bits)}.</div>')
        elif failed:
            out.append(f'<div class="verdict bad">All {len(failed)} drawing exports '
                       "failed.</div>")
        if export.get("zipUrl"):
            out.append(
                f'<p><a href="{esc(export["zipUrl"])}" style="color:var(--accent)">'
                f'Download the drawing package (.zip)</a></p>'
            )
        elif export.get("zipPath"):
            out.append(f'<p>Saved to <code>{esc(export["zipPath"])}</code></p>')
        if failed:
            out.append("<details><summary>Failed exports "
                       f"({len(failed)})</summary>" + table(
                ["Part", "Rev", "Error"],
                [[f'<code>{esc(e["pn"])}</code>', esc(e["rev"]), esc(e["error"])]
                 for e in failed],
            ) + "</details>")

    # 10. STEP export, if requested
    step = result.get("stepExport")
    if step:
        out.append("<h3>STEP export</h3>")
        if step.get("error"):
            out.append(f'<div class="verdict bad">Export failed: {esc(step["error"])}</div>')
        elif step.get("skipped"):
            out.append('<div class="verdict ok">Already in the folder at this revision '
                       "&mdash; not fetched again.</div>")
        else:
            out.append('<div class="verdict ok">Exported.</div>')
            if step.get("url"):
                out.append(f'<p><a href="{esc(step["url"])}" style="color:var(--accent)">'
                           f'Download the STEP file</a></p>')
            elif step.get("path"):
                out.append(f'<p>Saved to <code>{esc(step["path"])}</code></p>')
            out.append(
                '<p class="hint" style="color:var(--muted);font-size:12.5px">'
                "Component names inside this file use Onshape's part <b>name</b> unless "
                "an Export Rule mapping to Part Number is configured in Company Settings "
                "&rarr; Preferences &rarr; Export Rules (object type Part, format STEP, "
                "convention <code>${partNumber}</code>). Confirmed by testing: passing "
                "evaluateExportRule alone does nothing without that rule in place.</p>"
            )

    # full BOM
    bom_rows = [
        [esc(r["item"]), pn_cell(r["pn"], r.get("url"), "Open this BOM line in Onshape"),
         esc(r["name"]), esc(r["rev"]), state_tag(r["state"]), esc(r["qty"]),
         esc(r["material"])]
        for r in rows
    ]
    out.append(
        f"<details><summary>Full BOM ({len(rows)} lines)</summary>"
        + table(["Item", "Part", "Name", "Rev", "State", "Qty", "Material"], bom_rows,
                ["num", "", "", "", "", "num", ""])
        + "</details>"
    )
    return "".join(out)


def build_report(results, base_url, folder_notes=None, generated=None):
    """Everything a report needs, as one JSON-serialisable dict. This is what
    gets saved to disk; the HTML is rendered from it on demand, so the stored
    record stays small and a later version of the renderer can re-render an
    old audit with whatever the report looks like by then.

    folder_notes: list of "Label path" strings shown in the subtitle, e.g.
    ["DXF folder K:\\...\\DXF FILES", "SAT folder K:\\...\\SAT FILES"]. A bare
    string is accepted too, for callers that only ever had one folder."""
    if isinstance(folder_notes, str):
        folder_notes = [folder_notes] if folder_notes else []
    return {
        "app": APP_NAME,
        "format": REPORT_FORMAT,
        "generated": generated or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "baseUrl": base_url,
        "folderNotes": list(folder_notes or []),
        "results": results,
    }


def save_report(path, data):
    """Write report data as compact JSON. Returns the Path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                    encoding="utf-8")
    return path


def load_report(path):
    """Read report data saved by save_report. Raises ValueError if the file
    is not a RevAudit report."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError(f"{path} is not a {APP_NAME} report")
    return data


def report_name_part(part_numbers, limit=3):
    """The assembly numbers as a filename fragment: up to `limit` of them
    joined with '+', then '+<n>more'. Anything a filename can't hold - or
    that the report-name regexes treat as structure ('-' is fine, '_' is
    not) - becomes '.'."""
    clean = [re.sub(r"[^A-Za-z0-9.\-]+", ".", str(pn)).strip(".") or "x"
             for pn in part_numbers]
    clean = [c[:24] for c in clean if c]
    if not clean:
        return "audit"
    shown = "+".join(clean[:limit])
    if len(clean) > limit:
        shown += f"+{len(clean) - limit}more"
    return shown


def render_report(results, base_url, folder_notes=None, generated=None):
    """Render straight from audit results - build_report + render_data."""
    return render_data(build_report(results, base_url, folder_notes, generated))


def render_data(data):
    """The HTML report for a dict from build_report / load_report."""
    results = data.get("results") or []
    base_url = data.get("baseUrl") or ""
    folder_notes = data.get("folderNotes") or []
    generated = data.get("generated") or ""
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{APP_NAME}</title><style>{CSS}</style></head><body><div class='wrap'>",
        f"<h1>{APP_NAME}</h1>",
        f"<p class='sub'>Generated {esc(generated)} &middot; {esc(base_url)}"
        + "".join(f" &middot; {esc(note)}" for note in folder_notes)
        + "</p>",
    ]

    if len(results) > 1:
        # column names come from whichever file checks any assembly used
        check_names = []
        for r in results:
            for name in (r.get("findings", {}).get("files") or {}):
                if name not in check_names:
                    check_names.append(name)

        summary = []
        for r in results:
            f = r.get("findings", {})
            asm = r.get("assembly")
            if not asm:
                summary.append(
                    [pn_cell(r["partNumber"]), "<b>not found</b>", "", "", ""]
                    + ["" for _ in check_names])
                continue
            nd = len(f.get("noDrawing", []))
            beh = len(f.get("drawingBehindPart", []))
            dup = len([d for d in f.get("duplicatePartNumbers", []) if d["mixedRevisions"]])
            row = [
                f'<a href="#{esc(r["partNumber"])}"><code>{esc(r["partNumber"])}</code></a>'
                f'{os_link(asm.get("url"), "Open the released assembly in Onshape")}',
                esc(asm["revision"]),
                f"<b>{nd}</b>" if nd else "0",
                f"<b>{beh}</b>" if beh else "0",
                f"<b>{dup}</b>" if dup else "0",
            ]
            files = f.get("files") or {}
            for name in check_names:
                miss = len(files.get(name, {}).get("missing", []))
                row.append(f'<span class="miss">{miss}</span>' if miss else "0")
            summary.append(row)
        parts.append("<h3>Summary</h3>")
        parts.append(table(
            ["Assembly", "Rev", "No drawing", "Drawing behind part",
             "Mixed-rev part numbers"] + [f"Missing {n}" for n in check_names],
            summary, ["", "", "num", "num", "num"] + ["num"] * len(check_names),
        ))

    for result in results:
        parts.append(render_assembly(result))

    parts.append(
        "<footer>A part is counted as released when it has a non-obsolete revision in "
        "the company revision registry &mdash; in Onshape a revision only exists once a "
        "release completes. Part revisions and drawing revisions are queried separately, "
        "which is what surfaces a drawing that lags its part. File matching (DXF, SAT) is "
        "case-insensitive with digit boundaries, so PRT-12 never matches PRT-123. "
        "Click any part number or filename to copy it; the &#8599; beside it opens that "
        "exact revision and configuration in Onshape."
        "<p style='margin-top:14px'>" + credit_html(f"{APP_NAME} &middot; ") + "</p>"
        "</footer>" + COPY_SCRIPT + "</div></body></html>"
    )
    return "".join(parts)


# Click any <code> token (part numbers, filenames, patterns) to copy it.
# navigator.clipboard needs a secure context - fine over https or localhost,
# blocked on a plain-http LAN IP, so there is an execCommand fallback.
COPY_SCRIPT = """<script>
(function(){
  function copy(text){
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function(){ return legacy(text); });
    }
    return legacy(text);
  }
  function legacy(text){
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand('copy'); } catch(e){}
    document.body.removeChild(ta);
    return Promise.resolve();
  }
  document.addEventListener('click', function(e){
    var el = e.target.closest && e.target.closest('code');
    if (!el) return;
    var txt = (el.textContent || '').trim();
    if (!txt) return;
    copy(txt).then(function(){
      el.classList.add('copied');
      setTimeout(function(){ el.classList.remove('copied'); }, 900);
    });
  });
})();
</script>"""


# --------------------------------------------------------------------------
# terminal summary
# --------------------------------------------------------------------------

def print_summary(result):
    pn = result["partNumber"]
    if not result.get("assembly"):
        print(f"  {pn}: NOT FOUND")
        for err in result.get("errors", []):
            print(f"    {err}")
        return
    f = result.get("findings", {})
    total = len(result.get("partStatuses", {}))
    nd = len(f.get("noDrawing", []))
    beh = len(f.get("drawingBehindPart", []))
    dup = len([d for d in f.get("duplicatePartNumbers", []) if d["mixedRevisions"]])
    obs = len(f.get("obsoleteLines", []))
    asm = result["assembly"]
    print(f"  {pn}  (doc {asm['documentName']}, Rev {asm['revision']})")
    print(f"    drawings released ... {total - nd}/{total}" + ("  <-- gap" if nd else ""))
    print(f"    drawing behind part . {beh}" + ("  <-- check" if beh else ""))
    print(f"    mixed-rev part nums . {dup}" + ("  <-- check" if dup else ""))
    print(f"    obsolete BOM lines .. {obs}")
    for name, fc in (f.get("files") or {}).items():
        miss = len(fc.get("missing", []))
        label = f"missing {name}".ljust(20, ".")
        print(f"    {label} {miss}" + ("  <-- gap" if miss else ""))
        multi = len(fc.get("multiple", []))
        if multi:
            print(f"    {(name + ' extras').ljust(20, '.')} {multi}  <-- review")
    export = result.get("drawingExport")
    if export:
        ok, skipped, failed = export_counts(export.get("entries", []))
        print(f"    drawing PDFs ........ {ok} exported"
             + (f", {skipped} already there" if skipped else "")
             + (f", {failed} failed  <-- check" if failed else ""))
    step = result.get("stepExport")
    if step:
        print("    STEP export ......... "
             + ("failed  <-- check: " + step["error"] if step.get("error")
                else "already there" if step.get("skipped") else "ok"))
    for err in result.get("errors", []):
        print(f"    ! {err}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")     # before the parser: export defaults read env vars

    parser = argparse.ArgumentParser(
        prog="revaudit",
        description="Release / drawing / production-file audit for Onshape assemblies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" --open\n'
            '  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" '
            '--sat-dir "<share>\\SAT FILES" --open\n'
            "  py -3.13 revaudit.py ASM-1 ASM-2 ASM-3 -o audit.html\n"
            "  py -3.13 revaudit.py --render reports/revaudit-20260921-1405.json --open\n"
        ),
    )
    parser.add_argument("assemblies", nargs="*", metavar="ASM",
                        help="assembly part numbers, e.g. ASM-12345")
    parser.add_argument("--render", metavar="FILE.json",
                        help="re-render the HTML report from a saved report's JSON "
                             "data instead of running an audit - no Onshape calls")
    parser.add_argument("--dxf-dir", metavar="PATH",
                        help="folder to check for DXF files (omit to skip the DXF check)")
    parser.add_argument("--dxf-recursive", action="store_true",
                        help="search the DXF folder recursively")
    parser.add_argument("--dxf-ext", default=".dxf",
                        help="comma-separated DXF extensions (default: .dxf)")
    parser.add_argument("--material-regex", default=r"SHEET",
                        help="materials that require a DXF (default: 'SHEET')")
    parser.add_argument("--sat-dir", metavar="PATH",
                        help="folder to check for SAT files (omit to skip the SAT check)")
    parser.add_argument("--sat-recursive", action="store_true",
                        help="search the SAT folder recursively")
    parser.add_argument("--sat-ext", default=".sat",
                        help="comma-separated SAT extensions (default: .sat)")
    parser.add_argument("--sat-material-regex", default=r"TUBE",
                        help="materials that require a SAT file (default: 'TUBE')")
    # Exports are on whenever a folder is configured (revaudit.conf [folders]
    # pdf / step, or REVAUDIT_PDF_DIR / REVAUDIT_STEP_DIR). Files already in
    # the folder at the current revision are never fetched again, so leaving
    # them on costs nothing once an assembly has been exported once.
    parser.add_argument("--export-drawings", nargs="?", const="", metavar="DIR",
                        default=config.folder("pdf") or None,
                        help="export a PDF of every released drawing (the assembly's "
                             "own + every part's) into DIR, flat and named "
                             "<part>_Rev<rev>.pdf, plus a zip of the same files for "
                             "this run. On by default when a pdf folder is configured; "
                             "DIR overrides it. Drawings whose PDF is already in the "
                             "folder at the current revision are skipped.")
    parser.add_argument("--no-export-drawings", action="store_true",
                        help="skip the drawing PDF export even if a folder is configured")
    parser.add_argument("--drawing-workers", type=int, default=4,
                        help="parallel PDF exports (default: 4)")
    parser.add_argument("--export-step", nargs="?", const="", metavar="DIR",
                        default=config.folder("step") or None,
                        help="export each assembly's own 3D geometry to a single STEP "
                             "file into DIR, flat and named <asm>_Rev<rev>.step. On by "
                             "default when a step folder is configured; DIR overrides "
                             "it. Skipped when the file is already there at the current "
                             "revision. Component names inside the file use Onshape's "
                             "part NAME unless an Export Rule mapping to Part Number is "
                             "configured in Company Settings -> Preferences -> Export "
                             "Rules - this tool cannot set that up for you.")
    parser.add_argument("--no-export-step", action="store_true",
                        help="skip the STEP export even if a folder is configured")
    parser.add_argument("--force-export", action="store_true",
                        help="re-fetch PDFs/STEP files even when they are already in "
                             "the folder")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="report path; the audit data is saved as FILE's .json "
                             "twin and the HTML rendered next to it (default: "
                             "revaudit-<timestamp>.json + .html in the configured "
                             "report folder)")
    parser.add_argument("--open", action="store_true", dest="open_report",
                        help="open the report in the browser when done")
    parser.add_argument("--base-url", help="Onshape base URL (or ONSHAPE_BASE_URL)")
    parser.add_argument("--company-id", help="company id (auto-detected by default)")
    parser.add_argument("--workers", type=int, default=6,
                        help="parallel API lookups (default: 6)")
    parser.add_argument("--no-deep", action="store_true",
                        help="skip the extra element-type sweep on parts with no drawing")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every API call")
    args = parser.parse_args(argv)

    if args.render:
        # Offline path: stored data -> HTML, no credentials needed.
        try:
            data = load_report(args.render)
        except (OSError, ValueError) as exc:
            print(f"Cannot render {args.render}: {exc}", file=sys.stderr)
            return 2
        out_path = Path(args.output) if args.output else Path(args.render).with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_data(data), encoding="utf-8")
        print(f"Report written to {out_path}")
        if args.open_report:
            webbrowser.open(out_path.resolve().as_uri())
        return 0
    if not args.assemblies:
        parser.error("give at least one assembly number, or --render FILE.json")

    def export_dir(flag, kind, disabled, fallback):
        """--export-X semantics: absent -> the configured folder (or off);
        bare flag -> configured folder, else a local fallback; DIR -> DIR."""
        if disabled or flag is None:
            return None
        return flag or config.folder(kind) or str(here / fallback)

    pdf_dir = export_dir(args.export_drawings, "pdf", args.no_export_drawings, "drawings")
    step_dir = export_dir(args.export_step, "step", args.no_export_step, "step")

    access = os.environ.get("ONSHAPE_ACCESS_KEY")
    secret = os.environ.get("ONSHAPE_SECRET_KEY")
    if not access or not secret:
        print(
            "Missing credentials.\n"
            "  Create an API key at https://dev-portal.onshape.com (Keys -> Create new key,\n"
            "  read scopes are enough), then put it in a file called .env next to this\n"
            "  script:\n\n"
            "    ONSHAPE_ACCESS_KEY=your_access_key\n"
            "    ONSHAPE_SECRET_KEY=your_secret_key\n"
            "    ONSHAPE_BASE_URL=https://yourcompany.onshape.com\n",
            file=sys.stderr,
        )
        return 2

    base_url = args.base_url or os.environ.get("ONSHAPE_BASE_URL") or DEFAULT_BASE_URL
    client = OnshapeClient(base_url, access, secret, verbose=args.verbose)

    try:
        company_id = args.company_id or os.environ.get("ONSHAPE_COMPANY_ID")
        if not company_id:
            companies = client.companies()
            if not companies:
                print("No company found for these credentials. Pass --company-id.",
                      file=sys.stderr)
                return 2
            company_id = companies[0]["id"]
            print(f"Company: {companies[0].get('name', '').strip()} ({company_id})")

        def split_exts(raw):
            return tuple(
                e if e.startswith(".") else "." + e
                for e in (x.strip() for x in raw.split(",")) if e
            )

        file_checks = []
        folder_notes = []
        for check_name, folder, recursive, ext_str, regex_str in (
            ("DXF", args.dxf_dir, args.dxf_recursive, args.dxf_ext, args.material_regex),
            ("SAT", args.sat_dir, args.sat_recursive, args.sat_ext,
             args.sat_material_regex),
        ):
            index = None
            if folder:
                print(f"Indexing {check_name} folder {folder} ...")
                index = FileIndex(folder, recursive, split_exts(ext_str))
                print(f"  {len(index)} files indexed")
                folder_notes.append(f"{check_name} folder {folder}")
            file_checks.append({
                "name": check_name,
                "index": index,
                "material_regex": re.compile(regex_str, re.IGNORECASE),
            })

        results = []
        for asm_pn in args.assemblies:
            print(f"Auditing {asm_pn} ...")
            try:
                result = audit_assembly(
                    client, company_id, asm_pn, file_checks,
                    workers=args.workers, deep=not args.no_deep, verbose=args.verbose,
                )
            except OnshapeError as exc:
                # keep going so one bad assembly does not lose the whole batch
                print(f"  {asm_pn} failed: {exc}", file=sys.stderr)
                results.append(
                    {"partNumber": asm_pn, "errors": [str(exc)], "findings": {}}
                )
                continue

            if pdf_dir and result.get("assembly"):
                # Flat, like the DXF/SAT folders - not one subfolder per
                # assembly - so this is a real shared archive: exporting the
                # same part from two assemblies just re-saves the same file,
                # and anything already there at its current rev is reused.
                out_dir = Path(pdf_dir)
                print(f"  exporting drawing PDFs to {out_dir} ...")

                def progress(done, total, _pn=asm_pn):
                    print(f"    {_pn}: {done}/{total} drawings checked", end="\r")

                entries = export_assembly_drawings(
                    client, result, out_dir, workers=args.drawing_workers,
                    on_progress=progress, force=args.force_export,
                )
                print()  # clear the \r progress line
                # The zip is a one-off snapshot of this run, not part of the
                # permanent archive, so it stays next to the report - not in
                # the shared K: folder alongside everyone's individual PDFs.
                zip_path = zip_pdfs(entries, here / f"{asm_pn}-drawings.zip")
                ok, skipped, failed = export_counts(entries)
                print(f"    {ok} exported, {skipped} already there, {failed} failed"
                     + (f", zipped to {zip_path}" if zip_path else ""))
                result["drawingExport"] = {
                    "entries": entries, "zipPath": str(zip_path) if zip_path else None,
                }

            if step_dir and result.get("assembly"):
                print(f"  exporting STEP file to {step_dir} ...")
                entry = export_assembly_step_file(client, result, Path(step_dir),
                                                  force=args.force_export)
                if entry and entry["error"]:
                    print(f"    failed: {entry['error']}")
                elif entry and entry.get("skipped"):
                    print(f"    already there: {entry['path']}")
                elif entry:
                    print(f"    exported to {entry['path']}")
                result["stepExport"] = entry

            results.append(result)
    except OnshapeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    print(f"\n{client.call_count} API calls\n")
    print("Results")
    print("-" * 60)
    for result in results:
        print_summary(result)
        print()

    # The JSON is the record - small, and re-renderable with --render; the
    # HTML beside it is the same report rendered for reading now.
    data = build_report(results, base_url, folder_notes)
    report_html = render_data(data)
    # revaudit-<stamp>_<assemblies>: the '_' after the stamp is what tells the
    # web UI this is a CLI file with no share token in its name (see serve.py).
    stamp = ("revaudit-" + datetime.now().strftime("%Y%m%d-%H%M%S")
             + "_" + report_name_part(args.assemblies))
    if args.output:
        out_path = Path(args.output).with_suffix(".html")
    else:
        # config's report folder (usually a K: share), falling back to here
        out_path = Path(config.report_dir()) / f"{stamp}.html"
    try:
        save_report(out_path.with_suffix(".json"), data)
        out_path.write_text(report_html, encoding="utf-8")
    except OSError as exc:
        if args.output:
            raise
        print(f"  {out_path.parent} not writable ({exc}); saving locally", file=sys.stderr)
        out_path = here / f"{stamp}.html"
        save_report(out_path.with_suffix(".json"), data)
        out_path.write_text(report_html, encoding="utf-8")
    print(f"Report written to {out_path}  (data: {out_path.with_suffix('.json').name})")

    if args.open_report:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
