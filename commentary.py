"""Presentation commentary bot — two bots comment on live presentations.

Combines:
- Audio capture & transcript from cpc-mwm-cwm
- Claude Code subprocess from cpc-slack-bot
- Zoom screenshot capture (new)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from session import Message, SessionManager
from slides import download_file_from_slack, extract_slide_texts
from transcript import parse_vtt
from screenshot import (
    get_pending_screenshots,
    cleanup_screenshots,
    periodic_screenshot_capture,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    name: str
    resource_dir: str
    icon_emoji: str = ":robot_face:"
    icon_url: str | None = None


# ---------------------------------------------------------------------------
# Claude Code subprocess (from cpc-slack-bot/bot.py)
# ---------------------------------------------------------------------------

async def run_claude(prompt: str, resource_dir: str) -> str:
    """Run claude -p as a subprocess and return the answer."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=resource_dir,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("claude exited with %d: %s", proc.returncode, stderr.decode())
        return ""

    return stdout.decode().strip()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_commentary_prompt(
    observation: str,
    bot_name: str,
    screenshot_paths: list[Path],
    recent_transcript: str,
) -> str:
    """Build the prompt for Claude Code to generate a commentary."""
    parts: list[str] = []

    parts.append(
        "あなたは研究合宿でのプレゼンテーションを聞いている研究者です。\n"
        "以下の発表の内容を踏まえて、あなたの専門知識・研究経験に基づいたコメントを1つ投稿してください。\n"
    )

    parts.append(observation)

    if screenshot_paths:
        parts.append("\n## 現在のスライド画像")
        parts.append(
            "このディレクトリ内に _screenshots/ フォルダがあります。"
            "Read ツールで中の画像ファイルを読んで、今どのスライドが表示されているか把握してください。"
            "スクショのファイル名はHHMMSS形式のタイムスタンプです。"
        )
        parts.append("")

    parts.append(
        "\n## 回答フォーマット\n"
        "以下のフォーマットで回答してください:\n"
        "1行目: 発表の中から引用したい部分（短く。なければ省略可）\n"
        "2行目: 空行\n"
        "3行目以降: あなたのコメント（2-4文。くだけた口調で。）\n"
        "\n"
        "例:\n"
        "引用: 意識の統合情報理論に基づいて考えると\n"
        "\n"
        "これ、ベルクソンの持続概念との接点が面白いところだよね。"
        "統合情報って結局、不可分な時間的全体を量的に扱おうとしてる試みに見える。\n"
    )

    if observation and "スライド" in observation:
        parts.append(
            "【重要】スライドPDFが読み込まれている場合、transcriptやスクリーンショットで"
            "既に言及・表示されたスライドの内容のみに基づいてコメントしてください。"
            "まだ発表で出てきていないスライドには絶対に言及しないでください。\n"
        )

    parts.append(
        "【スタイル】\n"
        "- Slackでの雑談です。論文調ではなく、くだけた口調で。\n"
        "- 2-4文で簡潔に。長すぎない。\n"
        "- 自分の研究経験やディレクトリ内の知識を踏まえて、独自の視点でコメントしてください。\n"
        "- 「です・ます」調は使わない。\n"
        "- メタ的な説明（「トランスクリプトから回答します」「スクリーンショットが読めませんでした」等）は絶対に書かないでください。コメント本文だけを出力すること。\n"
    )

    return "\n".join(parts)


def format_slack_message(claude_response: str) -> str:
    """Format Claude's response into Slack message with quote block."""
    lines = claude_response.strip().split("\n")

    # Try to extract quote line
    quote_line = ""
    comment_lines: list[str] = []
    found_quote = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not found_quote and stripped.startswith("引用:"):
            quote_text = stripped[len("引用:"):].strip()
            if quote_text:
                quote_line = quote_text
            found_quote = True
        elif stripped:
            comment_lines.append(stripped)

    if quote_line:
        return f"> {quote_line}\n\n" + "\n".join(comment_lines)
    else:
        return "\n".join(comment_lines) if comment_lines else claude_response.strip()


# ---------------------------------------------------------------------------
# Mention helpers (from cpc-slack-bot/bot.py)
# ---------------------------------------------------------------------------

CHANNEL_HISTORY_LIMIT = 20


def strip_mention(text: str) -> str:
    """Remove <@BOTID> mention from message text."""
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


