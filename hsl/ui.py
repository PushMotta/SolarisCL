"""PySide6 launcher window.

Runs in ordinary Python -- it never imports ``hou``. Scene reading happens in
a worker thread that shells out to hython via :mod:`hsl.bridge`.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from . import bridge, farm, husk as husk_mod, preflight, presets, progress, resources
from .manifest import TASK_RENDER, RenderRop, SceneManifest
from .runner import RenderQueue, State, Task

MONO = "Menlo" if sys.platform == "darwin" else ("Consolas" if os.name == "nt" else "DejaVu Sans Mono")

# Windows groups taskbar buttons by this string and takes the button's icon
# from whichever app owns it. Left unset, we inherit the host interpreter's
# identity -- so the launcher shows python.exe's icon no matter what Qt is
# told. Any unique dotted string works; keep it stable or pinned shortcuts
# will detach.
APP_USER_MODEL_ID = "PushMotta.SolarisCL.Launcher"


# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------

class InspectWorker(QObject):
    finished = Signal(object)   # SceneManifest
    failed = Signal(str)

    def __init__(self, hip_path: str, export_usd: bool, flatten: bool,
                 hython: str = ""):
        super().__init__()
        self.hip_path = hip_path
        self.export_usd = export_usd
        self.flatten = flatten
        self.hython = hython

    @Slot()
    def run(self) -> None:
        try:
            manifest = bridge.inspect_hip(
                self.hip_path, hython=self.hython,
                export_usd=self.export_usd, flatten=self.flatten,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        try:
            bridge.save_cached(manifest)
        except OSError:
            pass
        self.finished.emit(manifest)


class FilterWorker(QObject):
    """Runs bridge.filter_aovs (a hython subprocess) off the Qt thread."""
    finished = Signal(str)      # path to the AOV-filtered overlay USD
    failed = Signal(str)

    def __init__(self, usd_in: str, keep_paths, hython: str = ""):
        super().__init__()
        self.usd_in = usd_in
        self.keep_paths = keep_paths
        self.hython = hython

    @Slot()
    def run(self) -> None:
        try:
            out = bridge.filter_aovs(self.usd_in, self.keep_paths, hython=self.hython)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(out)


class PrepareWorker(QObject):
    """Applies the husk-engine USD edits before a render, off the Qt thread.

    Render-setting overrides and the AOV filter are both overlays and each must
    compose over the last, so they run in one worker in order rather than being
    chained through signals. hython needs none of this — it carries the same
    edits on its command line and applies them inside the network.
    """
    finished = Signal(str)      # the USD to actually render
    failed = Signal(str)

    def __init__(self, usd_in: str, settings_overrides: Optional[dict] = None,
                 keep_paths=None, settings_prim: str = "", hython: str = ""):
        super().__init__()
        self.usd_in = usd_in
        self.settings_overrides = settings_overrides or {}
        self.keep_paths = keep_paths
        self.settings_prim = settings_prim
        self.hython = hython

    @Slot()
    def run(self) -> None:
        usd = self.usd_in
        try:
            if self.settings_overrides:
                result = bridge.override_settings(
                    usd, self.settings_overrides, hython=self.hython,
                    settings_prim=self.settings_prim)
                if result.get("usd_out"):
                    usd = result["usd_out"]
                for entry in result.get("skipped", []):
                    # Not fatal, but the user asked for it and did not get it.
                    sys.stderr.write(f"skipped {entry['key']}: {entry['why']}\n")
            if self.keep_paths is not None:
                usd = bridge.filter_aovs(usd, self.keep_paths, hython=self.hython)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(usd)


class RelinkWorker(QObject):
    """Runs bridge.relink_assets (a hython subprocess) off the Qt thread."""
    finished = Signal(object)   # the result dict
    failed = Signal(str)

    def __init__(self, usd_in: str, search_dirs, hython: str = ""):
        super().__init__()
        self.usd_in = usd_in
        self.search_dirs = search_dirs
        self.hython = hython

    @Slot()
    def run(self) -> None:
        try:
            result = bridge.relink_assets(self.usd_in, self.search_dirs, hython=self.hython)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class QueueBridge(QObject):
    """Marshals RenderQueue callbacks onto the Qt event loop."""
    task_started = Signal(object)
    task_output = Signal(object, str)
    task_progress = Signal(object, int)
    task_finished = Signal(object)
    queue_finished = Signal(object)

    def dispatch(self, event: str, *args) -> None:
        getattr(self, event).emit(*args)


# Severity colours for the scene status line. It used to be fixed at
# `palette(mid)` for its whole life, so the messages that matter most — missing
# textures, volumes that will bake tens of GB on export — were drawn in the same
# dim grey as the idle placeholder, and were unreadable on a dark theme.
_DETAIL_PLACEHOLDER = "Read a scene to see its render settings, camera and AOVs."

_STATUS_STYLES = {
    "muted": "color: palette(mid);",
    "info": "color: palette(text);",
    "warning": "color: #FF9800; font-weight: bold;",
    "error": "color: #F44336; font-weight: bold;",
}


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Solaris Render Launcher")
        icon = QIcon(resources.icon_path())
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1180, 820)

        self.manifest: Optional[SceneManifest] = None
        self.queue: Optional[RenderQueue] = None
        self.tasks: list[Task] = []
        self._has_tasks = False
        self._running = False
        # id(task) for every task that has actually reported an ALF_PROGRESS
        # percentage -- husk's own signal, never invented. Distinguishes "0%,
        # nothing received yet" (hython, which reports none) from "0%, husk
        # just said so", so the progress display never shows a number that
        # never arrived.
        self._progress_seen: set = set()
        self._thread: Optional[QThread] = None
        self._worker: Optional[InspectWorker] = None
        # ROP node path -> relinked overlay USD, once assets have been repathed.
        # husk only: that path edits the exported USD up front.
        self._relinked_usd: dict = {}
        # ROP node path -> texture search dirs, for the hython engine. There is
        # no exported USD to edit there, so a relink cannot happen until render
        # time — render_direct composes the repaths into the LOP network. The
        # button therefore records an intent here rather than doing work now.
        self._relink_dirs: dict = {}

        self._build_ui()
        self._populate_hython_options()
        self._connect()
        self._set_scene_loaded(False)

    # -- construction -----------------------------------------------------

    MODE_RENDER, MODE_COOK = 0, 1

    def _engine(self) -> str:
        """The selected render engine token — "hython" (default) or "husk"."""
        return self.engine_combo.currentData() or husk_mod.DEFAULT_ENGINE

    def _set_status(self, text: str, level: str = "info") -> None:
        """Set the scene status line and colour it by severity.

        Always go through here rather than touching ``status_label`` directly:
        the colour is part of the message, and one left over from a previous
        state is misleading.
        """
        self.status_label.setStyleSheet(
            _STATUS_STYLES.get(level, _STATUS_STYLES["info"]))
        self.status_label.setText(text)

    def _populate_hython_options(self) -> None:
        self.hython_combo.blockSignals(True)
        self.hython_combo.clear()
        installs = bridge.list_hython_installations()
        for label, path in installs:
            # The version is what anyone picks by; the full path made the row
            # unreadable and was repeated in the field beside it anyway.
            self.hython_combo.addItem(label, path)
            self.hython_combo.setItemData(self.hython_combo.count() - 1,
                                          path, Qt.ToolTipRole)
        self.hython_combo.addItem("Custom path…", "")

        saved = bridge.load_user_settings().get("hython_path", "")
        active = bridge.find_hython(saved)
        if active:
            idx = self.hython_combo.findData(active)
            if idx >= 0:
                self.hython_combo.setCurrentIndex(idx)
            else:
                self.hython_combo.setCurrentIndex(self.hython_combo.count() - 1)
            self.hython_edit.setText(active)
        else:
            self.hython_combo.setCurrentIndex(self.hython_combo.count() - 1)
        self.hython_combo.blockSignals(False)

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # --- scene row ---
        scene_row = QHBoxLayout()
        self.hip_edit = QLineEdit()
        self.hip_edit.setPlaceholderText("Choose a .hip file to read")
        self.browse_btn = QPushButton("Browse…")
        self.read_btn = QPushButton("Read scene")
        self.read_btn.setDefault(True)
        scene_row.addWidget(QLabel("Scene"))
        scene_row.addWidget(self.hip_edit, 1)
        scene_row.addWidget(self.browse_btn)
        scene_row.addWidget(self.read_btn)
        outer.addLayout(scene_row)

        # --- hython selector row ---
        hython_row = QHBoxLayout()
        self.hython_combo = QComboBox()
        self.hython_edit = QLineEdit()
        self.hython_edit.setPlaceholderText("Path to hython executable")
        self.hython_browse_btn = QPushButton("Browse Hython…")
        self.hython_rescan_btn = QPushButton("Rescan")
        self.hython_rescan_btn.setToolTip(
            "Search again for Houdini installations — use this after installing "
            "a new Houdini or connecting a drive, without restarting the app.")
        hython_row.addWidget(QLabel("Hython"))
        hython_row.addWidget(self.hython_combo, 1)
        # Still the authoritative path (and what "Custom path…" edits), but it
        # showed the same string as the combo beside it, across half the row.
        # Kept in the layout, out of sight, so every reader of it still works.
        self.hython_edit.setVisible(False)
        hython_row.addWidget(self.hython_edit)
        hython_row.addWidget(self.hython_rescan_btn)
        hython_row.addWidget(self.hython_browse_btn)
        hython_row.addStretch(1)
        outer.addLayout(hython_row)

        opts_row = QHBoxLayout()
        self.export_usd_check = QCheckBox("Write USD while reading")
        self.export_usd_check.setChecked(False)
        self.export_usd_check.setToolTip(
            "If unchecked, scene reading is fast (metadata only). Turn on to pre-bake "
            "USD stages to disk."
        )
        self.flatten_check = QCheckBox("Flatten stage")
        self.flatten_check.setToolTip(
            "Collapse all layers into one file. More portable across a farm, "
            "larger on disk."
        )
        opts_row.addWidget(self.export_usd_check)
        opts_row.addWidget(self.flatten_check)
        opts_row.addStretch(1)
        self.status_label = QLabel("No scene read yet.")
        self.status_label.setStyleSheet(_STATUS_STYLES["muted"])
        self.status_label.setWordWrap(True)
        opts_row.addWidget(self.status_label, 1)
        outer.addLayout(opts_row)

        # --- middle splitter ---
        splitter = QSplitter(Qt.Vertical)

        render_page = QWidget()
        render_layout = QHBoxLayout(render_page)
        render_layout.setContentsMargins(0, 0, 0, 0)
        render_layout.addWidget(self._build_rop_panel(), 1)

        # Tabbed so the render-settings editor has room without pushing the
        # window taller — and so it is visible rather than buried in a form.
        right = QTabWidget()
        right.addTab(self._build_override_panel(), "Overrides")
        right.addTab(self._build_settings_panel(), "Render settings")
        render_layout.addWidget(right, 1)

        # One mode at a time. Rendering and cooking share almost none of their
        # controls, and showing both at once was most of what made this a wall
        # of widgets. The Run panel below is shared: both end up in the queue.
        self.mode_tabs = QTabWidget()
        self.mode_tabs.addTab(render_page, "Render")
        self.mode_tabs.addTab(self._build_tasks_panel(), "Caches && Sims")
        splitter.addWidget(self.mode_tabs)

        splitter.addWidget(self._build_run_panel())
        splitter.setSizes([440, 360])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self._build_menu()

    def _build_rop_panel(self) -> QWidget:
        # "&&" because Qt reads a single & as a keyboard accelerator and eats it,
        # which is why these titles used to render with the word missing.
        box = QGroupBox("Scene")
        layout = QVBoxLayout(box)

        # Shown instead of nothing when a scene has no render ROPs. This used
        # to be a line of grey monospace below an empty AOV list -- the single
        # most important thing on screen, rendered as the least visible.
        self.empty_hint = QLabel()
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet(_STATUS_STYLES["warning"])
        self.empty_hint.setVisible(False)
        layout.addWidget(self.empty_hint)

        self.rop_combo = QComboBox()
        self.rop_combo.setToolTip(
            "Which ROP the panels on the right describe and edit. On a scene "
            "with several ROPs, tick which ones render below -- this combo "
            "only chooses which one's overrides you are looking at.")
        layout.addWidget(self.rop_combo)

        # Only a scene with more than one render ROP ever shows this -- a
        # single-ROP scene has nothing to tick, so it looks exactly as it did
        # before this table existed. Ticking several queues them into one run,
        # in the order listed; the combo above still targets one of them for
        # the override panels on the right, since AOVs, resolution, camera and
        # the rest are inherently one ROP's settings.
        self.rop_queue_hint = QLabel("")
        self.rop_queue_hint.setWordWrap(True)
        self.rop_queue_hint.setStyleSheet(_STATUS_STYLES["muted"])
        self.rop_queue_hint.setVisible(False)
        layout.addWidget(self.rop_queue_hint)

        self.rop_table = QTableWidget(0, 2)
        self.rop_table.setHorizontalHeaderLabels(["ROP", "Frames"])
        self.rop_table.verticalHeader().setVisible(False)
        self.rop_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.rop_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        # Tall enough for ~2-3 rows before it scrolls -- most scenes have a
        # handful of render ROPs. minimumHeight is a floor the layout cannot
        # violate; without it a tight window squeezes this below its header
        # plus one row before it touches the AOV list and detail box below,
        # which need the rest of the panel's height.
        self.rop_table.setMinimumHeight(90)
        self.rop_table.setMaximumHeight(140)
        self.rop_table.setVisible(False)
        layout.addWidget(self.rop_table)

        self.aov_box = QGroupBox("AOVs to render")
        self.aov_box.setToolTip("Ticked AOVs are kept; the rest are dropped from "
                           "the exported USD (husk engine only).")
        aov_layout = QVBoxLayout(self.aov_box)

        self.aov_list = QListWidget()
        self.aov_list.setMinimumHeight(96)
        aov_layout.addWidget(self.aov_list)

        aov_btn_row = QHBoxLayout()
        self.aov_all_btn = QPushButton("Select All")
        self.aov_beauty_btn = QPushButton("Beauty + Depth")
        self.aov_none_btn = QPushButton("Clear All")
        aov_btn_row.addWidget(self.aov_all_btn)
        aov_btn_row.addWidget(self.aov_beauty_btn)
        aov_btn_row.addWidget(self.aov_none_btn)
        aov_layout.addLayout(aov_btn_row)

        layout.addWidget(self.aov_box)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont(MONO, 9))
        self.detail.setPlaceholderText(_DETAIL_PLACEHOLDER)
        # Hidden until a ROP is actually described -- it carries the layout's
        # stretch, so left visible-but-empty it was a large blank box under
        # the empty-state message above.
        self.detail.setVisible(False)
        layout.addWidget(self.detail, 1)
        return box

    def _build_settings_panel(self) -> QWidget:
        """Editor for arbitrary karma:* / husk:* render settings."""
        box = QWidget()
        layout = QVBoxLayout(box)

        hint = QLabel(
            "husk has no flag for these — each one is authored as a USD overlay, "
            "and works on either engine. Only settings the scene already declares "
            "can be set; the type comes from the scene, so 64 stays an integer.")
        hint.setWordWrap(True)
        hint.setStyleSheet(_STATUS_STYLES["muted"])
        layout.addWidget(hint)

        self.settings_table = QTableWidget(0, 2)
        self.settings_table.setHorizontalHeaderLabels(["Setting", "Value"])
        self.settings_table.verticalHeader().setVisible(False)
        header = self.settings_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self.settings_table, 1)

        row = QHBoxLayout()
        self.setting_add_btn = QPushButton("Add override")
        self.setting_remove_btn = QPushButton("Remove")
        self.setting_clear_btn = QPushButton("Clear all")
        row.addWidget(self.setting_add_btn)
        row.addWidget(self.setting_remove_btn)
        row.addWidget(self.setting_clear_btn)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _available_settings(self) -> list:
        """Every karma:*/husk:* knob the scene declares, for the picker."""
        if not self.manifest:
            return []
        names: set = set()
        for settings in self.manifest.settings:
            names.update(settings.renderer_settings.keys())
        return sorted(names)

    def _current_setting_value(self, key: str):
        for settings in self.manifest.settings if self.manifest else []:
            if key in settings.renderer_settings:
                return settings.renderer_settings[key]
        return None

    def _setting_row_of(self, widget) -> int:
        for row in range(self.settings_table.rowCount()):
            if self.settings_table.cellWidget(row, 0) is widget:
                return row
        return -1

    def _setting_overrides(self) -> dict:
        """The table as a {key: value} dict, skipping incomplete rows."""
        overrides: dict = {}
        for row in range(self.settings_table.rowCount()):
            combo = self.settings_table.cellWidget(row, 0)
            item = self.settings_table.item(row, 1)
            key = combo.currentText().strip() if combo else ""
            value = item.text().strip() if item else ""
            if key and value:
                overrides[key] = value
        return overrides

    @Slot()
    def add_setting_override(self) -> None:
        names = self._available_settings()
        if not names:
            QMessageBox.information(
                self, "No render settings to override",
                "Read a scene first — the list of knobs is read from its render "
                "settings prim, and only settings the scene declares can be set.")
            return

        row = self.settings_table.rowCount()
        self.settings_table.insertRow(row)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(names)
        self.settings_table.setCellWidget(row, 0, combo)
        self.settings_table.setItem(row, 1, QTableWidgetItem(""))
        # Seed the value with what the scene currently has, so the row starts
        # as a no-op the user edits rather than a blank that silently does nothing.
        self._seed_setting_value(row, combo.currentText())
        combo.currentTextChanged.connect(
            lambda _text, c=combo: self._on_setting_key_changed(c))

    def _seed_setting_value(self, row: int, key: str) -> None:
        current = self._current_setting_value(key)
        item = self.settings_table.item(row, 1)
        if item is not None:
            item.setText("" if current is None else str(current))

    def _on_setting_key_changed(self, combo) -> None:
        row = self._setting_row_of(combo)
        if row >= 0:
            self._seed_setting_value(row, combo.currentText())
        self.refresh_command()

    @Slot()
    def remove_setting_override(self) -> None:
        rows = sorted({i.row() for i in self.settings_table.selectedIndexes()},
                      reverse=True)
        if not rows and self.settings_table.rowCount():
            rows = [self.settings_table.rowCount() - 1]
        for row in rows:
            self.settings_table.removeRow(row)
        self.refresh_command()

    @Slot()
    def clear_setting_overrides(self) -> None:
        self.settings_table.setRowCount(0)
        self.refresh_command()

    def _build_override_panel(self) -> QWidget:
        box = QGroupBox("Render options")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom Configuration", "")
        for p_name in presets.get_default_presets().keys():
            self.preset_combo.addItem(p_name, p_name)
        form.addRow("Preset", self.preset_combo)

        # Hython first: it is the default engine. It renders the ROP directly,
        # so it needs no USD export -- which on a volume-heavy scene would bake
        # tens of GB per frame. The token lives in the item data so the rest of
        # the window never depends on the order of this list.
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Hython (direct ROP, no USD export)", "hython")
        self.engine_combo.addItem("Husk (USD export — needed for AOV filter, relink, farm)", "husk")
        form.addRow("Engine", self.engine_combo)

        self.renderer_combo = QComboBox()
        self.renderer_combo.setEditable(True)
        form.addRow("Render delegate", self.renderer_combo)

        self.settings_combo = QComboBox()
        form.addRow("Settings prim", self.settings_combo)

        self.camera_combo = QComboBox()
        self.camera_combo.setEditable(True)
        form.addRow("Camera", self.camera_combo)

        res_row = QHBoxLayout()
        self.res_check = QCheckBox("Override")
        self.res_x = QSpinBox(); self.res_x.setRange(1, 65536); self.res_x.setValue(1920)
        self.res_y = QSpinBox(); self.res_y.setRange(1, 65536); self.res_y.setValue(1080)
        for widget in (self.res_x, self.res_y):
            widget.setEnabled(False)
        res_row.addWidget(self.res_check)
        res_row.addWidget(self.res_x)
        res_row.addWidget(QLabel("×"))
        res_row.addWidget(self.res_y)
        res_row.addStretch(1)
        form.addRow("Resolution", res_row)

        frame_row = QHBoxLayout()
        self.frame_start = QSpinBox(); self.frame_start.setRange(-100000, 1000000)
        self.frame_end = QSpinBox(); self.frame_end.setRange(-100000, 1000000)
        self.frame_inc = QSpinBox(); self.frame_inc.setRange(1, 1000); self.frame_inc.setValue(1)
        frame_row.addWidget(self.frame_start)
        frame_row.addWidget(QLabel("to"))
        frame_row.addWidget(self.frame_end)
        frame_row.addWidget(QLabel("step"))
        frame_row.addWidget(self.frame_inc)
        frame_row.addStretch(1)
        form.addRow("Frames", frame_row)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Leave empty to use the product name from USD")
        # Two buttons because the two modes mean different things: a file gives
        # the first product that exact name, a folder keeps every product's own
        # filename. A save-file dialog alone cannot express the second.
        self.output_browse_btn = QPushButton("File…")
        self.output_browse_btn.setToolTip(
            "Choose an output file. The first render product takes this exact "
            "path; any others are written alongside it under their own names.")
        self.output_folder_btn = QPushButton("Folder…")
        self.output_folder_btn.setToolTip(
            "Choose an output folder. Every render product keeps its own "
            "filename and is written into this folder.")
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_browse_btn)
        output_row.addWidget(self.output_folder_btn)
        form.addRow("Output", output_row)

        # Everything below is a tuning knob with a sane default. Folded away by
        # default so the panel shows the handful of things a render actually
        # needs; tick the box to open it.
        self.advanced_box = QGroupBox("Advanced")
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Chunking, concurrency and husk verbosity. The defaults are fine "
            "for a single-machine render.")
        advanced = QFormLayout(self.advanced_box)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(0, 10000)
        self.chunk_spin.setSpecialValueText("all in one")
        self.chunk_spin.setToolTip("Frames per husk process.")
        advanced.addRow("Chunk size", self.chunk_spin)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 64)
        self.parallel_spin.setValue(1)
        self.parallel_spin.setToolTip("How many husk processes run at once.")
        advanced.addRow("Concurrent renders", self.parallel_spin)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 512)
        self.threads_spin.setSpecialValueText("all cores")
        advanced.addRow("Threads per render", self.threads_spin)

        self.snapshot_spin = QSpinBox()
        self.snapshot_spin.setRange(0, 3600)
        self.snapshot_spin.setSpecialValueText("off")
        self.snapshot_spin.setSuffix(" s")
        self.snapshot_spin.setToolTip(
            "Flush a partial image this often so you can check progress."
        )
        advanced.addRow("Snapshot every", self.snapshot_spin)

        self.verbosity_edit = QLineEdit("3")
        self.verbosity_edit.setToolTip("husk verbosity level. 'a' is appended for progress.")
        advanced.addRow("Verbosity", self.verbosity_edit)

        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("--disable-motionblur --complexity high")
        advanced.addRow("Extra flags", self.extra_edit)

        # A checkable QGroupBox disables its children rather than hiding them,
        # which would grey out live settings. Hide them instead, so unticking
        # is purely visual and the values still reach the command.
        self.advanced_box.toggled.connect(self._set_advanced_visible)
        form.addRow(self.advanced_box)          # spans both columns
        self._set_advanced_visible(False)

        return box

    def _set_advanced_visible(self, shown: bool) -> None:
        for child in self.advanced_box.findChildren(QWidget):
            child.setVisible(shown)
        self.advanced_box.setFlat(not shown)
        # Hiding the children leaves the frame's own margins behind as an empty
        # strip, so collapse the box to its title row as well.
        if shown:
            self.advanced_box.setMaximumHeight(16777215)
        else:
            self.advanced_box.setMaximumHeight(
                self.advanced_box.fontMetrics().height() + 8)

    def _build_run_panel(self) -> QWidget:
        box = QGroupBox("Run")
        layout = QVBoxLayout(box)

        self.preflight_label = QLabel("Preflight: Ready.")
        self.preflight_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        layout.addWidget(self.preflight_label)

        self.command_view = QPlainTextEdit()
        self.command_view.setReadOnly(True)
        self.command_view.setFont(QFont(MONO, 9))
        self.command_view.setMinimumHeight(56)
        self.command_view.setPlaceholderText("The husk command appears here.")
        layout.addWidget(self.command_view)

        button_row = QHBoxLayout()
        self.render_btn = QPushButton("Render")
        # The app's whole purpose; it used to be the same size and weight as
        # "Copy command", fifth in a row of six.
        primary_font = QFont()
        primary_font.setBold(True)
        self.render_btn.setFont(primary_font)
        self.render_btn.setMinimumHeight(30)
        self.render_btn.setMinimumWidth(150)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.copy_btn = QPushButton("Copy command")
        self.relink_btn = QPushButton("Relink textures…")
        self.relink_btn.setToolTip(
            "Pick a folder to search for missing textures and repath them "
            "(husk renders the relinked USD).")
        self.farm_btn = QPushButton("Submit to Farm…")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setFormat("%p%")
        # Idle, an empty progress bar reads as a text field someone forgot to
        # fill in. It appears when there is progress to report.
        self.overall_bar.setVisible(False)
        button_row.addWidget(self.render_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.copy_btn)
        button_row.addWidget(self.relink_btn)
        button_row.addWidget(self.farm_btn)
        # Takes the slack while the bar is hidden, so the buttons keep their
        # natural width instead of stretching across the window.
        button_row.addStretch(1)
        button_row.addWidget(self.overall_bar, 2)
        layout.addLayout(button_row)

        queue_widget = QWidget()
        q_layout = QVBoxLayout(queue_widget)
        q_layout.setContentsMargins(0, 0, 0, 0)
        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(["Frames", "State", "Progress", "Time"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        # Wide enough for "frame 1234: 100%" / "chunk 1-100: 100%" without
        # truncating -- the plain "42%" this replaced fit in half the space.
        self.task_table.setColumnWidth(2, 170)
        self.task_table.setMinimumHeight(120)
        q_layout.addWidget(self.task_table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(MONO, 9))
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("husk output appears here.")
        q_layout.addWidget(self.log_view, 1)
        layout.addWidget(queue_widget, 1)
        return box

    def _build_tasks_panel(self) -> QWidget:
        """Everything in the scene that can be cooked but is not a render."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.task_list = QTableWidget(0, 5)
        self.task_list.setHorizontalHeaderLabels(
            ["Node", "Kind", "Frames", "Waits for", "Writes"])
        self.task_list.verticalHeader().setVisible(False)
        self.task_list.setSelectionBehavior(QTableWidget.SelectRows)
        self.task_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.task_list, 1)

        # No button here: cooking is started by the one primary action in the
        # Run panel, which follows the selected mode. Two "go" buttons in two
        # different places was half the confusion.
        self.cook_hint = QLabel("")
        self.cook_hint.setWordWrap(True)
        self.cook_hint.setStyleSheet(_STATUS_STYLES["muted"])
        layout.addWidget(self.cook_hint)
        return widget

    def _populate_tasks(self) -> None:
        tasks = [t for t in (self.manifest.tasks if self.manifest else [])
                 if t.kind != TASK_RENDER]
        self.task_list.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            item = QTableWidgetItem(task.node_path)
            item.setData(Qt.UserRole, task.node_path)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.task_list.setItem(row, 0, item)
            self.task_list.setItem(row, 1, QTableWidgetItem(task.kind))

            frames = (f"{task.frame_start}-{task.frame_end}"
                      if task.use_frame_range else str(task.frame_start))
            frame_item = QTableWidgetItem(frames)
            if task.sequential:
                frame_item.setToolTip(
                    "Frame N depends on N-1, so this is cooked in one process "
                    "in order. Chunk size does not apply."
                )
            self.task_list.setItem(row, 2, frame_item)
            self.task_list.setItem(
                row, 3, QTableWidgetItem(", ".join(task.depends_on) or "—"))
            self.task_list.setItem(
                row, 4, QTableWidgetItem(task.outputs[0] if task.outputs else "—"))

        self._has_tasks = bool(tasks)
        self._refresh_primary_action()
        sequential = sum(1 for t in tasks if t.sequential)
        if not tasks:
            self.cook_hint.setText("No caches or simulations in this scene.")
        elif sequential:
            self.cook_hint.setText(
                f"{sequential} of {len(tasks)} carry state between frames and "
                f"are never split across processes."
            )
        else:
            self.cook_hint.setText("")

    def _unticked_dependencies(self, chosen) -> list:
        """Tasks ``chosen`` needs that are in the scene but were not ticked.

        Transitive: pulling in a cache may pull in whatever *it* waits on.
        Dependencies the scene does not describe are left alone -- the queue
        treats those as already satisfied and says so in its warnings.
        """
        if not self.manifest:
            return []
        needed, seen = [], {t.node_path for t in chosen}
        queue = list(chosen)
        while queue:
            for path in queue.pop().depends_on:
                if path in seen:
                    continue
                seen.add(path)
                upstream = self.manifest.task(path)
                if upstream is not None:
                    needed.append(upstream)
                    queue.append(upstream)
        return needed

    def _ticked_cook_tasks(self) -> list:
        """The OutputTasks whose checkbox is ticked in Caches & Sims."""
        if not self.manifest:
            return []
        chosen = []
        for row in range(self.task_list.rowCount()):
            item = self.task_list.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                task = self.manifest.task(item.data(Qt.UserRole))
                if task is not None:
                    chosen.append(task)
        return chosen

    def _resolve_cook_dependencies(self, chosen: list, verb: str) -> Optional[list]:
        """Ask about ticked work's un-ticked dependencies; used by both cooking
        and farm export, so the question reads the same either way.

        Returns the (possibly extended) task list, or ``None`` if the user
        cancelled -- callers must stop rather than act on a stale ``chosen``.
        """
        extra = self._unticked_dependencies(chosen)
        if not extra:
            return chosen
        names = "\n".join(f"    {t.node_path}" for t in extra)
        answer = QMessageBox.question(
            self, "Include what this depends on?",
            f"The ticked work depends on {len(extra)} task(s) that are "
            f"not ticked:\n\n{names}\n\n{verb} without them reuses "
            f"whatever is already on disk, which may be stale or missing.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Cancel:
            return None
        if answer == QMessageBox.Yes:
            return extra + chosen
        return chosen

    def _jobs_for_cook_tasks(self, tasks: list) -> list:
        """RenderJobs for a list of OutputTasks, via jobs_for_task -- never
        rebuilt by hand, which is how a job previously lost its renderer,
        camera and settings prim and rendered wrong while exiting 0."""
        hython = self.hython_combo.currentData() or ""
        jobs = []
        for task in tasks:
            jobs += husk_mod.jobs_for_task(
                self.manifest, task,
                chunk_size=self.chunk_spin.value(),
                hython_exe=hython or None,
            )
        return jobs

    @Slot()
    def cook_selected(self) -> None:
        if not self.manifest:
            return
        chosen = self._ticked_cook_tasks()
        if not chosen:
            self._set_status("Tick at least one cache or simulation.", "error")
            return

        chosen = self._resolve_cook_dependencies(chosen, "Cooking")
        if chosen is None:
            return

        self._launch_queue(self._jobs_for_cook_tasks(chosen), verb="Cooking")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_action = QAction("&Open scene…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.choose_hip)
        file_menu.addAction(open_action)

        reread = QAction("&Re-read scene", self)
        reread.setShortcut("Ctrl+R")
        reread.triggered.connect(self.read_scene)
        file_menu.addAction(reread)

    def _connect(self) -> None:
        self.browse_btn.clicked.connect(self.choose_hip)
        self.read_btn.clicked.connect(self.read_scene)
        self.hython_combo.currentIndexChanged.connect(self.on_hython_combo_changed)
        self.hython_edit.textChanged.connect(self.on_hython_path_changed)
        self.hython_browse_btn.clicked.connect(self.choose_hython)
        self.hython_rescan_btn.clicked.connect(self.rescan_hython)
        self.rop_combo.currentIndexChanged.connect(self.on_rop_changed)
        self.rop_table.itemChanged.connect(lambda *_: self._on_rop_ticked())
        self.preset_combo.currentIndexChanged.connect(self.on_preset_changed)
        self.aov_all_btn.clicked.connect(self.select_all_aovs)
        self.aov_beauty_btn.clicked.connect(self.select_beauty_aovs)
        self.aov_none_btn.clicked.connect(self.clear_all_aovs)
        self.aov_list.itemChanged.connect(self.refresh_command)
        self.res_check.toggled.connect(self.res_x.setEnabled)
        self.res_check.toggled.connect(self.res_y.setEnabled)
        self.copy_btn.clicked.connect(self.copy_command)
        self.relink_btn.clicked.connect(self.relink_textures)
        self.output_browse_btn.clicked.connect(self.choose_output)
        self.output_folder_btn.clicked.connect(self.choose_output_folder)
        self.setting_add_btn.clicked.connect(self.add_setting_override)
        self.setting_remove_btn.clicked.connect(self.remove_setting_override)
        self.setting_clear_btn.clicked.connect(self.clear_setting_overrides)
        self.settings_table.itemChanged.connect(lambda *_: self.refresh_command())
        self.farm_btn.clicked.connect(self.export_farm_job)
        self.render_btn.clicked.connect(self._primary_action)
        self.mode_tabs.currentChanged.connect(lambda *_: self._refresh_primary_action())
        self.cancel_btn.clicked.connect(self.cancel_render)

        self.engine_combo.currentIndexChanged.connect(self.refresh_command)
        for widget in (self.renderer_combo, self.camera_combo, self.settings_combo):
            widget.currentTextChanged.connect(self.refresh_command)
        for widget in (self.frame_start, self.frame_end, self.frame_inc,
                       self.chunk_spin, self.threads_spin, self.snapshot_spin,
                       self.res_x, self.res_y):
            widget.valueChanged.connect(self.refresh_command)
        for widget in (self.output_edit, self.verbosity_edit, self.extra_edit):
            widget.textChanged.connect(self.refresh_command)
        self.res_check.toggled.connect(self.refresh_command)

    def _set_scene_loaded(self, loaded: bool) -> None:
        for widget in (self.rop_combo, self.copy_btn):
            widget.setEnabled(loaded)
        # The primary button depends on the mode as well as the scene, so it
        # is owned by one place rather than set from several.
        self._refresh_primary_action()

    def _cooking(self) -> bool:
        return self.mode_tabs.currentIndex() == self.MODE_COOK

    def _refresh_primary_action(self) -> None:
        """One button, labelled and enabled for whichever mode is showing."""
        if self._cooking():
            self.render_btn.setText("Cook Ticked")
            self.render_btn.setToolTip(
                "Cook the ticked caches and simulations. Order comes from the "
                "scene, so dependencies are handled for you.")
            ready = self._has_tasks
            self.farm_btn.setToolTip(
                "Export the ticked caches and simulations as a farm "
                "submission file, in dependency order.")
        else:
            self.render_btn.setText("Render")
            ticked = self._ticked_render_rops()
            if self._multi_rop_scene() and len(ticked) > 1:
                self.render_btn.setToolTip(
                    f"Render the {len(ticked)} ticked ROPs, in order. The options "
                    f"on the right apply only to the one shown above the list.")
                self.farm_btn.setToolTip(
                    "Export the ticked ROPs as a farm submission file.")
            else:
                self.render_btn.setToolTip(
                    "Render the selected ROP with the options on the right.")
                self.farm_btn.setToolTip(
                    "Export this render as a farm submission file.")
            ready = bool(ticked)
        self.render_btn.setEnabled(ready and not self._running)

    @Slot()
    def _primary_action(self) -> None:
        if self._cooking():
            self.cook_selected()
        else:
            self.start_render()

    # -- hython slots -----------------------------------------------------

    @Slot(int)
    def on_hython_combo_changed(self, index: int) -> None:
        path = self.hython_combo.itemData(index)
        if path:
            self.hython_edit.setText(path)
            bridge.save_user_setting("hython_path", path)

    @Slot(str)
    def on_hython_path_changed(self, text: str) -> None:
        path = text.strip()
        if path and os.path.isfile(path):
            bridge.save_user_setting("hython_path", path)

    @Slot()
    def rescan_hython(self) -> None:
        """Re-run the install scan and repopulate the dropdown."""
        current = self.hython_edit.text().strip()
        self._populate_hython_options()
        found = self.hython_combo.count() - 1        # last entry is "Custom path..."
        if current:
            index = self.hython_combo.findData(current)
            if index >= 0:
                self.hython_combo.setCurrentIndex(index)
        self._set_status(
            f"Found {found} Houdini install(s)." if found
            else "No Houdini installs found — set $HFS or browse to hython.")

    @Slot()
    def choose_hython(self) -> None:
        filter_str = "hython.exe (hython.exe);;All files (*)" if os.name == "nt" else "hython (hython);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Hython executable", self.hython_edit.text() or os.path.expanduser("~"),
            filter_str,
        )
        if path:
            self.hython_edit.setText(path)
        bridge.save_user_setting("hython_path", path)
        idx = self.hython_combo.findData(path)
        if idx >= 0:
            self.hython_combo.setCurrentIndex(idx)
        else:
            self.hython_combo.setCurrentIndex(self.hython_combo.count() - 1)

    # -- presets and AOV slots --------------------------------------------

    @Slot(int)
    def on_preset_changed(self, index: int) -> None:
        name = self.preset_combo.itemData(index)
        if not name:
            return
        all_presets = presets.get_default_presets()
        if name in all_presets:
            p = all_presets[name]
            if "renderer" in p:
                self.renderer_combo.setCurrentText(p["renderer"])
            if "resolution" in p and p["resolution"]:
                self.res_check.setChecked(True)
                self.res_x.setValue(p["resolution"][0])
                self.res_y.setValue(p["resolution"][1])
            if "threads" in p:
                self.threads_spin.setValue(p["threads"])
            if "snapshot_interval" in p:
                self.snapshot_spin.setValue(p["snapshot_interval"])
            if "verbosity" in p:
                self.verbosity_edit.setText(p["verbosity"])
            if "extra_args" in p:
                self.extra_edit.setText(" ".join(p["extra_args"]))
            self.refresh_command()

    @Slot()
    def select_all_aovs(self) -> None:
        self.aov_list.blockSignals(True)
        for i in range(self.aov_list.count()):
            self.aov_list.item(i).setCheckState(Qt.Checked)
        self.aov_list.blockSignals(False)
        self.refresh_command()

    @Slot()
    def select_beauty_aovs(self) -> None:
        self.aov_list.blockSignals(True)
        for i in range(self.aov_list.count()):
            item = self.aov_list.item(i)
            name = item.text().lower()
            keep = ("beauty" in name or "depth" in name or name in ("c", "z", "alpha"))
            item.setCheckState(Qt.Checked if keep else Qt.Unchecked)
        self.aov_list.blockSignals(False)
        self.refresh_command()

    @Slot()
    def clear_all_aovs(self) -> None:
        self.aov_list.blockSignals(True)
        for i in range(self.aov_list.count()):
            self.aov_list.item(i).setCheckState(Qt.Unchecked)
        self.aov_list.blockSignals(False)
        self.refresh_command()

    def _cook_jobs_for_farm(self) -> Optional[list]:
        """RenderJobs for whatever is ticked in Caches & Sims, for farm export.

        Mirrors cook_selected's own selection and dependency prompt so an
        exported job matches what "Cook Ticked" would actually run. Returns
        None (having already told the user why) when there is nothing to
        export or they cancelled the dependency prompt.
        """
        if not self.manifest:
            QMessageBox.warning(self, "Nothing to export", "Read a scene first.")
            return None
        chosen = self._ticked_cook_tasks()
        if not chosen:
            QMessageBox.warning(
                self, "Nothing to export",
                # A single & here: QMessageBox text has no mnemonic handling,
                # so && would display doubled (unlike the tab label above).
                "Tick at least one cache or simulation in the Caches & Sims "
                "tab first.")
            return None
        chosen = self._resolve_cook_dependencies(chosen, "Exporting")
        if chosen is None:
            return None
        return self._jobs_for_cook_tasks(chosen)

    @Slot()
    def export_farm_job(self) -> None:
        if self._cooking():
            jobs = self._cook_jobs_for_farm()
            if not jobs:
                return          # already told the user why, or they cancelled
        else:
            jobs = self.build_render_jobs()
            if not jobs:
                QMessageBox.warning(self, "No Render Job", "Read a scene and select a valid ROP first.")
                return

        choice, ok = QFileDialog.getSaveFileName(
            self, "Export Farm Submission File",
            os.path.expanduser("~/deadline_job.job"),
            "Deadline Job (*.job);;Tractor Job (*.alf)",
        )
        if not ok or not choice:
            return

        try:
            if choice.endswith(".alf"):
                farm.export_tractor_job(jobs, choice)
                QMessageBox.information(self, "Tractor Job Exported", f"Exported Tractor job script:\n{choice}")
            else:
                out_dir = os.path.dirname(choice) or os.getcwd()
                j_path, p_path = farm.export_deadline_job(jobs, out_dir)
                QMessageBox.information(self, "Deadline Job Exported", f"Exported Deadline job files:\n{j_path}\n{p_path}")
        except ValueError as exc:
            # Deadline can't express a dependency between whole jobs, or one
            # job spanning several tasks -- it refuses rather than writing
            # something that races. Say so; the message already names what and
            # points at Tractor, which encodes the dependency as a DAG.
            QMessageBox.critical(self, "Cannot export this job", str(exc))
        except OSError as exc:
            QMessageBox.critical(self, "Could not write the farm submission", str(exc))

    # -- scene reading ----------------------------------------------------

    @Slot()
    def choose_hip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a Houdini scene", self.hip_edit.text() or os.path.expanduser("~"),
            "Houdini scenes (*.hip *.hipnc *.hiplc);;All files (*)",
        )
        if path:
            self.hip_edit.setText(path)
            self.read_scene()

    @Slot()
    def read_scene(self, force_reload: bool = False) -> None:
        hip_path = self.hip_edit.text().strip()
        if not hip_path:
            self._set_status("Choose a .hip file first.", "warning")
            return
        if not os.path.isfile(hip_path):
            QMessageBox.warning(self, "Scene not found",
                                f"There is no file at:\n{hip_path}")
            return

        if not force_reload:
            cached = bridge.load_cached(hip_path)
            if cached:
                self.on_scene_read(cached)
                self._set_status(f"Loaded cached manifest for {os.path.basename(hip_path)} (Instant).")
                return

        self.read_btn.setEnabled(False)
        self._set_status("Reading scene in hython. Large scenes take a while…")

        self._thread = QThread(self)
        self._worker = InspectWorker(
            hip_path,
            export_usd=self.export_usd_check.isChecked(),
            flatten=self.flatten_check.isChecked(),
            hython=self.hython_edit.text().strip(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self.on_scene_read)
        self._worker.failed.connect(self.on_scene_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    @Slot(object)
    def on_scene_read(self, manifest: SceneManifest) -> None:
        self.manifest = manifest
        self.read_btn.setEnabled(True)
        # A fresh read invalidates any prior relink, on either engine. The
        # setting overrides go too: their knob names come from the old scene.
        self._relinked_usd.clear()
        self._relink_dirs.clear()
        self.settings_table.setRowCount(0)

        self.rop_combo.blockSignals(True)
        self.rop_combo.clear()
        for rop in manifest.rops:
            self.rop_combo.addItem(rop.node_path, rop.node_path)
        self.rop_combo.blockSignals(False)
        self._populate_rop_queue()
        self._refresh_output_override_state()

        self._populate_tasks()

        # Populate AOVs — display the (distinct) prim name, key by prim path.
        self.aov_list.blockSignals(True)
        self.aov_list.clear()
        for v in manifest.vars:
            item = QListWidgetItem(v.prim_path.rsplit("/", 1)[-1])
            item.setData(Qt.UserRole, v.prim_path)
            item.setToolTip(f"{v.prim_path}\nsource: {v.source_name or '—'} "
                            f"({v.source_type or '—'}, {v.data_type or '—'})")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.aov_list.addItem(item)
        self.aov_list.blockSignals(False)

        self.settings_combo.clear()
        self.settings_combo.addItem("(from ROP / stage default)", "")
        for settings in manifest.settings:
            self.settings_combo.addItem(settings.prim_path, settings.prim_path)

        self.camera_combo.clear()
        self.camera_combo.addItem("", "")
        for camera in manifest.cameras:
            self.camera_combo.addItem(camera.prim_path, camera.prim_path)

        current = self.renderer_combo.currentText()
        self.renderer_combo.clear()
        self.renderer_combo.addItems(husk_mod.list_renderers())
        if current:
            self.renderer_combo.setCurrentText(current)

        found = f"{len(manifest.rops)} ROP(s), {len(manifest.settings)} render settings prim(s)"
        # Anything worth a ⚠ colours the whole line, so it reads as a warning.
        level = "warning" if (manifest.missing_assets or manifest.live_volumes) else "info"
        if manifest.missing_assets:
            found += f" — ⚠ {len(manifest.missing_assets)} missing texture(s), use Relink"
        if manifest.live_volumes:
            found += (f" — ⚠ {len(manifest.live_volumes)} live volume(s) bake on export, "
                      f"prefer Hython engine")
        if manifest.warnings:
            found += f" — {len(manifest.warnings)} warning(s)"
        self._set_status(found, level)

        cookable = [t for t in manifest.tasks if t.kind != TASK_RENDER]
        if manifest.rops:
            self.empty_hint.setVisible(False)
        else:
            # Say what to do next, and point at the tab that can still help --
            # a scene with no render ROP often still has caches worth cooking.
            message = ("This scene has no USD Render ROP, so there is nothing "
                       "to render. Add one at the end of the LOP network, save, "
                       "and read the scene again.")
            if cookable:
                message += (f"\n\nIt does have {len(cookable)} cache/simulation "
                            f"task(s) — see the “Caches & Sims” tab.")
            self.empty_hint.setText(message)
            self.empty_hint.setVisible(True)

        self._set_scene_loaded(bool(manifest.rops))
        if manifest.rops:
            self.detail.setPlaceholderText(_DETAIL_PLACEHOLDER)
            self.on_rop_changed(0)
        else:
            # empty_hint already says this, prominently -- repeating it here in
            # grey monospace made the panel look full of nothing. Hide the box
            # rather than leave it empty; it reappears once a ROP is described.
            self.detail.clear()
            self.detail.setPlaceholderText("")
            self.detail.setVisible(False)

        # An empty AOV list is a large box saying nothing; it earns its space
        # only once the scene actually declares some.
        self.aov_box.setVisible(bool(manifest.vars))

    @Slot(str)
    def on_scene_failed(self, message: str) -> None:
        self.read_btn.setEnabled(True)
        self._set_status("Reading the scene failed.", "error")
        QMessageBox.critical(self, "Could not read the scene", message[-4000:])

    # -- ROP selection ----------------------------------------------------

    def current_rop(self) -> Optional[RenderRop]:
        if not self.manifest:
            return None
        node_path = self.rop_combo.currentData() or self.rop_combo.currentText()
        return self.manifest.rop(node_path)

    @Slot(int)
    def on_rop_changed(self, index: int) -> None:
        rop = self.current_rop()
        if not rop or not self.manifest:
            return
        settings = self.manifest.resolve_settings(rop)

        if rop.renderer:
            self.renderer_combo.setCurrentText(rop.renderer)
        self.settings_combo.setCurrentText(rop.settings_prim or "(from ROP / stage default)")
        camera = rop.camera or (settings.camera if settings else "")
        self.camera_combo.setCurrentText(camera)

        self.frame_start.setValue(rop.frame_start)
        self.frame_end.setValue(rop.frame_end)
        self.frame_inc.setValue(rop.frame_inc)
        self.output_edit.setText(rop.output_override)

        if settings and settings.resolution:
            self.res_x.setValue(settings.resolution[0])
            self.res_y.setValue(settings.resolution[1])

        self.detail.setPlainText(self._describe(rop, settings))
        self.detail.setVisible(True)
        self._update_rop_queue_hint()
        self.refresh_command()

    # -- multi-ROP queue (render mode) -------------------------------------
    #
    # A scene with one render ROP behaves exactly as it always did: the combo
    # above picks it, the panels on the right edit it, "Render" renders it.
    # A scene with several gets one more control -- a ticked list of which
    # ROPs run together in this pass -- and nothing else changes shape. The
    # combo keeps its old job (which ROP the override panels describe); the
    # table adds a new one (which ROPs are actually queued).

    def _multi_rop_scene(self) -> bool:
        return bool(self.manifest and len(self.manifest.rops) > 1)

    def _update_rop_queue_hint(self) -> None:
        if not self._multi_rop_scene():
            return
        rop = self.current_rop()
        name = rop.node_path if rop else "the selected ROP"
        self.rop_queue_hint.setText(
            f"Tick the ROPs to render in this run — they render in the order "
            f"listed. The panel on the right (AOVs, render settings, camera, "
            f"resolution, output…) edits {name} only; any other ticked ROP "
            f"renders with its own settings from the scene.")

    def _populate_rop_queue(self) -> None:
        """(Re)build the ticked-ROP table from the manifest. Hidden and empty
        for zero or one ROPs, so a single-ROP scene shows no new chrome."""
        rops = self.manifest.rops if self.manifest else []
        multi = len(rops) > 1
        self.rop_table.setVisible(multi)
        self.rop_queue_hint.setVisible(multi)
        if not multi:
            self.rop_table.setRowCount(0)
            return

        self.rop_table.blockSignals(True)
        self.rop_table.setRowCount(len(rops))
        for row, rop in enumerate(rops):
            item = QTableWidgetItem(rop.node_path)
            item.setData(Qt.UserRole, rop.node_path)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # Only the ROP the combo opens on (row 0, the first found) starts
            # ticked -- a render started without touching this table renders
            # exactly what it always rendered: that one ROP, nothing else.
            item.setCheckState(Qt.Checked if row == 0 else Qt.Unchecked)
            self.rop_table.setItem(row, 0, item)
            frames = (f"{rop.frame_start}-{rop.frame_end}" if rop.use_frame_range
                      else str(rop.frame_start))
            self.rop_table.setItem(row, 1, QTableWidgetItem(frames))
        self.rop_table.blockSignals(False)
        self._update_rop_queue_hint()

    def _ticked_render_rops(self) -> list:
        """RenderRops queued for this run, in display (submission) order.

        A single- (or zero-) ROP scene never shows the table -- there is only
        ever one ROP to render, so this returns it unconditionally rather than
        reading state from a hidden, empty widget. That is what keeps a
        single-ROP scene's behaviour identical to before this feature existed.
        """
        if not self.manifest:
            return []
        if not self._multi_rop_scene():
            rop = self.current_rop()
            return [rop] if rop else []
        chosen = []
        for row in range(self.rop_table.rowCount()):
            item = self.rop_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                rop = self.manifest.rop(item.data(Qt.UserRole))
                if rop is not None:
                    chosen.append(rop)
        return chosen

    @Slot()
    def _on_rop_ticked(self) -> None:
        self._refresh_output_override_state()
        self._refresh_primary_action()
        self.refresh_command()

    def _refresh_output_override_state(self) -> None:
        """Disable the Output override once several ROPs are queued.

        One path shared by several ROPs would have each overwrite the others'
        frames -- the exact reason ``hsl render --output`` refuses to run with
        more than one ``--rop``. The GUI cannot silently ignore it the way a
        stray CLI flag might go unnoticed either, so the field is disabled
        with a reason on hover, and build_render_jobs() ignores it outright
        rather than trusting a value the user can no longer see is live.
        """
        multiple = len(self._ticked_render_rops()) > 1
        for widget in (self.output_edit, self.output_browse_btn, self.output_folder_btn):
            widget.setEnabled(not multiple)
        if multiple:
            reason = ("Disabled: several ROPs are ticked to render together, and "
                       "one output path shared by all of them would have each "
                       "overwrite the others' frames -- the same reason "
                       "`hsl render --output` refuses this with more than one "
                       "--rop. Tick a single ROP to set an explicit output, or "
                       "leave outputs to the scene.")
            self.output_edit.setToolTip(reason)
            self.output_browse_btn.setToolTip(reason)
            self.output_folder_btn.setToolTip(reason)
        else:
            self.output_edit.setToolTip("")
            self.output_browse_btn.setToolTip(
                "Choose an output file. The first render product takes this exact "
                "path; any others are written alongside it under their own names.")
            self.output_folder_btn.setToolTip(
                "Choose an output folder. Every render product keeps its own "
                "filename and is written into this folder.")

    def _native_jobs_for_rop(self, rop: RenderRop) -> list:
        """RenderJobs for a ticked ROP other than the one being edited.

        Uses the ROP's own renderer, camera, settings prim, frame range,
        resolution, AOVs and output exactly as the scene declares them --
        build_jobs() (for the ROP the combo targets) is the only place the
        override panel's edits take effect, so a second ticked ROP must not
        silently inherit whatever happens to be sitting in those fields for a
        different node. Advanced tuning (chunk size, threads, snapshot
        interval, verbosity, extra flags) is shared across every ticked ROP,
        for the same reason ``--chunk``/``--threads``/``--snapshot``/``--extra``
        apply uniformly across ``--all-rops`` on the CLI -- those are process
        tuning, not scene content. Never hand-built: this still goes through
        ``jobs_for_rop``, same as build_jobs().
        """
        engine = self._engine()
        usd_file = self._base_usd(rop)
        if engine == "husk" and not usd_file:
            return []
        overrides = {
            "engine": engine,
            "threads": self.threads_spin.value(),
            "snapshot_interval": self.snapshot_spin.value(),
            "verbosity": self.verbosity_edit.text().strip() or "3",
            "extra_args": self.extra_edit.text().split(),
            "relink_dirs": (list(self._relink_dirs.get(rop.node_path, []))
                            if engine == "hython" else []),
        }
        return husk_mod.jobs_for_rop(
            self.manifest, rop, usd_file,
            chunk_size=self.chunk_spin.value(), **overrides,
        )

    def build_render_jobs(self, current_usd_override: Optional[str] = None) -> list:
        """RenderJobs for every ticked ROP, in display order.

        A single-ROP scene has nothing to tick, so this always renders that
        one ROP -- exactly what build_jobs() alone used to do. ``task_id`` is
        only set when more than one ROP is ticked, matching the CLI
        (``_render_jobs_for_rop``'s ``tag_task``): a single-ROP render keeps
        today's untagged jobs.
        """
        if not self.manifest:
            return []
        ticked = self._ticked_render_rops()
        if not ticked:
            return []
        current = self.current_rop()
        tag_task = len(ticked) > 1
        jobs: list = []
        for rop in ticked:
            if current is not None and rop.node_path == current.node_path:
                rop_jobs = self.build_jobs(usd_override=current_usd_override,
                                          allow_output=not tag_task)
            else:
                rop_jobs = self._native_jobs_for_rop(rop)
            if tag_task:
                for job in rop_jobs:
                    job.task_id = rop.node_path
            jobs += rop_jobs
        return jobs

    def _describe(self, rop: RenderRop, settings) -> str:
        m = self.manifest
        lines = [
            f"ROP        {rop.node_path}  ({rop.node_type})",
            f"Input LOP  {rop.input_lop or '—'}",
            f"Renderer   {rop.renderer or '—'}",
            f"USD file   {rop.usd_path or 'not written yet'}",
            f"Frames     {rop.frame_start}-{rop.frame_end} step {rop.frame_inc}"
            f"  ({rop.frame_count} frame(s))" if rop.use_frame_range
            else f"Frames     {rop.frame_start} (single)",
            "",
        ]

        if settings:
            resolution = ("%d × %d" % settings.resolution) if settings.resolution else "—"
            lines += [
                f"Settings   {settings.prim_path}",
                f"Resolution {resolution}",
                f"Camera     {settings.camera or '—'}",
                f"Purposes   {', '.join(settings.included_purposes) or '—'}",
                "",
            ]

            outputs = m.outputs_for(settings)
            lines.append("Outputs")
            lines += [f"  {path}" for path in outputs] or ["  —"]
            lines.append("")

            aovs = m.aovs_for(settings)
            lines.append(f"AOVs ({len(aovs)})")
            lines += [f"  {v.label:<24} {v.data_type} [{v.source_type}]" for v in aovs] or ["  —"]
            lines.append("")

            if settings.renderer_settings:
                lines.append("Delegate settings")
                for key in sorted(settings.renderer_settings):
                    lines.append(f"  {key:<44} {settings.renderer_settings[key]}")
                lines.append("")
        else:
            lines.append("No RenderSettings prim resolved for this ROP.")
            lines.append("")

        if m.warnings:
            lines.append("Warnings")
            lines += [f"  {w}" for w in m.warnings]

        return "\n".join(lines)

    # -- command ----------------------------------------------------------

    def _selected_aov_paths(self) -> Optional[list]:
        """Checked AOV prim paths, or None when every AOV is selected.

        None means "no filtering" — render the scene's products untouched. A
        strict subset returns the paths to keep; AOV editing is a USD edit
        (husk has no AOV flag), applied at render time via bridge.filter_aovs.
        """
        if not self.manifest or self.aov_list.count() == 0:
            return None
        checked = [self.aov_list.item(i).data(Qt.UserRole)
                   for i in range(self.aov_list.count())
                   if self.aov_list.item(i).checkState() == Qt.Checked]
        checked = [p for p in checked if p]
        all_paths = {v.prim_path for v in self.manifest.vars}
        if not all_paths or set(checked) == all_paths:
            return None
        return checked

    def build_jobs(self, usd_override: Optional[str] = None,
                   allow_output: bool = True) -> list:
        """RenderJobs for the ROP the combo currently targets, with every
        override on the right applied. ``allow_output`` is False when several
        ROPs are ticked to render together -- see build_render_jobs() and
        _refresh_output_override_state() for why one output path cannot be
        shared between them.
        """
        rop = self.current_rop()
        if not rop or not self.manifest:
            return []

        engine = self._engine()
        usd_file = usd_override or self._base_usd(rop)
        if engine == "husk" and not usd_file:
            return []

        settings_prim = self.settings_combo.currentData()
        if settings_prim is None:
            settings_prim = self.settings_combo.currentText()

        overrides = {
            "engine": engine,
            "renderer": self.renderer_combo.currentText().strip(),
            "settings_prim": settings_prim or "",
            "camera": self.camera_combo.currentText().strip(),
            "output": self.output_edit.text().strip() if allow_output else "",
            "threads": self.threads_spin.value(),
            "snapshot_interval": self.snapshot_spin.value(),
            "verbosity": self.verbosity_edit.text().strip() or "3",
            "extra_args": self.extra_edit.text().split(),
            "resolution": ((self.res_x.value(), self.res_y.value())
                           if self.res_check.isChecked() else None),
            # hython repaths inside the LOP network at render time. On husk the
            # exported USD was already relinked, so it needs nothing here.
            "relink_dirs": (list(self._relink_dirs.get(rop.node_path, []))
                            if engine == "hython" else []),
            # hython applies these itself at render time. On husk they are
            # authored into an overlay first, so the job must not also carry
            # them (build_command would not emit them anyway, but keeping the
            # job honest matters when it is exported to a farm).
            "settings_overrides": (self._setting_overrides()
                                   if engine == "hython" else {}),
        }

        # Frame range comes from the UI, not the ROP, so edits take effect.
        edited = RenderRop(**{**rop.__dict__})
        edited.frame_start = self.frame_start.value()
        edited.frame_end = self.frame_end.value()
        edited.frame_inc = self.frame_inc.value()
        edited.use_frame_range = edited.frame_end != edited.frame_start

        return husk_mod.jobs_for_rop(
            self.manifest, edited, usd_file,
            chunk_size=self.chunk_spin.value(), **overrides,
        )

    @Slot()
    def refresh_command(self) -> None:
        ticked = self._ticked_render_rops()
        current = self.current_rop()
        multi = len(ticked) > 1

        per_rop = []
        for rop in ticked:
            if current is not None and rop.node_path == current.node_path:
                rop_jobs = self.build_jobs(allow_output=not multi)
            else:
                rop_jobs = self._native_jobs_for_rop(rop)
            per_rop.append((rop, rop_jobs))

        all_jobs = [job for _, job_list in per_rop for job in job_list]
        if not all_jobs:
            engine = self._engine()
            if current and engine == "husk" and not current.usd_path:
                self.command_view.setPlainText(
                    "No USD on disk for this ROP. Turn on “Write USD while "
                    "reading” and read the scene again, or switch to Hython engine."
                )
            else:
                self.command_view.setPlainText("")
            self.preflight_label.setStyleSheet("color: palette(mid);")
            self.preflight_label.setText("Preflight: No active job.")
            return

        engine = self._engine()
        first_rop, first_jobs = next((rj for rj in per_rop if rj[1]), (current, []))
        target = current.node_path if current else first_rop.node_path
        lines = []
        if multi:
            lines.append(f"# --- {first_rop.node_path} ---")
        lines.append(husk_mod.format_command(husk_mod.build_command(first_jobs[0])))
        remaining = len(all_jobs) - 1
        if multi:
            lines.append(f"\n… and {remaining} more chunk(s) across "
                         f"{len(ticked)} ticked ROP(s).")
            no_usd = [r.node_path for r, j in per_rop if not j]
            if no_usd:
                lines.append(f"# No USD on disk yet for: {', '.join(no_usd)} "
                             f"— nothing will be rendered for them.")
            lines.append(f"\n# The AOV, render-settings, camera, resolution and output "
                         f"overrides above apply to {target} only; other ticked ROPs "
                         f"render with their own settings from the scene.")
        elif remaining:
            lines.append(f"\n… and {remaining} more chunk(s) with different --frame values.")
        if engine == "husk" and self._selected_aov_paths() is not None:
            lines.append("\n# AOVs will be filtered to your selection via a USD "
                         "overlay authored in hython at render start.")
        if engine == "hython" and first_jobs[0].relink_dirs:
            lines.append("\n# Missing textures will be repathed from the folder(s) "
                         "above and composed into the LOP network at render start.")
        if engine == "husk" and self._setting_overrides():
            listed = ", ".join(f"{k}={v}" for k, v in self._setting_overrides().items())
            lines.append(f"\n# Render settings ({listed}) will be authored into a "
                         f"USD overlay at render start — husk has no flag for them.")
        if multi and self.output_edit.text().strip():
            lines.append("\n# Output override is disabled while several ROPs are "
                         "ticked (see the Output field's tooltip) — each ROP writes "
                         "wherever the scene says.")

        # Resolved filenames, so the output path can be checked before starting
        # a render rather than after it lands somewhere unexpected.
        for rop, rop_jobs in per_rop:
            if not rop_jobs:
                continue
            frames = [f for job in rop_jobs
                      for f in (job.chunk.start + i * job.chunk.inc
                                for i in range(job.chunk.count))]
            output_for_preview = "" if multi else self.output_edit.text().strip()
            planned = husk_mod.planned_outputs(
                self.manifest, rop, output=output_for_preview, frames=frames)
            if multi:
                lines.append(f"\n# --- {rop.node_path} ---")
                header = "# Files this render will write:"
            else:
                header = "\n# Files this render will write:"
            if planned:
                lines.append(header)
                for entry in planned:
                    if entry["unresolved"]:
                        lines.append(f"#   {entry['template']}   "
                                     f"(unexpandable token — cannot preview)")
                    elif entry["files"]:
                        lines.append(f"#   {entry['files'][0]}")
                        if len(entry["files"]) > 1:
                            lines.append(f"#   … {len(entry['files'])} files, "
                                         f"last {entry['files'][-1]}")
                    else:
                        lines.append(f"#   {entry['template']}   "
                                     f"(filename decided by husk / the ROP)")
            else:
                prefix = "" if multi else "\n"
                lines.append(f"{prefix}# No output path declared in the scene — husk "
                             "or the ROP decides it." + ("" if multi else " Set Output above to choose."))
        self.command_view.setPlainText("\n".join(lines))

        # Preflight — judged on each ticked ROP's first chunk, with messages
        # deduplicated so several ROPs do not repeat the same finding once per
        # ROP. Mirrors the CLI's own multi-ROP preflight (cmd_render).
        seen_checks: set = set()
        pf_warnings = []
        for _, rop_jobs in per_rop:
            if not rop_jobs:
                continue
            for check in preflight.run_preflight_checks(rop_jobs[0], self.manifest):
                key = (check.level, check.message)
                if key not in seen_checks:
                    seen_checks.add(key)
                    pf_warnings.append(check)

        if pf_warnings:
            errs = [w for w in pf_warnings if w.level == "error"]
            warns = [w for w in pf_warnings if w.level == "warning"]
            if errs:
                self.preflight_label.setStyleSheet("color: #F44336; font-weight: bold;")
                self.preflight_label.setText(f"Preflight: {len(errs)} Error(s), {len(warns)} Warning(s).")
            else:
                self.preflight_label.setStyleSheet("color: #FF9800; font-weight: bold;")
                self.preflight_label.setText(f"Preflight: {len(warns)} Warning(s) detected.")
            self.preflight_label.setToolTip("\n".join(f"[{w.level.upper()}] {w.message}" for w in pf_warnings))
        else:
            self.preflight_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
            self.preflight_label.setText("Preflight: All checks passed cleanly.")
            self.preflight_label.setToolTip("Scene and job parameters are valid.")

    @Slot()
    def copy_command(self) -> None:
        QApplication.clipboard().setText(self.command_view.toPlainText())
        self._set_status("Command copied.")

    # -- assets & output --------------------------------------------------

    def _base_usd(self, rop) -> str:
        """The USD to render for this ROP — the relinked overlay if one exists."""
        if rop is None:
            return ""
        return self._relinked_usd.get(rop.node_path) or rop.usd_path

    @Slot()
    def choose_output(self) -> None:
        start = self.output_edit.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose an output image path", start,
            "Images (*.exr *.png *.jpg *.tif);;All files (*)")
        if path:
            self.output_edit.setText(path)

    @Slot()
    def choose_output_folder(self) -> None:
        start = self.output_edit.text() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self, "Choose an output folder", start)
        if folder:
            # Keep the trailing separator: that is what marks this as directory
            # mode, so every product keeps its own filename instead of the first
            # one being renamed to the folder.
            self.output_edit.setText(folder.rstrip("/\\") + "/")

    @Slot()
    def relink_textures(self) -> None:
        rop = self.current_rop()
        engine = self._engine()

        # The two engines relink at different moments. husk edits the exported
        # USD *now*; hython has no export, so the repaths are composed into the
        # LOP network at render time and this button only records the folder.
        if engine == "hython":
            if rop is None:
                QMessageBox.information(self, "Nothing to relink",
                                        "Read a scene and choose a render ROP first.")
                return
            folder = QFileDialog.getExistingDirectory(
                self, "Choose a folder to search for missing textures",
                os.path.expanduser("~"))
            if not folder:
                return
            dirs = self._relink_dirs.setdefault(rop.node_path, [])
            if folder not in dirs:
                dirs.append(folder)
            self._set_status(
                f"Textures will be relinked from {len(dirs)} folder(s) when the "
                f"render starts (Hython composes the repaths into the LOP network).")
            self.refresh_command()
            return

        base = self._base_usd(rop)
        if not base:
            QMessageBox.information(
                self, "Nothing to relink",
                "The husk engine relinks the exported USD, so read the scene "
                "with “Write USD while reading” enabled first.\n\n"
                "The Hython engine does not need it — it repaths inside the "
                "LOP network at render time.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder to search for missing textures",
            os.path.expanduser("~"))
        if not folder:
            return

        self.relink_btn.setEnabled(False)
        self._set_status("Relinking textures in hython…")
        self._relink_thread = QThread(self)
        self._relink_worker = RelinkWorker(base, [folder],
                                           hython=self.hython_edit.text().strip())
        self._relink_worker.moveToThread(self._relink_thread)
        self._relink_thread.started.connect(self._relink_worker.run)
        self._relink_worker.finished.connect(self._on_relinked)
        self._relink_worker.failed.connect(self._on_relink_failed)
        self._relink_worker.finished.connect(self._relink_thread.quit)
        self._relink_worker.failed.connect(self._relink_thread.quit)
        self._relink_thread.start()

    @Slot(object)
    def _on_relinked(self, result: dict) -> None:
        self.relink_btn.setEnabled(True)
        rop = self.current_rop()
        out = result.get("usd_out")
        relinked = result.get("relinked", [])
        still = result.get("still_missing", [])
        if out and rop:
            self._relinked_usd[rop.node_path] = out
        # Drop the now-resolved assets so preflight stops flagging them.
        if self.manifest and relinked:
            done = {r.get("old") for r in relinked}
            self.manifest.missing_assets = [
                a for a in self.manifest.missing_assets if a.asset_path not in done]
        msg = f"Relinked {len(relinked)} texture(s); {len(still)} still missing."
        self._set_status(msg, "warning" if still else "info")
        if still:
            QMessageBox.warning(
                self, "Some textures still missing",
                msg + "\n\n" + "\n".join(s.get("path", "") for s in still[:20]))
        self.refresh_command()

    @Slot(str)
    def _on_relink_failed(self, message: str) -> None:
        self.relink_btn.setEnabled(True)
        self._set_status("Relink failed.", "error")
        QMessageBox.critical(self, "Could not relink textures", message[-4000:])

    # -- rendering --------------------------------------------------------

    @Slot()
    def start_render(self) -> None:
        ticked = self._ticked_render_rops()
        if not ticked:
            detail = ("Tick at least one ROP to render."
                      if self._multi_rop_scene()
                      else "Read a scene and choose a render ROP first.")
            QMessageBox.information(self, "Nothing to render", detail)
            return

        engine = self._engine()
        if engine == "husk":
            no_usd = [r.node_path for r in ticked if not self._base_usd(r)]
            if no_usd:
                # Only the husk engine needs a USD on disk; saying so
                # unconditionally would send a hython user chasing an export
                # they do not need. Naming which ROP(s) matters once there is
                # more than one to tell apart.
                detail = ("Read a scene with “Write USD while reading” enabled "
                          "first — the husk engine renders an exported USD.")
                if len(ticked) > 1:
                    detail += "\n\nNo USD on disk for:\n" + "\n".join(no_usd)
                QMessageBox.information(self, "Nothing to render", detail)
                return

        jobs = self.build_render_jobs()
        if not jobs:
            QMessageBox.information(self, "Nothing to render",
                                    "Read a scene and choose a render ROP first.")
            return

        if engine == "husk" and not husk_mod.find_husk():
            QMessageBox.warning(
                self, "husk not found",
                "husk is not on PATH and $HFS is not set.\n\n"
                "Source houdini_setup, or set $HSL_HUSK to the husk binary.",
            )
            return

        # AOV selection and render-setting overrides are USD edits, not husk
        # flags, and apply only to the ROP the combo targets (see build_jobs).
        # On husk they are authored in a worker thread first and the queue runs
        # against the result — never freeze the UI on a hython subprocess.
        # hython needs neither: it carries both on its command line. If the
        # ROP they would apply to is not even ticked to render, there is
        # nothing to prepare -- skip straight to launching the ticked ones.
        current = self.current_rop()
        current_is_ticked = current is not None and any(
            r.node_path == current.node_path for r in ticked)
        keep = self._selected_aov_paths() if engine == "husk" and current_is_ticked else None
        overrides = self._setting_overrides() if engine == "husk" and current_is_ticked else {}
        if keep is not None or overrides:
            self.render_btn.setEnabled(False)
            what = "render settings" if overrides and keep is None else (
                "AOVs" if keep is not None and not overrides else
                "render settings and AOVs")
            self._set_status(f"Applying {what} in hython…")
            settings_prim = self.settings_combo.currentData()
            self._filter_thread = QThread(self)
            self._filter_worker = PrepareWorker(
                self._base_usd(self.current_rop()),
                settings_overrides=overrides, keep_paths=keep,
                settings_prim=settings_prim or "",
                hython=self.hython_edit.text().strip())
            self._filter_worker.moveToThread(self._filter_thread)
            self._filter_thread.started.connect(self._filter_worker.run)
            self._filter_worker.finished.connect(self._on_prepared)
            self._filter_worker.failed.connect(self._on_prepare_failed)
            self._filter_worker.finished.connect(self._filter_thread.quit)
            self._filter_worker.failed.connect(self._filter_thread.quit)
            self._filter_thread.start()
            return

        self._launch_queue(jobs)

    @Slot(str)
    def _on_prepared(self, prepared_usd: str) -> None:
        jobs = self.build_render_jobs(current_usd_override=prepared_usd)
        if not jobs:
            self._on_prepare_failed("The prepared USD produced no render jobs.")
            return
        self._launch_queue(jobs)

    @Slot(str)
    def _on_prepare_failed(self, message: str) -> None:
        self._running = False
        self._refresh_primary_action()
        self._set_status("Preparing the USD failed.", "error")
        QMessageBox.critical(self, "Could not prepare the render", message[-4000:])

    def _launch_queue(self, jobs: list, verb: str = "Rendering") -> None:
        self.log_view.clear()
        self.overall_bar.setValue(0)
        self.overall_bar.setVisible(True)
        self._progress_seen = set()

        self.bridge = QueueBridge()
        self.bridge.task_started.connect(self.on_task_started)
        self.bridge.task_output.connect(self.on_task_output)
        self.bridge.task_progress.connect(self.on_task_progress)
        self.bridge.task_finished.connect(self.on_task_finished)
        self.bridge.queue_finished.connect(self.on_queue_finished)

        try:
            self.queue = RenderQueue(jobs, max_parallel=self.parallel_spin.value(),
                                     on_event=self.bridge.dispatch)
        except ValueError as exc:
            # A dependency cycle in the scene: nothing can ever be scheduled.
            self.queue = None
            self._set_status(str(exc), "error")
            QMessageBox.critical(self, "Cannot build the queue", str(exc))
            return

        for warning in self.queue.warnings:
            self.log_view.appendPlainText(f"warning: {warning}")

        self.tasks = self.queue.tasks
        self._populate_task_table()
        self._set_progress_bar_text(self._progress_summary())

        self._running = True
        self._refresh_primary_action()
        self.cancel_btn.setEnabled(True)
        self._set_status(f"{verb} {len(jobs)} chunk(s).")
        self.queue.start()

    @Slot()
    def cancel_render(self) -> None:
        if self.queue:
            self.queue.cancel()
            self._set_status("Cancelling…")

    def _populate_task_table(self) -> None:
        self.task_table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            self.task_table.setItem(row, 0, QTableWidgetItem(str(task.job.chunk)))
            self.task_table.setItem(row, 1, QTableWidgetItem(task.state.value))
            self.task_table.setItem(row, 2, QTableWidgetItem(self._task_progress_text(task)))
            self.task_table.setItem(row, 3, QTableWidgetItem("—"))

    def _row_for(self, task: Task) -> int:
        try:
            return self.tasks.index(task)
        except ValueError:
            return -1

    def _task_progress_text(self, task: Task) -> str:
        """What the Progress column says for one chunk.

        Gathers this window's own bookkeeping (which tasks have actually
        reported a percentage) and hands off to hsl.progress -- plain stdlib
        logic kept out of this Qt-only, import-check-only module so it can
        have a test.
        """
        return progress.task_progress_text(task, self._progress_seen)

    def _progress_summary(self) -> str:
        """"Frame N of M" across the whole queue, plus an ETA. See
        hsl.progress.queue_progress_summary for how -- kept there rather than
        here so it can be tested without Qt installed.
        """
        percent = self.queue.progress if self.queue else 0
        return progress.queue_progress_summary(self.tasks, percent, self._progress_seen)

    def _set_progress_bar_text(self, text: str) -> None:
        """QProgressBar only treats %p/%v/%m as tokens -- a lone '%' (what our
        own percentages produce) displays as itself, so nothing needs escaping."""
        self.overall_bar.setFormat(text or "%p%")

    def _update_row(self, task: Task) -> None:
        row = self._row_for(task)
        if row < 0:
            return
        self.task_table.item(row, 1).setText(task.state.value)
        self.task_table.item(row, 2).setText(self._task_progress_text(task))
        self.task_table.item(row, 3).setText(f"{task.duration:.0f}s")
        if self.queue:
            self.overall_bar.setValue(self.queue.progress)
            self._set_progress_bar_text(self._progress_summary())

    @Slot(object)
    def on_task_started(self, task: Task) -> None:
        self._update_row(task)
        self.log_view.appendPlainText(f"--- {task.job.label} ---")

    @Slot(object, str)
    def on_task_output(self, task: Task, line: str) -> None:
        self.log_view.appendPlainText(line)

    @Slot(object, int)
    def on_task_progress(self, task: Task, _percent: int) -> None:
        self._progress_seen.add(id(task))
        self._update_row(task)

    @Slot(object)
    def on_task_finished(self, task: Task) -> None:
        self._update_row(task)
        if task.state is State.FAILED:
            self.log_view.appendPlainText(
                f"!!! {task.job.label} failed with exit code {task.returncode}"
            )

    @Slot(object)
    def on_queue_finished(self, queue: RenderQueue) -> None:
        self._running = False
        self._refresh_primary_action()
        self.cancel_btn.setEnabled(False)
        failed = sum(1 for t in queue.tasks if t.state is State.FAILED)
        # Skipped chunks never ran -- something they depend on did not finish.
        # Reporting only "done" would claim a complete render that isn't one.
        skipped = sum(1 for t in queue.tasks if t.state is State.SKIPPED)
        done = sum(1 for t in queue.tasks if t.state is State.DONE)
        self.overall_bar.setValue(100)
        self._set_progress_bar_text(self._progress_summary())
        if failed or skipped:
            trailer = f"{failed} failed" if failed else ""
            if skipped:
                trailer += f"{', ' if trailer else ''}{skipped} skipped"
            self._set_status(f"{done} chunk(s) rendered, {trailer}.", "error")
        else:
            self._set_status(f"{done} chunk(s) rendered.")


def _claim_windows_taskbar_identity() -> None:
    """Tell the Windows shell this process is its own application.

    Without it the launcher is just another window belonging to python.exe (or
    hython.exe), so the taskbar shows the interpreter's icon and groups us with
    any other Python window. Setting the icon in Qt alone does not fix that.

    Has to happen before the first window exists -- the shell reads the id when
    the window is created, not when it is shown. Best-effort: an old shell32 or
    a non-Windows host just means we keep the default identity.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except (ImportError, AttributeError, OSError):
        pass


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _claim_windows_taskbar_identity()
    app = QApplication(argv)
    app.setApplicationName("Solaris Render Launcher")
    icon = QIcon(resources.icon_path())
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = LauncherWindow()
    if len(argv) > 1 and argv[1].endswith((".hip", ".hipnc", ".hiplc")):
        window.hip_edit.setText(argv[1])
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
