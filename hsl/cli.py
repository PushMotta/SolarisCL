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
from dataclasses import replace
from typing import Optional

from . import bridge, husk as husk_mod, memlog, preflight, sysinfo
from .manifest import (
    TASK_CACHE, TASK_RENDER, TASK_SIM, RenderRop, SceneManifest,
)
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


def _pick_rops(manifest: SceneManifest, requested, all_rops: bool):
    """The render ROPs to run, in the order asked for.

    Returns None when selection failed (already reported to stderr). With no
    request at all this defers to :func:`_pick_rop`, so a single-ROP scene
    still renders without flags and a multi-ROP scene still asks the user to
    choose — rendering *everything* stays an explicit ``--all-rops`` opt-in.
    """
    if all_rops:
        if not manifest.rops:
            sys.stderr.write("No USD Render ROPs found in this scene.\n")
            return None
        return list(manifest.rops)
    if requested:
        chosen, missing = [], []
        for path in requested:
            rop = manifest.rop(path)
            if rop is None:
                missing.append(path)
            elif rop in chosen:
                sys.stderr.write(f"Ignoring duplicate --rop {path}.\n")
            else:
                chosen.append(rop)
        if missing:
            names = "\n  ".join(r.node_path for r in manifest.rops) or "(none)"
            for path in missing:
                sys.stderr.write(f"No ROP at {path}.\n")
            sys.stderr.write(f"Scene contains:\n  {names}\n")
            return None
        return chosen
    single = _pick_rop(manifest, "")
    return [single] if single else None


def _run_queue(jobs, parallel: int, verb: str) -> int:
    """Drive jobs through a RenderQueue with console reporting; exit code.

    Shared by render and batch. Chunks tagged with a task id (multi-ROP,
    batch) get it as a prefix so interleaved output stays attributable; an
    untagged single-ROP render prints exactly what it always did.
    """
    lock = threading.Lock()

    def on_event(event, *payload):
        if event == "task_output":
            task, line = payload
            with lock:
                prefix = f"{task.job.task_id} " if task.job.task_id else ""
                sys.stdout.write(f"[{prefix}{task.job.chunk}] {line}\n")
                sys.stdout.flush()
        elif event == "task_finished":
            task = payload[0]
            with lock:
                prefix = f"{task.job.task_id} " if task.job.task_id else ""
                sys.stderr.write(
                    f"[{prefix}{task.job.chunk}] {task.state.value} "
                    f"in {task.duration:.1f}s (exit {task.returncode})\n"
                )

    try:
        queue = RenderQueue(jobs, max_parallel=parallel, on_event=on_event)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 4
    queue.start(block=True)

    for warning in queue.warnings:
        sys.stderr.write(f"warning: {warning}\n")

    failed = [t for t in queue.tasks if t.state is State.FAILED]
    # A skipped chunk never ran because something upstream of it did not
    # finish. Nothing was rendered, so it cannot count as success either.
    skipped = [t for t in queue.tasks if t.state is State.SKIPPED]
    if failed or skipped:
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed")
        if skipped:
            parts.append(f"{len(skipped)} skipped (a dependency did not finish)")
        sys.stderr.write(f"\n{', '.join(parts)}, of {len(queue.tasks)} chunk(s).\n")
        return 1
    sys.stderr.write(f"\n{verb} {len(queue.tasks)} chunk(s).\n")
    return 0


