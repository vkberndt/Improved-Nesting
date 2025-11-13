import asyncio
import os

class RCONClient:
    def __init__(self, host=None, port=None, password=None):
        self.host = host or os.getenv("RCON_HOST")
        self.port = int(port or os.getenv("RCON_PORT", 25575))
        self.password = password or os.getenv("RCON_PASSWORD")
        self.reader = None
        self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        # Send login packet
        await self._send_packet(3, self.password)
        resp_id, _ = await self._read_packet()
        if resp_id == -1:
            raise Exception("RCON authentication failed")

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    async def command(self, cmd: str) -> str:
        """
        Send a command string to the RCON server and return its response.
        """
        await self._send_packet(2, cmd)
        _, data = await self._read_packet()
        return data

    # ---------- Internal helpers ----------

    async def _send_packet(self, out_type: int, out_data: str):
        out_data_bytes = out_data.encode("utf8")
        length = 10 + len(out_data_bytes)
        req_id = 0xBADC0DE  # arbitrary request ID
        packet = (
            length.to_bytes(4, "little")
            + req_id.to_bytes(4, "little")
            + out_type.to_bytes(4, "little")
            + out_data_bytes
            + b"\x00\x00"
        )
        self.writer.write(packet)
        await self.writer.drain()

    async def _read_packet(self):
        length_bytes = await self.reader.readexactly(4)
        length = int.from_bytes(length_bytes, "little")
        body = await self.reader.readexactly(length)
        resp_id = int.from_bytes(body[0:4], "little")
        resp_type = int.from_bytes(body[4:8], "little")
        data = body[8:-2].decode("utf8")
        return resp_id, data

# ---------- Convenience wrappers ----------

async def playerinfo(alderon_id: str) -> dict:
    """
    Run /playerinfo <AID> and parse the response.
    """
    client = RCONClient()
    await client.connect()
    raw = await client.command(f"/playerinfo {alderon_id}")
    await client.close()

    # Example parsing: adjust to actual server output format
    # Suppose raw looks like: "Species: Barsboldia, Position: 100 200 300"
    info = {}
    for line in raw.splitlines():
        if "Species:" in line:
            info["species_code"] = line.split(":")[1].strip().lower()
        if "Position:" in line:
            coords = line.split(":")[1].strip().split()
            info["x"], info["y"], info["z"] = map(float, coords)
    return info

async def get_position(alderon_id: str):
    info = await playerinfo(alderon_id)
    return info["x"], info["y"], info["z"]

async def setattr_growth(alderon_id: str, value: int = 0):
    client = RCONClient()
    await client.connect()
    await client.command(f"/setattr {alderon_id} growth {value}")
    await client.close()

async def teleport(alderon_id: str, x: float, y: float, z: float):
    client = RCONClient()
    await client.connect()
    await client.command(f"/teleport {alderon_id} {x} {y} {z}")
    await client.close()