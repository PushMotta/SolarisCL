"""Scene manifest schema.

This module is the contract between the two halves of the tool:

  * ``hsl.inspector`` runs under **hython**, opens a .hip, cooks the LOP
    network and emits one of these as JSON.
  * ``hsl.husk`` / ``hsl.ui`` run under **plain Python** and consume it.

It must therefore import nothing beyond the standard library -- no ``hou``,
no ``pxr``, no Qt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from typing import Any, Optional

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# USD-side description (read from the composed stage)
# --------------------------------------------------------------------------

@dataclass
class RenderVar:
    """A UsdRenderVar -- one AOV."""
    prim_path: str
    source_name: str = ""
    source_type: str = ""        # "raw", "primvar", "lpe", ...
    data_type: str = ""          # "color3f", "float", ...
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.source_name or self.prim_path.rsplit("/", 1)[-1]


@dataclass
class RenderProduct:
    """A UsdRenderProduct -- one output file."""
    prim_path: str
    product_name: str = ""       # the output path husk will write
    product_type: str = "raster"
    ordered_vars: list[str] = field(default_factory=list)   # RenderVar prim paths


@dataclass
class RenderSettings:
    """A UsdRenderSettings prim."""
    prim_path: str
    resolution: Optional[tuple[int, int]] = None
    pixel_aspect_ratio: Optional[float] = None
    aspect_ratio_conform_policy: str = ""
    camera: str = ""                                   # camera prim path
    products: list[str] = field(default_factory=list)  # RenderProduct prim paths
    included_purposes: list[str] = field(default_factory=list)
    instantaneous_shutter: Optional[bool] = None
    # Delegate-specific knobs: karma:*, ri:*, arnold:*, driver:* etc.
    renderer_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Camera:
    """A UsdGeomCamera."""
    prim_path: str
    focal_length: Optional[float] = None
    horizontal_aperture: Optional[float] = None
    vertical_aperture: Optional[float] = None
    near_clip: Optional[float] = None
    far_clip: Optional[float] = None
    f_stop: Optional[float] = None
    focus_distance: Optional[float] = None
    shutter_open: Optional[float] = None
    shutter_close: Optional[float] = None

    @property
    def label(self) -> str:
        return self.prim_path.rsplit("/", 1)[-1]


@dataclass
class MissingAsset:
    """An asset-path attribute on the stage that does not resolve on disk.

    ``attr_path`` is the exact attribute holding the reference (e.g.
    ``/mat/tex.inputs:file``), which is what relinking needs to repath it.
    """
    attr_path: str
    asset_path: str          # the authored, unresolved path
    kind: str = "texture"    # texture / reference / volume / ...

    @property
    def basename(self) -> str:
        return self.asset_path.replace("\\", "/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# Houdini-side description (read from node parameters)
# --------------------------------------------------------------------------

@dataclass
class RenderRop:
    """A USD Render ROP (or equivalent) found in the .hip."""
    node_path: str
    node_type: str
    input_lop: str = ""                 # the LOP whose stage feeds this ROP
    renderer: str = ""                  # e.g. BRAY_HdKarmaXPU
    settings_prim: str = ""             # RenderSettings prim path, may be empty
    camera: str = ""
    frame_start: int = 1
    frame_end: int = 1
    frame_inc: int = 1
    use_frame_range: bool = False       # False => single (current) frame
    output_override: str = ""           # ROP-level output path override
    usd_path: str = ""                  # populated once the stage is exported
    # Everything the inspector managed to read, for debugging / display.
    raw_parms: dict[str, Any] = field(default_factory=dict)

    @property
    def frame_count(self) -> int:
        if not self.use_frame_range:
            return 1
        inc = self.frame_inc or 1
        return max(1, (self.frame_end - self.frame_start) // inc + 1)

    @property
    def label(self) -> str:
        return self.node_path


@dataclass
class SceneManifest:
    """Everything the launcher needs to know about one .hip file."""
    schema_version: int = SCHEMA_VERSION
    hip_path: str = ""
    houdini_version: str = ""
    fps: float = 24.0
    stage_start_time_code: Optional[float] = None
    stage_end_time_code: Optional[float] = None
    default_settings_prim: str = ""     # stage metadata 'renderSettingsPrimPath'
    rops: list[RenderRop] = field(default_factory=list)
    settings: list[RenderSettings] = field(default_factory=list)
    products: list[RenderProduct] = field(default_factory=list)
    vars: list[RenderVar] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    missing_assets: list[MissingAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------

    def rop(self, node_path: str) -> Optional[RenderRop]:
        return next((r for r in self.rops if r.node_path == node_path), None)

    def settings_for(self, prim_path: str) -> Optional[RenderSettings]:
        return next((s for s in self.settings if s.prim_path == prim_path), None)

    def product(self, prim_path: str) -> Optional[RenderProduct]:
        return next((p for p in self.products if p.prim_path == prim_path), None)

    def var(self, prim_path: str) -> Optional[RenderVar]:
        return next((v for v in self.vars if v.prim_path == prim_path), None)

    def resolve_settings(self, rop: RenderRop) -> Optional[RenderSettings]:
        """Best guess at which RenderSettings prim a ROP will actually use."""
        for candidate in (rop.settings_prim, self.default_settings_prim):
            if candidate:
                found = self.settings_for(candidate)
                if found:
                    return found
        return self.settings[0] if len(self.settings) == 1 else None

    def outputs_for(self, settings: RenderSettings) -> list[str]:
        """Output file paths declared by a settings prim's products."""
        paths = []
        for prim_path in settings.products:
            product = self.product(prim_path)
            if product and product.product_name:
                paths.append(product.product_name)
        return paths

    def aovs_for(self, settings: RenderSettings) -> list[RenderVar]:
        seen, out = set(), []
        for prim_path in settings.products:
            product = self.product(prim_path)
            if not product:
                continue
            for var_path in product.ordered_vars:
                if var_path in seen:
                    continue
                seen.add(var_path)
                found = self.var(var_path)
                if found:
                    out.append(found)
        return out

    # -- serialisation ----------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=_fallback)

    @classmethod
    def from_json(cls, text: str) -> "SceneManifest":
        return _build(cls, json.loads(text))


def _fallback(obj: Any) -> Any:
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


# Field name -> element dataclass. Name-based lookup rather than parsing type
# annotations, which arrive as strings under ``from __future__ import
# annotations``.
_ELEMENT_TYPES = {
    "rops": RenderRop,
    "settings": RenderSettings,
    "products": RenderProduct,
    "vars": RenderVar,
    "cameras": Camera,
    "missing_assets": MissingAsset,
}


def _build(cls, data: Any):
    """Rebuild nested dataclasses from plain dicts."""
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        inner = _ELEMENT_TYPES.get(f.name)
        if inner is not None and isinstance(value, list):
            value = [_build(inner, v) for v in value]
        elif f.name == "resolution" and isinstance(value, list):
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)
