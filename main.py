from __future__ import annotations

import math
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QEvent, Qt, QTimer, QSize
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png"}
FPS_OPTIONS = (24, 25, 30, 60)
CACHE_LIMIT = 48
WATCH_INTERVAL_MS = 15_000
WATCH_IDLE_AFTER_SECONDS = 120
DISPLAY_SIZE_OPTIONS = (
    ("Fit Window", None),
    ("1280 x 720", (1280, 720)),
    ("1920 x 1080", (1920, 1080)),
)


def natural_sort_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.casefold())
    return [int(part) if part.isdigit() else part for part in parts]


@dataclass
class Clip:
    name: str
    folder: Path | None = None
    frames: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineFrame:
    clip_index: int
    clip_frame_index: int
    path: Path


class FrameView(QLabel):
    def __init__(self, on_resize) -> None:
        super().__init__()
        self._on_resize = on_resize
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(480, 270))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            "QLabel { background: #000000; color: #d6d6d6; border: 1px solid #232323; }"
        )
        self.setWordWrap(True)
        self.setText("Select one or more folders to begin.")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._on_resize()


class SequenceStitchPlayer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sequence Stitch Player")
        self.resize(1280, 820)

        self.clips = [Clip("A"), Clip("B"), Clip("C"), Clip("D")]
        self.timeline: list[TimelineFrame] = []
        self.current_frame_index = 0
        self.paused_frame_index = 0
        self.playback_started_at = 0.0
        self.is_playing = False
        self.loop_enabled = False
        self.fps = 30
        self.display_size: tuple[int, int] | None = None
        self.last_error_message = ""
        self.dropped_frames = 0
        self.last_playback_raw_index = 0
        self.updating_slider = False
        self.folder_snapshot: tuple[tuple[str, int, int], ...] = ()
        self.folder_watch_status = "Clean"
        self.inactive_started_at: float | None = None
        self.cache_all_enabled = False
        self.cache_memory_bytes = 0
        self.details_expanded = False
        self.pixmap_cache: OrderedDict[tuple[str, int, int], QPixmap] = OrderedDict()

        self.setWindowIcon(self.create_app_icon())

        self.frame_view = FrameView(self.refresh_current_frame)
        self.status_label = QLabel()
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.setStyleSheet(
            "QLabel { background: #111111; color: #efefef; padding: 10px; border: 1px solid #292929; }"
        )
        self.status_label.setWordWrap(True)

        self.folder_buttons = [
            QPushButton("Select Folder A"),
            QPushButton("Select Folder B"),
            QPushButton("Select Folder C"),
            QPushButton("Select Folder D"),
        ]
        for index, button in enumerate(self.folder_buttons):
            button.clicked.connect(lambda checked=False, idx=index: self.select_folder(idx))

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)

        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self.reload_sequences)

        self.clear_button = QPushButton("Clear All")
        self.clear_button.clicked.connect(self.clear_all)

        self.optimize_action = QAction("Optimize Folder...", self)
        self.optimize_action.triggered.connect(self.optimize_folder)

        self.shortcuts_action = QAction("Shortcuts", self)
        self.shortcuts_action.triggered.connect(self.show_shortcuts)

        self.readme_action = QAction("README", self)
        self.readme_action.triggered.connect(self.show_readme)

        self.details_action = QAction("Show Details", self)
        self.details_action.setCheckable(True)
        self.details_action.toggled.connect(self.toggle_details)

        self.tools_menu = QMenu(self)
        self.tools_menu.addAction(self.optimize_action)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction(self.shortcuts_action)
        self.tools_menu.addAction(self.readme_action)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction(self.details_action)

        self.tools_button = QToolButton()
        self.tools_button.setText("Tools")
        self.tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tools_button.setMenu(self.tools_menu)

        self.fps_combo = QComboBox()
        for option in FPS_OPTIONS:
            self.fps_combo.addItem(str(option), option)
        self.fps_combo.setCurrentText(str(self.fps))
        self.fps_combo.currentIndexChanged.connect(self.change_fps)

        self.display_size_combo = QComboBox()
        for label, size in DISPLAY_SIZE_OPTIONS:
            self.display_size_combo.addItem(label, size)
        self.display_size_combo.currentIndexChanged.connect(self.change_display_size)

        self.loop_checkbox = QCheckBox("Loop")
        self.loop_checkbox.stateChanged.connect(self.toggle_loop_from_checkbox)

        self.cache_all_checkbox = QCheckBox("Cache All")
        self.cache_all_checkbox.stateChanged.connect(self.toggle_cache_all)

        self.timeline_slider = QSlider(Qt.Orientation.Horizontal)
        self.timeline_slider.setMinimum(0)
        self.timeline_slider.setMaximum(0)
        self.timeline_slider.setEnabled(False)
        self.timeline_slider.sliderPressed.connect(self.pause_playback)
        self.timeline_slider.sliderMoved.connect(self.seek_from_slider)

        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.time_label.setMinimumWidth(140)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.health_label = QLabel("● Smooth")
        self.health_label.setMinimumWidth(110)
        self.health_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.folder_watch_label = QLabel("● Clean")
        self.folder_watch_label.setMinimumWidth(130)
        self.folder_watch_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._build_layout()
        self._install_shortcuts()
        self.update_optimize_button_state()

        self.render_timer = QTimer(self)
        self.render_timer.setInterval(10)
        self.render_timer.timeout.connect(self.update_playback)
        self.render_timer.start()

        self.folder_watch_timer = QTimer(self)
        self.folder_watch_timer.setInterval(WATCH_INTERVAL_MS)
        self.folder_watch_timer.timeout.connect(self.check_folder_watch)
        self.folder_watch_timer.start()

        self.update_status()

    def _build_layout(self) -> None:
        central = QWidget()
        central.setStyleSheet(
            """
            QWidget {
                background: #181818;
                color: #f0f0f0;
            }
            QPushButton, QToolButton, QComboBox {
                background: #2a2a2a;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 4px 9px;
                min-height: 22px;
            }
            QPushButton:hover, QToolButton:hover, QComboBox:hover {
                background: #343434;
                border-color: #505050;
            }
            QPushButton:pressed, QToolButton:pressed {
                background: #202020;
            }
            QPushButton:disabled, QToolButton:disabled {
                color: #777777;
                background: #222222;
            }
            QMenu {
                background: #242424;
                color: #f0f0f0;
                border: 1px solid #3a3a3a;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 22px;
            }
            QMenu::item:selected {
                background: #3a3a3a;
            }
            QCheckBox {
                spacing: 6px;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: #333333;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px;
                margin: -5px 0;
                border-radius: 7px;
                background: #6aa9ff;
            }
            """
        )
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        for button in self.folder_buttons:
            folder_row.addWidget(button)
        folder_row.addStretch(1)

        playback_row = QHBoxLayout()
        playback_row.setSpacing(8)
        playback_row.addWidget(self.play_button)
        playback_row.addWidget(self.reload_button)
        playback_row.addWidget(self.clear_button)
        playback_row.addSpacing(12)
        playback_row.addWidget(QLabel("FPS"))
        playback_row.addWidget(self.fps_combo)
        playback_row.addWidget(QLabel("Display Size"))
        playback_row.addWidget(self.display_size_combo)
        playback_row.addSpacing(12)
        playback_row.addWidget(self.loop_checkbox)
        playback_row.addWidget(self.cache_all_checkbox)
        playback_row.addWidget(self.tools_button)
        playback_row.addStretch(1)

        timeline_row = QHBoxLayout()
        timeline_row.setSpacing(8)
        timeline_row.addWidget(self.timeline_slider, stretch=1)
        timeline_row.addWidget(self.time_label)
        timeline_row.addWidget(self.health_label)
        timeline_row.addWidget(self.folder_watch_label)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)

        root.addLayout(folder_row)
        root.addLayout(playback_row)
        root.addLayout(timeline_row)
        root.addWidget(divider)
        root.addWidget(self.frame_view, stretch=1)
        root.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.apply_status_label_size()

    def _install_shortcuts(self) -> None:
        shortcut_map = {
            "Space": self.toggle_playback,
            "Left": lambda: self.step_frames(-1),
            "Right": lambda: self.step_frames(1),
            "Shift+Left": lambda: self.step_frames(-10),
            "Shift+Right": lambda: self.step_frames(10),
            "Home": self.jump_to_first,
            "End": self.jump_to_last,
            "R": self.reload_sequences,
            "L": self.toggle_loop_shortcut,
        }
        for key, handler in shortcut_map.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(handler)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.Type.ActivationChange:
            return
        if self.isActiveWindow():
            self.inactive_started_at = None
            if self.folder_watch_status == "Idle":
                self.folder_watch_status = "Clean"
                self.check_folder_watch()
        elif self.inactive_started_at is None:
            self.inactive_started_at = time.perf_counter()

    def create_app_icon(self) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#171717"))
        painter.drawRoundedRect(4, 4, 56, 56, 10, 10)
        painter.setBrush(QColor("#e8f3ff"))
        painter.drawRoundedRect(14, 16, 36, 32, 4, 4)
        painter.setBrush(QColor("#37d67a"))
        painter.drawRect(18, 20, 10, 24)
        painter.setBrush(QColor("#4aa3ff"))
        painter.drawRect(31, 20, 15, 24)
        painter.end()
        return QIcon(pixmap)

    def select_folder(self, clip_index: int) -> None:
        start_dir = str(self.clips[clip_index].folder or Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            f"Select Folder {self.clips[clip_index].name}",
            start_dir,
        )
        if not selected:
            return
        self.clips[clip_index].folder = Path(selected)
        self.reload_sequences()

    def scan_clip(self, clip: Clip) -> list[Path]:
        if clip.folder is None:
            return []
        if not clip.folder.exists() or not clip.folder.is_dir():
            self.last_error_message = f"Folder {clip.name} not found: {clip.folder}"
            return []
        frames = [
            path
            for path in clip.folder.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
        ]
        frames.sort(key=natural_sort_key)
        return frames

    def reload_sequences(self) -> None:
        preserved_index = self.current_frame_index
        was_playing = self.is_playing
        self.pause_playback()
        self.last_error_message = ""
        self.timeline.clear()
        self.clear_cache()

        for clip_index, clip in enumerate(self.clips):
            clip.frames = self.scan_clip(clip)
            for clip_frame_index, path in enumerate(clip.frames):
                self.timeline.append(TimelineFrame(clip_index, clip_frame_index, path))

        if self.timeline:
            self.current_frame_index = max(0, min(preserved_index, len(self.timeline) - 1))
            self.paused_frame_index = self.current_frame_index
        else:
            self.current_frame_index = 0
            self.paused_frame_index = 0

        self.reset_playback_stats()
        self.folder_snapshot = self.build_folder_snapshot()
        self.folder_watch_status = "Clean"
        self.refresh_current_frame(force=True)
        if self.cache_all_enabled and self.timeline:
            self.cache_all_frames()
        self.update_status()

        if was_playing and self.timeline:
            self.start_playback()

    def toggle_playback(self) -> None:
        if self.is_playing:
            self.pause_playback()
        else:
            self.start_playback()

    def start_playback(self) -> None:
        if not self.timeline:
            self.last_error_message = "No frames available. Select at least one folder with images."
            self.is_playing = False
            self.play_button.setText("Play")
            self.update_status()
            return
        self.is_playing = True
        self.playback_started_at = time.perf_counter()
        self.paused_frame_index = self.current_frame_index
        self.last_playback_raw_index = self.paused_frame_index
        self.play_button.setText("Pause")
        self.update_status()

    def pause_playback(self) -> None:
        if self.is_playing:
            self.sync_current_frame_from_clock()
        self.is_playing = False
        self.paused_frame_index = self.current_frame_index
        self.play_button.setText("Play")
        self.update_status()

    def change_fps(self) -> None:
        selected = self.fps_combo.currentData()
        if selected is None:
            return
        if self.is_playing:
            self.sync_current_frame_from_clock()
            self.paused_frame_index = self.current_frame_index
            self.playback_started_at = time.perf_counter()
        self.fps = int(selected)
        self.update_status()

    def change_display_size(self) -> None:
        selected = self.display_size_combo.currentData()
        self.display_size = selected if selected is None else tuple(selected)
        self.update_optimize_button_state()
        self.clear_cache()
        self.refresh_current_frame(force=True)
        if self.cache_all_enabled and self.timeline:
            self.cache_all_frames()
        self.update_status()

    def update_optimize_button_state(self) -> None:
        self.optimize_action.setEnabled(self.display_size is not None)

    def toggle_loop_from_checkbox(self) -> None:
        self.loop_enabled = self.loop_checkbox.isChecked()
        self.update_status()

    def toggle_cache_all(self) -> None:
        self.cache_all_enabled = self.cache_all_checkbox.isChecked()
        self.clear_cache()
        self.refresh_current_frame(force=True)
        if self.cache_all_enabled and self.timeline:
            self.cache_all_frames()
        self.update_status()

    def toggle_details(self, checked: bool) -> None:
        self.details_expanded = checked
        self.details_action.setText("Hide Details" if checked else "Show Details")
        self.apply_status_label_size()
        self.update_status()

    def apply_status_label_size(self) -> None:
        if self.details_expanded:
            self.status_label.setMaximumHeight(420)
        else:
            self.status_label.setMaximumHeight(42)

    def toggle_loop_shortcut(self) -> None:
        self.loop_checkbox.setChecked(not self.loop_checkbox.isChecked())

    def sync_current_frame_from_clock(self) -> None:
        if not self.is_playing or not self.timeline:
            return
        elapsed = max(0.0, time.perf_counter() - self.playback_started_at)
        raw_index = self.paused_frame_index + math.floor(elapsed * self.fps)
        if self.loop_enabled:
            self.current_frame_index = raw_index % len(self.timeline)
        else:
            self.current_frame_index = min(raw_index, len(self.timeline) - 1)

    def update_playback(self) -> None:
        if not self.is_playing or not self.timeline:
            return

        elapsed = max(0.0, time.perf_counter() - self.playback_started_at)
        raw_index = self.paused_frame_index + math.floor(elapsed * self.fps)

        if raw_index > self.last_playback_raw_index + 1:
            self.dropped_frames += raw_index - self.last_playback_raw_index - 1
        if raw_index > self.last_playback_raw_index:
            self.last_playback_raw_index = raw_index

        if self.loop_enabled:
            target_index = raw_index % len(self.timeline)
        else:
            target_index = min(raw_index, len(self.timeline) - 1)

        if target_index != self.current_frame_index:
            self.current_frame_index = target_index
            self.refresh_current_frame(force=True)
            self.update_status()

        if not self.loop_enabled and raw_index >= len(self.timeline):
            self.current_frame_index = len(self.timeline) - 1
            self.pause_playback()
            self.refresh_current_frame(force=True)
            self.update_status()

    def step_frames(self, delta: int) -> None:
        self.pause_playback()
        if not self.timeline:
            return
        self.current_frame_index = max(
            0,
            min(self.current_frame_index + delta, len(self.timeline) - 1),
        )
        self.paused_frame_index = self.current_frame_index
        self.reset_playback_stats()
        self.refresh_current_frame(force=True)
        self.update_status()

    def jump_to_first(self) -> None:
        self.pause_playback()
        if not self.timeline:
            return
        self.current_frame_index = 0
        self.paused_frame_index = 0
        self.reset_playback_stats()
        self.refresh_current_frame(force=True)
        self.update_status()

    def jump_to_last(self) -> None:
        self.pause_playback()
        if not self.timeline:
            return
        self.current_frame_index = len(self.timeline) - 1
        self.paused_frame_index = self.current_frame_index
        self.reset_playback_stats()
        self.refresh_current_frame(force=True)
        self.update_status()

    def clear_cache(self) -> None:
        self.pixmap_cache.clear()
        self.cache_memory_bytes = 0

    def reset_playback_stats(self) -> None:
        self.dropped_frames = 0
        self.last_playback_raw_index = self.current_frame_index

    def clear_all(self) -> None:
        self.pause_playback()
        for clip in self.clips:
            clip.folder = None
            clip.frames = []
        self.timeline.clear()
        self.current_frame_index = 0
        self.paused_frame_index = 0
        self.last_error_message = ""
        self.clear_cache()
        self.reset_playback_stats()
        self.folder_snapshot = ()
        self.folder_watch_status = "Clean"
        self.refresh_current_frame(force=True)
        self.update_status()

    def build_folder_snapshot(self) -> tuple[tuple[str, int, int], ...]:
        entries = []
        for clip in self.clips:
            if clip.folder is None or not clip.folder.exists() or not clip.folder.is_dir():
                continue
            for path in self.scan_clip(clip):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def check_folder_watch(self) -> None:
        if self.is_playing:
            return
        if self.inactive_started_at is not None:
            inactive_seconds = time.perf_counter() - self.inactive_started_at
            if inactive_seconds >= WATCH_IDLE_AFTER_SECONDS:
                self.folder_watch_status = "Idle"
                self.update_status()
            return
        if not any(clip.folder for clip in self.clips):
            self.folder_watch_status = "Clean"
            self.update_status()
            return
        if self.folder_watch_status == "Changed":
            self.update_status()
            return
        current_snapshot = self.build_folder_snapshot()
        if current_snapshot != self.folder_snapshot:
            self.folder_watch_status = "Changed"
        else:
            self.folder_watch_status = "Clean"
        self.update_status()

    def seek_from_slider(self, value: int) -> None:
        if self.updating_slider or not self.timeline:
            return
        self.pause_playback()
        self.current_frame_index = max(0, min(value, len(self.timeline) - 1))
        self.paused_frame_index = self.current_frame_index
        self.reset_playback_stats()
        self.refresh_current_frame(force=True)
        self.update_status()

    def show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Shortcuts",
            "\n".join(
                [
                    "Space: Play / Pause",
                    "Left / Right: Step 1 frame",
                    "Shift + Left / Right: Step 10 frames",
                    "Home / End: First / Last frame",
                    "R: Reload folders",
                    "L: Toggle Loop",
                ]
            ),
        )

    def show_readme(self) -> None:
        readme_path = Path(__file__).with_name("README.md")
        try:
            content = readme_path.read_text(encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "README", f"Failed to read README.md:\n{exc}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("README")
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)
        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(content)
        layout.addWidget(viewer)
        dialog.exec()

    def optimize_folder(self) -> None:
        if self.display_size is None:
            return

        selected = QFileDialog.getExistingDirectory(
            self,
            f"Optimize Folder to {self.display_size[0]} x {self.display_size[1]}",
            str(Path.home()),
        )
        if not selected:
            return

        folder = Path(selected)
        frames = [
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
        ]
        frames.sort(key=natural_sort_key)
        if not frames:
            QMessageBox.information(self, "Optimize Folder", "No JPG/JPEG/PNG images found.")
            return

        answer = QMessageBox.warning(
            self,
            "Overwrite Images?",
            (
                f"This will overwrite {len(frames)} image(s) in:\n\n{folder}\n\n"
                "Images larger than the selected Display Size will be resized in place.\n"
                "This cannot be undone unless you have a backup."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return

        self.pause_playback()
        target_size = self.display_size
        progress = QProgressDialog("Optimizing images...", "Cancel", 0, len(frames), self)
        progress.setWindowTitle("Optimize Folder")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        optimized = 0
        skipped = 0
        failed = 0
        for index, path in enumerate(frames, start=1):
            if progress.wasCanceled():
                break
            progress.setValue(index - 1)
            progress.setLabelText(f"Optimizing {index} / {len(frames)}\n{path.name}")
            QApplication.processEvents()
            try:
                with Image.open(path) as image:
                    image = ImageOps.exif_transpose(image)
                    if image.width <= target_size[0] and image.height <= target_size[1]:
                        skipped += 1
                        continue
                    image.thumbnail(target_size, Image.Resampling.LANCZOS)
                    self.save_optimized_image(path, image)
                    optimized += 1
            except Exception as exc:
                failed += 1
                self.last_error_message = f"Failed to optimize image: {path} | {exc}"

        progress.setValue(len(frames))
        self.clear_cache()
        self.reload_sequences()
        QMessageBox.information(
            self,
            "Optimize Folder",
            (
                f"Optimized: {optimized}\n"
                f"Skipped: {skipped}\n"
                f"Failed: {failed}"
            ),
        )

    def save_optimized_image(self, path: Path, image: Image.Image) -> None:
        suffix = path.suffix.casefold()
        if suffix in {".jpg", ".jpeg"}:
            image.convert("RGB").save(path, quality=92, optimize=True)
        else:
            image.save(path, optimize=True)

    def cache_all_frames(self) -> None:
        if not self.timeline:
            return
        progress = QProgressDialog("Caching frames in memory...", "Cancel", 0, len(self.timeline), self)
        progress.setWindowTitle("Cache All")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        for index, frame in enumerate(self.timeline, start=1):
            if progress.wasCanceled():
                self.cache_all_checkbox.setChecked(False)
                self.clear_cache()
                self.update_status()
                return
            progress.setValue(index - 1)
            progress.setLabelText(f"Caching {index} / {len(self.timeline)}\n{frame.path.name}")
            QApplication.processEvents()
            self.get_scaled_pixmap(frame.path)

        progress.setValue(len(self.timeline))
        self.update_status()

    def refresh_current_frame(self, force: bool = False) -> None:
        if not self.timeline:
            self.frame_view.setPixmap(QPixmap())
            self.frame_view.setText("No readable image frames loaded.")
            return

        frame = self.timeline[self.current_frame_index]
        pixmap = self.get_scaled_pixmap(frame.path)
        if pixmap is None:
            self.frame_view.setPixmap(QPixmap())
            self.frame_view.setText(f"Failed to read image:\n{frame.path}")
            return

        self.frame_view.setText("")
        self.frame_view.setPixmap(pixmap)

    def get_scaled_pixmap(self, path: Path) -> QPixmap | None:
        view_width = max(1, self.frame_view.width() - 2)
        view_height = max(1, self.frame_view.height() - 2)
        if self.display_size is None:
            target_width = view_width
            target_height = view_height
        else:
            target_width = min(view_width, self.display_size[0])
            target_height = min(view_height, self.display_size[1])
        cache_key = (str(path), target_width, target_height)
        cached = self.pixmap_cache.get(cache_key)
        if cached is not None:
            self.pixmap_cache.move_to_end(cache_key)
            return cached

        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
                rgba = image.convert("RGBA")
                buffer = rgba.tobytes("raw", "RGBA")
                qimage = QImage(
                    buffer,
                    rgba.width,
                    rgba.height,
                    rgba.width * 4,
                    QImage.Format.Format_RGBA8888,
                ).copy()
                pixmap = QPixmap.fromImage(qimage)
        except Exception as exc:
            self.last_error_message = f"Failed to read image: {path} | {exc}"
            self.update_status()
            return None

        self.pixmap_cache[cache_key] = pixmap
        self.recalculate_cache_memory()
        self.pixmap_cache.move_to_end(cache_key)
        while not self.cache_all_enabled and len(self.pixmap_cache) > CACHE_LIMIT:
            self.pixmap_cache.popitem(last=False)
        self.recalculate_cache_memory()
        return pixmap

    def recalculate_cache_memory(self) -> None:
        self.cache_memory_bytes = sum(
            pixmap.width() * pixmap.height() * 4 for pixmap in self.pixmap_cache.values()
        )

    def clip_summary(self) -> str:
        summaries = []
        for clip in self.clips:
            folder = str(clip.folder) if clip.folder else "Not selected"
            summaries.append(f"{clip.name}: {len(clip.frames)} frames | {folder}")
        return "\n".join(summaries)

    def update_status(self) -> None:
        total_frames = len(self.timeline)
        global_frame = self.current_frame_index + 1 if total_frames else 0
        state = "Playing" if self.is_playing else "Paused"
        loop = "On" if self.loop_enabled else "Off"

        if total_frames:
            frame = self.timeline[self.current_frame_index]
            clip = self.clips[frame.clip_index]
            clip_name = clip.folder.name if clip.folder else f"Clip {clip.name}"
            clip_frame = frame.clip_frame_index + 1
            clip_total = len(clip.frames)
        else:
            clip_name = "-"
            clip_frame = 0
            clip_total = 0

        self.update_timeline_controls(total_frames)
        summary_line = (
            f"Global: {global_frame}/{total_frames} | "
            f"Clip: {clip_name} {clip_frame}/{clip_total} | "
            f"FPS: {self.fps} | State: {state} | Loop: {loop} | "
            f"Cache: {self.format_bytes(self.cache_memory_bytes)} | "
            f"Drops: {self.dropped_frames} | Watch: {self.folder_watch_status}"
        )
        detail_lines = [
            f"Global Frame: {global_frame} / {total_frames}",
            f"Clip: {clip_name}",
            f"Clip Frame: {clip_frame} / {clip_total}",
            "",
            "Loaded Folders:",
            self.clip_summary(),
            "",
            f"FPS: {self.fps}",
            f"Display Size: {self.display_size_combo.currentText()}",
            f"State: {state}",
            f"Loop: {loop}",
            f"Cache All: {'On' if self.cache_all_enabled else 'Off'}",
            f"Image Cache: {self.format_bytes(self.cache_memory_bytes)}",
            f"Dropped Frames: {self.dropped_frames}",
            f"Playback Health: {self.playback_health_text()}",
            f"Folder Watch: {self.folder_watch_status}",
        ]
        if self.last_error_message:
            detail_lines.extend(["", f"Error: {self.last_error_message}"])
        self.status_label.setText("\n".join(detail_lines) if self.details_expanded else summary_line)

    def update_timeline_controls(self, total_frames: int) -> None:
        self.updating_slider = True
        self.timeline_slider.setEnabled(total_frames > 0)
        self.timeline_slider.setMaximum(max(0, total_frames - 1))
        self.timeline_slider.setValue(self.current_frame_index if total_frames else 0)
        self.updating_slider = False

        current_seconds = self.current_frame_index / self.fps if total_frames else 0.0
        total_seconds = total_frames / self.fps if total_frames else 0.0
        self.time_label.setText(
            f"{self.format_time(current_seconds)} / {self.format_time(total_seconds)}"
        )
        health_text = self.playback_health_text()
        color = {
            "Smooth": "#37d67a",
            "Minor Drops": "#f2c94c",
            "Dropping": "#ff5c5c",
        }[health_text]
        self.health_label.setText(f"● {health_text}")
        self.health_label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: 600; }}")
        watch_color = {
            "Clean": "#37d67a",
            "Changed": "#f2c94c",
            "Idle": "#8a8a8a",
        }[self.folder_watch_status]
        self.folder_watch_label.setText(f"● Watch {self.folder_watch_status}")
        self.folder_watch_label.setStyleSheet(
            f"QLabel {{ color: {watch_color}; font-weight: 600; }}"
        )

    def playback_health_text(self) -> str:
        if self.dropped_frames == 0:
            return "Smooth"
        if self.dropped_frames <= max(2, self.fps // 5):
            return "Minor Drops"
        return "Dropping"

    @staticmethod
    def format_time(seconds: float) -> str:
        minutes = int(seconds // 60)
        remaining = seconds - minutes * 60
        return f"{minutes:02d}:{remaining:05.2f}"

    @staticmethod
    def format_bytes(byte_count: int) -> str:
        if byte_count >= 1024**3:
            return f"{byte_count / 1024**3:.2f} GB"
        if byte_count >= 1024**2:
            return f"{byte_count / 1024**2:.1f} MB"
        if byte_count >= 1024:
            return f"{byte_count / 1024:.1f} KB"
        return f"{byte_count} B"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Sequence Stitch Player")
    font = QFont()
    font.setPointSize(10)
    app.setFont(font)
    window = SequenceStitchPlayer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
