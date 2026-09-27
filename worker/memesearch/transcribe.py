from __future__ import annotations

import logging
import threading
from pathlib import Path

from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model: str, compute_type: str, threads: int, download_root: Path) -> None:
        self.model_name = model
        self.compute_type = compute_type
        self.threads = threads
        self.download_root = download_root
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def _load(self) -> WhisperModel:
        if self._model is None:
            log.info("loading whisper %s", self.model_name)
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.threads,
                download_root=str(self.download_root),
            )
        return self._model

    def transcribe(self, wav: Path) -> tuple[str, str]:
        with self._lock:
            model = self._load()
            segments, info = model.transcribe(
                str(wav),
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.7)
        if info.language_probability < 0.4 or len(text) < 3:
            return "", info.language
        return text.strip(), info.language