def cmd_memory(args) -> int:
    """Show what past renders actually used, or what this machine can measure.

    Deliberately a *record*, not a prediction. Every number here came off a
    render that really ran; nothing is modelled or extrapolated.
    """
    _human_bytes = sysinfo.human_bytes

    if args.machine:
        info = sysinfo.describe()
        print(f"platform        {info.get('platform_detail') or info.get('platform') or 'unknown'}")
        print(f"installed RAM   {_human_bytes(info.get('total_ram_bytes'))}")
        print(f"available RAM   {_human_bytes(info.get('available_ram_bytes'))}")
        method = info.get("peak_rss_method") or ""
        print(f"per-render RAM  {'yes' if info.get('peak_rss_available') else 'no'}"
              + (f"   ({method})" if method else ""))
        # Whether that figure covers a renderer launched through a wrapper
        # script, or only the wrapper. Worth its own line: the two differ by
        # a factor of forty and nothing else in the report would show it.
        tree_method = info.get("peak_rss_tree_method") or ""
        print(f"whole-tree RAM  {'yes' if info.get('peak_rss_tree') else 'no'}"
              + (f"   ({tree_method})" if tree_method else ""))
        smi = info.get("nvidia_smi") or ""
        print(f"nvidia-smi      {smi or 'not found'}")
        for gpu in info.get("gpus") or []:
            print(f"  gpu           [{gpu.get('index')}] {gpu.get('name')}  "
                  f"{_human_bytes(gpu.get('total_vram_bytes'))}")
        print(f"per-render VRAM {'yes' if info.get('per_process_vram') else 'no'}")
        for note in info.get("notes") or []:
            print(f"\n  note: {note}")
        return 0

    if args.forget:
        removed = memlog.forget(args.hip or "")
        scope = args.hip or "every scene"
        print(f"Forgot {removed} recorded render(s) for {scope}.")
        return 0

    entries = memlog.history(args.hip or "")
    if not entries:
        print("Nothing measured yet.\n"
              "Renders record what they used as they run, so this fills in by\n"
              "itself. 'hsl memory --machine' shows what can be measured here.")
        return 0

    import datetime
    print(f"{len(entries)} measured render(s), newest first. "
          f"These are measurements, not predictions.\n")
    shown = entries[:args.limit]
    for entry in shown:
        when = (datetime.datetime.fromtimestamp(entry.when).strftime("%Y-%m-%d %H:%M")
                if entry.when else "unknown time")
        vram = _human_bytes(entry.peak_vram)
        if entry.peak_vram and entry.vram_sampled:
            vram += " (sampled)"
        # A single-process figure must never be shown as if it were the whole
        # tree. A render launched through a wrapper script measured that way
        # reads as the wrapper -- megabytes for a job that used gigabytes --
        # so the scope is printed next to the number, not inferred from it.
        ram = _human_bytes(entry.peak_rss)
        if entry.peak_rss:
            ram += " (whole tree)" if entry.peak_rss_is_tree \
                else " (main process only)"
        print(f"  {when}   RAM {ram}   VRAM {vram}")
        print(f"      {entry.hip_path}")
        print(f"      {entry.rop_path or '(no rop)'}  [{entry.engine}]  "
              f"{entry.frames} frame(s)")
    if len(entries) > args.limit:
        print(f"\n  ... {len(entries) - args.limit} older, --limit to see more")

    if any(e.peak_rss and not e.peak_rss_is_tree for e in shown):
        print("\n  'main process only' means the figure covers just the process\n"
              "  hsl started. If that was a wrapper script rather than the\n"
              "  renderer itself, the real render used more -- possibly far more.")

    total = sysinfo.total_ram_bytes()
    measured = [e for e in entries if e.peak_rss]
    peak_entry = max(measured, key=lambda e: e.peak_rss) if measured else None
    if total and peak_entry:
        scope = ("whole process tree" if peak_entry.peak_rss_is_tree
                 else "main process only")
        print(f"\nWorst recorded peak {_human_bytes(peak_entry.peak_rss)} of "
              f"{_human_bytes(total)} installed "
              f"({100.0 * peak_entry.peak_rss / total:.0f}% of this machine's "
              f"RAM, {scope}).")
    return 0


