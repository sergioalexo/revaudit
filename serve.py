#!/usr/bin/env python3
"""
RevAudit web UI - type an assembly number in a browser, get the report.

    py -3.13 serve.py            then open http://localhost:8000
    py -3.13 serve.py --open     opens the browser for you
    py -3.13 serve.py --port 9000 --host 0.0.0.0    reachable from other machines

Runs the same audit as the CLI. Credentials come from .env exactly as before,
so the key never reaches the browser.
"""

from __future__ import annotations

import argparse
import base64
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
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from audit_core import FileIndex, audit_assembly
from onshape_client import (DEFAULT_BASE_URL, ET_ASSEMBLY, OnshapeClient,
                            OnshapeError, current_revision)
from pdf_export import (export_assembly_drawings, export_assembly_step_file,
                        export_counts, safe_filename, zip_pdfs)
import config
from revaudit import (APP_NAME, CSS, asset, build_report, credit_html,
                      data_json, load_dotenv, load_report, render_data,
                      report_name_part, save_report)

HERE = Path(__file__).resolve().parent
config.ensure_from_example()          # so `launch.bat` alone still works
DEFAULT_DXF_DIR = config.folder("dxf")
DEFAULT_SAT_DIR = config.folder("sat")
DEFAULT_PDF_DIR = config.folder("pdf")
DEFAULT_STEP_DIR = config.folder("step")

# A report is stored as revaudit-<timestamp>-<assemblies>-<random token>.html
# - the finished page with the audit data embedded in it - and re-rendered
# from that data whenever someone opens /report/<that id>. The token (exactly
# 16 url-safe chars, always last) is what makes a report link safe to hand
# out without also handing out the site password: nobody can guess it, so
# holding the link is proof enough that you were given it. The same id with
# .json appended returns just the data. Names from before the assemblies
# were in them (revaudit-<timestamp>-<token>) still fit.
TOKEN_CHARS = 16                                  # secrets.token_urlsafe(12)
SHARED_REPORT_RE = (r"revaudit-\d{8}-\d{6}-(?:[A-Za-z0-9.+-]+-)?[A-Za-z0-9_-]{16}"
                    r"(?:\.json|\.html)?")

# revaudit-<timestamp>[_<assemblies>] with no token - reports the CLI writes
# into the shared folder, and web reports from before link sharing existed.
# They carry no secret of their own, so they still need the site password
# rather than being reachable by anyone who finds the filename. The '_' right
# after the timestamp is what keeps a CLI name from ever looking like a
# tokened one: a shared name always has '-' there.
LEGACY_REPORT_RE = r"revaudit-\d{8}-\d{6}(?:_[A-Za-z0-9.+-]+)?(?:\.json|\.html)?"

# .json: files from the few hours the data was stored bare; still readable
REPORT_EXTS = (".html", ".json")


def new_report_id(part_numbers=()):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    token = secrets.token_urlsafe(12)
    assert len(token) == TOKEN_CHARS
    return f"revaudit-{stamp}-{report_name_part(part_numbers)}-{token}"


def report_id(name):
    """revaudit-...json / .html -> the bare report id used in /report/ links."""
    for ext in REPORT_EXTS:
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def report_label(name):
    """Short label for the 'recent reports' list: the assemblies and the
    time, never the random token, which would be meaningless clutter."""
    rid = report_id(name)
    m = re.fullmatch(r"revaudit-(\d{8}-\d{6})(?:-(.+))?-[A-Za-z0-9_-]{16}", rid)
    if not m:
        m = re.fullmatch(r"revaudit-(\d{8}-\d{6})(?:_(.+))?", rid)
    if not m:
        return name
    stamp, pns = m.group(1), m.group(2)
    when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    return f"{pns.replace('+', ' + ')} \u00b7 {when}" if pns else when


def report_dirs():
    """Every folder a report might live in: the configured one (usually a K:
    share, so the audit history is browsable by anyone), plus this app's own
    folder - where older reports sit, and where writes fall back if the
    configured folder is unreachable."""
    dirs = []
    try:
        configured = Path(config.report_dir())
        if configured.resolve() != HERE:
            dirs.append(configured)
    except (OSError, ValueError):
        pass
    dirs.append(HERE)
    return dirs


