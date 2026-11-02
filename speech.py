# speech.py - turn a voice message into text (local Whisper: free, offline).
# It also turns spoken numbers into digits: "pool forty zero twenty" -> "pool 40 0 20".
import logging
import os

import history

# Keep the model on the persistent disk (Render erases everything else).
os.environ.setdefault("HF_HOME", str(history.DATA_DIR / "models"))

MODEL = os.getenv("STT_MODEL", "base")   # tiny / base / small
MAX_VOICE = 10_000_000                   # 10 MB
_model = None


def transcribe(path):
    """Transcribe an audio file to text. Returns "" if it cannot."""
    global _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logging.warning("faster-whisper is not installed; voice input is off")
        return ""
    try:
        if _model is None:
            _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
        segments, _info = _model.transcribe(str(path), beam_size=5)
        return " ".join(s.text for s in segments).strip()
    except Exception:
        logging.exception("could not transcribe the voice message")
        return ""


# ---------- spoken numbers ----------
_ONES = {"zero": 0, "oh": 0, "nought": 0, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SMALL = {k for k, v in _ONES.items() if 1 <= v <= 9}   # "one".."nine" (a ten merges with these)


def spoken_numbers(text):
    """Replace spoken numbers with digits. 'pool forty zero twenty' -> 'pool 40 0 20'."""
    tokens = text.lower().replace(",", " ").replace("-", " ").split()
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in _ONES or t in _TENS:
            num = _ONES[t] if t in _ONES else _TENS[t]
            i += 1
            # "one hundred" / "two hundred"
            while i < len(tokens) and tokens[i] in ("hundred", "thousand"):
                num = num * (100 if tokens[i] == "hundred" else 1000)
                i += 1
            # "twenty one" -> 21, but "fifty ten" -> 50 and 10 (ten starts a new number)
            if t in _TENS and i < len(tokens) and tokens[i] in _SMALL:
                num += _ONES[tokens[i]]
                i += 1
            out.append(str(num))
        else:
            out.append(t)
            i += 1
    return " ".join(out)
