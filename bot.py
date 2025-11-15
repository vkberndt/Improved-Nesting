# --- Imports ---
import os, json, re, asyncio
import discord
from discord import app_commands
from discord.ext import commands
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from mcrcon import MCRcon
import db
from typing import Optional

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
tree = bot.tree

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")
SERVER_NAME = os.getenv("SERVER_NAME", "Anthrax")

# --- Google Sheets Setup ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_client(env_var="GOOGLE_JSON", fallback_file="credentials.json"):
    creds_dict = None
    if os.getenv(env_var):
        creds_dict = json.loads(os.getenv(env_var))
    else:
        with open(fallback_file, encoding="utf-8") as f:
            creds_dict = json.load(f)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

gs_client = get_client()
aid_wb = gs_client.open("Anthrax Registration")
aid_map_ws = aid_wb.sheet1

def get_aid_by_discord(discord_id: int) -> Optional[str]:
    """Lookup Alderon ID for a Discord ID from the sheet."""
    col_discord = aid_map_ws.col_values(1)  # Discord ID column
    col_aid = aid_map_ws.col_values(3)      # Alderon ID column
    for d_id, aid in zip(col_discord[1:], col_aid[1:]):  # skip header row
        if d_id.strip() == str(discord_id).strip():
            return aid.strip()
    return None

def load_google_sheet() -> list[dict]:
    """Load all rows from the sheet into a list of dicts for bulk sync."""
    col_discord = aid_map_ws.col_values(1)  # Discord ID column
    col_aid = aid_map_ws.col_values(3)      # Alderon ID column

    rows = []
    # Skip the first row (headers)
    for d_id, aid in zip(col_discord[1:], col_aid[1:]):
        if d_id.strip() and aid.strip():
            rows.append({
                "discord_id": d_id.strip(),
                "aid": aid.strip()
            })
    return rows

# --- RCON helpers ---
import re
from rcon import RCONClient

async def get_playerinfo(aid: str):
    """
    Run /playerinfo <AID> and parse out name, agid, dinosaur, growth, and coords.
    """
    try:
        client = RCONClient()
        await client.connect()
        resp = await client.command(f"/playerinfo {aid}") or ""
        await client.close()
    except Exception as e:
        print("[RCON] Error:", e)
        return None

    resp_clean = re.sub(r"^\(playerinfo [^)]+\):\s*", "", resp)

    fields: dict[str, str] = {}
    for segment in resp_clean.split(" / "):
        if ":" not in segment:
            continue
        key, val = map(str.strip, segment.split(":", 1))
        fields[key.lower()] = val

    name     = fields.get("name")
    agid     = fields.get("agid")
    dinosaur = fields.get("dinosaur")  # preserve case
    growth   = fields.get("growth")
    role     = fields.get("role")
    marks    = fields.get("marks")
    location = fields.get("location")

    coords = None
    if location:
        m = re.search(r"X=([-\d\.]+)\s*Y=([-\d\.]+)\s*Z=([-\d\.]+)", location)
        if m:
            coords = (float(m.group(1)), float(m.group(2)), float(m.group(3)))

    return {
        "name": name,
        "agid": agid,
        "species_code": dinosaur if dinosaur else None,  # no lowercasing
        "growth": growth,
        "role": role,
        "marks": marks,
        "coords": coords,
        "raw": resp_clean,
    }


async def setattr_growth(aid: str, growth_value: float):
    """
    Run /setattr <AID> growth <value> to set a player's growth.
    """
    try:
        client = RCONClient()
        await client.connect()
        cmd = f"/setattr {aid} growth {growth_value}"
        resp = await client.command(cmd)
        await client.close()
        print(f"[RCON] {cmd} -> {resp}")
        return resp
    except Exception as e:
        print("[RCON] Error in setattr_growth:", e)
        return None


async def teleport(aid: str, x: float, y: float, z: float):
    """
    Run /teleport (X=<x>,Y=<y>,Z=<z>) to move a player to given coordinates.
    """
    try:
        client = RCONClient()
        await client.connect()
        # Format exactly as server expects
        cmd = f"/teleport (X={x},Y={y},Z={z})"
        resp = await client.command(cmd)
        await client.close()
        print(f"[RCON] {cmd} -> {resp}")
        return resp
    except Exception as e:
        print("[RCON] Error in teleport:", e)
        return None

