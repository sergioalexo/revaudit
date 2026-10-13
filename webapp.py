"""The parts both web front ends share - serve.py (one API key behind a site
password) and oauth_app.py (everyone signs in with their own Onshape
account): the form and its pages, the production-file indexes, running an
audit plus its exports and saving the report, and the download links.
"""

from __future__ import annotations

import gzip
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import config
from audit_core import FileIndex, audit_assembly
from fsutil import write_atomic
from onshape_client import OnshapeError
from pdf_export import (export_assembly_drawings, export_assembly_step_file,
                        export_counts, zip_pdfs)
from report import APP_NAME, CSS, api_usage, api_usage_line, asset, build_report, credit_html
from report_store import (find_report, list_reports, new_report_id, newest_first, report_id,
                          report_label, write_report)

HERE = Path(__file__).resolve().parent

MAX_FORM_BYTES = 1024 * 1024            # the /run form is a few hundred bytes
MAX_INDEX_BYTES = 32 * 1024 * 1024


# --------------------------------------------------------------------------
# production-file checks the form offers
# --------------------------------------------------------------------------

# Every file check the built-in web forms offer, in display order. The
# default folder for each comes from revaudit.conf [folders] <field>.
FILE_CHECK_DEFS = [
    {"name": "DXF", "field": "dxf", "ext": ".dxf", "default_regex": "SHEET"},
    {"name": "SAT", "field": "sat", "ext": ".sat", "default_regex": "TUBE"},
]


def default_dir(field):
    return config.folder(field) or ""


# --------------------------------------------------------------------------
# file indexes: scanned live and cached, or uploaded by file_indexer.py
# --------------------------------------------------------------------------

_LEGACY_INDEX_FILE = HERE / "dxf-index.json"   # pre-multi-check index, DXF only


def index_file_path(name):
    return HERE / f"file-index-{name.lower()}.json"


# Indexing a network folder takes ~15s, so keep it between runs - but only
# while the folder is unchanged. Each entry remembers the folder's mtime at
# scan time (any file added or removed in the top level bumps it) and its
# age; it is rescanned when either says the list may be stale. Without this
# a DXF saved after RevAudit started stayed invisible until the next restart.
_index_cache = {}
_index_lock = threading.Lock()
INDEX_MAX_AGE = 10 * 60          # seconds; also catches changes in subfolders


def load_uploaded_index(name, extensions=(".dxf",)):
    """The name list published by file_indexer.py for this check, if one has
    been uploaded. Falls back to the pre-multi-check dxf-index.json for DXF,
    so an existing upload keeps working without anyone re-running the indexer."""
    path = index_file_path(name)
    if not path.is_file() and name.upper() == "DXF" and _LEGACY_INDEX_FILE.is_file():
        path = _LEGACY_INDEX_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return FileIndex.from_names(
        data.get("names", []), data.get("folder", "(uploaded index)"),
        data.get("recursive", False), data.get("generated"), extensions,
    )


def _folder_mtime(folder):
    try:
        return os.stat(folder).st_mtime
    except OSError:
        return None


def get_file_index(name, folder, recursive, extensions=(".dxf",)):
    """Scan the folder live; fall back to an uploaded index when this host
    cannot see the share (the usual case for a remote deployment)."""
    key = (name, str(folder), bool(recursive))
    mtime = _folder_mtime(folder)
    with _index_lock:
        hit = _index_cache.get(key)
    if hit is not None:
        index, scanned_at, seen_mtime = hit
        fresh = time.time() - scanned_at < INDEX_MAX_AGE
        # an uploaded index is only ever a stand-in - retry the folder each
        # time so it takes over as soon as the share becomes reachable
        if (fresh and mtime is not None and mtime == seen_mtime
                and index.source == "scanned live"):
            return index
    try:
        index = FileIndex(folder, recursive, extensions)
    except OnshapeError:
        index = load_uploaded_index(name, extensions)
        if index is None:
            raise
    with _index_lock:
        _index_cache[key] = (index, time.time(), mtime)
    return index


