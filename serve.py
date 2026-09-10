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
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from audit_core import FileIndex, audit_assembly
from onshape_client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError
from pdf_export import (export_assembly_drawings, export_assembly_step_file,
                        safe_filename, zip_pdfs)
import config
from revaudit import APP_NAME, CSS, credit_html, load_dotenv, render_report

HERE = Path(__file__).resolve().parent
config.ensure_from_example()          # so `launch.bat` alone still works
DEFAULT_DXF_DIR = config.folder("dxf")
DEFAULT_SAT_DIR = config.folder("sat")
DEFAULT_PDF_DIR = config.folder("pdf")
DEFAULT_STEP_DIR = config.folder("step")

# revaudit-<timestamp>-<random token>.html - the token is what makes a report
# link safe to hand out without also handing out the site password: nobody can
# guess it, so holding the link is proof enough that you were given it.
SHARED_REPORT_RE = r"revaudit-\d{8}-\d{6}-[A-Za-z0-9_-]{10,}\.html"

# revaudit-<timestamp>.html with no token - reports made before link sharing
# existed. They carry no secret of their own, so they still need the site
# password rather than being reachable by anyone who finds the filename.
LEGACY_REPORT_RE = r"revaudit-\d{8}-\d{6}\.html"


def new_report_name():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"revaudit-{stamp}-{secrets.token_urlsafe(12)}.html"


def report_label(filename):
    """Short label for the 'recent reports' list - just the timestamp, not
    the random token, which would be meaningless clutter to read."""
    m = re.match(r"revaudit-(\d{8}-\d{6})-", filename)
    return m.group(1) if m else filename


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


def write_report(name, html_text):
    """Save a report, preferring the configured folder, falling back to the
    app folder if that can't be written. Returns the Path actually used."""
    for target_dir in report_dirs():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / name
            path.write_text(html_text, encoding="utf-8")
            return path
        except OSError as exc:
            print(f"  report folder {target_dir} not writable ({exc}); trying next",
                  file=sys.stderr)
    raise OnshapeError("could not write the report to any folder")


def find_report(name):
    """The report file for `name`, wherever it lives, or None."""
    for d in report_dirs():
        candidate = d / name
        try:
            if candidate.is_file() and candidate.parent == d:
                return candidate
        except OSError:
            continue
    return None


def list_reports(limit=5):
    """The most recent report files across every report folder, newest first."""
    seen = {}
    for d in report_dirs():
        try:
            for p in d.glob("revaudit-*.html"):
                if p.name not in seen or p.stat().st_mtime > seen[p.name].stat().st_mtime:
                    seen[p.name] = p
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


# Indexing a network folder takes ~15s, so keep it between runs.
_index_cache = {}
_index_lock = threading.Lock()
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


def get_file_index(name, folder, recursive, extensions=(".dxf",)):
    """Scan the folder live; fall back to an uploaded index when this host
    cannot see the share (the usual case for a remote deployment)."""
    key = (name, str(folder), bool(recursive))
    with _index_lock:
        if key in _index_cache:
            return _index_cache[key]
    try:
        index = FileIndex(folder, recursive, extensions)
    except OnshapeError:
        index = load_uploaded_index(name, extensions)
        if index is None:
            raise
    with _index_lock:
        _index_cache[key] = index
    return index


# kept for anything still calling the pre-multi-check name
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
# pages
# --------------------------------------------------------------------------

FORM_CSS = """
.form{max-width:640px}
label{display:block;margin:18px 0 6px;font-weight:600;font-size:14px}
.hint{color:var(--muted);font-weight:400;font-size:12.5px;margin-top:3px}
input[type=text]{width:100%;padding:10px 12px;border-radius:7px;
  border:1px solid var(--line);background:var(--panel);color:var(--fg);font-size:14px;
  font-family:inherit}
input[type=text]:focus{outline:2px solid var(--accent);outline-offset:1px}
.row{display:flex;align-items:center;gap:9px;margin:16px 0}
.row label{margin:0;font-weight:400}
button{margin-top:26px;padding:12px 26px;border-radius:7px;border:0;
  background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer;
  font-family:inherit}
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.6;cursor:default}
.recent{margin-top:34px;font-size:13.5px}
.recent a{color:var(--accent);text-decoration:none;margin-right:14px}
.spin{display:none;margin-top:18px;color:var(--muted);font-size:14px}
.spin.on{display:block}
"""


