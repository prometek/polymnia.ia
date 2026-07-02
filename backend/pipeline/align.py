#!/usr/bin/env python3
"""
Forced alignment: real per-word timestamps of a KNOWN text over its audio.

Same technique as WhisperX (wav2vec2 CTC forced alignment), via torchaudio's
multilingual MMS_FA bundle -> no extra heavy deps, no numpy downgrade.

We already know the narration text (we sent it to the TTS), so this is pure
alignment (not transcription): map each spoken word to its start/end time.
Used to convert each `narration_cue` into an exact `startFrame`.
"""

import re
import unicodedata

_model = None
_tokenizer = None
_aligner = None


def _load():
    global _model, _tokenizer, _aligner
    if _model is None:
        import torch  # noqa: F401
        from torchaudio.pipelines import MMS_FA as bundle

        _model = bundle.get_model(with_star=False)
        _tokenizer = bundle.get_tokenizer()
        _aligner = bundle.get_aligner()
    return _model, _tokenizer, _aligner


def normalize_words(text: str) -> list[str]:
    """Lowercase, strip accents, split on non-letters -> ascii word list.

    Same normalization is used for the narration and for the cues, so they match.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    return [w for w in re.split(r"[^a-z]+", text) if w]


def word_timestamps(wav_path: str, text: str) -> list[dict]:
    """Return [{word, start, end}] (seconds) aligning `text` over `wav_path`.

    Returns [] if alignment fails (caller can fall back to a rough estimate).
    """
    try:
        import torch
        import torchaudio

        model, tokenizer, aligner = _load()
        words = normalize_words(text)
        if not words:
            return []

        waveform, sr = torchaudio.load(wav_path)
        if waveform.size(0) > 1:
            waveform = waveform.mean(dim=0, keepdim=True)  # mono
        if sr != 16000:
            waveform = torchaudio.functional.resample(waveform, sr, 16000)

        with torch.inference_mode():
            emission, _ = model(waveform)
            token_spans = aligner(emission[0], tokenizer(words))

        num_frames = emission.size(1)
        sec_per_frame = (waveform.size(1) / num_frames) / 16000

        out = []
        for word, spans in zip(words, token_spans, strict=False):
            out.append(
                {
                    "word": word,
                    "start": round(spans[0].start * sec_per_frame, 3),
                    "end": round(spans[-1].end * sec_per_frame, 3),
                }
            )
        return out
    except Exception:  # noqa: BLE001 — alignment is best-effort; fall back to no timing
        return []