async def fetch_conversation_context(client, channel: str, thread_ts: str, event_ts: str) -> str:
    """Fetch conversation context from thread or channel."""
    is_in_thread = thread_ts != event_ts

    if is_in_thread:
        result = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=CHANNEL_HISTORY_LIMIT
        )
        messages = result.get("messages", [])
    else:
        result = await client.conversations_history(
            channel=channel, limit=CHANNEL_HISTORY_LIMIT
        )
        messages = list(reversed(result.get("messages", [])))

    lines = []
    for msg in messages:
        user = msg.get("user", "bot")
        text = strip_mention(msg.get("text", ""))
        if text:
            lines.append(f"<@{user}>: {text}")

    return "\n".join(lines)


def build_mention_prompt(
    question: str,
    conversation_context: str,
    session_context: str,
) -> str:
    """Build prompt for responding to an @mention."""
    parts: list[str] = []

    parts.append(
        "あなたは研究合宿に参加している研究者です。\n"
        "Slackでメンションされたので、質問・依頼に応えてください。\n"
    )

    if session_context:
        parts.append("## 現在の発表の状況")
        parts.append(session_context)
        parts.append("")

    parts.append("## Slackでの会話")
    parts.append(conversation_context)
    parts.append("")

    parts.append(
        "## 回答のスタイル\n"
        "- Slackでの雑談です。論文調ではなく、くだけた口調で。\n"
        "- 簡潔に答える。長すぎない。\n"
        "- 自分の専門知識・ディレクトリ内の知識を踏まえて回答する。\n"
        "- 「です・ます」調は使わない。\n"
        "- メタ的な説明は書かない。回答本文だけを出力すること。\n"
    )

    return "\n".join(parts)


def select_bot_by_name(text: str, bots: list) -> object | None:
    """If the text contains a bot name, return that bot."""
    text_lower = text.lower()
    for bot in bots:
        if bot.name.lower() in text_lower:
            return bot
    return None


# ---------------------------------------------------------------------------
# Slack event handlers (pattern from cpc-mwm-cwm/slack_app.py)
# ---------------------------------------------------------------------------

def register_handlers(
    app: AsyncApp,
    session_mgr: SessionManager,
    channel_id: str,
    bots: list | None = None,
) -> None:
    """Register Slack event handlers."""

    bots = bots or []

    @app.event("app_mention")
    async def handle_mention(event: dict, client) -> None:
        channel = event["channel"]
        event_ts = event["ts"]
        thread_ts = event.get("thread_ts") or event_ts
        question = strip_mention(event.get("text", ""))

        if not question:
            return

        # Add thinking_face reaction
        try:
            await client.reactions_add(
                channel=channel, name="thinking_face", timestamp=event_ts
            )
        except Exception:
            pass

        # Select bot: by name in message, or random
        bot = select_bot_by_name(question, bots) if bots else None
        if not bot and bots:
            bot = random.choice(bots)

        # Fetch Slack conversation context
        conversation_context = await fetch_conversation_context(
            client, channel, thread_ts, event_ts
        )

        # Include session context (slides, transcript) if available
        session_context = ""
        if session_mgr.current_session:
            session_context = session_mgr.build_observation()

        # Build prompt
        prompt = build_mention_prompt(
            question=question,
            conversation_context=conversation_context,
            session_context=session_context,
        )

        logger.info(
            "Mention from channel=%s, bot=%s, question=%s",
            channel, bot.name if bot else "none", question[:80],
        )

        # Run Claude
        resource_dir = bot.resource_dir if bot else "."
        answer = await run_claude(prompt, resource_dir)

        if not answer:
            answer = "すみません、回答の生成中にエラーが発生しました。"

        # Post reply in thread
        post_kwargs = dict(
            channel=channel,
            text=answer,
            thread_ts=thread_ts,
        )
        if bot:
            post_kwargs["username"] = bot.name
            post_kwargs["icon_emoji"] = bot.icon_emoji

        await client.chat_postMessage(**post_kwargs)

        # Remove thinking_face reaction
        try:
            await client.reactions_remove(
                channel=channel, name="thinking_face", timestamp=event_ts
            )
        except Exception:
            pass

    @app.event("message")
    async def handle_message(event: dict, client) -> None:
        ch = event.get("channel", "")
        text = event.get("text", "")
        user = event.get("user", "unknown")
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")
        ts = event.get("ts", "")

        if subtype in ("message_changed", "message_deleted"):
            return

        if ch != channel_id:
            return

        # Session commands
        if text.startswith("!session start "):
            parts = text.split(maxsplit=2)
            session_name = parts[2].strip() if len(parts) > 2 else "unnamed"
            session_mgr.start_session(session_name, channel_id)
            await client.chat_postMessage(
                channel=channel_id,
                text=f"セッション「{session_name}」を開始しました。",
            )
            return

        if text.strip() == "!session end":
            session_mgr.end_session()
            await client.chat_postMessage(
                channel=channel_id,
                text="セッションを終了しました。",
            )
            return

        if text.strip() == "!session status":
            session = session_mgr.current_session
            if not session:
                await client.chat_postMessage(
                    channel=channel_id,
                    text="現在アクティブなセッションはありません。",
                )
            else:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f"*セッション: {session.name}*\n"
                        f"スライド: {len(session.slide_texts)} ページ\n"
                        f"トランスクリプト: {len(session.transcript_chunks)} チャンク\n"
                        f"チャンネルメッセージ: {len(session.channel_messages)} 件"
                    ),
                )
            return

        # File attachments (PDF, VTT)
        files = event.get("files", [])
        for file_info in files:
            filetype = file_info.get("filetype", "")
            filename = file_info.get("name", "")

            if filetype == "pdf" or filename.endswith(".pdf"):
                pdf_bytes = await download_file_from_slack(client, file_info)
                if pdf_bytes:
                    slide_texts = extract_slide_texts(pdf_bytes)
                    session_mgr.add_slides(slide_texts)
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=f"PDF「{filename}」を読み込みました（{len(slide_texts)}ページ）。",
                    )

            elif filetype == "vtt" or filename.endswith(".vtt"):
                vtt_bytes = await download_file_from_slack(client, file_info)
                if vtt_bytes:
                    vtt_content = vtt_bytes.decode("utf-8", errors="replace")
                    entries = parse_vtt(vtt_content)
                    for entry in entries:
                        text_str = f"{entry.speaker}: {entry.text}" if entry.speaker else entry.text
                        session_mgr.add_transcript(text_str, source="vtt")
                    logger.info("Processed %d VTT entries from %s", len(entries), filename)

        # Track channel messages
        msg = Message(
            user=event.get("username", user),
            text=text,
            ts=ts,
            timestamp=datetime.fromtimestamp(float(ts)) if ts else datetime.now(),
            is_bot=bool(bot_id),
        )
        session_mgr.add_channel_message(msg)