def page(title, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}{FORM_CSS}</style></head>"
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
            f"<a href='/report/{html.escape(p.name)}'>{html.escape(report_label(p.name))}</a>"
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
                 {"checked" if v.get("export_drawings") else ""}>
          <label for='export_drawings'>Export a PDF of every drawing &mdash; adds
            roughly 1 second per drawing, so off by default</label>
        </div>
        <label>PDF export folder
          <div class='hint'>Every released drawing (the assembly's own + every part's)
            is saved here, flat and named like the DXF/SAT folders, plus a zip of this
            run's files for sharing. Only used when the box above is checked.</div>
        </label>
        <input type='text' name='pdf_dir' id='pdf_dir'
               value='{html.escape(v.get("pdf_dir", DEFAULT_PDF_DIR))}'
               {"" if v.get("export_drawings") else "disabled"}>

        <div class='row'>
          <input type='checkbox' name='export_step' id='export_step'
                 onchange="document.getElementById('step_dir').disabled = !this.checked"
                 {"checked" if v.get("export_step") else ""}>
          <label for='export_step'>Export each assembly's 3D geometry as STEP &mdash;
            off by default</label>
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
               {"" if v.get("export_step") else "disabled"}>

        <button type='submit' id='btn'>Run audit</button>
        <div class='spin' id='spin'>Running &mdash; the first run against any given
          folder indexes it, which takes about 15 seconds. Later runs reuse it.
          Exporting drawings takes about a second each, on top of that.</div>
      </form>
      {recent}
      <footer>{credit_html()}</footer>
      <script>
      function go(){{
        document.getElementById('btn').disabled = true;
        document.getElementById('btn').textContent = 'Running...';
        document.getElementById('spin').classList.add('on');
      }}
      </script>
    """)


SHARE_CSS = """
.share{display:flex;gap:8px;align-items:center;margin:0 0 18px;flex-wrap:wrap}
.share input{flex:1;min-width:260px;padding:8px 10px;border-radius:6px;
  border:1px solid var(--line);background:var(--panel);color:var(--fg);
  font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.share button{margin:0;padding:8px 14px;font-size:13px;border-radius:6px;
  border:1px solid var(--line);background:var(--panel2);color:var(--fg);cursor:pointer}
.share button:hover{background:var(--panel)}
.share .back{color:var(--accent);text-decoration:none;font-size:13.5px;white-space:nowrap}
"""


def with_back_link(report_html, saved_name, share_url=None):
    """share_url, if given, is shown as a copyable link - the report's filename
    carries enough random entropy that anyone with the link can open it without
    the site password, so it is safe to hand to someone who only needs to see
    this one result."""
    link_row = ""
    if share_url:
        link_row = (
            f"<div class='share'><input readonly id='shareUrl' "
            f"value='{html.escape(share_url)}' onclick='this.select()'>"
            f"<button type='button' onclick='copyShareUrl()'>Copy link</button>"
            f"<span id='copied' style='color:var(--ok);font-size:12.5px;display:none'>"
            f"copied</span></div>"
            "<script>function copyShareUrl(){"
            "const el=document.getElementById('shareUrl');el.select();"
            "navigator.clipboard.writeText(el.value).then(()=>{"
            "const c=document.getElementById('copied');c.style.display='inline';"
            "setTimeout(()=>c.style.display='none',1500);});}</script>"
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
            target = find_report(name)      # searches the report folder + app folder
            if target and re.fullmatch(SHARED_REPORT_RE, name):
                return self._send(target.read_bytes())
            if target and re.fullmatch(LEGACY_REPORT_RE, name):
                if not self._authorized():
                    return self._challenge()
                return self._send(target.read_bytes())
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

    def do_POST(self):
        if self.path == "/dxf-index":
            return handle_index_upload(self)     # token-authenticated, not a user
        if not self._authorized():
            return self._challenge()
        if self.path != "/run":
            return self._send(page("Not found", "<h1>Not found</h1>"), 404)

        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        values = {
            "assemblies": (fields.get("assemblies", [""])[0]).strip(),
            "export_drawings": bool(fields.get("export_drawings")),
            "pdf_dir": (fields.get("pdf_dir", [""])[0]).strip(),
            "export_step": bool(fields.get("export_step")),
            "step_dir": (fields.get("step_dir", [""])[0]).strip(),
        }
        for check in FILE_CHECK_DEFS:
            field = check["field"]
            values[f"{field}_dir"] = (fields.get(f"{field}_dir", [""])[0]).strip()
            values[f"{field}_recursive"] = bool(fields.get(f"{field}_recursive"))
            values[f"{field}_material_regex"] = (
                fields.get(f"{field}_material_regex", [check["default_regex"]])[0]
            ).strip()

        names = [n for n in re.split(r"[\s,]+", values["assemblies"]) if n]
        if not names:
            return self._send(form_page(values, "Enter at least one assembly number."))

        # Built from the Host header the request actually arrived on, so a
        # share link is correct whether this was reached at localhost or the
        # LAN IP - computed up front since the drawing export needs it too.
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")

        try:
            with _run_lock:          # one audit at a time keeps API load predictable
                results = run_audit(names, values)
                if values["export_drawings"] and values["pdf_dir"]:
                    export_drawings_for(results, SERVER_STATE["client"], host,
                                        values["pdf_dir"])
                if values["export_step"] and values["step_dir"]:
                    export_step_for(results, SERVER_STATE["client"], host,
                                    values["step_dir"])
        except OnshapeError as exc:
            return self._send(form_page(values, str(exc)))
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            return self._send(form_page(values, f"{type(exc).__name__}: {exc}"))

        folder_notes = [
            f"{check['name']} folder {values[check['field'] + '_dir']}"
            for check in FILE_CHECK_DEFS if values[check["field"] + "_dir"]
        ]
        report = render_report(results, self.server.base_url, folder_notes)
        name = new_report_name()
        saved = write_report(name, report)          # configured folder, or app folder
        share_url = f"http://{host}/report/{name}"
        self._send(with_back_link(report, str(saved), share_url).encode("utf-8"))


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


def export_drawings_for(results, client, host, pdf_dir):
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
        entries = export_assembly_drawings(client, result, pdf_dir, workers=6)
        zip_name = new_zip_name(asm_pn)
        zip_path = zip_pdfs(entries, DRAWINGS_DIR / zip_name)
        failed = len([e for e in entries if e.get("error")])
        print(f"    {len(entries) - failed} exported, {failed} failed", file=sys.stderr)
        result["drawingExport"] = {
            "entries": entries,
            "zipUrl": f"http://{host}/download/{zip_name}" if zip_path else None,
        }


def export_step_for(results, client, host, step_dir):
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
        entry = export_assembly_step_file(client, result, step_dir)
        if entry and not entry.get("error"):
            step_name = new_step_name(asm_pn)
            shutil.copyfile(entry["path"], DRAWINGS_DIR / step_name)
            entry["url"] = f"http://{host}/download/{step_name}"
            print("    exported", file=sys.stderr)
        elif entry:
            print(f"    failed: {entry['error']}", file=sys.stderr)
        result["stepExport"] = entry


SERVER_STATE = {}


def main(argv=None):
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
    client = OnshapeClient(base_url, access, secret)
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

    httpd = ThreadingHTTPServer((host, port), Handler)
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
