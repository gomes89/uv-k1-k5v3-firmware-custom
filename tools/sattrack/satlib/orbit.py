# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""SGP4 look angles, range rate and pass search."""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sgp4.api import Satrec, jday

from .fmt import MAX_SAMPLES

OMEGA_E = 7.2921150e-5                       # rad/s
WGS84_A, WGS84_F = 6378.137, 1 / 298.257223563
C_KM_S = 299792.458


def gmst(jd_ut1):
    """Greenwich mean sidereal time (IAU 1982), radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    s = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return math.radians((s % 86400.0) / 240.0)


class Observer:
    def __init__(self, lat, lon, alt_m=0.0):
        la, lo = math.radians(lat), math.radians(lon)
        e2 = WGS84_F * (2 - WGS84_F)
        n = WGS84_A / math.sqrt(1 - e2 * math.sin(la) ** 2)
        h = alt_m / 1000.0
        self.o = ((n + h) * math.cos(la) * math.cos(lo),
                  (n + h) * math.cos(la) * math.sin(lo),
                  (n * (1 - e2) + h) * math.sin(la))
        self.sl, self.cl, self.so, self.co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)

    def look(self, sat, t):
        """-> (az deg, el deg, range km, range rate km/s) at UTC datetime t."""
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond * 1e-6)
        err, r, v = sat.sgp4(jd, fr)
        if err:
            raise RuntimeError(f"SGP4 error {err}")
        th = gmst(jd + fr)
        c, s = math.cos(th), math.sin(th)
        x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]       # TEME -> ECEF
        vx = c * v[0] + s * v[1] + OMEGA_E * y
        vy = -s * v[0] + c * v[1] - OMEGA_E * x
        dx, dy, dz = x - self.o[0], y - self.o[1], z - self.o[2]
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        rr = (dx * vx + dy * vy + dz * v[2]) / rng
        e = -self.so * dx + self.co * dy
        n = -self.sl * self.co * dx - self.sl * self.so * dy + self.cl * dz
        u = self.cl * self.co * dx + self.cl * self.so * dy + self.sl * dz
        return math.degrees(math.atan2(e, n)) % 360.0, math.degrees(math.asin(u / rng)), rng, rr


@dataclass
class Pass:
    sat: dict          # satellite config entry
    satrec: Satrec
    aos: datetime
    los: datetime
    max_el: float
    aos_az: float
    los_az: float

    @property
    def minutes(self):
        return (self.los - self.aos).total_seconds() / 60


def find_passes(obs, satrec, sat, start, hours, min_el):
    """Passes rising after start, AOS/LOS to 1 s at the 0 deg horizon,
    keeping those whose maximum elevation reaches min_el."""
    el_at = lambda t: obs.look(satrec, t)[1]

    def edge(a, b, rising):
        while (b - a).total_seconds() > 1:
            m = a + (b - a) / 2
            if (el_at(m) > 0) == rising:
                b = m
            else:
                a = m
        return (b if rising else a).replace(microsecond=0)

    step = timedelta(seconds=30)
    t, end = start, start + timedelta(hours=hours)
    while el_at(t) > 0 and t < end:          # skip a pass already in progress
        t += step
    prev = el_at(t)
    out = []
    while t < end:
        nt = t + step
        cur = el_at(nt)
        if prev <= 0 < cur:
            aos = edge(t, nt, True)
            lt = nt
            while el_at(lt) > 0:
                lt += step
            los = edge(lt - step, lt, False)
            best_t, best = aos, -90.0
            s = aos
            while s <= los:
                e = el_at(s)
                if e > best:
                    best, best_t = e, s
                s += timedelta(seconds=10)
            best = max(el_at(best_t + timedelta(seconds=d)) for d in range(-10, 11))
            if best >= min_el and los > aos:
                out.append(Pass(sat, satrec, aos, los, best,
                                obs.look(satrec, aos)[0], obs.look(satrec, los)[0]))
            t, prev = lt, el_at(lt)
            continue
        t, prev = nt, cur
    return out


def sample_pass(obs, p):
    """-> (samples [(rr, az, el)], step s). 2 s steps, longer for long passes."""
    dur = (p.los - p.aos).total_seconds()
    step = 2 if dur <= 2 * (MAX_SAMPLES - 1) else math.ceil(dur / (MAX_SAMPLES - 1))
    out = []
    for i in range(int(dur // step) + 1):
        az, el, _, rr = obs.look(p.satrec, p.aos + timedelta(seconds=i * step))
        out.append((rr, az, el))
    return out, step


def pass_extras(obs, p):
    """Display extras for the radio: sky plot points (N up, E right, in
    framebuffer pixels with y squashed like the dome, evenly spaced in time),
    TCA and the AOS/LOS labels."""
    from .fmt import PLOT_POINTS
    from .plot import PX, PY, R, RY, frame
    dur = (p.los - p.aos).total_seconds()
    n = PLOT_POINTS
    plot, best, tca = [], -90.0, 0
    for i in range(n):
        t = dur * i / (n - 1)
        az, el, _, _ = obs.look(p.satrec, p.aos + timedelta(seconds=t))
        r = R * (90 - max(0.0, el)) / 90
        plot.append((PX + round(r * math.sin(math.radians(az))), PY + round(-r * RY / R * math.cos(math.radians(az)))))
    for s in range(0, int(dur) + 1):
        el = obs.look(p.satrec, p.aos + timedelta(seconds=s))[1]
        if el > best:
            best, tca = el, s
    lab = lambda d: f"{d.astimezone():%a %H:%M:%S}"
    return {"aos": lab(p.aos), "los": lab(p.los), "tca_s": tca, "plot": plot, "frame": frame(plot)}


def compass(az):
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((az + 22.5) % 360 // 45)]
