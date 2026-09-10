#!/usr/bin/env python3
"""
Publish a DXF filename index so a host that cannot see the share can still
check DXF coverage.

Only file NAMES leave this machine - never the drawings themselves. The audit
matches part numbers against filenames, so a name list is all it needs.

Run it on any PC that can see the share:

    py -3.13 dxf_indexer.py --dir "<share path>" --out dxf-index.json
    py -3.13 dxf_indexer.py --dir "<share path>" --post http://<host>:8000/dxf-index

Schedule it hourly with Task Scheduler and everyone gets a fresh index with
nothing installed on their own machine:

    schtasks /create /tn "RevAudit DXF index" /sc hourly ^
      /tr "py -3.13 C:\\path\\to\\dxf_indexer.py --dir <share> --post http://<host>:8000/dxf-index"
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_DIR = os.environ.get("REVAUDIT_DXF_DIR", "")   # pass --dir if not set


def build_index(folder, recursive=False, extensions=(".dxf",)):
    root = Path(folder)
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")
    exts = tuple(e.lower() if e.startswith(".") else "." + e.lower()
                 for e in extensions)
    names = []
    walker = root.rglob("*") if recursive else root.glob("*")
    for path in walker:
        try:
            if path.is_file() and path.suffix.lower() in exts:
                names.append(path.name)
        except OSError:
            continue
    return {
        "folder": str(root),
        "recursive": bool(recursive),
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(names),
        "names": sorted(names),
    }


def post_index(url, index, token=None):
    body = gzip.compress(json.dumps(index).encode("utf-8"))
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    if token:
        headers["X-Index-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"Could not reach {url}: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Publish a DXF filename index (names only, never file contents).")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="folder to index")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--ext", default=".dxf", help="comma-separated extensions")
    parser.add_argument("--out", help="write the index to this JSON file")
    parser.add_argument("--post", metavar="URL",
                        help="upload the index to a RevAudit host")
    parser.add_argument("--token", default=os.environ.get("REVAUDIT_INDEX_TOKEN"),
                        help="shared token the host requires (or REVAUDIT_INDEX_TOKEN)")
    args = parser.parse_args(argv)

    if not args.dir:
        parser.error("no folder to index: pass --dir, or set REVAUDIT_DXF_DIR")

    started = datetime.now()
    index = build_index(args.dir, args.recursive,
                        [e.strip() for e in args.ext.split(",") if e.strip()])
    secs = (datetime.now() - started).total_seconds()
    raw = len(json.dumps(index).encode())
    print(f"Indexed {index['count']} files from {index['folder']} in {secs:.1f}s "
          f"({raw / 1024:.0f} KB of names)")

    if not args.out and not args.post:
        args.out = "dxf-index.json"

    if args.out:
        Path(args.out).write_text(json.dumps(index), encoding="utf-8")
        print(f"Wrote {args.out}")

    if args.post:
        status, body = post_index(args.post, index, args.token)
        print(f"POST {args.post} -> {status} {body}")
        if status == 401:
            print("The host rejected the token. Set REVAUDIT_INDEX_TOKEN to match "
                  "the value in the host's .env.", file=sys.stderr)
            return 1
        if status >= 300:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