def cmd_inspect(args) -> int:
    manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                  export_usd=args.export_usd,
                                  usd_dir=args.usd_dir, flatten=args.flatten,
                                  frame=args.frame, footprint=args.footprint)
    if args.json:
        print(manifest.to_json())
        return 0

    print(f"{manifest.hip_path}   Houdini {manifest.houdini_version}   {manifest.fps} fps")
    if manifest.inspected_frame is not None:
        print(f"  described at frame {manifest.inspected_frame:g} "
              f"(a stage can differ at another frame; --frame to pick one)")
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

    cookable = [t for t in manifest.tasks if t.kind != TASK_RENDER]
    if cookable:
        print(f"\n  {len(cookable)} cookable task(s) — 'hsl cook' runs these:")
        for task in cookable:
            frames = (f"{task.frame_start}-{task.frame_end}"
                      if task.use_frame_range else str(task.frame_start))
            flags = "  sequential" if task.sequential else ""
            print(f"    {task.node_path}  [{task.kind}]  frames {frames}{flags}")
            if task.outputs:
                print(f"      -> {task.outputs[0]}")
            if task.depends_on:
                print(f"      after: {', '.join(task.depends_on)}")

    if manifest.missing_assets:
        print(f"\n  {len(manifest.missing_assets)} missing asset(s) — render will fail without a relink:")
        for asset in manifest.missing_assets:
            print(f"    [x] {asset.asset_path}  (at {asset.attr_path})")

    if manifest.live_volumes:
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        print(f"\n  {len(manifest.live_volumes)} live volume(s), {total_fields} field(s) "
              f"with no on-disk VDB — a husk USD export will bake ~GB/frame:")
        for v in manifest.live_volumes:
            print(f"    [!] {v.prim_path}  ({', '.join(v.field_names)})")
        print("    -> render with --engine hython (no export) or cache the volumes to .vdb.")

    if manifest.footprint:
        fp = manifest.footprint

        def _count(value: Optional[int]) -> str:
            # A count is a fact, not a byte figure -- sysinfo.human_bytes
            # would fold None and 0 together, which is exactly the mistake
            # SceneFootprint's own docstring warns against.
            return "unknown" if value is None else str(value)

        heaviest = fp.heaviest or "unknown"
        print(f"\n  scene footprint -- heaviest: {heaviest}  "
              f"(scan cost {fp.seconds:.1f}s)")
        print(f"    volumes      {fp.volume_count} volume(s), "
              f"{_count(fp.active_voxels)} active voxel(s), "
              f"{sysinfo.human_bytes(fp.voxel_bytes)} voxel data, "
              f"uncompressed, no renderer overhead")
        print(f"    textures     {fp.texture_count} texture(s), "
              f"{sysinfo.human_bytes(fp.texture_bytes)} on disk, not in memory")
        print(f"    geometry     {_count(fp.point_count)} point(s), "
              f"{_count(fp.prim_count)} prim(s), "
              f"{_count(fp.instance_count)} point-instancer instance(s)")
        print(f"    framebuffer  {sysinfo.human_bytes(fp.framebuffer_bytes)} exact "
              f"(width x height x channels x bytes-per-channel, summed over products)")
        if fp.skipped:
            print("    skipped (not counted above):")
            for item in fp.skipped:
                print(f"      - {item}")

    for warning in manifest.warnings:
        print(f"\n  warning: {warning}")
    return 0


def _select_tasks(manifest: SceneManifest, paths, kinds):
    """(tasks to run, node paths that matched nothing)."""
    if paths:
        chosen, missing = [], []
        for path in paths:
            task = manifest.task(path)
            if task is None:
                missing.append(path)
            else:
                chosen.append(task)
        return chosen, missing
    return [t for t in manifest.tasks if t.kind in kinds], []


def _describe_plan(tasks, chunk: int) -> None:
    """Print what is about to be cooked, and why some of it will not split."""
    print(f"{len(tasks)} task(s) to cook:")
    for task in tasks:
        chunks = husk_mod.chunks_for_task(task, chunk)
        frames = (f"{task.frame_start}-{task.frame_end}"
                  if task.use_frame_range else str(task.frame_start))
        note = ""
        if task.sequential and chunk:
            # Say so rather than letting --chunk look respected.
            note = "  (sequential: one process, --chunk ignored)"
        elif len(chunks) > 1:
            note = f"  ({len(chunks)} chunks)"
        print(f"  {task.node_path}  [{task.kind}]  frames {frames}{note}")
        if task.depends_on:
            print(f"      after: {', '.join(task.depends_on)}")


