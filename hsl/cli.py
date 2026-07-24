"""Headless entry point -- the same engine as the GUI, for terminals and farms.

    hsl inspect shot.hip
    hsl render  shot.hip --rop /stage/usdrender1 --frames 1001-1100 --chunk 10
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from typing import Optional

from . import bridge, husk as husk_mod
from .manifest import RenderRop, SceneManifest
from .runner import RenderQueue, State

_FRAMES = re.compile(r"^(-?\d+)(?:[-:](-?\d+))?(?:[x/](\d+))?$")


def parse_frames(text: str) -> tuple[int, int, int]:
    """Parse ``1001``, ``1001-1100`` or ``1001-1100x2``."""
    match = _FRAMES.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Cannot read '{text}' as a frame range. Use 1001, 1001-1100 or 1001-1100x2."
        )
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    inc = int(match.group(3) or 1)
    return start, end, inc


def _pick_rop(manifest: SceneManifest, requested: str) -> Optional[RenderRop]:
    if requested:
        rop = manifest.rop(requested)
        if rop is None:
            names = "\n  ".join(r.node_path for r in manifest.rops) or "(none)"
            sys.stderr.write(f"No ROP at {requested}. Scene contains:\n  {names}\n")
        return rop
    if len(manifest.rops) == 1:
        return manifest.rops[0]
    if not manifest.rops:
        sys.stderr.write("No USD Render ROPs found in this scene.\n")
        return None
    names = "\n  ".join(r.node_path for r in manifest.rops)
    sys.stderr.write(f"Several ROPs found; choose one with --rop:\n  {names}\n")
    return None


def cmd_inspect(args) -> int:
    manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                  export_usd=args.export_usd,
                                  usd_dir=args.usd_dir, flatten=args.flatten)
    if args.json:
        print(manifest.to_json())
        return 0

    print(f"{manifest.hip_path}   Houdini {manifest.houdini_version}   {manifest.fps} fps")
    for rop in manifest.rops:
        settings = manifest.resolve_settings(rop)
        print(f"\n  {rop.node_path}  [{rop.node_type}]")
        print(f"    renderer   {rop.renderer or '—'}")
        print(f"    frames     {rop.frame_start}-{rop.frame_end} step {rop.frame_inc}")
        if rop.usd_path:
            print(f"    usd        {rop.usd_path}")
        if settings:
            resolution = "%d×%d" % settings.resolution if settings.resolution else "—"
            print(f"    settings   {settings.prim_path}")
            print(f"    resolution {resolution}")
            print(f"    camera     {settings.camera or '—'}")
            for path in manifest.outputs_for(settings):
                print(f"    output     {path}")
            aovs = manifest.aovs_for(settings)
            if aovs:
                print(f"    aovs       {', '.join(v.label for v in aovs)}")

    for warning in manifest.warnings:
        print(f"\n  warning: {warning}")
    return 0


def _aov_keys(var) -> set:
    """Names a var can be referred to by: prim basename, label, source name."""
    basename = var.prim_path.rsplit("/", 1)[-1]
    return {k.lower() for k in (basename, var.label, var.source_name) if k}


def _resolve_aovs(manifest: SceneManifest, spec: str) -> list:
    """Map a comma spec (``beauty,depth``) to matching RenderVar prim paths."""
    wanted = [s.strip().lower() for s in spec.split(",") if s.strip()]
    return [v.prim_path for v in manifest.vars
            if any(w in _aov_keys(v) for w in wanted)]


def cmd_render(args) -> int:
    engine = "hython" if getattr(args, "direct_hython", False) else getattr(args, "engine", "husk")
    export_usd = (engine == "husk")
    manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                  export_usd=export_usd, usd_dir=args.usd_dir,
                                  flatten=args.flatten, rop=args.rop)
    rop = _pick_rop(manifest, args.rop)
    if rop is None:
        return 2
    if engine == "husk" and not rop.usd_path:
        sys.stderr.write("The stage could not be written to USD; nothing to render.\n")
        for warning in manifest.warnings:
            sys.stderr.write(f"  {warning}\n")
        return 3

    if args.frames:
        start, end, inc = args.frames
        rop.frame_start, rop.frame_end, rop.frame_inc = start, end, inc
        rop.use_frame_range = end != start

    # AOV selection is a USD edit, not a husk flag: keep only the requested
    # RenderVars by rendering an overlay produced by bridge.filter_aovs.
    usd_for_render = rop.usd_path
    keep = None
    if engine == "husk" and getattr(args, "aovs", ""):
        keep = _resolve_aovs(manifest, args.aovs)
        if not keep:
            available = sorted({v.prim_path.rsplit("/", 1)[-1] for v in manifest.vars})
            sys.stderr.write(f"--aovs '{args.aovs}' matched no RenderVars. "
                             f"Available: {', '.join(available) or '(none)'}\n")
            return 4
        if set(keep) == {v.prim_path for v in manifest.vars}:
            keep = None                       # selection is everything: no filter

    if keep is not None and not args.dry_run:
        try:
            usd_for_render = bridge.filter_aovs(rop.usd_path, keep, hython=args.hython)
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3

    jobs = husk_mod.jobs_for_rop(
        manifest, rop, usd_for_render,
        chunk_size=args.chunk,
        engine=engine,
        renderer=args.renderer or None,
        camera=args.camera or None,
        settings_prim=args.settings or None,
        output=args.output or None,
        threads=args.threads or None,
        snapshot_interval=args.snapshot or None,
        resolution=tuple(args.res) if args.res else None,
        extra_args=args.extra or None,
    )

    if args.dry_run:
        if keep is not None:
            print(f"# AOVs filtered to: {args.aovs} "
                  f"(overlay USD authored from {os.path.basename(rop.usd_path)} at render time)")
        for job in jobs:
            print(husk_mod.format_command(husk_mod.build_command(job)))
        return 0

    lock = threading.Lock()

    def on_event(event, *payload):
        if event == "task_output":
            task, line = payload
            with lock:
                sys.stdout.write(f"[{task.job.chunk}] {line}\n")
                sys.stdout.flush()
        elif event == "task_finished":
            task = payload[0]
            with lock:
                sys.stderr.write(
                    f"[{task.job.chunk}] {task.state.value} "
                    f"in {task.duration:.1f}s (exit {task.returncode})\n"
                )

    queue = RenderQueue(jobs, max_parallel=args.parallel, on_event=on_event)
    queue.start(block=True)

    failed = [t for t in queue.tasks if t.state is State.FAILED]
    if failed:
        sys.stderr.write(f"\n{len(failed)} of {len(queue.tasks)} chunk(s) failed.\n")
        return 1
    sys.stderr.write(f"\nRendered {len(queue.tasks)} chunk(s).\n")
    return 0


def cmd_ui(args) -> int:
    from .ui import main as ui_main
    argv = ["hsl"]
    if args.hip:
        argv.append(args.hip)
    return ui_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hsl", description="Launch Solaris renders from a .hip file.")
    parser.add_argument("--hython", default="", help="Path to hython")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("hip")
    common.add_argument("--usd-dir", default="", help="Where to write exported USD")
    common.add_argument("--flatten", action="store_true",
                        help="Flatten the stage on export")

    p_inspect = sub.add_parser("inspect", parents=[common],
                               help="Describe the scene without rendering")
    p_inspect.add_argument("--json", action="store_true", help="Emit raw JSON")
    p_inspect.add_argument("--export-usd", action="store_true",
                           help="Also write the USD to disk")
    p_inspect.set_defaults(func=cmd_inspect)

    p_render = sub.add_parser("render", parents=[common], help="Render with husk or hython")
    p_render.add_argument("--engine", choices=["husk", "hython"], default="husk",
                          help="Render engine: husk (USD export) or hython (direct ROP)")
    p_render.add_argument("--direct-hython", action="store_true",
                          help="Shortcut for --engine hython (0 USD disk space)")
    p_render.add_argument("--rop", default="", help="ROP node path")
    p_render.add_argument("--frames", type=parse_frames, default=None,
                          help="1001, 1001-1100 or 1001-1100x2")
    p_render.add_argument("--chunk", type=int, default=0,
                          help="Frames per husk process (0 = one process)")
    p_render.add_argument("--parallel", type=int, default=1,
                          help="Concurrent husk processes")
    p_render.add_argument("--renderer", default="")
    p_render.add_argument("--camera", default="")
    p_render.add_argument("--settings", default="", help="RenderSettings prim path")
    p_render.add_argument("--aovs", default="",
                          help="Comma list of AOVs to keep (by name), e.g. beauty,depth. "
                               "Others are dropped from the USD products (husk only)")
    p_render.add_argument("--output", default="")
    p_render.add_argument("--res", nargs=2, type=int, metavar=("W", "H"))
    p_render.add_argument("--threads", type=int, default=0)
    p_render.add_argument("--snapshot", type=int, default=0, metavar="SECONDS")
    p_render.add_argument("--extra", nargs=argparse.REMAINDER,
                          help="Everything after this is passed to husk verbatim")
    p_render.add_argument("--dry-run", action="store_true",
                          help="Print the husk commands and stop")
    p_render.set_defaults(func=cmd_render)

    p_ui = sub.add_parser("ui", help="Open the launcher window")
    p_ui.add_argument("hip", nargs="?", default="")
    p_ui.set_defaults(func=cmd_ui)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except bridge.InspectError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
