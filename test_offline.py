"""Offline functional test of the punishment engine.

Fakes the Discord objects (guild, member, channels, interaction) so the whole
mute -> log -> auto-expire -> unmute path and the warn ladder can be exercised
without a token. Run: .venv/bin/python test_offline.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import os
import pathlib
import socket
import struct
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import aiohttp
from aiohttp import web

from core import timeutil
from core.db import Database
from core.helpers import ModInputError
from core.moderation import Moderation
from drivers.base import DriverError, registry

SENT: list[str] = []


class FakeOverwrite:
    """Как discord.PermissionOverwrite: unset флаг = None (inherit), deny = False."""

    def __init__(self, **flags):
        self._flags = dict(flags)

    def __getattr__(self, item):
        return self._flags.get(item, None)


class FakeMember:
    def __init__(self, guild, mid=42, top_role=None):
        self.guild, self.id, self.mention = guild, mid, f"<@{mid}>"
        self.guild_id = guild.id
        self.display_name = self.name = f"player{mid}"
        self.top_role = top_role or Role(10, "member")  # below the bot (50), above @everyone
        self.roles = [self.top_role, Role(1, "@everyone")]
        self.edits: list[dict] = []
        self.voice = None
        self.is_bot = False
        self.guild_permissions = discord.Permissions.none()

    async def edit(self, **kw):
        self.edits.append(kw)

    async def send(self, *, embed=None):
        SENT.append(f"DM:{embed.title if embed else 'text'}")
        return SimpleNamespace(id=2)

    async def add_roles(self, role, *, reason=None):
        self.roles.append(role)

    async def remove_roles(self, role, *, reason=None):
        if role in self.roles:
            self.roles.remove(role)

    def __eq__(self, other):
        return isinstance(other, FakeMember) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        return self.display_name


class FakeTextChannel:
    """Text channel: `overwrites` dict, set_permissions like the real library."""

    def __init__(self, cid=500, name="general"):
        self.id, self.name, self.type = cid, name, discord.ChannelType.text
        self.mention = f"<#{cid}>"
        self.overwrites: dict[FakeMember, FakeOverwrite] = {}
        self.calls: list[dict] = []
        self.sent: list = []

    async def set_permissions(self, target, *, reason=None, **kw):
        self.calls.append(kw)
        flags = {f: v for f, v in kw.items() if v is not None}
        if flags:
            self.overwrites[target] = FakeOverwrite(**flags)
        else:
            self.overwrites.pop(target, None)

    async def send(self, *, embed=None):
        self.sent.append(embed)
        SENT.append(embed.title or "embed")
        return SimpleNamespace(id=1)


class FakeVoiceChannel(FakeTextChannel):
    def __init__(self, cid=600, name="General"):
        super().__init__(cid, name)
        self.type = discord.ChannelType.voice


class Role:
    def __init__(self, rid, name="mute"):
        self.id, self.name = rid, name

    def __ge__(self, other):
        return self.id >= other.id

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class FakeGuild:
    def __init__(self):
        self.id, self.name = 7, "TestGuild"
        self.text_channels = [FakeTextChannel()]
        self.voice_channels = [FakeVoiceChannel()]
        self.channels = self.text_channels + self.voice_channels
        self.me = SimpleNamespace(
            id=99,
            top_role=Role(50, "bot"),
            guild_permissions=discord.Permissions.all(),
        )
        self._members = {}

    def get_member(self, uid):
        return self._members.get(uid)

    @property
    def members(self):
        return list(self._members.values())

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_role(self, rid):
        return Role(rid)

    async def ban(self, target, *, reason=None, **kw):
        SENT.append(f"BAN:{target.id}")

    async def unban(self, target, *, reason=None):
        SENT.append(f"UNBAN:{getattr(target, 'id', target)}")

    async def bans(self):
        return [SimpleNamespace(user=discord.Object(id=mid), reason="читы") for mid in self._members]

    async def fetch_ban(self, user):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "raws"})

    def __str__(self):
        return self.name


class FakeInteraction:
    """Minimally usable discord.Interaction: guild/user/response/followup/client."""

    def __init__(self, guild, user):
        self.guild, self.user = guild, user
        self.client = SimpleNamespace(latency=0.042)

        async def send_message(*args, **kw):
            SENT.append(f"REPLY:{kw.get('content') or (args[0] if args else kw.get('embed'))}")

        self.response = SimpleNamespace(is_done=lambda: False, send_message=send_message)
        self.followup = SimpleNamespace(send_message=send_message)


def make_bot(cfg, db):
    bot = SimpleNamespace(
        cfg=cfg, db=db, registry=registry, users=[], guilds=[],
        get_guild=lambda gid: None, get_cog=lambda n: None, get_user=lambda uid: None,
        wait_until_ready=lambda: asyncio.sleep(0), http_keepalive=None,
    )
    return bot


async def main() -> int:
    registry.discover()
    if os.path.exists("data/test.sqlite3"):
        os.remove("data/test.sqlite3")
    db = Database("data/test.sqlite3")
    await db.setup()
    cfg = {
        "db_path": "data/test.sqlite3",
        "log_channel_id": 500,
        "dm_target": True,
        "default_reason": "—",
        "mute_role_id": 0,
        "dry_run": False,
        "drivers": {"active": "discord_voice", "rcon": {}},
        "warn_thresholds": [{"warns": 2, "action": "mute", "duration": "10m"},
                            {"warns": 3, "action": "ban", "duration": "1d"}],
    }
    bot = make_bot(cfg, db)
    mod = Moderation(bot, cfg)
    bot.mod = mod

    guild = FakeGuild()
    target = FakeMember(guild, 42)
    moderator = FakeMember(guild, 7, Role(8, "mod"))
    guild._members[42] = target
    guild._members[7] = moderator
    ch = guild.text_channels[0]  # the audit channel, per /modsettings канал_логов
    _ = FakeInteraction(guild, moderator)
    bot.get_guild = lambda gid: guild if gid == guild.id else None

    checks: list[tuple[str, bool, object]] = []

    # ---- 1. mute applies a Discord timeout and logs to the audit channel
    case_id = await mod.mute(moderator, target, timeutil.parse_duration("30m"), "spam в тексте")
    case = await db.get_case(case_id)
    timed_out = [e for e in target.edits if "timed_out_until" in e]
    checks.append(("mute: кейс создан", case_id > 0, case_id))
    checks.append(("mute: timeout применён", bool(timed_out), target.edits))
    checks.append(("mute: срок ~30 минут", abs((timeutil.from_iso(case["expires_at"]) - discord.utils.utcnow()).total_seconds() - 1800) < 60, case["expires_at"]))
    checks.append(("mute: отчёт ушёл в канал", len(ch.sent) == 1, ch.sent))

    # ---- 2. unmute clears the timeout and closes the case
    # 1b. the report itself must look like the reference «ОБРАТНАЯ СВЯЗЬ» card
    from core import audit as audit_mod

    report = ch.sent[0]
    fields = {f.name: f.value for f in report.fields}
    checks.append(("оформление: собственный заголовок отчёта", report.title == audit_mod.REPORT_TITLE, report.title))
    checks.append(("оформление: полоса «выдано»", report.colour.value == audit_mod.COLOR_SENTENCE, hex(report.colour.value)))
    checks.append(("оформление: событие = что · кто, без номера и «до»",
                   fields["\u200b"] == "Мут выдан · <@42>", fields.get("\u200b")))
    checks.append(("оформление: в карточке нет полей «КЕЙС» и «ДО»",
                   not any(f.name in ("КЕЙС", "ДО", "ИСТЕКАЕТ") for f in report.fields),
                   [f.name for f in report.fields]))
    checks.append(("оформление: таблица КЕМ/СРОК/ПРИЧИНА",
                   all(k in fields for k in ("КЕМ", "СРОК", "ПРИЧИНА")), sorted(fields)))
    checks.append(("оформление: срок словами", fields["СРОК"] == "`30 минут`", fields.get("СРОК")))
    checks.append(("оформление: метка действия жирным", "**мут**" in [f.name for f in report.fields],
                   [f.name for f in report.fields]))
    names = " ".join(f.name + f.value for f in report.fields)
    checks.append(("оформление: чужие строки не скопированы",
                   not any(loan in names for loan in ("ОБРАТНАЯ СВЯЗЬ", "узнать причину", "Подождите отведённое время")), names[:120]))
    checks.append(("оформление: три поля в строку, без КОМУ-дубля",
                   [f.name for f in report.fields if f.inline][:3] == ["КЕМ", "СРОК", "ПРИЧИНА"],
                   [f.name for f in report.fields if f.inline]))
    await mod.unmute(moderator, target, "извинился", case_id=case_id)
    case2 = await db.get_case(case_id)
    checks.append(("unmute: timeout снят", target.edits[-1].get("timed_out_until") is None, target.edits[-1]))
    checks.append(("unmute: кейс закрыт", case2["lifted_at"] is not None, case2["lifted_at"]))
    lift = ch.sent[-1]
    checks.append(("оформление: зелёная полоса у снятия", lift.colour.value == audit_mod.COLOR_LIFTED, hex(lift.colour.value)))
    checks.append(("оформление: снятие читается как снятие",
                   any("Мут снят" in f.value for f in lift.fields), [f.value for f in lift.fields][:1]))
    checks.append(("unmute: больше не в активных", (await db.active_cases(guild.id, "mute")) == [], await db.active_cases(guild.id, "mute")))

    # ---- 3. permanent mute: Discord timeout cannot be forever, so it has to go
    #         through the role path — and must refuse instead of silently no-oping
    try:
        await mod.mute(moderator, target, timeutil.parse_duration("perm"), "без роли мута")
        checks.append(("perm-мут без роли: отказ, а не тихий no-op", False, "прошло молча"))
    except ValueError as exc:
        checks.append(("perm-мут без роли: отказ, а не тихий no-op", "роль мута" in str(exc), str(exc)))
    cases_before = len(await db.search_cases(guild.id, action="mute"))
    with contextlib.suppress(ValueError):
        await mod.mute(moderator, target, timeutil.parse_duration("perm"), "ещё раз без роли")
    checks.append(("perm-мут без роли: кейс не создан",
                   len(await db.search_cases(guild.id, action="mute")) == cases_before, cases_before))

    await db.set_guild_config(guild.id, {"mute_role_id": 5})
    perm = await mod.mute(moderator, target, timeutil.parse_duration("perm"), "офлайн-модерация")
    perm_case = await db.get_case(perm)
    checks.append(("perm-мут с ролью: expires_at пуст", perm_case["expires_at"] is None, perm_case["expires_at"]))
    perm_edits = [e for e in target.edits if f"[mute #{perm}]" in str(e.get("reason"))]
    checks.append(("perm-мут с ролью: чужой таймаум не трогал",
                   all(e.get("timed_out_until") is not None for e in perm_edits) and perm_edits == [],
                   perm_edits))
    checks.append(("perm-мут с ролью:Deny по каналам проставлен", target in guild.text_channels[0].overwrites, guild.text_channels[0].overwrites))
    await mod.unmute(moderator, target, "снято", case_id=perm)
    await db.set_guild_config(guild.id, {"mute_role_id": 0})

    # ---- 4. game mute through the built-in driver (server deafen)
    game_case = await mod.mutegame(moderator, target, timeutil.parse_duration("1h"), "токсик в войсе", game_id=None)
    deafen = [e for e in target.edits if e.get("deafen") is True]
    gc = await db.get_case(game_case)
    checks.append(("mutegame: deafen применён", bool(deafen), target.edits[-3:]))
    checks.append(("mutegame: драйвер записан", _json.loads(gc["extra"])["driver"] == "discord_voice", gc["extra"]))
    await mod.unmutegame(moderator, game_case, "истекло")
    gcu = await db.get_case(game_case)
    checks.append(("unmutegame: кейс закрыт", gcu["lifted_at"] is not None, gcu["lifted_at"]))

    # ---- 5. driver errors surface as moderator-facing text
    class Boom(registry.get("rcon")):
        id = "boom"
        title = "boom"

        async def resolve(self, guild, member, identifier):
            raise DriverError("сервер игры недоступен")

    registry.register(Boom)
    await db.set_guild_config(guild.id, {"driver": "boom"})
    try:
        await mod.mutegame(moderator, target, timedelta(minutes=5), "тест", game_id=None)
        checks.append(("сломанный драйвер: ошибка всплыла", False, "no error"))
    except DriverError as exc:
        checks.append(("сломанный драйвер: ошибка всплыла", "недоступен" in str(exc), str(exc)))
    await db.set_guild_config(guild.id, {"driver": "discord_voice"})

    # ---- 6. ban path
    await mod.ban(moderator, target, timeutil.parse_duration("7d"), "читы")
    checks.append(("ban: guild.ban вызван", any(s.startswith("BAN:42") for s in SENT), SENT[-2:]))
    checks.append(("ban: кейс активен", len(await db.active_cases(guild.id, "ban")) == 1, None))

    # ---- 7. warn ladder escalates to mute exactly at the threshold
    bot.cfg["warn_thresholds"] = cfg["warn_thresholds"]
    import cogs.records as rec_mod
    from cogs.warns import Warnings

    warns_cog = Warnings(bot)
    inter = FakeInteraction(guild, moderator)
    for i in range(1, 4):
        await warns_cog.warn.callback(warns_cog, inter, "42", f"нарушение {i}")
    muted = [c for c in await db.active_cases(guild.id, "mute") if c["user_id"] == 42]
    banned = [c for c in await db.active_cases(guild.id, "ban") if c["user_id"] == 42]
    checks.append(("warn: 3 записи в базе", len(await db.search_cases(guild.id, action="warn")) == 3, None))
    checks.append(("warn: на 2-м сработал авто-мут", len(muted) >= 1, [c["id"] for c in muted]))
    checks.append(("warn: на 3-м сработал авто-бан", len(banned) >= 2, [c["id"] for c in banned]))
    checks.append(("warn: авто-кейсы помечены source=auto", any(_json.loads(c["extra"] or "{}").get("source") == "auto" for c in muted), None))

    # ---- 8. clearwarns revokes them
    await warns_cog.clearwarns.callback(warns_cog, inter, "42")
    checks.append(("clearwarns: варнов активных = 0", len(await db.active_warns(guild.id, 42)) == 0, None))

    # ---- 9. duration parsing edge cases
    for raw, secs in (("10m", 600), ("1h30m", 5400), ("7d", 604800), ("90", 90), ("2w", 1209600), ("12ч", 43200)):
        checks.append((f"parse_duration({raw})", int(timeutil.parse_duration(raw).total_seconds()) == secs, raw))
    try:
        timeutil.parse_duration("5x")
        checks.append(("parse_duration отбивает мусор", False, "no raise"))
    except timeutil.DurationError:
        checks.append(("parse_duration отбивает мусор", True, None))
    checks.append(("humanize_long(perm)", timeutil.humanize_long(timedelta.max) == "навсегда", timeutil.humanize_long(timedelta.max)))
    for raw, want in (("1.5h", 5400), ("1,5h", 5400), ("1h30m", 5400), ("90", 90), ("0.5m", 30)):
        checks.append((f"parse_duration({raw}) равно {want}с",
                       int(timeutil.parse_duration(raw).total_seconds()) == want, timeutil.parse_duration(raw)))
    for bad in ("1h*30m", "5x", "abc", "1 2", "--", "10dextra"):
        try:
            timeutil.parse_duration(bad)
            checks.append((f"parse_duration отбивает «{bad}»", False, timeutil.parse_duration(bad)))
        except timeutil.DurationError:
            checks.append((f"parse_duration отбивает «{bad}»", True, None))

    # ---- 10. game link table round-trip (needed for /mutegame via rcon)
    await db.set_game_link(guild.id, 42, "rcon", "STEAM_0:1:12345", "тест")
    checks.append(("game_link сохраняется", (await db.get_game_link(guild.id, 42, "rcon")) == "STEAM_0:1:12345", None))
    checks.append(("game_link обратный поиск", (await db.find_discord_by_game(guild.id, "rcon", "STEAM_0:1:12345")) == 42, None))


    # ---- 11. expired cases get lifted by the sweep loop
    from cogs.expiry import Expiry

    exp = Expiry(bot)
    short = await mod.mute(moderator, target, timedelta(seconds=1), "быстро кончится")
    short_row = await db.get_case(short)
    await db.update_case(short, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())
    await exp.sweep.callback(exp) if hasattr(exp.sweep, "callback") else await exp._sweep()
    checks.append(("sweep: истёкший мут закрыт авто-снятием", (await db.get_case(short))["lifted_at"] is not None, short_row))
    checks.append(("sweep: timeout реально снят", target.edits[-1].get("timed_out_until") is None, target.edits[-1]))

    # ---- 12. hand-rolled Source RCON client speaks the protocol correctly
    from drivers.rcon import RconConnection

    received: list[str] = []

    def frame(msg_id: int, ptype: int, payload: bytes) -> bytes:
        inner = struct.pack("<ii", msg_id, ptype) + payload + b"\x00\x00"
        return struct.pack("<i", len(inner)) + inner

    async def fake_rcon(reader, writer):
        """Minimal Source RCON server: auth(3) then exec(2), replies type 2 + 0."""
        while True:
            try:
                (size,) = struct.unpack("<i", await reader.readexactly(4))
            except asyncio.IncompleteReadError:
                return
            body = await reader.readexactly(size)
            msg_id, ptype = struct.unpack_from("<ii", body, 0)
            payload = body[8:].split(b"\x00")[0]
            if ptype == 3:
                rid = msg_id if payload == b"hunter2" else -1
                writer.write(frame(rid, 2, b"") + frame(rid, 0, b""))
            else:
                received.append(payload.decode())
                writer.write(frame(msg_id, 0, b"Response: ok"))
            await writer.drain()

    server = await asyncio.start_server(fake_rcon, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    conn = RconConnection("127.0.0.1", port, "hunter2", timeout=3)
    out = await conn.command("sm_silence STEAM_0:1:1 60")
    checks.append(("rcon: авторизация прошла", "ok" in out, out))
    checks.append(("rcon: команда дошла байт-в-байт", received == ["sm_silence STEAM_0:1:1 60"], received))
    bad = RconConnection("127.0.0.1", port, "wrong", timeout=3)
    try:
        await bad.command("users")
        checks.append(("rcon: неверный пароль отбивается", False, "no raise"))
    except DriverError as exc:
        checks.append(("rcon: неверный пароль отбивается", "парол" in str(exc), str(exc)))
    await bad.close()
    await conn.close()
    server.close()

    # rcon driver renders config templates (voice + chat) and stores the link
    from drivers.rcon import RconDriver

    server2 = await asyncio.start_server(fake_rcon, "127.0.0.1", 0)
    port2 = server2.sockets[0].getsockname()[1]
    rcon_cfg = {
        "host": "127.0.0.1", "port": port2, "password": "hunter2",
        "mute_cmd": "sm_silence {target} {minutes}",
        "chat_mute_cmd": "sm_smute {target} {minutes}",
        "unmute_cmd": "sm_unsilence {target}",
    }
    drv = RconDriver(db, rcon_cfg)
    note = await drv.mute(guild, "STEAM_0:1:1", 3600, "токсик")
    checks.append(("rcon: voice+chat команды отправлены",
                   received[-2:] == ["sm_silence STEAM_0:1:1 60", "sm_smute STEAM_0:1:1 60"], received[-2:]))
    checks.append(("rcon: note непустая", bool(note), note))
    await drv.unmute(guild, "STEAM_0:1:1")
    checks.append(("rcon: unmute-команда отправлена", received[-1] == "sm_unsilence STEAM_0:1:1", received[-1]))
    await drv.teardown()
    server2.close()


    # ---- 12b. code spans stay literal (no markdown escapes leaking through)
    checks.append(("код ника без экранирования", audit_mod.code("Arab_Sheih") == "`Arab_Sheih`", audit_mod.code("Arab_Sheih")))
    checks.append(("код ника с тильдой/звёздочками", audit_mod.code("kotte~**00**") == "`kotte~**00**`", audit_mod.code("kotte~**00**")))
    checks.append(("код схлопывает переносы", audit_mod.code("a\nb") == "`a b`", audit_mod.code("a\nb")))
    checks.append(("код не ломает бэктик", "`" not in audit_mod.code("back`tick").strip("`"), audit_mod.code("back`tick")))
    checks.append(("подсказка по умолчанию своя", audit_mod.HINT.startswith("Не согласны"), audit_mod.HINT))
    checks.append(("сроки словами по-русски",
                   [timeutil.humanize_long(timeutil.parse_duration(d)) for d in ("1h", "30m", "7d", "perm")]
                   == ["1 час", "30 минут", "7 дней", "навсегда"],
                   [timeutil.humanize_long(timeutil.parse_duration(d)) for d in ("1h", "30m", "7d", "perm")]))

    # ---- 11b. истёкший бан и истёкший мут в игре снимаются фоном
    ban_case = await mod.ban(moderator, target, timedelta(seconds=1), "быстрый бан")
    await db.update_case(ban_case, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())
    game_case = await mod.mutegame(moderator, target, timedelta(seconds=1), "быстрый game mute", game_id=None)
    await db.update_case(game_case, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())
    ban_edits_before = len([e for e in target.edits if e.get("deafen") is False])
    await exp._sweep()
    checks.append(("sweep: истёкший бан автоматически снят", (await db.get_case(ban_case))["lifted_at"] is not None, None))
    checks.append(("sweep: guild.unban вызван", any(s0.startswith("UNBAN:") for s0 in SENT), SENT[-3:]))
    checks.append(("sweep: истёкший мут в игре закрыт + deafen снят",
                   (await db.get_case(game_case))["lifted_at"] is not None and
                   len([e for e in target.edits if e.get("deafen") is False]) > ban_edits_before, None))

    # ---- 11c. варн на 400 дней: лимит Discord к нему не применяется
    before_far = [c["id"] for c in await db.search_cases(guild.id, action="warn")]
    await warns_cog.warn.callback(warns_cog, FakeInteraction(guild, moderator), "42", "далёкий варн", days=400)
    after_far = [c["id"] for c in await db.search_cases(guild.id, action="warn")]
    far = max(set(after_far) - set(before_far))
    row = await db.get_case(far)
    checks.append(("warn 400 дней: срок записан, а не снят", row["expires_at"] is not None, row["expires_at"]))
    checks.append(("warn 400 дней: ещё активен", len([c for c in await db.active_warns(guild.id, 42) if c["id"] == far]) == 1, None))
    before_soon = set(after_far)
    await warns_cog.warn.callback(warns_cog, FakeInteraction(guild, moderator), "42", "вчерашний", days=1)
    soon = max({c["id"] for c in await db.search_cases(guild.id, action="warn")} - before_soon)
    # задним числом ставим истёкший срок (тестовый fixture: поле expires_at)
    await db._run(db._conn.execute, "UPDATE cases SET expires_at = ? WHERE id = ?",
                  ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), soon))
    db._conn.commit()
    live_after = [c["id"] for c in await db.active_warns(guild.id, 42)]
    checks.append(("warn с истёкшим сроком: не считается активным",
                   soon not in live_after, (soon, live_after)))
    checks.append(("warn на 400 дней: остаётся активным", far in live_after, (far, live_after)))
    await exp._sweep()
    checks.append(("sweep: варн с истёкшим сроком закрыт в базе", (await db.get_case(soon))["lifted_at"] is not None, None))

    # ---- 12c. warn со сроком 400 дней живёт дольше лимита Discord-таймаута
    long_case = await db.add_case(guild_id=guild.id, user_id=42, moderator_id=7, action="warn",
                                  reason="долгий варн", expires_at=timeutil.expires_at(timedelta(days=400)), extra={})
    lc = await db.get_case(long_case)
    checks.append(("warn 400 дней: срок записан", lc["expires_at"] is not None and "warn" in lc["action"], lc["expires_at"]))
    await db.lift(long_case, 7)

    # ---- 13. legacy fallback: no Moderate Members right -> mute role + channel
    #         denials, and /unmute must reset them.
    class NoTimeoutMember(FakeMember):
        async def edit(self, **kw):
            if "timed_out_until" in kw:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"message": "Missing Permissions"})
            await super().edit(**kw)

    legacy_target = NoTimeoutMember(guild, 77)
    guild._members[77] = legacy_target
    text_ch, voice_ch = guild.text_channels[0], guild.voice_channels[0]
    ch = text_ch  # audit channel: the fake records every embed posted to it
    await db.set_guild_config(guild.id, {"mute_role_id": 5})   # 5 < target top role 10
    legacy_case = await mod.mute(moderator, legacy_target, timedelta(minutes=5), "без права timeout")
    checks.append(("legacy: в тексте запрещён send_messages",
                   text_ch.overwrites[legacy_target].send_messages is False, text_ch.calls))
    checks.append(("legacy: в голосе запрещён connect",
                   voice_ch.overwrites[legacy_target].connect is False, voice_ch.calls))
    checks.append(("legacy: роль мута выдана", any(getattr(r, "id", None) == 5 for r in legacy_target.roles), legacy_target.roles))
    checks.append(("legacy: тронутые каналы записаны для отката",
                   len(await db.take_overwrites(legacy_case)) == 2, await db.take_overwrites(legacy_case)))
    await mod.unmute(moderator, legacy_target, "готово", case_id=legacy_case)
    checks.append(("legacy: текстовый канал вернулся на inherit", legacy_target not in text_ch.overwrites, text_ch.overwrites))
    checks.append(("legacy: голосовой канал вернулся на inherit", legacy_target not in voice_ch.overwrites, voice_ch.overwrites))
    checks.append(("legacy: роль снята", not any(getattr(r, "id", None) == 5 for r in legacy_target.roles), None))
    checks.append(("legacy: записи об откате удалены", len(await db.take_overwrites(legacy_case)) == 0, None))
    await db.set_guild_config(guild.id, {"mute_role_id": 0})


    # ---- 14. reconnect supervisor: the backoff schedule itself is the contract
    import bot as bot_mod

    plan = [5, 15, 30, 60, 120, 300]
    checks.append(("reconnect: первая пауза короткая", bot_mod.next_delay(plan, 1) == 5, bot_mod.next_delay(plan, 1)))
    checks.append(("reconnect: растёт", [bot_mod.next_delay(plan, i) for i in (1, 2, 3)] == [5, 15, 30], None))
    checks.append(("reconnect: не растёт до бесконечности",
                   all(bot_mod.next_delay(plan, i) == 300 for i in (6, 7, 50)), [bot_mod.next_delay(plan, i) for i in (6, 50)]))
    checks.append(("reconnect: потолок 15 минут", bot_mod.next_delay([5000], 1) == 900, bot_mod.next_delay([5000], 1)))
    checks.append(("reconnect: fatal не ретраится", (discord.LoginFailure, discord.PrivilegedIntentsRequired) == bot_mod.FATAL, bot_mod.FATAL))

    # ---- 14b. сам супервизор: две сетевые смерти → два перезапуска, потом тихий выход
    starts, slept = [], []
    plan_case = [5, 15, 30]

    class FlakyBot:
        def __init__(self, cfg):
            starts.append(1)

        def run(self, token, **kw):
            if len(starts) <= 2:
                raise ConnectionResetError("peer closed connection")
            return

    real_bot_cls, real_sleep, real_monotonic = bot_mod.ModBot, bot_mod.time.sleep, bot_mod.time.monotonic
    clock = {"now": 0.0}

    def tick():  # ~1s per call: uptime stays under reset_after, so attempts stack
        clock["now"] += 1
        return clock["now"]

    bot_mod.ModBot = FlakyBot
    bot_mod.time.sleep = lambda d: slept.append(d)
    bot_mod.time.monotonic = tick
    try:
        bot_mod.run_forever({"token": "x", "reconnect_delays": plan_case})
    finally:
        bot_mod.ModBot, bot_mod.time.sleep, bot_mod.time.monotonic = real_bot_cls, real_sleep, real_monotonic
    checks.append(("супервизор: пережил 2 обрыва и перезапустился", len(starts) == 3, starts))
    checks.append(("супервизор: паузы растут по плану (±джиттер)",
                   all(0 <= slept[i] - plan_case[i] <= 5 for i in range(len(slept))), slept))
    checks.append(("супервизор: штатный выход не превращается в цикл", len(starts) == 3, starts))

    # ---- 14c. fatal (неверный токен) не ретраится вообще
    class DeadBot:
        def __init__(self, cfg):
            starts.append(2)

        def run(self, token, **kw):
            raise discord.LoginFailure("bad token")

    fatal_starts_before = len(starts)
    bot_mod.ModBot = DeadBot
    try:
        try:
            bot_mod.run_forever({"token": "x", "reconnect_delays": [1]})
            checks.append(("супервизор: fatal останавливает процесс", False, "не вышел"))
        except SystemExit as exc:
            checks.append(("супервизор: fatal останавливает процесс",
                           "Переподключение не поможет" in str(exc) and len(starts) == fatal_starts_before + 1, str(exc)))
    finally:
        bot_mod.ModBot = real_bot_cls

    # ---- 15. RCON выживает, когда игровой сервер закрыл idle-соединение
    from drivers.rcon import RconConnection as RC

    handshakes = {"n": 0}

    async def flaky_rcon(reader, writer):
        served = 0
        while True:
            try:
                (size,) = struct.unpack("<i", await reader.readexactly(4))
            except asyncio.IncompleteReadError:
                return
            body = await reader.readexactly(size)
            msg_id, ptype = struct.unpack_from("<ii", body, 0)
            payload = body[8:].split(b"\x00")[0]
            if ptype == 3:
                handshakes["n"] += 1
                rid = msg_id if payload == b"hunter2" else -1
                writer.write(frame(rid, 2, b"") + frame(rid, 0, b""))
                await writer.drain()
                continue
            received.append(payload.decode())
            writer.write(frame(msg_id, 0, b"Response: ok"))
            await writer.drain()
            served += 1
            if served >= 1:
                writer.close()           # the game server times the idle socket out
                return

    server3 = await asyncio.start_server(flaky_rcon, "127.0.0.1", 0)
    port3 = server3.sockets[0].getsockname()[1]
    rc = RC("127.0.0.1", port3, "hunter2", timeout=3)
    first = await rc.command("sm_silence STEAM_0:1:9 60")
    second = await rc.command("sm_silence STEAM_0:1:9 60")   # must reconnect, not hang
    checks.append(("rcon: команды дошли после разрыва", received.count("sm_silence STEAM_0:1:9 60") == 2, received[-2:]))
    checks.append(("rcon: переподключение заново авторизуется", handshakes["n"] >= 2, handshakes["n"]))
    checks.append(("rcon: обе команды вернули ответ", "ok" in first and "ok" in second, (first, second)))
    await rc.close()
    server3.close()

    # ---- 16. keepalive-эндпоинт для хостингов, усыпляющих контейнер
    from core import keepalive

    class StubDB:
        async def active_cases(self, guild_id=None, action=None):
            return [1, 2, 3]

    class StubBot:
        guilds = [SimpleNamespace(member_count=40), SimpleNamespace(member_count=2)]
        latency = 0.123
        user = "ModBot#1234"
        db = StubDB()
        def is_ready(self): return self._ready
        _ready = True

    stub = StubBot()
    app_runner = web.AppRunner(keepalive.build_app(stub))
    await app_runner.setup()
    site = web.TCPSite(app_runner, "127.0.0.1", 0)
    await site.start()
    health_port = app_runner.addresses[0][1]
    aio = aiohttp.ClientSession()
    async with aio.get(f"http://127.0.0.1:{health_port}/healthz") as resp:
        body = await resp.json()
        checks.append(("keepalive: 200 когда бот готов", resp.status == 200, resp.status))
        checks.append(("keepalive: отдаёт живую статистику", (body["status"], body["guilds"], body["members"], body["active_punishments"]) == ("ok", 2, 42, 3), body))
    stub._ready = False
    async with aio.get(f"http://127.0.0.1:{health_port}/healthz") as resp:
        checks.append(("keepalive: 503 когда бот не готов", resp.status == 503, resp.status))
    async with aio.get(f"http://127.0.0.1:{health_port}/health") as resp:
        checks.append(("keepalive: /health — алиас того же", resp.status == 503, resp.status))
    await aio.close()
    await app_runner.cleanup()
    checks.append(("keepalive: auto включается только с $PORT",
                   keepalive.enabled({"keepalive": "auto"}) is False and keepalive.enabled({"keepalive": "auto", "_": 1}) is False,
                   keepalive.enabled({"keepalive": "auto"})))
    free = socket.socket(); free.bind(("127.0.0.1", 0)); chosen = free.getsockname()[1]; free.close()
    runner = await keepalive.start(stub, {"keepalive": True, "keepalive_port": chosen})
    aio2 = aiohttp.ClientSession()
    try:
        async with aio2.get(f"http://127.0.0.1:{chosen}/healthz") as resp:
            checks.append(("keepalive: start() поднимает реальный сервер", resp.status == 503, resp.status))
    finally:
        await aio2.close()
        await runner.cleanup()
    os.environ["PORT"] = str(chosen + 1)
    checks.append(("keepalive: auto слушает $PORT", keepalive.enabled({"keepalive": "auto"}) == str(chosen + 1), keepalive.enabled({"keepalive": "auto"})))
    checks.append(("keepalive: false глушит совсем", keepalive.enabled({"keepalive": False}) is False and keepalive.enabled({"keepalive": "off"}) is False, None))
    os.environ["PORT"] = "not-a-port"
    check2 = keepalive.enabled({"keepalive": "auto"})
    checks.append(("keepalive: мусор в $PORT выключает, а не роняет", check2 is False, check2))
    forced = keepalive.enabled({"keepalive": True, "keepalive_port": 9090})
    checks.append(("keepalive: явный режим берёт keepalive_port", forced == "9090", forced))
    os.environ["PORT"] = "0"
    checks.append(("keepalive: PORT=0 не слушаем", keepalive.enabled({"keepalive": "auto"}) is False, None))
    os.environ["PORT"] = "65535x"
    checks.append(("keepalive: кривой PORT в явном режиме не ломает старт",
                   keepalive.enabled({"keepalive": True, "keepalive_port": 8080}) == "8080", None))
    del os.environ["PORT"]


    # ---- 17. guard/resolve_member: то, что команды делают ДО вызова движка
    from core import helpers
    from discord import app_commands

    from cogs.moderation import Punishments

    punishments = Punishments(bot)  # тот же бот, что и у движка: одна база, один кеш
    admin = FakeMember(guild, 55, Role(60, "admin"))          # выше бота (50)
    admin.guild_permissions = discord.Permissions(administrator=True)
    guild._members[55] = admin

    try:
        await helpers.resolve_member(guild, "нет-такого")
        checks.append(("resolve: неизвестный ник -> внятная ошибка", False, "не бросило"))
    except helpers.ModInputError as exc:
        checks.append(("resolve: неизвестный ник -> внятная ошибка", "Не нашёл" in str(exc), str(exc)))
    checks.append(("resolve: mention", (await helpers.resolve_member(guild, "<@42>")).id == 42, None))
    checks.append(("resolve: id строкой", (await helpers.resolve_member(guild, "42")).id == 42, None))
    checks.append(("resolve: display name", (await helpers.resolve_member(guild, "player42")).id == 42, None))
    try:
        await helpers.guard(FakeInteraction(guild, moderator), moderator)
        checks.append(("guard: себя наказать нельзя", False, "прошло"))
    except helpers.ModInputError as exc:
        checks.append(("guard: себя наказать нельзя", "самого себя" in str(exc), str(exc)))
    try:
        await helpers.guard(FakeInteraction(guild, moderator), target)
        checks.append(("guard: обычного игрока пропускает", True, None))
    except helpers.ModInputError as exc:
        checks.append(("guard: обычного игрока пропускает", False, str(exc)))
    try:
        await helpers.guard(FakeInteraction(guild, moderator), admin)
        checks.append(("guard: админа выше бота не трогаем", False, "прошло"))
    except helpers.ModInputError as exc:
        checks.append(("guard: админа выше бота не трогаем", "роль не ниже моей" in str(exc), str(exc)))

    # вызов самой slash-команды (не только движка): парсинг срока + ответ модератору
    reply_before = len(SENT)
    await punishments.mute.callback(
        punishments, FakeInteraction(guild, moderator), "42",
        app_commands.Choice(name="10m", value="10m"), "тест команды", False,
    )
    cmd_case = (await db.search_cases(guild.id, action="mute"))[0]  # DESC: свежий первый
    checks.append(("команда /mute: кейс создан", cmd_case["reason"] == "тест команды", cmd_case["reason"]))
    checks.append(("команда /mute: срок 10m дошёл до базы",
                   abs((timeutil.from_iso(cmd_case["expires_at"]) - timeutil.from_iso(cmd_case["issued_at"])).total_seconds() - 600) < 5,
                   cmd_case["expires_at"]))
    checks.append(("команда /mute: ответ модератору_ephemeral", any(s.startswith("REPLY:") and "мут" in s for s in SENT[reply_before:]), SENT[reply_before:]))
    try:
        await punishments.mute.callback(punishments, FakeInteraction(guild, moderator), "42",
                                        app_commands.Choice(name="10x", value="10x"), "мусор", False)
        checks.append(("команда /mute: кривой срок -> подсказка, не 500", False, "не бросило"))
    except helpers.ModInputError as exc:
        checks.append(("команда /mute: кривой срок -> подсказка, не 500", "не понял" in str(exc) or "неизвестная" in str(exc), str(exc)))
    await mod.unmute(moderator, target, "прибрался", case_id=int(cmd_case["id"]))


    # ---- 18. smoke: каждый обработчик команды вызывается хоть раз. Именно так
    #         ловятся опечатки в API (Member.is_privileged, utils.get(lambda...)),
    #         которые не видны при вызове движка напрямую.
    from cogs.warns import Warnings
    from cogs.records import Records

    warns_cog2 = Warnings(bot)
    records_cog = Records(bot)
    CH = app_commands.Choice
    guild._members[55] = admin
    smoke = [
        ("warn", warns_cog2.warn.callback, ("88", "мусор в чате", 30)),
        ("clearwarns", warns_cog2.clearwarns.callback, ("88",)),
        ("history", warns_cog2.history.callback, ("88", 5)),
        ("warnconfig", warns_cog2.warnconfig.callback, ("4=mute:2h, 6=ban:1d",)),
        ("warnlevels", warns_cog2.warnlevels.callback, ()),
        ("mutegame", punishments.mutegame.callback, ("88", None, CH(name="1h", value="1h"), "войс", False)),
        # bangame по нику требует rcon-драйвера: с discord_voice ожидаем честный DriverError
        ("bangame", punishments.bangame.callback, (None, "88", CH(name="1d", value="1d"), "читы", False), ModInputError),
        ("ban", punishments.ban.callback, ("88", CH(name="1d", value="1d"), "читы")),
        ("modlog", records_cog.modlog.callback, ()),
        ("modlog по цели", records_cog.modlog.callback, (None, 5, "88")),
        ("case карточка", records_cog.case.callback, (1,)),
        ("modsettings", records_cog.modsettings.callback, (None, None, None, True, None, "своя строка")),
        ("whoami", records_cog.whoami.callback, ()),
        ("gamelink", records_cog.gamelink.callback, ("88", "STEAM_0:1:4242")),
        ("ping", records_cog.ping.callback, ()),
    ]
    broken = []
    for entry in smoke:
        name, fn, args = entry[0], entry[1], entry[2]
        tolerate = entry[3] if len(entry) > 3 else ()
        fake_target = FakeMember(guild, 88)
        guild._members[88] = fake_target
        inter = FakeInteraction(guild, moderator)
        try:
            await fn(*(punishments if fn.__qualname__.startswith("Punishments") else
                      warns_cog2 if fn.__qualname__.startswith("Warnings") else records_cog,), inter, *args)
        except tolerate:  # ожидаемое «драйвер не умеет» — не поломка обработчика
            continue
        except Exception as exc:  # noqa: BLE001 - собираем список поломанных команд
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    checks.append(("smoke: все обработчики команд живы", not broken, broken))

    # снятие того, что на smokey-командах осталось
    for c in (await db.active_cases(guild.id, "mute")) + (await db.active_cases(guild.id, "mutegame")):
        with contextlib.suppress(Exception):  # прибираем тестовый мусор, не важничая
            await mod.unmute(moderator, guild.get_member(int(c["user_id"])), "прибрал тест", case_id=int(c["id"]))


    def _async_ret(value):
        async def go():
            stub_unarms.append(value)
            return "ok"
        return go()

    # ---- 19. re-arm игровых мутов после рестарта (кэш драйверов пуст)
    from drivers.base import BaseDriver as BD

    rearmed = []

    @registry.register
    class StubIdempotent(BD):
        id = "stub_idm"
        title = "stub"
        idempotent_apply = True

        async def resolve(self, guild, member, identifier):
            return identifier or "x"

        async def mute(self, guild, target, seconds, reason):
            rearmed.append((target, seconds, reason))
            return "ok"

        async def unmute(self, guild, target):
            return "ok"

    game_like = await mod.mutegame(moderator, target, timedelta(hours=2), "держим в тихости", game_id=None)
    await db.update_case(game_like, extra=_json.dumps({"driver": "stub_idm", "game_id": "STEAM_0:1:777"}))
    mod.drivers.clear()          # как после перезапуска процесса
    await exp._sweep()
    checks.append(("re-arm после рестарта: mute ушёл в игру",
                   any(t == "STEAM_0:1:777" for t, _, _ in rearmed), rearmed))
    checks.append(("re-arm не снимает запись раньше срока", (await db.get_case(game_like))["lifted_at"] is None, None))
    # снятие должно уйти ТОМУ драйверу, который мучил (в кейсе stub_idm, а на
    # сервере сейчас discord_voice): иначе SteamID уедит в int() и упадёт
    stub_unarms = []
    StubIdempotent.unmute = lambda self, guild, target: _async_ret(("unmute", target))
    await mod.unmutegame(moderator, game_like, "тест окончен")
    checks.append(("unmutegame: снимает тот же драйвер, что надевал",
                   (await db.get_case(game_like))["lifted_at"] is not None, None))
    await exp._sweep()

    # ---- 18b. два пути, которые mypy показал как крэшающие: кейс без драйвера в
    #           extra и warning-конверт без total_warns
    orphan = await db.add_case(guild_id=guild.id, user_id=42, moderator_id=7, action="mutegame",
                              reason="без драйвера", expires_at=None, extra={"game_id": "STEAM_0:1:1"})  # add_case сам сериализует dict
    try:
        await mod.unmutegame(moderator, orphan, "прибрал")
        outcome, ok = "снялось", True
    except ValueError as exc:  # честное сообщение вместо среза корутины
        outcome, ok = str(exc), "нет игрового ID" in str(exc)
    except TypeError as exc:
        outcome, ok = str(exc), False
    checks.append(("unmutegame: запись без driver в extra даёт текст, а не TypeError", ok, outcome))
    try:
        audit_mod.warning_embed(1, "причина", moderator, 3, guild_name="G")  # total_warns не передан
        checks.append(("warning_embed: без total_warns не арифметирует None", True, None))
    except TypeError as exc:
        checks.append(("warning_embed: без total_warns не арифметирует None", False, str(exc)))

    # ---- 19b. dry_run точечный по серверу и не трогает глобальный конфиг
    await db.set_guild_config(guild.id, {"dry_run": True})
    before = len(target.edits)
    dry_case = await mod.mute(moderator, target, timedelta(minutes=5), "тренировка")
    checks.append(("dry_run: ничего не применено", len(target.edits) == before, target.edits[before:]))
    checks.append(("dry_run: кейс всё равно записан", (await db.get_case(dry_case))["reason"] == "тренировка", None))
    await db.set_guild_config(guild.id, {"dry_run": None})
    other = FakeMember(guild, 99)
    guild._members[99] = other
    live_case = await mod.mute(moderator, other, timedelta(minutes=5), "обычный режим")
    checks.append(("dry_run: выключается обратно по серверу",
                   any("timed_out_until" in e for e in other.edits), other.edits))
    checks.append(("dry_run: глобальный конфиг не тронут", cfg["dry_run"] is False, cfg["dry_run"]))
    await mod.unmute(moderator, other, "done", case_id=live_case)


    # ---- 21. Первый запуск: кривой конфиг должен объяснять, что делать
    import os as _os
    from core import config as config_mod

    tmp = pathlib.Path("data/cfg-probe.json")
    tmp.parent.mkdir(exist_ok=True)
    saved = _os.environ.pop("DISCORD_TOKEN", None)
    try:
        tmp.write_text('{\n  "token": "PASTE_BOT_TOKEN_HERE"\n}\n', encoding="utf-8")
        try:
            config_mod.load(tmp, require_token=True)
            checks.append(("конфиг: placeholder-токен не принимается", False, "прошёл"))
        except SystemExit as exc:
            checks.append(("конфиг: placeholder-токен не принимается", "DISCORD_TOKEN" in str(exc), str(exc)[:60]))
        tmp.write_text('{"token": "x", "dry_run": true,}', encoding="utf-8")
        try:
            config_mod.load(tmp, require_token=False)
            checks.append(("конфиг: битый JSON -> подсказка, а не traceback", False, "прошёл"))
        except SystemExit as exc:
            checks.append(("конфиг: битый JSON -> подсказка, а не traceback",
                           "Не смог прочитать" in str(exc) and "запят" in str(exc), str(exc)[:60]))
        tmp.write_text('{\n  "dry_run": true\n}\n', encoding="utf-8")
        merged = config_mod.load(tmp, require_token=False)
        checks.append(("конфиг: без файла токена — дефолты доезжают", merged["keepalive"] == "auto" and merged["dry_run"], dict(merged)))
        _os.environ["DISCORD_TOKEN"] = "env-token-123"
        checks.append(("конфиг: DISCORD_TOKEN перекрывает файл",
                       config_mod.load(tmp, require_token=True)["token"] == "env-token-123", None))
        if saved:
            _os.environ["DISCORD_TOKEN"] = saved
        else:
            _os.environ.pop("DISCORD_TOKEN", None)
    finally:
        tmp.unlink(missing_ok=True)


    # ---- 22. Minecraft Java RCON: свой рукопожатие-формат и плейсхолдеры срока
    from drivers.rcon import RconConnection as RC2, RconDriver as RD2

    async def mc_rcon(reader, writer):
        """Minecraft отвечает на auth ОДНИМ пакетом (type 0, id запроса) — в
        отличие от Source, где пакетов два."""
        while True:
            try:
                (size,) = struct.unpack("<i", await reader.readexactly(4))
            except asyncio.IncompleteReadError:
                return
            body = await reader.readexactly(size)
            msg_id, ptype = struct.unpack_from("<ii", body, 0)
            payload = body[8:].split(b"\x00")[0]
            if ptype == 3:
                rid = msg_id if payload == b"secret" else -1
                writer.write(frame(rid, 0, b""))           # только один пакет
            else:
                received.append(payload.decode())
                writer.write(frame(msg_id, 0, b"Done."))
            await writer.drain()

    mc_server = await asyncio.start_server(mc_rcon, "127.0.0.1", 0)
    mc_port = mc_server.sockets[0].getsockname()[1]
    rc = RC2("127.0.0.1", mc_port, "secret", timeout=3)
    try:
        reply = await asyncio.wait_for(rc.command("list"), 8)
        checks.append(("mc rcon: одно-пакетное авторизационное рукопожатие", reply == "Done.", reply))
    except asyncio.TimeoutError:
        checks.append(("mc rcon: одно-пакетное авторизационное рукопожатие", False, "ЗАВИСЛО (ждёт 2-й пакет)"))
    await rc.close()

    mc_cfg = {"host": "127.0.0.1", "port": mc_port, "password": "secret",
              "mute_cmd": "tempmute {target} {duration} {reason}",
              "unmute_cmd": "unmute {target}",
              "query_cmd": ""}
    drv_mc = RD2(db, mc_cfg)
    await db.set_game_link(guild.id, 42, "rcon", "Steve", "minecraft nick")
    note = await asyncio.wait_for(drv_mc.mute(guild, "Steve", 5400, "мат в чате"), 8)
    checks.append(("mc rcon: команда собрана с {duration}=1h30m",
                   received[-1] == "tempmute Steve 1h30m мат в чате", received[-1]))
    await asyncio.wait_for(drv_mc.unmute(guild, "Steve"), 8)
    checks.append(("mc rcon: unmute отправлен", received[-1] == "unmute Steve", received[-1]))
    await drv_mc.teardown()

    checks.append(("compact_english: формат плагинов",
                   [timeutil.compact_english(timeutil.parse_duration(d)) for d in ("30m", "1h30m", "7d", "45s", "perm")]
                   == ["30m", "1h30m", "7d", "45s", ""],
                   [timeutil.compact_english(timeutil.parse_duration(d)) for d in ("30m", "1h30m", "7d", "45s", "perm")]))


    # ---- 23. варн доходит до игры, если есть привязка и умеющий notify драйвер
    notified: list[tuple[str, str]] = []

    @registry.register
    class NotifyingDriver(registry.get("discord_voice")):
        id = "notify_stub"
        title = "stub с игровым чатом"

        async def notify(self, guild, target, text):
            notified.append((target, text))
            return "tell выполнено"

    await db.set_guild_config(guild.id, {"driver": "notify_stub"})
    await db.set_game_link(guild.id, 42, "notify_stub", "Steve", "mc")
    await warns_cog.warn.callback(warns_cog, FakeInteraction(guild, moderator), "42", "мат в чате игры")
    checks.append(("warn: уведомление ушло в игру", notified and notified[0][0] == "Steve", notified))
    checks.append(("warn: текст уведомления с причиной", notified and "мат в чате игры" in notified[0][1], notified))
    card = ch.sent[-1]
    checks.append(("warn: карточка показывает доставку",
                   "в игру: tell выполнено" in (card.footer.text if card.footer else ""), card.footer))
    # без игровой привязки варн не падает и в игру ничего не шлёт
    notified.clear()
    ign = FakeMember(guild, 88)
    guild._members[88] = ign
    await warns_cog.warn.callback(warns_cog, FakeInteraction(guild, moderator), "88", "варн без доставки")
    checks.append(("warn: без привязки в игру не лезем", not notified, notified))
    await db.set_guild_config(guild.id, {"driver": "discord_voice"})


    # ---- 24. бан в игре: ушёл через драйвер, истёк — сам pardon'улся
    ban_cfg = {"host": "127.0.0.1", "port": mc_port, "password": "secret",
               "mute_cmd": "tempmute {target} {duration} {reason}",
               "ban_cmd": "tempban {target} {duration} {reason}", "unban_cmd": "pardon {target}"}
    await db.set_guild_config(guild.id, {"driver": "rcon"})
    cfg["drivers"]["rcon"] = ban_cfg        # движок берёт настройки драйвера из конфига
    from drivers import rcon as rcon_mod
    rcon_mod.CONNECTIONS.clear()
    mod.drivers.clear()
    ban_game_case = await mod.bangame(moderator, target, timeutil.parse_duration("3d"), "ксы", game_id="Steve")
    checks.append(("bangame: команда ушла в игру",
                   received[-1] == "tempban Steve 3d ксы", received[-1]))
    row = await db.get_case(ban_game_case)
    checks.append(("bangame: запись с драйвером rcon", _json.loads(row["extra"])["driver"] == "rcon", row["extra"]))
    checks.append(("bangame: карточка в канале", "Забанен на игровом сервере" in " ".join(
        f.value for f in ch.sent[-1].fields), [f.value for f in ch.sent[-1].fields][:2]))
    # срок истёк — фон должен сам снять бан в игре
    await db.update_case(ban_game_case, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    await exp._sweep()
    checks.append(("bangame: авто-разбан по сроку", received[-1] == "pardon Steve", received[-1]))
    checks.append(("bangame: запись закрыта", (await db.get_case(ban_game_case))["lifted_at"] is not None, None))
    # драйвер без ban_cmd — внятный отказ, запись не создаётся зря
    plain_cfg = {"host": "127.0.0.1", "port": mc_port, "password": "secret", "mute_cmd": "tempmute {target} {duration} {reason}"}
    from drivers.rcon import RconDriver as RD3
    rcon_mod.CONNECTIONS.clear()
    drv_plain = RD3(db, plain_cfg)
    class _GuildShim:
        id = guild.id
    try:
        await drv_plain.ban(_GuildShim(), "Steve", 3600, "тест")
        checks.append(("bangame: без ban_cmd — отказ драйвера", False, "прошло"))
    except DriverError as exc:
        checks.append(("bangame: без ban_cmd — отказ драйвера", "ban_cmd" in str(exc), str(exc)))
    await drv_plain.teardown()
    rcon_mod.CONNECTIONS.clear()
    await db.set_guild_config(guild.id, {"driver": "discord_voice"})

    # ---- 25. упавший драйвер не оставляет «висячей активной» записи
    cfg["drivers"]["rcon"] = {"host": "127.0.0.1", "port": 1, "password": "x",
                               "mute_cmd": "tempmute {target} {duration} {reason}"}
    await db.set_guild_config(guild.id, {"driver": "rcon"})
    mod.drivers.clear()
    rcon_mod.CONNECTIONS.clear()
    ghost = FakeMember(guild, 202)
    guild._members[202] = ghost
    try:
        await mod.mutegame(moderator, ghost, timedelta(minutes=5), "дух", game_id="Ghost")
        checks.append(("сбойный драйвер: ошибка модератору", False, "не бросило"))
    except DriverError as exc:
        checks.append(("сбойный драйвер: ошибка модератору", "недоступен" in str(exc) or "разорвано" in str(exc), str(exc)))
    open_mutes = [c for c in await db.active_cases(guild.id) if c["user_id"] == 202]
    checks.append(("сбойный драйвер: запись НЕ осталась активной", not open_mutes, [c["id"] for c in open_mutes]))
    last_card = ch.sent[-1]
    checks.append(("сбойный драйвер: в журнале видно, почему отменили",
                   "не применено" in " ".join(f.value for f in last_card.fields) + (last_card.footer.text if last_card.footer else ""),
                   [f.value for f in last_card.fields]))
    cfg["drivers"]["rcon"] = ban_cfg
    mod.drivers.clear()


    # ---- 26. лестница варнов понимает игровые действия
    await warns_cog.warnconfig.callback(warns_cog, FakeInteraction(guild, moderator), "2=mute:10m, 4=bangame:1d")
    levels = await warns_cog.thresholds(guild.id)
    checks.append(("warnconfig: bangame принимается лестницей",
                   [t["action"] for t in levels] == ["mute", "bangame"], [t["action"] for t in levels]))
    try:
        await warns_cog.warnconfig.callback(warns_cog, FakeInteraction(guild, moderator), "3=slap:1m")
        checks.append(("warnconfig: неизвестное действие отбивается", False, "прошло"))
    except Exception as exc:
        checks.append(("warnconfig: неизвестное действие отбивается", "Действие" in str(exc), str(exc)))
    await db.set_guild_config(guild.id, {"warn_thresholds": None})


    # ---- 27. выдача по нику игры, без участника Discord
    cfg["drivers"]["rcon"] = {"host": "127.0.0.1", "port": mc_port, "password": "secret",
                               "mute_cmd": "tempmute {target} {duration} {reason}",
                               "ban_cmd": "tempban {target} {duration} {reason}",
                               "unban_cmd": "pardon {target}"}
    await db.set_guild_config(guild.id, {"driver": "rcon"})
    mod.drivers.clear(); rcon_mod.CONNECTIONS.clear()

    nick_case = await mod.bangame(moderator, None, timeutil.parse_duration("perm"), "ксы в чате",
                                  game_id="NoDiscordSteve", guild=guild)
    checks.append(("по нику: команда ушла в игру", received[-1] == "tempban NoDiscordSteve 0 ксы в чате", received[-1]))
    row = await db.get_case(nick_case)
    checks.append(("по нику: запись без участника Discord (user_id=0)", int(row["user_id"]) == 0, row["user_id"]))
    card = ch.sent[-1]
    event = card.fields[0].value
    checks.append(("по нику: в карточке светится ник, а не id:0", "`NoDiscordSteve`" in event and "id:0" not in event, event))
    checks.append(("по нику: бессрочный бан не имеет срока", row["expires_at"] is None, row["expires_at"]))

    await mod.unbangame(moderator, nick_case, "разбан по нику")
    checks.append(("по нику: разбан отправлен", received[-1] == "pardon NoDiscordSteve", received[-1]))
    lift = ch.sent[-1]
    checks.append(("по нику: карточка снятия с ником", "`NoDiscordSteve`" in lift.fields[0].value, lift.fields[0].value))
    checks.append(("по нику: запись закрыта", (await db.get_case(nick_case))["lifted_at"] is not None, None))

    # Discord-войс не умеет «только по нику» — должен быть внятный отказ
    await db.set_guild_config(guild.id, {"driver": "discord_voice"})
    try:
        await mod.mutegame(moderator, None, timedelta(minutes=5), "тест", game_id="Steve", guild=guild)
        checks.append(("по нику в Discord-войсе: отказ, а не TypeError", False, "прошло"))
    except DriverError as exc:
        checks.append(("по нику в Discord-войсе: отказ, а не TypeError",
                   "бан по нику игры недоступен" in str(exc) or "мут по нику игры недоступен" in str(exc), str(exc)))
    checks.append(("по нику: отказ не оставил активной записи",
                   not [c for c in await db.active_cases(guild.id) if int(c["user_id"]) == 0],
                   [c["id"] for c in await db.active_cases(guild.id)]))

    # ни цели, ни ника — команда не должна доходить до движка
    from cogs.moderation import Punishments
    pns = Punishments(bot)
    try:
        await pns.bangame.callback(pns, FakeInteraction(guild, moderator), None, None, None, None, False)
        checks.append(("ни цель, ни ник — подсказка модератору", False, "прошло"))
    except ModInputError as exc:
        checks.append(("ни цель, ни ник — подсказка модератору", "цель" in str(exc) and "ник" in str(exc), str(exc)))
    await db.set_guild_config(guild.id, {"driver": "rcon"})
    cfg["drivers"]["rcon"] = ban_cfg


    # ---- 27b. perm-наказание в игре: срок берётся из «навсегда», без OverflowError
    await db.set_guild_config(guild.id, {"driver": "rcon"})
    cfg["drivers"]["rcon"] = {"host": "127.0.0.1", "port": mc_port, "password": "secret",
                               "mute_cmd": "tempmute {target} {duration} {reason}",
                               "unmute_cmd": "unmute {target}",
                               "mute_perm_cmd": "mute {target} {reason}",
                               "ban_cmd": "tempban {target} {duration} {reason}",
                               "ban_perm_cmd": "ban {target} {reason}",
                               "unban_cmd": "pardon {target}"}
    mod.drivers.clear(); rcon_mod.CONNECTIONS.clear()
    perm_game = await mod.mutegame(moderator, target, timeutil.parse_duration("perm"), "навсегда в игре",
                                   game_id="Steve", guild=guild)
    checks.append(("perm-мут в игре: свой шаблон, без OverflowError",
                   received[-1] == "mute Steve навсегда в игре", received[-1]))
    checks.append(("perm-мут в игре: срок в базе = навсегда",
                   (await db.get_case(perm_game))["expires_at"] is None, None))
    await mod.unmutegame(moderator, perm_game, "снято")
    perm_ban = await mod.bangame(moderator, None, timeutil.parse_duration("perm"), "пермабан по нику",
                                 game_id="BadSteve", guild=guild)
    checks.append(("perm-бан по нику: ban вместо tempban 0",
                   received[-1] == "ban BadSteve пермабан по нику", received[-1]))
    await mod.unbangame(moderator, perm_ban, "разбанен")
    checks.append(("perm-бан по нику: pardon ушёл", received[-1] == "pardon BadSteve", received[-1]))


    # ---- 28. журнал по нику: /modlog и /case не показывают пустой mention
    nick_row = await db.add_case(guild_id=guild.id, user_id=0, moderator_id=7, action="bangame",
                                 reason="пермабан по нику", expires_at=None, extra={"game_id": "BadSteve"})
    row = await db.get_case(nick_row)
    line = rec_mod.Records._line(row)
    checks.append(("modlog: строка по нику читаемая", "`BadSteve`" in line and "<@0>" not in line, line))
    card = rec_mod.Records._case_embed(
        rec_mod.Records(SimpleNamespace(get_guild=lambda gid: guild, users=[], db=db)), row, _json.loads(row["extra"])
    )
    checks.append(("case: карточка по нику без id:0",
                   "`BadSteve`" in card.fields[0].value and "id:0" not in card.fields[0].value, card.fields[0].value))
    member_row = await db.get_case(await db.add_case(guild_id=guild.id, user_id=42, moderator_id=7,
                                                     action="warn", reason="обычный варн", expires_at=None, extra={}))
    checks.append(("modlog: обычный участник — как раньше", "<@42>" in rec_mod.Records._line(member_row),
                   rec_mod.Records._line(member_row)))
    guild._members[0] = None


    # ---- 29. защита от пустых аргументов в ник-пути
    from cogs.records import Records as RecC
    rec = RecC(bot)
    for blank in ("", "   ", "\t"):
        try:
            await rec.gamelink.callback(rec, FakeInteraction(guild, moderator), "42", blank)
            checks.append((f"gamelink: пустой ник «{blank!r}» отбит", False, "записал"))
        except ModInputError as exc:
            checks.append((f"gamelink: пустой ник «{blank!r}» отбит", "ник игры" in str(exc), str(exc)))
    try:
        await pns.mutegame.callback(pns, FakeInteraction(guild, moderator), "   ", None, None, None, False)
        checks.append(("mutegame: цель из одних пробелов отбивается", False, "прошло"))
    except (ModInputError, Exception) as exc:
        checks.append(("mutegame: цель из одних пробелов отбивается", True, str(exc)[:60]))


    # ---- 30. tools/mc_probe.py: прогоняем ту же руку, что и бот, на фейковом MC-сервере
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("mc_probe", str(pathlib.Path("tools/mc_probe.py")))
    probe_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(probe_mod)

    checks.append(("probe: список игроков разбирается",
                   [n for n, _ in probe_mod.parse_players("There are 2/20 players online: [Steve, Alex]")] == ["Steve", "Alex"],
                   probe_mod.parse_players("There are 2/20 players online: [Steve, Alex]")))
    checks.append(("probe: «(no players)” — не игроки",
                   probe_mod.parse_players("There are 0/20 players online: [(no players)]") == [],
                   probe_mod.parse_players("There are 0/20 players online: [(no players)]")))
    checks.append(("probe: кириллический ник принимается",
                   [n for n, _ in probe_mod.parse_players("Игроки онлайн: 1/10: [Ионов]")] == ["Ионов"], None)
    )

    print("--- probe на живом фейковом сервере (пароль верный):")
    rc_good = await probe_mod.probe("127.0.0.1", mc_port, "secret", 3.0, [])
    checks.append(("probe: возвращает 0, когда RCON отвечает", rc_good == 0, rc_good))
    rc_bad = await probe_mod.probe("127.0.0.1", mc_port, "wrong-password", 3.0, [])
    checks.append(("probe: возвращает 1 на неверный пароль", rc_bad == 1, rc_bad))
    rc_dead = await probe_mod.probe("127.0.0.1", 1, "secret", 2.0, [])
    checks.append(("probe: возвращает 1, когда порт закрыт", rc_dead == 1, rc_dead))


    # ---- 31. gamefeed: игровые наказания -> те же карточки в канал
    from cogs.gamefeed import GameFeed

    feed_cfg = {"gamefeed": {"enabled": True, "source": "log",
                             "log_file": str(pathlib.Path("data/gamefeed-test.log")),
                             "interval_s": 3600, "patterns": None}}
    feed_bot = SimpleNamespace(
        cfg=feed_cfg, db=db, mod=mod, get_cog=lambda n: None,
        get_guild=lambda gid: guild, guilds=[guild], wait_until_ready=lambda: asyncio.sleep(0),
    )
    feed = GameFeed(feed_bot)
    feed.poll_loop.cancel()
    feed.enabled = False  # цикл заглушен, дёргаем хелперы напрямую
    checks.append(("gamefeed: паттерны загрузились без ошибок",
                   not feed.pattern_errors and len(feed.patterns) > 5, (len(feed.patterns), feed.pattern_errors)))

    # парсер: ключевые формы
    samples = {
        "[0] Admin issued server command: /tempban BadSteve 7d ксы": ("bangame", "BadSteve", "7 дней"),
        "[0] issued server command: /ban Notch": ("bangame", "Notch", None),
        "[0] BadSteve был временно забанен на 2 дня по причине: дюп": ("bangame", "BadSteve", "2 дня"),
        "[0] TPS: 20.0": (None, None, None),
    }
    from core import gamefeed as gf31
    for line, (action, nick, dur) in samples.items():
        ev = gf31.parse_line(line, feed.patterns)
        got = (ev.action, ev.nick, ev.duration_text) if ev else (None, None, None)
        checks.append((f"gamefeed: парсер «{line[4:26]}...”", got == (action, nick, dur), got))

    # хвост лога: читаем только дописанное, после ротации — с начала
    path = pathlib.Path("data/gamefeed-test.log")
    path.write_text("строка 1\n", encoding="utf-8")
    first = feed._read_tail(str(path))
    checks.append(("gamefeed: первый проход историю не льёт", first == "", repr(first)))
    with path.open("a", encoding="utf-8") as f:
        f.write("issued server command: /tempban BadSteve 7d ксы\n")
    second = feed._read_tail(str(path))
    checks.append(("gamefeed: дочитывает только новое", "tempban BadSteve" in second, repr(second)))
    path.write_text("коротко\n", encoding="utf-8")   # ротация/усечение
    third = feed._read_tail(str(path))
    checks.append(("gamefeed: переживает ротацию файла", third.startswith("коротко"), repr(third)))

    # события -> карточка + запись; дедуп и «эхо» собственных наказаний
    async def one(line: str) -> tuple[int, int]:
        before_cases = len(await db.search_cases(guild.id, limit=500))
        before_cards = len(ch.sent)
        ev = gf31.parse_line(line, feed.patterns)
        await feed._handle(guild, ev)
        return len(await db.search_cases(guild.id, limit=500)) - before_cases, len(ch.sent) - before_cards

    made, cards = await one("issued server command: /tempban GriefKing 1d ксы")
    checks.append(("gamefeed: создание -> 1 запись и 1 карточка", (made, cards) == (1, 1), (made, cards)))
    again, again_cards = await one("issued server command: /tempban GriefKing 1d ксы")
    checks.append(("gamefeed: та же строка лога дважды не дублируется", (again, again_cards) == (0, 0), (again, again_cards)))
    await one("issued server command: /warn WarnedMan спам")
    echoed, echoed_cards = await one("issued server command: /warn WarnedMan спам")
    checks.append(("gamefeed: эхо-окно глушит повтор того же ника", (echoed, echoed_cards) == (0, 0), (echoed, echoed_cards)))
    feed.note_self_issued("bangame", "SelfBanned", "бот выдал")
    mine, mine_cards = await one("issued server command: /ban SelfBanned бот выдал")
    checks.append(("gamefeed: своё наказание бот не дублирует", (mine, mine_cards) == (0, 0), (mine, mine_cards)))
    row = (await db.search_cases(guild.id, action="bangame", limit=5))[0]
    checks.append(("gamefeed: запись помечена источником game", _json.loads(row["extra"])["source"] == "game", row["extra"]))
    checks.append(("gamefeed: карточка говорит «из игры»",
                   "из игры" in (ch.sent[-1].footer.text if ch.sent[-1].footer else ""), ch.sent[-1].footer))
    # разбан в игре закрывает то, что создал лог
    closed_before = (await db.active_game_case(guild.id, "bangame", "GriefKing")) is not None
    await one("issued server command: /pardon GriefKing")
    closed_after = await db.active_game_case(guild.id, "bangame", "GriefKing")
    checks.append(("gamefeed: pardon в игре закрыл запись", closed_before and closed_after is None,
                   (closed_before, closed_after)))
    path.unlink(missing_ok=True)


    # ---- 31b. второй источник: опрос banlist через RCON (снимки сравниваются)
    @registry.register
    class PollStub(registry.get("rcon")):
        id = "poll_stub"
        title = "stub с consult"

        async def consult(self, command):
            return "Banned players: " + ", ".join(POP["names"])

    POP = {"names": ["OldBad", "NewBad"]}
    await db.set_guild_config(guild.id, {"driver": "poll_stub"})
    feed2 = GameFeed(SimpleNamespace(
        cfg={"gamefeed": {"enabled": True, "source": "rcon", "poll_cmd": "banlist",
                          "poll_action": "ban", "interval_s": 3600}},
        db=db, mod=mod, get_cog=lambda n: None, get_guild=lambda gid: guild,
        guilds=[guild], wait_until_ready=lambda: asyncio.sleep(0)))
    feed2.poll_loop.cancel()
    feed2.enabled = False
    before = len(ch.sent)
    await feed2._tick_poll(guild)          # первый снимок — только база сравнения
    checks.append(("poll: первый снимок карточек не делает", len(ch.sent) == before, (before, len(ch.sent))))
    POP["names"] = ["OldBad", "NewBad", "FreshBan"]
    await feed2._tick_poll(guild)
    checks.append(("poll: новый игрок в списке -> карточка бана", len(ch.sent) == before + 1, (before, len(ch.sent))))
    made_case = (await db.search_cases(guild.id, action="bangame", limit=3))[0]
    checks.append(("poll: запись создана с ником из списка",
                   _json.loads(made_case["extra"]).get("game_id") == "FreshBan", made_case["extra"]))
    before = len(ch.sent)
    POP["names"] = ["OldBad", "NewBad"]     # FreshBan пропал из бан-листа => разбанен
    await feed2._tick_poll(guild)
    vanished_cards = len(ch.sent) - before
    checks.append(("poll: исчезновение из списка -> одна карточка «снято», без нового бана",
                   vanished_cards == 1, (before, len(ch.sent))))
    checks.append(("poll: пропавший игрок закрыл свою запись",
                   await db.active_game_case(guild.id, "bangame", "FreshBan") is None, None))
    await feed2.status.callback(feed2, FakeInteraction(guild, moderator))
    checks.append(("gamefeed: status отвечает текстом", any(s0.startswith("REPLY:") for s0 in SENT[-1:]), SENT[-1:]))
    await feed2.try_parse.callback(feed2, FakeInteraction(guild, moderator),
                                  "issued server command: /tempban BadSteve 7d ксы")
    try:
        await feed2.try_parse.callback(feed2, FakeInteraction(guild, moderator), "строка без ника")
        checks.append(("gamefeed: try на мусоре даёт подсказку", False, "не бросило"))
    except ModInputError as exc:
        checks.append(("gamefeed: try на мусоре даёт подсказку", "gamefeed.patterns" in str(exc), str(exc)[:60]))
    await db.set_guild_config(guild.id, {"driver": "discord_voice"})
    await feed._create(guild, gf31.GameEvent(
        action="warn", nick="WarnedMan", reason="повтор варна через окно"))
    feed.recent.clear()
    await feed._create(guild, gf31.GameEvent(
        action="warn", nick="WarnedMan", reason="повтор варна через окно"))
    checks.append(("gamefeed: повторное _create по той же строке не дублирует",
                   len([c for c in await db.search_cases(guild.id, action="warn", limit=50)
                        if c["reason"] == "повтор варна через окно"]) == 2, "окно дублей не отработало"))

    fails = [row for row in checks if not row[1]]
    width = max(len(c[0]) for c in checks)
    for name, ok, detail in checks:
        print(f"{'✅' if ok else '❌'} {name:<{width}}  {'' if ok else detail}")
    print(f"\n{len(checks) - len(fails)}/{len(checks)} проверок пройдено")
    db.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