def write_report(rid, data):
    """Save a report as <rid>.html (data embedded), preferring the configured
    folder, falling back to the app folder if that can't be written. Returns
    the Path actually used."""
    for target_dir in report_dirs():
        try:
            return save_report(target_dir / f"{rid}.html", data)
        except OSError as exc:
            print(f"  report folder {target_dir} not writable ({exc}); trying next",
                  file=sys.stderr)
    raise OnshapeError("could not write the report to any folder")


def find_report(name):
    """The file behind `name` - a full revaudit-*.html / .json filename, or a
    bare report id (.html preferred) - wherever it lives, or None."""
    names = [name] if name.endswith(REPORT_EXTS) else [name + e for e in REPORT_EXTS]
    for d in report_dirs():
        for candidate_name in names:
            candidate = d / candidate_name
            try:
                if candidate.is_file() and candidate.parent == d:
                    return candidate
            except OSError:
                continue
    return None


def list_reports(limit=5):
    """The most recent reports across every report folder, newest first, one
    entry per report id (the JSON-file interlude left .json and .html twins)."""
    seen = {}
    for d in report_dirs():
        for ext in REPORT_EXTS:
            try:
                for p in d.glob(f"revaudit-*{ext}"):
                    rid = report_id(p.name)
                    if rid not in seen or p.stat().st_mtime > seen[rid].stat().st_mtime:
                        seen[rid] = p
            except OSError:
                continue
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


# Same shareable-link idea as reports: the random token in the name is the
# only thing that gates access, so a drawing package or STEP file link needs
# no password.
SHARED_ZIP_RE = r"[A-Za-z0-9_-]{1,80}-drawings-[A-Za-z0-9_-]{10,}\.zip"
SHARED_STEP_RE = r"[A-Za-z0-9_-]{1,80}-step-[A-Za-z0-9_-]{10,}\.step"

DRAWINGS_DIR = HERE / "drawings"


def new_zip_name(asm_pn):
    return f"{safe_filename(asm_pn)}-drawings-{secrets.token_urlsafe(12)}.zip"


def new_step_name(asm_pn):
    return f"{safe_filename(asm_pn)}-step-{secrets.token_urlsafe(12)}.step"

# Every file check the built-in web forms offer, in display order.
FILE_CHECK_DEFS = [
    {"name": "DXF", "field": "dxf", "ext": ".dxf",
     "default_dir": DEFAULT_DXF_DIR, "default_regex": "SHEET"},
    {"name": "SAT", "field": "sat", "ext": ".sat",
     "default_dir": DEFAULT_SAT_DIR, "default_regex": "TUBE"},
]

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
_run_lock = threading.Lock()


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


def get_dxf_index(folder, recursive):
    return get_file_index("DXF", folder, recursive, (".dxf",))


def store_uploaded_index(raw, gzipped):
    """Validate and save an uploaded index under its check name, then drop
    that check's cache entries."""
    if gzipped:
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    names = data.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("index must contain a list of filenames")
    name = str(data.get("name") or "DXF").strip() or "DXF"
    index_file_path(name).write_text(json.dumps(data), encoding="utf-8")
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
    if not hmac.compare_digest(given, token):
        return handler._send(b"bad token", 401, plain)
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 32 * 1024 * 1024:
        return handler._send(b"index too large", 413, plain)
    raw = handler.rfile.read(length)
    gzipped = handler.headers.get("Content-Encoding", "").lower() == "gzip"
    try:
        count, folder, name = store_uploaded_index(raw, gzipped)
    except Exception as exc:                                      # noqa: BLE001
        return handler._send(f"bad index: {exc}".encode(), 400, plain)
    print(f"  {name} index updated: {count} names from {folder}", file=sys.stderr)
    return handler._send(f"ok, {count} names stored for {name}".encode(), 200, plain)


