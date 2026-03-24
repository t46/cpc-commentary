"""Real-time audio capture and transcription — ported from cpc-mwm-cwm/packages/cpc-mwm/src/cpc_mwm/audio_capture.py"""

from __future__ import annotations

import asyncio
import logging
import queue
from collections.abc import Awaitable, Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioTranscriber:
    """Captures audio via sounddevice and transcribes with faster-whisper."""

    def __init__(
        self,
        audio_device: str | None = None,
        whisper_model: str = "large-v3",
        whisper_language: str = "ja",
    ) -> None:
        self.audio_device = audio_device
        self.whisper_model = whisper_model
        self.whisper_language = whisper_language
        self.sample_rate = 16000
        self.buffer_seconds = 5
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._running = False
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "Loading Whisper model: %s (this may take a moment...)",
                self.whisper_model,
            )
            self._model = WhisperModel(
                self.whisper_model,
                device="auto",
                compute_type="auto",
            )
            logger.info("Whisper model loaded successfully")
        return self._model

    def _get_device_id(self) -> int | None:
        if not self.audio_device:
            return None
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if self.audio_device.lower() in dev["name"].lower():
                logger.info("Using audio device: %s (id=%d)", dev["name"], i)
                return i
        logger.warning(
            "Audio device '%s' not found. Available devices:", self.audio_device
        )
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                logger.warning("  [%d] %s", i, dev["name"])
        return None

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.warning("Audio status: %s", status)
        self._audio_queue.put(indata.copy())

    async def start(self, on_transcript: Callable[[str], Awaitable[None]]) -> None:
        self._running = True
        device_id = self._get_device_id()
        model = await asyncio.to_thread(self._get_model)

        logger.info(
            "Starting audio capture (device=%s, rate=%d, buffer=%ds)",
            device_id or "default",
            self.sample_rate,
            self.buffer_seconds,
        )

        stream = sd.InputStream(
            device=device_id,
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=int(self.sample_rate * 0.5),
            callback=self._audio_callback,
        )

        buffer = np.array([], dtype=np.float32)
        target_samples = self.sample_rate * self.buffer_seconds

        with stream:
            while self._running:
                try:
                    chunk = await asyncio.to_thread(self._audio_queue.get, timeout=1.0)
                    buffer = np.concatenate([buffer, chunk.flatten()])

                    if len(buffer) >= target_samples:
                        audio_segment = buffer[:target_samples]
                        buffer = buffer[target_samples:]

                        text = await asyncio.to_thread(
                            self._transcribe, model, audio_segment
                        )
                        if text:
                            logger.info("Transcribed: %s", text[:80])
                            await on_transcript(text)

                except queue.Empty:
                    continue
                except Exception:
                    logger.exception("Error in audio capture loop")
                    await asyncio.sleep(1)

    def _transcribe(self, model, audio: np.ndarray) -> str:
        segments, _info = model.transcribe(
            audio,
            language=self.whisper_language,
            beam_size=5,
            vad_filter=True,
        )
        texts = [segment.text.strip() for segment in segments if segment.text.strip()]
        return " ".join(texts)

    async def stop(self) -> None:
        self._running = False
        logger.info("Audio capture stopped")
