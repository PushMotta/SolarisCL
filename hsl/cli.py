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

from . import bridge, husk as husk_mod, preflight
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
            frames = ([rop.frame_start, rop.frame_end] if rop.use_frame_range
                      else [rop.frame_start])
            for entry in husk_mod.planned_outputs(manifest, rop, frames=frames):
                if entry["unresolved"]:
                    print(f"    output     {entry['template']}  "
                          f"(contains a token this tool cannot expand)")
                elif entry["files"]:
                    first = entry["files"][0]
                    print(f"    output     {first}"
                          + (f"  …  {entry['files'][-1]}" if len(entry["files"]) > 1
                             else ""))
                else:
                    print(f"    output     {entry['template']}")
            aovs = manifest.aovs_for(settings)
            if aovs:
                print(f"    aovs       {', '.join(v.label for v in aovs)}")

    if manifest.missing_assets:
        print(f"\n  {len(manifest.missing_assets)} missing asset(s) — render will fail without a relink:")
        for asset in manifest.missing_assets:
            print(f"    ✗ {asset.asset_path}  (at {asset.attr_path})")

    if manifest.live_volumes:
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        print(f"\n  {len(manifest.live_volumes)} live volume(s), {total_fields} field(s) "
              f"with no on-disk VDB — a husk USD export will bake ~GB/frame:")
        for v in manifest.live_volumes:
            print(f"    ⚠ {v.prim_path}  ({', '.join(v.field_names)})")
        print("    → render with --engine hython (no export) or cache the volumes to .vdb.")

    for warning in manifest.warnings:
        print(f"\n  warning: {warning}")
    return 0


def _resolve_hython_choice(installs, choice: str) -> str:
    """Map a ``hsl hython --set`` argument to a hython path.

    Accepts a 1-based index into ``installs`` (as printed by ``hsl hython``) or
    an explicit path to an executable. Returns "" if neither matches, so the
    caller can report the problem rather than persisting a bad setting.
    """
    choice = (choice or "").strip()
    if not choice:
        return ""
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(installs):
            return installs[index - 1][1]
        return ""
    return choice if os.path.isfile(choice) else ""


