# bot.py
# Discord trial-key bot with admin tools.
# Python 3.10+. Requires discord.py.
#
# Features added:
# - /addkeys (admin) : add keys (existing behavior)
# - /listkeys (admin) : list available keys or attach full file
# - /listclaims (admin) : list claimed user->key pairs (attached if large)
# - /revoke (admin) : revoke a user's claim, optionally return key to pool
# - /assign (admin) : force-assign a key to a user (removes from pool if needed)
# - /removekey (admin) : remove a key from pool (or from claims if present)
# - /exportkeys (admin) : download pool as text file
# - /exportclaims (admin) : download claims as CSV
# - /setdays (admin) : change min required days (default 7)
# - /setmode (admin) : "account" or "guild" age check
# - /mykey : user checks their key
# - persistent Try button view
# - optional ADMIN_LOG_CHANNEL_ID env var to log admin actions
#
# STORAGE:
# - keys.json -> {"pool": [keys...], "config": {"min_days": 7, "mode": "account"}}
# - claims.json -> { "user_id": "key", ... }
#
# Usage:
# export DISCORD_TOKEN=...
# (if using guild join age checks set intents.members = True and enable Members intent in dev portal)
# python bot.py

import os
import json
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
import io
import csv

import discord
from discord import app_commands
from discord.ui import View, button, Button

# Config paths
DATA_DIR = os.getenv("DATA_DIR", ".")
KEYS_PATH = os.path.join(DATA_DIR, "keys.json")
CLAIMS_PATH = os.path.join(DATA_DIR, "claims.json")

# Environment
TOK = os.getenv("DISCORD_TOKEN")
ADMIN_LOG_CHANNEL_ID = os.getenv("ADMIN_LOG_CHANNEL_ID")  # optional channel id for admin logs (string or int)

# Defaults
DEFAULT_MIN_DAYS = 7
DEFAULT_MODE = "account"  # "account" or "guild"

# Safety
if not TOK:
    raise RuntimeError("Set DISCORD_TOKEN in environment")

def _atomic_write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path) or ".") as tf:
        json.dump(payload, tf, indent=2, ensure_ascii=False)
        tmpname = tf.name
    os.replace(tmpname, path)

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

class KeyStore:
    def __init__(self, keys_path: str, claims_path: str):
        self.keys_path = keys_path
        self.claims_path = claims_path
        self._lock = asyncio.Lock()
        self._pool: List[str] = []
        self._claims: Dict[str, str] = {}
        self._config = {"min_days": DEFAULT_MIN_DAYS, "mode": DEFAULT_MODE}
        self._load()

    def _load(self):
        data = _load_json(self.keys_path, {"pool": [], "config": {"min_days": DEFAULT_MIN_DAYS, "mode": DEFAULT_MODE}})
        self._pool = list(dict.fromkeys([k.strip() for k in data.get("pool", []) if k and k.strip()]))
        cfg = data.get("config", {})
        self._config["min_days"] = int(cfg.get("min_days", DEFAULT_MIN_DAYS))
        self._config["mode"] = cfg.get("mode", DEFAULT_MODE)
        self._claims = _load_json(self.claims_path, {})

    def _save_pool(self):
        _atomic_write(self.keys_path, {"pool": self._pool, "config": self._config})

    def _save_claims(self):
        _atomic_write(self.claims_path, self._claims)

    async def add_keys(self, keys: List[str]) -> int:
        keys = [k.strip() for k in keys if k and k.strip()]
        if not keys:
            return 0
        async with self._lock:
            existing = set(self._pool) | set(self._claims.values())
            new = [k for k in keys if k not in existing]
            if not new:
                return 0
            self._pool.extend(new)
            self._save_pool()
            return len(new)

    async def has_claimed(self, user_id: int) -> bool:
        return str(user_id) in self._claims

    async def get_claim(self, user_id: int) -> Optional[str]:
        return self._claims.get(str(user_id))

    async def claim(self, user_id: int) -> Optional[str]:
        async with self._lock:
            if str(user_id) in self._claims:
                return None
            if not self._pool:
                return None
            key = self._pool.pop(0)
            self._claims[str(user_id)] = key
            self._save_pool()
            self._save_claims()
            return key

    async def revoke_claim(self, user_id: int, return_to_pool: bool = True) -> Optional[str]:
        async with self._lock:
            key = self._claims.pop(str(user_id), None)
            if key and return_to_pool:
                # avoid duplicates
                if key not in self._pool:
                    self._pool.insert(0, key)
            if key:
                self._save_pool()
                self._save_claims()
            return key

    async def assign_key_to_user(self, user_id: int, key: str, remove_from_pool: bool = True) -> bool:
        async with self._lock:
            uid = str(user_id)
            if uid in self._claims:
                return False
            # remove key from pool if present
            if remove_from_pool:
                try:
                    self._pool.remove(key)
                except ValueError:
                    pass
            self._claims[uid] = key
            self._save_pool()
            self._save_claims()
            return True

    async def remove_key_from_pool(self, key: str) -> bool:
        async with self._lock:
            if key in self._pool:
                self._pool = [k for k in self._pool if k != key]
                self._save_pool()
                return True
            # also remove from claims if present
            removed_from_claims = False
            for u, v in list(self._claims.items()):
                if v == key:
                    del self._claims[u]
                    removed_from_claims = True
            if removed_from_claims:
                self._save_claims()
            return removed_from_claims

    def available_count(self) -> int:
        return len(self._pool)

    def claim_count(self) -> int:
        return len(self._claims)

    def list_pool(self) -> List[str]:
        return list(self._pool)

    def list_claims(self) -> Dict[str, str]:
        return dict(self._claims)

    def get_config(self):
        return dict(self._config)

    async def set_config(self, min_days: Optional[int] = None, mode: Optional[str] = None):
        async with self._lock:
            if min_days is not None:
                self._config["min_days"] = int(min_days)
            if mode is not None:
                if mode not in ("account", "guild"):
                    raise ValueError("mode must be 'account' or 'guild'")
                self._config["mode"] = mode
            self._save_pool()