# --------------------------------------------------------------------------
# "this was already audited" check
# --------------------------------------------------------------------------
#
# An audit is hundreds of Onshape calls - one per part, more for the parts
# with no drawing - and it is usually asked for again long before anything
# it looks at has moved. So before running, RevAudit asks one question
# ("what revision is this assembly at?") and looks for a report that already
# covers that exact revision. If it finds one, it offers that report instead
# of spending the calls, and the person decides which they want.

def report_pn_parts(rid):
    """(the assembly numbers written into a report id, whether any were left
    out of the name). A report name holds at most three, then '+<n>more' -
    and a report from before names carried assemblies holds none at all,
    which counts as "left out": such a report might cover anything."""
    m = re.fullmatch(r"revaudit-\d{8}-\d{6}(?:-(.+))?-[A-Za-z0-9_-]{16}", rid)
    if not m:
        m = re.fullmatch(r"revaudit-\d{8}-\d{6}(?:_(.+))?", rid)
    if not m or not m.group(1):
        return [], True
    parts = m.group(1).split("+")
    truncated = bool(re.fullmatch(r"\d+more", parts[-1]))
    return (parts[:-1] if truncated else parts), truncated


def report_may_cover(rid, fragment):
    """Could this report hold an audit of `fragment` (a part number as
    report_name_part writes it)? A filename test only - cheap enough to run
    over a whole folder, and it is what keeps the real check down to a
    handful of files instead of every report ever written."""
    parts, truncated = report_pn_parts(rid)
    return truncated or fragment in parts


def result_checks(result):
    """The optional extras one audited assembly in a saved report came with -
    'DXF', 'SAT', 'drawing PDFs', 'STEP' - so a previous report can say what
    it did and did not cover."""
    names = list((result.get("findings") or {}).get("files") or {})
    if result.get("drawingExport"):
        names.append("drawing PDFs")
    if result.get("stepExport"):
        names.append("STEP")
    return names


# Reports live on a share and carry their whole data block, so each one is
# parsed once and kept by (mtime, size) - a saved report is never edited in
# place, so that pair changing means it really is a different file.
_coverage_cache = {}
_COVERAGE_CACHE_MAX = 500


