import discord
import aiohttp
import re
import os
from dotenv import load_dotenv
from discord.ext import tasks

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
API_URL = "http://reactcast-backend:8000/api/suggestions/"
VIP_ROLE_NAME = os.getenv('VIP_ROLE_NAME', 'VIP')

# Globale Caches für das dynamische Multi-Team-Handling
channel_teams = {}  # Format: channel_id (int) -> team_id (int)
channel_locks = {}  # Format: channel_id (int) -> is_locked (bool)

class RequestListButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 

    @discord.ui.button(label="Aktuelle Liste per DM 📬", style=discord.ButtonStyle.primary, custom_id="get_list_button")
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Finde heraus, zu welchem Team dieser Kanal gehört
        team_id = channel_teams.get(interaction.channel_id)
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
        except Exception as e:
            await interaction.response.send_message("Konnte das Backend nicht erreichen.", ephemeral=True)
            print(e, flush=True)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@tasks.loop(seconds=4)
async def sync_bot_channels():
    global channel_teams, channel_locks
    print("[Loop] Synchronisiere registrierte Team-Kanäle aus der DB...", flush=True)
    
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
                        
                        # In temporäre Map eintragen
                        fresh_channel_teams[ch_id] = team_id
                        
                        # Statusänderung (Lock/Unlock) ermitteln
                        old_lock_state = channel_locks.get(ch_id)
                        if old_lock_state != is_locked:
                            channel_locks[ch_id] = is_locked
                            
                            try:
                                channel = await client.fetch_channel(ch_id)
                                if channel:
                                    overwrite = channel.overwrites_for(channel.guild.default_role)
                                    overwrite.send_messages = not is_locked
                                    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
                                    print(f"[Loop] Kanalrechte für {ch_id} angepasst! Gesperrt={is_locked}", flush=True)
                                    
                                    if old_lock_state is not None:
                                        if is_locked:
                                            await channel.send("**Channel zu!** Gerne wieder im nächsten Stream. **Sonntag 17:00 Uhr.**")
                                        else:
                                            await channel.send("**Channel geöffnet!** Ihr könnt wieder Songs einreichen. **Bitte vorher die angepinnte Nachricht lesen!**")
                            except Exception as e:
                                print(f"[Loop] Fehler beim Anpassen der Kanalrechte für {ch_id}: {e}", flush=True)
                                
                    channel_teams = fresh_channel_teams
                else:
                    print(f"[Loop] Backend Fehler: Statuscode {response.status}", flush=True)
    except Exception as e:
        print(f"[Loop] Verbindung zum Django-Backend fehlgeschlagen: {e}", flush=True)

@client.event
async def on_connect():
    print("Bot erfolgreich mit Discord verbunden. Synchronisations-Loop startet...", flush=True)
    if not sync_bot_channels.is_running():
        sync_bot_channels.start()

@client.event
async def on_ready():
    print(f'Bot-Cache vollständig geladen. Bereit als {client.user}', flush=True)
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
        await message.delete()
        return

    # 2. Link-Verarbeitung
    url_match = re.search(r"(?P<url>https?://[^\s]+)", message.content)
    if url_match:
        detected_url = url_match.group("url")
        
        if "youtube.com/" not in detected_url and "youtu.be/" not in detected_url:
            try:
                await message.delete()
                await message.channel.send(
                    f"⚠️ {message.author.mention}, in diesem Kanal sind ausschließlich Links von YouTube erlaubt!", 
                    delete_after=5
                )
            except Exception as e:
                print(f"Fehler beim Löschen einer Fremd-URL: {e}", flush=True)
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
                    if response.status in [201, 202]:
                        await message.add_reaction("✅")
                        if is_vip:
                            await message.add_reaction("🌟")
                    elif response.status == 200:
                        data = await response.json()
                        if data.get("status") == "already_played":
                            await message.add_reaction("🔄")
                    elif response.status == 400:
                        await message.add_reaction("❌")
                        data = await response.json()
                        error_msg = data.get("error", "Unbekannter Fehler")
                        await message.author.send(f"Dein Vorschlag wurde abgelehnt:\n**Grund:** {error_msg}")
        except Exception as e:
            print(f"Fehler bei der Verbindung zu Django: {e}", flush=True)

client.run(TOKEN)