# --- Parent Details Modal (Mother/Father Info) ---
class ParentDetailsModal(discord.ui.Modal):
    def __init__(self, nest_id: int, role: str):
        super().__init__(title=f"{role.capitalize()} Details")
        self.nest_id = nest_id
        self.role = role

        # ✅ Max 5 inputs allowed per modal
        self.dino_name = discord.ui.TextInput(label="Dino Name", required=False)
        self.subspecies = discord.ui.TextInput(label="Subspecies", required=False)
        self.skins = discord.ui.TextInput(
            label="Skins (Dominant / Recessive)",
            required=False,
            placeholder="Dominant / Recessive"
        )
        self.immunity_gene = discord.ui.TextInput(label="Immunity Gene", required=False)
        self.character_sheet_url = discord.ui.TextInput(label="Character Sheet URL", required=False)

        self.add_item(self.dino_name)
        self.add_item(self.subspecies)
        self.add_item(self.skins)
        self.add_item(self.immunity_gene)
        self.add_item(self.character_sheet_url)

    async def on_submit(self, interaction: discord.Interaction):
        async with db.POOL.acquire() as conn:
            alderon_id = get_aid_by_discord(interaction.user.id)  # dashed string
            if alderon_id:
                pinfo = await get_playerinfo(alderon_id)
                growth_val = float(pinfo.get("growth") or 0)
                if growth_val < 0.75:
                    await interaction.response.send_message(
                        "❌ You must be at least Sub Adult (Growth ≥ 0.75) to parent a nest.",
                        ephemeral=True
                    )
                    return

            # Save cosmetic parent details
            await conn.execute("""
                insert into nest_parent_details (
                  nest_id, parent_role, dino_name, subspecies,
                  dominant_skin, recessive_skin, immunity_gene,
                  character_sheet_url
                ) values (
                  $1, $2, $3, $4, $5, $6, $7, $8
                )
                on conflict (nest_id, parent_role) do update set
                  dino_name = excluded.dino_name,
                  subspecies = excluded.subspecies,
                  dominant_skin = excluded.dominant_skin,
                  recessive_skin = excluded.recessive_skin,
                  immunity_gene = excluded.immunity_gene,
                  character_sheet_url = excluded.character_sheet_url
            """,
                self.nest_id,
                self.role,
                self.dino_name.value,
                self.subspecies.value,
                (self.skins.value.split("/", 1)[0].strip() if self.skins.value else None),
                (self.skins.value.split("/", 1)[1].strip() if self.skins.value and "/" in self.skins.value else None),
                self.immunity_gene.value,
                self.character_sheet_url.value
            )

            # 🔑 Update linkage in nests table
            if alderon_id:
                if self.role == "mother":
                    await conn.execute(
                        "update nests set mother_id=$1, mother_alderon_id=$2 where id=$3",
                        interaction.user.id, alderon_id, self.nest_id
                    )
                    if pinfo and pinfo.get("coords"):
                        x, y, z = pinfo["coords"]
                        await conn.execute(
                            "update nests set mother_x=$1, mother_y=$2, mother_z=$3 where id=$4",
                            x, y, z, self.nest_id
                        )
                elif self.role == "father":
                    await conn.execute(
                        "update nests set father_id=$1, father_alderon_id=$2 where id=$3",
                        interaction.user.id, alderon_id, self.nest_id
                    )

            # 🔄 Refresh the nest card
            embed, view = await render_nest_card(conn, self.nest_id)
            await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            f"{self.role.capitalize()} details saved!", ephemeral=True
        )

