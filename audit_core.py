"""Production-file index and the per-assembly audit routine."""

from __future__ import annotations

import concurrent.futures as cf
import re
import sys
from pathlib import Path

from onshape_client import (
    ET_ASSEMBLY,
    ET_BLOB,
    ET_APPLICATION,
    ET_DRAWING,
    ET_NAMES,
    ET_PARTSTUDIO,
    OnshapeError,
    current_revision,
    drawing_ids,
    normalize_bom,
    rev_lt,
)


# --------------------------------------------------------------------------
# production file index (DXF, SAT, or any other extension)
# --------------------------------------------------------------------------

class FileIndex:
    """Filename index over a production file folder - DXF, SAT, or anything
    else identified by extension.

    Only file *names* are ever used - the matching is pure string work against
    the part number. That is what lets a remote host audit coverage from an
    uploaded name list without any access to the files themselves.
    """

    def __init__(self, root, recursive=False, extensions=(".dxf",)):
        self.root = Path(root)
        if not self.root.is_dir():
            raise OnshapeError(f"Folder not found or not a directory: {self.root}")
        self.recursive = recursive
        self.extensions = tuple(e.lower() for e in extensions)
        self.files = []
        walker = self.root.rglob("*") if recursive else self.root.glob("*")
        for path in walker:
            try:
                if path.is_file() and path.suffix.lower() in self.extensions:
                    self.files.append(path)
            except OSError:
                continue
        self.names = [p.name for p in self.files]
        self.source = "scanned live"
        self.generated = None
        self._cache = {}

    @classmethod
    def from_names(cls, names, folder="(uploaded index)", recursive=False,
                   generated=None, extensions=(".dxf",)):
        """Build an index from a filename list produced by file_indexer.py."""
        self = cls.__new__(cls)
        self.root = Path(folder)
        self.recursive = recursive
        self.extensions = tuple(e.lower() for e in extensions)
        self.files = []
        self.names = list(names)
        self.source = "uploaded index"
        self.generated = generated
        self._cache = {}
        return self

    def matches(self, part_number):
        """Filenames containing the part number, bounded so PRT-12 != PRT-123."""
        if part_number in self._cache:
            return self._cache[part_number]
        pattern = re.compile(
            r"(?:^|[^0-9A-Za-z])" + re.escape(part_number) + r"(?:[^0-9]|$)",
            re.IGNORECASE,
        )
        hits = [n for n in self.names if pattern.search(n)]
        self._cache[part_number] = hits
        return hits

    def __len__(self):
        return len(self.names)


# kept for anything still importing the old name
DxfIndex = FileIndex


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------

def resolve_assembly(client, company_id, part_number):
    """Locate the released assembly revision for a part number."""
    revs = client.revisions(company_id, part_number, ET_ASSEMBLY)
    if not revs:
        return None
    current = current_revision(revs)
    chosen = current or revs[0]
    drawings = client.revisions(company_id, part_number, ET_DRAWING)
    drawing_current = current_revision(drawings)
    return {
        "partNumber": part_number,
        "documentId": chosen.get("documentId"),
        "documentName": chosen.get("documentName"),
        "elementId": chosen.get("elementId"),
        "versionId": chosen.get("versionId"),
        "revision": chosen.get("revision"),
        "obsolete": bool(chosen.get("isObsolete")),
        "releaseDate": (chosen.get("releaseCreatedDate") or "")[:10],
        "revisionCount": len(revs),
        "drawingRevision": drawing_current.get("revision") if drawing_current else None,
        "drawingCount": len(drawings),
        "hasCurrentDrawing": drawing_current is not None,
        # kept so a drawing can be PDF-exported later without re-querying
        "drawingIds": drawing_ids(drawing_current),
    }


def part_status(client, company_id, part_number):
    """Current part revision and current drawing revision for one part number."""
    part_revs = client.revisions(company_id, part_number, ET_PARTSTUDIO)
    drawing_revs = client.revisions(company_id, part_number, ET_DRAWING)
    part_cur = current_revision(part_revs)
    drawing_cur = current_revision(drawing_revs)
    return {
        "pn": part_number,
        "partRev": part_cur.get("revision") if part_cur else None,
        "partRevCount": len(part_revs),
        "drawingRev": drawing_cur.get("revision") if drawing_cur else None,
        "drawingRevCount": len(drawing_revs),
        "drawingDoc": drawing_cur.get("documentName") if drawing_cur else None,
        "sourceDoc": part_cur.get("documentName") if part_cur else None,
        # kept so a drawing can be PDF-exported later without re-querying
        "drawingIds": drawing_ids(drawing_cur),
    }


