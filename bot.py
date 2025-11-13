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
    for d_id, aid in zip(col_discord, col_aid):
        if d_id.strip() == str(discord_id).strip():
            return aid.strip()
    return None

# --- RCON helpers ---
def _parse_species(text: str):
    m = re.search(r"Dinosaur:\s*([^/]+?)(?:\s*/|$)", text, flags=re.IGNORECASE)
    return m.group(1).strip().lower() if m else None

def _parse_position(text: str):
    m = re.search(r"Location:\s*\(X=([-\d\.]+)\s*Y=([-\d\.]+)\s*Z=([-\d\.]+)\)", text)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    return (0.0, 0.0, 0.0)

async def get_playerinfo(aid: str):
    try:
        def _run():
            with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
                return mcr.command(f"playerinfo {aid}") or ""
        resp = await asyncio.to_thread(_run)
    except Exception as e:
        print("[RCON] Error:", e)
        return None
    species = _parse_species(resp)
    coords = _parse_position(resp)
    return {"species_code": species, "coords": coords}

async def setattr_growth(aid: str, value: int = 0):
    def _run():
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            mcr.command(f"setattr {aid} growth {value}")
    await asyncio.to_thread(_run)

async def teleport(aid: str, x: float, y: float, z: float):
    def _run():
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            mcr.command(f"teleport {aid} {x} {y} {z}")
    await asyncio.to_thread(_run)
        
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
@bot.tree.command(name="setseason", description="Set the active season")
@app_commands.choices(season=[
    app_commands.Choice(name="Spring", value="Spring"),
    app_commands.Choice(name="Summer", value="Summer"),
    app_commands.Choice(name="Autumn", value="Autumn"),
    app_commands.Choice(name="Winter", value="Winter"),
])
async def setseason(interaction: discord.Interaction, season: app_commands.Choice[str]):
    # Permission check
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only server administrators can set the active season.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        async with db.POOL.acquire() as conn:
            # Flip active flag in one statement (case-insensitive match)
            await conn.execute("UPDATE seasons SET is_active = (lower(name) = lower($1))", season.value)

            # Fetch the active season to confirm
            active = await conn.fetchrow("SELECT id, name FROM seasons WHERE is_active = true LIMIT 1")

            # Try to write an audit entry if the table exists; ignore errors if it doesn't
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
    except Exception as e:
        await interaction.followup.send(f"Failed to set season: {e}", ephemeral=True)

# --- Commands ---
@bot.command(name="anthranest")
async def anthranest(ctx, asexual: bool=False):
    async with db.POOL.acquire() as conn:
        alderon_id = get_aid_by_discord(ctx.author.id)
        if not alderon_id:
            await ctx.send("No Alderon ID registered for you. Please register first.")
            return

        pinfo = await get_playerinfo(alderon_id)
        if not pinfo or not pinfo["species_code"]:
            await ctx.send("Could not determine your species from RCON.")
            return

        species_row = await conn.fetchrow("select id, name from species where code=$1", pinfo["species_code"])
        if not species_row:
            await ctx.send(f"Species {pinfo['species_code']} not recognized in database.")
            return
        species_id = species_row["id"]

        # Seasonal rule check
        rule = await db.get_active_rules(conn, species_id)
        if not rule:
            await ctx.send(f"No seasonal rules configured for {species_row['name']} in the active season.")
            return

        if not rule["can_nest"]:
            await ctx.send(f"Nesting for {species_row['name']} is disabled this season.")
            return

        max_clutches = rule["max_clutches_per_player"] or 0
        if max_clutches > 0:
            ok = await db.bump_clutch_counter(conn, ctx.author.id, species_id, max_clutches)
            if not ok:
                await ctx.send(f"You have reached the maximum of {max_clutches} clutches for {species_row['name']} this season.")
                return

        # Create nest
        nest_id = await db.create_nest(conn, ctx.author.id, species_id, None, None,
                                       (0,0,0), SERVER_NAME, asexual)

        # Insert eggs if egg_count > 0
        egg_count = rule["egg_count"] or 0
        if egg_count > 0:
            await conn.execute(
                "insert into eggs (nest_id, slot_index) select $1, generate_series(1, $2)",
                nest_id, egg_count
            )

        # Render UX card
        embed, view = await render_nest_card(conn, nest_id)
        msg = await ctx.send(embed=embed, view=view)
        await db.set_nest_message(conn, nest_id, ctx.channel.id, msg.id)

# --- Button interactions ---
@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Only handle component interactions here; guard against other interaction types
    if not interaction.data or "component_type" not in interaction.data:
        return

    if interaction.data["component_type"] == 2:  # button
        parts = interaction.data["custom_id"].split(":")
        action = parts[0]
        nest_id = int(parts[1])

        if action == "claim":
            conn = await db.connect()
            egg_id = await db.claim_first_egg(conn, nest_id, interaction.user.id)
            embed, view = await render_nest_card(conn, nest_id)
            await interaction.response.edit_message(embed=embed, view=view)
            await conn.close()

        elif action == "unclaim":
            conn = await db.connect()
            slot_index = await db.unclaim_egg(conn, nest_id, interaction.user.id)
            embed, view = await render_nest_card(conn, nest_id)
            await conn.close()

            if slot_index is not None:
                await interaction.response.edit_message(embed=embed, view=view)
                await interaction.followup.send(
                    f"You have released your claim on egg {slot_index}.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "You don’t currently have a claimed egg in this nest.",
                    ephemeral=True
                )

        elif action == "hatch":
            conn = await db.connect()
            nest = await conn.fetchrow(
                "select mother_x, mother_y, mother_z from nests where id=$1", nest_id
            )
            if not nest:
                await conn.close()
                await interaction.response.send_message("Nest not found.", ephemeral=True)
                return

            alderon_id = get_aid_by_discord(interaction.user.id)
            if not alderon_id:
                await conn.close()
                await interaction.response.send_message("No Alderon ID registered for you.", ephemeral=True)
                return

            # Ensure mother coords exist before teleport
            if nest["mother_x"] is not None and nest["mother_y"] is not None and nest["mother_z"] is not None:
                # Reset growth and teleport only when coords are valid
                await setattr_growth(alderon_id, 0)
                await teleport(alderon_id, nest["mother_x"], nest["mother_y"], nest["mother_z"])
            else:
                await conn.close()
                await interaction.response.send_message(
                    "Mother’s nest location has not been set yet. Please have the mother fill in her details.",
                    ephemeral=True
                )
                return

            # Mark egg as hatched in DB
            egg_id = await db.mark_egg_hatched(conn, nest_id, interaction.user.id)
            await conn.close()

            if egg_id:
                await interaction.response.send_message(
                    f"You have hatched from egg {egg_id} and been teleported to the nest!",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "You don’t have a claimed egg in this nest.",
                    ephemeral=True
                )

        elif action == "parent":
            role = parts[2]  # "mother" or "father"
            await interaction.response.send_modal(ParentDetailsModal(nest_id, role=role))

        elif action == "close":
            creator_id = int(parts[2])
            if interaction.user.id != creator_id:
                await interaction.response.send_message("Only the nest creator can close this nest.", ephemeral=True)
                return
            conn = await db.connect()
            await conn.execute("update nests set status='expired' where id=$1", nest_id)
            embed, view = await render_nest_card(conn, nest_id)
            await interaction.response.edit_message(embed=embed, view=view)
            await conn.close()

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
    await db.init_db_pool()
    bot.loop.create_task(nest_expiry_task())

# --- Startup ---
bot.run(DISCORD_TOKEN)