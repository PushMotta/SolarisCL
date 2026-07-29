"""Preflight health checks for Solaris render jobs.

Performs sanity and environment checks before launching hython or husk processes:
  * Frame range validity
  * Resolution bounds (Indie 1080p limit)
  * Output directory existence and write permissions
  * Available disk space
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

from .husk import RenderJob, has_frame_token
from .manifest import SceneManifest


@dataclass
class PreflightWarning:
    level: str      # "warning", "error", "info"
    category: str   # "frame_range", "resolution", "output_path", "disk_space"
    message: str


def run_preflight_checks(job: RenderJob, manifest: Optional[SceneManifest] = None) -> list[PreflightWarning]:
    """Run all preflight checks on a RenderJob and optional SceneManifest."""
    warnings: list[PreflightWarning] = []

    # 1. Frame range check
    if job.chunk.start < 0 or job.chunk.count <= 0 or job.chunk.inc <= 0:
        warnings.append(PreflightWarning(
            level="error",
            category="frame_range",
            message=f"Invalid frame range configuration: start={job.chunk.start}, count={job.chunk.count}, inc={job.chunk.inc}"
        ))

    # 2. Resolution check (Indie 1080p limit check)
    if job.resolution:
        w, h = job.resolution
        if w > 1920 or h > 1080:
            warnings.append(PreflightWarning(
                level="warning",
                category="resolution",
                message=f"Resolution ({w}x{h}) exceeds 1920x1080. If running Houdini Indie, husk will cap render output to 1080p."
            ))

    # 3. Output path check
    if job.output:
        out_dir = os.path.dirname(os.path.abspath(job.output))
        if out_dir and not os.path.exists(out_dir):
            warnings.append(PreflightWarning(
                level="warning",
                category="output_path",
                message=f"Output directory does not exist yet: {out_dir}. Husk will create it if write permissions allow."
            ))
        elif out_dir and not os.access(out_dir, os.W_OK):
            warnings.append(PreflightWarning(
                level="error",
                category="output_path",
                message=f"No write permission for output directory: {out_dir}"
            ))

        # 3b. An output with no frame token means every frame of a sequence
        #     writes to the same file and only the last one survives. Easy to do
        #     by accident by picking a filename in a save dialog.
        if job.chunk.count > 1 and not has_frame_token(job.output):
            warnings.append(PreflightWarning(
                level="warning",
                category="output_path",
                message=(f"{job.output} has no frame token ($F4, %04d, <F4>, …) "
                         f"but {job.chunk.count} frames are being rendered — every "
                         f"frame would overwrite the same file. Add a token, or "
                         f"point --output at a folder to keep each product's own "
                         f"name.")
            ))

        # 4. Disk space check
        if out_dir and os.path.exists(out_dir):
            try:
                usage = shutil.disk_usage(out_dir)
                free_gb = usage.free / (1024 ** 3)
                if free_gb < 1.0:
                    warnings.append(PreflightWarning(
                        level="warning",
                        category="disk_space",
                        message=f"Low disk space on output drive ({free_gb:.2f} GB remaining)."
                    ))
            except OSError:
                pass

    # 5. Missing textures / unresolved assets — husk only reports these mid-render,
    #    so catching them here is the whole point. Normally a hard error.
    #
    #    Unless a relink is already scheduled: a hython job carries its search
    #    dirs and repaths inside the LOP network *at render time*, so the
    #    manifest still lists the assets as unresolved when this runs. Failing
    #    on them would refuse to start the very render that fixes them. Still
    #    reported — the relink may not find everything — but as a warning.
    if manifest and manifest.missing_assets:
        relink_scheduled = bool(getattr(job, "relink_dirs", None))
        for asset in manifest.missing_assets:
            message = f"Unresolved {asset.kind}: {asset.asset_path}  (at {asset.attr_path})"
            if relink_scheduled:
                message += "  — a relink will be attempted at render time."
            warnings.append(PreflightWarning(
                level="warning" if relink_scheduled else "error",
                category="missing_asset",
                message=message,
            ))

    # 5b. Live (SOP-imported) volumes bake tens of GB/frame into a USD export.
    #     Only the husk engine exports; the hython-direct engine renders the
    #     live data with no export, so this is a husk-only hazard.
    if (manifest and manifest.live_volumes
            and getattr(job, "engine", "husk") == "husk"):
        n = len(manifest.live_volumes)
        total_fields = sum(v.field_count for v in manifest.live_volumes)
        fields = ", ".join(sorted({f for v in manifest.live_volumes
                                   for f in v.field_names}))
        detail = f" ({fields})" if fields else ""
        warnings.append(PreflightWarning(
            level="warning",
            category="volume_bake",
            message=(
                f"{n} live volume(s), {total_fields} field(s){detail} have no "
                f"on-disk VDB (OpenVDBAsset.filePath empty) and will bake into "
                f"the USD export -- tens of GB per frame. Render with the hython "
                f"engine (no export) or point the volumes at a .vdb cache."
            ),
        ))

    # 6. Attach any manifest scene warnings
    if manifest and manifest.warnings:
        for msg in manifest.warnings:
            warnings.append(PreflightWarning(
                level="info",
                category="scene",
                message=msg
            ))

    return warnings


def run_cook_preflight_checks(jobs, manifest: Optional[SceneManifest] = None) -> list[PreflightWarning]:
    """Preflight for a cook plan (``hsl cook``).

    Differs from the render preflight deliberately:

      * It judges the **first chunk of every task**, not only ``jobs[0]`` -- a
        cook plan mixes unrelated nodes writing to unrelated places, so one
        job says nothing about the rest.
      * With no ``--output`` override, destinations come from the job's
        ``expected_outputs`` -- what the node itself says it writes.
      * A missing asset is a **warning**, not an error. A geometry cache or a
        sim does not necessarily read the textures a render does, and blocking
        the cook on them would refuse work that may be fine. Still reported,
        so nothing fails silently later.
      * No resolution or volume-bake checks: a cook renders nothing and never
        exports USD.
    """
    warnings: list[PreflightWarning] = []
    seen_tasks: set = set()
    seen_dirs: set = set()

    for job in jobs:
        task_id = getattr(job, "task_id", "") or job.rop_path
        if task_id in seen_tasks:
            continue
        seen_tasks.add(task_id)

        if job.chunk.start < 0 or job.chunk.count <= 0 or job.chunk.inc <= 0:
            warnings.append(PreflightWarning(
                level="error",
                category="frame_range",
                message=(f"{task_id}: invalid frame range configuration: "
                         f"start={job.chunk.start}, count={job.chunk.count}, "
                         f"inc={job.chunk.inc}")))

        # An explicit override with no frame token collapses a sequence onto
        # one file. Node-declared outputs keep their own tokens, so only the
        # override needs this check.
        if job.output and job.chunk.count > 1 and not has_frame_token(job.output):
            warnings.append(PreflightWarning(
                level="warning",
                category="output_path",
                message=(f"{job.output} has no frame token ($F4, %04d, <F4>, ...) "
                         f"but {job.chunk.count} frames are being cooked -- every "
                         f"frame would overwrite the same file.")))

        paths = [job.output] if job.output else list(job.expected_outputs or [])
        for raw in paths:
            out_dir = os.path.dirname(os.path.abspath(raw))
            if not out_dir or out_dir in seen_dirs:
                continue
            seen_dirs.add(out_dir)
            if not os.path.exists(out_dir):
                warnings.append(PreflightWarning(
                    level="warning",
                    category="output_path",
                    message=(f"Output directory does not exist yet: {out_dir}. "
                             f"It will be created if write permissions allow.")))
                continue
            if not os.access(out_dir, os.W_OK):
                warnings.append(PreflightWarning(
                    level="error",
                    category="output_path",
                    message=f"No write permission for output directory: {out_dir}"))
            try:
                usage = shutil.disk_usage(out_dir)
                free_gb = usage.free / (1024 ** 3)
                if free_gb < 1.0:
                    warnings.append(PreflightWarning(
                        level="warning",
                        category="disk_space",
                        message=(f"Low disk space on output drive "
                                 f"({free_gb:.2f} GB remaining) for {out_dir}.")))
            except OSError:
                pass

    if manifest and manifest.missing_assets:
        for asset in manifest.missing_assets:
            warnings.append(PreflightWarning(
                level="warning",
                category="missing_asset",
                message=(f"Unresolved {asset.kind}: {asset.asset_path}  "
                         f"(at {asset.attr_path}) -- may not affect a cook; "
                         f"a render would refuse to start on this.")))

    if manifest and manifest.warnings:
        for msg in manifest.warnings:
            warnings.append(PreflightWarning(
                level="info", category="scene", message=msg))

    return warnings
