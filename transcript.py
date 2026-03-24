"""VTT transcript parser — ported from cpc-mwm-cwm/packages/cpc-mwm/src/cpc_mwm/transcript.py"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class VTTEntry:
    start_time: str
    end_time: str
    speaker: str | None
    text: str


def parse_vtt(content: str) -> list[VTTEntry]:
    """Parse WebVTT format content into entries."""
    entries: list[VTTEntry] = []
    content = content.strip().lstrip("\ufeff")
    if content.startswith("WEBVTT"):
        content = content[len("WEBVTT"):]
        idx = content.find("\n\n")
        if idx != -1:
            content = content[idx:]

    blocks = re.split(r"\n\s*\n", content.strip())

    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue

        line_idx = 0
        if lines[line_idx].strip().isdigit():
            line_idx += 1
            if line_idx >= len(lines):
                continue

        timestamp_pattern = r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})"
        match = re.match(timestamp_pattern, lines[line_idx].strip())
        if not match:
            continue

        start_time = match.group(1)
        end_time = match.group(2)
        line_idx += 1

        text_lines = [line.strip() for line in lines[line_idx:] if line.strip()]
        if not text_lines:
            continue

        full_text = " ".join(text_lines)

        speaker = None
        speaker_match = re.match(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", full_text)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            full_text = speaker_match.group(2).strip()
        else:
            colon_match = re.match(r"^([^:]{1,30}):\s+(.+)$", full_text)
            if colon_match:
                speaker = colon_match.group(1).strip()
                full_text = colon_match.group(2).strip()

        entries.append(VTTEntry(
            start_time=start_time,
            end_time=end_time,
            speaker=speaker,
            text=full_text,
        ))

    return entries