# --- UX rendering ---
async def render_nest_card(conn, nest_id: int):
    nest = await conn.fetchrow("""
        select n.id, n.status, n.expires_at, n.server_name,
               n.created_by_player_id,
               sp.name as species_name, sp.image_url as species_image_url,
               n.image_url as nest_image_url,
               s.name as season_name
        from nests n
        join species sp on sp.id = n.species_id
        join seasons s on s.id = n.season_id
        where n.id = $1
    """, nest_id)

    eggs = await conn.fetch("""
        select slot_index, claimed_by_player_id
        from eggs
        where nest_id = $1
        order by slot_index
    """, nest_id)

    claimants = [f"<@{row['claimed_by_player_id']}>" for row in eggs if row['claimed_by_player_id']]

    embed = discord.Embed(
        title=f"{nest['species_name']} Nest",
        description=f"Season: {nest['season_name']}\nStatus: {nest['status'].upper()}",
        color=discord.Color.green() if nest['status'] == "open" else discord.Color.red()
    )
    embed.add_field(
        name="Eggs Available",
        value=str(sum(1 for e in eggs if not e['claimed_by_player_id'])),
        inline=True
    )
    embed.add_field(
        name="Claimants",
        value=", ".join(claimants) if claimants else "None yet",
        inline=True
    )
    embed.set_footer(text=f"Server: {nest['server_name']} | Expires {nest['expires_at']}")

    # 👇 Prefer nest image if supplied, else species default
    chosen_image = (nest["nest_image_url"] or nest["species_image_url"])
    if chosen_image:
        embed.set_image(url=chosen_image.strip())
    else:
        embed.add_field(name="Image", value="No image available", inline=False)

    # Parent details
    details = await conn.fetch("""
        select parent_role, dino_name, subspecies, dominant_skin, recessive_skin,
               immunity_gene, character_sheet_url
        from nest_parent_details
        where nest_id = $1
    """, nest_id)
    for row in details:
        block = (
            f"**Name:** {row['dino_name'] or '—'}\n"
            f"**Subspecies:** {row['subspecies'] or '—'}\n"
            f"**Skins:** {row['dominant_skin'] or '—'} / {row['recessive_skin'] or '—'}\n"
            f"**Immunity:** {row['immunity_gene'] or '—'}\n"
        )
        if row['character_sheet_url']:
            block += f"\n[Character Sheet]({row['character_sheet_url']})"
        embed.add_field(
            name=f"{row['parent_role'].capitalize()} Details",
            value=block,
            inline=False
        )

    # ✅ Always return NestView with correct creator_id
    view = NestView(nest_id, nest["created_by_player_id"])
    return embed, view

