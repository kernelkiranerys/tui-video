import os
import subprocess
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from srt_ops import (
    parse_srt,
    get_speakers,
    get_video_duration,
    seconds_to_srt_time,
)
from ffmpeg_ops import (
    crop_video,
    cut_clip,
    cut_clip_with_watermark,
    overlay_watermark,
    concat_clips,
    concat_with_intro,
    crop_to_portrait,
    extract_audio,
    enhance_audio,
    create_speaker_switched_video,
)
from llm_ops import select_clips

log = logging.getLogger("pipeline")

WHISPERX_CMD = [
    "nix-shell",
    "./whisperx-shell.nix",
    "--run",
]

HF_TOKEN = os.environ.get("HF_TOKEN", "")
WHISPERX_MODEL = "large-v2"
DIARIZE_MODEL = "pyannote/speaker-diarization-3.1"


@dataclass
class PipelineConfig:
    video_path: str
    speaker_mode: str  # "single" or "multi"
    format_mode: str  # "shorts" or "long"
    crop_mode: str = ""  # "left_half", "right_half", etc.
    left_speaker: str = ""
    right_speaker: str = ""
    want_hooks: bool = False
    audio_only: bool = False
    watermark: bool = False
    watermark_path: str = "./assets/watermark.png"
    intro_path: str = "./assets/intro.mp4"


