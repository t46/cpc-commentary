"""Zoom window screenshot capture for macOS using Quartz API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("/tmp/cpc_zoom")


def _find_zoom_window_id() -> int | None:
    """Find the largest Zoom window using CGWindowListCopyWindowInfo."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )

        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        best_id = None
        best_area = 0

        for w in windows:
            if w.get("kCGWindowOwnerName") != "zoom.us":
                continue
            layer = w.get("kCGWindowLayer", 0)
            if layer != 0:
                continue
            bounds = w.get("kCGWindowBounds", {})
            width = bounds.get("Width", 0)
            height = bounds.get("Height", 0)
            area = width * height
            if area > best_area:
                best_area = area
                best_id = w["kCGWindowNumber"]

        if best_id:
            logger.debug("Found Zoom window: id=%d, area=%d", best_id, best_area)
        else:
            logger.debug("No Zoom window found")
        return best_id
    except Exception:
        logger.debug("Failed to find Zoom window")
        return None


def capture_zoom_screenshot() -> Path | None:
    """Capture a screenshot of the Zoom window using Quartz. Returns the file path or None."""
    window_id = _find_zoom_window_id()
    if window_id is None:
        return None

    try:
        from Quartz import (
            CGWindowListCreateImage,
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            kCGWindowImageBoundsIgnoreFraming,
        )
        import CoreFoundation
        import objc

        image = CGWindowListCreateImage(
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            window_id,
            kCGWindowImageBoundsIgnoreFraming,
        )
        if image is None:
            logger.warning("Failed to capture Zoom window image")
            return None

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H%M%S")
        filepath = SCREENSHOT_DIR / f"screenshot_{timestamp}.png"

        from Quartz import (
            CGImageDestinationCreateWithURL,
            CGImageDestinationAddImage,
            CGImageDestinationFinalize,
        )

        url = CoreFoundation.CFURLCreateWithFileSystemPath(
            None, str(filepath), 0, False  # kCFURLPOSIXPathStyle = 0
        )
        dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if dest is None:
            logger.warning("Failed to create image destination")
            return None

        CGImageDestinationAddImage(dest, image, None)
        CGImageDestinationFinalize(dest)

        if filepath.exists() and filepath.stat().st_size > 0:
            logger.info("Captured Zoom screenshot: %s", filepath.name)
            return filepath
        else:
            logger.warning("Screenshot file empty or missing")
            return None
    except Exception:
        logger.exception("Failed to capture screenshot")
        return None


def get_pending_screenshots(since: datetime | None = None) -> list[Path]:
    """Get all screenshots taken since the given timestamp."""
    if not SCREENSHOT_DIR.exists():
        return []
    screenshots = sorted(SCREENSHOT_DIR.glob("screenshot_*.png"))
    if since is None:
        return screenshots
    return [
        p for p in screenshots
        if datetime.fromtimestamp(p.stat().st_mtime) > since
    ]


def cleanup_screenshots(paths: list[Path]) -> None:
    """Delete used screenshots."""
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


async def periodic_screenshot_capture(
    interval_seconds: int = 30,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodically capture Zoom screenshots."""
    logger.info("Starting periodic screenshot capture (interval=%ds)", interval_seconds)
    while True:
        if stop_event and stop_event.is_set():
            break
        capture_zoom_screenshot()
        await asyncio.sleep(interval_seconds)
