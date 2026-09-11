"""The engine every command calls.

`Moderation` owns the *how* of a punishment:
  * Discord timeout  -> text + voice blocked everywhere in one shot
  * deny overwrites  -> belt and braces for clients that ignore timeouts
  * game driver      -> in-game silence (plugin, see drivers/)
It also records the case, posts it to the audit channel, DMs the target and
undoes everything on /unmute, auto-expiry or restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import timedelta

import discord

from core import audit, timeutil
from drivers.base import DriverError, BaseDriver, Registry

log = logging.getLogger("modbot")


def game_seconds(delta) -> int:
    """Срок для игрового драйвера: 0 = навсегда.

    `int(timedelta.max.total_seconds())` — это ~8.6e13, от чего `timedelta(...)`
    падает с OverflowError ещё до отправки команды.
    """
    return 0 if timeutil.is_permanent(delta) else int(delta.total_seconds())


class GameTarget:
    """Игрок, о котором известен только ник в игре (в Discord его нет/не привязан).

    Нужен, чтобы карточка журнала оставалась читаемой: `name_of` вернёт ник,
    упоминания не будет, `id` = 0 — по нему такие записи и отличают.
    """

    def __init__(self, nick: str):
        self.display_name = str(nick)
        self.id = 0

VOICE_KEYS = ("connect", "speak", "use_voice_activation")
TEXT_KEYS = ("send_messages", "add_reactions", "create_public_threads")


class Moderation:
    def __init__(self, bot, cfg) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = bot.db
        self.registry: Registry = bot.registry
        self.drivers: dict[str, BaseDriver] = {}

    # ------------------------------------------------------------- resources
    async def effective(self, guild_id: int) -> dict:
        """Guild settings layered over config.json defaults (empty = inherit)."""
        cfg = await self.db.get_guild_config(guild_id)
        merged = {
            "feedback_hint": (cfg.get("feedback_hint") or self.cfg.get("feedback_hint") or audit.HINT),
            "log_channel_id": int(cfg.get("log_channel_id") or self.cfg.get("log_channel_id") or 0),
            "mute_role_id": int(cfg.get("mute_role_id") or self.cfg.get("mute_role_id") or 0),
            "dm_target": cfg.get("dm_target") if cfg.get("dm_target") is not None else bool(self.cfg.get("dm_target", True)),
            # None = inherit config.json; an explicit per-guild value wins both ways
            "dry_run": bool(self.cfg.get("dry_run")) if cfg.get("dry_run") is None else bool(cfg["dry_run"]),
            "driver": (cfg.get("driver") or self.cfg.get("drivers", {}).get("active") or "discord_voice"),
            "warn_thresholds": cfg.get("warn_thresholds") if cfg.get("warn_thresholds") is not None else self.cfg.get("warn_thresholds") or [],
        }
        return merged

    async def resolve_driver(self, guild_id: int) -> tuple[str, BaseDriver]:
        cfg = await self.effective(guild_id)
        driver_id = cfg["driver"]
        cls = self.registry.get(driver_id)
        if cls is None:
            raise DriverError(
                f"драйвер «{driver_id}» не найден. Доступны: {', '.join(self.registry.ids())}"
            )
        if driver_id not in self.drivers:
            sub = dict(self.cfg.get("drivers", {})).get(driver_id) or {}
            self.drivers[driver_id] = cls(self.db, sub)
            await self.drivers[driver_id].setup()
        return driver_id, self.drivers[driver_id]

    async def driver_by_id(self, driver_id: str) -> BaseDriver | None:
        """Instantiate a driver by id, even if no command used it yet.

        Re-arming game mutes after a restart runs before any /mutegame, so the
        cache is still empty — resolving only via a guild would skip every case.
        """
        cls = self.registry.get(driver_id)
        if cls is None:
            return None
        if driver_id not in self.drivers:
            sub = dict(self.cfg.get("drivers", {})).get(driver_id) or {}
            self.drivers[driver_id] = cls(self.db, sub)
            await self.drivers[driver_id].setup()
        return self.drivers[driver_id]

    async def dry_run(self, guild_id: int) -> bool:
        return (await self.effective(guild_id))["dry_run"]

    async def log_channel(self, guild: discord.Guild):
        """Канал отчётов, или None если он не задан/недоступен."""
        cfg = await self.effective(guild.id)
        channel_id = cfg["log_channel_id"]
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if getattr(channel, "send", None) else None


    async def post(self, guild: discord.Guild, embed: discord.Embed, *, silent: bool = False) -> None:
        if silent:
            return
        channel = await self.log_channel(guild)
        if channel is None:
            log.warning("Канал логов не задан — кейс %s не опубликован", embed.title)
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Не могу писать в канал логов %s (нет прав)", getattr(channel, "id", "?"))

    async def notify(self, member: discord.Member, embed: discord.Embed) -> None:
        if not (await self.effective(member.guild.id))["dm_target"]:
            return
        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            log.debug("DM закрыт для %s", member)
        except Exception:  # noqa: BLE001 - never fail a punishment over a DM
            log.exception("DM не отправлен")

    # ------------------------------------------------------------- mute / ds
    async def mute(
        self,
        moderator: discord.abc.User | None,
        member: discord.Member,
        delta: timedelta,
        reason: str,
        *,
        silent: bool = False,
        source: str = "manual",
        parent_case: int | None = None,
    ) -> int:
        guild = member.guild
        cfg_eff = await self.effective(guild.id)
        expires_at = timeutil.expires_at(delta)
        role = guild.get_role(cfg_eff["mute_role_id"]) if cfg_eff["mute_role_id"] else None
        # A Discord timeout cannot be forever (max 356 days), so "perm" has to go
        # through the role/permission path or it would silently not be enforced.
        if expires_at is None and role is None and not cfg_eff["dry_run"]:
            raise ValueError(
                "Discord не умеет мут навсегда таймаутом. Задайте роль мута "
                "(/modsettings роль_мута:@Muted) или используйте срок до 356 дней."
            )
        extra = {"source": source, "parent_case": parent_case, "seconds": game_seconds(delta)}
        case_id = await self.db.add_case(
            guild_id=member.guild.id,
            user_id=member.id,
            moderator_id=moderator.id if moderator else None,
            action="mute",
            reason=reason,
            expires_at=expires_at,
            extra=extra,
        )
        notes: list[str] = []
        hint = cfg_eff["feedback_hint"]
        if cfg_eff["dry_run"]:
            notes.append("dry_run: изменения не применены")
            await self._publish("mute", case_id, member, moderator, reason, delta, expires_at, " / ".join(notes))
            return case_id

        # 1. Discord native timeout: text + voice blocked in one shot, survives
        #    reconnects and cannot be removed by the punished member.
        fallback_reason = None
        if expires_at is None:
            fallback_reason = "бессрочный мут — таймаум не подходит"
        else:
            try:
                await member.edit(timed_out_until=expires_at, reason=f"[mute #{case_id}] {reason}")
                notes.append("timeout (текст + голос)")
            except discord.Forbidden:
                fallback_reason = "нет прав Modern Timeout"
            except discord.HTTPException as exc:
                fallback_reason = f"timeout отклонён ({exc})"
        if fallback_reason:
            notes.append(f"⚠️ {fallback_reason} — перехожу на разрешения/роль")

        # 2. Fallback: deny overwrites + optional mute role.
        deny_needed = fallback_reason is not None
        if deny_needed:
            rows = await self._apply_overwrites(member, case_id, reason)
            notes.append(f"каналы с deny: {rows}")
        if role:
            if role >= member.top_role:
                notes.append("⚠️ роль мута выше роли цели — поднимите бота в иерархии")
            else:
                try:
                    await member.add_roles(role, reason=f"[mute #{case_id}] {reason}")
                    notes.append(f"роль «{role.name}»")
                except discord.Forbidden:
                    notes.append("⚠️ не выдал роль мута")
        await self._publish("mute", case_id, member, moderator, reason, delta, expires_at, " / ".join(notes), silent=silent)
        await self.notify(
            member,
            audit.punishment_embed(
                "mute", timeutil.humanize_long(delta), reason,
                guild_name=guild.name, target=member, hint=hint,
            ),
        )
        return case_id

    async def unmute(self, moderator, member, reason, *, case_id: int | None = None, source="manual") -> int | None:
        guild = member.guild
        target_case = None
        if case_id:
            target_case = await self.db.get_case(case_id)
            if target_case is None or target_case["guild_id"] != guild.id:
                raise ValueError("Такой записи на этом сервере нет")
            if target_case["action"] not in ("mute", "mutegame"):
                raise ValueError(f"Запись №{case_id} — это «{target_case['action']}», снимается /unban")
            if target_case["lifted_at"]:
                raise ValueError(f"Запись №{case_id} уже снята")
        else:
            active = [
                c for c in await self.db.active_cases(guild.id, "mute") if c["user_id"] == member.id
            ]
            if not active:
                active_game = [
                    c for c in await self.db.active_cases(guild.id, "mutegame") if c["user_id"] == member.id
                ]
                if not active_game:
                    raise ValueError(f"У {member} нет активных мутов")
                target_case = active_game[-1]
            else:
                target_case = active[-1]

        case_id = int(target_case["id"])
        hint = (await self.effective(guild.id))["feedback_hint"]
        notes = await self.undo(target_case, member, reason)
        await self.db.lift(case_id, moderator.id if moderator else None)
        action = "unmutegame" if target_case["action"] == "mutegame" else "unmute"
        embed = audit.case_embed(
            action,
            target=member,
            moderator=moderator,
            reason=f"{reason} | было: {target_case['reason']}",
            note=" / ".join(notes) or None,
            hint=hint,
        )
        await self.post(guild, embed)  # авто-истечение тоже должно быть видно
        return case_id

    async def undo(self, case, member: discord.Member, reason: str) -> list[str]:
        """Remove every Discord-side trace of a mute/mutegame case."""
        if await self.dry_run(member.guild.id):
            return ["dry_run: ничего не снято"]
        guild = member.guild
        notes: list[str] = []
        with contextlib.suppress(discord.HTTPException):
            # timeout may already be gone (expired natively) — that is fine
            await member.edit(timed_out_until=None, reason=f"[unmute #{case['id']}] {reason}")
        if member.voice and (member.voice.deaf or member.voice.mute):
            try:
                await member.edit(deafen=False, mute=False, reason=f"[unmute #{case['id']}]")
                notes.append("voice снят")
            except discord.Forbidden:
                notes.append("⚠️ не снял voice-state (нет прав)")
        await self._restore_overwrites(int(case["id"]), member, notes)
        role_id = (await self.effective(guild.id))["mute_role_id"]
        role = guild.get_role(role_id) if role_id else None
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"[unmute #{case['id']}] {reason}")
                notes.append(f"роль «{role.name}» снята")
            except discord.Forbidden:
                notes.append("⚠️ не снял роль мута")
        return notes

    # ------------------------------------------------------------- game mute
    def _note_game_event(self, action: str, member, nick: str | None, reason: str) -> None:
        """Сказать gamefeed'у, что это наказание отправил бот (окно эха)."""
        feed = self.bot.get_cog("GameFeed")
        nick = nick or getattr(member, "display_name", None)
        if feed is not None and nick:
            feed.note_self_issued(action, str(nick), reason)

    async def mutegame(
        self, moderator, member, delta, reason, *, game_id: str | None,
        guild=None, silent=False, source="manual",
    ) -> int:
        guild = member.guild if member is not None else guild
        if guild is None:
            raise ValueError("нужен сервер: укажите участника Discord или сервер")
        driver_id, driver = await self.resolve_driver(guild.id)
        if member is None and not driver.by_nick_only:
            raise DriverError(
                f"{driver.title}: мут по нику игры недоступен — отметьте участника Discord"
            )
        expires_at = timeutil.expires_at(delta)
        extra = {
            "source": source,
            "driver": driver_id,
            "seconds": game_seconds(delta),
            "game_id": game_id,
        }
        target = game_id or (await self.db.get_game_link(guild.id, member.id, driver_id) if member else None)
        if member is None and not target:
            raise DriverError("укажите ник игры (`ник:`) — без него и без привязки не кого наказывать")
        case_id = await self.db.add_case(
            guild_id=guild.id,
            user_id=member.id if member else 0,
            moderator_id=moderator.id if moderator else None,
            action="mutegame",
            reason=reason,
            expires_at=expires_at,
            extra=extra,
        )
        subject = member or GameTarget(str(target))
        if await self.dry_run(guild.id):
            await self._publish("mutegame", case_id, subject, moderator, reason, delta, expires_at,
                                f"dry_run · драйвер {driver_id}", silent=silent, guild=guild)
            return case_id
        try:
            resolved = await driver.resolve(guild, member, target)
            extra["game_id"] = resolved
            await self.db.update_case(case_id, extra=json.dumps(extra, ensure_ascii=False))
            if resolved and member is not None and driver_id != "discord_voice":
                await self.db.set_game_link(guild.id, member.id, driver_id, resolved)
            note = await driver.mute(guild, resolved, game_seconds(delta), reason)
        except DriverError as exc:
            await self._fail_game_case(case_id, subject, reason, exc, guild=guild)
            raise
        await self._publish("mutegame", case_id, subject, moderator, reason, delta, expires_at,
                            f"{driver.title}: {note}", silent=silent, guild=guild)
        if member is not None:  # наказанный по нику игры — писать в ЛС некому
            await self.notify(
                member,
                audit.punishment_embed(
                    "mutegame", timeutil.humanize_long(delta), reason,
                    guild_name=guild.name, target=member,
                    hint=(await self.effective(guild.id))["feedback_hint"],
                ),
            )
        return case_id

    async def unmutegame(self, moderator, case_id: int, reason: str) -> None:
        """Снять мут в игре."""
        await self._lift_game(case_id, moderator, reason, want=("mutegame",), verb="unmutegame")

    async def unbangame(self, moderator, case_id: int, reason: str) -> None:
        """Разбанить на игровом сервере."""
        await self._lift_game(case_id, moderator, reason, want=("bangame",), verb="unbangame")

    async def _lift_game(self, case_id: int, moderator, reason: str, *, want: tuple[str, ...], verb: str) -> None:
        case = await self.db.get_case(case_id)
        if case is None:
            raise ValueError("Такой записи нет")
        if case["action"] not in want:
            raise ValueError(f"Запись №{case_id} — это «{case['action']}», а не {'/'.join(want)}")
        if case["lifted_at"]:
            raise ValueError(f"Запись №{case_id} уже снята")
        guild = self.bot.get_guild(case["guild_id"])
        if guild is None:
            raise ValueError("Сервер недоступен")
        member = guild.get_member(int(case["user_id"]))
        extra = json.loads(case["extra"] or "{}")
        if member is None:  # выдача по нику игры: в карточке снятия светится ник
            member = GameTarget(str(extra.get("game_id") or case["user_id"]))
        # Lift with the driver that applied it: an admin who switched drivers in
        # between would otherwise feed a SteamID to the Discord voice driver and
        # unmute nothing (or crash on int()).
        recorded = str(extra.get("driver") or "")
        # скобки обязательны: `await x()[1]` — это срез корутины, а не её результата
        driver = await self.driver_by_id(recorded) or (await self.resolve_driver(guild.id))[1]
        driver_id = recorded or driver.id
        # id из кейса принадлежит тому драйверу, который его записал; если сервер
        # сменили, берём привязку уже нового драйвера (/gamelink), а не чужой id
        target = (extra.get("game_id") if recorded == driver.id else None) or await self.db.get_game_link(
            guild.id, case["user_id"], driver_id
        )
        if not target:
            raise ValueError(
                f"для драйвера `{driver_id}` нет игрового ID игрока — "
                "привяжите через /gamelink или снимите мут на стороне игры"
            )
        note = await (driver.unmute if verb == "unmutegame" else driver.unban)(guild, str(target))
        await self.db.lift(case_id, moderator.id if moderator else None)
        embed = audit.case_embed(
            verb, target=member, moderator=moderator,
            reason=f"{reason} | было: {case['reason']}", note=f"{driver.title}: {note}",
        )
        await self.post(guild, embed)  # как и с mute: срок истёк — запись в журнал

    async def _fail_game_case(self, case_id: int, subject, reason: str, exc: Exception, *, guild) -> None:
        """Драйвер не смог — запись закрываем сразу, иначе она «висит активной».

        Иначе фоновый цикл каждую минуту пытался бы снять наказание, которого в
        игре нет, а модератор видел бы в /modlog несуществующий мут/бан.
        """
        note = f"не применено: {exc}"
        await self.db.lift(case_id, None)
        try:
            embed = audit.case_embed(
                "auto", target=subject, moderator=None, reason=f"{reason} · {note}", note=note
            )
            await self.post(guild, embed)
        except Exception:  # noqa: BLE001 — журнал не важнее ошибки для модератора
            log.exception("не смог опубликовать отмену кейса #%s", case_id)

    async def notify_game(self, member: discord.Member, text: str) -> str | None:
        """Доставить уведомление в игру, если драйвер это умеет и есть привязка.

        Намеренно не бросает: варн не должен падать из-за офلاینвого игрового
        сервера — вместо этого карточка получит пояснение.
        """
        try:
            driver_id, driver = await self.resolve_driver(member.guild.id)
        except DriverError as exc:
            return f"драйвер не готов: {exc}"
        game_id = await self.db.get_game_link(member.guild.id, member.id, driver_id)
        if not game_id:
            return None  # не с чем работать: в игре молчим
        try:
            return await driver.notify(member.guild, str(game_id), text)
        except DriverError as exc:
            return f"в игру не дошло: {exc}"

    async def bangame(
        self, moderator, member, delta, reason, *, game_id: str | None,
        guild=None, silent=False, source="manual",
    ) -> int:
        """Бан на игровом сервере (Discord-бан — это /ban)."""
        guild = member.guild if member is not None else guild
        if guild is None:
            raise ValueError("нужен сервер: укажите участника Discord или сервер")
        driver_id, driver = await self.resolve_driver(guild.id)
        if member is None and not driver.by_nick_only:
            raise DriverError(
                f"{driver.title}: бан по нику игры недоступен — отметьте участника Discord"
            )
        expires_at = timeutil.expires_at(delta)
        extra = {"source": source, "driver": driver_id, "seconds": game_seconds(delta), "game_id": game_id}
        target = game_id or (await self.db.get_game_link(guild.id, member.id, driver_id) if member else None)
        if member is None and not target:
            raise DriverError("укажите ник игры (`ник:`) — без него и без привязки некого банить")
        case_id = await self.db.add_case(
            guild_id=guild.id, user_id=member.id if member else 0,
            moderator_id=moderator.id if moderator else None,
            action="bangame", reason=reason, expires_at=expires_at, extra=extra,
        )
        subject = member or GameTarget(str(target))
        if await self.dry_run(guild.id):
            await self._publish("bangame", case_id, subject, moderator, reason, delta, expires_at,
                                f"dry_run · драйвер {driver_id}", silent=silent, guild=guild)
            return case_id
        try:
            resolved = await driver.resolve(guild, member, target)
            extra["game_id"] = resolved
            await self.db.update_case(case_id, extra=json.dumps(extra, ensure_ascii=False))
            if resolved and member is not None:
                await self.db.set_game_link(guild.id, member.id, driver_id, resolved)
            note = await driver.ban(guild, resolved, game_seconds(delta), reason)
        except DriverError as exc:
            await self._fail_game_case(case_id, subject, reason, exc, guild=guild)
            raise
        await self._publish("bangame", case_id, subject, moderator, reason, delta, expires_at,
                            f"{driver.title}: {note}", silent=silent, guild=guild)
        if member is not None:  # наказанный по нику игры — писать в ЛС некому
            await self.notify(member, audit.punishment_embed(
                "bangame", timeutil.humanize_long(delta), reason, guild_name=guild.name, target=member,
                hint=(await self.effective(guild.id))["feedback_hint"]))
        return case_id

    # ------------------------------------------------------------- ban
    async def ban(self, moderator, member, delta, reason) -> int:
        guild = member.guild if isinstance(member, discord.Member) else self.bot.get_guild(member.guild_id)
        expires_at = timeutil.expires_at(delta)
        case_id = await self.db.add_case(
            guild_id=guild.id,
            user_id=member.id,
            moderator_id=moderator.id if moderator else None,
            action="ban",
            reason=reason,
            expires_at=expires_at,
            extra={"seconds": int(delta.total_seconds()) if expires_at else None},
        )
        if await self.dry_run(guild.id):
            await self._publish("ban", case_id, member, moderator, reason, delta, expires_at, "dry_run")
            return case_id
        await guild.ban(member, reason=f"[ban #{case_id}] {reason}")
        note = "забанен" + ("" if expires_at else " навсегда")
        await self._publish("ban", case_id, member, moderator, reason, delta, expires_at, note)
        return case_id

    async def unban(self, moderator, user: discord.abc.User | int, reason: str) -> int | None:
        guild = moderator.guild
        user_id = user if isinstance(user, int) else user.id
        bans = await guild.bans()
        entry = next((b for b in bans if b.user.id == user_id), None)
        if entry is None:
            raise ValueError("Пользователь не в бане")
        await guild.unban(entry.user, reason=f"[unban] {reason}")
        case = next(
            (c for c in await self.db.active_cases(guild.id, "ban") if c["user_id"] == user_id), None
        )
        if case:
            await self.db.lift(case["id"], moderator.id)
        embed = audit.case_embed(
            "unban", target=entry.user, moderator=moderator,
            reason=reason, note=f"было: {entry.reason or '—'}",
        )
        await self.post(guild, embed)
        return int(case["id"]) if case else None

    # ------------------------------------------------------------- publish
    async def _publish(self, action, case_id, subject, moderator, reason, delta, expires_at, note,
                       *, silent=False, guild=None):
        # цель может быть GameTarget (ник без участника Discord) — сервер приходит аргументом
        target_guild = getattr(subject, "guild", None) or guild
        hint = (await self.effective(target_guild.id))["feedback_hint"]
        embed = audit.case_embed(
            action,
            target=subject,
            moderator=moderator,
            reason=reason,
            duration=timeutil.humanize_long(delta),
            note=note,
            hint=hint,
        )
        await self.post(target_guild, embed, silent=silent)

    # ------------------------------------------------------------- overwrites
    async def _apply_overwrites(self, member: discord.Member, case_id: int, reason: str) -> int:
        """Fallback when Modern Timeouts are unavailable: deny text + voice in every
        channel the member can see, and remember which channels were touched."""
        touched: list[int] = []
        for channel in member.guild.channels:
            if isinstance(channel, (discord.StageChannel, discord.ForumChannel)):
                continue
            # `.overwrites` holds only explicitly set entries, so its absence means
            # "inherit"; `overwrites_for()` cannot tell those apart from a deny.
            entry = channel.overwrites.get(member)
            if entry is not None and entry.send_messages is False:
                continue  # already denied here by hand or by another role path
            voice_channel = channel.type in (
                discord.ChannelType.voice,
                discord.ChannelType.stage_voice,
            )
            denial = {k: False for k in (VOICE_KEYS if voice_channel else TEXT_KEYS)}
            try:
                await channel.set_permissions(member, reason=f"[mute #{case_id}] {reason}", **denial)
            except (discord.Forbidden, discord.HTTPException):
                continue
            touched.append(int(channel.id))
        if touched:
            await self.db.save_overwrites(case_id, member.guild.id, touched)
        return len(touched)

    async def _restore_overwrites(
        self, case_id: int, member: discord.Member, notes: list[str]
    ) -> None:
        """Reset the overwrites created by `_apply_overwrites` back to inheritance."""
        rows = await self.db.take_overwrites(case_id)
        guild = member.guild
        restored = 0
        for row in rows:
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                continue
            try:
                await channel.set_permissions(member, reason=f"[unmute case #{case_id}]")
                restored += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await self.db.forget_overwrites(case_id)
        if restored:
            notes.append(f"сброшено разрешений: {restored} канал(ов)")
