import re
import subprocess
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pipeline.srt")


@dataclass
class SubtitleSegment:
    start: float
    end: float
    speaker: str
    text: str


def parse_srt(srt_path: str) -> list[SubtitleSegment]:
    log.info(f"parse_srt: {srt_path}")
    content = Path(srt_path).read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", content.strip())
    segments = []
    skipped = 0
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            skipped += 1
            continue
        time_line = lines[1]
        time_match = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", time_line
        )
        if not time_match:
            skipped += 1
            continue
        start = _srt_time_to_seconds(time_match.group(1))
        end = _srt_time_to_seconds(time_match.group(2))
        text_lines = lines[2:]
        full_text = " ".join(text_lines)
        speaker_match = re.match(r"\[?(SPEAKER_\d+)\]?:?\s*(.*)", full_text)
        if speaker_match:
            speaker = speaker_match.group(1)
            text = speaker_match.group(2).strip()
        else:
            speaker = "SPEAKER_UNKNOWN"
            text = full_text
        segments.append(SubtitleSegment(start, end, speaker, text))
    log.info(f"parse_srt: {len(segments)} segments, {skipped} skipped")
    return segments


def parse_vtt(vtt_path: str) -> list[SubtitleSegment]:
    content = Path(vtt_path).read_text(encoding="utf-8")
    if content.startswith("WEBVTT"):
        content = "\n".join(content.split("\n")[1:])
    return parse_srt_from_text(content)


def parse_srt_from_text(content: str) -> list[SubtitleSegment]:
    blocks = re.split(r"\n\s*\n", content.strip())
    segments = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        time_line = None
        text_start = 0
        for i, line in enumerate(lines):
            if "-->" in line:
                time_line = line
                text_start = i + 1
                break
        if not time_line:
            continue
        time_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})", time_line
        )
        if not time_match:
            continue
        start = _srt_time_to_seconds(time_match.group(1).replace(".", ","))
        end = _srt_time_to_seconds(time_match.group(2).replace(".", ","))
        text_lines = lines[text_start:]
        full_text = " ".join(text_lines)
        speaker_match = re.match(r"\[?(SPEAKER_\d+)\]?:?\s*(.*)", full_text)
        if speaker_match:
            speaker = speaker_match.group(1)
            text = speaker_match.group(2).strip()
        else:
            speaker = "SPEAKER_UNKNOWN"
            text = full_text
        segments.append(SubtitleSegment(start, end, speaker, text))
    return segments


def _srt_time_to_seconds(time_str: str) -> float:
    h, m, s_ms = time_str.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def seconds_to_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def get_speakers(segments: list[SubtitleSegment]) -> list[str]:
    seen = []
    for seg in segments:
        if seg.speaker not in seen:
            seen.append(seg.speaker)
    log.debug(f"get_speakers: {seen}")
    return seen


def get_segments_for_speaker(
    segments: list[SubtitleSegment], speaker: str
) -> list[SubtitleSegment]:
    return [seg for seg in segments if seg.speaker == speaker]


def format_transcript_for_llm(segments: list[SubtitleSegment]) -> str:
    lines = []
    for seg in segments:
        timestamp = f"[{seconds_to_srt_time(seg.start)} - {seconds_to_srt_time(seg.end)}]"
        lines.append(f"{timestamp} {seg.speaker}: {seg.text}")
    return "\n".join(lines)


def get_video_duration(video_path: str) -> float:
    log.debug(f"get_video_duration: {video_path}")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    import json

    if result.returncode != 0:
        log.error(f"get_video_duration: ffprobe failed: {result.stderr[:200]}")
        raise RuntimeError(f"ffprobe failed for {video_path}: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    dur = float(data["format"]["duration"])
    log.debug(f"get_video_duration: {dur:.1f}s ({dur/60:.1f}min)")
    return dur


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    log.debug(f"get_video_dimensions: {video_path}")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-print_format",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    import json

    if result.returncode != 0:
        log.error(f"get_video_dimensions: ffprobe failed: {result.stderr[:200]}")
        raise RuntimeError(f"ffprobe failed for {video_path}: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    dims = (int(stream["width"]), int(stream["height"]))
    log.debug(f"get_video_dimensions: {dims[0]}x{dims[1]}")
    return dims
