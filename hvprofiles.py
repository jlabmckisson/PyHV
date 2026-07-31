#!/usr/bin/env python3
"""
hvprofiles -- named connection profiles for hvctl.

A profile is a name and an address -- an IP address or hostname for the
Ethernet modules, a serial port for the USB ones.  The point is that neither
the CLI nor the TUI should need one typed at it: you name each module once,
and pick it from a list afterwards.

Stored as JSON so it can be hand-edited, and written atomically so a crash
mid-save cannot truncate the file.  Nothing here imports hv.py -- the CLI
reads profiles at startup and the TUI edits them, and a cycle between the
two would be gratuitous.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 1470
DEFAULT_BAUD = 9600         # the 803x USB VCP setting: 9600 8N1
SCHEMA = 1

# Hostnames and IPv4 literals.  Deliberately permissive: the connect attempt
# is the real test, and rejecting something resolvable would be worse than
# letting it through.
HOST_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# A serial port, told apart from a hostname by shape alone: /dev/ttyACM0,
# /dev/tty.usbmodem1101, COM3.  No hostname begins with a slash, so one
# address field can carry either and the dialog needs no mode switch.
COM_RE = re.compile(r"^COM\d+$", re.I)


def looks_like_device(text: str) -> bool:
    text = text.strip()
    return text.startswith("/") or bool(COM_RE.match(text))


@dataclass
class Profile:
    """One module you talk to, by name.

    Either a network address or a serial port, according to `kind`.  The
    unused pair stays at its default and is left out of the saved file, so a
    hand-edited profile only ever shows the fields that apply to it.
    """

    name: str
    host: str = ""
    port: int = DEFAULT_PORT
    kind: str = "tcp"           # "tcp", "serial", or "sim" for the fake module
    device: str = ""            # serial only, e.g. /dev/ttyACM0
    baud: int = DEFAULT_BAUD    # serial only

    @property
    def is_sim(self) -> bool:
        return self.kind == "sim"

    @property
    def is_serial(self) -> bool:
        return self.kind == "serial"

    @property
    def address(self) -> str:
        if self.is_sim:
            return "built-in simulator"
        if self.is_serial:
            return f"{self.device} @{self.baud}"
        return f"{self.host}:{self.port}"

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind}
        if self.is_serial:
            d.update(device=self.device, baud=self.baud)
        else:
            d.update(host=self.host, port=self.port)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(name=str(d.get("name", "")).strip(),
                   host=str(d.get("host", "")).strip(),
                   port=int(d.get("port") or DEFAULT_PORT),
                   kind=str(d.get("kind") or "tcp"),
                   device=str(d.get("device", "")).strip(),
                   baud=int(d.get("baud") or DEFAULT_BAUD))


# Always offered, never stored: the simulator needs no configuration and
# should not be something you can delete by accident.
SIM_PROFILE = Profile(name="Simulator", kind="sim")


# --------------------------------------------------------------------------
# Field validation -- shared by the TUI dialog and anything else that builds
# a Profile from text.
# --------------------------------------------------------------------------

def clean_name(text: str, taken: list[str] | tuple = ()) -> str:
    name = " ".join(text.split())
    if not name:
        raise ValueError("name required")
    if len(name) > 40:
        raise ValueError("name is too long (40 characters max)")
    if name.casefold() == SIM_PROFILE.name.casefold():
        raise ValueError(f"{SIM_PROFILE.name!r} is reserved")
    for other in taken:
        if name.casefold() == other.casefold():
            raise ValueError(f"a profile named {other!r} already exists")
    return name


def clean_device(text: str) -> str:
    """Check a serial port path.  Existence is the connect attempt's problem.

    A module that is unplugged now may be plugged in later, and refusing to
    save a profile for it would be unhelpful.
    """
    device = text.strip()
    if not device:
        raise ValueError("address required")
    if not looks_like_device(device):
        raise ValueError(f"{device!r} is not a serial port")
    return device


def clean_baud(text: str) -> int:
    text = text.strip()
    if not text:
        return DEFAULT_BAUD
    try:
        baud = int(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a baud rate")
    if not 50 <= baud <= 4_000_000:
        raise ValueError("baud rate is out of range")
    return baud


def clean_host(text: str) -> tuple[str, int | None]:
    """Return (host, port) -- port is not None when 'host:port' was typed."""
    host = text.strip()
    if not host:
        raise ValueError("address required")
    port = None
    if ":" in host:
        host, _, tail = host.rpartition(":")
        port = clean_port(tail)
        host = host.strip()
    if not HOST_RE.match(host):
        raise ValueError(f"{host!r} is not a hostname or IP address")
    return host, port


def clean_port(text: str) -> int:
    text = text.strip()
    if not text:
        return DEFAULT_PORT
    try:
        port = int(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a port number")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def default_path() -> Path:
    env = os.environ.get("HVCTL_PROFILES")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "hvctl" / "profiles.json"


class ProfileStore:
    """The saved profiles, plus which one was used last.

    Loading never raises: a missing file is an empty store, and an unreadable
    one leaves `load_error` set so the UI can say so instead of pretending
    the user had no profiles.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_path()
        self.profiles: list[Profile] = []
        self.last_used: str = ""
        self.load_error: str = ""
        self.load()

    # -- reading -----------------------------------------------------------

    def load(self) -> None:
        self.profiles, self.last_used, self.load_error = [], "", ""
        try:
            raw = self.path.read_text("utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            self.load_error = f"cannot read {self.path}: {e}"
            return
        try:
            data = json.loads(raw)
            entries = data.get("profiles", []) if isinstance(data, dict) else data
            self.profiles = [Profile.from_dict(d) for d in entries
                             if isinstance(d, dict) and d.get("name")]
            if isinstance(data, dict):
                self.last_used = str(data.get("last_used", ""))
        except (ValueError, TypeError, AttributeError) as e:
            self.profiles, self.last_used = [], ""
            self.load_error = f"{self.path} is not valid profile JSON: {e}"

    def names(self) -> list[str]:
        return [p.name for p in self.profiles]

    def get(self, name: str) -> Profile | None:
        if not name:
            return None
        if name.casefold() == SIM_PROFILE.name.casefold():
            return SIM_PROFILE
        for p in self.profiles:
            if p.name.casefold() == name.casefold():
                return p
        return None

    # -- writing -----------------------------------------------------------

    def save(self) -> None:
        """Write the file atomically.  Raises OSError if the disk says no."""
        # A file we could not parse is never silently overwritten -- whatever
        # was in it may be worth recovering by hand.
        if self.load_error and self.path.exists():
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            self.load_error = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"schema": SCHEMA, "last_used": self.last_used,
                   "profiles": [p.to_dict() for p in self.profiles
                                if not p.is_sim]}
        tmp.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.path)

    def upsert(self, profile: Profile, old_name: str = "") -> None:
        """Add or replace a profile.

        A rename keeps its row where it was: the list is what the user reads
        in the picker, and having entries jump to the bottom when edited
        would be a small, repeated annoyance.
        """
        def index_of(name: str) -> int | None:
            return next((i for i, p in enumerate(self.profiles)
                         if p.name.casefold() == name.casefold()), None)

        at = index_of(old_name) if old_name else None
        if at is None:
            at = index_of(profile.name)
        if at is None:
            self.profiles.append(profile)
        else:
            self.profiles[at] = profile
            # A rename onto an existing name absorbs that entry.
            self.profiles = [p for i, p in enumerate(self.profiles)
                             if i == at
                             or p.name.casefold() != profile.name.casefold()]
        if old_name and self.last_used.casefold() == old_name.casefold():
            self.last_used = profile.name

    def delete(self, name: str) -> None:
        self.profiles = [p for p in self.profiles
                         if p.name.casefold() != name.casefold()]
        if self.last_used.casefold() == name.casefold():
            self.last_used = ""

    def remember(self, name: str) -> None:
        """Record the last successful connection, best-effort."""
        self.last_used = name
        try:
            self.save()
        except OSError:
            pass        # not worth interrupting a live session over
