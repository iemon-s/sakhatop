import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands

# =========================
# НАСТРОЙКИ
# =========================


CISTIERS_URL = "https://cistiers.com"
PREFIX = "!"
CACHE_SECONDS = 600
MAX_CONCURRENT_REQUESTS = 20
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "players.json"

# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
)

http: aiohttp.ClientSession | None = None
cache: dict[str, tuple[float, dict]] = {}


def load_players() -> dict[str, list[str]]:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}", encoding="utf-8")
        return {}

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_players(data: dict[str, list[str]]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(DATA_FILE)

    print(f"Список игроков сохранён в: {DATA_FILE}")


def get_server_players(guild_id: int) -> list[str]:
    return load_players().get(str(guild_id), [])


def set_server_players(guild_id: int, players: list[str]) -> None:
    data = load_players()
    data[str(guild_id)] = players
    save_players(data)
    print(f"Сервер {guild_id}: сохранено игроков: {players}")


async def fetch_cistiers_profile(nickname: str) -> dict:
    if http is None:
        raise RuntimeError("HTTP session is not initialized")

    key = nickname.casefold()
    now = time.time()

    cached = cache.get(key)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    # Для iemon__ запрос будет именно:
    # https://cistiers.com/api/profile/iemon__
    url = f"{CISTIERS_URL}/api/profile/{quote(nickname, safe='')}"

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "CisTierTagger/26.2",
    }

    timeout = aiohttp.ClientTimeout(total=45)

    async with http.get(url, headers=headers, timeout=timeout) as response:
        body = await response.text()

        if response.status == 404:
            raise ValueError(f"Игрок `{nickname}` не найден на CISTiers.")

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"CISTiers HTTP {response.status}: {body[:300]}"
            )

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"CISTiers вернул некорректный JSON: {body[:300]}"
            ) from exc

    cache[key] = (now, data)
    return data