def store_uploaded_index(raw, gzipped):
    """Validate and save an uploaded index under its check name, then drop
    that check's cache entries. The name must be one of FILE_CHECK_DEFS - it
    becomes part of a filename, so nothing else is accepted."""
    if gzipped:
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("index must be a JSON object")
    names = data.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("index must contain a list of filenames")
    wanted = str(data.get("name") or "DXF").strip().upper()
    known = {c["name"].upper(): c["name"] for c in FILE_CHECK_DEFS}
    if wanted not in known:
        raise ValueError(f"unknown check {wanted!r}; expected one of {', '.join(known)}")
    name = known[wanted]
    write_atomic(index_file_path(name), json.dumps(data))
    with _index_lock:
        for key in [k for k in _index_cache if k[0] == name]:
            _index_cache.pop(key, None)
    return len(names), data.get("folder", "?"), name


def handle_index_upload(handler):
    """POST /dxf-index from file_indexer.py. Authenticated by a shared token,
    not by a user session, since it is a machine talking to us. The payload's
    "name" field ("DXF", "SAT", ...) says which check this index is for."""
    plain = "text/plain; charset=utf-8"
    token = os.environ.get("REVAUDIT_INDEX_TOKEN")
    if not token:
        return handler._send(
            b"index uploads are disabled: set REVAUDIT_INDEX_TOKEN in .env", 503, plain)
    given = handler.headers.get("X-Index-Token", "")
    if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
        return handler._send(b"bad token", 401, plain)
    length = content_length(handler)
    if length is None or length > MAX_INDEX_BYTES:
        return handler._send(b"index too large or no length given", 413, plain)
    raw = handler.rfile.read(length)
    gzipped = handler.headers.get("Content-Encoding", "").lower() == "gzip"
    try:
        count, folder, name = store_uploaded_index(raw, gzipped)
    except Exception as exc:                                      # noqa: BLE001
        return handler._send(f"bad index: {exc}".encode(), 400, plain)
    print(f"  {name} index updated: {count} names from {folder}", file=sys.stderr)
    return handler._send(f"ok, {count} names stored for {name}".encode(), 200, plain)


# --------------------------------------------------------------------------
# download links: a run's drawing zip and STEP copy
# --------------------------------------------------------------------------

# Same shareable-link idea as reports: the random token in the name is the
# only thing that gates access, so a drawing package or STEP file link needs
# no password.
SHARED_ZIP_RE = r"[A-Za-z0-9_-]{1,80}-drawings-[A-Za-z0-9_-]{10,}\.zip"
SHARED_STEP_RE = r"[A-Za-z0-9_-]{1,80}-step-[A-Za-z0-9_-]{10,}\.step"

DRAWINGS_DIR = HERE / "drawings"


def _link_stem(asm_pn):
    """The assembly number as the download regexes accept it - anything
    else (a '.', a space) would make a link that can never be opened."""
    return (re.sub(r"[^A-Za-z0-9_-]+", "_", str(asm_pn)).strip("_") or "asm")[:80]


def new_zip_name(asm_pn):
    return f"{_link_stem(asm_pn)}-drawings-{secrets.token_urlsafe(12)}.zip"


def new_step_name(asm_pn):
    return f"{_link_stem(asm_pn)}-step-{secrets.token_urlsafe(12)}.step"


def prune_downloads(keep_days=None, now=None):
    """Delete this app's own download copies older than keep_days
    ([folders] download_keep_days, default 90; 0 keeps them forever). Only
    files named like a download link are touched - they are snapshots, the
    permanent PDFs and STEP files stay in their folders. Returns the count."""
    keep_days = config.download_keep_days() if keep_days is None else keep_days
    if keep_days <= 0:
        return 0
    cutoff = (now or time.time()) - keep_days * 86400
    removed = 0
    try:
        candidates = list(DRAWINGS_DIR.iterdir())
    except OSError:
        return 0
    for path in candidates:
        if not (re.fullmatch(SHARED_ZIP_RE, path.name)
                or re.fullmatch(SHARED_STEP_RE, path.name)):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def send_download(handler, name):
    """Stream drawings/<name> when name is a download-link name and the file
    exists; otherwise a 404. Streamed rather than read whole - a STEP file
    can run to hundreds of MB."""
    if re.fullmatch(SHARED_ZIP_RE, name):
        ctype = "application/zip"
    elif re.fullmatch(SHARED_STEP_RE, name):
        ctype = "application/octet-stream"
    else:
        ctype = None
    target = DRAWINGS_DIR / name
    try:
        size = target.stat().st_size if ctype and target.is_file() else None
    except OSError:
        size = None
    if size is None:
        return handler._send(page("Not found", "<h1>File not found</h1>"), 404)
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
    handler.end_headers()
    with open(target, "rb") as fh:
        shutil.copyfileobj(fh, handler.wfile, 1024 * 1024)
    return None


