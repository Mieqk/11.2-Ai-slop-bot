"""Health endpoint for free/ephemeral hosts.

A bot has no HTTP port, so hosts that sleep containers on inactivity (Render
free tier and friends) freeze it mid-conversation: the WebSocket dies, and
everything the bot was about to do simply never happens. This serves a tiny
JSON status on `$PORT` so an external pinger keeps the process awake, and the
same URL doubles as "is my bot alive and how many guilds does it see".

Only started when it makes sense: `keepalive: true`, or `auto` + `$PORT` set by
the host. It never stores or exposes anything but bot identity and counters.
"""

from __future__ import annotations

import logging
import os
import time

from aiohttp import web

log = logging.getLogger("modbot.keepalive")

STARTED = time.monotonic()


def _digits(value: object) -> str | None:
    """Хостинги отдают PORT строкой; мусор превращать в исключение не надо."""
    text = str(value or "").strip()
    return text if text.isdigit() and 0 < int(text) < 65536 else None


def enabled(cfg) -> str | bool:
    """Resolve the keepalive flag into the port to bind, or False."""
    mode = cfg.get("keepalive", "auto")
    port = _digits(os.getenv("PORT")) or _digits(os.getenv("WEB_PORT"))
    truthy = mode is True or (isinstance(mode, str) and mode.lower() in {"on", "true", "auto", "yes"})
    if not truthy:
        return False
    if mode == "auto":
        return port or False          # автомат — только если хост сам дал $PORT
    return port or str(cfg.get("keepalive_port") or 8080)


def build_app(bot) -> web.Application:
    """The status app, split out so tests can run it on an ephemeral port."""

    async def health(_: web.Request) -> web.Response:
        guilds = len(bot.guilds)
        users = sum(g.member_count or 0 for g in bot.guilds)
        pending = len(await bot.db.active_cases()) if bot.db else 0
        payload = {
            "status": "ok" if bot.is_ready() else "starting",
            "bot": str(bot.user) if bot.user else None,
            "gateway_latency_ms": round(bot.latency * 1000, 1) if bot.is_ready() else None,
            "guilds": guilds,
            "members": users,
            "active_punishments": pending,
            # false = Discord не принял загрузку slash-команд (бот жив, но команд в
            # списке сервера нет — см. строки ERROR в логе и SETUP.md §2.3)
            "slash_commands_synced": getattr(bot, "commands_synced", None),
            "uptime_s": round(time.monotonic() - STARTED),
        }
        return web.json_response(payload, status=200 if bot.is_ready() else 503)

    async def root(_: web.Request) -> web.Response:
        return web.Response(text=f"{bot.user or 'modbot'} — живой.\n", content_type="text/plain")

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/healthz", health)
    app.router.add_get("/health", health)
    return app


class Server:
    """Handle to the running health server (aiohttp's runner has __slots__)."""

    def __init__(self, runner: web.AppRunner, port: int):
        self.runner, self.port = runner, port

    async def cleanup(self) -> None:
        await self.runner.cleanup()


async def start(bot, cfg) -> Server | None:
    port = enabled(cfg)
    if not port:
        return None
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    log.info("health-сервер на :%s (/, /healthz) — пингуйте его, чтобы хост не усыплял бота", port)
    return Server(runner, int(port))