store = KeyStore(KEYS_PATH, CLAIMS_PATH)

# Intents: if you plan to use guild join age, enable members intent and set intents.members = True
intents = discord.Intents.default()
intents.members = True  # required if mode == "guild"
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Helper: admin check
def is_admin():
    return app_commands.checks.has_permissions(administrator=True)

async def admin_log(guild: Optional[discord.Guild], text: str):
    # send optional log to channel
    if not ADMIN_LOG_CHANNEL_ID:
        return
    try:
        chan_id = int(ADMIN_LOG_CHANNEL_ID)
    except Exception:
        return
    try:
        channel = None
        if guild:
            channel = guild.get_channel(chan_id)
        if channel is None:
            channel = bot.get_channel(chan_id)
        if channel:
            await channel.send(text)
    except Exception:
        # do not raise; logging best-effort only
        pass

# Persistent Try button view
class TryView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Try", style=discord.ButtonStyle.success, custom_id="trial:try")
    async def try_button(self, interaction: discord.Interaction, _: Button):
        now = datetime.now(timezone.utc)
        cfg = store.get_config()
        min_days = int(cfg.get("min_days", DEFAULT_MIN_DAYS))
        mode = cfg.get("mode", DEFAULT_MODE)

        # Choose which date to check
        required_delta = timedelta(days=min_days)

        # Determine reference timestamp
        if mode == "guild":
            if not interaction.guild:
                await interaction.response.send_message("This check requires a guild context.", ephemeral=True)
                return
            try:
                member = interaction.guild.get_member(interaction.user.id)
                if member is None:
                    member = await interaction.guild.fetch_member(interaction.user.id)
            except Exception:
                member = None
            joined_at = getattr(member, "joined_at", None)
            if not joined_at or (now - joined_at) < required_delta:
                await interaction.response.send_message(
                    f"Requirement not met. You must have been in this server at least {min_days} day(s).",
                    ephemeral=True,
                )
                return
        else:  # "account"
            created_at = getattr(interaction.user, "created_at", None)
            if not created_at or (now - created_at) < required_delta:
                await interaction.response.send_message(
                    f"Requirement not met. Your Discord account must be at least {min_days} day(s) old.",
                    ephemeral=True,
                )
                return

        if await store.has_claimed(interaction.user.id):
            prev = await store.get_claim(interaction.user.id)
            await interaction.response.send_message(
                f"You already claimed a key. Your key: `{prev}`",
                ephemeral=True,
            )
            return

        key = await store.claim(interaction.user.id)
        if not key:
            await interaction.response.send_message("No trial keys available.", ephemeral=True)
            return

        await interaction.response.send_message(f"Here is your trial key:\n`{key}`", ephemeral=True)
        await admin_log(interaction.guild, f"User {interaction.user} ({interaction.user.id}) claimed a key.")

