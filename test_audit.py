"""Offline tests for the audit logic - runs audit_assembly against a mock client.

    py -3.13 test_audit.py
"""

import json
import re
import sys
import tempfile
from pathlib import Path

from audit_core import audit_assembly
from pdf_export import (_collect_jobs, export_assembly_drawings,
                        export_assembly_step_file, export_counts, safe_filename)
from onshape_client import OnshapeError, normalize_bom, rev_lt
from revaudit import build_report, load_report, render_data, save_report

# --------------------------------------------------------------------------
# a mock Onshape backend shaped exactly like the real payloads
# --------------------------------------------------------------------------

H = {"h1": "ITEM", "h2": "PART NUMBER", "h3": "NAME", "h4": "REV",
     "h5": "STATE", "h6": "QTY", "h7": "MATERIAL"}

SHEET = "SHEET AL 5052 H32 0.188"
WOOD = "WOOD KEBONY S4S 2X4"
TUBE = "TUBE ALUMINUM"


def bom_row(item, pn, name, rev, state, qty, material):
    return {"headerIdToValue": {
        "h1": item, "h2": pn, "h3": name, "h4": rev,
        "h5": state, "h6": qty, "h7": material},
        # what Onshape attaches to every BOM line: where that exact item lives
        "itemSource": {"viewHref": f"https://cad.example/documents/D/v/V-{pn}-{rev}/e/E"
                                   f"?configuration=default"}}


VERSION_BOM = {
    "headers": [{"id": k, "name": v} for k, v in H.items()],
    "rows": [
        # a part number appearing twice at two revisions
        bom_row(1, "PRT-100", "ANGLE", "A", "OBSOLETE", 6, SHEET),
        bom_row(2, "PRT-100", "ANGLE", "B", "RELEASED", 6, SHEET),
        # obsolete with no successor in the BOM
        bom_row(3, "PRT-101", "BRACKET", "A", "OBSOLETE", 4, SHEET),
        # clean released part
        bom_row(4, "PRT-102", "RIB", "B", "RELEASED", 18, SHEET),
        # part whose drawing lags the part revision
        bom_row(5, "PRT-103", "PANEL", "C", "RELEASED", 2, SHEET),
        # wood part with no drawing at all
        bom_row(6, "PRT-104", "BACK WOOD", "A", "RELEASED", 42, WOOD),
        # BOM pins an older rev than the current part
        bom_row(7, "PRT-105", "SPACER", "A", "RELEASED", 3, SHEET),
        # hardware with no revision
        bom_row(8, "SCREW SOC HEX", "SHCS", None, "IN PROGRESS", 76, "Stainless Steel"),
        # tube part - has a SAT file
        bom_row(9, "PRT-106", "RAIL TUBE", "A", "RELEASED", 5, TUBE),
        # tube part - missing its SAT file
        bom_row(10, "PRT-107", "LEG TUBE", "A", "RELEASED", 2, TUBE),
    ],
}

# same content -> no drift by default
WORKSPACE_BOM = {"headers": VERSION_BOM["headers"], "rows": list(VERSION_BOM["rows"])}

PART_REVS = {
    "PRT-100": ["B", "A"],
    "PRT-101": ["A"],
    "PRT-102": ["B", "A"],
    "PRT-103": ["C", "B", "A"],
    "PRT-104": ["A"],
    "PRT-105": ["C", "B", "A"],   # BOM says A -> stale
    "PRT-106": ["A"],
    "PRT-107": ["A"],
}
DRAWING_REVS = {
    "PRT-100": ["B", "A"],
    "PRT-101": ["A"],
    "PRT-102": ["B", "A"],
    "PRT-103": ["B", "A"],        # drawing B behind part C
    "PRT-104": [],                # no drawing at all
    "PRT-105": ["C", "B", "A"],
    "PRT-106": ["A"],
    "PRT-107": ["A"],
}


