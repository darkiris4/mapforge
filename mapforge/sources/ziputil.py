"""Read remote zips with HTTP range requests (no full download)."""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import httpx


@dataclass
class ZipMember:
    name: str
    size: int  # uncompressed
    csize: int
    method: int
    offset: int  # local header offset


def list_remote_zip(client: httpx.Client, url: str) -> list[ZipMember]:
    """Members of a remote zip, from its central directory."""
    r = client.get(url, headers={"Range": "bytes=-65536"})
    r.raise_for_status()
    tail = r.content
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError(f"not a zip (no end-of-central-directory): {url}")
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12 : eocd + 20])
    start = eocd - cd_size
    if start < 0:  # central directory bigger than our tail read; fetch it exactly
        r = client.get(url, headers={"Range": f"bytes={cd_off}-{cd_off + cd_size - 1}"})
        r.raise_for_status()
        cd = r.content
    else:
        cd = tail[start:eocd]
    out: list[ZipMember] = []
    p = 0
    while p + 46 <= len(cd) and cd[p : p + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", cd[p + 10 : p + 12])[0]
        csz, usz = struct.unpack("<II", cd[p + 20 : p + 28])
        nl, el, cl = struct.unpack("<HHH", cd[p + 28 : p + 34])
        off = struct.unpack("<I", cd[p + 42 : p + 46])[0]
        out.append(ZipMember(cd[p + 46 : p + 46 + nl].decode("utf-8", "replace"), usz, csz, method, off))
        p += 46 + nl + el + cl
    return out


def read_remote_member(client: httpx.Client, url: str, m: ZipMember, max_bytes: int = 1 << 20) -> bytes:
    """Fetch and inflate one small member."""
    if m.csize > max_bytes:
        raise ValueError(f"{m.name} too large to fetch inline")
    r = client.get(url, headers={"Range": f"bytes={m.offset}-{m.offset + 30 + 1024 + m.csize}"})
    r.raise_for_status()
    d = r.content
    nl, el = struct.unpack("<HH", d[26:30])
    raw = d[30 + nl + el : 30 + nl + el + m.csize]
    if m.method == 0:
        return raw
    if m.method == 8:
        return zlib.decompressobj(-15).decompress(raw)
    raise ValueError(f"unsupported zip compression method {m.method}")
