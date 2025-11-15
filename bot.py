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

# --- Parent Details Modal ---
class ParentDetailsModal(discord.ui.Modal):
    def __init__(self, nest_id: int, role: str):
        super().__init__(title=f"{role.capitalize()} Details")
        self.nest_id = nest_id
        self.role = role

        # Add text inputs for parent details
        self.add_item(discord.ui.TextInput(label="Dino Name", required=False))
        self.add_item(discord.ui.TextInput(label="Subspecies", required=False))
        self.add_item(discord.ui.TextInput(label="Dominant Skin", required=False))
        self.add_item(discord.ui.TextInput(label="Recessive Skin", required=False))
        self.add_item(discord.ui.TextInput(label="Immunity Gene", required=False))
        self.add_item(discord.ui.TextInput(label="Character Sheet URL", required=False))
        self.add_item(discord.ui.TextInput(label="Mutations", required=False))

    async def on_submit(self, interaction: discord.Interaction):
        async with db.POOL.acquire() as conn:
            # Save parent details
            await conn.execute("""
                insert into nest_parent_details (
                  nest_id, parent_role, dino_name, subspecies,
                  dominant_skin, recessive_skin, immunity_gene,
                  character_sheet_url, mutations
                ) values (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9
                )
                on conflict (nest_id, parent_role) do update set
                  dino_name = excluded.dino_name,
                  subspecies = excluded.subspecies,
                  dominant_skin = excluded.dominant_skin,
                  recessive_skin = excluded.recessive_skin,
                  immunity_gene = excluded.immunity_gene,
                  character_sheet_url = excluded.character_sheet_url,
                  mutations = excluded.mutations
            """, self.nest_id, self.role,
                 self.children[0].value,  # Dino Name
                 self.children[1].value,  # Subspecies
                 self.children[2].value,  # Dominant Skin
                 self.children[3].value,  # Recessive Skin
                 self.children[4].value,  # Immunity Gene
                 self.children[5].value,  # Character Sheet URL
                 self.children[6].value)  # Mutations

            # If this is the mother, also update nest coords from RCON
            if self.role == "mother":
                alderon_id = get_aid_by_discord(interaction.user.id)
                if alderon_id:
                    pinfo = await get_playerinfo(alderon_id)
                    if pinfo and pinfo["coords"]:
                        x, y, z = pinfo["coords"]
                        await conn.execute("""
                            update nests
                            set mother_x=$1, mother_y=$2, mother_z=$3
                            where id=$4
                        """, x, y, z, self.nest_id)

        await interaction.response.send_message(
            f"{self.role.capitalize()} details saved!", ephemeral=True
        )

