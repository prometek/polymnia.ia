#!/usr/bin/env python3
"""
Narration-synced timing: map each `narration_cue` to a `startFrame`.

Preferred: real per-word timestamps from forced alignment (align.word_timestamps)
-> a cue's startFrame = the time its first word is actually spoken.

Fallback (no alignment): a cue's start ~ its character position in the narration as
a fraction of the audio duration. Rougher but dependency-free.

startFrame is relative to the SCENE start, so we add the lead-in offset (the voice
begins `lead_frames` after the scene starts). startFrames are forced monotonic.
"""

from typing import Any

from align import normalize_words


def _frame_by_words(
    cue: str, words: list[dict[str, Any]], cursor: int, fps: int, lead: int
) -> tuple[int | None, int]:
    """Find cue in the aligned word stream from `cursor`. Returns (frame, next_cursor)."""
    cue_tokens = normalize_words(cue)
    if not cue_tokens:
        return None, cursor
    seq = [w["word"] for w in words]
    for i in range(cursor, len(seq) - len(cue_tokens) + 1):
        if seq[i : i + len(cue_tokens)] == cue_tokens:
            return lead + round(words[i]["start"] * fps), i + len(cue_tokens)
    return None, cursor  # not found -> caller keeps previous frame


def _frame_by_position(cue: str, narration: str, audio_frames: int, lead: int) -> int:
    """Rough fallback: char position of the cue / narration length * audio frames."""
    if not cue or cue not in narration:
        return lead
    return lead + round((narration.index(cue) / max(1, len(narration))) * audio_frames)


def attach_start_frames(
    props: dict[str, Any],
    audio_duration_s: float,
    fps: int,
    lead_frames: int = 0,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inject `startFrame` on each cued step of `props` (in place, returns props).

    Uses forced-aligned `words` if provided, else the char-position fallback.
    startFrames are clamped to be monotonically increasing.
    """
    narration = props.get("narration", "") or ""
    audio_frames = round(audio_duration_s * fps)
    cursor = 0
    last = lead_frames

    # Cued elements live under one of these arrays (see layout_store.CUED_LAYOUTS).
    elements = (
        props.get("steps") or props.get("items") or props.get("stats") or props.get("nodes") or []
    )
    for item in elements:
        cue = item.get("narration_cue")
        frame = None
        if words:
            frame, cursor = _frame_by_words(cue, words, cursor, fps, lead_frames)
        if frame is None:
            frame = _frame_by_position(cue, narration, audio_frames, lead_frames)
        frame = max(frame, last)  # monotonic
        item["startFrame"] = frame
        last = frame

    return props
