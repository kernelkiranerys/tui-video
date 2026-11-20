import json
import re
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:14b-instruct-q4_K_M"

MIN_SEGMENT_WORDS = 5
MIN_CLIP_WORDS = 40


def call_ollama(prompt: str, model: str = DEFAULT_MODEL, temperature: float = 0.7) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        },
        timeout=600,
    )
    response.raise_for_status()
    return response.json()["response"]


def filter_interesting_segments(segments: list) -> list:
    filtered = []
    for seg in segments:
        word_count = len(seg.text.split())
        if word_count >= MIN_SEGMENT_WORDS:
            filtered.append(seg)
    return filtered


def build_context_around(segments: list, seg, window: int = 2) -> str:
    idx = segments.index(seg) if seg in segments else -1
    if idx == -1:
        return seg.text
    start = max(0, idx - window)
    end = min(len(segments), idx + window + 1)
    lines = []
    for s in segments[start:end]:
        marker = ">>>" if s is seg else "   "
        lines.append(f"{marker} [{s.start:.1f}s] {s.speaker}: {s.text}")
    return "\n".join(lines)


def select_clips(segments: list, video_duration: float) -> list[dict]:
    interesting = filter_interesting_segments(segments)

    formatted = []
    for seg in interesting:
        formatted.append(f"[{seg.start:.1f}s - {seg.end:.1f}s] {seg.speaker}: {seg.text}")
    transcript = "\n".join(formatted)

    duration_min = video_duration / 60

    prompt = f"""You are an expert short-form video producer who has created thousands of viral TikTok and YouTube Shorts clips. You know exactly what makes people stop scrolling.

You are given a transcript ({duration_min:.0f} minutes long) with timestamps. You MUST select EXACTLY 5 clips for short-form content.

CRITICAL RULES:
1. You MUST return exactly 5 clips - no more, no less
2. Each clip MUST be 45-75 seconds long
3. Each clip MUST contain actual substantive content - someone making a point, telling a story, sharing an opinion, or making an argument
4. NEVER select clips that are mostly silence, "yeah", "okay", greetings, or filler
5. Select clips from DIFFERENT parts of the video - spread across the beginning, middle, and end
6. Prefer clips where someone is passionate, controversial, funny, or sharing expertise
7. Each clip should make sense as a standalone piece of content

TRANSCRIPT (only segments with 5+ words are shown):
{transcript}

You MUST respond with ONLY a JSON array. No markdown, no backticks, no explanation before or after. Exactly this format:

[{{"start": 123.4, "end": 183.4, "reason": "One sentence explaining why this is compelling"}}, {{"start": 456.7, "end": 516.7, "reason": "..."}}, {{"start": 789.0, "end": 849.0, "reason": "..."}}, {{"start": 1020.5, "end": 1080.5, "reason": "..."}}, {{"start": 1350.2, "end": 1410.2, "reason": "..."}}]

Return exactly 5 clips now:"""

    raw = call_ollama(prompt, temperature=0.4)

    clips = _extract_json_array(raw)
    if len(clips) < 5:
        if len(clips) > 0:
            for clip in clips:
                clip.setdefault("reason", "")
            retry_prompt = f"""You previously selected only {len(clips)} clips. You MUST select exactly 5. Here is the transcript again. Select {5 - len(clips)} MORE clips from parts of the video NOT covered by these timestamps: {[{{"start": c["start"], "end": c["end"]}} for c in clips]}

TRANSCRIPT:
{transcript}

Return ONLY a JSON array of {5 - len(clips)} additional clips in this format:
[{{"start": 123.4, "end": 183.4, "reason": "..."}}]

Return {5 - len(clips)} clips now:"""
            retry_raw = call_ollama(retry_prompt, temperature=0.5)
            retry_clips = _extract_json_array(retry_raw)
            clips.extend(retry_clips)

    clips = clips[:5]

    for clip in clips:
        clip["start"] = float(clip["start"])
        clip["end"] = float(clip["end"])
        dur = clip["end"] - clip["start"]
        if dur < 30:
            clip["end"] = clip["start"] + 60
        elif dur > 90:
            clip["end"] = clip["start"] + 60
        if clip["end"] > video_duration:
            clip["end"] = video_duration
        if clip["start"] < 0:
            clip["start"] = 0

    return clips


def _extract_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "clips" in result:
            return result["clips"]
    except json.JSONDecodeError:
        pass

    arrays = re.findall(r"\[[\s\S]*?\]", raw)
    for arr_str in reversed(arrays):
        try:
            parsed = json.loads(arr_str)
            if isinstance(parsed, list) and len(parsed) > 0:
                return parsed
        except json.JSONDecodeError:
            continue

    objects = re.findall(r"\{[^{}]*\}", raw)
    if objects:
        clips = []
        for obj_str in objects:
            try:
                clips.append(json.loads(obj_str))
            except json.JSONDecodeError:
                continue
        if clips:
            return clips

    return []


def select_hook(segments: list, clip_start: float, clip_end: float) -> dict:
    clip_segs = [s for s in segments if s.start >= clip_start and s.end <= clip_end]
    interesting = [s for s in clip_segs if len(s.text.split()) >= 4]

    if not interesting:
        if clip_segs:
            return {
                "start": clip_segs[0].start,
                "end": min(clip_segs[0].start + 3, clip_end),
                "text": clip_segs[0].text,
            }
        return {"start": clip_start, "end": clip_start + 2, "text": ""}

    formatted = []
    for seg in interesting:
        formatted.append(f"[{seg.start:.1f}s - {seg.end:.1f}s] {seg.text}")
    transcript_segment = "\n".join(formatted)

    clip_dur = clip_end - clip_start

    prompt = f"""You are a viral short-form video editor. Your job is to pick the single best HOOK from this {clip_dur:.0f}-second clip.

A hook is the most punchy, surprising, controversial, or bold statement - the thing that makes someone stop scrolling immediately.

TRANSCRIPT:
{transcript_segment}

Pick ONE moment that is 1-3 seconds long. Respond with ONLY this JSON (no markdown, no explanation):
{{"start": 125.3, "end": 127.5, "text": "exact words"}}

The start/end must be exact timestamps from the transcript above. Pick the hook now:"""

    raw = call_ollama(prompt, temperature=0.6)

    raw = raw.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        hook = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\}", raw)
        if match:
            try:
                hook = json.loads(match.group())
            except json.JSONDecodeError:
                best = max(interesting, key=lambda s: len(s.text.split()))
                hook = {"start": best.start, "end": min(best.start + 3, clip_end), "text": best.text}
        else:
            best = max(interesting, key=lambda s: len(s.text.split()))
            hook = {"start": best.start, "end": min(best.start + 3, clip_end), "text": best.text}

    hook["start"] = float(hook["start"])
    hook["end"] = float(hook["end"])

    if hook["end"] - hook["start"] > 5:
        hook["end"] = hook["start"] + 3
    if hook["end"] - hook["start"] < 0.5:
        hook["end"] = hook["start"] + 2
    if hook["start"] < clip_start:
        hook["start"] = clip_start
    if hook["end"] > clip_end:
        hook["end"] = min(clip_start + 3, clip_end)

    return hook


def format_segment_for_llm(segments: list, start: float, end: float) -> str:
    from srt_ops import seconds_to_srt_time

    lines = []
    for seg in segments:
        if seg.start >= start and seg.end <= end:
            ts = f"[{seconds_to_srt_time(seg.start)} - {seconds_to_srt_time(seg.end)}]"
            lines.append(f"{ts} {seg.speaker}: {seg.text}")
    return "\n".join(lines) if lines else "(no transcript in this range)"
