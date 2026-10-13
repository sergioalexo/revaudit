"""The report: one self-contained HTML page with the audit data embedded.

build_report() turns audit results into a JSON-serialisable dict,
render_data() turns that dict into the page, and save_report() /
load_report() write and read it back. The CLI, the web UI and the OAuth app
all go through here, so a report looks the same however it was made - and a
saved one re-renders with whatever layout the current code has.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

from fsutil import write_atomic
from onshape_client import number
from pdf_export import export_counts

APP_NAME = "RevAudit"
AUTHOR = "Sergio Alexo"
AUTHOR_URL = "https://sergioalexo.com"

STATIC = Path(__file__).resolve().parent / "static"


def asset(name):
    """CSS/JS source kept in static/; inlined into pages so reports stay self-contained."""
    return (STATIC / name).read_text(encoding="utf-8")


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
# HTML report
# --------------------------------------------------------------------------

CSS = asset("report.css")


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


def qty(value):
    """A quantity for display: Onshape sends 2.0, people read 2."""
    if value is None or value == "":
        return ""
    n = number(value)
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return esc(n if n or value in (0, 0.0, "0") else value)


def other_elements_note(other):
    """'blob x1, assembly x2' for whatever else is released under a part
    number that has no drawing - often where the drawing really went."""
    return ", ".join(f"{esc(name)} &times;{esc(count)}" for name, count in (other or {}).items())


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

    # 1. no drawing - plus, when anything else is released under the number
    # (an uploaded PDF blob, an assembly), where the drawing may really be
    released_as = any(e.get("otherElements") for e in no_drawing)
    body = table(
        ["Part", "Name", "Material", "Qty", "Part rev", "Source document"]
        + (["Also released as"] if released_as else []),
        [
            [pn_cell(e["pn"], e.get("partUrl")), esc(e["name"]), esc(e["material"]),
             qty(e["qty"]), rev_cell(e["partRev"], e.get("partUrl")), esc(e["sourceDoc"])]
            + ([other_elements_note(e.get("otherElements"))] if released_as else [])
            for e in no_drawing
        ],
        ["", "", "", "num", "", ""] + ([""] if released_as else []),
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
             qty(e["qty"])]
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
            f'&times;{qty(l["qty"])} {state_tag(l["state"])}'
            for l in d["lines"]
        )
        total = sum(number(l["qty"]) for l in d["lines"])
        first_url = next((l.get("url") for l in d["lines"] if l.get("url")), None)
        dup_rows.append([pn_cell(d["pn"], first_url, "Open in Onshape"), detail, qty(total)])
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
             esc(o["name"]), esc(o["rev"]), qty(o["qty"]),
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
             qty(e["qty"])]
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
             esc(u["name"]), state_tag(u["state"]), qty(u["qty"])]
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
                     esc(m["material"]), qty(m["qty"])]
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
        else:
            out.append('<div class="verdict ok">Already in the folder at this revision '
                       "&mdash; not fetched again.</div>" if step.get("skipped")
                       else '<div class="verdict ok">Exported.</div>')
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
         esc(r["name"]), esc(r["rev"]), state_tag(r["state"]), qty(r["qty"]),
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


def build_report(results, base_url, folder_notes=None, generated=None, api=None):
    """Everything a report needs, as one JSON-serialisable dict. It travels
    inside the saved HTML (see render_data / load_report), so a report file
    is both the page you double-click and the data a later version of the
    renderer can re-render with whatever the report looks like by then.

    folder_notes: list of "Label path" strings shown in the subtitle, e.g.
    ["DXF folder <share>\\DXF FILES", "SAT folder <share>\\SAT FILES"]. A bare
    string is accepted too, for callers that only ever had one folder.

    api: the run's Onshape usage from api_usage(), kept so the reports
    double as a record of how many calls a check costs."""
    if isinstance(folder_notes, str):
        folder_notes = [folder_notes] if folder_notes else []
    return {
        "app": APP_NAME,
        "format": REPORT_FORMAT,
        "generated": generated or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "baseUrl": base_url,
        "folderNotes": list(folder_notes or []),
        "results": results,
        **({"api": api} if api else {}),
    }


def api_usage(results, calls, cached, audit_calls=None, step_calls=None):
    """{"calls", "cached", "auditCalls", "exportCalls", "assemblies", "parts"}
    for one run. calls/cached are this run's share of the client's counters;
    audit_calls splits off the export round trips when the caller knows it;
    step_calls then splits those into STEP (stepCalls) and drawing PDFs (pdfCalls)."""
    usage = {
        "calls": calls,
        "cached": cached,
        "assemblies": len(results),
        "parts": sum(len(r.get("partStatuses") or {}) for r in results),
    }
    if audit_calls is not None:
        usage["auditCalls"] = audit_calls
        usage["exportCalls"] = calls - audit_calls
        if step_calls is not None:
            usage["stepCalls"] = step_calls
            usage["pdfCalls"] = usage["exportCalls"] - step_calls
    return usage


def api_usage_line(usage):
    """One greppable log line: 'api-calls 94 (audit 80, exports 14), ...'."""
    split = (f" (audit {usage['auditCalls']}, exports {usage['exportCalls']})"
             if "auditCalls" in usage else "")
    return (f"api-calls {usage['calls']}{split}, {usage['cached']} from memory"
            f" - {usage['assemblies']} assemblies, {usage['parts']} parts")


# The report data rides inside the HTML in this block, so one file is both
# the readable page and the record. type=application/json means the browser
# never runs it. Every "<" is written as < (still valid JSON), so no part
# name - "</script>", "<!--" - can end the block early or change how the
# browser reads the rest of the page.
DATA_TAG_OPEN = '<script type="application/json" id="revaudit-data">'
DATA_TAG_CLOSE = "</script>"
_DATA_BLOCK_RE = re.compile(re.escape(DATA_TAG_OPEN) + r"(.*?)" + re.escape(DATA_TAG_CLOSE),
                            re.DOTALL)


def data_json(data):
    """Compact JSON for embedding or for the /report/<id>.json endpoint."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def data_block(data):
    return DATA_TAG_OPEN + data_json(data).replace("<", "\\u003c") + DATA_TAG_CLOSE


