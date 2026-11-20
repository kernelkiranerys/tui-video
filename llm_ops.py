import json
import os
import re
import time
import requests
import logging
from srt_ops import SubtitleSegment

log = logging.getLogger("pipeline.llm")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-lite"

NUM_CLIPS = 10
CLIP_DURATION = 60
MIN_CLIP_DURATION = 45
MAX_CLIP_DURATION = 75


def detect_best_model(api_key: str, preferred: str = DEFAULT_MODEL) -> str:
    log.debug(f"detect_best_model(preferred={preferred})")
    try:
        r = requests.get(
            f"{GEMINI_API_BASE}/models?key={api_key}",
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"detect_best_model: HTTP {r.status_code}, returning preferred '{preferred}'")
            return preferred
        valid_models = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                valid_models.append(name)
        log.debug(f"detect_best_model: {len(valid_models)} valid models available")
        if preferred in valid_models:
            log.debug(f"detect_best_model: preferred '{preferred}' is available")
            return preferred
        priorities = [
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite-preview",
            "gemini-3-flash-preview",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
        ]
        for p in priorities:
            if p in valid_models:
                log.info(f"detect_best_model: '{preferred}' not found, using '{p}'")
                return p
        if valid_models:
            log.info(f"detect_best_model: '{preferred}' not found, using '{valid_models[0]}'")
            return valid_models[0]
        log.warning(f"detect_best_model: no valid models found, returning '{preferred}'")
        return preferred
    except Exception as e:
        log.error(f"detect_best_model: exception: {e}")
        return preferred


def gemini_request(
    system_prompt: str,
    user_text: str,
    api_key: str = GEMINI_API_KEY,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str | None:
    log.debug(f"gemini_request(model={model}, temp={temperature}, prompt_len={len(user_text)})")
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": user_text}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    last_error = None
    for attempt in range(8):
        log.debug(f"gemini_request: attempt {attempt+1}/8")
        try:
            r = requests.post(
                url,
                json=payload,
                timeout=120,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            last_error = f"Network error: {e}"
            log.warning(f"gemini_request: {last_error}")
            time.sleep(5)
            continue
        log.debug(f"gemini_request: HTTP {r.status_code}")
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30 * (attempt + 1)))
            wait = max(wait, 30 * (attempt + 1))
            last_error = f"Rate limited, waited {wait}s"
            log.warning(f"gemini_request: {last_error}")
            time.sleep(wait)
            continue
        if r.status_code in (503, 502, 504):
            wait = 10 * (attempt + 1)
            last_error = f"HTTP {r.status_code} (server overloaded), retrying in {wait}s"
            log.warning(f"gemini_request: {last_error}")
            time.sleep(wait)
            continue
        if r.status_code == 404:
            new_model = detect_best_model(api_key, model)
            if new_model != model:
                log.info(f"gemini_request: switching model '{model}' -> '{new_model}'")
                url = f"{GEMINI_API_BASE}/models/{new_model}:generateContent?key={api_key}"
                model = new_model
                continue
            last_error = f"Model '{model}' not found (404)"
            log.error(f"gemini_request: {last_error}")
            return None
        if r.status_code != 200:
            last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            log.error(f"gemini_request: {last_error}")
            return None
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            finish_reason = data["candidates"][0].get("finishReason", "?")
            log.info(f"gemini_request: success ({len(text)} chars, finish={finish_reason})")
            log.debug(f"gemini_request: response preview: {text[:200]}")
            return text
        except (KeyError, IndexError) as e:
            last_error = f"Response parse error: {e}. Response: {json.dumps(data)[:300]}"
            log.error(f"gemini_request: {last_error}")
            return None
    log.error(f"gemini_request: exhausted 8 attempts. Last error: {last_error}")
    return None


def _clean_json_text(text: str) -> str:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        text = text[arr_start : arr_end + 1]
        return text
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        text = text[obj_start : obj_end + 1]
    return text


