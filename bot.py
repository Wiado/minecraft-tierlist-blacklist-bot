import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MODERATOR_ROLE_NAME = os.getenv("MODERATOR_ROLE_NAME", "Moderator")
TESTER_ROLE_NAME = os.getenv("TESTER_ROLE_NAME", "Tester")
BLACKLIST_ROLE_NAME = os.getenv("BLACKLIST_ROLE_NAME", "Blacklisted")
PROTECTED_ROLE_NAMES = [
    "Administrator",
    "Admin",
    "Owner",
    "Wiado",
]
DB_PATH = os.getenv("DB_PATH", "blacklist.db")

# Roles shown in the screenshot. These are removed when someone is blacklisted.
WAITLIST_ROLE_NAMES = [
    "Crystal-Waitlist",
    "Mace-Waitlist",
    "Sword-Waitlist",
    "Axe-Waitlist",
    "Spear Mace-Waitlist",
    "UHC-Waitlist",
    "SMP-Waitlist",
    "Pot-Waitlist",
    "NethPot-Waitlist",
]

if not TOKEN or not GUILD_ID:
    raise RuntimeError("Set DISCORD_TOKEN and GUILD_ID in your .env file.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tierlist-blacklist")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
db: aiosqlite.Connection | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def role_by_name(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.find(lambda r: r.name == name, guild.roles)


def moderator_can_use(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    moderator_role = role_by_name(interaction.guild, MODERATOR_ROLE_NAME)
    if moderator_role is None:
        return False
    # Moderator itself and every role above it can use /blacklist.
    return interaction.user.top_role >= moderator_role


def tester_can_use(interaction: discord.Interaction) -> bool:
    """Return True for Testers and every role above Tester."""
    if not isinstance(interaction.user, discord.Member):
        return False
    tester_role = role_by_name(interaction.guild, TESTER_ROLE_NAME)
    if tester_role is None:
        return False
    return interaction.user.top_role >= tester_role


def is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    """Ticket channels start with ticket-, e.g. ticket-1 or ticket-mace-ht3-wiado."""
    return isinstance(channel, discord.TextChannel) and channel.name.startswith("ticket-") and len(channel.name) > 7


def has_protected_role(member: discord.Member) -> str | None:
    """Return the protected role name the member has, if any."""
    for role_name in PROTECTED_ROLE_NAMES:
        role = role_by_name(member.guild, role_name)
        if role and role in member.roles:
            return role_name
    return None


async def get_guild() -> discord.Guild:
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        raise RuntimeError("The configured GUILD_ID is not a server the bot is in.")
    return guild


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY,
            expires_at REAL NOT NULL,
            added_by INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    await db.commit()


async def save_blacklist(user_id: int, expires_at: datetime, added_by: int) -> None:
    assert db is not None
    await db.execute(
        """
        INSERT INTO blacklist(user_id, expires_at, added_by, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            expires_at=excluded.expires_at,
            added_by=excluded.added_by,
            created_at=excluded.created_at
        """,
        (user_id, expires_at.timestamp(), added_by, utcnow().timestamp()),
    )
    await db.commit()


async def delete_blacklist(user_id: int) -> None:
    assert db is not None
    await db.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
    await db.commit()


async def get_blacklist(user_id: int):
    assert db is not None
    async with db.execute(
        "SELECT user_id, expires_at, added_by, created_at FROM blacklist WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def get_all_blacklists():
    assert db is not None
    async with db.execute(
        "SELECT user_id, expires_at, added_by, created_at FROM blacklist ORDER BY expires_at ASC"
    ) as cursor:
        return await cursor.fetchall()


async def remove_blacklist_roles(member: discord.Member, blacklist_role: discord.Role) -> None:
    roles_to_remove = []
    for name in WAITLIST_ROLE_NAMES:
        role = role_by_name(member.guild, name)
        if role and role in member.roles:
            roles_to_remove.append(role)

    if roles_to_remove:
        await member.remove_roles(
            *roles_to_remove,
            reason="Blacklisted: removing tier-list waitlist roles",
        )

    if blacklist_role not in member.roles:
        await member.add_roles(blacklist_role, reason="Blacklisted")


async def expire_user(user_id: int) -> None:
    guild = await get_guild()
    member = guild.get_member(user_id)

    blacklist_role = role_by_name(guild, BLACKLIST_ROLE_NAME)
    if member and blacklist_role and blacklist_role in member.roles:
        await member.remove_roles(blacklist_role, reason="Blacklist period expired")

    await delete_blacklist(user_id)


async def cleanup_expired() -> None:
    now = utcnow().timestamp()
    rows = await get_all_blacklists()
    for user_id, expires_at, _, _ in rows:
        if expires_at <= now:
            try:
                await expire_user(user_id)
                log.info("Expired blacklist for user %s", user_id)
            except discord.HTTPException as exc:
                log.warning("Could not remove blacklist role from %s: %s", user_id, exc)
                # Keep it in DB so the next cleanup can retry.
            except Exception:
                log.exception("Unexpected error expiring blacklist for %s", user_id)


@tasks.loop(seconds=30)
async def blacklist_cleanup_loop():
    await cleanup_expired()


@blacklist_cleanup_loop.before_loop
async def before_cleanup_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        # Sync commands only to your tier-list server, so they appear quickly.
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Slash commands synced to %s", guild.name)

    if not blacklist_cleanup_loop.is_running():
        blacklist_cleanup_loop.start()


@bot.tree.command(name="blacklist", description="Blacklist a user for a number of days.")
@app_commands.describe(
    user="The Discord user to blacklist.",
    days="How many days the blacklist should last.",
)
async def blacklist(interaction: discord.Interaction, user: discord.Member, days: app_commands.Range[int, 1, 3650]):
    if not moderator_can_use(interaction):
        await interaction.response.send_message(
            f"You need the **{MODERATOR_ROLE_NAME}** role or a role above it to use this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    assert guild is not None

    protected_role = has_protected_role(user)
    if protected_role:
        await interaction.response.send_message(
            f"You cannot blacklist {user.mention} because they have the **{protected_role}** role.",
            ephemeral=True,
        )
        return

    # Moderators also cannot blacklist themselves.
    if user.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot blacklist yourself.",
            ephemeral=True,
        )
        return

    blacklist_role = role_by_name(guild, BLACKLIST_ROLE_NAME)
    if blacklist_role is None:
        await interaction.response.send_message(
            f"I couldn't find the **{BLACKLIST_ROLE_NAME}** role.",
            ephemeral=True,
        )
        return

    # Discord will reject role changes if the bot's highest role is not above
    # the role being changed. Give a useful error instead of silently failing.
    if blacklist_role >= guild.me.top_role:
        await interaction.response.send_message(
            f"My bot role must be **above `{BLACKLIST_ROLE_NAME}`** in the server role list.",
            ephemeral=True,
        )
        return

    blocked = []
    for name in WAITLIST_ROLE_NAMES:
        role = role_by_name(guild, name)
        if role and role >= guild.me.top_role:
            blocked.append(name)

    if blocked:
        await interaction.response.send_message(
            "My bot role must be above these roles before I can remove them:\n"
            + "\n".join(f"• `{name}`" for name in blocked),
            ephemeral=True,
        )
        return

    # If the user is already blacklisted, this command resets their timer.
    expires_at = utcnow() + timedelta(days=days)

    try:
        await remove_blacklist_roles(user, blacklist_role)
        await save_blacklist(user.id, expires_at, interaction.user.id)
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the role change. Make sure the bot has **Manage Roles** "
            "and its highest role is above the roles it needs to edit.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"Discord returned an error while changing roles: `{exc}`",
            ephemeral=True,
        )
        return

    # Exact notice requested, with the target mentioned.
    notice = (
        f"{user.mention} — You have been Blacklisted for **{days} days**. "
        "You will have to manually get the waitlist roles once your "
        "Blacklisted period is over"
    )
    await interaction.response.send_message(notice)


@bot.tree.command(name="unblacklist", description="Remove a user's Blacklisted role and blacklist timer.")
@app_commands.describe(user="The Discord user to unblacklist.")
async def unblacklist(interaction: discord.Interaction, user: discord.Member):
    if not moderator_can_use(interaction):
        await interaction.response.send_message(
            f"You need the **{MODERATOR_ROLE_NAME}** role or a role above it to use this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    assert guild is not None

    record = await get_blacklist(user.id)
    blacklist_role = role_by_name(guild, BLACKLIST_ROLE_NAME)

    if record is None and (blacklist_role is None or blacklist_role not in user.roles):
        await interaction.response.send_message(
            f"{user.mention} is not currently Blacklisted.",
            ephemeral=True,
        )
        return

    if blacklist_role is None:
        await interaction.response.send_message(
            f"I couldn't find the **{BLACKLIST_ROLE_NAME}** role.",
            ephemeral=True,
        )
        return

    if blacklist_role >= guild.me.top_role:
        await interaction.response.send_message(
            f"My bot role must be **above `{BLACKLIST_ROLE_NAME}`** in the server role list.",
            ephemeral=True,
        )
        return

    try:
        if blacklist_role in user.roles:
            await user.remove_roles(
                blacklist_role,
                reason=f"Unblacklisted by {interaction.user}",
            )
        await delete_blacklist(user.id)
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the role change. Make sure the bot has **Manage Roles** "
            "and its highest role is above the Blacklisted role.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"Discord returned an error while removing the blacklist: `{exc}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{user.mention} has been **unblacklisted**. Their waitlist roles were not restored; "
        "they must manually get them again."
    )


@bot.tree.command(name="blacklisted", description="List everyone currently blacklisted.")
async def blacklisted(interaction: discord.Interaction):
    await cleanup_expired()
    rows = await get_all_blacklists()

    if not rows:
        await interaction.response.send_message("Nobody is currently Blacklisted.")
        return

    now = utcnow()
    lines = ["**Currently Blacklisted:**"]
    for user_id, expires_at, _, _ in rows:
        remaining = datetime.fromtimestamp(expires_at, timezone.utc) - now
        if remaining.total_seconds() <= 0:
            continue

        member = interaction.guild.get_member(user_id) if interaction.guild else None
        name = member.mention if member else f"<@{user_id}>"
        total_seconds = int(remaining.total_seconds())
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)

        if days:
            left = f"{days}d {hours}h"
        elif hours:
            left = f"{hours}h {minutes}m"
        else:
            left = f"{minutes}m"

        lines.append(f"• {name} — **{left} left**")

    # Keep each Discord message under the 2000-character limit.
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}".strip()
    if current:
        chunks.append(current)

    await interaction.response.send_message(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


@bot.tree.command(name="timeleft", description="Check how long a user's blacklist has left.")
@app_commands.describe(user="The Discord user to check.")
async def timeleft(interaction: discord.Interaction, user: discord.Member):
    record = await get_blacklist(user.id)

    if record is None:
        await interaction.response.send_message(
            f"{user.mention} is not currently Blacklisted."
        )
        return

    expires_at = datetime.fromtimestamp(record[1], timezone.utc)
    remaining = expires_at - utcnow()

    if remaining.total_seconds() <= 0:
        try:
            await expire_user(user.id)
        except Exception:
            log.exception("Failed to clean up expired blacklist for %s", user.id)
        await interaction.response.send_message(
            f"{user.mention}'s Blacklist has expired."
        )
        return

    total_seconds = int(remaining.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    if days:
        text = f"{days} day(s), {hours} hour(s), {minutes} minute(s)"
    elif hours:
        text = f"{hours} hour(s), {minutes} minute(s), {seconds} second(s)"
    else:
        text = f"{minutes} minute(s), {seconds} second(s)"

    await interaction.response.send_message(
        f"{user.mention} has **{text}** left on their Blacklist."
    )


# ---------------------------------------------------------------------------
# Ticket commands
# ---------------------------------------------------------------------------

ticket_group = app_commands.Group(name="ticket", description="Manage high-tier test tickets.")


def ticket_channel_error(interaction: discord.Interaction) -> str | None:
    if not is_ticket_channel(interaction.channel):
        return "This command can only be used inside a ticket channel (for example, `ticket-1`)."
    return None


@ticket_group.command(name="add", description="Add a Discord user to this ticket.")
@app_commands.describe(user="The Discord user to add to this ticket.")
async def ticket_add(interaction: discord.Interaction, user: discord.Member):
    if not tester_can_use(interaction):
        await interaction.response.send_message(
            f"You need the **{TESTER_ROLE_NAME}** role or a role above it to use this command.",
            ephemeral=True,
        )
        return

    error = ticket_channel_error(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    overwrite = channel.overwrites_for(user)
    overwrite.view_channel = True
    overwrite.send_messages = True
    overwrite.read_message_history = True
    overwrite.attach_files = True
    overwrite.embed_links = True

    try:
        await channel.set_permissions(
            user,
            overwrite=overwrite,
            reason=f"Added to ticket by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the permission change. Make sure the bot has **Manage Channels**.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"Discord returned an error while adding the user: `{exc}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ {user.mention} has been **added to this ticket**."
    )


@ticket_group.command(name="remove", description="Remove a Discord user from this ticket.")
@app_commands.describe(user="The Discord user to remove from this ticket.")
async def ticket_remove(interaction: discord.Interaction, user: discord.Member):
    if not tester_can_use(interaction):
        await interaction.response.send_message(
            f"You need the **{TESTER_ROLE_NAME}** role or a role above it to use this command.",
            ephemeral=True,
        )
        return

    error = ticket_channel_error(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    guild = interaction.guild
    assert guild is not None

    moderator_role = role_by_name(guild, MODERATOR_ROLE_NAME)
    if moderator_role and user.top_role >= moderator_role:
        await interaction.response.send_message(
            f"You cannot remove {user.mention} from a ticket because they have the "
            f"**{MODERATOR_ROLE_NAME}** role or a role above it.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    try:
        await channel.set_permissions(
            user,
            overwrite=None,
            reason=f"Removed from ticket by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the permission change. Make sure the bot has **Manage Channels**.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"Discord returned an error while removing the user: `{exc}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ {user.mention} has been **removed from this ticket**."
    )


GAMEMODE_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in [
        "Mace", "Spear Mace", "UHC", "Nethpot", "Pot",
        "Sword", "Axe", "SMP", "Crystal", "Diamond SMP",
    ]
]

TIER_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in ["LOW", "HT3", "LT2", "HT2", "LT1", "HT1"]
]


@ticket_group.command(name="rename", description="Rename this ticket with its gamemode, tier and Minecraft username.")
@app_commands.describe(
    gamemode="The gamemode being tested.",
    tier="The tier being tested for.",
    minecraft_username="The Minecraft username (3-16 characters).",
)
@app_commands.choices(gamemode=GAMEMODE_CHOICES, tier=TIER_CHOICES)
async def ticket_rename(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    tier: app_commands.Choice[str],
    minecraft_username: str,
):
    if not tester_can_use(interaction):
        await interaction.response.send_message(
            f"You need the **{TESTER_ROLE_NAME}** role or a role above it to use this command.",
            ephemeral=True,
        )
        return

    error = ticket_channel_error(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    if not 3 <= len(minecraft_username) <= 16:
        await interaction.response.send_message(
            "The Minecraft username must be between **3 and 16 characters**.",
            ephemeral=True,
        )
        return

    def slugify(value: str) -> str:
        return value.lower().replace(" ", "-")

    new_name = (
        f"ticket-{slugify(gamemode.value)}-"
        f"{tier.value.lower()}-{minecraft_username.lower()}"
    )

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    try:
        await channel.edit(
            name=new_name,
            reason=f"Ticket renamed by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the channel rename. Make sure the bot has **Manage Channels**.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"Discord returned an error while renaming the ticket: `{exc}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ Ticket renamed to **`{new_name}`**."
    )


bot.tree.add_command(ticket_group)


async def main():
    await init_db()
    try:
        await bot.start(TOKEN)
    finally:
        if db:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
