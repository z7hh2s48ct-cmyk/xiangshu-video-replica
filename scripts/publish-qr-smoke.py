"""Read-only platform QR probe: no login, credentials, QR bytes, or user data printed."""

import asyncio
from contextlib import aclosing

from app.publish_browser import Platform
from app.publish_browser_engine import login_events


async def probe(platform: Platform) -> bool:
    seen = set()
    try:
        async with asyncio.timeout(60):
            async with aclosing(login_events(platform)) as events:
                async for event in events:
                    if event.phase not in seen:
                        print(platform, event.phase, flush=True)
                        seen.add(event.phase)
                    if event.phase == "qr_ready":
                        print(
                            platform,
                            "qr_data_chars",
                            len(event.image or ""),
                            flush=True,
                        )
                        return True
    except Exception as error:  # noqa: BLE001 - redact short-lived login URLs in browser errors
        print(platform, type(error).__name__, flush=True)
    return False


async def main() -> int:
    platforms: tuple[Platform, ...] = ("douyin", "wechat_channels", "xiaohongshu")
    results = [await probe(platform) for platform in platforms]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
