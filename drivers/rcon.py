"""Generic Source-RCON driver — works for CS2, CS:S, GMod, MTA, Rust (RCON-ish)
and every other engine speaking the Source RCON TCP protocol.

Commands to send live in config so no code is needed per game:

    "drivers": {
      "active": "rcon",
      "rcon": {
        "host": "1.2.3.4", "port": 27015, "password": "secret",
        "query_cmd":     "users",
        "mute_cmd":      "sm_silence {target} {minutes}",
        "chat_mute_cmd": "sm_smute {target} {minutes}",
        "unmute_cmd":    "sm_usmute {target}; sm_silence {target} 0"
      }
    }

Placeholders: {target} {duration} {minutes} {seconds} {nick} {id} {reason}
  {duration} — «30m»/«1h30m»/«7d», как ждут игровые плагины сроков;
  {nick}/{id} — участник Discord, если он привязан (`/gamelink`).
Minecraft Java: `tempmute {target} {duration} {reason}` / `unmute {target}`
(EssentialsX); варн доставляется в игру командой `notify_cmd` («tell …»).
`query_cmd` output is scanned for the Discord-linked id (see core/links.py) so a
moderator can type only a Discord name; if it fails, pass `game_id` manually.
"""

from __future__ import annotations

import asyncio
import struct
from datetime import timedelta

from .base import BaseDriver, DriverError, registry

SOURCE2 = 2  # MULTI_PACKET_IGNORE / secure
REQ_AUTH = 3
REQ_EXEC = 2
RESP_AUTH = 2
RESP_RESPONSE = 0


def _packet(msg_id: int, payload_type: int, body: str, extra: str = "") -> bytes:
    raw = struct.pack("<ii", msg_id, payload_type) + body.encode() + b"\x00" + extra.encode() + b"\x00"
    return struct.pack("<i", len(raw)) + raw


def _parse(data: bytes) -> tuple[int, int, str]:
    length, msg_id, payload_type = struct.unpack_from("<iii", data, 0)
    body = data[12 : 8 + length].split(b"\x00")[0].decode(errors="replace")
    return msg_id, payload_type, body


class RconConnection:
    """Minimal asyncio Source-RCON client (no third-party dependency).

    Idle connections get dropped a lot — a game server times them out, and a bot
    sleeping on a free tier comes back to a socket the peer already closed. So a
    half-open socket is detected on read (`IncompleteReadError`) and the command
    is retried once over a freshly authenticated connection.
    """

    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._id = 0

    async def connect(self) -> None:
        if self._writer and not self._writer.is_closing():
            return
        await self.drop()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        self._id += 1
        self._writer.write(_packet(self._id, REQ_AUTH, self.password))
        await self._writer.drain()
        # Разные реализации жмутся по-разному: Source шлёт ДВА пакета
        # (AUTH_RESPONSE + пустой RESPONSE), Minecraft Java — ОДИН с id запроса.
        # Ждать второй без таймаута означало бы вечное зависание на Minecraft,
        # поэтому читаем до двух ответов, каждый под таймаутом.
        seen: list[tuple[int, int]] = []
        for _ in range(2):
            try:
                msg_id, payload_type, _ = await asyncio.wait_for(self._read_response(), self.timeout)
            except asyncio.TimeoutError:
                break
            except (asyncio.IncompleteReadError, ConnectionError):
                await self.drop()
                raise DriverError("RCON: сервер закрыл соединение при авторизации") from None
            seen.append((msg_id, payload_type))
            if payload_type != RESP_AUTH or len(seen) == 2:
                break
        if not seen:
            await self.drop()
            raise DriverError("RCON: сервер не ответил на авторизацию")
        if any(msg_id == -1 for msg_id, _ in seen):
            await self.drop()
            raise DriverError("RCON: неверный пароль")
        if not any(msg_id == self._id for msg_id, _ in seen):
            await self.drop()
            raise DriverError("RCON: странный ответ на авторизацию (id не совпал)")

    async def drop(self) -> None:
        """Forget the socket; the next connect() authenticates again."""
        if self._writer is not None:
            self._writer.close()
        self._reader, self._writer = None, None

    async def _read_response(self) -> tuple[int, int, str]:
        assert self._reader is not None
        head = await self._reader.readexactly(4)
        (length,) = struct.unpack("<i", head)
        body = await self._reader.readexactly(length)
        return _parse(head + body)

    async def _execute(self, line: str) -> str:
        assert self._writer is not None
        self._id += 1
        self._writer.write(_packet(self._id, REQ_EXEC, line))
        await self._writer.drain()
        chunks: list[str] = []
        try:
            while True:
                msg_id, payload_type, payload = await asyncio.wait_for(
                    self._read_response(), self.timeout
                )
                if msg_id != self._id:
                    continue
                chunks.append(payload)
                if payload_type != SOURCE2:
                    break
        except asyncio.TimeoutError:
            pass  # plenty of servers answer nothing for a successful command
        return "\n".join(chunks).strip()

    async def command(self, line: str) -> str:
        async with self._lock:
            try:
                await self.connect()
                return await self._execute(line)
            except (asyncio.IncompleteReadError, ConnectionError) as exc:
                # peer closed the idle socket: reconnect once, then surface it
                await self.drop()
                try:
                    await self.connect()
                    return await self._execute(line)
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                    raise DriverError(
                        f"RCON: соединение с {self.host}:{self.port} разорвано ({exc.__class__.__name__})"
                    ) from None
            except (OSError, asyncio.TimeoutError) as exc:
                await self.drop()
                raise DriverError(f"RCON: сервер недоступен ({exc.__class__.__name__})") from None

    async def close(self) -> None:
        await self.drop()


