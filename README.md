# 🔮 SM GrowMart HQ — Premium Verification System (Python & PostgreSQL)

Welcome to the production-deployable **Python (FastAPI + discord.py)** edition of your premium Discord Verification Bot. This app let users connect their accounts using **Secure OAuth2**, automatically saves verified statuses inside a **PostgreSQL Database** on Render, and updates their Role instantly.

---

## 🛠️ How It Works (The Complete Architecture)

```
[ Discord User ] ──(Clicks Verify Button)──> [ Discord OAuth2 Consent Page ]
                                                        │
                                                 (User Authorizes)
                                                        │
                                                        ▼
[ Guild Member Joined & Role Created ] <──(Bot)── [ /callback Web Server ]
                                                        │
                                           (Saved to PostgreSQL Database)
```

1. **Persistent Messages:** When you trigger the embed message setup, Discord saves it in the channel. **Even if your Render service restarts or gets upgraded, the message will remain in the channel forever.** The button will always work as long as your Render web server is active.
2. **PostgreSQL Database:** Every verified user’s profiles, authentication scopes, access tokens, and verification timestamps are saved securely. This is perfect for verifying who authorized and tracking member counts! 
3. **Automatic Joiner:** By utilizing the `guilds.join` scope, if a reader is not currently a member of your server but completes authorization, the system will automatically join them to your Guild and award the role.

---

## 🚀 Easy Deploy Guide to Render in 5 Steps

### Step 1: Create application inside Discord Developer Portal
1. Go to [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and name it (e.g. `SM GrowMart-Verify`).
3. Under the **OAuth2** tab:
   - Copy the **Client ID** and **Client Secret**.
   - Under **Redirects**, click **Add Redirect** and paste your Web App URL (from Render) with `/callback` ending. 
     *Example:* `https://sm-growmart-verify.onrender.com/callback` (or `http://localhost:3000/callback` for local testing).
4. Under the **Bot** tab:
   - Click **Add Bot**.
   - Copy the **Bot Token** securely.
   - Scroll down to **Privileged Gateway Intents** and enable **Server Members Intent** (required to assign roles).

---

### Step 2: Provision a free PostgreSQL Database on Render
1. Open your [Render Dashboard](https://dashboard.render.com).
2. Tap **New +** and select **PostgreSQL**.
3. Name your Database and create it. Once provisioned, copy the **External Database URL** (which starts with `postgresql://`).

---

### Step 3: Deploy Web Service to Render
1. Create a **New +** Web Service on Render.
2. Connect your GitHub repository containing the files.
3. Select Environment as **Python**.
4. Set the following Build Command:
   ```bash
   pip install -r python_bot/requirements.txt
   ```
5. Set the following Start Command:
   ```bash
   python python_bot/main.py
   ```

---

### Step 4: Add Environment Variables in Render Dashboard
Go to the **Environment** tab inside your Render Web Service configurations and add:

| Environment Variable | Description | Example / Location |
| :--- | :--- | :--- |
| **`DISCORD_TOKEN`** | Your Discord Bot token | Discord Developer Portal -> Bot -> Token |
| **`DISCORD_CLIENT_ID`** | Your OAuth Client ID | Discord Developer Portal -> OAuth2 -> General -> Client ID |
| **`DISCORD_CLIENT_SECRET`** | Your OAuth Client Secret | Discord Developer Portal -> OAuth2 -> General -> Client Secret |
| **`DISCORD_REDIRECT_URI`** | Authorized Callback Endpoint | `https://sm-growmart-verify.onrender.com/callback` |
| **`GUILD_ID`** | Your Discord Server ID | Right-click server avatar in Discord -> Copy Server ID |
| **`VERIFIED_ROLE_ID`** | Role to assign on verify completion | Role Settings in Discord -> Right-click the Role -> Copy ID |
| **`DATABASE_URL`** | Postgres connection string | Render PostgreSQL space -> External Database URL |

---

### Step 5: Send the Verification Panel (Termux / Command Line)

You only need to do this **ONCE**! After your Render service is live, use Termux (or any command line tool) and type the following command to make the bot drop the Panel into your desired channel:

```bash
curl -X POST "https://verify.digamber.in/api/send-embed?channel_id=YOUR_CHANNEL_ID"
```

- **Boom!** The beautiful message is sent directly, fully persistent and live!

---

## 💎 Features Included:
- **FastAPI Core:** Super fast and handles highly scalable requests asynchronously.
- **Auto-Join Engine:** Users instantly join the server of choice if they aren't already.
- **Glassmorphic Callback Pages:** Gorgeous visual layouts with premium modern CSS, animations, success/failed parameters.
- **Auto Database Recovery:** If database link goes slow or disconnects, the portal safe-guards logs to prevent program crashes.