# --------------------------------------------------------------------------
# the /run form
# --------------------------------------------------------------------------

def content_length(handler):
    """The request's Content-Length as an int, or None when it is missing,
    negative or not a number."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    return length if length >= 0 else None


def read_form(handler):
    """The posted /run form as a values dict, or None when the body is too
    big to be the form (nothing is read in that case)."""
    length = content_length(handler)
    if length is None or length > MAX_FORM_BYTES:
        return None
    raw = handler.rfile.read(length)
    return parse_run_form(parse_qs(raw.decode("utf-8", "replace")))


def parse_run_form(fields):
    """The /run form's fields (from parse_qs) as the values dict the audit,
    export and re-rendered form all read."""
    def text(key, default=""):
        return (fields.get(key, [default])[0] or "").strip()

    values = {
        "assemblies": text("assemblies"),
        "export_drawings": bool(fields.get("export_drawings")),
        "pdf_dir": text("pdf_dir"),
        "export_step": bool(fields.get("export_step")),
        "step_dir": text("step_dir"),
        "force_export": bool(fields.get("force_export")),
        # set by the "already audited" page's buttons - run it regardless...
        "force_run": bool(fields.get("force_run")),
        # ...and, for "Run the audit again", re-read everything from Onshape
        "fresh": bool(fields.get("fresh")),
    }
    for check in FILE_CHECK_DEFS:
        field = check["field"]
        values[f"{field}_dir"] = text(f"{field}_dir")
        values[f"{field}_recursive"] = bool(fields.get(f"{field}_recursive"))
        values[f"{field}_material_regex"] = text(f"{field}_material_regex",
                                                 check["default_regex"])
    return values


def assembly_names(values):
    """The assembly numbers typed into the form, in order, each once."""
    seen = []
    for name in re.split(r"[\s,;]+", values.get("assemblies", "")):
        if name and name not in seen:
            seen.append(name)
    return seen


def form_error(values):
    """What is wrong with a submitted form, or None. Checked before any API
    call, so a typo costs nothing."""
    if not assembly_names(values):
        return "Enter at least one assembly number."
    for check in FILE_CHECK_DEFS:
        raw = values.get(f"{check['field']}_material_regex") or check["default_regex"]
        try:
            re.compile(raw)
        except re.error as exc:
            return f"The {check['name']} material pattern {raw!r} is not valid: {exc}"
    try:
        re.compile(config.part_pattern())
    except re.error as exc:
        return f"revaudit.conf [audit] part_pattern is not a valid pattern: {exc}"
    return None


def requested_checks(values):
    """The extras this submitted form asks for, named the way result_checks()
    names them, so what a previous report covered can be compared with what
    is being asked for now."""
    names = [c["name"] for c in FILE_CHECK_DEFS if values.get(c["field"] + "_dir")]
    if values.get("export_drawings") and values.get("pdf_dir"):
        names.append("drawing PDFs")
    if values.get("export_step") and values.get("step_dir"):
        names.append("STEP")
    return names


def folder_notes_for(values):
    """The "DXF folder <path>" subtitle notes for the checks that ran."""
    return [
        f"{check['name']} folder {values[check['field'] + '_dir']}"
        for check in FILE_CHECK_DEFS if values.get(check["field"] + "_dir")
    ]


def build_file_checks(values):
    """Turn submitted form values into the file_checks list audit_assembly
    expects, indexing each configured folder as needed."""
    file_checks = []
    for check in FILE_CHECK_DEFS:
        field = check["field"]
        folder = values.get(f"{field}_dir")
        index = None
        if folder:
            index = get_file_index(check["name"], folder,
                                   values.get(f"{field}_recursive"), (check["ext"],))
        regex = values.get(f"{field}_material_regex") or check["default_regex"]
        file_checks.append({
            "name": check["name"],
            "index": index,
            "material_regex": re.compile(regex, re.IGNORECASE),
        })
    return file_checks


# --------------------------------------------------------------------------
# running an audit
# --------------------------------------------------------------------------

# serve.py runs one audit at a time - it has one API key, and one run at a
# time keeps the load on it predictable.
run_lock = threading.Lock()


def run_audit(client, company_id, names, values, who=None):
    """audit_assembly for each name, with the form's file checks. One
    assembly failing never loses the others."""
    file_checks = build_file_checks(values)
    part_pattern = config.part_pattern()
    tag = f"[{who}] " if who else ""
    results = []
    for name in names:
        print(f"  {tag}auditing {name} ...", file=sys.stderr)
        try:
            results.append(audit_assembly(client, company_id, name, file_checks,
                                          workers=6, deep=True, part_pattern=part_pattern))
        except OnshapeError as exc:
            print(f"  {name} failed: {exc}", file=sys.stderr)
            results.append({"partNumber": name, "errors": [str(exc)], "findings": {}})
    return results


def run_exports(results, client, host, values):
    """The PDF and STEP exports the form asked for, attached to results."""
    force = bool(values.get("force_export"))
    if values.get("export_drawings") and values.get("pdf_dir"):
        export_drawings_for(results, client, host, values["pdf_dir"], force=force)
    if values.get("export_step") and values.get("step_dir"):
        export_step_for(results, client, host, values["step_dir"], force=force)


def _export_failed(result, what, exc):
    """Record an export that blew up as a whole on the result, so the
    report says so - and still gets saved, since the audit's calls are spent."""
    if not isinstance(exc, (OSError, OnshapeError)):
        traceback.print_exc()
    message = f"{what} failed: {type(exc).__name__}: {exc}"
    print(f"    {message}", file=sys.stderr)
    result.setdefault("errors", []).append(message)