def cmd_cook(args) -> int:
    """Cook caches, simulations and non-Solaris ROPs.

    Rendering has its own command; this one drives everything else a .hip can
    produce. Dependencies come from the scene, so tasks run in the right order
    without the caller sequencing them.
    """
    # Reuse a cached read of an unchanged .hip — a cook never exports USD, so
    # the reuse is always safe (render must re-read when it exports).
    manifest = None
    if not args.no_cache:
        manifest = bridge.load_cached(args.hip)
        if manifest is not None:
            sys.stderr.write(f"Using the cached scene read "
                             f"({bridge.cache_path_for(args.hip)}); "
                             f"--no-cache to read the scene again.\n")
    if manifest is None:
        manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                      export_usd=False)
        try:
            bridge.save_cached(manifest)
        except OSError:
            pass                       # a cache we cannot write is not an error

    # Rendering has its own command, so plain `hsl cook` means the other work.
    kinds = (args.kind,) if args.kind else (TASK_CACHE, TASK_SIM)
    tasks, missing = _select_tasks(manifest, args.task, kinds)

    for path in missing:
        sys.stderr.write(f"error: no task at {path}\n")
    if missing:
        known = ", ".join(t.node_path for t in manifest.tasks) or "none"
        sys.stderr.write(f"Tasks found in this scene: {known}\n")
        return 2

    if not tasks:
        sys.stderr.write(
            "No cookable tasks matched. This scene has "
            f"{len(manifest.tasks)} task(s); use --kind or --task to pick one, "
            "or 'hsl inspect' to list them.\n")
        return 2

    if args.output and len(tasks) > 1:
        sys.stderr.write(
            "--output sets one path, but this would cook "
            f"{len(tasks)} tasks. Narrow it with --task.\n")
        return 4

    if args.frames:
        start, end, inc = args.frames
        tasks = [replace(t, frame_start=start, frame_end=end, frame_inc=inc,
                         use_frame_range=True) for t in tasks]

    jobs = []
    for task in tasks:
        jobs += husk_mod.jobs_for_task(
            manifest, task, chunk_size=args.chunk,
            hython_exe=args.hython or None,
            output=args.output or None,
        )

    _describe_plan(tasks, args.chunk)

    # Preflight — render has had this at the CLI for a while; a cook can fill
    # a disk or write somewhere unwritable just as easily.
    checks = preflight.run_cook_preflight_checks(jobs, manifest)
    for check in checks:
        sys.stderr.write(f"[preflight {check.level}] {check.message}\n")
    errors = [c for c in checks if c.level == "error"]
    if errors and not args.dry_run and not args.skip_preflight:
        sys.stderr.write(
            f"\n{len(errors)} preflight error(s) — nothing cooked. Fix them, or "
            f"re-run with --skip-preflight to cook anyway.\n")
        return 5

    if args.dry_run:
        print()
        for job in jobs:
            print(husk_mod.format_command(husk_mod.build_command(job)))
        return 0

    lock = threading.Lock()

    def on_event(event, *payload):
        if event == "task_output":
            task, line = payload
            with lock:
                sys.stdout.write(f"[{task.job.task_id} {task.job.chunk}] {line}\n")
                sys.stdout.flush()
        elif event == "task_finished":
            task = payload[0]
            with lock:
                sys.stderr.write(f"[{task.job.task_id} {task.job.chunk}] "
                                 f"{task.state.value} in {task.duration:.1f}s\n")

    try:
        queue = RenderQueue(jobs, max_parallel=args.parallel, on_event=on_event)
    except ValueError as exc:
        # A dependency cycle in the scene -- nothing can be scheduled.
        sys.stderr.write(f"error: {exc}\n")
        return 4

    queue.start(block=True)

    for warning in queue.warnings:
        sys.stderr.write(f"warning: {warning}\n")

    failed = [t for t in queue.tasks if t.state is State.FAILED]
    skipped = [t for t in queue.tasks if t.state is State.SKIPPED]
    if failed or skipped:
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed")
        if skipped:
            parts.append(f"{len(skipped)} skipped (a dependency did not finish)")
        sys.stderr.write(f"\n{', '.join(parts)}, of {len(queue.tasks)} chunk(s).\n")
        return 1
    sys.stderr.write(f"\nCooked {len(queue.tasks)} chunk(s).\n")
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


