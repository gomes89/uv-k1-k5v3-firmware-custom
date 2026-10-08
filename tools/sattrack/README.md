# Sat Track

Follow FM satellites with your UV-K1 / UV-K5 V3. The radio corrects the
Doppler shift for you. It does not need a clock: you press one key when the
satellite rises.

## 1. Build and flash the firmware

Labs already has everything:

    ./compile-firmware.sh Labs

Fusion needs two extra options:

    ./compile-firmware.sh Fusion -DENABLE_FEAT_F4HWN_OVERLAY_APPS=ON -DENABLE_FEAT_F4HWN_OVERLAY_SAT=ON

This turns on apps. On Fusion the built-in FM broadcast radio then becomes an
app too, which you install in step 2.

Flash the firmware with [UV Studio](https://armel.github.io/uvstudio/).

## 2. Install the app

    ./compile-app.sh sat fm
    uv run tools/sattrack/sattrack.py install build/Apps/SatTrack.app

On Fusion, also install `build/Apps/BroadcastFM.app` to keep the FM radio.
It asks for the radio's port the first time. Leave the radio on its main
screen while the computer talks to it.

## 3. Choose satellites and passes

    uv run tools/sattrack/sattrack.py

The first time, it asks where you are. Then, from the menu:

- **Add a satellite**: type its NORAD number (ISS is 25544) or part of its
  name, pick the transmitter from the list, and enter the uplink tone if
  it needs one. Check the frequencies: satellites change.
- **Choose passes and load the radio**: pick from the table, for example
  `1 3 5-8`, or `next` for the next 16. The radio holds 16.
- **Calibrate the radio clock**: do this once. It takes 2 minutes.
- **Show what is on the radio**: the passes it holds right now.
- **Delete passes from the radio**: one pass by its number in that list,
  or all of them.

Your settings and satellites are saved, so next time you only choose passes.
For scripts: `sattrack.py add 25544`, `sattrack.py load next --yes`,
`sattrack.py clear 3 --yes`, `sattrack.py --help`.

[uv](https://docs.astral.sh/uv/) installs the two Python packages it needs
on first run. Without uv: `pip install -r tools/sattrack/requirements.txt`,
then run the commands with `python` instead of `uv run`.

## 4. Use it

1. Press **F**, then **7**, and pick **Sat Track**.
2. The screen shows the next pass: when it starts and ends, to the second,
   and its path on the sky plot, with a square where it rises. You do not
   need to set a frequency.
3. When the satellite rises, press **MENU**.
4. Listen and talk with **PTT** as usual. The radio keeps the frequencies right.
5. At the end, press **MENU** for the next pass, or **EXIT**.

You can leave with **EXIT** during a pass. Come back and it carries on.

| Key          | Before the pass   | During the pass               |
|--------------|-------------------|-------------------------------|
| MENU         | start now         |                               |
| UP / DOWN    | choose a pass     | move 1 second later / earlier |
| 0            | mark flown / not  | stop / resume the pass        |
| 3 / 9        |                   | tune up / down a little       |
| 5            |                   | fix the timing (see below)    |
| PTT          |                   | talk                          |
| F, UP / DOWN | squelch up / down | squelch up / down             |
| EXIT         | leave             | leave (the pass keeps going)  |

**On the screen.** `EL45/67` is the elevation now and the highest it will
get. On the sky plot, north is up and east is right; the solid line is the
path so far and the dot is the satellite. The bar next to the signal level
runs from -130 dBm (just noise) to -90 dBm (strong), and the thin mark on
it is the strongest so far in this pass. The small arrows next to the pass
number show you can browse passes.

A pass marked flown is skipped when the app picks the next pass. Press **0**
again on it, any time, to clear the mark.
On the UV-K1, UP / DOWN are the LEFT / RIGHT keys.

**Pressed MENU late?** A second or two does not matter. If the satellite
sounds off-tune, tune with **3** / **9** until it sounds right, then press
**5**: the radio works out how late you were and fixes it. Do this in the
middle of the pass; near the start and end it beeps twice and does nothing.

## Good to know

- Power, bandwidth and squelch come from the VFO you were on. The app never
  changes your channels.
- The radio only transmits on FM passes, inside your TX band settings.
- Passes are stored at 0x130000-0x141000 in the radio's flash. The format is
  in `App/apps/sat/sat_format.h`.