def export_drawings_for(results, client, host, pdf_dir, force=False):
    """Export + zip drawings for every audited assembly that resolved, and
    attach a shareable download link to each result in place. One PDF export
    round trip per drawing, so this is the slow part of a run - callers show
    a "this takes a while" note before triggering it.

    Individual PDFs go into pdf_dir (whatever the user chose - typically the
    shared archive), flat and shared across assemblies in this run, same
    convention as the DXF/SAT folders. The zip is a one-off snapshot of this
    particular run, not part of that permanent archive, so it stays under
    this app's own DRAWINGS_DIR instead - the fixed, server-controlled root
    the public /download/ route trusts, regardless of what pdf_dir is.
    """
    DRAWINGS_DIR.mkdir(exist_ok=True)
    prune_downloads()
    pdf_dir = Path(pdf_dir)
    for result in results:
        if not result.get("assembly"):
            continue
        asm_pn = result["partNumber"]
        print(f"  exporting drawings for {asm_pn} to {pdf_dir} ...", file=sys.stderr)
        try:
            entries = export_assembly_drawings(client, result, pdf_dir, workers=6,
                                               force=force)
            zip_name = new_zip_name(asm_pn)
            zip_path = zip_pdfs(entries, DRAWINGS_DIR / zip_name)
        except Exception as exc:                                  # noqa: BLE001
            _export_failed(result, "Drawing PDF export", exc)
            continue
        ok, skipped, failed = export_counts(entries)
        print(f"    {ok} exported, {skipped} already there, {failed} failed",
              file=sys.stderr)
        result["drawingExport"] = {
            "entries": entries,
            "zipUrl": f"http://{host}/download/{zip_name}" if zip_path else None,
        }