def cmd_hython(args) -> int:
    """List the hython installations found on this machine, or set the default."""
    installs = bridge.list_hython_installations()

    if args.set:
        target = _resolve_hython_choice(installs, args.set)
        if not target:
            sys.stderr.write(
                f"Cannot use '{args.set}' as a hython: it is neither a file on "
                f"disk nor one of the {len(installs)} listed index(es). "
                f"Run 'hsl hython' to see the list.\n")
            return 2
        bridge.save_user_setting("hython_path", target)
        print(f"Default hython set to:\n  {target}")
        return 0

    if not installs:
        sys.stderr.write(
            "No hython installations found.\n"
            "Set $HFS (source houdini_setup), set $HSL_HYTHON to the hython "
            "binary, or pass --hython explicitly.\n")
        return 1

    active = bridge.find_hython()
    print(f"{len(installs)} hython installation(s) found "
          f"(* = the one that will be used):\n")
    for index, (label, path) in enumerate(installs, 1):
        mark = "*" if os.path.normcase(path) == os.path.normcase(active) else " "
        print(f" {mark} [{index}] {label}")
        print(f"        {path}")
    print("\nChoose one with:  hsl hython --set <index|path>")
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
    engine = ("hython" if getattr(args, "direct_hython", False)
              else getattr(args, "engine", husk_mod.DEFAULT_ENGINE))

    # --aovs edits the exported USD's product orderedVars, which the hython
    # engine has no equivalent of. Silently ignoring it would render the wrong
    # AOVs, so reject it here — before the scene is loaded, so a bad argv costs
    # no Houdini launch. (--relink-from *is* supported on both engines: hython
    # sublayers the repaths into the LOP network instead.)
    if engine == "hython" and getattr(args, "aovs", ""):
        sys.stderr.write(
            "--aovs only applies to the husk engine: it rewrites the exported "
            "USD's product orderedVars, and the hython engine renders the ROP "
            "directly without exporting.\n"
            "  → add --engine husk to use it, or drop it to render with hython.\n")
        return 4

    # Parsed before the scene loads: a malformed --set should not cost a
    # Houdini launch to discover.
    try:
        settings_overrides = husk_mod.parse_setting_args(
            getattr(args, "setting_overrides", []))
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 4

    export_usd = (engine == "husk")
    allow_volume_bake = getattr(args, "allow_volume_bake", False)
    # Reuse a cached scene read when there is nothing to export — that skips a
    # whole Houdini launch. Never when exporting: the point of that run is to
    # write the USD, and a cached manifest's usd_path may be long deleted.
    manifest = None
    if not export_usd and not args.no_cache:
        manifest = bridge.load_cached(args.hip)
        if manifest is not None:
            sys.stderr.write(f"Using the cached scene read "
                             f"({bridge.cache_path_for(args.hip)}); "
                             f"--no-cache to read the scene again.\n")

    if manifest is None:
        manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                      export_usd=export_usd, usd_dir=args.usd_dir,
                                      flatten=args.flatten, rop=args.rop,
                                      allow_volume_bake=allow_volume_bake,
                                      # Export only the frames asked for, not
                                      # the ROP's whole authored range.
                                      export_frames=args.frames if export_usd else None)
        if not export_usd:
            try:
                bridge.save_cached(manifest)
            except OSError:
                pass                       # a cache we cannot write is not an error
    rop = _pick_rop(manifest, args.rop)
    if rop is None:
        return 2

    # Live volumes bake tens of GB/frame into a husk USD export. Unless the user
    # opted in with --allow-volume-bake, the inspector skipped the export — so
    # explain why and how to proceed instead of the generic failure below.
    if engine == "husk" and manifest.live_volumes and not allow_volume_bake:
        n = len(manifest.live_volumes)
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        sys.stderr.write(
            f"Aborted: {n} live volume(s) ({total_fields} field(s) with no "
            f"on-disk VDB) would bake tens of GB per frame into the USD export.\n"
            f"  → render with --engine hython (no export, recommended for this shot),\n"
            f"  → or cache the volumes to .vdb and re-read,\n"
            f"  → or pass --allow-volume-bake to export anyway.\n")
        for v in manifest.live_volumes:
            sys.stderr.write(f"    ⚠ {v.prim_path}  ({', '.join(v.field_names)})\n")
        return 3

    if engine == "husk" and manifest.live_volumes and allow_volume_bake:
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        sys.stderr.write(
            f"--allow-volume-bake: exporting {len(manifest.live_volumes)} live "
            f"volume(s), {total_fields} field(s) — expect tens of GB per frame.\n")

    if engine == "husk" and not rop.usd_path:
        sys.stderr.write("The stage could not be written to USD; nothing to render.\n")
        for warning in manifest.warnings:
            sys.stderr.write(f"  {warning}\n")
        return 3

    if args.frames:
        start, end, inc = args.frames
        rop.frame_start, rop.frame_end, rop.frame_inc = start, end, inc
        rop.use_frame_range = end != start

    # Missing textures fail the render mid-flight — surface them for *both*
    # engines (they are a property of the scene, not of the export), and relink
    # if asked. Reporting these was husk-only until hython became the default,
    # which would have made broken textures silent on the common path.
    base_usd = rop.usd_path
    if manifest.missing_assets:
        sys.stderr.write(f"{len(manifest.missing_assets)} unresolved asset(s) in the scene:\n")
        for asset in manifest.missing_assets:
            sys.stderr.write(f"  ✗ {asset.asset_path}\n")
        if engine == "hython" and not getattr(args, "relink_from", None):
            sys.stderr.write(
                "  (pass --relink-from DIR to repath them, or fix the scene.)\n")

    if engine == "husk" and getattr(args, "relink_from", None) and not args.dry_run:
        try:
            result = bridge.relink_assets(base_usd, args.relink_from, hython=args.hython)
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
        if result.get("usd_out"):
            base_usd = result["usd_out"]
            sys.stderr.write(f"Relinked {len(result['relinked'])} asset(s); "
                             f"{len(result['still_missing'])} still missing.\n")
            # Drop what the relink resolved, so preflight below judges the USD
            # that is actually about to render — not the state before the fix.
            resolved = {entry.get("old") for entry in result.get("relinked", [])}
            manifest.missing_assets = [a for a in manifest.missing_assets
                                       if a.asset_path not in resolved]

    # AOV selection is a USD edit, not a husk flag: keep only the requested
    # RenderVars by rendering an overlay produced by bridge.filter_aovs.
    usd_for_render = base_usd
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
            usd_for_render = bridge.filter_aovs(base_usd, keep, hython=args.hython)
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3

    # Karma knobs are USD attributes, not husk flags. On husk they go into an
    # overlay now; on hython render_direct sublayers them at render start.
    if engine == "husk" and settings_overrides and not args.dry_run:
        try:
            result = bridge.override_settings(usd_for_render, settings_overrides,
                                              hython=args.hython,
                                              settings_prim=args.settings or "")
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
        if result.get("usd_out"):
            usd_for_render = result["usd_out"]
        for entry in result.get("applied", []):
            sys.stderr.write(f"  {entry['key']}: {entry['old']!r} -> {entry['new']!r}\n")
        if result.get("skipped"):
            for entry in result["skipped"]:
                sys.stderr.write(f"  skipped {entry['key']}: {entry['why']}\n")

    # husk -o redirects only the FIRST product, so on a multi-product shot the
    # crypto/depth passes would quietly keep writing where the scene pointed
    # them. Redirect them all in USD instead. One product needs no overlay --
    # husk's own flag does the job without a hython round-trip.
    output_for_engine = args.output or None
    settings_for_output = manifest.resolve_settings(rop)
    product_count = len(settings_for_output.products) if settings_for_output else 0
    if engine == "husk" and args.output and product_count > 1:
        if args.dry_run:
            print(f"# {product_count} products will be redirected under {args.output} "
                  f"by a USD overlay authored at render time (husk -o moves only the first)")
        else:
            try:
                usd_for_render = bridge.override_output(
                    usd_for_render, args.output, hython=args.hython)
            except bridge.InspectError as exc:
                sys.stderr.write(f"{exc}\n")
                return 3
            sys.stderr.write(f"Redirected all {product_count} product(s) under "
                             f"{args.output}.\n")
            output_for_engine = None      # the overlay did it; don't double-apply

    jobs = husk_mod.jobs_for_rop(
        manifest, rop, usd_for_render,
        chunk_size=args.chunk,
        engine=engine,
        renderer=args.renderer or None,
        camera=args.camera or None,
        settings_prim=args.settings or None,
        output=output_for_engine,
        threads=args.threads or None,
        snapshot_interval=args.snapshot or None,
        resolution=tuple(args.res) if args.res else None,
        # hython repaths inside the LOP network at render time; the husk path
        # already relinked the exported USD above, so it needs nothing here.
        relink_dirs=(args.relink_from or None) if engine == "hython" else None,
        settings_overrides=(settings_overrides or None) if engine == "hython" else None,
        extra_args=args.extra or None,
    )

    # Preflight — frame range, resolution, output path, free disk, unresolved
    # assets, volume bake. This ran only in the GUI until now, so CLI and farm
    # users got none of it.
    checks = preflight.run_preflight_checks(jobs[0], manifest)
    for check in checks:
        sys.stderr.write(f"[preflight {check.level}] {check.message}\n")
    errors = [c for c in checks if c.level == "error"]
    if errors and not args.dry_run and not args.skip_preflight:
        sys.stderr.write(
            f"\n{len(errors)} preflight error(s) — nothing rendered. Fix them, or "
            f"re-run with --skip-preflight to render anyway.\n")
        return 5

    if args.dry_run:
        if keep is not None:
            print(f"# AOVs filtered to: {args.aovs} "
                  f"(overlay USD authored from {os.path.basename(rop.usd_path)} at render time)")

        # What this will actually leave on disk, resolved per frame.
        chunk_frames = [f for job in jobs
                        for f in (job.chunk.start + i * job.chunk.inc
                                  for i in range(job.chunk.count))]
        planned = husk_mod.planned_outputs(manifest, rop, output=args.output,
                                           frames=chunk_frames)
        if planned:
            print("# Files this render will write:")
            for entry in planned:
                if entry["unresolved"]:
                    print(f"#   {entry['template']}  "
                          f"(unexpandable token — cannot preview the filename)")
                elif entry["files"]:
                    print(f"#   {entry['files'][0]}")
                    if len(entry["files"]) > 1:
                        print(f"#   … {len(entry['files'])} files, last "
                              f"{entry['files'][-1]}")
                else:
                    print(f"#   {entry['template']}   "
                          f"(filename decided by husk / the ROP)")
        else:
            print("# No output path is declared in the scene; husk/the ROP decides it.")

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
    p_render.add_argument("--engine", choices=["husk", "hython"],
                          default=husk_mod.DEFAULT_ENGINE,
                          help="Render engine: hython renders the ROP directly with no "
                               "USD export (default); husk exports USD first, which is "
                               "what --aovs, --relink-from and farm submission need")
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
    p_render.add_argument("--relink-from", action="append", default=[], metavar="DIR",
                          help="Search DIR for missing textures and repath them "
                               "(repeatable; works on both engines)")
    # NB: dest is deliberately not "settings" — that is already --settings, the
    # RenderSettings prim path, and reusing it would silently clobber it.
    p_render.add_argument("--set", action="append", default=[],
                          dest="setting_overrides", metavar="KEY=VALUE",
                          help="Override a render-settings attribute, e.g. "
                               "karma:global:samplesperpixel=64. husk has no flag "
                               "for these, so they are authored as a USD overlay "
                               "(repeatable; works on both engines)")
    p_render.add_argument("--allow-volume-bake", action="store_true",
                          help="Export live (SOP-imported) volumes to USD even though "
                               "they bake ~GB/frame. Default: abort and suggest --engine hython.")
    p_render.add_argument("--output", default="", help="Override the output image path (husk --output)")
    p_render.add_argument("--res", nargs=2, type=int, metavar=("W", "H"))
    p_render.add_argument("--threads", type=int, default=0)
    p_render.add_argument("--snapshot", type=int, default=0, metavar="SECONDS")
    p_render.add_argument("--extra", nargs=argparse.REMAINDER,
                          help="Everything after this is passed to husk verbatim")
    p_render.add_argument("--no-cache", action="store_true",
                          help="Always re-read the scene instead of reusing a "
                               "cached read of an unchanged .hip")
    p_render.add_argument("--skip-preflight", action="store_true",
                          help="Render even if preflight reports errors (missing "
                               "textures, unwritable output path, …)")
    p_render.add_argument("--dry-run", action="store_true",
                          help="Print the render commands and stop")
    p_render.set_defaults(func=cmd_render)

    p_hython = sub.add_parser("hython",
                              help="List the Houdini/hython installs found on this machine")
    p_hython.add_argument("--set", default="", metavar="INDEX|PATH",
                          help="Remember this install as the default (by list index or path)")
    p_hython.set_defaults(func=cmd_hython)

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
