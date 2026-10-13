"""Export Onshape drawings to PDF, and package the results.

Uses Onshape's async translation API: POST a translation job for a drawing
at a specific version, poll until it finishes, then download the resulting
file. Verified against a real released drawing before this was built out -
see the flow confirmed in README.md's PDF export section.

API cost per file fetched: the POST, the status polls, the download. The
POST and the download are fixed, so the polls are where calls are saved - see
PollPlan. A file already in the folder at its revision costs nothing.
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import itertools
import re
import threading
import time
import zipfile
from pathlib import Path

from fsutil import write_atomic
from onshape_client import OnshapeError


def safe_filename(text):
    """Strip characters Windows filenames can't hold."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(text)).strip()
    return cleaned or "file"


class PollPlan:
    """When to ask Onshape "is that translation done yet?" for one kind of
    job. Every ask is an API call.

    The first check waits about as long as recent jobs of this kind really
    took (the median of the last few), so most jobs are found finished on
    the first ask; after that the waits back off. Until anything has been
    measured, `first` is the guess. A fixed 2-second poll used to spend ~5
    calls per drawing just asking.
    """

    def __init__(self, first, then, timeout, low, high, keep=25):
        self.first = first
        self.then = tuple(then)
        self.timeout = timeout
        self.low, self.high = low, high
        self._seen = collections.deque(maxlen=keep)
        self._lock = threading.Lock()

    def first_wait(self):
        with self._lock:
            seen = sorted(self._seen)
        if not seen:
            return self.first
        return min(self.high, max(self.low, seen[len(seen) // 2]))

    def waits(self):
        """Seconds to sleep before poll 1, 2, 3, ... - never runs out."""
        yield self.first_wait()
        yield from self.then
        yield from itertools.repeat(self.then[-1])

    def record(self, seconds):
        """How long a finished job took, from its POST to the poll that saw
        it done (an upper bound - which is the safe side to learn from)."""
        with self._lock:
            self._seen.append(seconds)


# A drawing usually takes 5-10s; a STEP file of a big assembly can take
# minutes, so it starts later, backs off further and is allowed longer.
POLL_PLANS = {
    "drawings": PollPlan(first=6, then=(3, 4, 6, 8, 10), timeout=180, low=3, high=30),
    "assemblies": PollPlan(first=15, then=(10, 15, 20, 30), timeout=900, low=5, high=120),
}


def translate_element(client, kind, document_id, version_id, element_id, format_name,
                      extra_body=None, plan=None):
    """Translate one Onshape element, at one immutable version, to another
    format. Returns the downloaded file's raw bytes.

    kind is the element's URL segment: "drawings" for a drawing -> PDF,
    "assemblies" for an assembly -> STEP/Parasolid/etc. storeInDocument is
    always False: nothing is ever written back into Onshape, this only reads.
    Version-scoped exports work because versions are immutable - confirmed
    against the real API rather than assumed from docs alone, for both kinds.
    """
    plan = plan or POLL_PLANS.get(kind) or POLL_PLANS["drawings"]
    body = {"formatName": format_name, "storeInDocument": False, **(extra_body or {})}
    started = time.monotonic()
    status = client.post(
        f"/api/v6/{kind}/d/{document_id}/v/{version_id}/e/{element_id}/translations", body)
    job_id = (status or {}).get("id")
    if not job_id:
        raise OnshapeError(f"translation did not return a job id: {status}")

    deadline = started + plan.timeout
    waits = plan.waits()
    while status.get("requestState") not in ("DONE", "FAILED"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OnshapeError(f"translation timed out after {plan.timeout}s")
        time.sleep(min(next(waits), remaining))
        status = client.get(f"/api/v6/translations/{job_id}")
        if status is None:
            # 404: the job is gone - polling on would only spend calls
            raise OnshapeError("translation job disappeared (404) before it finished")

    if status["requestState"] == "FAILED":
        raise OnshapeError(status.get("failureReason") or "translation failed")
    plan.record(time.monotonic() - started)
    ids = status.get("resultExternalDataIds") or []
    if not ids:
        raise OnshapeError("translation finished but returned no file")
    return client.get_bytes(f"/api/v6/documents/d/{document_id}/externaldata/{ids[0]}")


def export_pdf(client, document_id, version_id, element_id, **kw):
    """Translate one drawing to PDF. See translate_element for details."""
    return translate_element(client, "drawings", document_id, version_id, element_id,
                             "PDF", **kw)


def export_step(client, document_id, version_id, element_id, **kw):
    """Translate one assembly to STEP. See translate_element for details.

    evaluateExportRule tells Onshape to apply whatever Export Rule is
    configured (Company/Enterprise Settings -> Preferences -> Export Rules)
    for how to name components - e.g. Part Number instead of Name. Verified
    empirically that this flag alone does nothing until such a rule exists;
    see README.md's STEP export section.
    """
    return translate_element(client, "assemblies", document_id, version_id, element_id,
                             "STEP", extra_body={"evaluateExportRule": True}, **kw)


def _collect_jobs(result):
    """Every drawing worth exporting from one audit result: the assembly's
    own drawing, plus each unique part's current released drawing. Parts
    with no drawing are skipped - there is nothing to export."""
    jobs = []
    asm = result.get("assembly")
    if asm and asm.get("drawingIds"):
        jobs.append({
            "kind": "assembly", "pn": result["partNumber"],
            "rev": asm.get("drawingRevision"), "ids": asm["drawingIds"],
        })
    for pn, status in sorted((result.get("partStatuses") or {}).items()):
        if status.get("drawingIds"):
            jobs.append({
                "kind": "part", "pn": pn,
                "rev": status.get("drawingRev"), "ids": status["drawingIds"],
            })
    return jobs


def _already_there(path):
    """True when a non-empty file is already at path. A revision is immutable,
    so a file named for it is the same file whenever it was made - fetching
    it again would only spend Onshape API calls. Files are written
    atomically, so a non-empty one is a complete one."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _fetch_to(path, fetch, base, force):
    """Shared by both exports: skip a file already there, otherwise fetch it
    and save it. Never raises - an error (Onshape or the folder) is recorded
    on the entry, so one bad file doesn't lose the rest of the run."""
    if not force and _already_there(path):
        return {**base, "path": str(path), "error": None, "skipped": True}
    try:
        write_atomic(path, fetch())
    except OnshapeError as exc:
        return {**base, "path": None, "error": str(exc), "skipped": False}
    except OSError as exc:
        return {**base, "path": None, "error": f"could not save {path.name}: {exc}",
                "skipped": False}
    return {**base, "path": str(path), "error": None, "skipped": False}


def export_assembly_drawings(client, result, out_dir, workers=4, on_progress=None,
                             force=False):
    """Export PDFs for one audited assembly: its own drawing plus every part
    that has a released one. Returns a list of
    {kind, pn, rev, path, error, skipped} - one entry per drawing, in the
    order jobs were submitted (not completion order).

    A drawing whose <pn>_Rev<rev>.pdf is already in out_dir is skipped
    (skipped=True, no API call) unless force is set, so re-auditing an
    assembly only fetches the drawings that changed since last time.

    Raises OSError only when out_dir itself can't be created."""
    jobs = _collect_jobs(result)
    if not jobs:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def target_for(job):
        suffix = "_ASSEMBLY" if job["kind"] == "assembly" else ""
        return out_dir / (f"{safe_filename(job['pn'])}_Rev{safe_filename(job['rev'])}"
                          f"{suffix}.pdf")

    def run_one(job):
        ids = job["ids"]
        return _fetch_to(
            target_for(job),
            lambda: export_pdf(client, ids["documentId"], ids["versionId"], ids["elementId"]),
            {k: job[k] for k in ("kind", "pn", "rev")}, force)

    entries = [None] * len(jobs)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_one, job): i for i, job in enumerate(jobs)}
        for fut in cf.as_completed(futures):
            entries[futures[fut]] = fut.result()
            done += 1
            if on_progress:
                on_progress(done, len(jobs))
    return entries


def export_assembly_step_file(client, result, out_dir, force=False):
    """Export one assembly's own 3D geometry to a single flat STEP file,
    named and located like the DXF/SAT/PDF folders. Unlike drawing PDFs this
    is one file per assembly (not one per part) - there is only one job to
    run, so no thread pool. Returns {pn, rev, path, error, skipped}.

    Like the drawings, an existing <asm>_Rev<rev>.step is reused rather than
    fetched again unless force is set.

    Raises OSError only when out_dir itself can't be created."""
    asm = result.get("assembly")
    if not asm:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pn, rev = result["partNumber"], asm.get("revision")
    return _fetch_to(
        out_dir / f"{safe_filename(pn)}_Rev{safe_filename(rev)}.step",
        lambda: export_step(client, asm["documentId"], asm["versionId"], asm["elementId"]),
        {"pn": pn, "rev": rev}, force)


def export_counts(entries):
    """(exported, skipped, failed) totals for a list of export entries."""
    failed = sum(1 for e in entries if e.get("error"))
    skipped = sum(1 for e in entries if e.get("skipped"))
    return len(entries) - failed - skipped, skipped, failed


def zip_pdfs(entries, zip_path):
    """Bundle every successfully exported PDF into one zip file. Returns the
    path, or None if nothing succeeded. A file that vanished from the folder
    since it was exported is left out rather than failing the zip."""
    ok = [e for e in entries if e.get("path")]
    if not ok:
        return None
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in ok:
            try:
                zf.write(e["path"], arcname=Path(e["path"]).name)
                written += 1
            except OSError:
                continue
    if not written:
        zip_path.unlink(missing_ok=True)
        return None
    return zip_path