# --- UX rendering ---
async def render_nest_card(conn, nest_id: int):
    nest = await conn.fetchrow("""
        select n.id, n.status, n.expires_at, n.server_name,
               n.created_by_player_id,
               sp.name as species_name, sp.image_url,
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
    embed.add_field(name="Eggs Available", value=str(sum(1 for e in eggs if not e['claimed_by_player_id'])), inline=True)
    embed.add_field(name="Claimants", value=", ".join(claimants) if claimants else "None yet", inline=True)
    embed.set_footer(text=f"Server: {nest['server_name']} | Expires {nest['expires_at']}")

    if nest["image_url"]:
        embed.set_image(url=nest["image_url"])
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
        embed.add_field(name=f"{row['parent_role'].capitalize()} Details", value=block, inline=False)

    # Buttons
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🥚 Claim Egg", style=discord.ButtonStyle.primary, custom_id=f"claim:{nest_id}"))
    view.add_item(discord.ui.Button(label="👩 Mother Details", style=discord.ButtonStyle.secondary, custom_id=f"parent:{nest_id}:mother"))
    view.add_item(discord.ui.Button(label="👨 Father Details", style=discord.ButtonStyle.secondary, custom_id=f"parent:{nest_id}:father"))
    view.add_item(discord.ui.Button(label="🐣 Hatch", style=discord.ButtonStyle.success, custom_id=f"hatch:{nest_id}"))
    view.add_item(discord.ui.Button(label="❌ Close", style=discord.ButtonStyle.danger,
                                    custom_id=f"close:{nest_id}:{nest['created_by_player_id']}"))
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
        await interaction.response.send_message("Only server administrators can set the active season.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    async with db.POOL.acquire() as conn:
        await conn.execute("UPDATE seasons SET is_active = (lower(name) = lower($1))", season.value)
        active = await conn.fetchrow("SELECT id, name FROM seasons WHERE is_active = true LIMIT 1")
        try:
            await conn.execute(
                "INSERT INTO season_changes (changed_by, season_name) VALUES ($1, $2)",
                interaction.user.id, season.value
            )
        except Exception:
            pass

    if active:
        await interaction.followup.send(f"Season set to {active['name']}", ephemeral=True)
    else:
        await interaction.followup.send(f"No season named {season.value} found", ephemeral=True)


# --- Slash command: /anthranest ---
@tree.command(name="anthranest", description="Create a nest")
@app_commands.describe(asexual="Whether the nest is asexual")
async def anthranest_slash(interaction: discord.Interaction, asexual: bool=False):
    async with db.POOL.acquire() as conn:
        alderon_id = get_aid_by_discord(interaction.user.id)
        if not alderon_id:
            await interaction.response.send_message(
                "No Alderon ID registered for you. Please register first.",
                ephemeral=True
            )
            return

        # ✅ Pull species + coords from RCON
        pinfo = await get_playerinfo(alderon_id)
        if not pinfo or not pinfo["species_code"]:
            await interaction.response.send_message(
                "Could not determine your species from RCON.",
                ephemeral=True
            )
            return

        species_row = await conn.fetchrow(
            "select id, name from species where code=$1", pinfo["species_code"]
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
        if max_clutches > 0:
            ok = await db.bump_clutch_counter(conn, interaction.user.id, species_id, max_clutches)
            if not ok:
                await interaction.response.send_message(
                    f"You have reached the maximum of {max_clutches} clutches for {species_row['name']} this season.",
                    ephemeral=True
                )
                return

        # ✅ Use RCON coords if available, fallback to (0,0,0)
        coords = pinfo["coords"] if pinfo.get("coords") else (0, 0, 0)

        nest_id = await db.create_nest(
            conn,
            interaction.user.id,
            species_id,
            None,
            None,
            coords,
            SERVER_NAME,
            asexual
        )

        egg_count = rule["egg_count"] or 0
        if egg_count > 0:
            await conn.execute(
                "insert into eggs (nest_id, slot_index) select $1, generate_series(1, $2)",
                nest_id, egg_count
            )

        embed, _ = await render_nest_card(conn, nest_id)
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
            egg_id = await db.claim_first_egg(conn, self.nest_id, interaction.user.id)
            embed, view = await render_nest_card(conn, self.nest_id)
            await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="❌ Unclaim Egg", style=discord.ButtonStyle.secondary)
    async def unclaim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.POOL.acquire() as conn:
            slot_index = await db.unclaim_egg(conn, self.nest_id, interaction.user.id)
            embed, view = await render_nest_card(conn, self.nest_id)

        if slot_index is not None:
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(f"You released your claim on egg {slot_index}.", ephemeral=True)
        else:
            await interaction.response.send_message("You don’t currently have a claimed egg in this nest.", ephemeral=True)

    @discord.ui.button(label="🐣 Hatch", style=discord.ButtonStyle.success)
    async def hatch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.POOL.acquire() as conn:
            nest = await conn.fetchrow("select mother_x, mother_y, mother_z from nests where id=$1", self.nest_id)
            if not nest:
                await interaction.response.send_message("Nest not found.", ephemeral=True)
                return

            alderon_id = get_aid_by_discord(interaction.user.id)
            if not alderon_id:
                await interaction.response.send_message("No Alderon ID registered for you.", ephemeral=True)
                return

            if nest["mother_x"] is not None:
                await setattr_growth(alderon_id, 0)
                await teleport(alderon_id, nest["mother_x"], nest["mother_y"], nest["mother_z"])
                egg_id = await db.mark_egg_hatched(conn, self.nest_id, interaction.user.id)
            else:
                await interaction.response.send_message("Mother’s nest location has not been set yet.", ephemeral=True)
                return

        if egg_id:
            await interaction.response.send_message(f"You hatched from egg {egg_id} and were teleported to the nest!", ephemeral=True)
        else:
            await interaction.response.send_message("You don’t have a claimed egg in this nest.", ephemeral=True)

    @discord.ui.button(label="👩 Mother Details", style=discord.ButtonStyle.secondary)
    async def mother_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ParentDetailsModal(self.nest_id, role="mother"))

    @discord.ui.button(label="👨 Father Details", style=discord.ButtonStyle.secondary)
    async def father_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ParentDetailsModal(self.nest_id, role="father"))

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("Only the nest creator can close this nest.", ephemeral=True)
            return

        async with db.POOL.acquire() as conn:
            await conn.execute("update nests set status='expired' where id=$1", self.nest_id)
            embed, view = await render_nest_card(conn, self.nest_id)
            await interaction.response.edit_message(embed=embed, view=view)

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