# --- Slash command: /setseason (admin only) ---
@tree.command(name="setseason", description="Set the active season")
@app_commands.choices(season=[
    app_commands.Choice(name="Spring", value="Spring"),
    app_commands.Choice(name="Summer", value="Summer"),
    app_commands.Choice(name="Autumn", value="Autumn"),
    app_commands.Choice(name="Winter", value="Winter"),
])
async def setseason(interaction: discord.Interaction, season: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only server administrators can set the active season.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with db.POOL.acquire() as conn:
        # 🔒 Ensure only one active season
        await conn.execute("UPDATE seasons SET is_active = false")
        await conn.execute(
            "UPDATE seasons SET is_active = true WHERE lower(name) = lower($1)",
            season.value
        )

        active = await conn.fetchrow(
            "SELECT id, name FROM seasons WHERE is_active = true LIMIT 1"
        )

        # 🧹 Clean out all old stats for a fresh season
        await conn.execute("DELETE FROM player_season_species_stats")

        # 📜 Log the season change with timestamp
        try:
            await conn.execute(
                "INSERT INTO season_changes (changed_by, season_name, changed_at) VALUES ($1, $2, now())",
                interaction.user.id, season.value
            )
        except Exception:
            pass

    if active:
        await interaction.followup.send(
            f"Season set to {active['name']} — all clutch stats reset. Change logged at {active['name']} season start.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"No season named {season.value} found",
            ephemeral=True
        )

# --- Slash command: /anthranest ---
@tree.command(name="anthranest", description="Create a nest")
@app_commands.describe(
    asexual="Whether the nest is asexual",
    image_url="Optional custom image URL for the nest card",
    additional_info="Optional player-written blurb to include in the nest post",
    egg_count_override="Optional egg count (must be less than the species max)"
)
async def anthranest_slash(
    interaction: discord.Interaction,
    asexual: bool = False,
    image_url: str | None = None,
    additional_info: str | None = None,
    egg_count_override: int | None = None
):
    async with db.POOL.acquire() as conn:
        alderon_id = get_aid_by_discord(interaction.user.id)
        if not alderon_id:
            await interaction.response.send_message(
                "No Alderon ID registered for you. Please register first.",
                ephemeral=True
            )
            return

        # Pull species + coords from RCON
        pinfo = await get_playerinfo(alderon_id)
        if not pinfo or not pinfo.get("species_code"):
            await interaction.response.send_message(
                "Could not determine your species from RCON.",
                ephemeral=True
            )
            return

        # Growth requirement check (cast to float)
        growth_val = float(pinfo.get("growth") or 0)
        if growth_val < 0.75:
            await interaction.response.send_message(
                "❌ You must be Sub Adult or above to create a nest.",
                ephemeral=True
            )
            return

        species_row = await conn.fetchrow(
            "select id, name, image_url from species where code=$1",
            pinfo["species_code"]
        )
        if not species_row:
            await interaction.response.send_message(
                f"Species {pinfo['species_code']} not recognized in database.",
                ephemeral=True
            )
            return
        species_id = species_row["id"]

        rule = await db.get_active_rules(conn, species_id)
        if not rule or not rule["can_nest"]:
            await interaction.response.send_message(
                f"Nesting for {species_row['name']} is disabled this season.",
                ephemeral=True
            )
            return

        max_clutches = rule["max_clutches_per_player"] or 0
        default_egg_count = rule["egg_count"] or 0

        # Validate optional override
        if egg_count_override is not None:
            if egg_count_override <= 0 or egg_count_override >= default_egg_count:
                await interaction.response.send_message(
                    f"❌ Egg count override must be greater than 0 and less than the normal maximum ({default_egg_count}).",
                    ephemeral=True
                )
                return
            egg_count = egg_count_override
        else:
            egg_count = default_egg_count

        # Use RCON coords if available, fallback to (0,0,0)
        coords = pinfo["coords"] if pinfo.get("coords") else (0, 0, 0)

        # Choose nest image: player-provided or species default
        chosen_image = image_url.strip() if image_url else species_row["image_url"]

        # Transaction-safe clutch + nest creation
        nest_id = await db.start_nest_transaction(
            conn,
            interaction.user.id,   # player_id
            species_id,
            interaction.user.id,   # mother_id always = invoking player
            None if asexual else None,  # father_id stays None for now
            interaction.user.id,   # creator_id
            coords,
            SERVER_NAME,
            asexual,
            max_clutches,
            chosen_image,          # pass image into nest record
            additional_info        # pass player blurb into nest record
        )

        if nest_id is None:
            await interaction.response.send_message(
                f"You have reached the maximum of {max_clutches} clutches for {species_row['name']} this season.",
                ephemeral=True
            )
            return

        if egg_count > 0:
            await conn.execute(
                "insert into eggs (nest_id, slot_index) select $1, generate_series(1, $2)",
                nest_id, egg_count
            )

        embed, _ = await render_nest_card(conn, nest_id)
        # Append player blurb if present
        if additional_info:
            embed.add_field(name="Player Note", value=additional_info, inline=False)

        view = NestView(nest_id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)
        await db.set_nest_message(conn, nest_id, interaction.channel.id, interaction.id)
        
# --- Button interactions via NestView ---
class NestView(discord.ui.View):
    def __init__(self, nest_id: int, creator_id: int):
        super().__init__(timeout=None)
        self.nest_id = nest_id
        self.creator_id = creator_id

    @discord.ui.button(label="🥚 Claim Egg", style=discord.ButtonStyle.primary)
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.POOL.acquire() as conn:
            try:
                egg_id = await db.claim_first_egg(conn, self.nest_id, interaction.user.id)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return

            if egg_id is None:
                await interaction.response.send_message(
                    "❌ No eggs available to claim in this nest.",
                    ephemeral=True
                )
                return

            # 🔄 Refresh embed after claim
            embed, view = await render_nest_card(conn, self.nest_id)
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(f"🥚 You successfully claimed egg #{egg_id}!", ephemeral=True)

    @discord.ui.button(label="❌ Unclaim Egg", style=discord.ButtonStyle.secondary)
    async def unclaim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.POOL.acquire() as conn:
            slot_index = await db.unclaim_egg(conn, self.nest_id, interaction.user.id)
            embed, view = await render_nest_card(conn, self.nest_id)

        if slot_index is not None:
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(f"You released your claim on egg {slot_index}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "You don’t currently have a claimed egg in this nest.",
                ephemeral=True
            )

    @discord.ui.button(label="🐣 Hatch", style=discord.ButtonStyle.success)
    async def hatch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.POOL.acquire() as conn:
            # Pull mother’s nest coordinates
            nest = await conn.fetchrow(
                "select mother_x, mother_y, mother_z from nests where id=$1",
                self.nest_id
            )
            if not nest:
                await interaction.response.send_message("Nest not found.", ephemeral=True)
                return

            alderon_id = get_aid_by_discord(interaction.user.id)
            if not alderon_id:
                await interaction.response.send_message("No Alderon ID registered for you.", ephemeral=True)
                return

            # ✅ Ensure all three coordinates are present
            if nest["mother_x"] is not None and nest["mother_y"] is not None and nest["mother_z"] is not None:
                # Reset growth to hatchling
                await setattr_growth(alderon_id, 0)
                # Teleport to mother’s nest coordinates
                await teleport(alderon_id, nest["mother_x"], nest["mother_y"], nest["mother_z"])
                # Mark egg as hatched in DB
                egg_id = await db.mark_egg_hatched(conn, self.nest_id, interaction.user.id)
            else:
                await interaction.response.send_message(
                    "Mother’s nest location has not been set yet.",
                    ephemeral=True
                )
                return

        # ✅ Respond to player
        if egg_id:
            await interaction.response.send_message(
                f"🐣 You hatched from egg {egg_id} and were teleported to the nest!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You don’t have a claimed egg in this nest.",
                ephemeral=True
            )

    @discord.ui.button(label="👩 Mother Details", style=discord.ButtonStyle.secondary)
    async def mother_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ParentDetailsModal(self.nest_id, role="mother"))

    @discord.ui.button(label="👨 Father Details", style=discord.ButtonStyle.secondary)
    async def father_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ParentDetailsModal(self.nest_id, role="father"))

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the nest creator can close this nest.",
                ephemeral=True
            )
            return

        async with db.POOL.acquire() as conn:
            await conn.execute("update nests set status='expired' where id=$1", self.nest_id)
            embed, view = await render_nest_card(conn, self.nest_id)
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send("Nest has been closed.", ephemeral=True)