class Pipeline:
    def __init__(self, config: PipelineConfig, progress_cb: Callable = None):
        self.config = config
        self.progress_cb = progress_cb or (lambda msg: None)
        self.video_path = config.video_path
        self.work_dir = Path(config.video_path).parent
        self.stem = Path(config.video_path).stem
        self.segments = []
        log.info(f"Pipeline initialized: video={config.video_path}")
        log.info(f"Config: speaker_mode={config.speaker_mode} format_mode={config.format_mode} "
                 f"crop_mode={config.crop_mode} want_hooks={config.want_hooks} audio_only={config.audio_only}")
        log.info(f"Work dir: {self.work_dir}, stem: {self.stem}")

    def _log(self, msg: str):
        log.info(msg)
        self.progress_cb(msg)

    def run_whisperx(self, video_path: str = None) -> str:
        target = video_path or self.video_path
        log.info(f"run_whisperx: target={target}")
        self._log(f"Running WhisperX on {target}...")
        output_dir = str(Path(target).parent)
        run_cmd = (
            f"source $HOME/.whisperx-venv/bin/activate && "
            f"whisperx '{target}' --model {WHISPERX_MODEL} --diarize "
            f"--batch_size 1 --diarize_model {DIARIZE_MODEL} "
            f"--hf_token {HF_TOKEN} --output_dir '{output_dir}'"
        )
        cmd = WHISPERX_CMD + [run_cmd]
        log.debug(f"run_whisperx: command={' '.join(cmd[:3])} ...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"run_whisperx: FAILED (code {result.returncode})")
            log.error(f"run_whisperx: stderr (last 500): {result.stderr[-500:]}")
            log.error(f"run_whisperx: stdout (last 500): {result.stdout[-500:]}")
            raise RuntimeError(f"WhisperX failed: {result.stderr[-500:]}")
        srt_path = str(Path(target).with_suffix(".srt"))
        log.info(f"run_whisperx: success, SRT at {srt_path}")
        self._log(f"WhisperX complete. SRT: {srt_path}")
        return srt_path

    def load_subtitles(self, srt_path: str):
        log.info(f"load_subtitles: {srt_path}")
        self._log("Parsing subtitles...")
        self.segments = parse_srt(srt_path)
        speakers = get_speakers(self.segments)
        self._log(f"Found {len(self.segments)} segments, {len(speakers)} speakers: {speakers}")
        log.info(f"load_subtitles: {len(self.segments)} segments, speakers={speakers}")
        return speakers

    def step_crop(self) -> str:
        if not self.config.crop_mode:
            log.info("step_crop: no crop mode set, skipping")
            return self.video_path
        log.info(f"step_crop: mode={self.config.crop_mode}")
        self._log(f"Cropping video ({self.config.crop_mode})...")
        output = str(self.work_dir / f"{self.stem}_cropped.mp4")
        crop_video(self.video_path, output, self.config.crop_mode)
        self.video_path = output
        log.info(f"step_crop: video_path now {output}")
        self._log(f"Cropped to {output}")
        return output

    def step_subtitles(self) -> str:
        log.info("step_subtitles: starting")
        srt_path = self.run_whisperx(self.video_path)
        self.load_subtitles(srt_path)
        log.info("step_subtitles: done")
        return srt_path

    def step_shorts_single(self, srt_path: str):
        log.info("step_shorts_single: starting")
        self._log("Selecting clips with Gemini...")
        duration = get_video_duration(self.video_path)
        log.info(f"step_shorts_single: video duration={duration:.1f}s, segments={len(self.segments)}")
        try:
            clips = select_clips(self.segments, duration)
        except Exception as e:
            log.error(f"step_shorts_single: Gemini failed: {e}", exc_info=True)
            self._log(f"[red]Gemini clip selection failed: {e}[/red]")
            return str(self.work_dir)
        self._log(f"Gemini selected {len(clips)} clips")
        log.info(f"step_shorts_single: {len(clips)} clips selected")

        if len(clips) < 10:
            log.warning(f"step_shorts_single: only {len(clips)} clips (expected 10)")
            self._log(f"[yellow]WARNING: Only got {len(clips)} clips instead of 10[/yellow]")

        for i, clip in enumerate(clips):
            self._log(f"  Clip {i+1}: {clip.get('reason', 'N/A')}")
            if clip.get("hook_text"):
                self._log(f"    Hook: \"{clip['hook_text']}\"")
            log.info(f"step_shorts_single: clip {i+1} start={clip['start']:.1f}s end={clip['end']:.1f}s "
                     f"hook@{clip.get('hook_start',0):.1f}s-{clip.get('hook_end',0):.1f}s")

        shorts_dir = self.work_dir / f"{self.stem}_shorts"
        shorts_dir.mkdir(exist_ok=True)
        log.info(f"step_shorts_single: output dir={shorts_dir}")

        wm = self.config.watermark
        wm_path = self.config.watermark_path if wm else None
        intro = self.config.intro_path
        want_hooks = self.config.want_hooks

        for i, clip in enumerate(clips):
            start = clip["start"]
            end = clip["end"]
            clip_dur = end - start
            label = f"short_{i+1:02d}_{int(start)}s"
            self._log(f"Processing clip {i+1}/{len(clips)}: {start:.1f}s - {end:.1f}s ({clip_dur:.0f}s)")
            log.info(f"step_shorts_single: processing clip {i+1} label={label} wm={wm} hooks={want_hooks}")

            clip_path = str(shorts_dir / f"{label}.mp4")

            if wm:
                cut_clip_with_watermark(self.video_path, clip_path, start, clip_dur, wm_path)
            else:
                cut_clip(self.video_path, clip_path, start, clip_dur)

            txt_path = str(shorts_dir / f"{label}.txt")
            clip_segs = [s for s in self.segments if s.start >= start and s.end <= end]
            transcript_lines = "\n".join(
                f"[{seconds_to_srt_time(s.start)}] {s.speaker}: {s.text}" for s in clip_segs
            )

            caption = clip.get("caption", "")
            hook_text = clip.get("hook_text", "")
            txt_content = f"HOOK: {hook_text}\nCAPTION: {caption}\n\nTRANSCRIPT:\n{transcript_lines}"
            Path(txt_path).write_text(txt_content)
            log.debug(f"step_shorts_single: wrote txt {txt_path}")

            if want_hooks:
                try:
                    hook_start = clip["hook_start"]
                    hook_end = clip["hook_end"]
                    hook_dur = hook_end - hook_start
                    self._log(f"  Hook: \"{hook_text[:60]}\" ({hook_dur:.1f}s) at {hook_start:.1f}s")

                    hook_path = str(shorts_dir / f"{label}_hook.mp4")
                    cut_clip(self.video_path, hook_path, hook_start, hook_dur)

                    final_path = str(shorts_dir / f"{label}_final.mp4")
                    concat_with_intro(hook_path, intro, clip_path, final_path)
                    Path(hook_path).unlink(missing_ok=True)
                    Path(final_path).rename(clip_path)
                    self._log(f"  Hook + intro prepended to clip {i+1}")
                except Exception as e:
                    log.error(f"step_shorts_single: hook failed for clip {i+1}: {e}", exc_info=True)
                    self._log(f"  [red]Hook failed for clip {i+1}: {e}[/red]")
                    self._log(f"  Clip {i+1} saved without hook")

        self._log(f"Done! {len(clips)} shorts saved to {shorts_dir}")
        return str(shorts_dir)

    def step_shorts_multi(self, srt_path: str):
        log.info("step_shorts_multi: starting")
        self._log("Creating speaker-switched portrait video...")
        output = str(self.work_dir / f"{self.stem}_shorts_multi.mp4")
        wm_path = self.config.watermark_path if self.config.watermark else None
        log.info(f"step_shorts_multi: left={self.config.left_speaker} right={self.config.right_speaker} "
                 f"watermark={'yes' if wm_path else 'no'}")
        create_speaker_switched_video(
            self.video_path,
            output,
            self.segments,
            self.config.left_speaker,
            self.config.right_speaker,
            progress_callback=lambda done, total: self._log(f"  Segment {done}/{total}"),
            watermark_path=wm_path,
        )
        self._log(f"Multi-speaker short saved to {output}")

        shorts_dir = self.work_dir / f"{self.stem}_shorts"
        shorts_dir.mkdir(exist_ok=True)
        shutil.move(output, str(shorts_dir / Path(output).name))
        log.info(f"step_shorts_multi: moved to {shorts_dir}")
        self._log(f"Done! Saved to {shorts_dir}")
        return str(shorts_dir)

    def step_long_form(self, srt_path: str):
        log.info("step_long_form: starting")
        vtt_path = str(Path(self.video_path).with_suffix(".vtt"))
        self._log(f"Subtitles saved alongside video: {srt_path}, {vtt_path}")
        log.info(f"step_long_form: srt={srt_path} vtt={vtt_path}")

        if self.config.watermark:
            self._log("Applying watermark...")
            wm_output = str(self.work_dir / f"{self.stem}_watermarked.mp4")
            overlay_watermark(self.video_path, wm_output, self.config.watermark_path)
            self.video_path = wm_output
            self._log(f"Watermarked video saved to {wm_output}")

        return srt_path

    def step_long_audio(self, srt_path: str):
        log.info("step_long_audio: starting")
        self._log("Extracting audio...")
        raw_audio = str(self.work_dir / f"{self.stem}_raw.wav")
        extract_audio(self.video_path, raw_audio)

        self._log("Enhancing audio for podcast quality...")
        enhanced = str(self.work_dir / f"{self.stem}_enhanced.flac")
        enhance_audio(raw_audio, enhanced)

        Path(raw_audio).unlink(missing_ok=True)
        log.info(f"step_long_audio: enhanced audio at {enhanced}")
        self._log(f"Enhanced audio saved to {enhanced}")
        return enhanced

    def run(self):
        log.info("=== Pipeline run() started ===")
        self._log("=== Pipeline started ===")

        if self.config.speaker_mode == "single":
            self.step_crop()
            srt_path = self.step_subtitles()
        else:
            srt_path = self.step_subtitles()

        if self.config.format_mode == "shorts":
            if self.config.speaker_mode == "single":
                self.step_shorts_single(srt_path)
            else:
                self.step_shorts_multi(srt_path)
        elif self.config.format_mode == "long":
            if self.config.audio_only:
                self.step_long_audio(srt_path)
            else:
                self.step_long_form(srt_path)

        log.info("=== Pipeline run() complete ===")
        self._log("=== Pipeline complete ===")