def export_step_for(results, client, host, step_dir, force=False):
    """Export one STEP file per audited assembly that resolved, and attach a
    shareable download link to each result in place.

    The permanent file goes into step_dir (typically the shared archive),
    flat and named like the DXF/SAT/PDF folders. A copy under this app's own
    DRAWINGS_DIR - the fixed, server-controlled root the /download/ route
    trusts - gets a random-token name for the shareable link, same idea as
    the drawing package zip; there's only one file so it isn't zipped first.
    """
    DRAWINGS_DIR.mkdir(exist_ok=True)
    prune_downloads()
    step_dir = Path(step_dir)
    for result in results:
        if not result.get("assembly"):
            continue
        asm_pn = result["partNumber"]
        print(f"  exporting STEP for {asm_pn} to {step_dir} ...", file=sys.stderr)
        try:
            entry = export_assembly_step_file(client, result, step_dir, force=force)
            if entry and not entry.get("error"):
                step_name = new_step_name(asm_pn)
                shutil.copyfile(entry["path"], DRAWINGS_DIR / step_name)
                entry["url"] = f"http://{host}/download/{step_name}"
        except Exception as exc:                                  # noqa: BLE001
            _export_failed(result, "STEP export", exc)
            continue
        if entry and entry.get("error"):
            print(f"    failed: {entry['error']}", file=sys.stderr)
        elif entry:
            print("    already there" if entry.get("skipped") else "    exported",
                  file=sys.stderr)
        result["stepExport"] = entry


def audit_and_report(client, company_id, names, values, host, base_url, who=None):
    """The whole of a /run once the "already audited?" question is settled:
    audit, export, save the report. Returns (report id, saved path, data).

    The run's API usage goes into the report and the log. It is measured on
    this client across the run - exact for serve.py, which holds run_lock
    around it, and for oauth_app.py, where every session has its own client.
    """
    calls0, cached0 = client.usage()
    results = run_audit(client, company_id, names, values, who)
    audit_calls = client.usage()[0] - calls0
    run_exports(results, client, host, values)
    calls1, cached1 = client.usage()
    api = api_usage(results, calls1 - calls0, cached1 - cached0, audit_calls)
    print(f"  {api_usage_line(api)}", file=sys.stderr)
    data = build_report(results, base_url, folder_notes_for(values), api=api)
    rid = new_report_id(names)
    saved = write_report(rid, data)             # configured folder, or app folder
    return rid, saved, data


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

FORM_CSS = asset("form.css")
FORM_JS = asset("form.js")
ALREADY_CSS = asset("already.css")
SHARE_CSS = asset("share.css")
SHARE_JS = asset("share.js")


def page(title, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{CSS}{FORM_CSS}{ALREADY_CSS}</style></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    ).encode("utf-8")


def file_check_fields_html(v):
    """One folder/recursive/material-regex block per configured file check
    (DXF, SAT, ...), each posting as {field}_dir / {field}_recursive /
    {field}_material_regex so the form scales to however many checks are
    configured without special-casing any one of them."""
    blocks = []
    for check in FILE_CHECK_DEFS:
        field = check["field"]
        dir_val = html.escape(v.get(f"{field}_dir", default_dir(field)))
        regex_val = html.escape(v.get(f"{field}_material_regex", check["default_regex"]))
        rec = "checked" if v.get(f"{field}_recursive") else ""
        blocks.append(f"""
        <label>{html.escape(check["name"])} folder
          <div class='hint'>Leave blank to skip the {html.escape(check["name"])} check.</div>
        </label>
        <input type='text' name='{field}_dir' value='{dir_val}'>

        <div class='row'>
          <input type='checkbox' name='{field}_recursive' id='rec_{field}' {rec}>
          <label for='rec_{field}'>Search subfolders too</label>
        </div>

        <label>Materials that require a {html.escape(check["name"])} file
          <div class='hint'>Regular expression matched against the BOM material.</div>
        </label>
        <input type='text' name='{field}_material_regex' value='{regex_val}'>
        """)
    return "".join(blocks)