class MockClient:
    def __init__(self, drift=False):
        self.drift = drift
        self.call_count = 0
        self.bom_configurations = []

    def revisions(self, cid, pn, element_type):
        self.call_count += 1
        if pn == "ASM-TEST" and element_type == 1:
            return [{"documentId": "D", "documentName": "TESTDOC", "elementId": "E",
                     "versionId": "V", "revision": "A", "isObsolete": False,
                     "configuration": "List_x=STEEL",
                     "viewRef": "https://cad.example/documents/D/v/V/e/E"
                                "?configuration=List_x%3DSTEEL",
                     "releaseCreatedDate": "2026-08-25T00:00:00.000+00:00"}]
        if pn == "ASM-TEST" and element_type == 2:
            return [{"revision": "A", "isObsolete": False, "documentId": "D",
                     "versionId": "V-asm-dwg", "elementId": "E-asm-dwg",
                     "viewRef": "https://cad.example/documents/D/v/V-asm-dwg/e/E-asm-dwg"}]
        table = PART_REVS if element_type == 0 else DRAWING_REVS if element_type == 2 else {}
        revs = table.get(pn, [])
        # drawing revisions (element_type 2) carry ids so PDF export has
        # something real to find; part revisions (element_type 0) don't need
        # them since only drawings get exported.
        if element_type == 2:
            return [{"revision": r, "isObsolete": i > 0, "documentName": "SRC",
                     "documentId": "D", "versionId": f"V-{pn}-{r}", "elementId": f"E-{pn}",
                     "viewRef": f"https://cad.example/documents/D/v/V-{pn}-{r}/e/E-{pn}"}
                    for i, r in enumerate(revs)]
        return [{"revision": r, "isObsolete": i > 0, "documentName": "SRC",
                 "viewRef": f"https://cad.example/documents/D/v/V-{pn}-{r}/e/P-{pn}"}
                for i, r in enumerate(revs)]

    def document(self, did):
        return {"defaultWorkspace": {"id": "W"}}

    def bom(self, did, wv, wvid, eid, configuration=None):
        # the audit must ask for the released configuration, otherwise parts
        # that configuration suppresses leak in from the default one
        self.bom_configurations.append(configuration)
        if wv == "w" and self.drift:
            rows = list(VERSION_BOM["rows"])[:-1]
            return {"headers": VERSION_BOM["headers"], "rows": rows}
        return VERSION_BOM if wv == "v" else WORKSPACE_BOM


class MockFileIndex:
    """Stands in for a FileIndex - used for both the DXF and SAT checks."""
    recursive = False
    source = "scanned live"
    generated = None

    def __init__(self, have, root="MOCK", ext=".dxf", extra=None):
        self.have = set(have)
        self.root = root
        self.ext = ext
        self.extra = extra or {}          # pn -> list of suffix variants

    def matches(self, pn):
        if pn not in self.have:
            return []
        return [pn + self.ext] + [pn + s + self.ext for s in self.extra.get(pn, [])]

    def __len__(self):
        return 99


# --------------------------------------------------------------------------

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}: {got}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILS.append(label)