def report_coverage(path):
    """{assembly part number: {"revision", "generated", "checks"}} for one
    saved report. An assembly that never resolved is left out - nothing was
    audited, so nothing is covered. An unreadable file gives {}."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime, stat.st_size)
    hit = _coverage_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = load_report(path)
    except (OSError, ValueError):          # unreadable, or not a report at all
        data = None
    coverage = {}
    for result in (data or {}).get("results") or []:
        pn = result.get("partNumber")
        revision = (result.get("assembly") or {}).get("revision")
        if not pn or not revision:
            continue
        coverage[str(pn)] = {
            "revision": str(revision),
            "generated": (data or {}).get("generated") or "",
            "checks": result_checks(result),
        }
    if len(_coverage_cache) > _COVERAGE_CACHE_MAX:
        _coverage_cache.clear()
    _coverage_cache[str(path)] = (stamp, coverage)
    return coverage


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_prior_report(part_number, revision, allowed_ids=None, scan_limit=40):
    """The newest saved report that already audited this assembly at exactly
    this revision - {"id", "label", "path", "revision", "generated",
    "checks"} - or None.

    allowed_ids, when given, restricts the search to those report ids: the
    OAuth app only ever shows someone their own reports, so it must not
    offer one it would then refuse to open.
    """
    if not revision:
        return None
    fragment = report_name_part([part_number])
    allowed = None if allowed_ids is None else set(allowed_ids)
    newest = {}
    for d in report_dirs():
        for ext in REPORT_EXTS:
            try:
                candidates = list(d.glob(f"revaudit-*{ext}"))
            except OSError:
                continue
            for path in candidates:
                rid = report_id(path.name)
                if allowed is not None and rid not in allowed:
                    continue
                if not report_may_cover(rid, fragment):
                    continue
                if rid not in newest or _mtime(path) > _mtime(newest[rid]):
                    newest[rid] = path
    for path in sorted(newest.values(), key=_mtime, reverse=True)[:scan_limit]:
        entry = report_coverage(path).get(str(part_number))
        if entry and entry["revision"] == str(revision):
            return {"id": report_id(path.name), "label": report_label(path.name),
                    "path": str(path), **entry}
    return None


def current_assembly_revision(client, company_id, part_number):
    """The revision an audit would report on, from the single revision
    lookup that audit starts with anyway - the client remembers it, so
    asking here and then running costs no extra call."""
    revs = client.revisions(company_id, part_number, ET_ASSEMBLY)
    if not revs:
        return None
    return (current_revision(revs) or revs[0]).get("revision")


def prior_runs(client, company_id, names, allowed_ids=None):
    """One entry per assembly: {"name", "revision", "prior", "error"}. Costs
    a single (memoised) revision lookup each - no BOM, no per-part sweep."""
    entries = []
    for name in names:
        try:
            revision = current_assembly_revision(client, company_id, name)
        except OnshapeError as exc:
            # the shortcut check must never be what stops a requested run
            entries.append({"name": name, "revision": None, "prior": None,
                            "error": str(exc)})
            continue
        entries.append({
            "name": name,
            "revision": revision,
            "prior": find_prior_report(name, revision, allowed_ids),
            "error": None,
        })
    return entries


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


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

FORM_CSS = asset("form.css")
FORM_JS = asset("form.js")


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
        dir_val = html.escape(v.get(f"{field}_dir", check["default_dir"]))
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
    # Exports default to on whenever their folder is configured - a re-run
    # costs nothing for files already there - and follow the form on a resubmit.
    export_drawings = v.get("export_drawings", bool(DEFAULT_PDF_DIR))
    export_step = v.get("export_step", bool(DEFAULT_STEP_DIR))

    if report_names is None:
        reports = list_reports(5)
    else:
        # OAuth mode passes an explicit per-user list of names
        named = [find_report(n) for n in report_names]
        reports = sorted((p for p in named if p),
                         key=lambda p: p.stat().st_mtime, reverse=True)[:5]
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
            (about a second each)</label>
        </div>
        <label>PDF export folder
          <div class='hint'>Every released drawing (the assembly's own + every part's)
            is saved here, flat and named like the DXF/SAT folders, plus a zip of this
            run's files for sharing. Only used when the box above is checked.</div>
        </label>
        <input type='text' name='pdf_dir' id='pdf_dir'
               value='{html.escape(v.get("pdf_dir", DEFAULT_PDF_DIR))}'
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
               value='{html.escape(v.get("step_dir", DEFAULT_STEP_DIR))}'
               {"" if export_step else "disabled"}>

        <div class='row'>
          <input type='checkbox' name='force_export' id='force_export'
                 {"checked" if v.get("force_export") else ""}>
          <label for='force_export'>Re-fetch files that are already in the folders</label>
        </div>

        <button type='submit' id='btn'>Run audit</button>
        <div class='spin' id='spin'>Running &mdash; the first run against any given
          folder indexes it, which takes about 15 seconds. Later runs reuse it.
          Each drawing not yet in the PDF folder takes about a second on top.</div>
      </form>
      {recent}
      <footer>{credit_html()}</footer>
      <script>{FORM_JS}</script>
    """)


ALREADY_CSS = asset("already.css")


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
        only_new = f"""
        <form method='post' action='/run'>
          {hidden_fields_html(values, assemblies=pending_names, force_run="1")}
          <button type='submit' class='second'>Audit only the
            {len(pending)} not yet covered</button>
        </form>"""

    return page(f"{APP_NAME} - already audited", f"""
      <h1>{APP_NAME}</h1>
      <p class='sub'>Already audited at this revision.</p>
      <p>Nothing was requested from Onshape yet beyond each assembly's current
      revision. A full audit is hundreds of API calls, so here is what already
      exists:</p>
      {"".join(cards)}
      <p style='color:var(--muted);font-size:13px;margin:18px 0 6px'>
      The assembly is still at the revision that report was written for. Its
      parts can have moved since though &mdash; a drawing released, a DXF
      dropped in the folder &mdash; so run it again if you need today's answer
      rather than that one.</p>
      <div class='choices'>
        <form method='post' action='/run'>
          {hidden_fields_html(values, force_run="1")}
          <button type='submit'>Run the audit again</button>
        </form>
        {only_new}
        <a class='back' href='/'>&larr; Change the settings</a>
      </div>
      <footer>{credit_html()}</footer>
    """)


