#!/usr/bin/env python3
"""
RevAudit web UI - type an assembly number in a browser, get the report.

    py -3.13 serve.py            then open http://localhost:8000
    py -3.13 serve.py --open     opens the browser for you
    py -3.13 serve.py --port 9000 --host 0.0.0.0    reachable from other machines

Runs the same audit as the CLI. Credentials come from .env exactly as before,
so the key never reaches the browser. One API key serves everyone, behind
the shared REVAUDIT_PASSWORD; oauth_app.py is the sign-in-per-person variant.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import html
import os
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import config
from onshape_client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError
from report import APP_NAME, data_json, load_report, render_data
from report_store import (LEGACY_REPORT_RE, REPORT_EXTS, SHARED_REPORT_RE, find_report,
                          prior_runs, report_id)
from webapp import (QuietServer, already_run_page, assembly_names, audit_and_report,
                    form_error, form_page, handle_index_upload, page, read_form, run_lock,
                    send_download, with_back_link)

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "RevAudit"
    # an idle or stalled connection gives its thread back after this long
    timeout = 120

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
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return False
        _, _, given = decoded.partition(":")
        # bytes, not str: compare_digest refuses non-ASCII str
        return hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))

    def _challenge(self):
        body = page("Authentication required", "<h1>Authentication required</h1>")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="RevAudit", charset="UTF-8"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host(self):
        """The host:port this request arrived on, so links are right at
        localhost and at the LAN address alike."""
        return self.headers.get("Host") or f"localhost:{self.server.server_port}"

    def do_GET(self):
        path = urlsplit(self.path).path
        # A report generated with a random token in its name can be opened by
        # anyone holding the exact link, no password needed - that is what
        # makes it shareable, and it is checked before _authorized() on
        # purpose. A report from before link-sharing existed has no token, so
        # it falls through to the normal password gate below instead.
        if path.startswith("/report/"):
            name = path[len("/report/"):]
            if re.fullmatch(SHARED_REPORT_RE, name):
                return self._serve_report(name)
            if re.fullmatch(LEGACY_REPORT_RE, name):
                if not self._authorized():
                    return self._challenge()
                return self._serve_report(name)
            return self._send(page("Not found", "<h1>Report not found</h1>"), 404)

        if path.startswith("/download/"):
            return send_download(self, path[len("/download/"):])

        if not self._authorized():
            return self._challenge()
        if path in ("/", "/index.html"):
            return self._send(form_page())
        if path == "/favicon.ico":
            return self._send(b"", 204, "image/x-icon")
        return self._send(page("Not found", "<h1>Not found</h1>"), 404)

    def do_HEAD(self):
        """A bare "is it up?" answer for monitors and port probes - no body,
        so nothing behind the password is revealed."""
        self.send_response(200 if urlsplit(self.path).path in ("/", "/index.html") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            return self._send(data_json(data), 200, "application/json; charset=utf-8")
        if data is None:
            return self._send(target.read_bytes())
        rid = report_id(target.name)
        return self._send(with_back_link(render_data(data), str(target), *self._share_urls(rid)))

    def _share_urls(self, rid):
        """(page url, data url) for a report id. A legacy id (no token) is
        password-gated, so it gets no share link."""
        if not re.fullmatch(SHARED_REPORT_RE, rid):
            return None, None
        host = self._host()
        return f"http://{host}/report/{rid}", f"http://{host}/report/{rid}.json"

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/dxf-index":
            return handle_index_upload(self)     # token-authenticated, not a user
        if not self._authorized():
            return self._challenge()
        if path != "/run":
            return self._send(page("Not found", "<h1>Not found</h1>"), 404)

        values = read_form(self)
        if values is None:
            return self._send(page("Too large", "<h1>That form is too large</h1>"), 413)
        error = form_error(values)
        if error:
            return self._send(form_page(values, error))
        names = assembly_names(values)

        client = SERVER_STATE["client"]
        try:
            company_id = server_company_id()
        except OnshapeError as exc:
            return self._send(form_page(values, str(exc)))
        if values.get("force_run"):
            if values.get("fresh"):
                # "Run the audit again" - the one place certainty matters more
                # than the saved calls, so nothing remembered is reused
                client.forget_revisions()
        else:
            entries = prior_runs(client, company_id, names)
            if any(e["prior"] for e in entries):
                return self._send(already_run_page(values, entries))

        try:
            with run_lock:          # one audit at a time keeps API load predictable
                rid, saved, data = audit_and_report(client, company_id, names, values,
                                                    self._host(), self.server.base_url)
        except (OnshapeError, OSError) as exc:
            return self._send(form_page(values, str(exc)))
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            return self._send(form_page(values, f"{type(exc).__name__}: {exc}"))
        self._send(with_back_link(render_data(data), str(saved), *self._share_urls(rid)))


SERVER_STATE = {}


def server_company_id():
    """The Onshape company audits run against: ONSHAPE_COMPANY_ID, else the
    first company the API key belongs to (looked up once)."""
    company_id = SERVER_STATE.get("company_id")
    if not company_id:
        companies = SERVER_STATE["client"].companies()
        if not companies:
            raise OnshapeError("No Onshape company found for these credentials.")
        company_id = SERVER_STATE["company_id"] = companies[0]["id"]
        print(f"Company: {companies[0].get('name', '').strip()}", file=sys.stderr)
    return company_id

# Everything the console shows is also appended here, so a server that died
# overnight in a minimised window can still be diagnosed the next morning.
# REVAUDIT_LOG moves it, so a test instance doesn't mix into the live log.
LOG_PATH = Path(os.environ.get("REVAUDIT_LOG") or HERE / "revaudit.log")
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

    config.ensure_from_example()      # so `launch.bat` alone still works
    host = args.host or config.bind_host()
    port = args.port or config.port()

    config.load_dotenv(HERE / ".env")
    access = os.environ.get("ONSHAPE_ACCESS_KEY")
    secret = os.environ.get("ONSHAPE_SECRET_KEY")
    if not access or not secret:
        print("Missing credentials. Fill in .env first "
              "(or run: py -3.13 import_key.py).", file=sys.stderr)
        return 2

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

    base_url = os.environ.get("ONSHAPE_BASE_URL") or DEFAULT_BASE_URL
    SERVER_STATE["client"] = OnshapeClient(
        base_url, access, secret, revision_cache_seconds=config.revision_cache_seconds())
    SERVER_STATE["company_id"] = os.environ.get("ONSHAPE_COMPANY_ID") or None
    if not SERVER_STATE["company_id"]:
        try:
            server_company_id()
        except OnshapeError as exc:
            # Onshape unreachable at boot: serve the page anyway and look the
            # company up on the first audit, rather than not starting at all
            print(f"Company lookup failed ({exc}); will retry on the first audit.",
                  file=sys.stderr)

    try:
        httpd = QuietServer((host, port), Handler)
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