# Commands
@tree.command(name="addkeys", description="Add trial keys (comma or newline separated)")
@is_admin()
async def cmd_addkeys(interaction: discord.Interaction, keys: str):
    parts = [p.strip() for chunk in keys.splitlines() for p in chunk.split(",")]
    added = await store.add_keys(parts)
    await interaction.response.send_message(f"Added {added} new key(s). Pool size: {store.available_count()}", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} added {added} key(s).")

@tree.command(name="posttrial", description="Post the Try button message")
@is_admin()
async def cmd_posttrial(interaction: discord.Interaction):
    view = TryView()
    await interaction.response.send_message("Click **Try** to request a trial key if you qualify.", view=view)
    await admin_log(interaction.guild, f"Admin {interaction.user} posted trial button message.")

@tree.command(name="mykey", description="Show your claimed trial key")
async def cmd_mykey(interaction: discord.Interaction):
    k = await store.get_claim(interaction.user.id)
    if not k:
        await interaction.response.send_message("You have not claimed a key.", ephemeral=True)
        return
    await interaction.response.send_message(f"Your key: `{k}`", ephemeral=True)

@tree.command(name="listkeys", description="List available keys or attach full list (admin)")
@is_admin()
async def cmd_listkeys(interaction: discord.Interaction, attach: Optional[bool] = app_commands.Transform(False, lambda v: v)):
    pool = store.list_pool()
    if not pool:
        await interaction.response.send_message("No keys in pool.", ephemeral=True)
        return
    # If small, show inline. If large or attach=True, send as file.
    if len(pool) <= 20 and not attach:
        display = "\n".join(f"{i+1}. `{k}`" for i, k in enumerate(pool))
        await interaction.response.send_message(f"Available keys ({len(pool)}):\n{display}", ephemeral=True)
    else:
        bio = io.StringIO("\n".join(pool))
        bio.seek(0)
        file = discord.File(fp=bio, filename="keys_pool.txt")
        await interaction.response.send_message(f"Attached keys pool ({len(pool)}).", file=file, ephemeral=True)

@tree.command(name="listclaims", description="List claimed keys (admin)")
@is_admin()
async def cmd_listclaims(interaction: discord.Interaction, attach: Optional[bool] = app_commands.Transform(False, lambda v: v)):
    claims = store.list_claims()
    if not claims:
        await interaction.response.send_message("No claims yet.", ephemeral=True)
        return
    # Prepare human readable; resolve user names if possible
    rows = []
    for uid, key in claims.items():
        uname = uid
        try:
            member = None
            # attempt to resolve in current guild
            if interaction.guild:
                member = interaction.guild.get_member(int(uid))
            if member is None and interaction.client:
                member = interaction.client.get_user(int(uid))
            if member:
                uname = f"{member} ({uid})"
        except Exception:
            uname = uid
        rows.append((uid, uname, key))
    if len(rows) <= 20 and not attach:
        text = "\n".join(f"{i+1}. {r[1]} -> `{r[2]}`" for i, r in enumerate(rows))
        await interaction.response.send_message(f"Claims ({len(rows)}):\n{text}", ephemeral=True)
    else:
        bio = io.StringIO()
        writer = csv.writer(bio)
        writer.writerow(["user_id", "user_display", "key"])
        for r in rows:
            writer.writerow(r)
        bio.seek(0)
        file = discord.File(fp=bio, filename="claims.csv")
        await interaction.response.send_message(f"Attached claims ({len(rows)}).", file=file, ephemeral=True)

