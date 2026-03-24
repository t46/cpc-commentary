"""Session management — simplified from cpc-mwm-cwm/packages/cpc-mwm/src/cpc_mwm/session.py"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TranscriptChunk:
    timestamp: datetime
    text: str
    source: str  # "audio" or "vtt"


@dataclass
class Message:
    user: str
    text: str
    ts: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    is_bot: bool = False


@dataclass
class Session:
    name: str
    channel_id: str
    slide_texts: list[str] = field(default_factory=list)
    transcript_chunks: list[TranscriptChunk] = field(default_factory=list)
    channel_messages: list[Message] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    last_comment_at: datetime | None = None
    _last_context_hash: str = field(default="", repr=False)


class SessionManager:
    """Manages presentation session state."""

    def __init__(self) -> None:
        self.current_session: Session | None = None

    def start_session(self, name: str, channel_id: str) -> Session:
        if self.current_session:
            logger.info("Ending previous session: %s", self.current_session.name)
        session = Session(name=name, channel_id=channel_id)
        self.current_session = session
        logger.info("Started session: %s (channel: %s)", name, channel_id)
        return session

    def end_session(self) -> None:
        if self.current_session:
            logger.info("Ended session: %s", self.current_session.name)
            self.current_session = None

    def add_transcript(self, text: str, source: str = "audio") -> None:
        if not self.current_session or not text.strip():
            return
        chunk = TranscriptChunk(
            timestamp=datetime.now(),
            text=text.strip(),
            source=source,
        )
        self.current_session.transcript_chunks.append(chunk)
        logger.debug("Added transcript chunk (%s): %s", source, text[:50])

    def add_slides(self, slide_texts: list[str]) -> None:
        if not self.current_session:
            return
        self.current_session.slide_texts = slide_texts
        logger.info("Added %d slides to session", len(slide_texts))

    def add_channel_message(self, msg: Message) -> None:
        if not self.current_session:
            return
        self.current_session.channel_messages.append(msg)
        self.current_session.channel_messages = self.current_session.channel_messages[-50:]

    def has_enough_new_context(self) -> bool:
        session = self.current_session
        if not session:
            return False

        if not session.transcript_chunks and not session.slide_texts:
            return False

        current_hash = self._compute_context_hash(session)
        if current_hash == session._last_context_hash:
            return False

        if session.last_comment_at is None:
            return len(session.transcript_chunks) >= 3 or len(session.slide_texts) > 0

        new_chunks = [
            c for c in session.transcript_chunks
            if c.timestamp > session.last_comment_at
        ]
        return len(new_chunks) >= 3

    def build_observation(self) -> str:
        session = self.current_session
        if not session:
            return ""

        parts: list[str] = []

        if session.slide_texts:
            parts.append(f"## 発表スライド（{len(session.slide_texts)}ページ）")
            for i, text in enumerate(session.slide_texts, 1):
                if text.strip():
                    parts.append(f"### スライド {i}")
                    parts.append(text.strip())
            parts.append("")

        if session.transcript_chunks:
            recent = session.transcript_chunks[-30:]
            parts.append(f"## トランスクリプト（最新{len(recent)}件）")
            for chunk in recent:
                ts = chunk.timestamp.strftime("%H:%M:%S")
                parts.append(f"[{ts}] {chunk.text}")
            parts.append("")

        recent_msgs = session.channel_messages[-15:]
        if recent_msgs:
            parts.append(f"## チャンネルの会話（最新{len(recent_msgs)}件）")
            for msg in recent_msgs:
                ts = msg.timestamp.strftime("%H:%M:%S") if msg.timestamp else ""
                prefix = "[bot] " if msg.is_bot else ""
                parts.append(f"[{ts}] {prefix}{msg.user}: {msg.text}")
            parts.append("")

        context = "\n".join(parts)
        session._last_context_hash = self._compute_context_hash(session)
        return context

    def get_recent_transcript_text(self, n: int = 5) -> str:
        """Get the most recent transcript chunks as text for quoting."""
        session = self.current_session
        if not session or not session.transcript_chunks:
            return ""
        recent = session.transcript_chunks[-n:]
        return " ".join(c.text for c in recent)

    def record_comment(self) -> None:
        if self.current_session:
            self.current_session.last_comment_at = datetime.now()

    @staticmethod
    def _compute_context_hash(session: Session) -> str:
        content = (
            str(len(session.transcript_chunks))
            + str(len(session.channel_messages))
            + str(len(session.slide_texts))
        )
        return hashlib.md5(content.encode()).hexdigest()