SHARE_CSS = asset("share.css")
SHARE_JS = asset("share.js")


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
            f"<span id='copied' style='color:var(--ok);font-size:12.5px;display:none'>"
            f"copied</span>{data_link}</div>"
            f"<script>{SHARE_JS}</script>"
        )
    bar = (
        f"<style>{SHARE_CSS}</style>"
        "<p style='margin:0 0 10px'><a class='back' href='/'>&larr; New audit</a>"
        f"<span style='color:var(--muted);margin-left:16px;font-size:13px'>"
        f"saved as {html.escape(saved_name)}</span></p>"
        f"{link_row}"
    )
    return report_html.replace("<div class='wrap'>", "<div class='wrap'>" + bar, 1)


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RevAudit"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _authorized(self):
        """Basic auth, enforced whenever REVAUDIT_PASSWORD is set."""
        password = getattr(self.server, "password", None)
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:                                    # noqa: BLE001
            return False
        _, _, given = decoded.partition(":")
        return hmac.compare_digest(given, password)

    def _challenge(self):
        body = page("Authentication required", "<h1>Authentication required</h1>")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="RevAudit"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A report generated with a random token in its name can be opened by
        # anyone holding the exact link, no password needed - that is what
        # makes it shareable, and it is checked before _authorized() on
        # purpose. A report from before link-sharing existed has no token, so
        # it falls through to the normal password gate below instead.
        if self.path.startswith("/report/"):
            name = self.path[len("/report/"):]
            if re.fullmatch(SHARED_REPORT_RE, name):
                return self._serve_report(name)
            if re.fullmatch(LEGACY_REPORT_RE, name):
                if not self._authorized():
                    return self._challenge()
                return self._serve_report(name)
            return self._send(page("Not found", "<h1>Report not found</h1>"), 404)

        if self.path.startswith("/download/"):
            name = self.path[len("/download/"):]
            target = DRAWINGS_DIR / name
            if not (target.is_file() and target.parent == DRAWINGS_DIR):
                return self._send(page("Not found", "<h1>File not found</h1>"), 404)
            if re.fullmatch(SHARED_ZIP_RE, name):
                return self._send(target.read_bytes(), 200, "application/zip")
            if re.fullmatch(SHARED_STEP_RE, name):
                return self._send(target.read_bytes(), 200, "application/octet-stream")
            return self._send(page("Not found", "<h1>File not found</h1>"), 404)

        if not self._authorized():
            return self._challenge()
        if self.path in ("/", "/index.html"):
            return self._send(form_page())
        if self.path == "/favicon.ico":
            return self._send(b"", 204, "image/x-icon")
        return self._send(page("Not found", "<h1>Not found</h1>"), 404)

    def _serve_report(self, name):
        """/report/<id> (or .html) re-renders the report from the data
        embedded in the saved file; /report/<id>.json returns that data. A
        report saved before data was embedded is served as it was written."""
        target = find_report(report_id(name) if name.endswith(REPORT_EXTS) else name)
        if target is None:
            target = find_report(name)
        if target is None:
            return self._send(page("Not found", "<h1>Report not found</h1>"), 404)
        try:
            data = load_report(target)
        except ValueError:
            data = None                       # pre-data HTML: nothing to re-render
        except OSError as exc:
            return self._send(page("Error", f"<h1>Unreadable report</h1>"
                                   f"<div class='err'>{html.escape(str(exc))}</div>"), 500)
        if name.endswith(".json"):
            if data is None:
                return self._send(page("Not found", "<h1>This report has no data block</h1>"
                                       "<p>It was saved before reports carried their data.</p>"),
                                  404)
            return self._send(data_json(data).encode("utf-8"), 200,
                              "application/json; charset=utf-8")
        if data is None:
            return self._send(target.read_bytes())
        rid = report_id(target.name)
        body = with_back_link(render_data(data), str(target), *self._share_urls(rid))
        return self._send(body.encode("utf-8"))

    def _share_urls(self, rid):
        """(page url, data url) for a report id, built from the Host header the
        request arrived on so the link is right at localhost and the LAN IP
        alike. A legacy id (no token) is password-gated, so no share link."""
        if not re.fullmatch(SHARED_REPORT_RE, rid):
            return None, None
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
        return f"http://{host}/report/{rid}", f"http://{host}/report/{rid}.json"

    def do_POST(self):
        if self.path == "/dxf-index":
            return handle_index_upload(self)     # token-authenticated, not a user
        if not self._authorized():
            return self._challenge()
        if self.path != "/run":
            return self._send(page("Not found", "<h1>Not found</h1>"), 404)

        length = int(self.headers.get("Content-Length") or 0)
        values = parse_run_form(parse_qs(self.rfile.read(length).decode("utf-8")))
        names = [n for n in re.split(r"[\s,]+", values["assemblies"]) if n]
        if not names:
            return self._send(form_page(values, "Enter at least one assembly number."))

        # Built from the Host header the request actually arrived on, so a
        # share link is correct whether this was reached at localhost or the
        # LAN IP - computed up front since the drawing export needs it too.
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")

        client = SERVER_STATE["client"]
        company_id = SERVER_STATE["company_id"]
        if values.get("force_run"):
            # an explicit re-run is the one place certainty matters more than
            # the saved calls, so nothing remembered is reused
            client.forget_revisions()
        else:
            try:
                entries = prior_runs(client, company_id, names)
            except OnshapeError as exc:
                return self._send(form_page(values, str(exc)))
            if any(e["prior"] for e in entries):
                return self._send(already_run_page(values, entries))

        try:
            with _run_lock:          # one audit at a time keeps API load predictable
                results = run_audit(names, values)
                run_exports(results, SERVER_STATE["client"], host, values)
        except OnshapeError as exc:
            return self._send(form_page(values, str(exc)))
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            return self._send(form_page(values, f"{type(exc).__name__}: {exc}"))

        data = build_report(results, self.server.base_url, folder_notes_for(values))
        rid = new_report_id(names)
        saved = write_report(rid, data)             # configured folder, or app folder
        body = with_back_link(render_data(data), str(saved), *self._share_urls(rid))
        self._send(body.encode("utf-8"))


