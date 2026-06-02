import discord
import aiohttp
import re
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
API_URL = "http://127.0.0.1:8000/api/suggestions/"

ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID', 0))
VIP_ROLE_NAME = os.getenv('VIP_ROLE_NAME', 'VIP')

class RequestListButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 

    @discord.ui.button(label="Aktuelle Liste per DM 📬", style=discord.ButtonStyle.primary, custom_id="get_list_button")
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL) as response:
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
            print(e)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'🚀 Juhu! Erfolgreich eingeloggt als {client.user}')
    client.add_view(RequestListButton())

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if ALLOWED_CHANNEL_ID != 0 and message.channel.id != ALLOWED_CHANNEL_ID:
        return

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
                async with session.post(API_URL + "reset/") as response:
                    if response.status == 200:
                        await message.channel.send("🔄 **Neue Runde! Alle Tokens wurden zurückgesetzt.**")
        except Exception:
            await message.channel.send("Konnte das Backend nicht erreichen.")
        await message.delete()
        return

    if "youtube.com/" in message.content or "youtu.be/" in message.content:
        match = re.search(r"(?P<url>https?://[^\s]+)", message.content)
        if not match: return
        
        is_vip = any(role.name == VIP_ROLE_NAME for role in message.author.roles) if hasattr(message.author, 'roles') else False

        payload = {
            "discord_user_id": str(message.author.id),
            "discord_username": str(message.author.name),
            "youtube_url": match.group("url"),
            "is_vip": is_vip
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload) as response:
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
            print(f"Fehler bei der Verbindung zu Django: {e}")

client.run(TOKEN)