def parse_cistiers_profile(data: dict) -> dict:
    """
    Из ответа CISTiers нам нужны ТОЛЬКО:
      - total_points
      - current_tiers[].kit
      - current_tiers[].tier

    Поддерживаются оба распространённых варианта:
      {"total_points": 43, "current_tiers": [...]}
    и:
      {"tierStats": {"totalPoints": 43, "currentTiers": [...]}}
    """

    if not isinstance(data, dict):
        raise ValueError("CISTiers вернул не JSON-объект.")

    # Иногда нужные поля находятся прямо в корне.
    container = data

    # А в ответах, соответствующих DTO CISTierTagger, они находятся
    # внутри tierStats.
    for key in ("tierStats", "tier_stats", "stats"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            container = candidate
            break

    # Поддержка возможной обёртки data/profile.
    if not (
        "total_points" in container
        or "totalPoints" in container
        or "current_tiers" in container
        or "currentTiers" in container
    ):
        for key in ("data", "profile"):
            candidate = data.get(key)
            if isinstance(candidate, dict):
                nested = candidate.get("tierStats") or candidate.get("tier_stats")
                if isinstance(nested, dict):
                    container = nested
                    break
                if any(
                    k in candidate
                    for k in ("total_points", "totalPoints",
                              "current_tiers", "currentTiers")
                ):
                    container = candidate
                    break

    total_points = container.get("total_points")
    if total_points is None:
        total_points = container.get("totalPoints")

    if total_points is None:
        # Не выдаём ложный Overall: 0, если поле реально не найдено.
        raise ValueError(
            "В ответе CISTiers не найдено поле total_points/totalPoints."
        )

    current_tiers = container.get("current_tiers")
    if current_tiers is None:
        current_tiers = container.get("currentTiers")

    if current_tiers is None:
        current_tiers = []

    if not isinstance(current_tiers, list):
        raise ValueError(
            "В ответе CISTiers поле current_tiers/currentTiers имеет "
            "неожиданный формат."
        )

    tiers = []

    for item in current_tiers:
        if not isinstance(item, dict):
            continue

        kit = item.get("kit")
        tier = item.get("tier")

        # points отдельного кита специально НЕ используем.
        if kit is None or tier is None:
            continue

        tiers.append({
            "kit": str(kit),
            "tier": str(tier),
        })

    nickname = (
        data.get("nickname")
        or data.get("name")
        or data.get("username")
        or "Unknown"
    )

    return {
        "nickname": str(nickname),
        "points": int(total_points),
        "tiers": tiers,
    }


async def get_player(nickname: str) -> dict:
    raw = await fetch_cistiers_profile(nickname)
    return parse_cistiers_profile(raw)


def format_tiers(tiers: list[dict]) -> str:
    if not tiers:
        return "`нет тиров`"

    # Только kit + tier. Например:
    # smp: lt3
    return "\n".join(
        f"**{discord.utils.escape_markdown(item['kit'])}**: "
        f"`{discord.utils.escape_markdown(item['tier'])}`"
        for item in tiers
    )


@bot.event
async def on_ready():
    print(f"Бот запущен: {bot.user} (ID: {bot.user.id})")
    print("API: https://cistiers.com/api/profile/<nickname>")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("❌ Укажи ник. Например: `!player iemon__`")
        return

    if isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("❌ Эта команда работает только на Discord-сервере.")
        return

    if isinstance(error, commands.CheckFailure):
        await ctx.reply("❌ Эта команда доступна только администраторам.")
        return

    print(f"Ошибка команды {ctx.command}: {repr(error)}")
    await ctx.reply(f"❌ Ошибка: `{type(error).__name__}`")


@bot.command(name="add")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def add_player(ctx: commands.Context, nickname: str):
    players = get_server_players(ctx.guild.id)

    if any(p.casefold() == nickname.casefold() for p in players):
        await ctx.reply(f"ℹ️ `{nickname}` уже есть в списке.")
        return

    try:
        player = await get_player(nickname)
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    except Exception as exc:
        print(f"Ошибка CISTiers для {nickname}: {repr(exc)}")
        await ctx.reply("❌ Не удалось получить данные игрока с CISTiers.")
        return

    players.append(nickname)
    set_server_players(ctx.guild.id, players)

    await ctx.reply(
        f"✅ `{nickname}` добавлен. "
        f"Overall: **{player['points']:,}**"
    )


@bot.command(name="remove")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def remove_player(ctx: commands.Context, nickname: str):
    players = get_server_players(ctx.guild.id)

    found = next(
        (p for p in players if p.casefold() == nickname.casefold()),
        None,
    )

    if found is None:
        await ctx.reply(f"❌ `{nickname}` не найден в списке.")
        return

    players.remove(found)
    set_server_players(ctx.guild.id, players)
    await ctx.reply(f"✅ `{found}` удалён из Overall-топа.")


@bot.command(name="top")
@commands.guild_only()
async def top(ctx: commands.Context, page: int = 1):
    players = get_server_players(ctx.guild.id)

    if not players:
        await ctx.reply(
            "❌ Список пуст. Администратор должен добавить игроков через `!add <ник>`."
        )
        return

    if page < 1:
        await ctx.reply("❌ Страница должна быть 1 или больше.")
        return

    msg = await ctx.reply("⏳ Получаю данные CISTiers...")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def request_player(name: str):
        async with semaphore:
            try:
                return await get_player(name), None
            except Exception as exc:
                return None, (name, exc)

    results = await asyncio.gather(
        *(request_player(name) for name in players)
    )

    successful = [
        player for player, error in results
        if player is not None
    ]

    successful.sort(
        key=lambda p: (-p["points"], p["nickname"].casefold())
    )

    per_page = 999
    total_pages = max(
        1,
        (len(successful) + per_page - 1) // per_page,
    )

    if page > total_pages:
        await msg.edit(
            content=f"❌ Такой страницы нет. Доступно: {total_pages}."
        )
        return

    start = (page - 1) * per_page
    selected = successful[start:start + per_page]

    lines = []

    for index, player in enumerate(selected, start=start + 1):
        lines.append(
            f"{index}. {discord.utils.escape_markdown(player['nickname'])} — "
            f"{player['points']}"
        )

    description = "\n".join(lines)

    if not description:
        description = "❌ Не удалось получить данные игроков."

    embed = discord.Embed(
        title="Sakha Top по очкам",
        description=description,
        color=discord.Color.gold(),
    )

    embed.set_footer(text=f"Страница {page}/{total_pages}")

    await msg.edit(content=None, embed=embed)

@bot.command(name="player")
@commands.guild_only()
async def player_command(ctx: commands.Context, nickname: str):
    try:
        player = await get_player(nickname)
    except ValueError as exc:
        await ctx.reply(f"❌ {exc}")
        return
    except Exception as exc:
        print(f"Ошибка CISTiers для {nickname}: {repr(exc)}")
        await ctx.reply("❌ Не удалось получить данные с CISTiers.")
        return

    embed = discord.Embed(
        title=discord.utils.escape_markdown(player["nickname"]),
    description=(
        f"**Overall:** {player['points']}\n\n"
        f"{format_tiers(player['tiers'])}"
    ),
        color=discord.Color.blurple(),
    )

    await ctx.reply(embed=embed)


@bot.command(name="list")
@commands.guild_only()
async def list_command(ctx: commands.Context):
    players = get_server_players(ctx.guild.id)

    if not players:
        await ctx.reply("📋 Список игроков пуст.")
        return

    await ctx.reply(
        "📋 Игроки в списке:\n"
        + "\n".join(
            f"**{i}.** {discord.utils.escape_markdown(name)}"
            for i, name in enumerate(players, 1)
        )
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="CISTiers Bot",
        description=(
            "`!top` — Overall-топ\n"
            "`!top 2` — вторая страница\n"
            "`!player <ник>` — Overall + тиры\n"
            "`!list` — список игроков\n"
            "`!add <ник>` — добавить игрока (админ)\n"
            "`!remove <ник>` — удалить игрока (админ)"
        ),
        color=discord.Color.blurple(),
    )

    await ctx.reply(embed=embed)


async def main():
    global http

    http = aiohttp.ClientSession()

    try:
        await bot.start(TOKEN)
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
