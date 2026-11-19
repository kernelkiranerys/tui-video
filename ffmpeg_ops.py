import subprocess
import shutil
import logging
from pathlib import Path

from srt_ops import get_video_dimensions, get_video_duration

log = logging.getLogger("pipeline.ffmpeg")


def run_ffmpeg(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + args
    log.debug(f"run_ffmpeg: {' '.join(cmd[:20])}{'...' if len(cmd)>20 else ''}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"run_ffmpeg FAILED (code {result.returncode}): {result.stderr[:500]}")
        if check:
            raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    else:
        log.debug(f"run_ffmpeg: success")
    return result


def crop_video(
    input_path: str,
    output_path: str,
    crop_mode: str,
) -> str:
    width, height = get_video_dimensions(input_path)
    log.info(f"crop_video: {input_path} ({width}x{height}) mode={crop_mode}")

    if crop_mode == "left_half":
        crop = f"{width // 2}:{height}:0:0"
    elif crop_mode == "right_half":
        crop = f"{width // 2}:{height}:{width // 2}:0"
    elif crop_mode == "first_third":
        crop = f"{width // 3}:{height}:0:0"
    elif crop_mode == "second_third":
        crop = f"{width // 3}:{height}:{width // 3}:0"
    elif crop_mode == "third_third":
        crop = f"{width // 3}:{height}:{2 * (width // 3)}:0"
    else:
        log.error(f"crop_video: unknown crop mode '{crop_mode}'")
        raise ValueError(f"Unknown crop mode: {crop_mode}")

    log.info(f"crop_video: crop filter = crop={crop} -> {output_path}")
    run_ffmpeg(["-i", input_path, "-vf", f"crop={crop}", "-c:a", "copy", output_path])
    log.info(f"crop_video: done")
    return output_path


def cut_clip(
    input_path: str,
    output_path: str,
    start: float,
    duration: float,
) -> str:
    log.info(f"cut_clip: {input_path} start={start:.1f}s dur={duration:.1f}s -> {output_path}")
    run_ffmpeg(
        [
            "-ss", f"{start:.3f}",
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    log.info(f"cut_clip: done")
    return output_path


def cut_clip_with_watermark(
    input_path: str,
    output_path: str,
    start: float,
    duration: float,
    watermark_path: str,
    width_percent: float = 0.15,
) -> str:
    width, height = get_video_dimensions(input_path)
    wm_width = int(width * width_percent)
    log.info(f"cut_clip_with_watermark: {input_path} start={start:.1f}s dur={duration:.1f}s "
             f"wm_width={wm_width}px -> {output_path}")
    run_ffmpeg(
        [
            "-ss", f"{start:.3f}",
            "-i", input_path,
            "-i", watermark_path,
            "-t", f"{duration:.3f}",
            "-filter_complex",
            f"[1:v]scale={wm_width}:-1[wm];[0:v][wm]overlay=W-w-10:10[v]",
            "-map", "[v]",
            "-map", "0:a",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    log.info(f"cut_clip_with_watermark: done")
    return output_path


def overlay_watermark(
    input_path: str,
    output_path: str,
    watermark_path: str,
    width_percent: float = 0.15,
) -> str:
    width, height = get_video_dimensions(input_path)
    wm_width = int(width * width_percent)
    log.info(f"overlay_watermark: {input_path} ({width}x{height}) wm_width={wm_width}px -> {output_path}")
    run_ffmpeg(
        [
            "-i", input_path,
            "-i", watermark_path,
            "-filter_complex",
            f"[1:v]scale={wm_width}:-1[wm];[0:v][wm]overlay=W-w-10:10[v]",
            "-map", "[v]",
            "-map", "0:a",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    log.info(f"overlay_watermark: done")
    return output_path


def concat_with_intro(
    hook_path: str,
    intro_path: str,
    clip_path: str,
    output_path: str,
) -> str:
    width, height = get_video_dimensions(clip_path)
    log.info(f"concat_with_intro: hook={hook_path} intro={intro_path} clip={clip_path} "
             f"target={width}x{height} -> {output_path}")
    run_ffmpeg(
        [
            "-i", hook_path,
            "-i", intro_path,
            "-i", clip_path,
            "-filter_complex",
            f"[1:v]scale={width}:{height},setsar=1[intro_v];"
            f"[1:a]aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[intro_a];"
            f"[0:v][0:a][intro_v][intro_a][2:v][2:a]concat=n=3:v=1:a=1[v][a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    log.info(f"concat_with_intro: done")
    return output_path


def cut_clip_with_subtitles(
    input_path: str,
    output_path: str,
    subtitle_path: str,
    start: float,
    duration: float,
) -> str:
    run_ffmpeg(
        [
            "-ss", f"{start:.3f}",
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-vf", f"subtitles='{subtitle_path}'",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    return output_path


def concat_clips(clip_paths: list[str], output_path: str) -> str:
    log.info(f"concat_clips: {len(clip_paths)} clips -> {output_path}")
    for i, p in enumerate(clip_paths):
        log.debug(f"concat_clips: clip {i+1} = {p}")
    list_file = Path(output_path).parent / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in clip_paths))
    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", output_path]
    )
    list_file.unlink(missing_ok=True)
    log.info(f"concat_clips: done")
    return output_path


def crop_to_portrait(input_path: str, output_path: str, side: str = "center") -> str:
    width, height = get_video_dimensions(input_path)
    target_width = int(height * 9 / 16)
    if target_width > width:
        target_width = width
    if side == "left":
        x_offset = 0
    elif side == "right":
        x_offset = width - target_width
    else:
        x_offset = (width - target_width) // 2
    crop = f"{target_width}:{height}:{x_offset}:0"
    run_ffmpeg(
        [
            "-i", input_path,
            "-vf", f"crop={crop},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ]
    )
    return output_path


def extract_audio(input_path: str, output_path: str) -> str:
    log.info(f"extract_audio: {input_path} -> {output_path}")
    run_ffmpeg(
        [
            "-i", input_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "2",
            output_path,
        ]
    )
    log.info(f"extract_audio: done")
    return output_path


def enhance_audio(input_path: str, output_path: str) -> str:
    log.info(f"enhance_audio: {input_path} -> {output_path}")
    run_ffmpeg(
        [
            "-i", input_path,
            "-af",
            "highpass=f=80,"
            "lowpass=f=16000,"
            "afftdn=nr=10:nf=-25,"
            "acompressor=threshold=-25dB:ratio=2:attack=300:release=2000,"
            "equalizer=f=3000:width_type=q:width=1:g=2,"
            "equalizer=f=6000:width_type=q:width=1:g=1,"
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "48000",
            "-ac", "2",
            "-c:a", "flac",
            output_path,
        ]
    )
    log.info(f"enhance_audio: done")
    return output_path


def create_speaker_switched_video(
    input_path: str,
    output_path: str,
    segments: list,
    left_speaker: str,
    right_speaker: str,
    progress_callback=None,
    watermark_path: str = None,
) -> str:
    width, height = get_video_dimensions(input_path)
    half_width = width // 2
    temp_dir = Path(output_path).parent / "temp_segments"
    temp_dir.mkdir(exist_ok=True)

    wm_width = int(1080 * 0.15) if watermark_path else None
    log.info(f"create_speaker_switched_video: {width}x{height} half={half_width} "
             f"watermark={'yes' if watermark_path else 'no'} wm_width={wm_width}")

    clip_paths = []
    total = len(segments)

    for i, seg in enumerate(segments):
        duration = seg.end - seg.start
        if duration < 0.1:
            continue
        if seg.speaker == left_speaker:
            crop = f"crop={half_width}:{height}:0:0,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        elif seg.speaker == right_speaker:
            crop = f"crop={half_width}:{height}:{half_width}:0,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        else:
            continue

        clip_path = str(temp_dir / f"seg_{i:04d}.mp4")

        if watermark_path:
            vf = f"{crop}[base];[1:v]scale={wm_width}:-1[wm];[base][wm]overlay=W-w-10:10[v]"
            run_ffmpeg(
                [
                    "-ss", f"{seg.start:.3f}",
                    "-i", input_path,
                    "-i", watermark_path,
                    "-t", f"{duration:.3f}",
                    "-filter_complex", vf,
                    "-map", "[v]",
                    "-map", "0:a",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "18",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    clip_path,
                ]
            )
        else:
            run_ffmpeg(
                [
                    "-ss", f"{seg.start:.3f}",
                    "-i", input_path,
                    "-t", f"{duration:.3f}",
                    "-vf", crop,
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "18",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    clip_path,
                ]
            )
        clip_paths.append(clip_path)
        if progress_callback:
            progress_callback(i + 1, total)

    if not clip_paths:
        raise RuntimeError("No segments to concatenate")

    list_file = temp_dir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in clip_paths))
    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", output_path]
    )

    for clip_path in clip_paths:
        Path(clip_path).unlink(missing_ok=True)
    list_file.unlink(missing_ok=True)
    temp_dir.rmdir()

    return output_path
