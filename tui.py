import sys
import threading
import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Header,
    Footer,
    Button,
    Label,
    Input,
    Select,
    Static,
    RichLog,
    Tree,
)
from textual.reactive import reactive
from textual.binding import Binding

from pipeline import PipelineConfig, Pipeline
from srt_ops import get_speakers
from logger import setup_logger, get_latest_log


CSS = """
Screen {
    background: $surface;
    layout: vertical;
}

#options-scroll {
    height: 1fr;
    padding: 1 2;
    overflow-y: auto;
}

#log-panel {
    height: 12;
    padding: 0 2;
    border-top: solid $accent;
    background: $panel;
}

#progress-log {
    border: solid $primary;
    height: 100%;
    margin: 1 0;
    background: $surface;
}

.title {
    text-align: center;
    text-style: bold;
    color: $accent;
    padding: 1 0;
    width: 100%;
}

.section-label {
    text-style: bold;
    padding: 1 0 0 0;
    color: $text;
}

.option-btn {
    width: 100%;
    margin: 0 0 1 0;
}

.option-btn.selected {
    background: $accent 20%;
    border: solid $accent;
}

.nav-btn {
    width: 1fr;
    margin: 1 0;
}

.prompt {
    padding: 0 0 1 0;
    color: $text-muted;
}

.hidden {
    display: none;
}

#speaker-assign {
    padding: 1 0;
}

.speaker-row {
    height: auto;
    padding: 0 0 1 0;
}

.input-field {
    margin: 0 0 1 0;
}

Horizontal {
    height: auto;
    width: 100%;
}

#video-input {
    width: 1fr;
}

#browse-btn {
    width: auto;
    margin: 0 0 1 1;
}

FileBrowserScreen {
    align: center middle;
}

#browser-dialog {
    background: $panel;
    border: solid $accent;
    width: 70;
    height: 80%;
    padding: 1 2;
}

#browser-tree {
    height: 1fr;
    border: solid $primary;
    margin: 1 0;
}

#browser-path {
    color: $text-muted;
    padding: 0 0 1 0;
}
"""

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"}


class FileBrowserScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    def __init__(self, start_dir: str = "~"):
        super().__init__()
        self.start_dir = str(Path(start_dir).expanduser())

    def compose(self) -> ComposeResult:
        with Container(id="browser-dialog"):
            yield Label("File Browser", classes="title")
            yield Label(self.start_dir, id="browser-path")
            yield Tree(self.start_dir, id="browser-tree")
            with Horizontal():
                yield Button("Select", id="browser-select-btn", variant="success")
                yield Button("Cancel", id="browser-cancel-btn", variant="error")

    def on_mount(self) -> None:
        tree = self.query_one("#browser-tree", Tree)
        self._populate_tree(tree, self.start_dir, tree.root)
        tree.root.expand()

    def _populate_tree(self, tree: Tree, path: str, node):
        try:
            entries = sorted(Path(path).iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if entry.is_dir():
                child = tree.root.add(entry.name, data=str(entry)) if node is tree.root else node.add(entry.name, data=str(entry))
                child.allow_expand = True
            elif entry.suffix.lower() in VIDEO_EXTENSIONS:
                label = f"[green]{entry.name}[/green]"
                if node is tree.root:
                    tree.root.add(label, data=str(entry))
                else:
                    node.add(label, data=str(entry))

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if node.data and Path(node.data).is_dir():
            if not node.children:
                self._populate_tree(self.query_one("#browser-tree", Tree), node.data, node)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self._try_select(event.node)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browser-select-btn":
            tree = self.query_one("#browser-tree", Tree)
            if tree.cursor_node:
                self._try_select(tree.cursor_node)
        elif event.button.id == "browser-cancel-btn":
            self.dismiss(None)

    def _try_select(self, node):
        if node.data:
            path = Path(node.data)
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                self.dismiss(str(path))

    def action_cancel(self):
        self.dismiss(None)

    def action_select(self):
        tree = self.query_one("#browser-tree", Tree)
        if tree.cursor_node:
            self._try_select(tree.cursor_node)


class VideoEditTUI(App):
    CSS = CSS
    TITLE = "Video Edit Pipeline"
    SUB_TITLE = "Automated editing with subtitles & speaker ID"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]

    video_path = reactive("")
    speaker_mode = reactive("")
    crop_mode = reactive("")
    format_mode = reactive("")
    left_speaker = reactive("")
    right_speaker = reactive("")
    want_hooks = reactive(False)
    audio_only = reactive(False)
    watermark = reactive(False)
    speakers = reactive(list)
    pipeline_running = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="options-scroll"):
            yield Label("Video Edit Pipeline", classes="title")
            yield Label("Select a video file to begin:", classes="section-label")
            with Horizontal():
                yield Input(placeholder="/path/to/video.mp4", id="video-input", classes="input-field")
                yield Button("Browse", id="browse-btn", variant="primary")
            yield Button("Confirm Video", id="confirm-video-btn", variant="success", classes="option-btn")
            yield Static("", id="video-info")

            yield Label("Option 1: Speaker Mode", classes="section-label")
            yield Button("Single Speaker", id="single-speaker-btn", classes="option-btn")
            yield Button("Multi Speaker", id="multi-speaker-btn", classes="option-btn")

            yield Static("", id="crop-section", classes="hidden")
            yield Label("Cropping Options", classes="section-label")
            for label, value in [
                ("Left Half", "left_half"),
                ("Right Half", "right_half"),
                ("First Third", "first_third"),
                ("Second Third", "second_third"),
                ("Third Third", "third_third"),
            ]:
                yield Button(label, id=f"crop-{value}", classes="option-btn")

            yield Static("", id="speaker-section", classes="hidden")
            yield Label("Speaker Assignment", classes="section-label")
            yield Static("After subtitles are generated, assign speakers to screen positions:", classes="prompt")
            with Container(id="speaker-assign"):
                yield Label("Left speaker:", classes="speaker-row")
                yield Select([], id="left-speaker-select", classes="input-field")
                yield Label("Right speaker:", classes="speaker-row")
                yield Select([], id="right-speaker-select", classes="input-field")

            yield Label("Option 2: Output Format", classes="section-label")
            yield Button("Shorts (Single Speaker)", id="shorts-single-btn", classes="option-btn")
            yield Button("Shorts (Multi Speaker)", id="shorts-multi-btn", classes="option-btn")
            yield Button("Long Form (Video)", id="long-form-btn", classes="option-btn")
            yield Button("Long Form (Audio Only)", id="long-audio-btn", classes="option-btn")

            yield Static("", id="hooks-section", classes="hidden")
            yield Label("Hooks for Shorts?", classes="section-label")
            yield Button("Yes - Add Hooks", id="hooks-yes-btn", classes="option-btn")
            yield Button("No Hooks", id="hooks-no-btn", classes="option-btn")

            yield Static("", id="watermark-section", classes="hidden")
            yield Label("Watermark?", classes="section-label")
            yield Button("Yes - Add Watermark", id="watermark-yes-btn", classes="option-btn")
            yield Button("No Watermark", id="watermark-no-btn", classes="option-btn")

            yield Button("Start Pipeline", id="start-btn", variant="success", classes="nav-btn")

        with Container(id="log-panel"):
            yield RichLog(id="progress-log", wrap=True, markup=True)

        yield Footer()

    def on_mount(self) -> None:
        self.logger = setup_logger()
        self.logger.info("TUI mounted")
        self.log_widget = self.query_one("#progress-log", RichLog)
        self.log_widget.write(f"[dim]Logging to: {get_latest_log()}[/dim]")
        if len(sys.argv) > 1:
            self.query_one("#video-input").value = sys.argv[1]
            self.logger.info(f"Video from argv: {sys.argv[1]}")

    def _set_selected(self, button_id: str, container_id: str = ""):
        if container_id:
            for btn in self.query(f"#{container_id} Button"):
                btn.remove_class("selected")
        self.query_one(f"#{button_id}").add_class("selected")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id

        if bid == "confirm-video-btn":
            self._confirm_video()
        elif bid == "browse-btn":
            self._open_file_browser()
        elif bid == "single-speaker-btn":
            self.speaker_mode = "single"
            self._set_selected(bid)
            self.query_one("#crop-section").remove_class("hidden")
            self.query_one("#speaker-section").add_class("hidden")
        elif bid == "multi-speaker-btn":
            self.speaker_mode = "multi"
            self._set_selected(bid)
            self.query_one("#crop-section").add_class("hidden")
            self.query_one("#speaker-section").remove_class("hidden")
        elif bid.startswith("crop-"):
            self.crop_mode = bid.replace("crop-", "")
            self._set_selected(bid, "main-container")
        elif bid == "shorts-single-btn":
            self.format_mode = "shorts"
            self.audio_only = False
            self.query_one("#hooks-section").remove_class("hidden")
            self.query_one("#watermark-section").remove_class("hidden")
            self._set_selected(bid)
        elif bid == "shorts-multi-btn":
            self.format_mode = "shorts"
            self.audio_only = False
            self.query_one("#hooks-section").remove_class("hidden")
            self.query_one("#watermark-section").remove_class("hidden")
            self._set_selected(bid)
        elif bid == "long-form-btn":
            self.format_mode = "long"
            self.audio_only = False
            self.query_one("#hooks-section").add_class("hidden")
            self.query_one("#watermark-section").remove_class("hidden")
            self._set_selected(bid)
        elif bid == "long-audio-btn":
            self.format_mode = "long"
            self.audio_only = True
            self.query_one("#hooks-section").add_class("hidden")
            self.query_one("#watermark-section").add_class("hidden")
            self._set_selected(bid)
        elif bid == "hooks-yes-btn":
            self.want_hooks = True
            self._set_selected(bid)
        elif bid == "hooks-no-btn":
            self.want_hooks = False
            self._set_selected(bid)
        elif bid == "watermark-yes-btn":
            self.watermark = True
            self._set_selected(bid)
        elif bid == "watermark-no-btn":
            self.watermark = False
            self._set_selected(bid)
        elif bid == "start-btn":
            self._start_pipeline()

    def _open_file_browser(self):
        current = self.query_one("#video-input").value.strip()
        start = str(Path(current).parent) if current and Path(current).parent.exists() else "~"
        self.push_screen(FileBrowserScreen(start), self._file_browser_callback)

    def _file_browser_callback(self, selected_path: str | None):
        if selected_path:
            self.query_one("#video-input").value = selected_path
            self._confirm_video()

    def _confirm_video(self):
        path = self.query_one("#video-input").value.strip()
        if not path:
            self.log_widget.write("[red]Please enter a video path[/red]")
            return
        p = Path(path)
        if not p.exists():
            self.log_widget.write(f"[red]File not found: {path}[/red]")
            return
        self.video_path = str(p)
        self.log_widget.write(f"[green]Video selected: {p.name}[/green]")

        try:
            from srt_ops import get_video_duration, get_video_dimensions
            dur = get_video_duration(str(p))
            w, h = get_video_dimensions(str(p))
            mins = int(dur // 60)
            secs = int(dur % 60)
            self.query_one("#video-info").update(
                f"Duration: {mins}m {secs}s | Resolution: {w}x{h}"
            )
        except Exception:
            self.query_one("#video-info").update("")

    def _populate_speakers(self, speakers: list[str]):
        self.speakers = speakers
        options = [(s, s) for s in speakers]
        self.query_one("#left-speaker-select").set_options(options)
        self.query_one("#right-speaker-select").set_options(options)
        if len(speakers) >= 2:
            self.query_one("#left-speaker-select").value = speakers[0]
            self.query_one("#right-speaker-select").value = speakers[1]

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "left-speaker-select":
            self.left_speaker = event.value
        elif event.select.id == "right-speaker-select":
            self.right_speaker = event.value

    def _start_pipeline(self):
        if self.pipeline_running:
            self.log_widget.write("[yellow]Pipeline already running...[/yellow]")
            return
        if not self.video_path:
            self.log_widget.write("[red]No video selected[/red]")
            return
        if not self.speaker_mode:
            self.log_widget.write("[red]Select speaker mode[/red]")
            return
        if not self.format_mode:
            self.log_widget.write("[red]Select output format[/red]")
            return
        if self.speaker_mode == "single" and not self.crop_mode:
            self.log_widget.write("[red]Select a crop option[/red]")
            return
        if self.speaker_mode == "multi":
            left = self.query_one("#left-speaker-select").value
            right = self.query_one("#right-speaker-select").value
            if not left or not right:
                self.log_widget.write("[red]Assign speakers to left/right[/red]")
                return
            if left == right:
                self.log_widget.write("[red]Left and right speakers must differ[/red]")
                return
            self.left_speaker = left
            self.right_speaker = right

        config = PipelineConfig(
            video_path=self.video_path,
            speaker_mode=self.speaker_mode,
            format_mode=self.format_mode,
            crop_mode=self.crop_mode,
            left_speaker=self.left_speaker,
            right_speaker=self.right_speaker,
            want_hooks=self.want_hooks,
            audio_only=self.audio_only,
            watermark=self.watermark,
        )

        self.pipeline_running = True
        self.log_widget.write("[bold green]Starting pipeline...[/bold green]")

        thread = threading.Thread(target=self._run_pipeline_thread, args=(config,), daemon=True)
        thread.start()

    def _run_pipeline_thread(self, config: PipelineConfig):
        def progress(msg: str):
            self.call_from_thread(self.log_widget.write, msg)

        try:
            pipe = Pipeline(config, progress_cb=progress)

            if config.speaker_mode == "single":
                pipe.step_crop()
                srt_path = pipe.step_subtitles()
            else:
                srt_path = pipe.step_subtitles()
                speakers = get_speakers(pipe.segments)
                self.call_from_thread(self._populate_speakers, speakers)
                if not config.left_speaker or not config.right_speaker:
                    progress("[yellow]Waiting for speaker assignment...[/yellow]")
                    progress("[yellow]Please select left/right speakers and click Start again.[/yellow]")
                    self.call_from_thread(self._pipeline_done)
                    return

            if config.format_mode == "shorts":
                if config.speaker_mode == "single":
                    pipe.step_shorts_single(srt_path)
                else:
                    pipe.step_shorts_multi(srt_path)
            elif config.format_mode == "long":
                if config.audio_only:
                    pipe.step_long_audio(srt_path)
                else:
                    pipe.step_long_form(srt_path)

        except Exception as e:
            self.call_from_thread(self.log_widget.write, f"[red]ERROR: {e}[/red]")
        finally:
            self.call_from_thread(self._pipeline_done)

    def _pipeline_done(self):
        self.pipeline_running = False
        self.log_widget.write("[bold green]=== Pipeline finished ===[/bold green]")


def main():
    app = VideoEditTUI()
    app.run()


if __name__ == "__main__":
    main()