def parse_run_form(fields):
    """The /run form's fields (from parse_qs) as the values dict the audit,
    export and re-rendered form all read."""
    values = {
        "assemblies": (fields.get("assemblies", [""])[0]).strip(),
        "export_drawings": bool(fields.get("export_drawings")),
        "pdf_dir": (fields.get("pdf_dir", [""])[0]).strip(),
        "export_step": bool(fields.get("export_step")),
        "step_dir": (fields.get("step_dir", [""])[0]).strip(),
        "force_export": bool(fields.get("force_export")),
        # set by the "already audited" page's buttons - run it regardless
        "force_run": bool(fields.get("force_run")),
    }
    for check in FILE_CHECK_DEFS:
        field = check["field"]
        values[f"{field}_dir"] = (fields.get(f"{field}_dir", [""])[0]).strip()
        values[f"{field}_recursive"] = bool(fields.get(f"{field}_recursive"))
        values[f"{field}_material_regex"] = (
            fields.get(f"{field}_material_regex", [check["default_regex"]])[0]
        ).strip()
    return values


def folder_notes_for(values):
    """The "DXF folder <path>" subtitle notes for the checks that ran."""
    return [
        f"{check['name']} folder {values[check['field'] + '_dir']}"
        for check in FILE_CHECK_DEFS if values.get(check["field"] + "_dir")
    ]


