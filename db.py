import os
import asyncpg
import ssl

# Global connection pool
POOL: asyncpg.Pool | None = None

async def init_db_pool():
    """
    Initialize a global asyncpg connection pool using DATABASE_URL from environment.
    Example DSN:
    postgresql://postgres:YOURPASSWORD@db.YOURHOST.supabase.co:5432/postgres
    """
    global POOL
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL or DB_DSN environment variable not set")

    ssl_context = ssl.create_default_context(cafile="/app/prod-ca-2021.crt")

    POOL = await asyncpg.create_pool(
        dsn=dsn,
        ssl=ssl_context,
        min_size=2,
        max_size=10,
        timeout=10,
        statement_cache_size=0,
        max_inactive_connection_lifetime=300,
        # 👇 Force schema search path
        server_settings={"search_path": "public"}
    )
    print("[SQL] Connection pool initialized")

    # --- Quick diagnostic check ---
    async with POOL.acquire() as conn:
        who = await conn.fetchval("select current_user;")
        db  = await conn.fetchval("select current_database();")
        path = await conn.fetchval("show search_path;")
        print(f"[SQL] Connected as role: {who}, database: {db}, search_path: {path}")


# ---------- Queries ----------

async def bulk_sync_players(conn, sheet_rows: list[dict]):
    """
    Bulk sync players from cached Google Sheet into the players table.
    Each row should have at least 'discord_id' and 'aid'.
    """
    for row in sheet_rows:
        discord_id = int(row["discord_id"])
        aid = row.get("aid")

        await conn.execute("""
            insert into players (id, aid)
            values ($1, $2)
            on conflict (id) do update set aid = excluded.aid
        """, discord_id, aid)

        await conn.execute("""
            insert into players (id, aid)
            values ($1, $2)
            on conflict (id) do update set aid = excluded.aid
        """, discord_id, aid)

async def get_active_rules(conn, species_id: int):
    """
    Return egg_count, max_clutches_per_player, and can_nest for the active season/species.
    """
    sql = """
    select r.egg_count, r.max_clutches_per_player, r.can_nest
    from active_season s
    join season_species_rules r on r.season_id = s.season_id
    where r.species_id = $1
    """
    return await conn.fetchrow(sql, species_id)


async def create_nest(conn, species_id: int, mother_id: int, father_id: int,
                      coords: tuple, server_name: str, asexual: bool):
    """
    Create a nest record and return its ID.
    """
    sql = """
    insert into nests (season_id, species_id, mother_id, father_id,
                       created_by_player_id, mother_x, mother_y, mother_z,
                       server_name, asexual, created_at, expires_at, status)
    values ((select season_id from active_season), $1, $2, $3,
            $2, $4, $5, $6, $7, $8, now(), now() + interval '30 minutes', 'open')
    returning id
    """
    return await conn.fetchval(sql, species_id, mother_id, father_id,
                               coords[0], coords[1], coords[2],
                               server_name, asexual)


async def set_nest_message(conn, nest_id: int, channel_id: int, message_id: int):
    """
    Store the Discord channel/message IDs for a nest so we can edit later.
    """
    sql = "update nests set discord_channel_id=$2, discord_message_id=$3 where id=$1"
    await conn.execute(sql, nest_id, channel_id, message_id)


async def expire_nests(conn):
    """
    Mark all nests past expires_at as expired.
    Returns list of expired nest rows with channel/message IDs.
    """
    sql = """
    update nests
    set status = 'expired'
    where status = 'open' and expires_at < now()
    returning id, discord_channel_id, discord_message_id
    """
    return await conn.fetch(sql)


async def claim_first_egg(conn, nest_id: int, player_id: int):
    """
    Claim the first available egg in a nest for a player.
    Returns the egg ID or None if no eggs left.
    """
    sql = """
    update eggs
    set claimed_by_player_id = $2, claimed_at = now()
    where id = (
      select id from eggs
      where nest_id = $1 and claimed_by_player_id is null
      order by slot_index
      limit 1
    )
    returning id
    """
    return await conn.fetchval(sql, nest_id, player_id)


async def unclaim_egg(conn, nest_id: int, player_id: int):
    # Find the egg claimed by this player in this nest
    row = await conn.fetchrow("""
        select slot_index from eggs
        where nest_id=$1 and claimed_by_player_id=$2
    """, nest_id, player_id)
    if not row:
        return None  # no egg claimed

    # Unclaim it
    await conn.execute("""
        update eggs
        set claimed_by_player_id=null
        where nest_id=$1 and slot_index=$2
    """, nest_id, row["slot_index"])
    return row["slot_index"]


async def bump_clutch_counter(conn, player_id: int, species_id: int, max_clutches: int):
    """
    Ensure a stats row exists, then increment clutches_started.
    Returns True if incremented, False if cap reached.
    """
    # Ensure row exists with clutches_started=0
    await conn.execute("""
        insert into player_season_species_stats (season_id, player_id, species_id, clutches_started)
        values ((select season_id from active_season), $1, $2, 0)
        on conflict (season_id, player_id, species_id) do nothing
    """, player_id, species_id)

    # Now try to increment
    sql = """
    update player_season_species_stats
    set clutches_started = clutches_started + 1
    where season_id = (select season_id from active_season)
      and player_id = $1
      and species_id = $2
      and clutches_started < $3
    returning clutches_started
    """
    result = await conn.fetchval(sql, player_id, species_id, max_clutches)
    return result is not None


async def mark_egg_hatched(conn, nest_id: int, player_id: int):
    """
    Mark the player's claimed egg in a nest as hatched.
    Returns the egg ID if updated, or None if no claimed egg found.
    """
    sql = """
    update eggs
    set hatched_at = now()
    where nest_id = $1 and claimed_by_player_id = $2
    returning id
    """
    return await conn.fetchval(sql, nest_id, player_id)