def select_clips(segments: list[SubtitleSegment], video_duration: float) -> list[dict]:
    log.info(f"select_clips: {len(segments)} segments, {video_duration:.0f}s ({video_duration/60:.0f}min)")
    srt_content = _segments_to_srt_text(segments)
    duration_min = video_duration / 60
    log.debug(f"select_clips: SRT text is {len(srt_content)} chars")

    system_prompt = (
        "You are an expert short-form video producer who has created thousands of viral "
        "TikTok and YouTube Shorts clips. You know exactly what makes people stop scrolling. "
        "You respond with ONLY valid JSON — no markdown, no code fences, no preamble, no commentary."
    )

    user_prompt = (
        f"Below is an SRT subtitle file from a {duration_min:.0f}-minute video.\n\n"
        f"You must select exactly {NUM_CLIPS} clips for short-form content.\n\n"
        f"CRITICAL RULES FOR CLIPS:\n"
        f"1. Return exactly {NUM_CLIPS} clips — no more, no less\n"
        f"2. Each clip should be {MIN_CLIP_DURATION}-{MAX_CLIP_DURATION} seconds long\n"
        f"3. NEVER pick moments that are mostly silence, 'yeah', 'okay', greetings, or filler\n"
        f"4. Pick moments where someone is passionate, controversial, funny, or sharing expertise\n"
        f"5. Spread clips across the ENTIRE video — beginning, middle, and end. Do NOT cluster them.\n"
        f"6. Each clip must make sense as standalone content\n\n"
        f"HOOK RULES (IMPORTANT):\n"
        f"For each clip you must also identify a HOOK — a 1-2 sentence moment from WITHIN the "
        f"clip that will be played BEFORE the full clip as a teaser.\n"
        f"The hook structure is: [hook plays] → [full clip plays from the beginning].\n"
        f"This means:\n"
        f"  - The hook must NOT be the opening line of the clip. If it is, the viewer hears "
        f"the same line twice in a row, which is jarring and amateurish.\n"
        f"  - Pick the hook from the MIDDLE or END of the clip — something that teases what's "
        f"coming and makes the viewer want to keep watching to hear it in context.\n"
        f"  - The best hooks are shocking, controversial, or create a 'wait, what?' moment "
        f"that only makes sense when you hear the full clip.\n"
        f"  - DO NOT rephrase, rewrite, or shorten the hook text. The hook_text MUST be the "
        f"EXACT words as they appear in the transcript \u2014 word for word. The audio will be "
        f"cut at these timestamps, so the viewer will hear the original speech. If you "
        f"rephrase it, the text won't match what they hear.\n"
        f"  - hook_start and hook_end must be timestamps from the transcript that fall WITHIN "
        f"the clip's start-end range, but NOT in the first 5 seconds of the clip.\n\n"
        f"For each clip provide:\n"
        f'  - "start": clip start time in HH:MM:SS format\n'
        f'  - "length": clip length in seconds (integer, {MIN_CLIP_DURATION}-{MAX_CLIP_DURATION})\n'
        f'  - "hook_start": hook start time in HH:MM:SS format (from MIDDLE/END of clip, NOT the opening)\n'
        f'  - "hook_end": hook end time in HH:MM:SS format\n'
        f'  - "hook_text": EXACT transcript text at these timestamps \u2014 do not rephrase\n'
        f'  - "caption": 1-2 sentence TikTok/Instagram caption (no hashtags, no emojis)\n'
        f'  - "reason": one sentence explaining why this clip is compelling\n\n'
        f"Return ONLY a JSON array with this exact shape:\n"
        f'[{{"start": "00:02:19", "length": 60, "hook_start": "00:02:45", "hook_end": "00:02:48", '
        f'"hook_text": "...", "caption": "...", "reason": "..."}}]\n\n'
        f"SRT content:\n"
        f"{srt_content}"
    )

    raw = gemini_request(system_prompt, user_prompt, temperature=0.7, max_tokens=4096)
    if not raw:
        log.error("select_clips: Gemini returned no response after all retries")
        raise RuntimeError("Gemini unavailable after 8 retries (503/overloaded). Check log for details.")

    cleaned = _clean_json_text(raw)
    log.debug(f"select_clips: cleaned JSON ({len(cleaned)} chars)")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.error(f"select_clips: JSON parse failed: {e}")
        log.debug(f"select_clips: raw response (first 500): {raw[:500]}")
        raise RuntimeError(f"Failed to parse Gemini JSON: {e}. Raw (first 300): {raw[:300]}")

    if isinstance(parsed, dict) and "clips" in parsed:
        log.debug("select_clips: response was wrapped in {\"clips\": [...]}, unwrapping")
        parsed = parsed["clips"]
    if not isinstance(parsed, list):
        log.error(f"select_clips: expected list, got {type(parsed)}")
        return []

    log.info(f"select_clips: Gemini returned {len(parsed)} clips")
    clips = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            log.warning(f"select_clips: item {idx} is not a dict, skipping")
            continue
        start_str = item.get("start", "")
        length = item.get("length", CLIP_DURATION)
        try:
            length = int(length)
        except (ValueError, TypeError):
            log.warning(f"select_clips: clip {idx} has invalid length '{length}', defaulting to {CLIP_DURATION}")
            length = CLIP_DURATION

        length = max(MIN_CLIP_DURATION, min(MAX_CLIP_DURATION, length))

        start_seconds = _parse_timestamp(start_str)
        if start_seconds is None:
            log.warning(f"select_clips: clip {idx} has unparseable start '{start_str}', skipping")
            continue

        end_seconds = start_seconds + length
        if end_seconds > video_duration:
            end_seconds = video_duration
            if end_seconds - start_seconds < MIN_CLIP_DURATION:
                start_seconds = max(0, end_seconds - MIN_CLIP_DURATION)

        hook_start = _parse_timestamp(item.get("hook_start", ""))
        hook_end = _parse_timestamp(item.get("hook_end", ""))
        hook_text = item.get("hook_text", "")

        if hook_start is not None and hook_end is not None:
            if hook_start < start_seconds:
                log.warning(f"select_clips: clip {idx} hook_start before clip start, adjusting")
                hook_start = start_seconds + (end_seconds - start_seconds) // 2
            if hook_start < start_seconds + 5:
                log.warning(f"select_clips: clip {idx} hook is in first 5s of clip, will cause repetition")
                midpoint = start_seconds + (end_seconds - start_seconds) // 2
                hook_start = midpoint
                hook_end = midpoint + 5
            if hook_end > end_seconds:
                hook_end = end_seconds
            if hook_end < hook_start:
                hook_end = hook_start + 3
            if hook_end - hook_start > 15:
                hook_end = hook_start + 10

            hook_start, hook_end = _snap_to_segments(segments, hook_start, hook_end, start_seconds, end_seconds)
        else:
            midpoint = start_seconds + (end_seconds - start_seconds) // 2
            hook_start = midpoint
            hook_end = midpoint + 5
            hook_start, hook_end = _snap_to_segments(segments, hook_start, hook_end, start_seconds, end_seconds)
            log.warning(f"select_clips: clip {idx} missing hook timestamps, using midpoint {midpoint:.0f}s")

        clips.append({
            "start": start_seconds,
            "end": end_seconds,
            "hook_start": hook_start,
            "hook_end": hook_end,
            "hook_text": hook_text,
            "caption": item.get("caption", ""),
            "reason": item.get("reason", ""),
        })
        log.info(f"select_clips: clip {idx+1} = {start_seconds:.0f}s-{end_seconds:.0f}s "
                 f"hook@{hook_start:.0f}s-{hook_end:.0f}s hook='{hook_text}' reason='{item.get('reason','')}'")

    log.info(f"select_clips: returning {len(clips)} valid clips")
    return clips


