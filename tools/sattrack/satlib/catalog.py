# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""Satellite lookup: TLEs from CelesTrak, transmitters from SatNOGS DB.
TLEs are cached so passes can be computed offline for a while."""
import json
import time
import urllib.parse
import urllib.request

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php"
SATNOGS = "https://db.satnogs.org/api/transmitters/"
TLE_FRESH_S = 24 * 3600          # refetch after a day
TLE_MAX_AGE_S = 14 * 86400       # refuse older cached TLEs
# SatNOGS modes that are carried on plain FM (packet, SSTV, narrow FM)
FM_CARRIED = {"FMN", "AFSK", "SSTV", "AFSK S-NET", "AFSK SALSAT"}


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "sattrack (UV-K5 Sat Track)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_tles(text):
    """3-line or 2-line TLE text -> [(norad, name, l1, l2)]."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    out = []
    for i in range(len(lines) - 1):
        if lines[i].startswith("1 ") and lines[i + 1].startswith("2 "):
            name = lines[i - 1].strip() if i and not lines[i - 1].startswith(("1 ", "2 ")) else ""
            norad = int(lines[i][2:7])
            out.append((norad, name or str(norad), lines[i], lines[i + 1]))
    return out


def search(query):
    """NORAD id or (part of a) name -> [(norad, name, l1, l2)]."""
    q = query.strip()
    key = "CATNR" if q.isdigit() else "NAME"
    return parse_tles(_get(f"{CELESTRAK}?{key}={urllib.parse.quote(q)}&FORMAT=TLE"))


def transmitters(norad):
    """Active transmitters known to SatNOGS, as dicts with downlink/uplink in
    MHz (uplink 0 if none), mode and description."""
    data = json.loads(_get(f"{SATNOGS}?satellite__norad_cat_id={int(norad)}&format=json"))
    out = []
    for t in data:
        if not t.get("alive", True) or t.get("status", "active") != "active":
            continue
        dl = t.get("downlink_low")
        if not dl:
            continue
        raw = (t.get("mode") or "?").upper()
        out.append({
            "downlink": dl / 1e6,
            "uplink": (t.get("uplink_low") or 0) / 1e6,
            "mode": "FM" if raw in FM_CARRIED else raw,
            "raw_mode": raw,
            "description": (t.get("description") or "").strip(),
            "invert": bool(t.get("invert")),
        })
    return out


def tle(cfg, norad, offline=False):
    """(l1, l2) for norad, from the cache in cfg or refreshed from CelesTrak.
    Returns (l1, l2, note) where note warns about stale data, or raises."""
    cache = cfg.data.setdefault("tle_cache", {})
    hit = cache.get(str(norad))
    age = time.time() - hit["fetched"] if hit else None
    if hit and (offline or age < TLE_FRESH_S):
        return hit["l1"], hit["l2"], ""
    try:
        found = search(str(norad))
        if not found:
            raise LookupError(f"CelesTrak has no TLE for {norad}")
        _, _, l1, l2 = found[0]
        cache[str(norad)] = {"l1": l1, "l2": l2, "fetched": time.time()}
        cfg.save()
        return l1, l2, ""
    except Exception as e:
        if hit and age < TLE_MAX_AGE_S:
            return hit["l1"], hit["l2"], f"offline, using {age / 86400:.0f}-day-old TLE ({e})"
        raise
