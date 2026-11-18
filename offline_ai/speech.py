import io
import json
import re
import wave
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np
import onnxruntime as ort
from faster_whisper import WhisperModel
from faster_whisper.feature_extractor import FeatureExtractor

from triage import ServiceError

ASSETS = Path(__file__).resolve().parent / ".runtime"
RATE = 16000
MAX_SECONDS = 30
_recognition = Lock()
_speech = Lock()
_letters = str.maketrans("abdfghijklmnopqrstuvxyz", "абдфгҳижклмнопқрстувхйз")
_digraphs = {"o'": "ў", "g'": "ғ", "sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ye": "е"}


def cyrillic(text):
    text = re.sub("[‘’ʻʼ`]", "'", text.casefold())
    text = re.sub(r"\be", "э", text)
    for latin, letter in _digraphs.items():
        text = text.replace(latin, letter)
    return " ".join(re.sub("[^а-яёғўқҳ ]", " ", text.translate(_letters).replace("e", "е").replace("'", "ъ")).split())


@lru_cache(maxsize=1)
def synthesizer():
    options = ort.SessionOptions()
    options.intra_op_num_threads = 3
    session = ort.InferenceSession(str(ASSETS / "tts/model.onnx"), options, providers=["CPUExecutionProvider"])
    vocab = json.loads((ASSETS / "tts/vocab.json").read_text("utf-8"))
    return session, vocab


def render_sentence(letters):
    session, vocab = synthesizer()
    ids = np.zeros((1, 2 * len(letters) + 1), dtype=np.int64)
    ids[0, 1::2] = [vocab[c] for c in letters]
    with _speech:
        samples, = session.run(["waveform"], {"input_ids": ids, "attention_mask": np.ones_like(ids)})
    return np.concatenate((samples.flatten(), np.zeros(RATE // 6, dtype=np.float32)))


# Only server-authored nurse instructions and measurement phrases enter this cache.
cached_instruction = lru_cache(maxsize=96)(render_sentence)


def synthesize(text, *, instruction=False):
    render = cached_instruction if instruction else render_sentence
    sentences = (cyrillic(s) for s in re.split(r"[.!?]+", text))
    parts = [render(sentence) for sentence in sentences if sentence]
    if not parts:
        raise ValueError("Ovozga aylantiriladigan so‘z topilmadi.")
    return np.concatenate(parts)


def wav_bytes(samples):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", ""))
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()


@lru_cache(maxsize=1)
def recognizer():
    model = WhisperModel(str(ASSETS / "stt"), device="cpu", compute_type="int8",
                         cpu_threads=4, local_files_only=True)
    # The published Uzbek checkpoint ships a 128-bin preprocessor for an 80-bin encoder.
    model.feature_extractor = FeatureExtractor(**{**model.feat_kwargs, "feature_size": model.model.n_mels})
    return model


def transcribe(data):
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE)
                    or not RATE // 3 <= wav.getnframes() <= RATE * MAX_SECONDS):
                raise ValueError
            frames = wav.readframes(wav.getnframes())
            if len(frames) != wav.getnframes() * 2:
                raise ValueError
            samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    except (wave.Error, EOFError, ValueError) as exc:
        raise ServiceError(422, "16 kHz mono PCM16 WAV, 0.34–30 soniya yuboring.") from exc
    if np.sqrt(np.mean(samples ** 2)) < 0.002:
        raise ServiceError(422, "Ovoz juda past. Shikoyatni qayta ayting.")
    with _recognition:
        segments, _ = recognizer().transcribe(samples, language="uz", beam_size=3,
                                              vad_filter=True, condition_on_previous_text=False)
        segments = list(segments)
    if not segments or any(s.avg_logprob < -1.0 or s.no_speech_prob > 0.65 for s in segments):
        raise ServiceError(422, "Nutq noaniq. Shikoyatni qayta ayting.")
    text = " ".join(s.text.strip() for s in segments).strip()
    if sum(c.isalpha() for c in text) < 3:
        raise ServiceError(422, "Shikoyat eshitilmadi. Qayta ayting.")
    return text