def _render_jobs_for_rop(args, manifest: SceneManifest, rop: RenderRop,
                         engine: str, settings_overrides, keep, tag_task: bool):
    """Overlays and chunks for one render ROP: ``(jobs, None)`` or ``(None, code)``.

    Everything here is genuinely per-ROP — the husk overlay chain edits *this*
    ROP's exported USD and the frame override rewrites *this* ROP's range —
    which is what lets a multi-ROP render just call it once per ROP.
    """
    if engine == "husk" and not rop.usd_path:
        prefix = f"{rop.node_path}: " if tag_task else ""
        sys.stderr.write(f"{prefix}The stage could not be written to USD; "
                         f"nothing to render.\n")
        for warning in manifest.warnings:
            sys.stderr.write(f"  {warning}\n")
        return None, 3

    if args.frames:
        start, end, inc = args.frames
        rop.frame_start, rop.frame_end, rop.frame_inc = start, end, inc
        rop.use_frame_range = end != start

    base_usd = rop.usd_path
    if engine == "husk" and getattr(args, "relink_from", None) and not args.dry_run:
        try:
            result = bridge.relink_assets(base_usd, args.relink_from, hython=args.hython)
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return None, 3
        if result.get("usd_out"):
            base_usd = result["usd_out"]
            sys.stderr.write(f"Relinked {len(result['relinked'])} asset(s); "
                             f"{len(result['still_missing'])} still missing.\n")
            # Drop what the relink resolved, so preflight judges the USD that
            # is actually about to render — not the state before the fix.
            resolved = {entry.get("old") for entry in result.get("relinked", [])}
            manifest.missing_assets = [a for a in manifest.missing_assets
                                       if a.asset_path not in resolved]

    # AOV selection is a USD edit, not a husk flag: keep only the requested
    # RenderVars by rendering an overlay produced by bridge.filter_aovs.
    usd_for_render = base_usd
    if keep is not None and not args.dry_run:
        try:
            usd_for_render = bridge.filter_aovs(base_usd, keep, hython=args.hython)
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return None, 3

    # Karma knobs are USD attributes, not husk flags. On husk they go into an
    # overlay now; on hython render_direct sublayers them at render start.
    if engine == "husk" and settings_overrides and not args.dry_run:
        try:
            result = bridge.override_settings(usd_for_render, settings_overrides,
                                              hython=args.hython,
                                              settings_prim=args.settings or "")
        except bridge.InspectError as exc:
            sys.stderr.write(f"{exc}\n")
            return None, 3
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
                return None, 3
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
        # Group this ROP's chunks under its own task id so a multi-ROP queue
        # reports per ROP; a single-ROP render keeps today's untagged output.
        task_id=rop.node_path if tag_task else None,
    )
    return jobs, None


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
            "  -> add --engine husk to use it, or drop it to render with hython.\n")
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

    # Narrow the export to one ROP only when exactly one was asked for — a
    # multi-ROP render needs every selected stage written.
    rop_filter = (args.rop[0]
                  if len(args.rop) == 1 and not getattr(args, "all_rops", False)
                  else "")
    if manifest is None:
        manifest = bridge.inspect_hip(args.hip, hython=args.hython,
                                      export_usd=export_usd, usd_dir=args.usd_dir,
                                      flatten=args.flatten, rop=rop_filter,
                                      allow_volume_bake=allow_volume_bake,
                                      # Export only the frames asked for, not
                                      # the ROP's whole authored range.
                                      export_frames=args.frames if export_usd else None)
        if not export_usd:
            try:
                bridge.save_cached(manifest)
            except OSError:
                pass                       # a cache we cannot write is not an error
    rops = _pick_rops(manifest, args.rop, getattr(args, "all_rops", False))
    if not rops:
        return 2

    if args.output and len(rops) > 1:
        sys.stderr.write(
            f"--output sets one path, but {len(rops)} ROPs are being rendered "
            f"— each would overwrite the others'. Narrow it with --rop, or "
            f"leave the outputs to the scene.\n")
        return 4

    # Live volumes bake tens of GB/frame into a husk USD export. Unless the user
    # opted in with --allow-volume-bake, the inspector skipped the export — so
    # explain why and how to proceed instead of the generic failure below.
    if engine == "husk" and manifest.live_volumes and not allow_volume_bake:
        n = len(manifest.live_volumes)
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        sys.stderr.write(
            f"Aborted: {n} live volume(s) ({total_fields} field(s) with no "
            f"on-disk VDB) would bake tens of GB per frame into the USD export.\n"
            f"  -> render with --engine hython (no export, recommended for this shot),\n"
            f"  -> or cache the volumes to .vdb and re-read,\n"
            f"  -> or pass --allow-volume-bake to export anyway.\n")
        for v in manifest.live_volumes:
            sys.stderr.write(f"    [!] {v.prim_path}  ({', '.join(v.field_names)})\n")
        return 3

    if engine == "husk" and manifest.live_volumes and allow_volume_bake:
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        sys.stderr.write(
            f"--allow-volume-bake: exporting {len(manifest.live_volumes)} live "
            f"volume(s), {total_fields} field(s) — expect tens of GB per frame.\n")

    # Missing textures fail the render mid-flight — surface them for *both*
    # engines (they are a property of the scene, not of one ROP), and relink
    # if asked (per exported USD, inside the per-ROP build below).
    if manifest.missing_assets:
        sys.stderr.write(f"{len(manifest.missing_assets)} unresolved asset(s) in the scene:\n")
        for asset in manifest.missing_assets:
            sys.stderr.write(f"  [x] {asset.asset_path}\n")
        if engine == "hython" and not getattr(args, "relink_from", None):
            sys.stderr.write(
                "  (pass --relink-from DIR to repath them, or fix the scene.)\n")

    # AOV selection resolves once against the manifest; the overlay itself is
    # authored per exported USD in the per-ROP build.
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

    # Build jobs ROP by ROP, in the order asked for. The queue starts chunks
    # in submission order, so at --parallel 1 this renders one ROP after the
    # other — and a failed ROP does not stop the ones behind it.
    per_rop = []
    for rop in rops:
        jobs, err = _render_jobs_for_rop(args, manifest, rop, engine,
                                         settings_overrides, keep,
                                         tag_task=len(rops) > 1)
        if err is not None:
            return err
        per_rop.append((rop, jobs))
    all_jobs = [job for _, jobs in per_rop for job in jobs]

    # Preflight — judged on each ROP's first chunk, with scene-level findings
    # deduplicated so N ROPs do not repeat every missing texture N times.
    seen_checks = set()
    checks = []
    for _, jobs in per_rop:
        for check in preflight.run_preflight_checks(jobs[0], manifest):
            key = (check.level, check.message)
            if key not in seen_checks:
                seen_checks.add(key)
                checks.append(check)
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
                  f"(overlay USD authored at render time)")

        for rop, jobs in per_rop:
            if len(per_rop) > 1:
                print(f"# --- {rop.node_path} ---")
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

        for job in all_jobs:
            print(husk_mod.format_command(husk_mod.build_command(job)))
        return 0

    return _run_queue(all_jobs, args.parallel, "Rendered")


