"""Minimal Onshape REST client plus BOM/revision helpers. Standard library only."""

from __future__ import annotations

import base64
import copy
import email.utils
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Onshape element type enum
ET_ANY = -1                 # "every type" - only valid as a query parameter
ET_PARTSTUDIO = 0
ET_ASSEMBLY = 1
ET_DRAWING = 2
ET_BLOB = 4
ET_APPLICATION = 5
ET_VARIABLE_STUDIO = 8

ET_NAMES = {0: "part studio", 1: "assembly", 2: "drawing", 4: "blob", 5: "application",
            8: "variable studio"}

DEFAULT_BASE_URL = "https://cad.onshape.com"

# Statuses worth retrying: rate limited, or a server-side hiccup.
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRY_AFTER = 120        # seconds; never sleep longer than this on a Retry-After
MAX_PAGES = 25               # safety stop when following "next" links


class OnshapeError(RuntimeError):
    pass


def retry_after_seconds(headers, now=None):
    """Seconds a 429/503 asked us to wait (Retry-After as seconds or as an HTTP
    date), clamped to MAX_RETRY_AFTER, or None when the header is absent or
    unreadable."""
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return min(MAX_RETRY_AFTER, max(0.0, seconds))


class OnshapeClient:
    """Onshape REST client using API-key basic auth or an OAuth bearer token.

    Thread-safe: the audit runs part lookups on a small worker pool and the
    web server shares one client across requests.
    """

    def __init__(self, base_url, access_key=None, secret_key=None, timeout=60,
                 verbose=False, bearer_token=None, revision_cache_seconds=600):
        """Authenticate with an API key (access_key + secret_key) or, for the
        OAuth flow, with a per-user bearer token.

        revision_cache_seconds is how long a revision lookup is reused - see
        revision_sets(). 0 turns the reuse off.
        """
        self.base = base_url.rstrip("/")
        self._origin = urllib.parse.urlsplit(self.base)[:2]
        if bearer_token:
            self.auth = "Bearer " + bearer_token
        elif access_key and secret_key:
            token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
            self.auth = "Basic " + token
        else:
            raise OnshapeError("No credentials: pass an API key or a bearer token.")
        self.timeout = timeout
        self.verbose = verbose
        self.revision_cache_seconds = revision_cache_seconds
        self.call_count = 0                       # requests actually sent
        self.cached_count = 0                     # lookups answered from memory
        self._count_lock = threading.Lock()
        self._memo_lock = threading.Lock()
        self._rev_cache = {}                      # (company, part number) -> (time, sets)
        self._workspaces = {}                     # document id -> default workspace id
        # When Onshape says "slow down", every worker waits, not just the one
        # that was told - otherwise the other five keep spending calls on 429s.
        self._pause_lock = threading.Lock()
        self._pause_until = 0.0

    def set_bearer(self, bearer_token):
        """Swap in a freshly refreshed OAuth token."""
        self.auth = "Bearer " + bearer_token

    # -- transport -------------------------------------------------------

    def _counted(self, cached=False):
        with self._count_lock:
            if cached:
                self.cached_count += 1
            else:
                self.call_count += 1

    def usage(self):
        """(calls sent, lookups answered from memory) so far."""
        with self._count_lock:
            return self.call_count, self.cached_count

    def _url(self, path, params=None):
        """Absolute URL for an API path - or for a full URL Onshape handed back
        (a "next" page link), which must stay on the same host: the request
        carries our credentials."""
        if path.startswith(("http://", "https://")):
            if urllib.parse.urlsplit(path)[:2] != self._origin:
                raise OnshapeError(f"Refusing to follow a link off {self.base}: {path}")
            url = path
        else:
            url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def _wait_if_paused(self):
        with self._pause_lock:
            wait = self._pause_until - time.time()
        if wait > 0:
            time.sleep(wait)

    def _pause(self, seconds):
        with self._pause_lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)

    def _unauthorized(self, method, path):
        if self.auth.startswith("Bearer "):
            return OnshapeError(f"401 Unauthorized on {method} {path}. The Onshape sign-in "
                                "has expired or was revoked - sign in again.")
        return OnshapeError(
            "401 Unauthorized. Check ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY, and that "
            "the keys belong to this Onshape account. An enterprise account needs "
            "ONSHAPE_BASE_URL set to the enterprise URL.")

    def _request(self, method, path, params=None, body=None, accept="application/json",
                 retries=4, missing_ok=False):
        """Send one request, retrying rate limits, 5xx and network errors with
        backoff. Returns the raw response body, or None for a 404 when
        missing_ok. Every attempt counts as a call - Onshape counts it too.

        A POST is retried as well. For a translation that can, rarely, start a
        second identical job if the first reply was lost in transit; that only
        costs Onshape some work, never correctness.
        """
        url = self._url(path, params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        label = path if len(path) < 160 else path[:160] + "..."
        delay = 1.0
        for attempt in range(retries + 1):
            headers = {"Accept": accept, "Authorization": self.auth}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            self._wait_if_paused()
            self._counted()
            if self.verbose:
                print(f"      {method} {label}", file=sys.stderr)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                try:
                    text = exc.read().decode("utf-8", "replace")[:400]
                except OSError:
                    text = ""
                if exc.code == 401:
                    raise self._unauthorized(method, label) from None
                if exc.code == 404 and missing_ok:
                    return None
                if exc.code in RETRY_STATUSES and attempt < retries:
                    asked = retry_after_seconds(exc.headers)
                    wait = asked if asked is not None else delay * random.uniform(0.75, 1.25)
                    if exc.code == 429:
                        self._pause(wait)
                    time.sleep(wait)
                    delay *= 2
                    continue
                raise OnshapeError(f"HTTP {exc.code} on {method} {label}: {text}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # A timeout while reading the response body arrives as a bare
                # TimeoutError, not wrapped in URLError, so it has to be caught
                # explicitly or it escapes the retry loop and kills the run.
                if attempt < retries:
                    time.sleep(delay * random.uniform(0.75, 1.25))
                    delay *= 2
                    continue
                raise OnshapeError(f"Network error on {method} {label}: "
                                   f"{type(exc).__name__}: {exc}") from None
        raise OnshapeError(f"Giving up on {method} {label}")     # pragma: no cover

    @staticmethod
    def _json(raw):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise OnshapeError(f"Onshape returned something that is not JSON: {exc}") from None

    def get(self, path, params=None, retries=4):
        """GET JSON. None when Onshape says 404 - "no such thing" is an answer."""
        raw = self._request("GET", path, params, retries=retries, missing_ok=True)
        return None if raw is None else self._json(raw)

    def post(self, path, body, retries=4):
        """POST a JSON body, return the parsed JSON response."""
        return self._json(self._request("POST", path, body=body, retries=retries))

    def get_bytes(self, path, retries=4):
        """GET a binary response (a downloaded file), return raw bytes."""
        return self._request("GET", path, accept="*/*", retries=retries)

    # -- endpoints -------------------------------------------------------

    def companies(self):
        data = self.get("/api/v6/companies") or {}
        return data.get("items", []) or []

    def revision_sets(self, company_id, part_number):
        """Every revision of a part number, grouped by element type:
        {ET_PARTSTUDIO: [...], ET_DRAWING: [...], ...}, each list newest first.

        One request covers every element type (elementType=-1), so a part's
        own revisions, its drawing's, and anything else released under the
        same number all arrive together - checked against the live API to
        return exactly what the per-type requests return, same order.

        Memoised for revision_cache_seconds. The same part number comes up
        repeatedly - the "already audited?" probe and then the audit, and every
        assembly that shares the part - while a released revision never
        changes. Only a release made in the last few minutes can be missed,
        and forget_revisions() drops everything when certainty is wanted.
        """
        key = (str(company_id), str(part_number))
        ttl = self.revision_cache_seconds
        if ttl:
            with self._memo_lock:
                hit = self._rev_cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                self._counted(cached=True)
                if self.verbose:
                    print(f"      cached revisions {part_number}", file=sys.stderr)
                # a copy: worker threads share this entry, and a caller that
                # edited one would silently rewrite everyone else's
                return copy.deepcopy(hit[1])
        fetched_at = time.time()
        sets = self._fetch_revision_sets(company_id, part_number)
        if ttl:
            with self._memo_lock:
                self._rev_cache[key] = (fetched_at, copy.deepcopy(sets))
        return sets

    def _fetch_revision_sets(self, company_id, part_number):
        pn = urllib.parse.quote(str(part_number), safe="")
        path = f"/api/v6/revisions/companies/{company_id}/partnumber/{pn}"
        data = self.get(path, {"elementType": ET_ANY}) or {}
        items = list(data.get("items") or [])
        pages = 1
        while data.get("next") and pages < MAX_PAGES:
            data = self.get(data["next"]) or {}
            items += data.get("items") or []
            pages += 1
        if any("elementType" not in item for item in items):
            # A server that ignored elementType=-1 would hand back records we
            # can't sort by type - ask per type instead, which is always right.
            return {et: list((self.get(path, {"elementType": et}) or {}).get("items") or [])
                    for et in (ET_PARTSTUDIO, ET_ASSEMBLY, ET_DRAWING, ET_BLOB)}
        sets = {}
        for item in items:
            try:
                et = int(item["elementType"])
            except (TypeError, ValueError):
                et = item["elementType"]
            sets.setdefault(et, []).append(item)
        return sets

    def revisions(self, company_id, part_number, element_type):
        """Every revision of one element type for a part number, newest first.
        A view over revision_sets(), so asking for several types of the same
        part number costs one request."""
        return self.revision_sets(company_id, part_number).get(element_type, [])

    def forget_revisions(self):
        """Drop every memoised revision lookup, so the next audit re-reads
        them from Onshape. Used when someone explicitly asks for a re-run."""
        with self._memo_lock:
            self._rev_cache.clear()

    def document(self, did):
        return self.get(f"/api/v6/documents/{did}")

    def default_workspace(self, did):
        """The id of a document's main workspace, or None. That id never
        changes for the life of a document, so it is asked for once."""
        with self._memo_lock:
            if did in self._workspaces:
                self._counted(cached=True)
                return self._workspaces[did]
        wsid = ((self.document(did) or {}).get("defaultWorkspace") or {}).get("id")
        if wsid:
            with self._memo_lock:
                self._workspaces[did] = wsid
        return wsid

    def bom(self, did, wv, wvid, eid, configuration=None):
        """configuration is the encoded string a revision record carries
        (e.g. "HEIGHT=1.016+meter;List_x=STEEL"). Without it Onshape
        returns the BOM of the default configuration, which can list parts
        the released configuration suppresses.

        Not memoised, even for a version: the STATE column is the referenced
        revision's state *now*, so a version's BOM changes when a part it
        references is obsoleted."""
        params = {"indented": "false", "multiLevel": "true", "generateIfAbsent": "true"}
        if configuration:
            params["configuration"] = configuration
        return self.get(f"/api/v6/assemblies/d/{did}/{wv}/{wvid}/e/{eid}/bom", params)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def current_revision(items):
    """First non-obsolete revision, or None. A revision only exists once a
    release completes, so 'has a non-obsolete revision' == 'is released'."""
    for item in items or []:
        if not item.get("isObsolete"):
            return item
    return None


def view_url(record):
    """The Onshape URL that opens this revision record or BOM item source at
    its exact version and configuration - Onshape supplies it as "viewRef" on
    a revision and "viewHref" on a BOM line's itemSource, so nothing has to
    be assembled by hand. None when the record carries neither."""
    if not record:
        return None
    return record.get("viewRef") or record.get("viewHref") or None


def drawing_ids(revision):
    """documentId/versionId/elementId for a revision dict, or None if any are
    missing - used to locate a drawing for PDF export without a second API
    call. Defensive by design: a malformed or partial entry (as in a test
    mock) yields None rather than a KeyError."""
    if not revision:
        return None
    ids = {k: revision.get(k) for k in ("documentId", "versionId", "elementId")}
    return ids if all(ids.values()) else None


def rev_key(rev):
    """Sortable key for revision labels: A < B < ... < Z < AA."""
    if not rev:
        return (0, 0, "")
    r = str(rev).strip().upper()
    if r.isdigit():
        return (1, int(r), "")
    return (2, len(r), r)


def rev_lt(a, b):
    """True if revision a is earlier than revision b."""
    if not a or not b:
        return False
    return rev_key(a) < rev_key(b)


def cell(value):
    """BOM cells are sometimes scalars, sometimes objects."""
    if isinstance(value, dict):
        for key in ("displayName", "value", "name", "id"):
            if value.get(key) is not None:
                return value[key]
        return None
    return value


def number(value):
    """A BOM quantity as a number (Onshape sends 2.0; a hand-made BOM may send
    "2"). Anything unreadable counts as 0 rather than breaking a total."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0


def normalize_bom(payload):
    """Flatten an Onshape BOM payload into the columns this tool uses."""
    if not payload:
        return []
    headers = {}
    for head in payload.get("headers", []) or []:
        headers[head.get("id")] = str(head.get("name", "")).strip().upper()

    rows = []
    for row in payload.get("rows", []) or []:
        values = row.get("headerIdToValue", {}) or {}
        flat = {}
        for hid, val in values.items():
            flat[headers.get(hid, hid)] = cell(val)
        state = flat.get("STATE")
        rows.append({
            "item": flat.get("ITEM"),
            "pn": flat.get("PART NUMBER"),
            "name": flat.get("NAME"),
            "rev": flat.get("REV"),
            "state": str(state).strip().upper() or None if state else None,
            "qty": flat.get("QTY") or flat.get("QUANTITY"),
            "material": flat.get("MATERIAL"),
            # opens this exact line - the version and configuration the
            # assembly actually references - in Onshape
            "url": view_url(row.get("itemSource")),
        })

    def order(r):
        try:
            return (0, int(str(r["item"])))
        except (TypeError, ValueError):
            return (1, 0)

    rows.sort(key=order)
    return rows
