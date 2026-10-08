#!/usr/bin/env python3
# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
#
# /// script
# requires-python = ">=3.9"
# dependencies = ["sgp4>=2.20", "pyserial>=3.5"]
# ///
"""Sat Track: pick satellite passes on your computer, load them into the radio.

Run without arguments for a guided menu, or use a command:

  setup                    your location and the radio's serial port
  add [NORAD|NAME]         add a satellite (frequencies from SatNOGS)
  remove NAME|NORAD        remove a satellite
  sats                     list your satellites
  passes                   show upcoming passes
  load [PICK]              choose passes and send them to the radio
                           PICK: "1 3 5-8", or "next" for the next 16
  radio                    show the passes on the radio
  install FILE...          install overlay apps (.app) on the radio
  calibrate [SECONDS]      measure the radio clock (default 120 s)

Run with: uv run tools/sattrack/sattrack.py
(or: pip install -r tools/sattrack/requirements.txt)
"""
import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from sgp4.api import Satrec

from satlib import catalog
from satlib.config import Config
from satlib.fmt import (CTCSS_TONES, NAME_LEN, PASS_COUNT, MODES, build_index,
                        build_pass, image, radio_ok)
from satlib.orbit import Observer, compass, find_passes, pass_extras, sample_pass


class Cancel(Exception):
    pass


# ---------------------------------------------------------------- prompts --
def ask(prompt, default=None):
    hint = f" [{default}]" if default not in (None, "") else ""
    try:
        v = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Cancel
    return v if v else ("" if default is None else str(default))


def ask_float(prompt, default=None, lo=None, hi=None):
    while True:
        v = ask(prompt, default)
        try:
            f = float(v.replace(",", "."))
            if (lo is None or f >= lo) and (hi is None or f <= hi):
                return f
        except ValueError:
            pass
        print(f"  enter a number" + (f" between {lo} and {hi}" if lo is not None else ""))


def ask_tone(prompt):
    """0 for none, else one of the firmware's CTCSS tones."""
    while True:
        f = ask_float(prompt, 0, 0, 300)
        if f == 0 or f in CTCSS_TONES:
            return f
        near = min(CTCSS_TONES, key=lambda t: abs(t - f))
        print(f"  {f:g} Hz is not a standard tone; did you mean {near:g}?")


def ask_yes(prompt, default=True):
    v = ask(prompt + (" [Y/n]" if default else " [y/N]")).lower()
    return default if not v else v.startswith("y")


def pick_one(n, prompt="Pick", allow=()):
    """Number 1..n, or one of the extra keys in allow; Enter cancels."""
    while True:
        v = ask(prompt).lower()
        if not v:
            raise Cancel
        if v in allow:
            return v
        if v.isdigit() and 1 <= int(v) <= n:
            return int(v) - 1
        print(f"  enter 1-{n}" + "".join(f" or {a}" for a in allow))