def save_report(path, data):
    """Write the report as a self-contained .html with its data embedded.
    Written atomically, so a report on the share is never half there.
    Returns the Path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_atomic(path, render_data(data))


def extract_data(html_text):
    """The embedded report data from rendered HTML, or None if it has none
    (a report saved before data was embedded)."""
    m = _DATA_BLOCK_RE.search(html_text)
    if not m:
        return None
    data = json.loads(m.group(1))
    return data if isinstance(data, dict) and "results" in data else None


def load_report(path):
    """Read report data back from a saved .html (embedded block) or from a
    bare .json written by the short-lived JSON-file format. Raises
    ValueError if the file is neither."""
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict) and "results" in data:
            return data
        raise ValueError(f"{path} is not a {APP_NAME} report")
    data = extract_data(text)
    if data is None:
        raise ValueError(f"{path} has no embedded {APP_NAME} data")
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
    api = data.get("api") or {}
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{APP_NAME}</title><style>{CSS}</style></head><body><div class='wrap'>",
        f"<h1>{APP_NAME}</h1>",
        f"<p class='sub'>Generated {esc(generated)} &middot; {esc(base_url)}"
        + "".join(f" &middot; {esc(note)}" for note in folder_notes)
        + (f" &middot; {esc(api['calls'])} API calls" if "calls" in api else "")
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
        "release completes. A part's revisions and its drawing's revisions are compared "
        "separately, which is what surfaces a drawing that lags its part. File matching "
        "(DXF, SAT) is "
        "case-insensitive with digit boundaries, so PRT-12 never matches PRT-123. "
        "Click any part number or filename to copy it; the &#8599; beside it opens that "
        "exact revision and configuration in Onshape."
        "<p class='credit'>" + credit_html(f"{APP_NAME} &middot; ") + "</p>"
        "</footer>" + COPY_SCRIPT + "</div>" + data_block(data) + "</body></html>"
    )
    return "".join(parts)


COPY_SCRIPT = f"<script>{asset('copy.js')}</script>"
