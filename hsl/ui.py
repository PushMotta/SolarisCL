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

from . import bridge, farm, husk as husk_mod, preflight, presets, resources
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
            self.hython_combo.addItem(f"{label}: {path}", path)
        self.hython_combo.addItem("Custom path...", "")

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
        hython_row.addWidget(self.hython_edit, 2)
        hython_row.addWidget(self.hython_rescan_btn)
        hython_row.addWidget(self.hython_browse_btn)
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

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self._build_rop_panel(), 1)

        # Tabbed so the render-settings editor has room without pushing the
        # window taller — and so it is visible rather than buried in a form.
        right = QTabWidget()
        right.addTab(self._build_override_panel(), "Overrides")
        right.addTab(self._build_settings_panel(), "Render settings")
        top_layout.addWidget(right, 1)
        splitter.addWidget(top)

        splitter.addWidget(self._build_run_panel())
        splitter.setSizes([420, 380])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self._build_menu()

    def _build_rop_panel(self) -> QWidget:
        # "&&" because Qt reads a single & as a keyboard accelerator and eats it,
        # which is why these titles used to render with the word missing.
        box = QGroupBox("Render ROPs && AOV Manager")
        layout = QVBoxLayout(box)

        self.rop_combo = QComboBox()
        layout.addWidget(self.rop_combo)

        # AOV Manager section
        aov_box = QGroupBox("AOV Manager (Check to include)")
        aov_layout = QVBoxLayout(aov_box)

        self.aov_list = QListWidget()
        self.aov_list.setMaximumHeight(130)
        aov_layout.addWidget(self.aov_list)

        aov_btn_row = QHBoxLayout()
        self.aov_all_btn = QPushButton("Select All")
        self.aov_beauty_btn = QPushButton("Beauty + Depth")
        self.aov_none_btn = QPushButton("Clear All")
        aov_btn_row.addWidget(self.aov_all_btn)
        aov_btn_row.addWidget(self.aov_beauty_btn)
        aov_btn_row.addWidget(self.aov_none_btn)
        aov_layout.addLayout(aov_btn_row)

        layout.addWidget(aov_box)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont(MONO, 9))
        self.detail.setPlaceholderText(
            "Read a scene to see its render settings, camera and AOVs."
        )
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
        box = QGroupBox("Overrides && Quality Presets")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom Configuration", "")
        for p_name in presets.get_default_presets().keys():
            self.preset_combo.addItem(p_name, p_name)
        form.addRow("Render Profile", self.preset_combo)

        # Hython first: it is the default engine. It renders the ROP directly,
        # so it needs no USD export -- which on a volume-heavy scene would bake
        # tens of GB per frame. The token lives in the item data so the rest of
        # the window never depends on the order of this list.
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Hython (direct ROP, no USD export)", "hython")
        self.engine_combo.addItem("Husk (USD export — needed for AOV filter, relink, farm)", "husk")
        form.addRow("Render engine", self.engine_combo)

        self.renderer_combo = QComboBox()
        self.renderer_combo.setEditable(True)
        form.addRow("Renderer", self.renderer_combo)

        self.settings_combo = QComboBox()
        form.addRow("Render settings", self.settings_combo)

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

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(0, 10000)
        self.chunk_spin.setSpecialValueText("all in one")
        self.chunk_spin.setToolTip("Frames per husk process.")
        form.addRow("Chunk size", self.chunk_spin)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 64)
        self.parallel_spin.setValue(1)
        self.parallel_spin.setToolTip("How many husk processes run at once.")
        form.addRow("Concurrent renders", self.parallel_spin)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 512)
        self.threads_spin.setSpecialValueText("all cores")
        form.addRow("Threads per render", self.threads_spin)

        self.snapshot_spin = QSpinBox()
        self.snapshot_spin.setRange(0, 3600)
        self.snapshot_spin.setSpecialValueText("off")
        self.snapshot_spin.setSuffix(" s")
        self.snapshot_spin.setToolTip(
            "Flush a partial image this often so you can check progress."
        )
        form.addRow("Snapshot every", self.snapshot_spin)

        self.verbosity_edit = QLineEdit("3")
        self.verbosity_edit.setToolTip("husk verbosity level. 'a' is appended for progress.")
        form.addRow("Verbosity", self.verbosity_edit)

        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("--disable-motionblur --complexity high")
        form.addRow("Extra flags", self.extra_edit)

        return box

    def _build_run_panel(self) -> QWidget:
        box = QGroupBox("Render && Diagnostics")
        layout = QVBoxLayout(box)

        self.preflight_label = QLabel("Preflight: Ready.")
        self.preflight_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        layout.addWidget(self.preflight_label)

        self.command_view = QPlainTextEdit()
        self.command_view.setReadOnly(True)
        self.command_view.setFont(QFont(MONO, 9))
        self.command_view.setMaximumHeight(80)
        self.command_view.setPlaceholderText("The husk command appears here.")
        layout.addWidget(self.command_view)

        button_row = QHBoxLayout()
        self.render_btn = QPushButton("Start render")
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
        button_row.addWidget(self.render_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.copy_btn)
        button_row.addWidget(self.relink_btn)
        button_row.addWidget(self.farm_btn)
        button_row.addWidget(self.overall_bar, 1)
        layout.addLayout(button_row)

        self.tabs = QTabWidget()

        # Tab 1: Queue & Logs
        queue_widget = QWidget()
        q_layout = QVBoxLayout(queue_widget)
        q_layout.setContentsMargins(0, 0, 0, 0)
        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(["Frames", "State", "Progress", "Time"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.setMaximumHeight(140)
        q_layout.addWidget(self.task_table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(MONO, 9))
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("husk output appears here.")
        q_layout.addWidget(self.log_view, 1)
        self.tabs.addTab(queue_widget, "Queue & Logs")
        self.tabs.addTab(self._build_tasks_panel(), "Caches & Sims")

        layout.addWidget(self.tabs, 1)
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

        row = QHBoxLayout()
        self.cook_btn = QPushButton("Cook Ticked")
        self.cook_btn.setEnabled(False)
        self.cook_btn.setToolTip(
            "Cook the ticked caches and simulations. Dependencies come from "
            "the scene, so order is handled for you."
        )
        self.cook_btn.clicked.connect(self.cook_selected)
        self.cook_hint = QLabel("")
        self.cook_hint.setWordWrap(True)
        row.addWidget(self.cook_btn)
        row.addWidget(self.cook_hint, 1)
        layout.addLayout(row)
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

        self.cook_btn.setEnabled(bool(tasks))
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

    @Slot()
    def cook_selected(self) -> None:
        if not self.manifest:
            return
        chosen = []
        for row in range(self.task_list.rowCount()):
            item = self.task_list.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                task = self.manifest.task(item.data(Qt.UserRole))
                if task is not None:
                    chosen.append(task)

        if not chosen:
            self._set_status("Tick at least one cache or simulation.", "error")
            return

        hython = self.hython_combo.currentData() or ""
        jobs = []
        for task in chosen:
            jobs += husk_mod.jobs_for_task(
                self.manifest, task,
                chunk_size=self.chunk_spin.value(),
                hython_exe=hython or None,
            )
        self._launch_queue(jobs, verb="Cooking")

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
        self.render_btn.clicked.connect(self.start_render)
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
        for widget in (self.rop_combo, self.render_btn, self.copy_btn):
            widget.setEnabled(loaded)

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

    @Slot()
    def export_farm_job(self) -> None:
        jobs = self.build_jobs()
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

        if choice.endswith(".alf"):
            farm.export_tractor_job(jobs, choice)
            QMessageBox.information(self, "Tractor Job Exported", f"Exported Tractor job script:\n{choice}")
        else:
            out_dir = os.path.dirname(choice) or os.getcwd()
            j_path, p_path = farm.export_deadline_job(jobs, out_dir)
            QMessageBox.information(self, "Deadline Job Exported", f"Exported Deadline job files:\n{j_path}\n{p_path}")

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

        self._set_scene_loaded(bool(manifest.rops))
        if manifest.rops:
            self.on_rop_changed(0)
        else:
            self.detail.setPlainText(
                "No USD Render ROPs in /stage or /out.\n\n"
                "Add a USD Render ROP at the end of the LOP network, save, "
                "and read the scene again."
            )

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
        self.refresh_command()

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

    def build_jobs(self, usd_override: Optional[str] = None) -> list:
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
            "output": self.output_edit.text().strip(),
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
        jobs = self.build_jobs()
        if not jobs:
            rop = self.current_rop()
            engine = self._engine()
            if rop and engine == "husk" and not rop.usd_path:
                self.command_view.setPlainText(
                    "No USD on disk for this ROP. Turn on “Write USD while "
                    "reading” and read the scene again, or switch to Hython engine."
                )
            else:
                self.command_view.setPlainText("")
            self.preflight_label.setStyleSheet("color: palette(mid);")
            self.preflight_label.setText("Preflight: No active job.")
            return

        preview = husk_mod.format_command(husk_mod.build_command(jobs[0]))
        if len(jobs) > 1:
            preview += f"\n\n… and {len(jobs) - 1} more chunk(s) with different --frame values."
        engine = self._engine()
        if engine == "husk" and self._selected_aov_paths() is not None:
            preview += ("\n\n# AOVs will be filtered to your selection via a USD "
                        "overlay authored in hython at render start.")
        if engine == "hython" and jobs[0].relink_dirs:
            preview += ("\n\n# Missing textures will be repathed from the folder(s) "
                        "above and composed into the LOP network at render start.")
        if engine == "husk" and self._setting_overrides():
            listed = ", ".join(f"{k}={v}" for k, v in self._setting_overrides().items())
            preview += (f"\n\n# Render settings ({listed}) will be authored into a "
                        f"USD overlay at render start — husk has no flag for them.")

        # Resolved filenames, so the output path can be checked before starting
        # a render rather than after it lands somewhere unexpected.
        rop = self.current_rop()
        frames = [f for job in jobs
                  for f in (job.chunk.start + i * job.chunk.inc
                            for i in range(job.chunk.count))]
        planned = husk_mod.planned_outputs(
            self.manifest, rop, output=self.output_edit.text().strip(), frames=frames)
        if planned:
            lines = ["", "# Files this render will write:"]
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
            preview += "\n" + "\n".join(lines)
        else:
            preview += ("\n\n# No output path declared in the scene — husk or the "
                        "ROP decides it. Set Output above to choose.")
        self.command_view.setPlainText(preview)

        pf_warnings = preflight.run_preflight_checks(jobs[0], self.manifest)
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
        jobs = self.build_jobs()
        if not jobs:
            # Only the husk engine needs a USD on disk; saying so unconditionally
            # would send a hython user chasing an export they do not need.
            detail = ("Read a scene with “Write USD while reading” enabled first — "
                      "the husk engine renders an exported USD."
                      if self._engine() == "husk"
                      else "Read a scene and choose a render ROP first.")
            QMessageBox.information(self, "Nothing to render", detail)
            return

        engine = self._engine()
        if engine == "husk" and not husk_mod.find_husk():
            QMessageBox.warning(
                self, "husk not found",
                "husk is not on PATH and $HFS is not set.\n\n"
                "Source houdini_setup, or set $HSL_HUSK to the husk binary.",
            )
            return

        # AOV selection and render-setting overrides are USD edits, not husk
        # flags. On husk they are authored in a worker thread first and the queue
        # runs against the result — never freeze the UI on a hython subprocess.
        # hython needs neither: it carries both on its command line.
        keep = self._selected_aov_paths() if engine == "husk" else None
        overrides = self._setting_overrides() if engine == "husk" else {}
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
        jobs = self.build_jobs(usd_override=prepared_usd)
        if not jobs:
            self._on_prepare_failed("The prepared USD produced no render jobs.")
            return
        self._launch_queue(jobs)

    @Slot(str)
    def _on_prepare_failed(self, message: str) -> None:
        self.render_btn.setEnabled(True)
        self._set_status("Preparing the USD failed.", "error")
        QMessageBox.critical(self, "Could not prepare the render", message[-4000:])

    def _launch_queue(self, jobs: list, verb: str = "Rendering") -> None:
        self.log_view.clear()
        self.overall_bar.setValue(0)

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

        self.render_btn.setEnabled(False)
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
            self.task_table.setItem(row, 2, QTableWidgetItem("0%"))
            self.task_table.setItem(row, 3, QTableWidgetItem("—"))

    def _row_for(self, task: Task) -> int:
        try:
            return self.tasks.index(task)
        except ValueError:
            return -1

    def _update_row(self, task: Task) -> None:
        row = self._row_for(task)
        if row < 0:
            return
        self.task_table.item(row, 1).setText(task.state.value)
        self.task_table.item(row, 2).setText(f"{task.progress}%")
        self.task_table.item(row, 3).setText(f"{task.duration:.0f}s")
        if self.queue:
            self.overall_bar.setValue(self.queue.progress)

    @Slot(object)
    def on_task_started(self, task: Task) -> None:
        self._update_row(task)
        self.log_view.appendPlainText(f"--- {task.job.label} ---")

    @Slot(object, str)
    def on_task_output(self, task: Task, line: str) -> None:
        self.log_view.appendPlainText(line)

    @Slot(object, int)
    def on_task_progress(self, task: Task, _percent: int) -> None:
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
        self.render_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        failed = sum(1 for t in queue.tasks if t.state is State.FAILED)
        # Skipped chunks never ran -- something they depend on did not finish.
        # Reporting only "done" would claim a complete render that isn't one.
        skipped = sum(1 for t in queue.tasks if t.state is State.SKIPPED)
        done = sum(1 for t in queue.tasks if t.state is State.DONE)
        self.overall_bar.setValue(100)
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