CONNECTIONS: dict[tuple[str, int, str], RconConnection] = {}


@registry.register
class RconDriver(BaseDriver):
    id = "rcon"
    title = "Источник-RCON (CS2 / GMod / Rust / MTA — что угодно по Source RCON)"
    supports_voice = True
    by_nick_only = True  # игра оперирует никами: Discord-участник не обязателен

    def __init__(self, db, config):
        super().__init__(db, config)
        self.host = str(config.get("host") or "")
        self.port = int(config.get("port") or 27015)
        self.password = str(config.get("password") or "")
        self.mute_cmd = str(config.get("mute_cmd") or "")
        self.chat_mute_cmd = str(config.get("chat_mute_cmd") or "")
        self.unmute_cmd = str(config.get("unmute_cmd") or "")
        self.query_cmd = str(config.get("query_cmd") or "")
        #: уведомление игроку в чат игры: vanilla «tell {target} …», или плагин-аналог
        self.notify_cmd = str(config.get("notify_cmd") or "tell {target} {text}")
        #: баны игры: Minecraft/EssentialsX «tempban {target} {duration} {reason}»
        #: и «pardon {target}»; пустой ban_cmd = бан через этот драйвер недоступен
        self.ban_cmd = str(config.get("ban_cmd") or "")
        #: отдельные шаблоны на бессрочные наказания: `ban {target} {reason}` вместо tempban
        self.mute_perm_cmd = str(config.get("mute_perm_cmd") or "")
        self.ban_perm_cmd = str(config.get("ban_perm_cmd") or "")
        self.unban_cmd = str(config.get("unban_cmd") or "")

    @property
    def ok(self) -> bool:
        return bool(self.host and self.password and self.mute_cmd)

    @property
    def can_ban(self) -> bool:
        return bool(self.host and self.password and self.ban_cmd)

    def _conn(self) -> RconConnection:
        key = (self.host, self.port, self.password)
        conn = CONNECTIONS.get(key)
        if conn is None:
            conn = CONNECTIONS[key] = RconConnection(
                self.host, self.port, self.password, float(self.config.get("timeout") or 5.0)
            )
        return conn

    async def teardown(self) -> None:
        for conn in list(CONNECTIONS.values()):
            await conn.close()
        CONNECTIONS.clear()

    async def resolve(self, guild, member, identifier):
        if identifier:
            return identifier
        # Without a game-side identity map we cannot guess the SteamID safely.
        raise DriverError(
            "не знаю SteamID/UID игрока — укажите `game_id` в команде "
            "(или включите сопоставление в core/links.py)"
        )

    async def _discord_member(self, guild, target: str):
        """Участник Discord по игровой привязке (или None, если её нет)."""
        uid = await self.db.find_discord_by_game(guild.id, self.id, target)
        return guild.get_member(uid) if uid else None

    async def _send(self, template: str, target: str, seconds: int, reason: str, guild=None) -> str:
        from core import timeutil  # локально: драйвер может импортироваться раньше ядра

        duration = timeutil.compact_english(timedelta(seconds=seconds)) if seconds else ""
        member = await self._discord_member(guild, target)
        rendered = template.format(
            target=target,
            minutes=max(1, round(seconds / 60)) if seconds else 0,
            seconds=seconds,
            # {duration} = «30m», «1h30m», «7d» — формат игровых плагинов сроков
            duration=duration or "0",
            # {nick}/{id} — сторона Discord, когда плагин тыкает игрока по-своему
            nick=getattr(member, "display_name", None) or target,
            id=getattr(member, "id", None) or "",
            reason=reason.replace('"', "'"),
        )
        out = ""
        for line in rendered.split(";"):
            line = line.strip()
            if line:
                out += await self._conn().command(line) + "\n"
        return out.strip()

    async def consult(self, command: str) -> str:
        """Выполнить read-only команду и вернуть вывод (для gamefeed-опроса)."""
        if not self.ok:
            raise DriverError("RCON не настроен — опрос недоступен")
        return await self._conn().command(command)

    async def ban(self, guild, target: str, seconds: int, reason: str) -> str:
        if not self.can_ban:
            raise DriverError("RCON: не задан `ban_cmd` — бан в игру не отправить")
        reply = await self._send(self._timed(self.ban_cmd, self.ban_perm_cmd, seconds),
                                  target, seconds, reason, guild)
        return reply[:900] or "забанен в игре"

    async def unban(self, guild, target: str) -> str:
        if not self.can_ban or not self.unban_cmd:
            raise DriverError("RCON: не задан `unban_cmd` — разбаньте игрока в игре вручную")
        reply = await self._send(self.unban_cmd, target, 0, "unban", guild)
        return reply[:900] or "разбанлен в игре"

    async def notify(self, guild, target: str, text: str) -> str | None:
        if not self.ok or not self.notify_cmd:
            return None
        rendered = self.notify_cmd.replace("{target}", str(target)).replace(
            "{text}", " ".join(str(text).split())[:200]
        )
        return await self._send(rendered, str(target), 0, text, guild) or "доставлено в игру"

    def _timed(self, template: str, forever_template: str, seconds: int) -> str:
        """Бессрочное наказание идёт своим шаблоном, если он задан.

        Иначе `{duration}` = «0», и плагин волен понять это как навсегда или
        отвергнуть команду — закладываться на это нельзя.
        """
        return (forever_template or template) if not seconds else template

    async def mute(self, guild, target: str, seconds: int, reason: str) -> str:
        if not self.ok:
            raise DriverError("RCON не настроен (нужны host/port/password/mute_cmd в config)")
        notes = [await self._send(self._timed(self.mute_cmd, self.mute_perm_cmd, seconds),
                                  target, seconds, reason, guild)]
        if self.chat_mute_cmd:
            notes.append(await self._send(self.chat_mute_cmd, target, seconds, reason, guild))
        reply = " / ".join(n for n in notes if n)
        return reply[:900] or "ok"

    async def unmute(self, guild, target: str) -> str:
        if not self.ok or not self.unmute_cmd:
            raise DriverError("RCON не настроен (нужен unmute_cmd в config)")
        reply = await self._send(self.unmute_cmd, target, 0, "unmute", guild)
        return reply[:900] or "ok"
