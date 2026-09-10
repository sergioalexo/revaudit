#!/usr/bin/env python3
"""
RevAudit with Onshape OAuth - each person signs in with their own Onshape account.

    py -3.13 oauth_app.py                 http://localhost:8000
    py -3.13 oauth_app.py --host 0.0.0.0 --port 8000

No shared API key. Every user gets a token scoped to their own Onshape
permissions, tokens refresh themselves, and access dies when their Onshape
account is disabled.

Register the app first at https://dev-portal.onshape.com (OAuth applications),
then put this in .env:

    ONSHAPE_OAUTH_CLIENT_ID=...
    ONSHAPE_OAUTH_CLIENT_SECRET=...
    ONSHAPE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth/callback
    ONSHAPE_OAUTH_SCOPE=OAuth2Read OAuth2ReadPII

For anyone else to sign in from a LAN IP or another hostname, add each one
Onshape should accept a callback for:

    ONSHAPE_OAUTH_ALLOWED_HOSTS=localhost:8000,192.168.1.50:8000

...and register a matching "http://<host>/oauth/callback" redirect URL on the
Onshape app for each entry - localhost and 127.0.0.1 on --port are always
included automatically. The app derives which redirect_uri to use per request
from the incoming Host header, so one running instance answers correctly at
every registered address rather than needing a separate deployment per host.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from audit_core import DxfIndex, audit_assembly
from onshape_client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError
from revaudit import APP_NAME, CSS, credit_html, load_dotenv, render_report
from serve import (FORM_CSS, find_report, form_page, get_dxf_index,
                   handle_index_upload, page, with_back_link, write_report)

HERE = Path(__file__).resolve().parent

AUTHORIZE_URL = "https://oauth.onshape.com/oauth/authorize"
TOKEN_URL = "https://oauth.onshape.com/oauth/token"

SESSION_COOKIE = "revaudit_session"
SESSION_MAX_AGE = 12 * 3600          # sign in again after 12 hours
TOKEN_SKEW = 300                     # refresh 5 minutes before expiry

_sessions = {}                       # sid -> session dict
_pending = {}                        # state -> created_at
_lock = threading.Lock()

CONFIG = {}


# --------------------------------------------------------------------------
# OAuth plumbing
# --------------------------------------------------------------------------

def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise OnshapeError(f"Token endpoint returned {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OnshapeError(f"Could not reach the token endpoint: {exc}")


def exchange_code(code, redirect_uri):
    # redirect_uri must be byte-identical to the one used in the /login
    # redirect, or Onshape refuses the exchange - so it is threaded through
    # per-request rather than read from a single global.
    return post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
        "redirect_uri": redirect_uri,
    })


def refresh_token(refresh):
    return post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
    })


def store_tokens(session, payload):
    session["access_token"] = payload["access_token"]
    # Onshape returns a new refresh token each time; keep the old one if absent.
    session["refresh_token"] = payload.get("refresh_token", session.get("refresh_token"))
    session["expires_at"] = time.time() + int(payload.get("expires_in", 3600))


def valid_token(session):
    """Return a live access token, refreshing it if it is about to expire."""
    if time.time() < session.get("expires_at", 0) - TOKEN_SKEW:
        return session["access_token"]
    if not session.get("refresh_token"):
        raise OnshapeError("Session expired. Sign in again.")
    store_tokens(session, refresh_token(session["refresh_token"]))
    return session["access_token"]


def client_for(session):
    client = OnshapeClient(CONFIG["base_url"], bearer_token=valid_token(session))
    return client


def sweep():
    """Drop stale sessions and unused state values."""
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items()
                    if now - v.get("created", 0) > SESSION_MAX_AGE]:
            _sessions.pop(sid, None)
        for state in [s for s, v in _pending.items() if now - v["created"] > 600]:
            _pending.pop(state, None)


def redirect_uri_for(handler):
    """Build /oauth/callback on whichever host+scheme this request arrived on,
    so the same instance answers correctly at localhost, a LAN IP, or a proxied
    hostname - as long as that host is in the allowlist and was registered as a
    redirect URL on the Onshape app.

    A forwarded scheme header is honoured only if the request came from a
    trusted reverse proxy, so a client cannot lie about https to skip the
    Secure cookie flag.
    """
    host = handler.headers.get("Host", "")
    if host not in CONFIG["allowed_hosts"]:
        return None
    scheme = "http"
    if CONFIG["trust_proxy"]:
        fwd = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
        if fwd in ("http", "https"):
            scheme = fwd
    return f"{scheme}://{host}/oauth/callback"


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

def landing_page(error=None):
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    return page(APP_NAME, f"""
      <h1>{APP_NAME}</h1>
      <p class='sub'>Release, drawing and DXF audit for Onshape assemblies.</p>
      {err}
      <p>Sign in with your own Onshape account. You will only ever see the
         documents your Onshape permissions already allow.</p>
      <p style='margin-top:28px'>
        <a href='/login' style='display:inline-block;padding:12px 26px;
           border-radius:7px;background:var(--accent);color:#fff;font-weight:600;
           text-decoration:none'>Sign in with Onshape</a>
      </p>
      <footer>{credit_html()}</footer>
    """)


def header_for(session):
    who = html.escape(session.get("name") or "signed in")
    return (
        f"<p style='margin:0 0 18px;font-size:13px;color:var(--muted)'>{who}"
        " &middot; <a href='/logout' style='color:var(--accent);"
        "text-decoration:none'>sign out</a></p>"
    )


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RevAudit"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ---------------------------------------------------------

    def _send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for key, value in (headers or []):
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None, None
        cookie = SimpleCookie(raw)
        if SESSION_COOKIE not in cookie:
            return None, None
        sid = cookie[SESSION_COOKIE].value
        with _lock:
            session = _sessions.get(sid)
        if session and time.time() - session.get("created", 0) > SESSION_MAX_AGE:
            with _lock:
                _sessions.pop(sid, None)
            return None, None
        return sid, session

    def _cookie_header(self, sid, https):
        secure = "; Secure" if https else ""
        return ("Set-Cookie",
                f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Lax; Path=/"
                f"; Max-Age={SESSION_MAX_AGE}{secure}")

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        sweep()
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        _, session = self._session()

        if path == "/favicon.ico":
            return self._send(b"", 204, "image/x-icon")

        if path == "/login":
            redirect_uri = redirect_uri_for(self)
            if not redirect_uri:
                return self._send(landing_page(
                    f"This app is not configured to serve requests for host "
                    f"'{html.escape(self.headers.get('Host', ''))}'. Ask an admin to "
                    "add it to ONSHAPE_OAUTH_ALLOWED_HOSTS and register the matching "
                    "redirect URL with Onshape."), 400)
            state = secrets.token_urlsafe(24)
            with _lock:
                _pending[state] = {"created": time.time(), "redirect_uri": redirect_uri}
            params = {
                "response_type": "code",
                "client_id": CONFIG["client_id"],
                "redirect_uri": redirect_uri,
                "state": state,
            }
            if CONFIG.get("scope"):
                params["scope"] = CONFIG["scope"]
            return self._redirect(AUTHORIZE_URL + "?" + urllib.parse.urlencode(params))

        if path == "/oauth/callback":
            if "error" in query:
                return self._send(landing_page(
                    f"Onshape refused the sign-in: {query['error'][0]}"))
            state = (query.get("state") or [""])[0]
            with _lock:
                known = _pending.pop(state, None)
            if not state or known is None:
                # guards against a forged callback (CSRF)
                return self._send(landing_page(
                    "Sign-in could not be verified. Start again."), 400)
            code = (query.get("code") or [""])[0]
            if not code:
                return self._send(landing_page("Onshape did not return a code."), 400)
            try:
                # must match the redirect_uri sent to /login for this same state
                payload = exchange_code(code, known["redirect_uri"])
                session = {"created": time.time()}
                store_tokens(session, payload)
                info = OnshapeClient(
                    CONFIG["base_url"], bearer_token=session["access_token"]
                ).get("/api/v6/users/sessioninfo") or {}
                session["name"] = info.get("name") or info.get("email") or "signed in"
                companies = OnshapeClient(
                    CONFIG["base_url"], bearer_token=session["access_token"]
                ).companies()
                session["company_id"] = companies[0]["id"] if companies else None

                # Anyone with any Onshape account can reach the sign-in page.
                # If an allowed company is configured, only its members get in.
                allowed = CONFIG.get("allowed_company")
                if allowed:
                    if allowed not in {c.get("id") for c in companies}:
                        print(f"  denied: {session.get('name')} is not in {allowed}",
                              file=sys.stderr)
                        return self._send(landing_page(
                            "Your Onshape account is not a member of the company "
                            "this instance serves, so there is nothing here for you."
                        ), 403)
                    session["company_id"] = allowed
            except OnshapeError as exc:
                return self._send(landing_page(str(exc)), 502)
            sid = secrets.token_urlsafe(32)
            with _lock:
                _sessions[sid] = session
            https = known["redirect_uri"].startswith("https://")
            return self._redirect("/", [self._cookie_header(sid, https)])

        if path == "/logout":
            sid, _ = self._session()
            if sid:
                with _lock:
                    _sessions.pop(sid, None)
            return self._redirect("/", [
                ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")])

        if not session:
            return self._send(landing_page())

        if path in ("/", "/index.html"):
            body = form_page(report_names=session.get("reports", [])).decode("utf-8")
            return self._send(body.replace("<h1>", header_for(session) + "<h1>", 1))

        if path.startswith("/report/"):
            name = path[len("/report/"):]
            target = find_report(name)
            if (name in session.get("reports", [])
                    and re.fullmatch(r"revaudit-[\w.-]+\.html", name)
                    and target is not None):
                return self._send(target.read_bytes())
            # another user's report, or one from before this session
            return self._send(page("Not found", "<h1>Report not found</h1>"), 404)

        return self._send(page("Not found", "<h1>Not found</h1>"), 404)

    def do_POST(self):
        sweep()
        if urlparse(self.path).path == "/dxf-index":
            return handle_index_upload(self)     # machine agent, no user session
        _, session = self._session()
        if not session:
            return self._send(landing_page("Please sign in first."), 401)
        if urlparse(self.path).path != "/run":
            return self._send(page("Not found", "<h1>Not found</h1>"), 404)

        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        values = {
            "assemblies": (fields.get("assemblies", [""])[0]).strip(),
            "dxf_dir": (fields.get("dxf_dir", [""])[0]).strip(),
            "material_regex": (fields.get("material_regex", ["^SHEET AL"])[0]).strip(),
            "dxf_recursive": bool(fields.get("dxf_recursive")),
        }
        names = [n for n in re.split(r"[\s,]+", values["assemblies"]) if n]
        if not names:
            return self._send(form_page(values, "Enter at least one assembly number.",
                                       session.get("reports", [])))

        try:
            client = client_for(session)
            company_id = session.get("company_id")
            if not company_id:
                companies = client.companies()
                if not companies:
                    raise OnshapeError(
                        "No Onshape company is associated with this account.")
                company_id = session["company_id"] = companies[0]["id"]

            dxf_index = None
            if values["dxf_dir"]:
                dxf_index = get_dxf_index(values["dxf_dir"], values["dxf_recursive"])
            material_re = re.compile(values["material_regex"] or "^SHEET AL", re.I)

            results = []
            for name in names:
                print(f"  [{session.get('name')}] auditing {name} ...", file=sys.stderr)
                try:
                    results.append(audit_assembly(
                        client, company_id, name, dxf_index, material_re,
                        workers=6, deep=True,
                    ))
                except OnshapeError as exc:
                    results.append(
                        {"partNumber": name, "errors": [str(exc)], "findings": {}})
        except OnshapeError as exc:
            return self._send(form_page(values, str(exc)))
        except Exception as exc:                                  # noqa: BLE001
            traceback.print_exc()
            return self._send(form_page(values, f"{type(exc).__name__}: {exc}"))

        report = render_report(results, CONFIG["base_url"], values["dxf_dir"] or None)
        name = "revaudit-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".html"
        saved = write_report(name, report)
        session.setdefault("reports", []).append(name)
        body = with_back_link(report, str(saved)).replace(
            "<div class='wrap'>", "<div class='wrap'>" + header_for(session), 1)
        self._send(body)


# --------------------------------------------------------------------------

def main(argv=None):
    import config
    parser = argparse.ArgumentParser(description="RevAudit with Onshape OAuth")
    parser.add_argument("--port", type=int, default=None, help="overrides revaudit.conf")
    parser.add_argument("--host", default=None, help="overrides revaudit.conf")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)

    host = args.host or config.bind_host()
    port = args.port or config.port()

    load_dotenv(HERE / ".env")
    CONFIG["client_id"] = os.environ.get("ONSHAPE_OAUTH_CLIENT_ID")
    CONFIG["client_secret"] = os.environ.get("ONSHAPE_OAUTH_CLIENT_SECRET")
    CONFIG["scope"] = os.environ.get("ONSHAPE_OAUTH_SCOPE", "OAuth2Read OAuth2ReadPII")
    CONFIG["base_url"] = os.environ.get("ONSHAPE_BASE_URL") or DEFAULT_BASE_URL
    CONFIG["allowed_company"] = (os.environ.get("REVAUDIT_ALLOWED_COMPANY_ID")
                                 or os.environ.get("ONSHAPE_COMPANY_ID") or "").strip()
    CONFIG["trust_proxy"] = os.environ.get("REVAUDIT_TRUST_PROXY", "").strip() == "1"

    # Every "host:port" this instance will answer OAuth sign-in for. localhost
    # and 127.0.0.1 on --port are always included so local testing keeps
    # working regardless of what else is configured. Each entry here also
    # needs its own "http://<host>/oauth/callback" registered on the Onshape
    # app, or Onshape will reject the sign-in with a redirect_uri mismatch.
    configured = os.environ.get("ONSHAPE_OAUTH_ALLOWED_HOSTS", "")
    allowed_hosts = {h.strip() for h in configured.split(",") if h.strip()}
    legacy = os.environ.get("ONSHAPE_OAUTH_REDIRECT_URI", "")
    if legacy:
        allowed_hosts.add(urlparse(legacy).netloc)
    allowed_hosts.add(f"localhost:{port}")
    allowed_hosts.add(f"127.0.0.1:{port}")
    CONFIG["allowed_hosts"] = allowed_hosts

    if not CONFIG["client_id"] or not CONFIG["client_secret"]:
        print(
            "Missing OAuth credentials.\n\n"
            "Register the app at https://dev-portal.onshape.com (OAuth applications),\n"
            "then add to .env:\n\n"
            "  ONSHAPE_OAUTH_CLIENT_ID=...\n"
            "  ONSHAPE_OAUTH_CLIENT_SECRET=...\n"
            f"  ONSHAPE_OAUTH_REDIRECT_URI=http://localhost:{port}/oauth/callback\n"
            "  ONSHAPE_OAUTH_SCOPE=OAuth2Read OAuth2ReadPII\n\n"
            "The redirect URI must match a registered one exactly.",
            file=sys.stderr,
        )
        return 2

    httpd = ThreadingHTTPServer((host, port), Handler)
    _adv = config.advertised_address()
    _shown = _adv or ("localhost" if host in ("127.0.0.1", "localhost") else host)
    print(f"\n{APP_NAME} (OAuth) running at http://{_shown}:{port}")
    print("Answers sign-in for: " + ", ".join(sorted(CONFIG["allowed_hosts"])))
    print("(each of these needs its own redirect URL registered on the Onshape app)")
    if CONFIG["allowed_company"]:
        print(f"Restricted to company: {CONFIG['allowed_company']}")
    else:
        print("WARNING: no company restriction - any Onshape account can sign in.")
        print("         Set REVAUDIT_ALLOWED_COMPANY_ID in .env to lock it down.")
    print(f"Scope:        {CONFIG['scope']}")
    print("Press Ctrl+C to stop.\n")
    if args.open_browser:
        webbrowser.open(f"http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