# --- Background Tasks ---
async def nest_expiry_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with db.POOL.acquire() as conn:
                expired = await db.expire_nests(conn)
                for row in expired:
                    try:
                        channel = bot.get_channel(row["discord_channel_id"])
                        if channel:
                            msg = await channel.fetch_message(row["discord_message_id"])
                            embed, view = await render_nest_card(conn, row["id"])
                            for item in view.children:
                                item.disabled = True
                            await msg.edit(embed=embed, view=view)
                    except Exception as e:
                        print(f"[Expiry] Failed to update nest {row['id']}: {e}")
        except Exception as e:
            print(f"[Expiry] Task loop error: {e}")
        await asyncio.sleep(60)

# --- Sync commands on ready ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Initialize DB pool and expiry task
    await db.init_db_pool()
    bot.loop.create_task(nest_expiry_task())

    # Bulk sync players from Google Sheet into DB
    sheet_rows = load_google_sheet()
    async with db.POOL.acquire() as conn:   # <-- use POOL here
        await db.bulk_sync_players(conn, sheet_rows)
    print(f"[Startup] Synced {len(sheet_rows)} players from Google Sheet into DB")

    # Sync slash commands to your guild
    GUILD_ID = 1374722200053088306
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    print(f"[Slash] Synced {len(synced)} commands to guild {GUILD_ID}: {[cmd.name for cmd in synced]}")

# --- Startup ---
bot.run(DISCORD_TOKEN)