def form_page(values=None, error=None, report_names=None):
    """report_names limits the 'recent reports' list. In OAuth mode each user is
    passed only their own, so one person's audit is never linked to another."""
    v = values or {}
    asm = html.escape(v.get("assemblies", ""))
    pdf_default, step_default = default_dir("pdf"), default_dir("step")
    # Exports default to on whenever their folder is configured - a re-run
    # costs nothing for files already there - and follow the form on a resubmit.
    export_drawings = v.get("export_drawings", bool(pdf_default))
    export_step = v.get("export_step", bool(step_default))

    if report_names is None:
        reports = list_reports(5)
    else:
        # OAuth mode passes an explicit per-user list of names
        reports = newest_first([p for p in map(find_report, report_names) if p], 5)
    recent = ""
    if reports:
        links = " ".join(
            f"<a href='/report/{html.escape(report_id(p.name))}'>"
            f"{html.escape(report_label(p.name))}</a>"
            for p in reports
        )
        recent = f"<div class='recent'><b>Recent reports</b><br>{links}</div>"

    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""

    return page(APP_NAME, f"""
      <h1>{APP_NAME}</h1>
      <p class='sub'>Release, drawing and production-file audit for Onshape assemblies.</p>
      {err}
      <form class='form' method='post' action='/run' onsubmit='go()'>
        <label>Assembly number
          <div class='hint'>One or more, separated by spaces or commas.</div>
        </label>
        <input type='text' name='assemblies' value='{asm}' autofocus
               placeholder='ASM-12345' required>

        {file_check_fields_html(v)}

        <div class='row'>
          <input type='checkbox' name='export_drawings' id='export_drawings'
                 onchange="document.getElementById('pdf_dir').disabled = !this.checked"
                 {"checked" if export_drawings else ""}>
          <label for='export_drawings'>Export a PDF of every drawing &mdash; only
            drawings not already in the folder at their current revision are fetched
            (a few seconds each)</label>
        </div>
        <label>PDF export folder
          <div class='hint'>Every released drawing (the assembly's own + every part's)
            is saved here, flat and named like the DXF/SAT folders, plus a zip of this
            run's files for sharing. Only used when the box above is checked.</div>
        </label>
        <input type='text' name='pdf_dir' id='pdf_dir'
               value='{html.escape(v.get("pdf_dir", pdf_default))}'
               {"" if export_drawings else "disabled"}>

        <div class='row'>
          <input type='checkbox' name='export_step' id='export_step'
                 onchange="document.getElementById('step_dir').disabled = !this.checked"
                 {"checked" if export_step else ""}>
          <label for='export_step'>Export each assembly's 3D geometry as STEP &mdash;
            skipped when the file is already in the folder at this revision</label>
        </div>
        <label>STEP export folder
          <div class='hint'>One file per assembly (not per part), same flat/named
            convention as the folders above. Component names inside use Onshape's part
            <b>name</b>, not part number, unless an Export Rule is configured in
            Onshape (Company Settings &rarr; Preferences &rarr; Export Rules) &mdash;
            this tool can't set that up for you. Only used when the box above is
            checked.</div>
        </label>
        <input type='text' name='step_dir' id='step_dir'
               value='{html.escape(v.get("step_dir", step_default))}'
               {"" if export_step else "disabled"}>

        <div class='row'>
          <input type='checkbox' name='force_export' id='force_export'
                 {"checked" if v.get("force_export") else ""}>
          <label for='force_export'>Re-fetch files that are already in the folders</label>
        </div>

        <button type='submit' id='btn'>Run audit</button>
        <div class='spin' id='spin'>Running &mdash; the first run against any given
          folder indexes it, which takes about 15 seconds. Later runs reuse it.
          Each drawing not yet in the PDF folder takes a few seconds on top.</div>
      </form>
      {recent}
      <footer>{credit_html()}</footer>
      <script>{FORM_JS}</script>
    """)


def hidden_fields_html(values, **overrides):
    """The whole /run form as hidden inputs, so a follow-up page can re-post
    the same audit - same folders, same checks, same export choices - without
    anyone having to fill the form in again. Booleans become the "1" a
    checkbox posts; blanks are dropped, which is exactly how a blank folder
    reads as "skip that check"."""
    fields = {**values, **overrides}
    out = []
    for key, value in fields.items():
        if value is True:
            value = "1"
        if value is False or value is None or value == "":
            continue
        out.append(f"<input type='hidden' name='{html.escape(str(key))}' "
                   f"value='{html.escape(str(value))}'>")
    return "".join(out)


