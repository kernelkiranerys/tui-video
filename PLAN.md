# Implementation Plan: Watermark + Intro

## Assets

- **Watermark:** `./assets/watermark.png` (142x142 PNG)
- **Intro:** `./assets/intro.mp4` (1318x2604, ~0.5s, h264 + aac)
- **Watermark size:** 15% of video width, top-right corner, 10px padding
- **Intro placement:** Between hook and main clip (only when hooks are enabled)

## Pipeline Flows

### Single-speaker shorts

| Hooks | Watermark | Flow |
|-------|-----------|------|
| Yes | Yes | Cut hook (no watermark) → Cut clip with watermark → Concat [hook, intro(scaled), watermarked_clip] |
| Yes | No | Cut hook → Cut clip → Concat [hook, intro(scaled), clip] |
| No | Yes | Cut clip with watermark → Done (no intro) |
| No | No | Cut clip → Done (current behaviour) |

### Multi-speaker shorts

| Watermark | Flow |
|-----------|------|
| Yes | Each segment: crop+scale to 1080x1920 + watermark overlay (162px) → concat all |
| No | Current behaviour (no watermark in filter chain) |

### Long form

| Watermark | Flow |
|-----------|------|
| Yes | overlay_watermark() on full video (re-encode) |
| No | Current behaviour (subs alongside video) |

### Audio-only

Always skip watermark and intro.

## File-by-File Changes

### 1. `ffmpeg_ops.py` — Three new functions + one modified

#### `cut_clip_with_watermark(input_path, output_path, start, duration, watermark_path, width_percent=0.15)`

Cuts a clip AND overlays watermark in a single ffmpeg pass (avoids double re-encoding).

- Gets video dimensions via `get_video_dimensions()`
- Calculates `wm_width = int(width * width_percent)`
- Uses `filter_complex`:
  ```
  [1:v]scale={wm_width}:-1[wm];
  [0:v][wm]overlay=W-w-10:10[v]
  ```
- Scales watermark maintaining aspect ratio (`-1` for height)
- Overlays at top-right with 10px padding (`W-w-10:10`)
- Output: re-encoded clip with watermark burned in

#### `overlay_watermark(input_path, output_path, watermark_path, width_percent=0.15)`

Applies watermark to an existing video file (for long form).

- Same scaling and overlay logic as above
- Re-encodes the full video
- Used when the video isn't being cut (long form path)

#### `concat_with_intro(hook_path, intro_path, clip_path, output_path)`

Concatenates hook + intro + clip using `filter_complex` (not concat demuxer) to handle resolution mismatch.

- Gets clip dimensions (W, H) via `get_video_dimensions()`
- Scales intro to match clip:
  ```
  [1:v]scale={W}:{H}[intro_v]
  ```
- Resamples intro audio to match:
  ```
  [1:a]aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[intro_a]
  ```
- Concatenates 3 segments:
  ```
  [0:v][0:a][intro_v][intro_a][2:v][2:a]concat=n=3:v=1:a=1[v][a]
  ```
- Handles any resolution combination automatically

#### Modified: `create_speaker_switched_video()`

- Accepts optional `watermark_path` parameter
- If provided, each segment's filter chain becomes:
  ```
  [0:v]crop=...,scale=1080:1920[base];
  [1:v]scale=162:-1[wm];
  [base][wm]overlay=W-w-10:10[v]
  ```
- Watermark is 15% of 1080 = 162px for portrait output

### 2. `pipeline.py` — Config + method updates

#### `PipelineConfig` — New fields

```python
watermark: bool = False
watermark_path: str = "./assets/watermark.png"
intro_path: str = "./assets/intro.mp4"
```

#### `step_shorts_single()` — Updated logic

```
if watermark AND want_hooks:
    cut_clip(hook)                          # no watermark on hook
    cut_clip_with_watermark(clip)           # watermark on main clip
    concat_with_intro(hook, intro, clip)    # [hook, intro, watermarked_clip]

elif watermark AND NOT want_hooks:
    cut_clip_with_watermark(clip)           # watermark on main clip, no intro

elif NOT watermark AND want_hooks:
    cut_clip(hook)
    cut_clip(clip)
    concat_with_intro(hook, intro, clip)    # [hook, intro, clip]

else:
    cut_clip(clip)                          # current behaviour
```

#### `step_shorts_multi()` — Updated

- Pass `watermark_path` to `create_speaker_switched_video()` if `config.watermark` is True
- Otherwise pass `None` (current behaviour)

#### `step_long_form()` — Updated

- If `config.watermark`: call `overlay_watermark()` on the full video
- Otherwise: current behaviour (subs alongside video)

#### `step_long_audio()` — No change

- Always skip watermark and intro

### 3. `tui.py` — UI additions

- Add `watermark` reactive variable (bool)
- Add "Watermark: Yes/No" toggle section in compose()
- Show for all output types except audio-only
- Hide when audio-only is selected (like hooks section)
- Pass `watermark` to `PipelineConfig`

### 4. `logger.py` — No changes needed

Existing logging in `ffmpeg_ops.py` and `pipeline.py` will automatically cover new functions.

## Technical Details

### Watermark scaling calculation (Python-side)

```python
width, height = get_video_dimensions(input_path)
wm_width = int(width * width_percent)
# Examples:
#   426px wide (single-speaker shorts):  wm_width = 63
#   1080px wide (multi-speaker portrait): wm_width = 162
#   1280px wide (long form):              wm_width = 192
```

### Intro scaling in concat (ffmpeg-side)

The intro is 1318x2604. It gets scaled to match the hook/clip dimensions (e.g. 426x720). This will stretch it since the aspect ratios differ, but at 0.5s duration it's imperceptible.

### Audio sync in concat

All three segments (hook, intro, clip) have audio. The intro's audio is the swoosh at 44100Hz mono. The concat filter handles resampling automatically, but we explicitly resample to match to avoid issues:

```
[1:a]aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[intro_a]
```

### Concat method: filter_complex vs concat demuxer

Using `filter_complex` with the `concat` filter instead of the concat demuxer (`-f concat`) because:
- The concat demuxer requires identical codecs/resolutions across all inputs
- The intro (1318x2604) differs from clips (426x720 or 1080x1920)
- `filter_complex` scales and resamples within the same ffmpeg pass

## Output Examples

### Single-speaker short with hooks + watermark

```
[0.5s hook from middle of clip, no watermark]
  → [0.5s Readers-Digressed logo + swoosh intro, scaled to 426x720]
  → [60s main clip with watermark in top-right corner]
  = ~61s final output
```

### Multi-speaker portrait short with watermark

```
[Speaker 1 talking → cropped to left half, scaled to 1080x1920, watermark top-right]
  → [Speaker 2 talking → cropped to right half, scaled to 1080x1920, watermark top-right]
  → [Speaker 1 talking → ...]
  = continuous portrait video with watermark throughout
```

### Long form with watermark

```
[Full 36-minute video with watermark in top-right corner, re-encoded]
  + SRT/VTT alongside
```