def _rop_spec_matches(spec: str, hip_argv_path: str, rop_node_path: str) -> bool:
    """True when a ``--rop`` SPEC selects ``rop_node_path`` in the scene read
    from ``hip_argv_path``.

    A SPEC is either a bare node path or scene-qualified as ``scene:path``,
    split at the LAST colon -- so a Windows drive letter is never mistaken
    for the scene/ROP separator. It counts as qualified only when the text
    after that colon starts with "/" and the text before it is non-empty
    (node paths never contain ":", so this is unambiguous); otherwise the
    whole SPEC is the bare node path.

    A bare SPEC applies to every scene. A qualified SPEC applies only when
    its qualifier equals the scene's argv path, its basename, or its
    basename without the .hip/.hiplc/.hipnc extension -- case-insensitively,
    since this is Windows.
    """
    qualifier, sep, tail = spec.rpartition(":")
    if sep and qualifier and tail.startswith("/"):
        node_path = tail
    else:
        qualifier, node_path = "", spec

    if node_path != rop_node_path:
        return False
    if not qualifier:
        return True

    basename = os.path.basename(hip_argv_path)
    stem = basename
    lowered = basename.lower()
    for ext in (".hip", ".hiplc", ".hipnc"):
        if lowered.endswith(ext):
            stem = basename[:-len(ext)]
            break
    candidates = (hip_argv_path, basename, stem)
    return any(os.path.normcase(qualifier) == os.path.normcase(c) for c in candidates)


