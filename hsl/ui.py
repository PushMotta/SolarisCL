"""PySide6 launcher window.

Runs in ordinary Python -- it never imports ``hou``. Scene reading happens in
a worker thread that shells out to hython via :mod:`hsl.bridge`.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import bridge, husk as husk_mod
from .manifest import RenderRop, SceneManifest
from .runner import RenderQueue, State, Task

MONO = "Menlo" if sys.platform == "darwin" else ("Consolas" if os.name == "nt" else "DejaVu Sans Mono")


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


class QueueBridge(QObject):
    """Marshals RenderQueue callbacks onto the Qt event loop."""
    task_started = Signal(object)
    task_output = Signal(object, str)
    task_progress = Signal(object, int)
    task_finished = Signal(object)
    queue_finished = Signal(object)

    def dispatch(self, event: str, *args) -> None:
        getattr(self, event).emit(*args)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Solaris Render Launcher")
        self.resize(1180, 820)

        self.manifest: Optional[SceneManifest] = None
        self.queue: Optional[RenderQueue] = None
        self.tasks: list[Task] = []
        self._thread: Optional[QThread] = None
        self._worker: Optional[InspectWorker] = None

        self._build_ui()
        self._connect()
        self._set_scene_loaded(False)

    # -- construction -----------------------------------------------------

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

        opts_row = QHBoxLayout()
        self.export_usd_check = QCheckBox("Write USD while reading")
        self.export_usd_check.setChecked(True)
        self.export_usd_check.setToolTip(
            "husk renders a USD file, not a .hip. Leave this on unless the "
            "stage is already on disk."
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
        self.status_label.setStyleSheet("color: palette(mid);")
        opts_row.addWidget(self.status_label)
        outer.addLayout(opts_row)

        # --- middle splitter ---
        splitter = QSplitter(Qt.Vertical)

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self._build_rop_panel(), 1)
        top_layout.addWidget(self._build_override_panel(), 1)
        splitter.addWidget(top)

        splitter.addWidget(self._build_run_panel())
        splitter.setSizes([420, 380])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self._build_menu()

    def _build_rop_panel(self) -> QWidget:
        box = QGroupBox("Render ROPs")
        layout = QVBoxLayout(box)

        self.rop_combo = QComboBox()
        layout.addWidget(self.rop_combo)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont(MONO, 10))
        self.detail.setPlaceholderText(
            "Read a scene to see its render settings, camera and AOVs."
        )
        layout.addWidget(self.detail, 1)
        return box

    def _build_override_panel(self) -> QWidget:
        box = QGroupBox("Overrides")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

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

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Leave empty to use the product name from USD")
        form.addRow("Output", self.output_edit)

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
        box = QGroupBox("Render")
        layout = QVBoxLayout(box)

        self.command_view = QPlainTextEdit()
        self.command_view.setReadOnly(True)
        self.command_view.setFont(QFont(MONO, 9))
        self.command_view.setMaximumHeight(90)
        self.command_view.setPlaceholderText("The husk command appears here.")
        layout.addWidget(self.command_view)

        button_row = QHBoxLayout()
        self.render_btn = QPushButton("Start render")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.copy_btn = QPushButton("Copy command")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        button_row.addWidget(self.render_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.copy_btn)
        button_row.addWidget(self.overall_bar, 1)
        layout.addLayout(button_row)

        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(["Frames", "State", "Progress", "Time"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.setMaximumHeight(150)
        layout.addWidget(self.task_table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(MONO, 9))
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("husk output appears here.")
        layout.addWidget(self.log_view, 1)
        return box

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
        self.rop_combo.currentIndexChanged.connect(self.on_rop_changed)
        self.res_check.toggled.connect(self.res_x.setEnabled)
        self.res_check.toggled.connect(self.res_y.setEnabled)
        self.copy_btn.clicked.connect(self.copy_command)
        self.render_btn.clicked.connect(self.start_render)
        self.cancel_btn.clicked.connect(self.cancel_render)

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
    def read_scene(self) -> None:
        hip_path = self.hip_edit.text().strip()
        if not hip_path:
            self.status_label.setText("Choose a .hip file first.")
            return
        if not os.path.isfile(hip_path):
            QMessageBox.warning(self, "Scene not found",
                                f"There is no file at:\n{hip_path}")
            return

        self.read_btn.setEnabled(False)
        self.status_label.setText("Reading scene in hython. Large scenes take a while…")

        self._thread = QThread(self)
        self._worker = InspectWorker(
            hip_path,
            export_usd=self.export_usd_check.isChecked(),
            flatten=self.flatten_check.isChecked(),
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

        self.rop_combo.blockSignals(True)
        self.rop_combo.clear()
        for rop in manifest.rops:
            self.rop_combo.addItem(rop.node_path, rop.node_path)
        self.rop_combo.blockSignals(False)

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
        if manifest.warnings:
            found += f" — {len(manifest.warnings)} warning(s)"
        self.status_label.setText(found)

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
        self.status_label.setText("Reading the scene failed.")
        QMessageBox.critical(self, "Could not read the scene", message[-4000:])

    # -- ROP selection ----------------------------------------------------

    def current_rop(self) -> Optional[RenderRop]:
        if not self.manifest:
            return None
        return self.manifest.rop(self.rop_combo.currentData() or "")

    @Slot(int)
    def on_rop_changed(self, _index: int) -> None:
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

    def build_jobs(self) -> list:
        rop = self.current_rop()
        if not rop or not self.manifest:
            return []

        usd_file = rop.usd_path
        if not usd_file:
            return []

        settings_prim = self.settings_combo.currentData()
        if settings_prim is None:
            settings_prim = self.settings_combo.currentText()

        overrides = {
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
            if rop and not rop.usd_path:
                self.command_view.setPlainText(
                    "No USD on disk for this ROP. Turn on “Write USD while "
                    "reading” and read the scene again."
                )
            else:
                self.command_view.setPlainText("")
            return

        preview = husk_mod.format_command(husk_mod.build_command(jobs[0]))
        if len(jobs) > 1:
            preview += f"\n\n… and {len(jobs) - 1} more chunk(s) with different --frame values."
        self.command_view.setPlainText(preview)

    @Slot()
    def copy_command(self) -> None:
        QApplication.clipboard().setText(self.command_view.toPlainText())
        self.status_label.setText("Command copied.")

    # -- rendering --------------------------------------------------------

    @Slot()
    def start_render(self) -> None:
        jobs = self.build_jobs()
        if not jobs:
            QMessageBox.information(
                self, "Nothing to render",
                "Read a scene with “Write USD while reading” enabled first.",
            )
            return

        if not husk_mod.find_husk():
            QMessageBox.warning(
                self, "husk not found",
                "husk is not on PATH and $HFS is not set.\n\n"
                "Source houdini_setup, or set $HSL_HUSK to the husk binary.",
            )
            return

        self.log_view.clear()
        self.overall_bar.setValue(0)

        self.bridge = QueueBridge()
        self.bridge.task_started.connect(self.on_task_started)
        self.bridge.task_output.connect(self.on_task_output)
        self.bridge.task_progress.connect(self.on_task_progress)
        self.bridge.task_finished.connect(self.on_task_finished)
        self.bridge.queue_finished.connect(self.on_queue_finished)

        self.queue = RenderQueue(jobs, max_parallel=self.parallel_spin.value(),
                                 on_event=self.bridge.dispatch)
        self.tasks = self.queue.tasks
        self._populate_task_table()

        self.render_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText(f"Rendering {len(jobs)} chunk(s).")
        self.queue.start()

    @Slot()
    def cancel_render(self) -> None:
        if self.queue:
            self.queue.cancel()
            self.status_label.setText("Cancelling…")

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
        done = sum(1 for t in queue.tasks if t.state is State.DONE)
        self.overall_bar.setValue(100)
        if failed:
            self.status_label.setText(f"{done} chunk(s) rendered, {failed} failed.")
        else:
            self.status_label.setText(f"{done} chunk(s) rendered.")


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("Solaris Render Launcher")
    window = LauncherWindow()
    if len(argv) > 1 and argv[1].endswith((".hip", ".hipnc", ".hiplc")):
        window.hip_edit.setText(argv[1])
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
