"""Minimal Onshape REST client plus BOM/revision helpers. Standard library only."""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Onshape element type enum
ET_PARTSTUDIO = 0
ET_ASSEMBLY = 1
ET_DRAWING = 2
ET_BLOB = 4
ET_APPLICATION = 5

ET_NAMES = {0: "part studio", 1: "assembly", 2: "drawing", 4: "blob", 5: "application"}

DEFAULT_BASE_URL = "https://cad.onshape.com"


class OnshapeError(RuntimeError):
    pass


class OnshapeClient:
    """Onshape REST client using API-key basic auth."""

    def __init__(self, base_url, access_key=None, secret_key=None, timeout=60,
                 verbose=False, bearer_token=None):
        """Authenticate with an API key (access_key + secret_key) or, for the
        OAuth flow, with a per-user bearer token."""
        self.base = base_url.rstrip("/")
        if bearer_token:
            self.auth = "Bearer " + bearer_token
        elif access_key and secret_key:
            token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
            self.auth = "Basic " + token
        else:
            raise OnshapeError("No credentials: pass an API key or a bearer token.")
        self.timeout = timeout
        self.verbose = verbose
        self.call_count = 0

    def set_bearer(self, bearer_token):
        """Swap in a freshly refreshed OAuth token."""
        self.auth = "Bearer " + bearer_token

    def get(self, path, params=None, retries=4):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": self.auth,
            },
        )
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                self.call_count += 1
                if self.verbose:
                    print(f"      GET {path}", file=sys.stderr)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:400]
                if exc.code == 401:
                    raise OnshapeError(
                        "401 Unauthorized. Check ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY, "
                        "and that the keys belong to this Onshape account. An enterprise "
                        "account needs ONSHAPE_BASE_URL set to the enterprise URL."
                    )
                if exc.code == 404:
                    return None
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"HTTP {exc.code} on {path}: {body}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # A timeout while reading the response body arrives as a bare
                # TimeoutError, not wrapped in URLError, so it has to be caught
                # explicitly or it escapes the retry loop and kills the run.
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"Network error on {path}: {type(exc).__name__}: {exc}")
        raise OnshapeError(f"Giving up on {path}")

    def post(self, path, body, retries=4):
        """POST a JSON body, return the parsed JSON response."""
        data = json.dumps(body).encode("utf-8")
        delay = 1.0
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                self.base + path, data=data, method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": self.auth,
                },
            )
            try:
                self.call_count += 1
                if self.verbose:
                    print(f"      POST {path}", file=sys.stderr)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", "replace")[:400]
                if exc.code == 401:
                    raise OnshapeError("401 Unauthorized on POST " + path)
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"HTTP {exc.code} on POST {path}: {body_text}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"Network error on POST {path}: {exc}")
        raise OnshapeError(f"Giving up on POST {path}")

    def get_bytes(self, path, retries=4):
        """GET a binary response (a downloaded file), return raw bytes."""
        delay = 1.0
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                self.base + path, headers={"Authorization": self.auth}
            )
            try:
                self.call_count += 1
                if self.verbose:
                    print(f"      GET(bytes) {path}", file=sys.stderr)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"HTTP {exc.code} downloading {path}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OnshapeError(f"Network error downloading {path}: {exc}")
        raise OnshapeError(f"Giving up on GET(bytes) {path}")

    # -- endpoints -------------------------------------------------------

    def companies(self):
        data = self.get("/api/v6/companies") or {}
        return data.get("items", []) or []

    def revisions(self, company_id, part_number, element_type):
        pn = urllib.parse.quote(str(part_number), safe="")
        data = self.get(
            f"/api/v6/revisions/companies/{company_id}/partnumber/{pn}",
            {"elementType": element_type},
        )
        return (data or {}).get("items", []) or []

    def document(self, did):
        return self.get(f"/api/v6/documents/{did}")

    def bom(self, did, wv, wvid, eid):
        return self.get(
            f"/api/v6/assemblies/d/{did}/{wv}/{wvid}/e/{eid}/bom",
            {"indented": "false", "multiLevel": "true", "generateIfAbsent": "true"},
        )


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
        rows.append({
            "item": flat.get("ITEM"),
            "pn": flat.get("PART NUMBER"),
            "name": flat.get("NAME"),
            "rev": flat.get("REV"),
            "state": (flat.get("STATE") or "").strip().upper() or None,
            "qty": flat.get("QTY") or flat.get("QUANTITY"),
            "material": flat.get("MATERIAL"),
        })

    def order(r):
        try:
            return (0, int(str(r["item"])))
        except (TypeError, ValueError):
            return (1, 0)

    rows.sort(key=order)
    return rows