def already_run_page(values, entries):
    """The page shown when an assembly is already covered by a report at its
    current revision: what was found, and the choice between opening it and
    spending the API calls again.

    entries is what prior_runs() returned. At least one of them has a prior
    report - otherwise the audit would just have run.
    """
    covered = [e for e in entries if e["prior"]]
    pending = [e for e in entries if not e["prior"]]
    wanted = requested_checks(values)

    cards = []
    for entry in covered:
        prior = entry["prior"]
        checks = prior["checks"]
        covered_note = (f" &middot; covered {html.escape(', '.join(checks))}"
                        if checks else "")
        gap = [c for c in wanted if c not in checks]
        gap_note = (f"<p class='gap'>This run would add: "
                    f"{html.escape(', '.join(gap))}.</p>" if gap else "")
        cards.append(f"""
        <div class='prior'>
          <div class='pn'>{html.escape(entry["name"])} &middot;
            Rev {html.escape(str(entry["revision"]))}</div>
          <p class='line'>Report from
            <b>{html.escape(prior["generated"] or "an earlier run")}</b>{covered_note}</p>
          {gap_note}
          <a class='open' href='/report/{html.escape(prior["id"])}'>
            Open that report &rarr;</a>
        </div>""")

    for entry in pending:
        if entry["error"]:
            note = html.escape(entry["error"])
        elif not entry["revision"]:
            note = "No released revision found - the audit will say why."
        else:
            note = f"Rev {html.escape(str(entry['revision']))} &middot; not audited yet"
        cards.append(
            f"<div class='prior fresh'><div class='pn'>{html.escape(entry['name'])}"
            f"</div><p class='line'>{note}</p></div>")

    pending_names = " ".join(e["name"] for e in pending)
    only_new = ""
    if pending:
        # no "fresh" here: the lookups the check just made are still good
        only_new = f"""
        <form method='post' action='/run'>
          {hidden_fields_html(values, assemblies=pending_names, force_run="1", fresh=False)}
          <button type='submit' class='second'>Audit only the
            {len(pending)} not yet covered</button>
        </form>"""

    return page(f"{APP_NAME} - already audited", f"""
      <h1>{APP_NAME}</h1>
      <p class='sub'>Already audited at this revision.</p>
      <p>Nothing was requested from Onshape yet beyond each assembly's current
      revision. A full audit costs an API call for every part (and the exports
      a few per new file), so here is what already exists:</p>
      {"".join(cards)}
      <p style='color:var(--muted);font-size:13px;margin:18px 0 6px'>
      The assembly is still at the revision that report was written for. Its
      parts can have moved since though &mdash; a drawing released, a DXF
      dropped in the folder &mdash; so run it again if you need today's answer
      rather than that one.</p>
      <div class='choices'>
        <form method='post' action='/run'>
          {hidden_fields_html(values, force_run="1", fresh="1")}
          <button type='submit'>Run the audit again</button>
        </form>
        {only_new}
        <a class='back' href='/'>&larr; Change the settings</a>
      </div>
      <footer>{credit_html()}</footer>
    """)


def with_back_link(report_html, saved_name, share_url=None, data_url=None):
    """share_url, if given, is shown as a copyable link - the report's filename
    carries enough random entropy that anyone with the link can open it without
    the site password, so it is safe to hand to someone who only needs to see
    this one result. data_url links the raw JSON the page was rendered from."""
    link_row = ""
    if share_url:
        data_link = (f"<a class='back' href='{html.escape(data_url)}'>data (.json)</a>"
                     if data_url else "")
        link_row = (
            f"<div class='share'><input readonly id='shareUrl' "
            f"value='{html.escape(share_url)}' onclick='this.select()'>"
            f"<button type='button' onclick='copyShareUrl()'>Copy link</button>"
            f"<span id='copied' class='copied-flag'>copied</span>{data_link}</div>"
            f"<script>{SHARE_JS}</script>"
        )
    bar = (
        f"<style>{SHARE_CSS}</style>"
        "<p class='backbar'><a class='back' href='/'>&larr; New audit</a>"
        f"<span class='saved-as'>saved as {html.escape(saved_name)}</span></p>"
        f"{link_row}"
    )
    return report_html.replace("<div class='wrap'>", "<div class='wrap'>" + bar, 1)


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't print a traceback every time a client
    hangs up early - a browser giving up on a long run, or the keep-alive
    task's port probe, which connects and closes every ten minutes and used
    to put a 20-line ConnectionResetError into revaudit.log each time."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)
