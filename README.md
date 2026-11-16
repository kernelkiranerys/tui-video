# tui-video

A terminal UI for an end-to-end vertical-video (TikTok / Reels) production
pipeline: drop a video file, transcribe it with WhisperX, ask a Gemini or
local Qwen LLM to pick the most engaging 60-second segments, cut them with
ffmpeg, optionally crop to portrait, prepend a hook + brand intro, and burn a
watermark — all from a single [Textual](https://textual.textualize.io/) app.

The TUI drives the pipeline in a background thread and streams per-stage
progress to a live RichLog panel.

## Pipeline stages

1. **Crop** (optional) — extract one half / third of the frame for portrait phone output.
2. **WhisperX transcription** — runs in a separate nix shell (with `--diarize`
   so multi-speaker content gets speaker labels).
3. **LLM clip selection** — asks Gemini (or local Qwen via Ollama) for 10
   clips with hook timestamps, snapped to actual subtitle segment boundaries.
4. **ffmpeg cut** — slices each clip, optionally prepends a hook + brand intro,
   optionally burns a 142×142 watermark, optionally extracts + enhances audio
   to FLAC (highpass/lowpass/FFT denoise/compressor/loudnorm).
5. **Multi-speaker switcher** — for diarized content, builds a portrait short
   that crops to the active speaker's half each turn and concatenates.

## TUI options

- File browser modal (or pass a path as `argv[1]`)
- Speaker mode: single / multi
- Crop mode: left-half / right-half / left-third / centre-third / right-third
- Format: short (~60s) or long
- Hooks: include / skip
- Watermark: include / skip
- Live log panel (per-session timestamped log file is also written to `logs/`)

## Tech stack

- Python 3 + [Textual](https://textual.textualize.io/) TUI framework
- [WhisperX](https://github.com/m-bain/whisperX) (transcription + diarization,
  run inside a `whisperx-shell.nix` venv)
- Google Gemini (default) or local Ollama (`qwen2.5:14b-instruct-q4_K_M`)
  for clip selection + hook generation
- ffmpeg / ffprobe (crop, cut, concat, watermark, audio enhance)
- `srt_ops.py` — SRT/VTT parser into `SubtitleSegment` dataclasses with
  speaker labels from WhisperX diarization

## Run

```
./launch.sh            # nix-shell + venv + TUI

# or manually:
nix-shell ./whisperx-shell.nix --run "python tui.py [optional/video/path]"
```

Required env vars (read by `llm_ops.py` / `pipeline.py`):

```
GEMINI_API_KEY=...      # required when using the Gemini LLM path
HF_TOKEN=...            # required by WhisperX for the pyannote diarization model
OLLAMA_HOST=http://localhost:11434   # optional (defaults to local)
```

The pipeline logs every stage to a timestamped file under `logs/`.

## License

MIT — see [LICENSE](LICENSE).