def run_exports(results, client, host, values):
    """The PDF and STEP exports the form asked for, attached to results."""
    force = bool(values.get("force_export"))
    if values.get("export_drawings") and values.get("pdf_dir"):
        export_drawings_for(results, client, host, values["pdf_dir"], force=force)
    if values.get("export_step") and values.get("step_dir"):
        export_step_for(results, client, host, values["step_dir"], force=force)


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


def run_audit(names, values):
    server_state = SERVER_STATE
    file_checks = build_file_checks(values)
    results = []
    for name in names:
        print(f"  auditing {name} ...", file=sys.stderr)
        try:
            results.append(audit_assembly(
                server_state["client"], server_state["company_id"], name,
                file_checks, workers=6, deep=True,
            ))
        except OnshapeError as exc:
            # one bad assembly should not lose the others
            print(f"  {name} failed: {exc}", file=sys.stderr)
            results.append({"partNumber": name, "errors": [str(exc)], "findings": {}})
    return results


def export_drawings_for(results, client, host, pdf_dir, force=False):
    """Export + zip drawings for every audited assembly that resolved, and
    attach a shareable download link to each result in place. One PDF export
    round trip per drawing, so this is the slow part of a run - callers show
    a "this takes a while" note before triggering it.

    Individual PDFs go into pdf_dir (whatever the user chose - typically the
    shared K: archive), flat and shared across assemblies in this run, same
    convention as the DXF/SAT folders. The zip is a one-off snapshot of this
    particular run, not part of that permanent archive, so it stays under
    this app's own DRAWINGS_DIR instead - the fixed, server-controlled root
    the public /download/ route trusts, regardless of what pdf_dir is.
    """
    DRAWINGS_DIR.mkdir(exist_ok=True)
    pdf_dir = Path(pdf_dir)
    for result in results:
        if not result.get("assembly"):
            continue
        asm_pn = result["partNumber"]
        print(f"  exporting drawings for {asm_pn} to {pdf_dir} ...", file=sys.stderr)
        entries = export_assembly_drawings(client, result, pdf_dir, workers=6,
                                           force=force)
        zip_name = new_zip_name(asm_pn)
        zip_path = zip_pdfs(entries, DRAWINGS_DIR / zip_name)
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

    The permanent file goes into step_dir (typically the shared K: archive),
    flat and named like the DXF/SAT/PDF folders. A copy under this app's own
    DRAWINGS_DIR - the fixed, server-controlled root the /download/ route
    trusts - gets a random-token name for the shareable link, same idea as
    the drawing package zip; there's only one file so it isn't zipped first.
    """
    DRAWINGS_DIR.mkdir(exist_ok=True)
    step_dir = Path(step_dir)
    for result in results:
        if not result.get("assembly"):
            continue
        asm_pn = result["partNumber"]
        print(f"  exporting STEP for {asm_pn} to {step_dir} ...", file=sys.stderr)
        entry = export_assembly_step_file(client, result, step_dir, force=force)
        if entry and not entry.get("error"):
            step_name = new_step_name(asm_pn)
            shutil.copyfile(entry["path"], DRAWINGS_DIR / step_name)
            entry["url"] = f"http://{host}/download/{step_name}"
            print("    already there" if entry.get("skipped") else "    exported",
                  file=sys.stderr)
        elif entry:
            print(f"    failed: {entry['error']}", file=sys.stderr)
        result["stepExport"] = entry


SERVER_STATE = {}

# Everything the console shows is also appended here, so a server that died
# overnight in a minimised window can still be diagnosed the next morning.
LOG_PATH = HERE / "revaudit.log"
LOG_KEEP = 512 * 1024          # bytes retained when the file grows past 4x this


class _Tee:
    """Writes to the console and the log file; a broken console (closed
    window, redirected handle) never takes the server down with it."""

    def __init__(self, console, log_file):
        self.console = console
        self.log_file = log_file
        self.lock = threading.Lock()

    def write(self, text):
        for stream in (self.console, self.log_file):
            try:
                with self.lock:
                    stream.write(text)
                    stream.flush()
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self):
        for stream in (self.console, self.log_file):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        return False


def open_log():
    """Append to revaudit.log, trimming it to the newest LOG_KEEP bytes when
    it has grown large. Returns None (console only) if the file can't be
    written - a log must never be the reason the server won't start."""
    try:
        if LOG_PATH.is_file() and LOG_PATH.stat().st_size > 4 * LOG_KEEP:
            tail = LOG_PATH.read_bytes()[-LOG_KEEP:]
            LOG_PATH.write_bytes(tail[tail.find(b"\n") + 1:])
        return open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    except OSError:
        return None


