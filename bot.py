import logging
import os
import re
import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("reactcast-bot")

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN ist nicht gesetzt. Bitte .env-Datei prüfen.")

# Muss auf den Backend-Service im docker-compose-Netzwerk zeigen. Der Service
# heisst dort "reactcast-backend", nicht "backend".
API_URL = os.getenv(
    'BACKEND_URL', "http://reactcast-backend:8000/api/suggestions/"
)

VIP_ROLE_NAME = os.getenv('VIP_ROLE_NAME', 'VIP')

# Reactions carry the outcome of a submission. They are named here so the
# meaning is readable at the call site and encoded once, not inline.
OK_REACTION = "\u2705"          # accepted
VIP_REACTION = "\U0001F31F"     # accepted, into the VIP wheel
REPEAT_REACTION = "\U0001F504"  # already played
REJECT_REACTION = "\u274c"      # rejected with a reason
WARN_REACTION = "\u26a0\ufe0f"  # rejected for a technical reason

channel_teams: dict[int, int] = {}
channel_locks: dict[int, bool] = {}


async def read_json(response, default=None):
    """Return the JSON body, or ``default`` when the body is not JSON.

    A rejected request may carry an HTML error page (proxy, crash page). That
    is still a rejection, so it must not be reported as "backend unreachable".
    """
    try:
        return await response.json()
    except Exception:
        log.warning("Antwort war kein JSON (Status %s)", response.status)
        return {} if default is None else default


async def read_error(response, fallback):
    """Return the 'error' field of a response body, or ``fallback``."""
    data = await read_json(response)
    return data.get("error") or fallback


class RequestListButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Aktuelle Liste per DM", style=discord.ButtonStyle.primary, custom_id="get_list_button")
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        team_id = channel_teams.get(interaction.channel_id) if interaction.channel_id else None
        if not team_id:
            await interaction.response.send_message("Dieser Kanal ist aktuell keiner aktiven Streamer-Community zugeordnet!", ephemeral=True)
            return

        headers = {"X-Team-ID": str(team_id)}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        vips = data.get('vip_pool', [])
                        normals = data.get('normal_pool', [])

                        if not vips and not normals:
                            await interaction.response.send_message("Die Liste ist momentan leer!", ephemeral=True)
                            return

                        msg = "**🎵 Aktuelle ReactCast Songliste:**\n\n"
                        if vips:
                            msg += "🌟 **VIP RAD:**\n"
                            for song in vips:
                                msg += f"• **{song['artist']}** - {song['title']} *(von {song['discord_username']})*\n"
                            msg += "\n"
                        if normals:
                            msg += "🎡 **NORMALES RAD:**\n"
                            for song in normals:
                                msg += f"• **{song['artist']}** - {song['title']} *(von {song['discord_username']})*\n"

                        await interaction.user.send(msg)
                        await interaction.response.send_message("Ich habe dir die Liste als Direktnachricht geschickt!", ephemeral=True)
                    else:
                        await interaction.response.send_message("Fehler beim Abrufen der API.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Konnte das Backend nicht erreichen.", ephemeral=True)
            log.exception("Fehler beim Abrufen der Vorschlagsliste per Button")


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@tasks.loop(seconds=4)
async def sync_bot_channels():
    global channel_teams

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL + "bot/teams/") as response:
                if response.status == 200:
                    teams_data = await response.json()

                    fresh_channel_teams = {}

                    for team in teams_data:
                        raw_channel_id = team.get("discord_channel_id")
                        if not raw_channel_id:
                            continue

                        try:
                            ch_id = int(raw_channel_id)
                        except (ValueError, TypeError):
                            continue

                        team_id = team["id"]
                        is_locked = team["is_channel_locked"]

                        fresh_channel_teams[ch_id] = team_id

                        old_lock_state = channel_locks.get(ch_id)
                        if old_lock_state != is_locked:
                            channel_locks[ch_id] = is_locked

                            try:
                                channel = await client.fetch_channel(ch_id)
                                if isinstance(channel, discord.TextChannel):
                                    overwrite = channel.overwrites_for(channel.guild.default_role)
                                    overwrite.send_messages = not is_locked
                                    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
                                    log.info("Kanalrechte für %s angepasst! Gesperrt=%s", ch_id, is_locked)

                                    if old_lock_state is not None:
                                        if is_locked:
                                            await channel.send("**Channel zu!** Gerne wieder im nächsten Stream. **Sonntag 17:00 Uhr.**")
                                        else:
                                            await channel.send("**Channel geöffnet!** Ihr könnt wieder Songs einreichen. **Bitte vorher die angepinnte Nachricht lesen!**")
                            except Exception:
                                log.exception("Fehler beim Anpassen der Kanalrechte für %s", ch_id)

                    channel_teams = fresh_channel_teams
                else:
                    log.warning("Backend Fehler: Statuscode %s", response.status)
    except Exception:
        log.exception("Verbindung zum Django-Backend fehlgeschlagen")