# ---------------------------------------------------------------------------
# Main commentary loop (pattern from cpc-mwm-cwm/main.py:periodic_response)
# ---------------------------------------------------------------------------

async def commentary_loop(
    app: AsyncApp,
    session_mgr: SessionManager,
    bots: list[BotConfig],
    channel_id: str,
    interval_seconds: int,
    enable_screenshot: bool,
) -> None:
    """Periodically generate and post commentary."""
    initial_delay = random.uniform(10, 30)
    logger.info(
        "Commentary loop started (interval=%ds, bots=%s, initial_delay=%.0fs)",
        interval_seconds,
        [b.name for b in bots],
        initial_delay,
    )
    await asyncio.sleep(initial_delay)

    while True:
        jitter = random.uniform(-15, 15)
        await asyncio.sleep(interval_seconds + jitter)

        if not session_mgr.current_session:
            continue

        if not session_mgr.has_enough_new_context():
            logger.debug("Not enough new context, skipping")
            continue

        # Select a random bot
        bot = random.choice(bots)
        logger.info("Selected bot: %s", bot.name)

        # Build observation
        observation = session_mgr.build_observation()

        # Gather screenshots
        screenshot_paths: list[Path] = []
        if enable_screenshot:
            last_comment = session_mgr.current_session.last_comment_at
            screenshot_paths = get_pending_screenshots(since=last_comment)

        # Get recent transcript for quoting
        recent_transcript = session_mgr.get_recent_transcript_text()

        # Build prompt
        prompt = build_commentary_prompt(
            observation=observation,
            bot_name=bot.name,
            screenshot_paths=screenshot_paths,
            recent_transcript=recent_transcript,
        )

        logger.info(
            "Generating commentary with %s (observation=%d chars, screenshots=%d)",
            bot.name, len(observation), len(screenshot_paths),
        )

        # Copy screenshots into resource_dir so claude -p can read them
        screenshots_dir = Path(bot.resource_dir) / "_screenshots"
        if screenshot_paths:
            screenshots_dir.mkdir(exist_ok=True)
            for p in screenshot_paths:
                shutil.copy2(p, screenshots_dir / p.name)
            logger.info("Copied %d screenshots to %s", len(screenshot_paths), screenshots_dir)

        try:
            # Run Claude Code
            response = await run_claude(prompt, bot.resource_dir)
        finally:
            # Always clean up copied screenshots from resource_dir
            if screenshots_dir.exists():
                shutil.rmtree(screenshots_dir, ignore_errors=True)

        if not response:
            logger.warning("Empty response from claude for %s", bot.name)
            continue

        # Format and post
        message = format_slack_message(response)
        if not message.strip():
            continue

        try:
            icon_kwargs = (
                {"icon_url": bot.icon_url}
                if bot.icon_url
                else {"icon_emoji": bot.icon_emoji}
            )
            await app.client.chat_postMessage(
                channel=channel_id,
                text=message,
                username=bot.name,
                **icon_kwargs,
            )
            session_mgr.record_comment()
            logger.info("[%s] Posted commentary: %s", bot.name, message[:80])

            # Clean up used screenshots from /tmp
            if screenshot_paths:
                cleanup_screenshots(screenshot_paths)

        except Exception:
            logger.exception("[%s] Failed to post commentary", bot.name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    channel_id = args.channel
    data_dir = Path(args.data_dir).resolve()
    bot_names = [name.strip() for name in args.bots.split(",")]
    interval = int(os.environ.get("RESPONSE_INTERVAL_SECONDS", "120"))
    enable_audio = os.environ.get("ENABLE_AUDIO", "false").lower() == "true"
    enable_screenshot = os.environ.get("ENABLE_SCREENSHOT", "false").lower() == "true"
    audio_device = os.environ.get("AUDIO_DEVICE")
    whisper_model = os.environ.get("WHISPER_MODEL", "large-v3")
    whisper_language = os.environ.get("WHISPER_LANGUAGE", "ja")
    screenshot_interval = int(os.environ.get("SCREENSHOT_INTERVAL_SECONDS", "30"))

    # Build bot configs
    default_emojis = {
        "hirai-bot": ":books:",
        "tanichu-bot": ":robot_face:",
        "maruyama-bot": ":microscope:",
        "daikoku-bot": ":brain:",
    }
    default_icon_urls = {
        "tanichu-bot": "https://ca.slack-edge.com/T0AEQGDELSG-U0AE5EV4NDV-4cf9e81069a1-512",
        "hirai-bot": "https://ca.slack-edge.com/T0AEQGDELSG-U0AL2BNC84Q-3fef70ee10ed-512",
    }
    bots: list[BotConfig] = []
    for name in bot_names:
        resource_path = data_dir / name
        if not resource_path.is_dir():
            logger.error("Bot resource directory not found: %s", resource_path)
            return
        bots.append(BotConfig(
            name=name,
            resource_dir=str(resource_path),
            icon_emoji=default_emojis.get(name, ":robot_face:"),
            icon_url=default_icon_urls.get(name),
        ))

    logger.info("Bots: %s", [(b.name, b.resource_dir) for b in bots])

    session_mgr = SessionManager()
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    register_handlers(app, session_mgr, channel_id, bots=bots)

    # Start audio capture
    if enable_audio:
        from audio_capture import AudioTranscriber

        transcriber = AudioTranscriber(
            audio_device=audio_device,
            whisper_model=whisper_model,
            whisper_language=whisper_language,
        )

        async def on_transcript(text: str) -> None:
            session_mgr.add_transcript(text, source="audio")

        asyncio.create_task(transcriber.start(on_transcript))
        logger.info("Audio capture enabled (device=%s)", audio_device or "default")

    # Start screenshot capture
    if enable_screenshot:
        asyncio.create_task(
            periodic_screenshot_capture(interval_seconds=screenshot_interval)
        )
        logger.info("Screenshot capture enabled (interval=%ds)", screenshot_interval)

    # Start commentary loop
    asyncio.create_task(
        commentary_loop(
            app=app,
            session_mgr=session_mgr,
            bots=bots,
            channel_id=channel_id,
            interval_seconds=interval,
            enable_screenshot=enable_screenshot,
        )
    )

    # Start Slack socket mode
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting commentary bot (channel=%s)", channel_id)
    await handler.start_async()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Presentation commentary bot powered by Claude Code"
    )
    parser.add_argument(
        "--bots",
        default="hirai-bot,tanichu-bot,daikoku-bot",
        help="Comma-separated bot names (default: hirai-bot,tanichu-bot,daikoku-bot)",
    )
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Path to bot data directory (default: ./data)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Slack channel ID to post commentary",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