def main(argv=None):
    log_file = open_log()
    if log_file is not None:
        sys.stdout = _Tee(sys.stdout, log_file)
        sys.stderr = _Tee(sys.stderr, log_file)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== {APP_NAME} starting {stamp}  (pid {os.getpid()}) ===", file=sys.stderr)
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException as exc:                              # noqa: BLE001
        # KeyboardInterrupt included: the log says *why* the server is gone.
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== {APP_NAME} died {stamp}: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise
    finally:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== {APP_NAME} stopped {stamp} ===", file=sys.stderr)


def _main(argv):
    parser = argparse.ArgumentParser(description="RevAudit web UI")
    parser.add_argument("--port", type=int, default=None,
                        help="overrides revaudit.conf")
    parser.add_argument("--host", default=None,
                        help="overrides revaudit.conf; 0.0.0.0 allows the LAN")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)

    host = args.host or config.bind_host()
    port = args.port or config.port()

    load_dotenv(HERE / ".env")
    access = os.environ.get("ONSHAPE_ACCESS_KEY")
    secret = os.environ.get("ONSHAPE_SECRET_KEY")
    if not access or not secret:
        print("Missing credentials. Fill in .env first "
              "(or run: py -3.13 import_key.py).", file=sys.stderr)
        return 2

    base_url = os.environ.get("ONSHAPE_BASE_URL") or DEFAULT_BASE_URL
    client = OnshapeClient(base_url, access, secret,
                           revision_cache_seconds=config.revision_cache_seconds())
    company_id = os.environ.get("ONSHAPE_COMPANY_ID")
    if not company_id:
        companies = client.companies()
        if not companies:
            print("No company found for these credentials.", file=sys.stderr)
            return 2
        company_id = companies[0]["id"]
        print(f"Company: {companies[0].get('name', '').strip()}")

    SERVER_STATE["client"] = client
    SERVER_STATE["company_id"] = company_id

    # Binding beyond localhost puts your Onshape key behind a page anyone on the
    # network can drive, so refuse to do it without a password.
    local_only = host in ("127.0.0.1", "localhost", "::1")
    password = os.environ.get("REVAUDIT_PASSWORD")
    if not local_only and not password:
        print(
            f"Refusing to bind to {host} without a password.\n\n"
            "Anyone who can reach this port would be able to run audits using YOUR\n"
            "Onshape key, with no login. Set a password first:\n\n"
            "  add REVAUDIT_PASSWORD=something-long to .env, then restart\n\n"
            "Or set bind_host = 127.0.0.1 in revaudit.conf to serve this machine only.",
            file=sys.stderr,
        )
        return 2

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"Could not listen on {host}:{port}: {exc}\n"
              "Is another RevAudit (or something else) already using that port?",
              file=sys.stderr)
        return 2
    httpd.base_url = base_url
    httpd.password = password

    print(f"\n{APP_NAME} is running.")
    if local_only:
        print(f"  This machine only:  http://localhost:{port}")
    else:
        advertised = config.advertised_address()
        if advertised:
            print(f"  On the network:     http://{advertised}:{port}")
        else:
            for ip in config.detect_lan_ips():
                print(f"  On the network:     http://{ip}:{port}")
            print("  (pin one by setting advertised_address in revaudit.conf)")
        print(f"  This machine:       http://localhost:{port}")
    print("\nClose this window to stop RevAudit.\n")

    if args.open_browser:
        webbrowser.open(f"http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