@client.event
async def on_connect():
    log.info("Bot erfolgreich mit Discord verbunden. Synchronisations-Loop startet...")
    if not sync_bot_channels.is_running():
        sync_bot_channels.start()


@client.event
async def on_ready():
    log.info("Bot-Cache vollständig geladen. Bereit als %s", client.user)
    client.add_view(RequestListButton())


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    team_id = channel_teams.get(message.channel.id)
    if not team_id:
        return

    headers = {"X-Team-ID": str(team_id)}

    if message.content == "!setup" and message.author.guild_permissions.administrator:
        await message.channel.send(
            "👇 **Hol dir die aktuelle Vorschlagsliste!** 👇\nKlicke auf den Button, um alle bisher eingereichten Songs per Direktnachricht zu erhalten.",
            view=RequestListButton()
        )
        await message.delete()
        return

    if message.content == "!reset" and message.author.guild_permissions.administrator:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL + "reset/", headers=headers) as response:
                    if response.status == 200:
                        await message.channel.send("**Alle tokens wurden zurück gesetzt. **")
        except Exception:
            await message.channel.send("Konnte das Backend nicht erreichen.")
            log.exception("Fehler beim Zurücksetzen der Tokens")
        await message.delete()
        return

    url_match = re.search(r"(?P<url>https?://[^\s]+)", message.content)
    if url_match:
        detected_url = url_match.group("url")

        if "youtube.com/" not in detected_url and "youtu.be/" not in detected_url:
            try:
                await message.delete()
                await message.channel.send(
                    f"⚠ {message.author.mention}, in diesem Kanal sind ausschließlich Links von YouTube erlaubt!",
                    delete_after=5
                )
            except Exception:
                log.exception("Fehler beim Löschen einer Fremd-URL")
            return

        is_vip = any(role.name == VIP_ROLE_NAME for role in message.author.roles) if hasattr(message.author, 'roles') else False

        payload = {
            "discord_user_id": str(message.author.id),
            "discord_username": str(message.author.name),
            "youtube_url": detected_url,
            "is_vip": is_vip
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload, headers=headers) as response:
                    if response.status == 201:
                        await message.add_reaction(OK_REACTION)
                        if is_vip:
                            await message.add_reaction(VIP_REACTION)
                    elif response.status == 202:
                        data = await read_json(response)
                        if data.get("status") == "already_played":
                            await message.add_reaction(REPEAT_REACTION)
                            error_msg = data.get("error", "Dieser Song wurde bereits im Stream gespielt!")
                            await message.author.send(f"Dein Vorschlag wurde abgelehnt:\n**Grund:** {error_msg}")
                    elif response.status == 400:
                        await message.add_reaction(REJECT_REACTION)
                        error_msg = await read_error(response, "Unbekannter Fehler")
                        await message.author.send(f"Dein Vorschlag wurde abgelehnt:\n**Grund:** {error_msg}")
                    elif response.status == 404:
                        # The channel maps to a team the backend no longer knows.
                        # Staying silent used to swallow the suggestion entirely.
                        await message.add_reaction(WARN_REACTION)
                        await message.author.send(
                            "Dein Vorschlag konnte nicht angenommen werden:\n"
                            "**Grund:** Dieser Kanal ist keinem Team mehr zugeordnet. "
                            "Bitte einen Admin informieren."
                        )
                        log.error(
                            "Backend kennt Team %s nicht mehr (Kanal %s)",
                            team_id,
                            message.channel.id,
                        )
                    else:
                        await message.add_reaction(WARN_REACTION)
                        await message.author.send(
                            "Dein Vorschlag konnte gerade nicht gepr\u00fcft werden:\n"
                            "**Grund:** Technischer Fehler im Backend. Bitte sp\u00e4ter erneut versuchen."
                        )
                        log.error(
                            "Unerwarteter Statuscode %s beim Einreichen eines Vorschlags",
                            response.status,
                        )
        except Exception:
            # Network trouble must be visible too: the user should know the
            # suggestion was not registered instead of assuming it was.
            log.exception("Fehler bei der Verbindung zu Django")
            try:
                await message.add_reaction(WARN_REACTION)
                await message.author.send(
                    "Dein Vorschlag konnte gerade nicht gepr\u00fcft werden:\n"
                    "**Grund:** Das Backend war nicht erreichbar. Bitte sp\u00e4ter erneut versuchen."
                )
            except Exception:
                log.exception("Konnte den Nutzer nicht \u00fcber den Fehler informieren")


if __name__ == "__main__":
    client.run(TOKEN)