def parse_pick(text, n):
    """'1 3 5-8' / '1,3,5-8' -> sorted unique 0-based indexes; ValueError."""
    out = set()
    for part in re.split(r"[\s,]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"'{part}' is not a number or range")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            raise ValueError(f"'{part}' is outside 1-{n}")
        out.update(range(a - 1, b))
    return sorted(out)


def status(msg):
    print(f"\r  {msg:<60}", end="", flush=True)


# ------------------------------------------------------------------ setup --
def cmd_setup(cfg, args=None):
    loc = cfg.data.get("location") or {}
    print("Your location (decimal degrees, north and east positive).")
    lat = ask_float("Latitude", loc.get("lat"), -90, 90)
    lon = ask_float("Longitude", loc.get("lon"), -180, 180)
    alt = ask_float("Altitude in metres", loc.get("alt", 0), -500, 9000)
    cfg.data["location"] = {"lat": lat, "lon": lon, "alt": alt}
    cfg.data["min_el"] = ask_float("Ignore passes lower than (degrees)", cfg.data.get("min_el", 15), 0, 90)
    cfg.data["port"] = choose_port(cfg.data.get("port")) or cfg.data.get("port")
    cfg.save()
    print(f"Saved to {cfg.path}")


def choose_port(current):
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
    except Exception:
        ports = []
    if ports:
        print("Serial ports:")
        for i, p in enumerate(ports, 1):
            print(f"  {i}  {p}")
        v = ask("Radio port (number or name, Enter to keep)", current)
        if v.isdigit() and 1 <= int(v) <= len(ports):
            return ports[int(v) - 1]
        return v or None
    return ask("Radio serial port (e.g. /dev/ttyACM0 or COM5)", current) or None


def need_location(cfg):
    if not cfg.data.get("location"):
        print("First, tell me where you are.")
        cmd_setup(cfg)
    loc = cfg.data["location"]
    return Observer(loc["lat"], loc["lon"], loc.get("alt", 0))


def need_port(cfg, args):
    port = getattr(args, "port", None) or cfg.data.get("port")
    if not port:
        port = choose_port(None)
        if not port:
            raise Cancel
        cfg.data["port"] = port
        cfg.save()
    from satlib.radio import Radio
    r = Radio(port)
    print(f"Radio on {port}: firmware {r.version}")
    return r


# ------------------------------------------------------------- satellites --
def short_name(name):
    """'ISS (ZARYA)' -> 'ISS', 'SAUDISAT 1C (SO-50)' -> 'SO-50'."""
    m = re.search(r"\(([A-Z]{2,3}-\d+)\)", name)
    base = m.group(1) if m else re.sub(r"\s*\(.*\)", "", name).strip()
    return base[:NAME_LEN] or name[:NAME_LEN]


def describe(s):
    up = f" / up {s['uplink']:.3f}" if s.get("uplink") else ""
    tone = f", tone {s['ctcss']:g}" if s.get("ctcss") else ""
    rx = "" if s.get("uplink") and s.get("mode", "FM") == "FM" else "  (listen only)"
    return f"{s['name']:<9} {s['norad']:>6}  down {s['downlink']:.3f}{up} MHz {s.get('mode', 'FM')}{tone}{rx}"


def cmd_sats(cfg, args=None):
    if not cfg.sats:
        print("No satellites yet. Add one with: add <NORAD id or name>")
        return
    for s in cfg.sats:
        print("  " + describe(s))


def cmd_add(cfg, args):
    query = (getattr(args, "query", None) or "").strip() or ask("NORAD id or name (e.g. 25544 or ISS)")
    if not query:
        raise Cancel
    print("Searching CelesTrak ...")
    found = catalog.search(query)
    if not found:
        print(f"Nothing found for '{query}'.")
        return
    if len(found) > 1:
        shown = found[:20]
        for i, (norad, name, _, _) in enumerate(shown, 1):
            print(f"  {i:>2}  {norad:>6}  {name}")
        if len(found) > 20:
            print(f"  ... {len(found) - 20} more, try a longer name or the NORAD id")
        norad, name, l1, l2 = shown[pick_one(len(shown), "Which satellite")]
    else:
        norad, name, l1, l2 = found[0]
        print(f"Found {norad} {name}")
    cfg.data.setdefault("tle_cache", {})[str(norad)] = {"l1": l1, "l2": l2, "fetched": time.time()}

    entry = {"norad": norad}
    if getattr(args, "downlink", None):                   # scripted: no questions
        entry.update(downlink=args.downlink, uplink=args.uplink or 0,
                     mode=(args.mode or "FM").upper(), ctcss=args.ctcss or 0)
    else:
        entry.update(pick_transmitter(norad))
        entry["ctcss"] = (ask_tone("Uplink CTCSS tone in Hz (0 for none)")
                          if entry.get("uplink") and entry["mode"] == "FM" else 0)
    if not radio_ok(entry["downlink"]) or (entry.get("uplink") and not radio_ok(entry["uplink"])):
        print("Those frequencies are outside what the radio can tune (18-1300 MHz).")
        return
    if entry["mode"] not in MODES:
        print(f"The radio cannot receive {entry['mode']}.")
        return
    if entry.get("ctcss") and entry["ctcss"] not in CTCSS_TONES:
        print(f"{entry['ctcss']:g} Hz is not a CTCSS tone the radio can send.")
        return

    default = getattr(args, "name", None) or short_name(name)
    while True:
        n = (default if getattr(args, "name", None) else ask(f"Name on the radio (max {NAME_LEN})", default))[:NAME_LEN]
        if not n.isascii() or not n:
            print("  use plain letters and digits")
        elif cfg.find(n):
            print(f"  you already have a satellite called {n}")
            if getattr(args, "name", None):
                return
        else:
            break
    entry["name"] = n
    cfg.sats.append(entry)
    cfg.save()
    print("Added " + describe(entry))


def pick_transmitter(norad):
    try:
        txs = catalog.transmitters(norad)
    except Exception as e:
        print(f"Could not reach SatNOGS ({e}).")
        txs = []
    usable = [t for t in txs if radio_ok(t["downlink"]) and t["mode"] in MODES]
    if usable:
        # Voice repeaters first (FM with an uplink), then other FM, then the rest
        usable.sort(key=lambda t: (0 if t["uplink"] and t["raw_mode"] == "FM" else
                                   1 if t["mode"] == "FM" else 2))
        print("Transmitters (from SatNOGS, check they are still active):")
        for i, t in enumerate(usable, 1):
            up = f" / up {t['uplink']:8.3f}" if t["uplink"] else " " * 14
            print(f"  {i:>2}  down {t['downlink']:8.3f}{up}  {t['raw_mode']:<5} {t['description'][:34]}")
        print("   m  enter frequencies yourself")
        c = pick_one(len(usable), "Which one", allow=("m",))
        if c != "m":
            t = usable[c]
            return {"downlink": round(t["downlink"], 5), "uplink": round(t["uplink"], 5), "mode": t["mode"]}
    elif txs:
        print("SatNOGS lists no transmitter this radio can use (FM, AM, USB, CW; 18-1300 MHz).")
    return {
        "downlink": ask_float("Downlink MHz", None, 18, 1300),
        "uplink": ask_float("Uplink MHz (0 for none)", 0, 0, 1300),
        "mode": (ask("Mode (FM, AM, USB, CW)", "FM").upper() or "FM"),
    }


def cmd_remove(cfg, args):
    key = (getattr(args, "name", None) or "").strip()
    if not key:
        if not cfg.sats:
            print("No satellites to remove.")
            return
        for i, s in enumerate(cfg.sats, 1):
            print(f"  {i}  {describe(s)}")
        hit = [cfg.sats[pick_one(len(cfg.sats), "Remove which")]]
    else:
        hit = cfg.find(key)
    if not hit:
        print(f"No satellite '{key}'.")
        return
    for s in hit:
        cfg.sats.remove(s)
        print(f"Removed {s['name']}")
    cfg.save()


# ----------------------------------------------------------------- passes --
def compute_passes(cfg, args):
    obs = need_location(cfg)
    sats = cfg.sats
    if getattr(args, "sat", None):
        sats = [s for k in args.sat for s in cfg.find(k)]
        if not sats:
            print("None of those satellites are in your list.")
            return obs, []
    if not sats:
        print("No satellites yet. Add one first.")
        return obs, []
    start = datetime.now(timezone.utc).replace(microsecond=0)
    hours = getattr(args, "hours", None) or 48
    min_el = getattr(args, "min_el", None)
    min_el = cfg.data.get("min_el", 15) if min_el is None else min_el
    out = []
    for s in sats:
        try:
            l1, l2, note = catalog.tle(cfg, s["norad"])
        except Exception as e:
            print(f"  {s['name']}: no orbit data ({e}), skipped")
            continue
        if note:
            print(f"  {s['name']}: {note}")
        status(f"computing {s['name']} ...")
        out += find_passes(obs, Satrec.twoline2rv(l1, l2), s, start, hours, min_el)
    print("\r" + " " * 64 + "\r", end="")
    out.sort(key=lambda p: p.aos)
    return obs, out


def print_passes(passes):
    now = datetime.now(timezone.utc)
    print(f" {'#':>3}  {'Satellite':<9}  {'Start':<15} {'In':>7}  {'Len':>5}  {'Max':>3}  Direction")
    for i, p in enumerate(passes, 1):
        mins = int((p.aos - now).total_seconds() // 60)
        when = f"{mins // 60}h{mins % 60:02d}" if mins >= 60 else f"{max(mins, 0)}m"
        note = ""
        if i > 1 and p.aos < passes[i - 2].los:
            note = f"  overlaps {i - 1}"
        tx = "" if p.sat.get("uplink") and p.sat.get("mode", "FM") == "FM" else "  RX"
        print(f" {i:>3}  {p.sat['name']:<9}  {p.aos.astimezone():%a %d %H:%M:%S} {when:>7}  "
              f"{p.minutes:4.1f}m  {p.max_el:3.0f}  {compass(p.aos_az):>2} > {compass(p.los_az):<2}{tx}{note}")


def cmd_passes(cfg, args):
    _, passes = compute_passes(cfg, args)
    if passes:
        print_passes(passes)
    elif cfg.sats:
        print("No passes in that window.")


def cmd_load(cfg, args):
    obs, passes = compute_passes(cfg, args)
    if not passes:
        if cfg.sats:
            print("No passes in that window.")
        return
    print_passes(passes)
    pick = (getattr(args, "pick", None) or "").strip()
    while True:
        if not pick:
            pick = ask(f"\nPick up to {PASS_COUNT} passes (e.g. 1 3 5-8, or next)")
            if not pick:
                raise Cancel
        try:
            chosen = (list(range(min(PASS_COUNT, len(passes)))) if pick.lower() in ("next", "all")
                      else parse_pick(pick, len(passes)))
            if not chosen:
                raise ValueError("nothing picked")
            if len(chosen) > PASS_COUNT:
                raise ValueError(f"{len(chosen)} picked, the radio holds {PASS_COUNT}")
            break
        except ValueError as e:
            print(f"  {e}")
            if getattr(args, "pick", None):
                return
            pick = ""
    sel = [passes[i] for i in chosen]                    # already in AOS order
    counts = {}
    for p in sel:
        counts[p.sat["name"]] = counts.get(p.sat["name"], 0) + 1
    summary = ", ".join(f"{n} x{c}" if c > 1 else n for n, c in counts.items())
    print(f"{len(sel)} passes: {summary}, first {sel[0].aos.astimezone():%a %H:%M}, "
          f"last {sel[-1].aos.astimezone():%a %H:%M}")

    blobs = []
    for i, p in enumerate(sel, 1):
        status(f"preparing pass {i}/{len(sel)}")
        samples, step = sample_pass(obs, p)
        blobs.append(build_pass(p.sat, samples, step, p.max_el, aos_label(p.aos),
                                pass_extras(obs, p)))
    print()
    index = build_index(int(time.time()), cfg.data.get("ppm", 0), len(blobs))

    if getattr(args, "out", None):
        with open(args.out, "wb") as f:
            f.write(image(index, blobs))
        print(f"Wrote {args.out}")
        return
    if not getattr(args, "yes", False) and not ask_yes("Send to the radio? This replaces the passes on it"):
        raise Cancel
    radio = need_port(cfg, args)
    radio.upload(index, blobs, progress=status)
    print()
    print(f"Done. On the radio: F, 7, Sat Track. Next pass: {sel[0].sat['name']} "
          f"{sel[0].aos.astimezone():%a %H:%M}.")
    if "ppm_measured" not in cfg.data:
        print("Tip: run 'calibrate' once so the radio keeps better time.")


def aos_label(aos):
    """AOS on the radio, local time with seconds, e.g. "Tu 14:32:10" (11 chars)."""
    t = aos.astimezone()
    return f"{t:%a}"[:2] + f" {t:%H:%M:%S}"


# ------------------------------------------------------------------ radio --
def cmd_radio(cfg, args):
    """What the radio holds now (the passes and the clock correction)."""
    radio = need_port(cfg, args)
    idx, heads = radio.stored()
    if idx is None:
        print("No passes on the radio.")
        return
    print(f"{len(heads)} passes on the radio, clock correction {idx['ppm']} ppm:")
    for i, h in heads:
        up = f"up {h['uplink']:.3f}" if h["uplink"] else "listen only"
        print(f"{i:>3}  {h['name']:<9}  {h['label']:<11}  max {h['max_el']:>2}°  "
              f"down {h['downlink']:.3f}  {up}")


def cmd_clear(cfg, args):
    """Delete one pass from the radio, or all of them."""
    which = getattr(args, "number", None)
    if which is None:
        reply = ask(f"Pass number to delete (1-{PASS_COUNT}, see 'Show what is on the radio'), or all")
        if not reply:
            raise Cancel
        which = reply.strip()
    if str(which).lower() == "all":
        which = None
    number = None
    if which is not None:
        try:
            number = int(which)
        except ValueError:
            print(f"  '{which}' is not a pass number")
            return
        if not 1 <= number <= PASS_COUNT:
            print(f"  choose 1-{PASS_COUNT}")
            return
    what = "all passes" if number is None else f"pass {number}"
    if not getattr(args, "yes", False) and not ask_yes(f"Delete {what} from the radio?"):
        raise Cancel
    radio = need_port(cfg, args)
    radio.clear(number)
    print(f"Deleted {what}.")


def cmd_install(cfg, args):
    files = getattr(args, "files", None) or [ask("App file", "build/Apps/SatTrack.app")]
    radio = need_port(cfg, args)
    for f in files:
        if f:
            name, slot = radio.install_app(f)
            print(f"Installed {name} in app slot {slot}")


def cmd_calibrate(cfg, args):
    secs = getattr(args, "seconds", None) or 120
    if secs < 60:
        print("Use at least 60 seconds.")
        return
    radio = need_port(cfg, args)
    ppm, err = radio.calibrate(secs, progress=status)
    print()
    print(f"Radio clock {'slow' if ppm > 0 else 'fast'} by {abs(ppm)} ppm (+/- {err}). Saved; "
          "it is used the next time you load passes.")
    cfg.data["ppm"], cfg.data["ppm_measured"] = ppm, True
    cfg.save()


# ------------------------------------------------------------------- menu --
def menu(cfg, args):
    if not cfg.data.get("location"):
        print("Welcome to Sat Track.")
        cmd_setup(cfg)
    items = [("Choose passes and load the radio", cmd_load),
             ("Add a satellite", cmd_add),
             ("Remove a satellite", cmd_remove),
             ("Show upcoming passes", cmd_passes),
             ("Show what is on the radio", cmd_radio),
             ("Delete passes from the radio", cmd_clear),
             ("Settings (location, port)", cmd_setup),
             ("Install the app on the radio", cmd_install),
             ("Calibrate the radio clock", cmd_calibrate)]
    while True:
        loc = cfg.data["location"]
        print(f"\nSat Track   {loc['lat']:.2f}, {loc['lon']:.2f}   port {cfg.data.get('port') or '-'}")
        print("Satellites: " + (", ".join(s["name"] for s in cfg.sats) or "none yet"))
        for i, (label, _) in enumerate(items, 1):
            print(f"  {i}  {label}")
        print("  q  Quit")
        try:
            v = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if v in ("q", "quit", "exit"):
            return
        if v.isdigit() and 1 <= int(v) <= len(items):
            run(items[int(v) - 1][1], cfg, argparse.Namespace(port=args.port))


def run(fn, cfg, args):
    try:
        fn(cfg, args)
        return 0
    except Cancel:
        print("Cancelled.")
    except (OSError, IOError, TimeoutError, ValueError, LookupError, RuntimeError) as e:
        print(f"\nError: {e}")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog="Without a command: guided menu.")
    ap.add_argument("--config", help="settings file (default: per-user config dir)")
    ap.add_argument("--port", help="serial port, overrides the saved one")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="your location and the radio's serial port")
    p = sub.add_parser("add", help="add a satellite by NORAD id or name")
    p.add_argument("query", nargs="?")
    p.add_argument("--downlink", type=float, help="MHz; skips the questions")
    p.add_argument("--uplink", type=float, help="MHz, 0 for none")
    p.add_argument("--mode", help="FM, AM or USB")
    p.add_argument("--ctcss", type=float, help="uplink tone, Hz")
    p.add_argument("--name", help=f"name on the radio, max {NAME_LEN}")
    p = sub.add_parser("remove", help="remove a satellite")
    p.add_argument("name", nargs="?")
    sub.add_parser("sats", help="list your satellites")
    for name in ("passes", "load"):
        p = sub.add_parser(name, help="show upcoming passes" if name == "passes"
                           else "choose passes and send them to the radio")
        if name == "load":
            p.add_argument("pick", nargs="*", help='e.g. 1 3 5-8, or "next"')
            p.add_argument("--yes", action="store_true", help="do not ask before sending")
            p.add_argument("--out", help="write the image to a file instead of the radio")
        p.add_argument("--sat", action="append", help="only this satellite (repeatable)")
        p.add_argument("--hours", type=float, help="how far ahead (default 48)")
        p.add_argument("--min-el", type=float, help="lowest max elevation, degrees")
    sub.add_parser("radio", help="show the passes now on the radio")
    p = sub.add_parser("clear", help="delete one pass from the radio (number from 'radio'), or all")
    p.add_argument("number", nargs="?", default="all")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("install", help="install overlay apps on the radio")
    p.add_argument("files", nargs="*")
    p = sub.add_parser("calibrate", help="measure the radio clock")
    p.add_argument("seconds", nargs="?", type=int)
    args = ap.parse_args(argv)
    if getattr(args, "pick", None) is not None:
        args.pick = " ".join(args.pick)

    cfg = Config(args.config)
    cmds = {"setup": cmd_setup, "add": cmd_add, "remove": cmd_remove, "sats": cmd_sats,
            "passes": cmd_passes, "load": cmd_load, "radio": cmd_radio, "clear": cmd_clear, "install": cmd_install, "calibrate": cmd_calibrate}
    if not args.cmd:
        menu(cfg, args)
        return 0
    return run(cmds[args.cmd], cfg, args)


if __name__ == "__main__":
    sys.exit(main())
