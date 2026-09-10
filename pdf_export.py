"""Export Onshape drawings to PDF, and package the results.

Uses Onshape's async translation API: POST a translation job for a drawing
at a specific version, poll until it finishes, then download the resulting
file. Verified against a real released drawing before this was built out -
see the flow confirmed in README.md's PDF export section.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import time
import zipfile
from pathlib import Path

from onshape_client import OnshapeError


def safe_filename(text):
    """Strip characters Windows filenames can't hold."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(text)).strip()
    return cleaned or "file"


def translate_element(client, kind, document_id, version_id, element_id, format_name,
                      extra_body=None, poll_seconds=2, timeout=180):
    """Translate one Onshape element, at one immutable version, to another
    format. Returns the downloaded file's raw bytes.

    kind is the element's URL segment: "drawings" for a drawing -> PDF,
    "assemblies" for an assembly -> STEP/Parasolid/etc. storeInDocument is
    always False: nothing is ever written back into Onshape, this only reads.
    Version-scoped exports work because versions are immutable - confirmed
    against the real API rather than assumed from docs alone, for both kinds.
    """
    body = {"formatName": format_name, "storeInDocument": False, **(extra_body or {})}
    job = client.post(
        f"/api/v6/{kind}/d/{document_id}/v/{version_id}/e/{element_id}/translations", body)
    job_id = job.get("id")
    if not job_id:
        raise OnshapeError(f"translation did not return a job id: {job}")

    deadline = time.time() + timeout
    while True:
        status = client.get(f"/api/v6/translations/{job_id}") or {}
        state = status.get("requestState")
        if state == "DONE":
            ids = status.get("resultExternalDataIds") or []
            if not ids:
                raise OnshapeError("translation finished but returned no file")
            return client.get_bytes(f"/api/v6/documents/d/{document_id}/externaldata/{ids[0]}")
        if state == "FAILED":
            raise OnshapeError(status.get("failureReason") or "translation failed")
        if time.time() > deadline:
            raise OnshapeError(f"translation timed out after {timeout}s")
        time.sleep(poll_seconds)


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


def export_assembly_drawings(client, result, out_dir, workers=4, on_progress=None):
    """Export PDFs for one audited assembly: its own drawing plus every part
    that has a released one. Returns a list of
    {kind, pn, rev, path, error} - one entry per attempted export, in the
    order jobs were submitted (not completion order)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = _collect_jobs(result)
    if not jobs:
        return []

    def run_one(job):
        ids = job["ids"]
        try:
            data = export_pdf(client, ids["documentId"], ids["versionId"], ids["elementId"])
        except OnshapeError as exc:
            return {**{k: job[k] for k in ("kind", "pn", "rev")}, "path": None,
                    "error": str(exc)}
        suffix = "_ASSEMBLY" if job["kind"] == "assembly" else ""
        fname = f"{safe_filename(job['pn'])}_Rev{safe_filename(job['rev'])}{suffix}.pdf"
        path = out_dir / fname
        path.write_bytes(data)
        return {**{k: job[k] for k in ("kind", "pn", "rev")}, "path": str(path), "error": None}

    entries = [None] * len(jobs)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, job): i for i, job in enumerate(jobs)}
        for fut in cf.as_completed(futures):
            entries[futures[fut]] = fut.result()
            done += 1
            if on_progress:
                on_progress(done, len(jobs))
    return entries


def export_assembly_step_file(client, result, out_dir):
    """Export one assembly's own 3D geometry to a single flat STEP file,
    named and located like the DXF/SAT/PDF folders. Unlike drawing PDFs this
    is one file per assembly (not one per part) - there is only one job to
    run, so no thread pool. Returns {pn, rev, path, error}."""
    asm = result.get("assembly")
    if not asm:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pn, rev = result["partNumber"], asm.get("revision")
    try:
        data = export_step(client, asm["documentId"], asm["versionId"], asm["elementId"])
    except OnshapeError as exc:
        return {"pn": pn, "rev": rev, "path": None, "error": str(exc)}
    path = out_dir / f"{safe_filename(pn)}_Rev{safe_filename(rev)}.step"
    path.write_bytes(data)
    return {"pn": pn, "rev": rev, "path": str(path), "error": None}


def zip_pdfs(entries, zip_path):
    """Bundle every successfully exported PDF into one zip file. Returns the
    path, or None if nothing succeeded."""
    ok = [e for e in entries if e.get("path")]
    if not ok:
        return None
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in ok:
            zf.write(e["path"], arcname=Path(e["path"]).name)
    return zip_path
