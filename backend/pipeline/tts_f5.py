#!/usr/bin/env python3
"""
Local TTS backend: F5-TTS (zero-shot clone) on Apple Silicon (MPS).

Clones the voice from the `ref_audio` sample and reads `gen_text` with it.
FR model: RASPIAUDIO/F5-French-MixedSpeakers-reduced (F5TTS_Base architecture).

LICENSE WARNING: the F5-TTS code is MIT, but the FR WEIGHTS are CC BY-NC 4.0
(non-commercial). OK for POC/testing, NOT for commercial production.

Same WAV bytes output as utils.synthesize -> interchangeable at step 5.
"""

import contextlib
import os
import sys
import tempfile
from typing import Any

# Config (overridable via env)
F5_MODEL = os.getenv("F5_MODEL", "F5TTS_Base")
F5_CKPT = os.getenv(
    "F5_CKPT", "hf://RASPIAUDIO/F5-French-MixedSpeakers-reduced/model_last_reduced.pt"
)
F5_VOCAB = os.getenv("F5_VOCAB", "hf://RASPIAUDIO/F5-French-MixedSpeakers-reduced/vocab.txt")
F5_REF_AUDIO = os.getenv("F5_REF_AUDIO", "inputs/voice_sample.wav")
F5_REF_TEXT = os.getenv("F5_REF_TEXT", "")  # "" => auto-transcribe the ref (ASR)

_model: Any = None


def _resolve(path: str) -> str:
    """Resolve a 'hf://owner/repo/file' path to a local file (HF cache)."""
    if not path.startswith("hf://"):
        return path
    from huggingface_hub import hf_hub_download

    rest = path[len("hf://") :]
    owner, repo, *sub = rest.split("/")
    return hf_hub_download(repo_id=f"{owner}/{repo}", filename="/".join(sub))


def _get_model() -> Any:
    """Load the model once (slow). MPS if available, otherwise CPU."""
    global _model
    if _model is None:
        import torch
        from f5_tts.api import F5TTS

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        # F5/vocos print to stdout -> redirect to stderr to keep stdout clean
        # (step 5 writes the chainable JSON there).
        with contextlib.redirect_stdout(sys.stderr):
            _model = F5TTS(
                model=F5_MODEL,
                ckpt_file=_resolve(F5_CKPT),
                vocab_file=_resolve(F5_VOCAB),
                device=device,
            )
    return _model


def synthesize_f5(text: str, ref_audio: str | None = None, ref_text: str | None = None) -> bytes:
    """Synthesize `text` with the cloned voice from the ref. Returns WAV bytes."""
    model = _get_model()
    ref_audio = ref_audio or F5_REF_AUDIO
    ref_text = F5_REF_TEXT if ref_text is None else ref_text

    if not os.path.exists(ref_audio):
        raise FileNotFoundError(f"Voice sample not found: {ref_audio}")

    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            model.infer(
                ref_file=ref_audio,
                ref_text=ref_text,  # "" -> F5 auto-transcribes the ref
                gen_text=text,
                file_wave=out,
                remove_silence=True,
            )
        with open(out, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(out):
            os.remove(out)