def deep_check_no_drawing(client, company_id, part_number):
    """Confirm a part truly has no drawing by sweeping the other element types."""
    found = {}
    for et in (ET_ASSEMBLY, ET_BLOB, ET_APPLICATION):
        items = client.revisions(company_id, part_number, et)
        if items:
            found[ET_NAMES[et]] = len(items)
    return found


def bom_signature(rows):
    return sorted((r["pn"] or "", str(r["rev"] or ""), r["state"] or "") for r in rows)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def audit_assembly(client, company_id, asm_pn, file_checks=None,
                   workers=6, deep=True, verbose=False):
    """Run every check for one assembly part number.

    file_checks is a list of {"name", "material_regex", "index"} dicts, one
    per production-file type to audit (DXF, SAT, ...). "index" is a FileIndex
    or None to skip that check. Order is preserved into the report.
    """
    file_checks = file_checks or []
    result = {"partNumber": asm_pn, "errors": [], "findings": {}}

    asm = resolve_assembly(client, company_id, asm_pn)
    if not asm:
        result["errors"].append(
            f"No assembly revision found for {asm_pn} in the company revision registry. "
            "It may never have been released, or the part number may be wrong."
        )
        return result
    result["assembly"] = asm

    payload = client.bom(asm["documentId"], "v", asm["versionId"], asm["elementId"])
    rows = normalize_bom(payload)
    if not rows:
        result["errors"].append("The released version returned an empty BOM.")
        return result
    result["rows"] = rows
    result["bomSource"] = f"released Rev {asm['revision']}"

    counts = {}
    for row in rows:
        key = row["state"] or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    result["stateCounts"] = counts

    # -- workspace drift -------------------------------------------------
    try:
        doc = client.document(asm["documentId"]) or {}
        wsid = (doc.get("defaultWorkspace") or {}).get("id")
        if wsid:
            ws_rows = normalize_bom(
                client.bom(asm["documentId"], "w", wsid, asm["elementId"])
            )
            if ws_rows:
                ws_counts = {}
                for row in ws_rows:
                    key = row["state"] or "UNKNOWN"
                    ws_counts[key] = ws_counts.get(key, 0) + 1
                result["findings"]["workspaceDrift"] = {
                    "drifted": bom_signature(rows) != bom_signature(ws_rows),
                    "versionCounts": counts,
                    "workspaceCounts": ws_counts,
                    "versionLines": len(rows),
                    "workspaceLines": len(ws_rows),
                }
    except OnshapeError as exc:
        result["errors"].append(f"Workspace comparison skipped: {exc}")

    # -- duplicate part numbers ------------------------------------------
    grouped = {}
    for row in rows:
        grouped.setdefault(row["pn"], []).append(row)
    duplicates = []
    for pn, lines in grouped.items():
        if len(lines) < 2:
            continue
        revs = {str(line["rev"]) for line in lines}
        duplicates.append({
            "pn": pn,
            "mixedRevisions": len(revs) > 1,
            "lines": [
                {"item": l["item"], "rev": l["rev"], "state": l["state"], "qty": l["qty"]}
                for l in lines
            ],
        })
    duplicates.sort(key=lambda d: (not d["mixedRevisions"], str(d["pn"])))
    result["findings"]["duplicatePartNumbers"] = duplicates

    # -- obsolete / unmanaged lines ---------------------------------------
    superseded = {
        d["pn"] for d in duplicates
        if d["mixedRevisions"] and any(l["state"] == "RELEASED" for l in d["lines"])
    }
    result["findings"]["obsoleteLines"] = [
        {
            "item": r["item"], "pn": r["pn"], "rev": r["rev"], "qty": r["qty"],
            "name": r["name"], "supersededInBom": r["pn"] in superseded,
        }
        for r in rows if r["state"] == "OBSOLETE"
    ]
    result["findings"]["notRevisionManaged"] = [
        {"item": r["item"], "pn": r["pn"], "qty": r["qty"], "state": r["state"],
         "name": r["name"], "material": r["material"]}
        for r in rows
        if r["state"] and r["state"] not in ("RELEASED", "OBSOLETE")
    ]

    # -- per-part drawing status -------------------------------------------
    part_rows = [r for r in rows if r["pn"] and re.match(r"^PRT-", str(r["pn"]), re.I)]
    unique_pns = sorted({r["pn"] for r in part_rows})

    bom_rev_by_pn = {}
    material_by_pn = {}
    name_by_pn = {}
    qty_by_pn = {}
    for r in part_rows:
        bom_rev_by_pn.setdefault(r["pn"], set()).add(str(r["rev"]) if r["rev"] else None)
        material_by_pn.setdefault(r["pn"], r["material"])
        name_by_pn.setdefault(r["pn"], r["name"])
        qty_by_pn[r["pn"]] = qty_by_pn.get(r["pn"], 0) + (r["qty"] or 0)

    statuses = {}
    if unique_pns:
        if verbose:
            print(f"    {len(unique_pns)} unique parts to check", file=sys.stderr)
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(part_status, client, company_id, pn): pn
                for pn in unique_pns
            }
            for fut in cf.as_completed(futures):
                pn = futures[fut]
                try:
                    statuses[pn] = fut.result()
                except OnshapeError as exc:
                    result["errors"].append(f"{pn}: {exc}")
    result["partStatuses"] = statuses

    no_drawing, behind, stale = [], [], []
    for pn in unique_pns:
        st = statuses.get(pn)
        if not st:
            continue
        entry = {
            "pn": pn,
            "name": name_by_pn.get(pn),
            "material": material_by_pn.get(pn),
            "qty": qty_by_pn.get(pn),
            "partRev": st["partRev"],
            "drawingRev": st["drawingRev"],
            "sourceDoc": st["sourceDoc"],
        }
        if not st["drawingRev"]:
            if deep:
                entry["otherElements"] = deep_check_no_drawing(client, company_id, pn)
            no_drawing.append(entry)
        elif st["partRev"] and rev_lt(st["drawingRev"], st["partRev"]):
            behind.append(entry)

        bom_revs = bom_rev_by_pn.get(pn, set())
        if st["partRev"] and bom_revs and st["partRev"] not in bom_revs:
            stale.append({**entry, "bomRevs": sorted(x for x in bom_revs if x)})

    result["findings"]["noDrawing"] = sorted(no_drawing, key=lambda e: -(e["qty"] or 0))
    result["findings"]["drawingBehindPart"] = behind
    result["findings"]["bomRevStale"] = stale

    # -- production-file coverage: DXF, SAT, or whatever else was configured -
    files_result = {}
    for check in file_checks:
        index = check.get("index")
        if index is None:
            continue
        material_re = check["material_regex"]
        missing, present, unexpected = [], [], []
        for pn in unique_pns:
            material = material_by_pn.get(pn) or ""
            expected = bool(material_re.search(material))
            hits = index.matches(pn)
            record = {
                "pn": pn,
                "name": name_by_pn.get(pn),
                "material": material,
                "qty": qty_by_pn.get(pn),
                "files": hits,
            }
            if expected and not hits:
                missing.append(record)
            elif expected:
                present.append(record)
            elif hits:
                unexpected.append(record)
        # a part that matched more than one file - e.g. PRT-1234.dxf plus a
        # PRT-1234-X4.dxf nest variant. Not a gap; worth a look, since the
        # extra could be a legit variant or a stale duplicate.
        multiple = sorted(
            (r for r in present + unexpected if len(r["files"]) > 1),
            key=lambda r: -len(r["files"]),
        )
        files_result[check["name"]] = {
            "materialPattern": material_re.pattern,
            "folder": str(index.root),
            "recursive": index.recursive,
            "source": getattr(index, "source", "scanned live"),
            "generated": getattr(index, "generated", None),
            "filesIndexed": len(index),
            "expected": len(missing) + len(present),
            "missing": missing,
            "present": present,
            "unexpected": unexpected,
            "multiple": multiple,
        }
    if files_result:
        result["findings"]["files"] = files_result
        if "DXF" in files_result:                          # back-compat
            result["findings"]["dxf"] = files_result["DXF"]

    return result
