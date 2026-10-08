# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""Saved settings: location, port, clock correction, your satellites."""
import json
import os
import sys


def default_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "sattrack", "config.json")


class Config:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.data = {"location": None, "port": None, "ppm": 0, "satellites": []}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data.update(json.load(f))

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def sats(self):
        return self.data["satellites"]

    def find(self, key):
        """Satellite entries matching a name (case-insensitive) or NORAD id."""
        k = str(key).strip().lower()
        return [s for s in self.sats if s["name"].lower() == k or str(s["norad"]) == k]