def cmd_batch(args) -> int:
    """Render several .hip files, one scene after another.

    Deliberately plainer than ``hsl render``: engine, frames, chunking and
    parallelism apply to every scene, and the per-render editing options
    (--aovs, --set, --output, --relink-from) do not exist here — run those as
    individual renders. Every scene is read before anything renders, so a bad
    path in scene 3 surfaces before scenes 1 and 2 spend hours rendering.

    ``--rop`` narrows which ROPs render, per scene (see _rop_spec_matches).
    With none given, every render ROP of every scene runs, as before.
    """
    export_usd = args.engine == "husk"

    plans = []
    for hip in args.hips:
        manifest = None
        if not export_usd and not args.no_cache:
            manifest = bridge.load_cached(hip)
            if manifest is not None:
                sys.stderr.write(f"{hip}: using the cached scene read; "
                                 f"--no-cache to read it again.\n")
        if manifest is None:
            try:
                manifest = bridge.inspect_hip(hip, hython=args.hython,
                                              export_usd=export_usd,
                                              allow_volume_bake=False,
                                              # The export now genuinely
                                              # narrows to the frames asked
                                              # for (UNVERIFIED C8); without
                                              # this, husk would be sent
                                              # frames the USD does not carry.
                                              export_frames=(args.frames
                                                             if export_usd
                                                             else None))
            except bridge.InspectError as exc:
                sys.stderr.write(f"{hip}: {exc}\n")
                return 2
            if not export_usd:
                try:
                    bridge.save_cached(manifest)
                except OSError:
                    pass               # a cache we cannot write is not an error
        if export_usd and manifest.live_volumes:
            sys.stderr.write(
                f"{hip}: {len(manifest.live_volumes)} live volume(s) would "
                f"bake into the USD export — render this scene with "
                f"--engine hython, or individually with "
                f"'hsl render --allow-volume-bake'.\n")
            return 3
        if not manifest.rops:
            sys.stderr.write(f"{hip}: no USD Render ROPs found.\n")
            return 2
        plans.append((hip, manifest))

    if args.rop:
        # Two passes: first work out what each scene would keep and which
        # SPECs fired anywhere, without touching the manifests yet -- so an
        # unmatched SPEC (a typo, most likely) can be reported on its own
        # even when it also happens to leave some scene empty. Reported
        # second, "scene left with nothing" then means what it says: every
        # SPEC was valid somewhere, this scene just has none of them.
        matched_any = [False] * len(args.rop)
        per_scene_kept = []
        for hip, manifest in plans:
            kept = []
            for rop in manifest.rops:
                for index, spec in enumerate(args.rop):
                    if _rop_spec_matches(spec, hip, rop.node_path):
                        matched_any[index] = True
                        kept.append(rop)
                        break
            per_scene_kept.append((hip, manifest, kept))

        missing = [spec for spec, hit in zip(args.rop, matched_any) if not hit]
        if missing:
            for spec in missing:
                sys.stderr.write(
                    f"--rop {spec} matched no ROP in any scene. Check the "
                    f"path, and, if it is scene-qualified, that the "
                    f"qualifier matches a .hip being rendered.\n")
            return 2

        empty = [(hip, manifest) for hip, manifest, kept in per_scene_kept if not kept]
        if empty:
            for hip, manifest in empty:
                names = "\n  ".join(r.node_path for r in manifest.rops)
                sys.stderr.write(
                    f"{hip}: no --rop matched a ROP in this scene, so nothing "
                    f"would render from it. Scene contains:\n  {names}\n"
                    f"  -> add a --rop for one of these, or drop --rop to "
                    f"render them all.\n")
            return 2

        for hip, manifest, kept in per_scene_kept:
            manifest.rops = kept

    per_rop, all_jobs = [], []
    for hip, manifest in plans:
        for rop in manifest.rops:
            if export_usd and not rop.usd_path:
                sys.stderr.write(f"{hip}: {rop.node_path}: the stage could not "
                                 f"be written to USD; nothing to render.\n")
                return 3
            if args.frames:
                start, end, inc = args.frames
                rop.frame_start, rop.frame_end, rop.frame_inc = start, end, inc
                rop.use_frame_range = end != start
            jobs = husk_mod.jobs_for_rop(
                manifest, rop, rop.usd_path,
                chunk_size=args.chunk, engine=args.engine,
                # Node paths repeat across scenes; the hip name keeps queue
                # reporting unambiguous.
                task_id=f"{os.path.basename(hip)}:{rop.node_path}",
            )
            per_rop.append((manifest, jobs))
            all_jobs += jobs

    sys.stderr.write(f"{len(plans)} scene(s), "
                     f"{sum(len(m.rops) for _, m in plans)} ROP(s), "
                     f"{len(all_jobs)} chunk(s).\n")

    # Preflight per ROP, deduplicated — same shape as render's.
    seen_checks = set()
    checks = []
    for manifest, jobs in per_rop:
        for check in preflight.run_preflight_checks(jobs[0], manifest):
            key = (check.level, check.message)
            if key not in seen_checks:
                seen_checks.add(key)
                checks.append(check)
    for check in checks:
        sys.stderr.write(f"[preflight {check.level}] {check.message}\n")
    errors = [c for c in checks if c.level == "error"]
    if errors and not args.dry_run and not args.skip_preflight:
        sys.stderr.write(
            f"\n{len(errors)} preflight error(s) — nothing rendered. Fix them, or "
            f"re-run with --skip-preflight to render anyway.\n")
        return 5

    if args.dry_run:
        current = None
        for job in all_jobs:
            if job.hip_file != current:
                current = job.hip_file
                print(f"# --- {current} ---")
            print(husk_mod.format_command(husk_mod.build_command(job)))
        return 0

    return _run_queue(all_jobs, args.parallel, "Rendered")


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
    p_inspect.add_argument("--frame", type=float, default=None,
                           help="Describe the stage at this frame instead of "
                                "the scene's current one")
    p_inspect.add_argument("--export-usd", action="store_true",
                           help="Also write the USD to disk")
    p_inspect.add_argument("--footprint", action="store_true",
                           help="Scan volumes, textures and geometry for what "
                                "the scene contains (costs scan time on a "
                                "heavy stage; reports scene contents, not a "
                                "memory requirement)")
    p_inspect.set_defaults(func=cmd_inspect)

    p_render = sub.add_parser("render", parents=[common], help="Render with husk or hython")
    p_render.add_argument("--engine", choices=["husk", "hython"],
                          default=husk_mod.DEFAULT_ENGINE,
                          help="Render engine: hython renders the ROP directly with no "
                               "USD export (default); husk exports USD first, which is "
                               "what --aovs, --relink-from and farm submission need")
    p_render.add_argument("--direct-hython", action="store_true",
                          help="Shortcut for --engine hython (0 USD disk space)")
    p_render.add_argument("--rop", action="append", default=[], metavar="PATH",
                          help="ROP node path (repeatable: several ROPs render "
                               "in order, one after another at --parallel 1)")
    p_render.add_argument("--all-rops", action="store_true",
                          help="Render every render ROP in the scene, in the "
                               "order found")
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

    p_cook = sub.add_parser(
        "cook", parents=[common],
        help="Cook caches, simulations and non-Solaris ROPs")
    p_cook.add_argument("--task", action="append", default=[], metavar="PATH",
                        help="Node path to cook (repeatable). Default: every "
                             "cache and simulation in the scene")
    p_cook.add_argument("--kind", choices=[TASK_CACHE, TASK_SIM, TASK_RENDER],
                        default="",
                        help="Cook only tasks of this kind")
    p_cook.add_argument("--frames", type=parse_frames, default=None,
                        help="Override the range: 1001, 1001-1100 or 1001-1100x2")
    p_cook.add_argument("--chunk", type=int, default=0,
                        help="Frames per process (0 = one). Ignored for "
                             "sequential tasks, which cannot be split")
    p_cook.add_argument("--parallel", type=int, default=1,
                        help="Concurrent processes")
    p_cook.add_argument("--output", default="",
                        help="Override the output path (one task only)")
    p_cook.add_argument("--no-cache", action="store_true",
                        help="Always re-read the scene instead of reusing a "
                             "cached read of an unchanged .hip")
    p_cook.add_argument("--skip-preflight", action="store_true",
                        help="Cook even if preflight reports errors (unwritable "
                             "output path, bad frame range, ...)")
    p_cook.add_argument("--dry-run", action="store_true",
                        help="Print the commands without running them")
    p_cook.set_defaults(func=cmd_cook)

    p_batch = sub.add_parser(
        "batch", help="Render several .hip files one after another")
    p_batch.add_argument("hips", nargs="+", metavar="hip",
                         help="Scenes to render, in order")
    p_batch.add_argument("--engine", choices=["husk", "hython"],
                         default=husk_mod.DEFAULT_ENGINE,
                         help="Render engine for every scene (see 'render')")
    p_batch.add_argument("--rop", action="append", default=[], metavar="SPEC",
                         help="Render only this ROP (repeatable). SPEC is a "
                              "bare node path (applies to every scene) or "
                              "scene-qualified as scene.hip:/path (applies "
                              "only to that scene, matched by argv path, "
                              "basename, or basename without extension). "
                              "Default: every render ROP of every scene")
    p_batch.add_argument("--frames", type=parse_frames, default=None,
                         help="Override every ROP's range: 1001, 1001-1100 "
                              "or 1001-1100x2")
    p_batch.add_argument("--chunk", type=int, default=0,
                         help="Frames per process (0 = one process)")
    p_batch.add_argument("--parallel", type=int, default=1,
                         help="Concurrent processes")
    p_batch.add_argument("--no-cache", action="store_true",
                         help="Always re-read each scene instead of reusing a "
                              "cached read of an unchanged .hip")
    p_batch.add_argument("--skip-preflight", action="store_true",
                         help="Render even if preflight reports errors")
    p_batch.add_argument("--dry-run", action="store_true",
                         help="Print the render commands and stop")
    p_batch.set_defaults(func=cmd_batch)

    p_memory = sub.add_parser(
        "memory",
        help="What past renders actually used, and what this machine can measure")
    p_memory.add_argument("hip", nargs="?", default="",
                          help="Show only this scene's measured renders")
    p_memory.add_argument("--machine", action="store_true",
                          help="Report what can be measured here (RAM, GPU tooling) "
                               "instead of the history")
    p_memory.add_argument("--forget", action="store_true",
                          help="Delete recorded measurements (all, or one scene's)")
    p_memory.add_argument("--limit", type=int, default=20,
                          help="How many records to print (default 20)")
    p_memory.set_defaults(func=cmd_memory)

    p_hython = sub.add_parser("hython",
                              help="List the Houdini/hython installs found on this machine")
    p_hython.add_argument("--set", default="", metavar="INDEX|PATH",
                          help="Remember this install as the default (by list index or path)")
    p_hython.set_defaults(func=cmd_hython)

    p_ui = sub.add_parser("ui", help="Open the launcher window")
    p_ui.add_argument("hip", nargs="?", default="")
    p_ui.set_defaults(func=cmd_ui)

    return parser


def _make_console_safe() -> None:
    """Stop an unencodable character turning a report into a traceback.

    cp1252 is still the default code page on a fresh Windows console and has
    no mapping for a lot of punctuation. With the default 'strict' handler,
    printing one raises ``UnicodeEncodeError`` *part way through* a report --
    so the user loses the output and gets a stack trace about a decoration.

    Nothing hsl prints is currently unmappable (there is a test), but a report
    is the wrong place to be strict about typography.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass        # not a reconfigurable stream (redirected, or 3.6)


def main(argv=None) -> int:
    _make_console_safe()
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
