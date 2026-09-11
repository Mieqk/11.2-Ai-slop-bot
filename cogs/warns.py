"""/warn and the warn ladder (3 warns -> mute, 5 -> 24h mute, 8 -> ban ...).

Thresholds come from config `warn_thresholds` or per-guild overrides via
/warnconfig. Escalation is evaluated on every new warn using the count of
active (not revoked, not expired) warns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from core import audit, timeutil
from core.helpers import ModInputError, find_case, guard, resolve_member
from core.moderation import Moderation as ModerationEngine


class Warnings(commands.Cog):
    """Предупреждения и авто-эскалация наказаний."""

    def __init__(self, bot: ModBot):
        self.bot = bot

    @property
    def mod(self) -> ModerationEngine:
        return self.bot.mod

    async def thresholds(self, guild_id: int) -> list[dict]:
        cfg = await self.bot.db.get_guild_config(guild_id)
        rows = cfg.get("warn_thresholds") or self.bot.cfg.get("warn_thresholds") or []
        out = []
        for row in rows:
            try:
                out.append(
                    {
                        "warns": int(row["warns"]),
                        "action": str(row["action"]).lower(),
                        "delta": timeutil.parse_duration(str(row.get("duration", "perm"))),
                    }
                )
            except (KeyError, timeutil.DurationError):
                continue
        return sorted(out, key=lambda r: r["warns"])

    # ------------------------------------------------------------------ warn
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="warn", description="Предупреждение с авто-эскалацией по порогам")
    @app_commands.rename(target="цель", reason="причина", days="срок_дней")
    @app_commands.describe(
        target="Участник: mention или ID",
        reason="Причина предупреждения",
        days="Через сколько дней варн сгорает (0 = бессрочно)",
    )
    async def warn(
        self,
        inter: discord.Interaction,
        target: str,
        reason: str,
        days: app_commands.Range[int, 0, 3650] = 0,
    ) -> None:
        member = await resolve_member(inter.guild, target)
        await guard(inter, member)
        expires_at = timeutil.expires_at(timedelta(days=days)) if days else None
        case_id = await self.bot.db.add_case(
            guild_id=inter.guild.id,
            user_id=member.id,
            moderator_id=inter.user.id,
            action="warn",
            reason=reason,
            expires_at=expires_at,
            extra={"source": "manual", "days": days},
        )
        active = await self.bot.db.active_warns(inter.guild.id, member.id)
        count = len(active)
        ladder = await self.thresholds(inter.guild.id)
        hit = next((t for t in ladder if t["warns"] == count), None)
        next_hit = next((t for t in ladder if t["warns"] > count), None)

        delivered = await self.mod.notify_game(
            member, f"[модерация] вам предупреждение №{count}: {reason}"
        )
        embed = audit.case_embed(
            "warn",
            target=member,
            moderator=inter.user,
            reason=reason,
            duration=timeutil.humanize_long(timedelta(days=days)) if days else "не ограничено",
            extra_fields={"ВАРНОВ": f"{count} активных"},
            note=_delivery_note(delivered),
        )
        await self.mod.post(inter.guild, embed)
        await self.mod.notify(
            member,
            audit.warning_embed(
                count, reason, inter.user,
                next_hit["warns"] if next_hit else None,
                guild_name=inter.guild.name, target=member, total_warns=count,
            ),
        )

        follow = ""
        if hit:
            try:
                if hit["action"] == "mute":
                    auto_case = await self.mod.mute(
                        None, member, hit["delta"],
                        f"Автонаказание: накопилось {count} варнов",
                        source="auto", parent_case=case_id,
                    )
                    follow = f"\n⏱ Авто-мут на {timeutil.humanize_long(hit['delta'])} (№{auto_case})."
                elif hit["action"] == "ban":
                    auto_case = await self.mod.ban(
                        None, member, hit["delta"],
                        f"Автонаказание: накопилось {count} варнов",
                    )
                    follow = f"\n🔨 Авто-бан на {timeutil.humanize_long(hit['delta'])} (№{auto_case})."
                elif hit["action"] in ("mutegame", "bangame"):
                    lift = self.mod.mutegame if hit["action"] == "mutegame" else self.mod.bangame
                    auto_case = await lift(
                        None, member, hit["delta"],
                        f"Автонаказание: {count} варнов", game_id=None, source="auto",
                    )
                    emoji = "🎮" if hit["action"] == "mutegame" else "🚫"
                    label = "мут в игре" if hit["action"] == "mutegame" else "бан в игре"
                    follow = f"\n{emoji} Авто-{label} на {timeutil.humanize_long(hit['delta'])} (№{auto_case})."
            except Exception as exc:  # noqa: BLE001 - ladder must not break /warn
                follow = f"\n⚠️ авто-наказание не применилось: {exc}"
        elif next_hit:
            remaining = next_hit["warns"] - count
            follow = f"\nЕщё {remaining} варн(а) и сработает {next_hit['action']} на {timeutil.humanize_long(next_hit['delta'])}."

        await inter.response.send_message(
            f"⚠️ {member.mention} — варн №{count} · запись №{case_id}.{follow}", ephemeral=True
        )

    # ---------------------------------------------------------------- clear
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="clearwarns", description="Снять варны участника (все или выборочно)")
    @app_commands.rename(target="цель", case="номер", reason="причина")
    @app_commands.describe(target="Участник", case="Конкретная запись, если нужно снять одну", reason="Причина")
    async def clearwarns(
        self,
        inter: discord.Interaction,
        target: str,
        case: int | None = None,
        reason: str | None = None,
    ) -> None:
        member = await resolve_member(inter.guild, target)
        if case:
            record = await find_case(self.bot.db, inter.guild.id, case, actions=("warn",))
            if record["lifted_at"]:
                raise ModInputError("Эта запись уже снята.")
            await self.bot.db.lift(case, inter.user.id)
            removed = 1
        else:
            rows = await self.bot.db.active_warns(inter.guild.id, member.id)
            for row in rows:
                await self.bot.db.lift(int(row["id"]), inter.user.id)
            removed = len(rows)
        if not removed:
            raise ModInputError("Активных варнов нет.")
        embed = discord.Embed(
            title=f"🧹 Варны сняты · {member.display_name}",
            description=f"Снято записей: **{removed}**\nПричина: {reason or '—'}\nМодератор: {inter.user.mention}",
            colour=audit.COLOR_LIFTED,
            timestamp=discord.utils.utcnow(),
        )
        await self.mod.post(inter.guild, embed)
        await inter.response.send_message(f"🧹 Снято варнов: {removed}.", ephemeral=True)

    # --------------------------------------------------------------- history
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="history", description="История наказаний участника")
    @app_commands.rename(target="цель", limit="лимит")
    async def history(self, inter: discord.Interaction, target: str, limit: int = 15) -> None:
        member = await resolve_member(inter.guild, target)
        rows = await self.bot.db.user_cases(inter.guild.id, member.id, max(1, min(limit, 25)))
        active_warns = await self.bot.db.active_warns(inter.guild.id, member.id)
        if not rows:
            raise ModInputError(f"У {member} нет записей.")
        lines = []
        for row in rows:
            state = "~~снято~~" if row["lifted_at"] else "активен"
            issued = timeutil.from_iso(row["issued_at"])
            lines.append(
                f"№{row['id']} · `{row['action']:<9}` · {state} · "
                f"{issued:%d.%m} · {(row['reason'] or '—')[:48]}"
            )
        embed = discord.Embed(
            title=f"История · {member.display_name}",
            description="\n".join(lines)[:4000],
            colour=audit.COLOR_SENTENCE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"активных варнов: {len(active_warns)} · записей всего: {len(rows)}")
        await inter.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------- warnconfig
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="warnconfig", description="Пороги варнов: N варнов ->действие на срок")
    @app_commands.rename(levels="уровни")
    @app_commands.describe(levels="Например: 3=mute:1h, 5=mute:24h, 8=ban:7d")
    async def warnconfig(self, inter: discord.Interaction, levels: str) -> None:
        parsed = []
        for chunk in levels.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                count, rest = chunk.split("=", 1)
                action, duration = rest.split(":", 1)
            except ValueError:
                raise ModInputError(f"Не понял «{chunk}». Формат: 3=mute:1h") from None
            if action.strip().lower() not in {"mute", "ban", "mutegame", "bangame"}:
                raise ModInputError("Действие: mute, mutegame, bangame или ban.")
            parsed.append(
                {
                    "warns": int(count),
                    "action": action.strip().lower(),
                    "duration": duration.strip(),
                    "delta": timeutil.humanize_long(timeutil.parse_duration(duration.strip())),
                }
            )
        if not parsed:
            raise ModInputError("Список пуст.")
        await self.bot.db.set_guild_config(
            inter.guild.id, {"warn_thresholds": [{k: v for k, v in p.items() if k != 'delta'} for p in parsed]}
        )
        text = "\n".join(f"• {p['warns']} варн(а) → {p['action']} на {p['delta']}" for p in parsed)
        await inter.response.send_message(f"✅ Пороги сохранены:\n{text}", ephemeral=True)

    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="warnlevels", description="Показать текущие пороги варнов")
    async def warnlevels(self, inter: discord.Interaction) -> None:
        rows = await self.thresholds(inter.guild.id)
        if not rows:
            await inter.response.send_message(
                "Порогов нет — варны копятся без авто-наказаний. Задайте через /warnconfig.",
                ephemeral=True,
            )
            return
        text = "\n".join(
            f"• {r['warns']} варн(а) → {r['action']} на {timeutil.humanize_long(r['delta'])}" for r in rows
        )
        await inter.response.send_message(text, ephemeral=True)


async def setup(bot: ModBot) -> None:
    await bot.add_cog(Warnings(bot))


def _delivery_note(delivered: str | None) -> str | None:
    """Что приписать в карточку про доставку варна в игру (None — не с чем работать)."""
    return None if delivered is None else f"в игру: {delivered or 'доставлено'}"
