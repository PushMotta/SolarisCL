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

from .husk import RenderJob
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
    #    so catching them here is the whole point. Each is a hard error.
    if manifest and manifest.missing_assets:
        for asset in manifest.missing_assets:
            warnings.append(PreflightWarning(
                level="error",
                category="missing_asset",
                message=f"Unresolved {asset.kind}: {asset.asset_path}  (at {asset.attr_path})"
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
