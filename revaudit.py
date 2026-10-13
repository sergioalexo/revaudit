#!/usr/bin/env python3
"""
revaudit.py - release / drawing / production-file audit for Onshape assemblies.

For each assembly part number it answers:
  * is every part's drawing released?
  * does any drawing lag its part revision?
  * does the BOM list one part number at two different revisions?
  * which BOM lines are obsolete or not revision-managed?
  * has the live workspace drifted from the released version?
  * does every sheet part have a DXF in the production folder?
  * does every tube part have a SAT file in the production folder?

Usage:
  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" ^
      --sat-dir "<share>\\SAT FILES" --open

Credentials come from the environment or a .env file beside this script:
  ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY, ONSHAPE_BASE_URL

Standard library only - no pip install required.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import config
from audit_core import FileIndex, audit_assembly
from onshape_client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError
from pdf_export import (export_assembly_drawings, export_assembly_step_file,
                        export_counts, safe_filename, zip_pdfs)
import usage_stats
from report import api_usage, build_report, load_report, report_name_part, save_report


# --------------------------------------------------------------------------
# terminal summary
# --------------------------------------------------------------------------

def print_summary(result):
    pn = result["partNumber"]
    if not result.get("assembly"):
        print(f"  {pn}: NOT FOUND")
        for err in result.get("errors", []):
            print(f"    {err}")
        return
    f = result.get("findings", {})
    total = len(result.get("partStatuses", {}))
    nd = len(f.get("noDrawing", []))
    beh = len(f.get("drawingBehindPart", []))
    dup = len([d for d in f.get("duplicatePartNumbers", []) if d["mixedRevisions"]])
    obs = len(f.get("obsoleteLines", []))
    asm = result["assembly"]
    print(f"  {pn}  (doc {asm['documentName']}, Rev {asm['revision']})")
    print(f"    drawings released ... {total - nd}/{total}" + ("  <-- gap" if nd else ""))
    print(f"    drawing behind part . {beh}" + ("  <-- check" if beh else ""))
    print(f"    mixed-rev part nums . {dup}" + ("  <-- check" if dup else ""))
    print(f"    obsolete BOM lines .. {obs}")
    for name, fc in (f.get("files") or {}).items():
        miss = len(fc.get("missing", []))
        label = f"missing {name}".ljust(20, ".")
        print(f"    {label} {miss}" + ("  <-- gap" if miss else ""))
        multi = len(fc.get("multiple", []))
        if multi:
            print(f"    {(name + ' extras').ljust(20, '.')} {multi}  <-- review")
    export = result.get("drawingExport")
    if export:
        ok, skipped, failed = export_counts(export.get("entries", []))
        print(f"    drawing PDFs ........ {ok} exported"
             + (f", {skipped} already there" if skipped else "")
             + (f", {failed} failed  <-- check" if failed else ""))
    step = result.get("stepExport")
    if step:
        print("    STEP export ......... "
             + ("failed  <-- check: " + step["error"] if step.get("error")
                else "already there" if step.get("skipped") else "ok"))
    for err in result.get("errors", []):
        print(f"    ! {err}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    here = Path(__file__).resolve().parent
    config.load_dotenv(here / ".env")   # before the parser: export defaults read env vars

    parser = argparse.ArgumentParser(
        prog="revaudit",
        description="Release / drawing / production-file audit for Onshape assemblies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" --open\n'
            '  py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\\DXF FILES" '
            '--sat-dir "<share>\\SAT FILES" --open\n'
            "  py -3.13 revaudit.py ASM-1 ASM-2 ASM-3 -o audit.html\n"
            "  py -3.13 revaudit.py --render reports/revaudit-20260921-1405_ASM-1.html --open\n"
        ),
    )
    parser.add_argument("assemblies", nargs="*", metavar="ASM",
                        help="assembly part numbers, e.g. ASM-12345")
    parser.add_argument("--render", metavar="FILE",
                        help="re-render a saved report (its embedded data, or a bare "
                             ".json) with the current layout instead of running an "
                             "audit - no Onshape calls. Overwrites FILE unless -o is "
                             "given")
    parser.add_argument("--dxf-dir", metavar="PATH",
                        help="folder to check for DXF files (omit to skip the DXF check)")
    parser.add_argument("--dxf-recursive", action="store_true",
                        help="search the DXF folder recursively")
    parser.add_argument("--dxf-ext", default=".dxf",
                        help="comma-separated DXF extensions (default: .dxf)")
    parser.add_argument("--material-regex", default=r"SHEET",
                        help="materials that require a DXF (default: 'SHEET')")
    parser.add_argument("--sat-dir", metavar="PATH",
                        help="folder to check for SAT files (omit to skip the SAT check)")
    parser.add_argument("--sat-recursive", action="store_true",
                        help="search the SAT folder recursively")
    parser.add_argument("--sat-ext", default=".sat",
                        help="comma-separated SAT extensions (default: .sat)")
    parser.add_argument("--sat-material-regex", default=r"TUBE",
                        help="materials that require a SAT file (default: 'TUBE')")
    # Exports are on whenever a folder is configured (revaudit.conf [folders]
    # pdf / step, or REVAUDIT_PDF_DIR / REVAUDIT_STEP_DIR). Files already in
    # the folder at the current revision are never fetched again, so leaving
    # them on costs nothing once an assembly has been exported once.
    parser.add_argument("--export-drawings", nargs="?", const="", metavar="DIR",
                        default=config.folder("pdf") or None,
                        help="export a PDF of every released drawing (the assembly's "
                             "own + every part's) into DIR, flat and named "
                             "<part>_Rev<rev>.pdf, plus a zip of the same files for "
                             "this run. On by default when a pdf folder is configured; "
                             "DIR overrides it. Drawings whose PDF is already in the "
                             "folder at the current revision are skipped.")
    parser.add_argument("--no-export-drawings", action="store_true",
                        help="skip the drawing PDF export even if a folder is configured")
    parser.add_argument("--drawing-workers", type=int, default=4,
                        help="parallel PDF exports (default: 4)")
    parser.add_argument("--export-step", nargs="?", const="", metavar="DIR",
                        default=config.folder("step") or None,
                        help="export each assembly's own 3D geometry to a single STEP "
                             "file into DIR, flat and named <asm>_Rev<rev>.step. On by "
                             "default when a step folder is configured; DIR overrides "
                             "it. Skipped when the file is already there at the current "
                             "revision. Component names inside the file use Onshape's "
                             "part NAME unless an Export Rule mapping to Part Number is "
                             "configured in Company Settings -> Preferences -> Export "
                             "Rules - this tool cannot set that up for you.")
    parser.add_argument("--no-export-step", action="store_true",
                        help="skip the STEP export even if a folder is configured")
    parser.add_argument("--force-export", action="store_true",
                        help="re-fetch PDFs/STEP files even when they are already in "
                             "the folder")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="report path - one self-contained .html with the audit "
                             "data embedded (default: revaudit-<timestamp>_<asm>.html "
                             "in the configured report folder)")
    parser.add_argument("--open", action="store_true", dest="open_report",
                        help="open the report in the browser when done")
    parser.add_argument("--base-url", help="Onshape base URL (or ONSHAPE_BASE_URL)")
    parser.add_argument("--company-id", help="company id (auto-detected by default)")
    parser.add_argument("--workers", type=int, default=6,
                        help="parallel API lookups (default: 6)")
    parser.add_argument("--part-pattern", default=config.part_pattern(), metavar="REGEX",
                        help="part numbers that get the per-part drawing/revision/file "
                             "checks (default: revaudit.conf [audit] part_pattern, "
                             "else '^PRT-')")
    parser.add_argument("--no-deep", action="store_true",
                        help="don't list what else is released under a part number that "
                             "has no drawing (it costs no API calls either way)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every API call")
    args = parser.parse_args(argv)

    if args.render:
        # Offline path: stored data -> HTML, no credentials needed.
        try:
            data = load_report(args.render)
        except (OSError, ValueError) as exc:
            print(f"Cannot render {args.render}: {exc}", file=sys.stderr)
            return 2
        out_path = Path(args.output) if args.output else Path(args.render).with_suffix(".html")
        save_report(out_path, data)
        print(f"Report written to {out_path}")
        if args.open_report:
            webbrowser.open(out_path.resolve().as_uri())
        return 0
    if not args.assemblies:
        parser.error("give at least one assembly number, or --render FILE")
    if args.workers < 1 or args.drawing_workers < 1:
        parser.error("--workers and --drawing-workers must be at least 1")
    try:
        regexes = {flag: re.compile(raw, re.IGNORECASE) for flag, raw in (
            ("--material-regex", args.material_regex),
            ("--sat-material-regex", args.sat_material_regex),
            ("--part-pattern", args.part_pattern))}
    except re.error as exc:
        parser.error(f"not a valid regular expression: {exc}")

    def export_dir(flag, kind, disabled, fallback):
        """--export-X semantics: absent -> the configured folder (or off);
        bare flag -> configured folder, else a local fallback; DIR -> DIR."""
        if disabled or flag is None:
            return None
        return flag or config.folder(kind) or str(here / fallback)

    pdf_dir = export_dir(args.export_drawings, "pdf", args.no_export_drawings, "drawings")
    step_dir = export_dir(args.export_step, "step", args.no_export_step, "step")

    access = os.environ.get("ONSHAPE_ACCESS_KEY")
    secret = os.environ.get("ONSHAPE_SECRET_KEY")
    if not access or not secret:
        print(
            "Missing credentials.\n"
            "  Create an API key at https://dev-portal.onshape.com (Keys -> Create new key,\n"
            "  read scopes are enough), then put it in a file called .env next to this\n"
            "  script:\n\n"
            "    ONSHAPE_ACCESS_KEY=your_access_key\n"
            "    ONSHAPE_SECRET_KEY=your_secret_key\n"
            "    ONSHAPE_BASE_URL=https://yourcompany.onshape.com\n",
            file=sys.stderr,
        )
        return 2

    base_url = args.base_url or os.environ.get("ONSHAPE_BASE_URL") or DEFAULT_BASE_URL
    client = OnshapeClient(base_url, access, secret, verbose=args.verbose,
                           revision_cache_seconds=config.revision_cache_seconds())

    try:
        company_id = args.company_id or os.environ.get("ONSHAPE_COMPANY_ID")
        if not company_id:
            companies = client.companies()
            if not companies:
                print("No company found for these credentials. Pass --company-id.",
                      file=sys.stderr)
                return 2
            company_id = companies[0]["id"]
            print(f"Company: {companies[0].get('name', '').strip()} ({company_id})")

        def split_exts(raw):
            return tuple(
                e if e.startswith(".") else "." + e
                for e in (x.strip() for x in raw.split(",")) if e
            )

        file_checks = []
        folder_notes = []
        for check_name, folder, recursive, ext_str, regex_flag in (
            ("DXF", args.dxf_dir, args.dxf_recursive, args.dxf_ext, "--material-regex"),
            ("SAT", args.sat_dir, args.sat_recursive, args.sat_ext, "--sat-material-regex"),
        ):
            index = None
            if folder:
                print(f"Indexing {check_name} folder {folder} ...")
                index = FileIndex(folder, recursive, split_exts(ext_str))
                print(f"  {len(index)} files indexed")
                folder_notes.append(f"{check_name} folder {folder}")
            file_checks.append({
                "name": check_name,
                "index": index,
                "material_regex": regexes[regex_flag],
            })

        results = []
        audit_calls = client.call_count        # the company lookup counts as audit
        step_calls = 0
        for asm_pn in args.assemblies:
            print(f"Auditing {asm_pn} ...")
            calls_before = client.call_count
            try:
                result = audit_assembly(
                    client, company_id, asm_pn, file_checks,
                    workers=args.workers, deep=not args.no_deep, verbose=args.verbose,
                    part_pattern=regexes["--part-pattern"],
                )
            except OnshapeError as exc:
                # keep going so one bad assembly does not lose the whole batch
                print(f"  {asm_pn} failed: {exc}", file=sys.stderr)
                results.append(
                    {"partNumber": asm_pn, "errors": [str(exc)], "findings": {}}
                )
                continue
            finally:
                audit_calls += client.call_count - calls_before

            if pdf_dir and result.get("assembly"):
                # Flat, like the DXF/SAT folders - not one subfolder per
                # assembly - so this is a real shared archive: exporting the
                # same part from two assemblies just re-saves the same file,
                # and anything already there at its current rev is reused.
                out_dir = Path(pdf_dir)
                print(f"  exporting drawing PDFs to {out_dir} ...")

                def progress(done, total, _pn=asm_pn):
                    print(f"    {_pn}: {done}/{total} drawings checked", end="\r")

                try:
                    entries = export_assembly_drawings(
                        client, result, out_dir, workers=args.drawing_workers,
                        on_progress=progress, force=args.force_export,
                    )
                    print()  # clear the \r progress line
                    # The zip is a one-off snapshot of this run, not part of
                    # the permanent archive, so it stays in the app folder -
                    # not in the shared folder alongside everyone's PDFs.
                    zip_path = zip_pdfs(entries,
                                        here / f"{safe_filename(asm_pn)}-drawings.zip")
                except OSError as exc:
                    # an unreachable share must not cost the audit already done
                    print(f"    PDF folder unusable: {exc}", file=sys.stderr)
                    entries, zip_path = [], None
                    result["errors"].append(f"Drawing PDF export skipped: {exc}")
                ok, skipped, failed = export_counts(entries)
                print(f"    {ok} exported, {skipped} already there, {failed} failed"
                     + (f", zipped to {zip_path}" if zip_path else ""))
                result["drawingExport"] = {
                    "entries": entries, "zipPath": str(zip_path) if zip_path else None,
                }

            if step_dir and result.get("assembly"):
                step_before = client.call_count
                print(f"  exporting STEP file to {step_dir} ...")
                try:
                    entry = export_assembly_step_file(client, result, Path(step_dir),
                                                      force=args.force_export)
                except OSError as exc:
                    entry = {"pn": asm_pn, "rev": result["assembly"].get("revision"),
                             "path": None, "error": f"STEP folder unusable: {exc}",
                             "skipped": False}
                if entry and entry["error"]:
                    print(f"    failed: {entry['error']}")
                elif entry and entry.get("skipped"):
                    print(f"    already there: {entry['path']}")
                elif entry:
                    print(f"    exported to {entry['path']}")
                result["stepExport"] = entry
                step_calls += client.call_count - step_before

            results.append(result)
    except OnshapeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    usage = api_usage(results, client.call_count, client.cached_count, audit_calls,
                      step_calls)
    print(f"\n{usage['calls']} API calls (audit {usage['auditCalls']}, "
          f"exports {usage['exportCalls']})"
          + (f", {usage['cached']} repeat lookups answered from memory"
             if usage["cached"] else "") + "\n")
    usage_stats.record(usage)
    print("Results")
    print("-" * 60)
    for result in results:
        print_summary(result)
        print()

    # One self-contained .html: the page, with the audit data embedded so it
    # can be re-rendered (--render) or read as data later.
    data = build_report(results, base_url, folder_notes, api=usage)
    # revaudit-<stamp>_<assemblies>: the '_' after the stamp is what tells the
    # web UI this is a CLI file with no share token in its name (see serve.py).
    stamp = ("revaudit-" + datetime.now().strftime("%Y%m%d-%H%M%S")
             + "_" + report_name_part(args.assemblies))
    if args.output:
        out_path = Path(args.output).with_suffix(".html")
    else:
        # config's report folder (usually a K: share), falling back to here
        out_path = Path(config.report_dir()) / f"{stamp}.html"
    try:
        save_report(out_path, data)
    except OSError as exc:
        if args.output:
            raise
        print(f"  {out_path.parent} not writable ({exc}); saving locally", file=sys.stderr)
        out_path = here / f"{stamp}.html"
        save_report(out_path, data)
    print(f"Report written to {out_path}")

    if args.open_report:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