def main():
    print("rev_lt")
    check("A < B", rev_lt("A", "B"), True)
    check("B < A", rev_lt("B", "A"), False)
    check("Z < AA", rev_lt("Z", "AA"), True)
    check("A < A", rev_lt("A", "A"), False)
    check("None safe", rev_lt(None, "B"), False)

    print("\nnormalize_bom")
    rows = normalize_bom(VERSION_BOM)
    check("row count", len(rows), 10)
    check("first pn", rows[0]["pn"], "PRT-100")
    check("state upper", rows[7]["state"], "IN PROGRESS")
    check("material", rows[0]["material"], SHEET)

    print("\naudit_assembly")
    client = MockClient()
    dxf = MockFileIndex(["PRT-100", "PRT-102", "PRT-103", "PRT-105"], "DXFDIR", ".dxf",
                        extra={"PRT-102": ["-X4"]})   # PRT-102.dxf + PRT-102-X4.dxf
    sat = MockFileIndex(["PRT-106"], "SATDIR", ".sat")               # PRT-107 missing
    file_checks = [
        {"name": "DXF", "index": dxf, "material_regex": re.compile(r"SHEET", re.I)},
        {"name": "SAT", "index": sat, "material_regex": re.compile(r"TUBE", re.I)},
    ]
    res = audit_assembly(client, "CID", "ASM-TEST", file_checks, workers=4, deep=False)
    f = res["findings"]

    check("assembly rev", res["assembly"]["revision"], "A")
    check("bom lines", len(res["rows"]), 10)
    check("state counts", res["stateCounts"],
          {"OBSOLETE": 2, "RELEASED": 7, "IN PROGRESS": 1})

    check("no drawing", [e["pn"] for e in f["noDrawing"]], ["PRT-104"])
    check("drawing behind part", [e["pn"] for e in f["drawingBehindPart"]], ["PRT-103"])
    check("bom rev stale", [e["pn"] for e in f["bomRevStale"]], ["PRT-105"])

    mixed = [d["pn"] for d in f["duplicatePartNumbers"] if d["mixedRevisions"]]
    check("mixed-rev part numbers", mixed, ["PRT-100"])

    orphans = [o["pn"] for o in f["obsoleteLines"] if not o["supersededInBom"]]
    check("orphan obsolete", orphans, ["PRT-101"])
    check("obsolete total", len(f["obsoleteLines"]), 2)

    check("not revision managed", [u["pn"] for u in f["notRevisionManaged"]],
          ["SCREW SOC HEX"])

    check("dxf missing", [m["pn"] for m in f["files"]["DXF"]["missing"]], ["PRT-101"])
    check("dxf expected count", f["files"]["DXF"]["expected"], 5)
    check("wood not expected to have dxf",
          "PRT-104" in [m["pn"] for m in f["files"]["DXF"]["missing"]], False)
    check("no duplicate dxf alias in the saved data", "dxf" in f, False)

    dxf_multi = f["files"]["DXF"]["multiple"]
    check("dxf 'multiple' flags PRT-102", [m["pn"] for m in dxf_multi], ["PRT-102"])
    check("dxf 'multiple' lists both files",
          sorted(dxf_multi[0]["files"]), ["PRT-102-X4.dxf", "PRT-102.dxf"])
    check("single-file part not in 'multiple'",
          "PRT-100" in [m["pn"] for m in dxf_multi], False)
    check("part with one file still counts as present",
          "PRT-102" in [p["pn"] for p in f["files"]["DXF"]["present"]], True)
    check("sat has no 'multiple'", f["files"]["SAT"]["multiple"], [])

    check("sat missing", [m["pn"] for m in f["files"]["SAT"]["missing"]], ["PRT-107"])
    check("sat expected count", f["files"]["SAT"]["expected"], 2)
    check("sheet not expected to have sat",
          "PRT-100" in [m["pn"] for m in f["files"]["SAT"]["missing"]], False)

    check("workspace drift (same)", f["workspaceDrift"]["drifted"], False)
    check("released configuration carried", res["assembly"]["configuration"], "List_x=STEEL")
    check("BOMs fetched for released configuration", client.bom_configurations,
          ["List_x=STEEL", "List_x=STEEL"])

    print("\nsafe_filename")
    check("strips illegal chars", safe_filename('PRT-1/2:3*4?"5<6>7|8'), "PRT-1_2_3_4_5_6_7_8")
    check("plain part number untouched", safe_filename("PRT-13417"), "PRT-13417")
    check("all-illegal input becomes one underscore", safe_filename("///"), "_")
    check("empty input falls back to 'file'", safe_filename(""), "file")

    print("\ndrawingIds / pdf_export job collection")
    check("assembly has drawingIds",
          res["assembly"]["drawingIds"] == {"documentId": "D", "versionId": "V-asm-dwg",
                                            "elementId": "E-asm-dwg"}, True)
    check("part with a drawing has drawingIds",
          res["partStatuses"]["PRT-100"]["drawingIds"] is not None, True)
    check("part with no drawing has drawingIds=None",
          res["partStatuses"]["PRT-104"]["drawingIds"], None)

    jobs = _collect_jobs(res)
    check("job count = assembly + 7 parts with drawings", len(jobs), 8)
    check("no job for PRT-104 (no drawing)",
          "PRT-104" in [j["pn"] for j in jobs], False)
    check("assembly job present",
          any(j["kind"] == "assembly" and j["pn"] == "ASM-TEST" for j in jobs), True)
    check("part job carries real ids",
          [j for j in jobs if j["pn"] == "PRT-100"][0]["ids"]["elementId"], "E-PRT-100")

    print("\nOnshape links carried through")
    check("assembly url is the released version + configuration",
          res["assembly"]["url"],
          "https://cad.example/documents/D/v/V/e/E?configuration=List_x%3DSTEEL")
    check("assembly drawing url", res["assembly"]["drawingUrl"],
          "https://cad.example/documents/D/v/V-asm-dwg/e/E-asm-dwg")
    check("part status has part + drawing urls at their current revs",
          (res["partStatuses"]["PRT-103"]["partUrl"],
           res["partStatuses"]["PRT-103"]["drawingUrl"]),
          ("https://cad.example/documents/D/v/V-PRT-103-C/e/P-PRT-103",
           "https://cad.example/documents/D/v/V-PRT-103-B/e/E-PRT-103"))
    check("BOM row url comes from itemSource", res["rows"][0]["url"],
          "https://cad.example/documents/D/v/V-PRT-100-A/e/E?configuration=default")
    check("no-drawing entry links the part", f["noDrawing"][0]["partUrl"],
          "https://cad.example/documents/D/v/V-PRT-104-A/e/P-PRT-104")
    check("drawing-behind entry links both",
          (f["drawingBehindPart"][0]["partUrl"], f["drawingBehindPart"][0]["drawingUrl"]),
          ("https://cad.example/documents/D/v/V-PRT-103-C/e/P-PRT-103",
           "https://cad.example/documents/D/v/V-PRT-103-B/e/E-PRT-103"))
    check("mixed-rev lines each link their own BOM line",
          [l["url"] for l in f["duplicatePartNumbers"][0]["lines"]],
          ["https://cad.example/documents/D/v/V-PRT-100-A/e/E?configuration=default",
           "https://cad.example/documents/D/v/V-PRT-100-B/e/E?configuration=default"])
    check("obsolete line links its BOM line", bool(f["obsoleteLines"][0]["url"]), True)
    check("unmanaged line links its BOM line", bool(f["notRevisionManaged"][0]["url"]), True)
    check("missing-DXF record links the part",
          f["files"]["DXF"]["missing"][0]["partUrl"],
          "https://cad.example/documents/D/v/V-PRT-101-A/e/P-PRT-101")

    print("\nreport data: build / save / load / render")
    data = build_report([res], "https://cad.example", ["DXF folder DXFDIR"])
    check("format tag", data["format"], 1)
    check("json-serialisable", isinstance(json.dumps(data), str), True)
    with tempfile.TemporaryDirectory() as tmp:
        path = save_report(Path(tmp) / "r.json", data)
        loaded = load_report(path)
        check("round trip keeps results", loaded["results"][0]["partNumber"], "ASM-TEST")
        check("round trip keeps generated stamp", loaded["generated"], data["generated"])
        html = render_data(loaded)
        check("rendered report names the assembly", '<h2 id="ASM-TEST">' in html, True)
        check("rendered report shows the stored timestamp",
              f"Generated {data['generated']}" in html, True)
        check("rendered report links the assembly in Onshape",
              'href="https://cad.example/documents/D/v/V/e/E?configuration=List_x%3DSTEEL"'
              in html, True)
        check("rendered report links a BOM line",
              'href="https://cad.example/documents/D/v/V-PRT-100-B/e/E?configuration=default"'
              in html, True)
        check("rendered report links the lagging drawing",
              'href="https://cad.example/documents/D/v/V-PRT-103-B/e/E-PRT-103"' in html,
              True)
        try:
            (Path(tmp) / "junk.json").write_text("[1,2]", encoding="utf-8")
            load_report(Path(tmp) / "junk.json")
            check("non-report json rejected", False, True)
        except ValueError:
            check("non-report json rejected", True, True)
    # a report saved before links existed still renders, just without arrows
    bare = json.loads(json.dumps(res))
    bare["assembly"].pop("url")
    bare["assembly"].pop("drawingUrl")
    for r in bare["rows"]:
        r.pop("url")
    for e in bare["findings"]["noDrawing"] + bare["findings"]["drawingBehindPart"]:
        e.pop("partUrl")
        e.pop("drawingUrl")
    html_bare = render_data(build_report([bare], "https://cad.example"))
    check("link-less data renders", '<h2 id="ASM-TEST">' in html_bare, True)
    check("link-less data has no dangling anchors", 'href="None"' in html_bare, False)

    print("\nexports skip files already in the folder")

    class NoApiClient:
        """Fails loudly if any export actually reaches Onshape."""
        call_count = 0

        def post(self, *a, **k):
            raise OnshapeError("API was called")

        def get(self, *a, **k):
            raise OnshapeError("API was called")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        # pre-existing files at the current revisions
        (out / "PRT-100_RevB.pdf").write_bytes(b"%PDF")
        (out / "ASM-TEST_RevA_ASSEMBLY.pdf").write_bytes(b"%PDF")
        (out / "PRT-102_RevB.pdf").write_bytes(b"")           # empty -> not trusted
        entries = export_assembly_drawings(NoApiClient(), res, out, workers=2)
        by_pn = {e["pn"]: e for e in entries}
        check("existing part PDF skipped, no API call",
              (by_pn["PRT-100"]["skipped"], by_pn["PRT-100"]["error"]), (True, None))
        check("existing assembly PDF skipped", by_pn["ASM-TEST"]["skipped"], True)
        check("skipped entry keeps its path", by_pn["PRT-100"]["path"],
              str(out / "PRT-100_RevB.pdf"))
        check("empty file is re-fetched (and here fails)",
              bool(by_pn["PRT-102"]["error"]), True)
        check("other drawings were attempted", bool(by_pn["PRT-103"]["error"]), True)
        check("export_counts", export_counts(entries), (0, 2, 6))
        forced = export_assembly_drawings(NoApiClient(), res, out, workers=2, force=True)
        check("force re-fetches everything", export_counts(forced), (0, 0, 8))

        (out / "ASM-TEST_RevA.step").write_bytes(b"ISO-10303")
        step = export_assembly_step_file(NoApiClient(), res, out)
        check("existing STEP skipped", (step["skipped"], step["error"]), (True, None))
        step_forced = export_assembly_step_file(NoApiClient(), res, out, force=True)
        check("forced STEP hits the API", bool(step_forced["error"]), True)

    print("\naudit_assembly with a drifted workspace")
    res2 = audit_assembly(MockClient(drift=True), "CID", "ASM-TEST", [], workers=4,
                          deep=False)
    check("workspace drift (differs)", res2["findings"]["workspaceDrift"]["drifted"], True)
    check("no file checks -> no files key", "files" not in res2["findings"], True)

    print("\nmissing assembly")
    res3 = audit_assembly(MockClient(), "CID", "ASM-NOPE", [], workers=2, deep=False)
    check("reports not found", bool(res3["errors"]), True)
    check("no crash", res3.get("assembly"), None)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
