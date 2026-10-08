# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""Serial link to the radio, using the framing of tools/serialtool."""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "serialtool"))

from .fmt import HDR_SIZE, PASS_COUNT, PPM_LIMIT, parse_header, parse_index  # noqa: E402

APP_SLOTS, APP_CODE_OFF, APP_ERR_MAGIC = 16, 0x1000, 2


class Radio:
    def __init__(self, port):
        import serial
        import msg as mm
        self.mm = mm
        self.ser = serial.Serial(port, baudrate=38400, timeout=0.05)
        self.rx = bytearray()
        self.ts = int(time.time()) & 0xFFFFFFFF
        r = self.call(0x0514, struct.pack("<I", self.ts), 0x0515, timeout=3)
        end = r.find(b"\0", 4, 20)
        self.version = r[4:end if end > 0 else 20].decode("ascii", "replace")

    def call(self, mid, data, want, timeout=2.0):
        self.ser.write(self.mm.make_packet(struct.pack("<HH", mid, len(data)) + data))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.rx += self.ser.read(256)
            while True:
                m = self.mm.fetch(self.rx)
                if m is None:
                    break
                if m.get_msg_type() == want:
                    return bytes(m.buf)
        raise TimeoutError(f"no answer from the radio (0x{mid:04X}). Is it on its main screen, "
                           "running firmware with Sat Track support?")

    # ---- pass store ----
    def erase(self, sector):
        r = self.call(0x0742, struct.pack("<BBI", sector, 0, self.ts), 0x0743, timeout=15)
        if r[5]:
            raise IOError(f"erase sector {sector}: status {r[5]}")

    def write(self, sector, data):
        for off in range(0, len(data), 128):
            chunk = data[off:off + 128]
            if chunk.count(0xFF) == len(chunk):     # erased flash already reads 0xFF
                continue
            r = self.call(0x0744, struct.pack("<BBIHI", sector, 0, off, len(chunk), self.ts) + chunk, 0x0745)
            if r[5]:
                raise IOError(f"write sector {sector}+{off}: status {r[5]}")

    def read(self, sector, length, skip=None):
        """skip: bytes; 128-byte chunks that are all 0xFF there are not read."""
        out = bytearray()
        for off in range(0, length, 128):
            n = min(128, length - off)
            if skip is not None and skip[off:off + n].count(0xFF) == n:
                out += b"\xff" * n
                continue
            r = self.call(0x0740, struct.pack("<BBHB", sector, 0, off, n), 0x0741)
            if r[5]:
                raise IOError(f"read sector {sector}+{off}: status {r[5]}")
            out += r[8:8 + r[6]]
        return bytes(out)

    def upload(self, index, blobs, verify=True, progress=print):
        """Erase, write passes, verify, then the index last: an interrupted
        upload leaves no valid index and the app shows NO PASSES."""
        progress("erasing ...")
        self.erase(0xFF)
        for i, b in enumerate(blobs):
            progress(f"writing pass {i + 1}/{len(blobs)}")
            self.write(1 + i, b)
            if verify and self.read(1 + i, len(b), skip=b) != b:
                raise IOError(f"verify failed on pass {i + 1}")
        self.write(0, index)
        if self.read(0, len(index)) != index:
            raise IOError("verify failed on index")

    # ---- clock ----
    def ticks(self):
        t0 = time.monotonic()
        r = self.call(0x0746, b"", 0x0747)
        return (t0 + time.monotonic()) / 2, struct.unpack_from("<I", r, 4)[0]

    def calibrate(self, seconds, progress=print):
        """-> (ppm, uncertainty). + means the radio clock runs slow."""
        h0, k0 = self.ticks()
        end = h0 + seconds
        while time.monotonic() < end:
            left = end - time.monotonic()
            progress(f"measuring clock ... {left:4.0f} s left")
            time.sleep(min(5, max(0, left)))
            self.ticks()                       # keeps the serial session alive
        h1, k1 = self.ticks()
        host, rad = h1 - h0, ((k1 - k0) & 0xFFFFFFFF) * 0.01
        ppm = round((host - rad) / rad * 1e6)
        if abs(ppm) > PPM_LIMIT:
            raise ValueError(f"implausible clock error ({ppm} ppm)")
        return ppm, round(0.03 / host * 1e6)

    def stored(self):
        """-> (index dict or None, [(pass number, header dict)]) as now on the radio."""
        idx = parse_index(self.read(0, 16))
        if idx is None:
            return None, []
        heads = [(i + 1, parse_header(self.read(1 + i, HDR_SIZE))) for i in range(PASS_COUNT)]
        return idx, [(n, h) for n, h in heads if h]

    def clear(self, number=None):
        """Erase one pass (1-16) or, with None, the whole pass store."""
        self.erase(0xFF if number is None else number)

    # ---- overlay apps ----
    def install_app(self, path):
        """Into the slot already holding an app of that name, else the first
        free one. Code first, header last, so a cut-off install never runs.
        -> slot number (1-based)."""
        with open(path, "rb") as f:
            blob = f.read()
        hdr, code = blob[:64], blob[64:]
        if hdr[:4] != b"FAP1":
            raise ValueError(f"{path}: not an overlay app")
        name = hdr[20:36].split(b"\0")[0]
        slot = free = None
        for i in range(APP_SLOTS):
            r = self.call(0x0730, bytes([i]), 0x0731)
            if r[6:10] == b"FAP1" and r[26:42].split(b"\0")[0] == name:
                slot = i
                break
            if free is None and r[5] == APP_ERR_MAGIC:
                free = i
        slot = free if slot is None else slot
        if slot is None:
            raise IOError("no free app slot")
        r = self.call(0x0732, struct.pack("<BBI", slot, 0, self.ts), 0x0733, timeout=10)
        if r[5]:
            raise IOError(f"app slot {slot + 1} erase failed: status {r[5]}")
        for part, base in ((code, APP_CODE_OFF), (hdr, 0)):
            for off in range(0, len(part), 128):
                chunk = part[off:off + 128]
                r = self.call(0x0734, struct.pack("<BBIHI", slot, 0, base + off, len(chunk), self.ts) + chunk, 0x0735)
                if r[5]:
                    raise IOError(f"app slot {slot + 1} write failed: status {r[5]}")
        r = self.call(0x0736, bytes([slot]), 0x0737)
        if r[5]:
            raise IOError(f"the radio rejected the app (status {r[5]}): firmware without Sat Track support?")
        return name.decode(), slot + 1