def _snap_to_segments(
    segments: list[SubtitleSegment],
    hook_start: float,
    hook_end: float,
    clip_start: float,
    clip_end: float,
) -> tuple[float, float]:
    clip_segs = [s for s in segments if s.start >= clip_start - 0.5 and s.end <= clip_end + 0.5]

    if not clip_segs:
        return hook_start, hook_end

    start_seg = None
    for seg in clip_segs:
        if seg.start <= hook_start <= seg.end:
            start_seg = seg
            break

    if start_seg is None:
        best_dist = float("inf")
        for seg in clip_segs:
            if seg.start >= hook_start:
                dist = seg.start - hook_start
                if dist < best_dist:
                    best_dist = dist
                    start_seg = seg
        if start_seg is None:
            best_dist = float("inf")
            for seg in reversed(clip_segs):
                if seg.end <= hook_start:
                    dist = hook_start - seg.end
                    if dist < best_dist:
                        best_dist = dist
                        start_seg = seg

    end_seg = None
    for seg in clip_segs:
        if seg.start <= hook_end <= seg.end:
            end_seg = seg
            break

    if end_seg is None:
        best_dist = float("inf")
        for seg in reversed(clip_segs):
            if seg.end <= hook_end:
                dist = hook_end - seg.end
                if dist < best_dist:
                    best_dist = dist
                    end_seg = seg
        if end_seg is None:
            best_dist = float("inf")
            for seg in clip_segs:
                if seg.start >= hook_end:
                    dist = seg.start - hook_end
                    if dist < best_dist:
                        best_dist = dist
                        end_seg = seg

    if start_seg:
        old_start = hook_start
        hook_start = start_seg.start
        if abs(old_start - hook_start) > 0.5:
            log.debug(f"_snap_to_segments: hook_start snapped {old_start:.1f}s -> {hook_start:.1f}s "
                      f"(seg text: {start_seg.text[:50]})")
    if end_seg:
        old_end = hook_end
        hook_end = end_seg.end
        if abs(old_end - hook_end) > 0.5:
            log.debug(f"_snap_to_segments: hook_end snapped {old_end:.1f}s -> {hook_end:.1f}s")

    if hook_end <= hook_start:
        if start_seg:
            hook_end = start_seg.end
        else:
            hook_end = hook_start + 3

    if hook_end - hook_start > 15:
        hook_end = hook_start + 10

    if hook_start < clip_start:
        hook_start = clip_start
    if hook_end > clip_end:
        hook_end = clip_end

    return hook_start, hook_end


def _segments_to_srt_text(segments: list[SubtitleSegment]) -> str:
    from srt_ops import seconds_to_srt_time

    lines = []
    for i, seg in enumerate(segments, 1):
        start_ts = seconds_to_srt_time(seg.start)
        end_ts = seconds_to_srt_time(seg.end)
        lines.append(str(i))
        lines.append(f"{start_ts} --> {end_ts}")
        lines.append(f"[{seg.speaker}] {seg.text}")
        lines.append("")
    return "\n".join(lines)


def _parse_timestamp(ts: str) -> float | None:
    ts = str(ts).strip()
    if re.match(r"^\d+$", ts):
        return float(ts)
    match = re.match(r"(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?", ts)
    if match:
        h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
        ms = int(match.group(4)) if match.group(4) else 0
        if len(match.group(4) or "") == 1:
            ms *= 100
        elif len(match.group(4) or "") == 2:
            ms *= 10
        return h * 3600 + m * 60 + s + ms / 1000.0
    match = re.match(r"(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?", ts)
    if match:
        m, s = int(match.group(1)), int(match.group(2))
        ms = int(match.group(3)) if match.group(3) else 0
        return m * 60 + s + ms / 1000.0
    return None
