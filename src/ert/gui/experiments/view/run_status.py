from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from PyQt6.QtCore import QModelIndex, QObject, Qt, QThread
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtCore import pyqtSlot as Slot
from PyQt6.QtGui import QHideEvent
from PyQt6.QtWidgets import (
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from typing_extensions import override

from ert.ensemble_evaluator.event import FullSnapshotEvent
from ert.gui.experiments.run_dialog import FMStepOverview
from ert.gui.experiments.view.progress_widget import ProgressWidget
from ert.gui.experiments.view.realization import RealizationWidget
from ert.gui.model.real_list import RealListModel
from ert.gui.model.snapshot import SnapshotModel
from ert.run_models.event import load_status_snapshot_event

logger = logging.getLogger(__name__)


class _SnapshotLoader(QObject):
    loaded = Signal(int, object)

    def __init__(self, load_id: int, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._load_id = load_id
        self._path = path

    @Slot()
    def run(self) -> None:
        event = load_status_snapshot_event(self._path)
        if event is not None and event.snapshot is not None:
            SnapshotModel.prerender(event.snapshot)
        self.loaded.emit(self._load_id, event)


class RunStatusView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._stack = QStackedWidget(self)

        self._placeholder = QLabel(
            "No run status snapshot is available for this ensemble.", self
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._placeholder)

        self._loading_label = QLabel("Loading…", self)
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._loading_label)

        self._content: QWidget | None = None
        self._loaded_path: Path | None = None
        self._next_load_id = 0
        self._current_load_id: int | None = None
        self._load_jobs: dict[int, tuple[QThread, _SnapshotLoader]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    def load_snapshot(self, path: Path) -> None:
        """Start loading the snapshot at *path* on a background thread.

        Re-requesting the path that is already loaded (or loading) is a
        no-op, so re-activating the tab for the same ensemble does not
        trigger a redundant disk read and re-render.
        """
        if path == self._loaded_path:
            return
        self._loaded_path = path
        self._cancel_pending_load()
        if self._content is not None:
            self._stack.removeWidget(self._content)
            self._content.deleteLater()
            self._content = None

        self._stack.setCurrentWidget(self._loading_label)

        self._next_load_id += 1
        load_id = self._next_load_id
        self._current_load_id = load_id

        loader = _SnapshotLoader(load_id, path)
        thread = QThread(parent=self)
        loader.moveToThread(thread)
        loader.loaded.connect(self._on_loaded)
        loader.loaded.connect(thread.quit)
        loader.loaded.connect(loader.deleteLater)
        thread.started.connect(loader.run)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda load_id=load_id: self._cleanup_load(load_id))
        self._load_jobs[load_id] = (thread, loader)
        thread.start()

    @Slot(int, object)
    def _on_loaded(self, load_id: int, event: FullSnapshotEvent | None) -> None:
        if load_id != self._current_load_id:
            return
        self._current_load_id = None
        if event is None or event.snapshot is None:
            self._stack.setCurrentWidget(self._placeholder)
            return
        self._content = self._build_content(event)
        self._stack.addWidget(self._content)
        self._stack.setCurrentWidget(self._content)

    def _build_content(self, event: FullSnapshotEvent) -> QWidget:
        content = QWidget(self)
        snapshot_model = SnapshotModel(content)
        snapshot = event.snapshot
        assert snapshot is not None
        snapshot_model._add_snapshot(snapshot, str(event.iteration))

        fm_step_overview = FMStepOverview(snapshot_model, self)
        fm_step_label = QLabel(self)

        realization_widget = RealizationWidget(0)
        realization_widget.setSnapshotModel(snapshot_model)

        def select_real(index: QModelIndex) -> None:
            if not index.isValid():
                return
            iter_ = cast(RealListModel, index.model()).get_iter()
            fm_step_overview.set_realization(iter_, index.row())
            fm_step_label.setText(
                f"Realization id {index.row()} in iteration {event.iteration}"
            )

        realization_widget.itemClicked.connect(select_real)

        progress_widget = ProgressWidget()
        progress_widget.update_progress(event.status_count, event.realization_count)

        content_layout = QVBoxLayout(content)
        content_layout.addWidget(progress_widget)
        content_layout.addWidget(realization_widget)
        content_layout.addWidget(fm_step_label)
        content_layout.addWidget(fm_step_overview)

        first_real = realization_widget._real_list_model.index(0, 0)
        if first_real.isValid():
            select_real(first_real)

        return content

    def _cancel_pending_load(self) -> None:
        if self._current_load_id is None:
            return
        load_id = self._current_load_id
        self._current_load_id = None
        thread, _ = self._load_jobs.get(load_id, (None, None))
        if thread is not None and thread.isRunning():
            thread.quit()

    def _cleanup_load(self, load_id: int) -> None:
        self._load_jobs.pop(load_id, None)

    @override
    def hideEvent(self, event: QHideEvent | None) -> None:
        self._cancel_pending_load()
        super().hideEvent(event)