@tree.command(name="revoke", description="Revoke a user's claim. Optionally return the key to pool.")
@is_admin()
@app_commands.describe(user="User to revoke", return_to_pool="Return key to pool (default true)")
async def cmd_revoke(interaction: discord.Interaction, user: discord.User, return_to_pool: Optional[bool] = True):
    key = await store.revoke_claim(user.id, return_to_pool)
    if not key:
        await interaction.response.send_message("That user has no claim.", ephemeral=True)
        return
    await interaction.response.send_message(f"Revoked claim from {user}. Key: `{key}`. Returned to pool: {return_to_pool}", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} revoked {user}'s claim. Key `{key}` returned_to_pool={return_to_pool}.")

@tree.command(name="assign", description="Force assign a key to a user (admin)")
@is_admin()
@app_commands.describe(user="User to assign", key="Key to assign (must not be claimed)")
async def cmd_assign(interaction: discord.Interaction, user: discord.User, key: str):
    # If key is currently claimed, fail
    if key in store.list_claims().values():
        await interaction.response.send_message("That key is already claimed. Revoke first or choose another key.", ephemeral=True)
        return
    success = await store.assign_key_to_user(user.id, key, remove_from_pool=True)
    if not success:
        await interaction.response.send_message("Unable to assign. User may already have a claim.", ephemeral=True)
        return
    await interaction.response.send_message(f"Assigned key `{key}` to {user}.", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} assigned key `{key}` to {user}.")

@tree.command(name="removekey", description="Remove a key from pool or claims (admin)")
@is_admin()
@app_commands.describe(key="Key to remove (from pool or claims)")
async def cmd_removekey(interaction: discord.Interaction, key: str):
    removed = await store.remove_key_from_pool(key)
    if not removed:
        await interaction.response.send_message("Key not found in pool or claims.", ephemeral=True)
        return
    await interaction.response.send_message(f"Removed key `{key}` from pool/claims.", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} removed key `{key}` from pool/claims.")

@tree.command(name="exportkeys", description="Download keys pool as a text file (admin)")
@is_admin()
async def cmd_exportkeys(interaction: discord.Interaction):
    pool = store.list_pool()
    if not pool:
        await interaction.response.send_message("No keys in pool.", ephemeral=True)
        return
    bio = io.StringIO("\n".join(pool))
    bio.seek(0)
    file = discord.File(fp=bio, filename="keys_pool.txt")
    await interaction.response.send_message("Attached keys pool.", file=file, ephemeral=True)

@tree.command(name="exportclaims", description="Download claims as CSV (admin)")
@is_admin()
async def cmd_exportclaims(interaction: discord.Interaction):
    claims = store.list_claims()
    if not claims:
        await interaction.response.send_message("No claims yet.", ephemeral=True)
        return
    bio = io.StringIO()
    writer = csv.writer(bio)
    writer.writerow(["user_id", "key"])
    for uid, key in claims.items():
        writer.writerow([uid, key])
    bio.seek(0)
    file = discord.File(fp=bio, filename="claims.csv")
    await interaction.response.send_message("Attached claims CSV.", file=file, ephemeral=True)

@tree.command(name="setdays", description="Set minimum days required to qualify (admin)")
@is_admin()
@app_commands.describe(days="Integer number of days (e.g. 7)")
async def cmd_setdays(interaction: discord.Interaction, days: int):
    if days < 0 or days > 3650:
        await interaction.response.send_message("Invalid days. Pick between 0 and 3650.", ephemeral=True)
        return
    await store.set_config(min_days=days)
    await interaction.response.send_message(f"Minimum required days set to {days}.", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} set min_days={days}.")

@tree.command(name="setmode", description="Set check mode: 'account' or 'guild' (admin)")
@is_admin()
@app_commands.describe(mode="account or guild")
async def cmd_setmode(interaction: discord.Interaction, mode: str):
    mode = mode.lower()
    if mode not in ("account", "guild"):
        await interaction.response.send_message("Invalid mode. Use 'account' or 'guild'.", ephemeral=True)
        return
    await store.set_config(mode=mode)
    await interaction.response.send_message(f"Mode set to '{mode}'.", ephemeral=True)
    await admin_log(interaction.guild, f"Admin {interaction.user} set mode={mode}.")

# Ready
@bot.event
async def on_ready():
    bot.add_view(TryView())
    try:
        await tree.sync()
    except Exception:
        pass
    print(f"Logged in as {bot.user} ({bot.user.id})")

# Error handler for command permission / parameter errors
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
    else:
        # generic
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass

if __name__ == "__main__":
    bot.run(TOK)

