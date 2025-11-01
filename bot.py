import os
import json
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import discord
from discord import app_commands
from discord.ui import View, button, Button

TOK = os.getenv("DISCORD_TOKEN")  # set this in your environment

DATA_DIR = os.getenv("DATA_DIR", ".")
KEYS_PATH = os.path.join(DATA_DIR, "keys.json")
CLAIMS_PATH = os.path.join(DATA_DIR, "claims.json")

AGE_REQUIREMENT = timedelta(days=7)  # 7 days

def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path) or ".") as tf:
        json.dump(payload, tf, indent=2)
        tmpname = tf.name
    os.replace(tmpname, path)

def _load_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default

class KeyStore:
    def __init__(self, keys_path: str, claims_path: str):
        self.keys_path = keys_path
        self.claims_path = claims_path
        self._lock = asyncio.Lock()
        self._pool: List[str] = []
        self._claims: Dict[str, str] = {}
        self._load()

    def _load(self):
        data = _load_json(self.keys_path, {"pool": []})
        self._pool = list(dict.fromkeys([k.strip() for k in data.get("pool", []) if k.strip()]))
        self._claims = _load_json(self.claims_path, {})

    def _save_pool(self):
        _atomic_write(self.keys_path, {"pool": self._pool})

    def _save_claims(self):
        _atomic_write(self.claims_path, self._claims)

    async def add_keys(self, keys: List[str]) -> int:
        keys = [k.strip() for k in keys if k.strip()]
        if not keys:
            return 0
        async with self._lock:
            existing = set(self._pool)
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

store = KeyStore(KEYS_PATH, CLAIMS_PATH)

intents = discord.Intents.default()  # member.joined_at check would need members intent set True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

class TryView(View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent
    @button(label="Try", style=discord.ButtonStyle.success, custom_id="trial:try")
    async def try_button(self, interaction: discord.Interaction, _: Button):
        now = datetime.now(timezone.utc)

        # Account age check. Switch to `member.joined_at` for guild-join age if desired:
        # joined_at = interaction.user.joined_at  # requires members intent
        # if not joined_at or (now - joined_at) < AGE_REQUIREMENT: ...
        created_at = interaction.user.created_at
        if not created_at or (now - created_at) < AGE_REQUIREMENT:
            await interaction.response.send_message(
                "Requirement not met. Your Discord account must be at least 7 days old.",
                ephemeral=True,
            )
            return

        if await store.has_claimed(interaction.user.id):
            prev = await store.get_claim(interaction.user.id)
            await interaction.response.send_message(
                f"You have already claimed a key.\nYour key: `{prev}`",
                ephemeral=True,
            )
            return

        key = await store.claim(interaction.user.id)
        if not key:
            await interaction.response.send_message(
                "No trial keys are available.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Here is your trial key:\n`{key}`",
            ephemeral=True,
        )

@tree.command(name="posttrial", description="Post the Try button message")
@app_commands.checks.has_permissions(administrator=True)
async def posttrial(interaction: discord.Interaction):
    view = TryView()
    await interaction.response.send_message(
        "Click **Try** to request a trial key if you qualify.",
        view=view,
        ephemeral=False,
    )

@tree.command(name="addkeys", description="Add trial keys (comma or newline separated)")
@app_commands.describe(keys="List of keys separated by commas or newlines")
@app_commands.checks.has_permissions(administrator=True)
async def addkeys(interaction: discord.Interaction, keys: str):
    # split by newline or comma
    parts = [p.strip() for chunk in keys.splitlines() for p in chunk.split(",")]
    added = await store.add_keys(parts)
    await interaction.response.send_message(f"Added {added} new key(s). Pool size: {len(store._pool)}", ephemeral=True)

@tree.command(name="mykey", description="Show your claimed trial key")
async def mykey(interaction: discord.Interaction):
    k = await store.get_claim(interaction.user.id)
    if not k:
        await interaction.response.send_message("You have not claimed a key.", ephemeral=True)
        return
    await interaction.response.send_message(f"Your key: `{k}`", ephemeral=True)

@bot.event
async def on_ready():
    # register persistent view so the button keeps working after restarts
    bot.add_view(TryView())
    try:
        await tree.sync()
    except Exception as e:
        print(f"Slash command sync error: {e}")
    print(f"Logged in as {bot.user} ({bot.user.id})")

if __name__ == "__main__":
    if not TOK:
        raise RuntimeError("Set DISCORD_TOKEN in your environment")
    bot.run(TOK)
