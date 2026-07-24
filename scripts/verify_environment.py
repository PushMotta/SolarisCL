#!/usr/bin/env python3
"""Probe a real Houdini install and reconcile it against docs/UNVERIFIED.md.

Run this on a machine that has Houdini. Everything it confirms can be moved
from the unverified register into the Verified table.

    python scripts/verify_environment.py            # summary
    python scripts/verify_environment.py --report   # full table
    python scripts/verify_environment.py --json     # machine readable

Plain Python — it shells out to hython for the Houdini half, exactly like
hsl.bridge does, so a failure here also tells you whether the bridge works.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hsl.bridge import find_hython          # noqa: E402
from hsl.husk import find_husk               # noqa: E402

# Section E of docs/UNVERIFIED.md: flags build_command() emits.
HUSK_FLAGS = [
    ("E1", "--renderer"), ("E2a", "--frame"), ("E2b", "--frame-count"),
    ("E2c", "--frame-inc"), ("E3", "--settings"), ("E4", "--camera"),
    ("E5", "--output"), ("E6", "--res"), ("E7", "--threads"),
    ("E8", "--complexity"), ("E9", "--purpose"), ("E10", "--snapshot"),
    ("E11", "--make-output-path"), ("E12", "--fast-exit"),
    ("E13", "--verbose"), ("E14", "--list-renderers"),
]

OK, MISSING, SKIP = "OK", "MISSING", "SKIP"

# _probe_houdini.py tags its JSON line with this so we can find it even when a
# render delegate (e.g. Octane) prints bracket-tagged banners around it.
PROBE_SENTINEL = "@@HSL_PROBE_JSON@@"


def check_husk(husk_exe: str) -> list[dict]:
    """Confirm each flag appears in husk --help."""
    results = []
    if not husk_exe:
        return [{"id": i, "item": f, "status": SKIP, "note": "husk not found"}
                for i, f in HUSK_FLAGS]

    try:
        proc = subprocess.run([husk_exe, "--help"], capture_output=True,
                              text=True, timeout=60)
        help_text = proc.stdout + proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"id": i, "item": f, "status": SKIP, "note": f"husk --help failed: {exc}"}
                for i, f in HUSK_FLAGS]

    for ident, flag in HUSK_FLAGS:
        present = flag in help_text
        note = ""
        if present and flag == "--verbose":
            note = "confirm the flag-letter syntax ('a' = ALF_PROGRESS) by eye"
        results.append({
            "id": ident, "item": flag,
            "status": OK if present else MISSING,
            "note": note or ("" if present else "not in --help; fix build_command()"),
        })

    # The delegate names the UI offers by default.
    try:
        proc = subprocess.run([husk_exe, "--list-renderers"],
                              capture_output=True, text=True, timeout=60)
        listed = (proc.stdout + proc.stderr).strip()
        results.append({
            "id": "E14b", "item": "delegates registered",
            "status": OK if listed else MISSING,
            "note": listed.replace("\n", " ")[:120] or "no output",
        })
    except (OSError, subprocess.SubprocessError):
        pass

    return results


def check_houdini(hython_exe: str) -> list[dict]:
    """Run the hython-side probe and return its findings."""
    if not hython_exe:
        return [{"id": "A-D", "item": "Houdini probe", "status": SKIP,
                 "note": "hython not found; set $HFS or $HSL_HYTHON"}]

    probe = os.path.join(ROOT, "scripts", "_probe_houdini.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")

    try:
        proc = subprocess.run([hython_exe, probe], capture_output=True,
                              text=True, timeout=900, env=env)
    except subprocess.SubprocessError as exc:
        return [{"id": "F4", "item": "hython bridge", "status": MISSING,
                 "note": f"could not run hython: {exc}"}]

    # The probe tags its JSON line with PROBE_SENTINEL. Scan upward for it, so a
    # render delegate that prints "[Octane] ..." banners after the JSON (or
    # Houdini's own license chatter before it) cannot be mistaken for the
    # payload — the old "last line starting with [" heuristic did exactly that.
    payload = ""
    for line in reversed(proc.stdout.splitlines()):
        marker = line.find(PROBE_SENTINEL)
        if marker != -1:
            payload = line[marker + len(PROBE_SENTINEL):]
            break

    if not payload:
        return [{"id": "F4", "item": "hython bridge", "status": MISSING,
                 "note": f"probe returned no JSON. stderr: {proc.stderr[-300:]}"}]

    try:
        return json.loads(payload)
    except ValueError as exc:
        return [{"id": "F4", "item": "hython bridge", "status": MISSING,
                 "note": f"probe JSON unreadable: {exc}"}]


def render_report(results: list[dict], verbose: bool) -> None:
    width = max((len(r["item"]) for r in results), default=10)
    for row in results:
        if not verbose and row["status"] == OK:
            continue
        mark = {OK: "  ok ", MISSING: " FAIL", SKIP: " skip"}[row["status"]]
        line = f"{mark}  {row['id']:<6} {row['item']:<{width}}"
        if row.get("note"):
            line += f"  {row['note']}"
        print(line)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true",
                        help="Show passing checks too")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON")
    parser.add_argument("--husk", default="", help="Path to husk")
    parser.add_argument("--hython", default="", help="Path to hython")
    parser.add_argument("--skip-houdini", action="store_true",
                        help="Only check husk (fast)")
    args = parser.parse_args(argv)

    husk_exe = find_husk(args.husk)
    hython_exe = find_hython(args.hython)

    results = check_husk(husk_exe)
    if not args.skip_houdini:
        results += check_houdini(hython_exe)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"husk    {husk_exe or 'not found'}")
    print(f"hython  {hython_exe or 'not found'}")
    print(f"HFS     {os.environ.get('HFS', 'unset')}\n")

    render_report(results, args.report)

    counts = {status: sum(1 for r in results if r["status"] == status)
              for status in (OK, MISSING, SKIP)}
    print(f"\n{counts[OK]} confirmed, {counts[MISSING]} wrong, {counts[SKIP]} not checked")

    if counts[MISSING]:
        print("\nFix the FAIL rows in the code, then move confirmed rows into the")
        print("Verified table in docs/UNVERIFIED.md with your Houdini version.")
    elif counts[SKIP] == len(results):
        print("\nNothing was checked. This needs a machine with Houdini installed.")
    else:
        print("\nMove the confirmed rows into docs/UNVERIFIED.md > Verified.")

    return 1 if counts[MISSING] else 0


if __name__ == "__main__":
    sys.exit(main())
