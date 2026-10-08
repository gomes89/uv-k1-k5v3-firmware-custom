# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""Pass-store format. Constants are read from the C header so the firmware,
the app and this tool cannot drift apart."""
import os
import re
import struct
import sys

FORMAT_H = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "App", "apps", "sat", "sat_format.h")


def _cdefine(name):
    pat = re.compile(r"^\s*#define\s+" + re.escape(name) + r"\s+(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\b")
    with open(FORMAT_H) as f:
        for line in f:
            m = pat.match(line)
            if m:
                return int(m.group(1), 0)
    sys.exit(f"sat_format.h: #define {name} not found")


SECTOR = _cdefine("SAT_SECTOR_SIZE")
PASS_COUNT = _cdefine("SAT_PASS_COUNT")
INDEX_MAGIC = _cdefine("SAT_INDEX_MAGIC")
PASS_MAGIC = _cdefine("SAT_PASS_MAGIC")
FMT_VERSION = _cdefine("SAT_FMT_VERSION")
HDR_SIZE = _cdefine("SAT_HDR_SIZE")
C_Q = _cdefine("SAT_C_Q")
EXT_OFF = _cdefine("SAT_EXT_OFF")
PLOT_POINTS = _cdefine("SAT_PLOT_POINTS")
PLOT_R = _cdefine("SAT_PLOT_R")
MAX_SAMPLES = (EXT_OFF - HDR_SIZE) // 4
SECTOR_COUNT = 1 + PASS_COUNT

def _ctcss_tones():
    """Tones the firmware can send, in Hz, read from App/dcs.c."""
    path = os.path.join(os.path.dirname(FORMAT_H), "..", "..", "dcs.c")
    with open(path) as f:
        m = re.search(r"CTCSS_Options\[\d+\]\s*=\s*\{([^}]*)\}", f.read())
    if not m:
        sys.exit("dcs.c: CTCSS_Options not found")
    return [int(v) / 10 for v in re.findall(r"\d+", m.group(1))]


CTCSS_TONES = _ctcss_tones()

MODES = {"FM": 0, "AM": 1, "USB": 2, "CW": 2}         # what the app can demodulate
FREQ_MIN, FREQ_MAX = 18.0, 1300.0           # MHz, as checked by the app
NAME_LEN = 9                                 # radio shows 9 characters
PPM_LIMIT = 20000


def crc16(data, crc=0):
    """CRC-16/XMODEM, as in the firmware and the app."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def radio_ok(mhz):
    return FREQ_MIN <= mhz <= FREQ_MAX


def build_pass(sat, samples, step, max_el, label, extras):
    """sat: config dict; samples: [(rr_km_s, az_deg, el_deg)] from AOS;
    extras: dict(aos, los, tca_s, plot=[(x, y)]) for the display.
    Returns the whole sector image up to the extras (0xFF in the gap)."""
    if not 2 <= len(samples) <= MAX_SAMPLES or not 1 <= step <= 4:
        raise ValueError("pass does not fit the format")
    body = bytearray()
    for rr, az, el in samples:
        body += struct.pack("<hBB", max(-32768, min(32767, round(rr * 4000))),
                            round(az * 256 / 360) & 0xFF, max(0, min(180, round(el * 2))))
    dl, ul = float(sat["downlink"]), float(sat.get("uplink") or 0)
    dl10, ul10 = round(dl * 1e5), round(ul * 1e5)
    # Doppler factors the app would otherwise compute (same floor rounding)
    k_dl, k_ul = (dl10 << 20) // C_Q, (ul10 << 20) // C_Q
    q8_dl = (C_Q << 8) // dl10
    hdr = struct.pack(
        "<IIIHHBBBBH10s12sIII8s",
        PASS_MAGIC, dl10, ul10, len(samples),
        round(float(sat.get("ctcss") or 0) * 10), step,
        MODES[sat.get("mode", "FM").upper()], round(max_el), FMT_VERSION, 0,
        sat["name"][:NAME_LEN].encode("ascii", "replace"),
        label[:11].encode("ascii", "replace"), k_dl, k_ul, q8_dl, b"")
    plot = extras["plot"]
    if not 2 <= len(plot) <= PLOT_POINTS:
        raise ValueError("bad sky plot")
    ext = struct.pack("<18s18sHB", ("AOS " + extras["aos"]).encode("ascii", "replace")[:17],
                      ("LOS " + extras["los"]).encode("ascii", "replace")[:17],
                      min(0xFFFF, int(extras["tca_s"])), len(plot))
    ext += b"".join(struct.pack("<BB", x, y) for x, y in plot)
    ext += b"\0" * (2 * (PLOT_POINTS - len(plot)))
    ext += extras["frame"]
    crc = crc16(ext, crc16(body, crc16(hdr)))
    head = hdr[:20] + struct.pack("<H", crc) + hdr[22:] + bytes(body)
    return head + b"\xff" * (EXT_OFF - len(head)) + ext


def build_index(generation, ppm, count):
    body = struct.pack("<IIhBBH", INDEX_MAGIC, generation & 0xFFFFFFFF, ppm, count, FMT_VERSION, 0)
    return body + struct.pack("<H", crc16(body))


def parse_index(raw):
    """-> dict, or None if the store holds no valid index."""
    magic, gen, ppm, count, ver, _ = struct.unpack_from("<IIhBBH", raw)
    if magic != INDEX_MAGIC or ver != FMT_VERSION or crc16(raw[:14]) != struct.unpack_from("<H", raw, 14)[0]:
        return None
    return {"generation": gen, "ppm": ppm, "count": count}


def parse_header(raw):
    """Pass header -> dict, or None. The app checks the sample CRC itself."""
    f = struct.unpack_from("<IIIHHBBBBH10s12sIII8s", raw)
    if f[0] != PASS_MAGIC or f[8] != FMT_VERSION:
        return None
    text = lambda b: b.split(b"\0")[0].decode("ascii", "replace")
    mode = next((k for k, v in MODES.items() if v == f[6]), "?")
    return {"downlink": f[1] / 1e5, "uplink": f[2] / 1e5, "ctcss": f[4] / 10, "mode": mode,
            "max_el": f[7], "name": text(f[10]), "label": text(f[11])}


def image(index, blobs):
    img = bytearray(b"\xff" * SECTOR * SECTOR_COUNT)
    img[:len(index)] = index
    for i, b in enumerate(blobs):
        img[SECTOR * (1 + i):SECTOR * (1 + i) + len(b)] = b
    return bytes(img)
