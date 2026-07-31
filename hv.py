#!/usr/bin/env python3
"""
hvctl -- command line control for CAEN 803x ("SMART HV") power supplies.

Targets DT8031/2/3/4, N803x, R803x over Ethernet (TCP :1470) or USB VCP
(/dev/ttyACM*, 9600 8N1).

Most subcommands are read-only (MON/INFO).  The `set`, `on`, `off` and `tui`
subcommands issue SET and *can change the state of the supply*, including
energising channels.  The module must be in REMOTE (BDCTR) for those to work.

With no arguments it opens the TUI, which asks which module to connect to.
Modules are saved as named profiles (see hvprofiles.py), so an address only
has to be typed once; --profile picks one from the command line, and the
last one connected is the default for the other subcommands.

Protocol (803x User Manual, "Communication Protocol"):
    $CMD:<MON|SET|INFO>[,CH:<n>],PAR:<name>[,VAL:<v>]<CR><LF>
    #<CMD|LOC|VAL|CH|PAR>:<OK|ERR>[,VAL:<value>]<CR><LF>

Examples:
    hvctl.py                            # TUI, pick a module from the list
    hvctl.py --sim tui
    hvctl.py --profile "Lab rack A" status
    hvctl.py --host 192.168.0.250 tui
    hvctl.py --sim info
    hvctl.py --host 192.168.0.250 status
    hvctl.py --host 192.168.0.250 monitor --interval 2
    hvctl.py --serial /dev/ttyACM0 get 3 VMON
    hvctl.py --host 192.168.0.250 set 3 VSET 1500
    hvctl.py --host 192.168.0.250 on 3
    hvctl.py --host 192.168.0.250 status --json | jq '.channels[].vmon'
    hvctl.py --host 192.168.0.250 raw '$CMD:MON,PAR:BDFREL'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from hvprofiles import Profile, ProfileStore

CRLF = b"\r\n"
DEFAULT_TCP_PORT = 1470

# STATUS bit assignments, 803x manual.  Order matters for display.
STATUS_BITS = [
    (0, "ON"), (1, "RUP"), (2, "RDW"), (3, "OVC"),
    (4, "OVV"), (5, "UNV"), (6, "TRIP"), (7, "OVP"),
    (8, "TWN"), (9, "OVT"), (10, "KILL"), (11, "INTLK"),
    (12, "ISDIS"), (13, "FAIL"), (14, "LOCK"), (15, "MAXV"),
]

# Flags that mean "something is wrong", for colouring.
ALARM_FLAGS = {"OVC", "OVV", "UNV", "TRIP", "OVP", "TWN", "OVT",
               "KILL", "INTLK", "FAIL", "MAXV"}
BUSY_FLAGS = {"RUP", "RDW"}

RESP_RE = re.compile(r"^#(?P<hdr>[A-Z]+):(?P<res>OK|ERR)(?:,VAL:(?P<val>.*))?$")

ERR_TEXT = {
    "CMD": "malformed or unsupported command",
    "LOC": "module is in LOCAL mode (SET refused; switch front panel to REMOTE)",
    "VAL": "value out of range",
    "CH": "invalid channel number",
    "PAR": "unknown parameter",
}

# Firmware revisions disagree about a few parameter names, and asking for the
# wrong one earns a PAR:ERR once per poll forever.  Each entry maps the name
# this program uses to every spelling worth trying, best first; `resolve_par`
# picks whichever one the module actually advertises.
PAR_ALIASES = {
    "RDW":     ("RDW", "RDWN", "RAMPDOWN"),
    "RUP":     ("RUP", "RAMPUP"),
    "STATUS":  ("STATUS", "STAT", "CHSTATUS"),
    "IMRANGE": ("IMRANGE", "IRANGE"),
    "MAXV":    ("MAXV", "MAXVSET", "SVMAX"),
    "BDALARM": ("BDALARM", "BDALARMS"),
}


class HVError(Exception):
    """Any protocol-level failure.

    `header` is the module's error class -- PAR, CH, LOC, VAL or CMD -- and is
    empty for framing and link failures.  The distinction matters: "I have no
    such parameter" is a permanent fact worth working around, while a timeout
    is a transient one worth reporting.
    """

    def __init__(self, message: str, header: str = ""):
        super().__init__(message)
        self.header = header

    @property
    def unknown_param(self) -> bool:
        return self.header == "PAR"


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class Transport:
    def write(self, data: bytes) -> None: raise NotImplementedError
    def readline(self) -> bytes: raise NotImplementedError
    def close(self) -> None: pass
    def describe(self) -> str: return "?"


class TcpTransport(Transport):
    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT, timeout: float = 3.0):
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

    def write(self, data): self.sock.sendall(data)

    def readline(self):
        while CRLF not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise HVError(f"timeout waiting for reply from {self.describe()}")
            if not chunk:
                raise HVError("connection closed by module")
            self._buf += chunk
        line, self._buf = self._buf.split(CRLF, 1)
        return line

    def close(self):
        try: self.sock.close()
        except OSError: pass

    def describe(self): return f"tcp://{self.host}:{self.port}"


class SerialTransport(Transport):
    def __init__(self, device: str, baud: int = 9600, timeout: float = 3.0):
        try:
            import serial  # pyserial
        except ImportError:
            raise HVError("serial mode needs pyserial: pip install pyserial")
        self.device = device
        self.ser = serial.Serial(device, baudrate=baud, bytesize=8,
                                 parity="N", stopbits=1, timeout=timeout)
        self._buf = b""

    def write(self, data): self.ser.write(data)

    def readline(self):
        while CRLF not in self._buf:
            chunk = self.ser.read(256)
            if not chunk:
                raise HVError(f"timeout waiting for reply from {self.describe()}")
            self._buf += chunk
        line, self._buf = self._buf.split(CRLF, 1)
        return line

    def close(self):
        try: self.ser.close()
        except Exception: pass

    def describe(self): return f"serial://{self.device}"


class SimTransport(Transport):
    """A fake DT8033 so the CLI and TUI can be exercised with no hardware.

    Unlike a static fixture this one integrates: channels ramp toward their
    setpoint at RUP/RDW V/s, draw a load current proportional to VMON, and
    trip on overcurrent after TRIP seconds.  That makes SET, ON/OFF and trip
    recovery observable without energising anything real.
    """

    NCH = 8
    LOAD = 8.0e-3       # uA per volt -- a ~125 Gohm load on every channel

    # INFO descriptors: ptype;access;min;max;decimals;resolution;exponent;
    # unit;on_state;off_state   (access 0=ro 2=rw)
    INFO = {
        "VSET":    "0;2;0;4000;1;0.1;0;V;;",
        "VMON":    "0;0;0;4000;1;0.1;0;V;;",
        "ISET":    "0;2;0;3000;1;0.1;0;uA;;",
        "IMON":    "0;0;0;3000;1;0.1;0;uA;;",
        "MAXV":    "0;2;0;4000;0;1;0;V;;",
        "RUP":     "0;2;1;500;0;1;0;V/s;;",
        "RDW":     "0;2;1;500;0;1;0;V/s;;",
        "TRIP":    "0;2;0;1000;1;0.1;0;s;;",
        "PDWN":    "2;2;;;;;;;RAMP;KILL",
        "IMRANGE": "2;2;;;;;;;HIGH;LOW",
        "POL":     "2;0;;;;;;;+;-",
        "STATUS":  "0;0;0;65535;0;1;0;;;",
        "ON":      "1;1;;;;;;;ON;OFF",
        "OFF":     "1;1;;;;;;;ON;OFF",
    }

    def __init__(self):
        self._out = []
        self._t = time.monotonic()
        self.ch = []
        for i in range(self.NCH):
            vset = [0.0, 1500.0, 1500.0, 2400.0, 0.0, 1800.0, 1800.0, 3000.0][i]
            self.ch.append({
                "VSET": vset, "VMON": 0.0, "ISET": 500.0, "IMON": 0.0,
                "MAXV": 4000.0, "RUP": 50.0, "RDW": 100.0, "TRIP": 2.0,
                "PDWN": "RAMP", "IMRANGE": "LOW", "POL": "+", "STATUS": 0,
                # simulator-private state, not exposed as parameters
                "_on": i in (1, 2, 3, 5, 6, 7),
                "_ovc": 0.0,        # seconds spent in overcurrent
                "_trip": False,
            })
        # Channel 6 has a leaky load and a tight current limit, so it crosses
        # ISET a few seconds into its ramp and then trips.  Trip-clear and the
        # alarm colouring therefore have something real to act on -- and the
        # fault persists, so clearing it makes the channel trip again.
        self.ch[6].update({"_leak": 35.0, "ISET": 200.0, "RUP": 150.0})
        self.bd = {
            "BDNAME": "DT8033 8 CH 4KV/3mA", "BDNCH": "8",
            "BDFREL": "1.09", "BDSNUM": "24-0417", "BDCTR": "REMOTE",
            "BDILK": "NO", "BDILKM": "UNDRIVEN", "BDALARM": "0",
            "BDHVMAX": "4000", "BDHIMAX": "3000",
        }
        self.bd_writable = {"BDILKM", "BDCLR"}

    # -- physics -----------------------------------------------------------

    def _tick(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        if dt <= 0:
            return
        for c in self.ch:
            target = c["VSET"] if (c["_on"] and not c["_trip"]) else 0.0
            v = c["VMON"]
            if v < target:
                v = min(target, v + c["RUP"] * dt)
            elif v > target:
                rate = c["RDW"] * dt if c["PDWN"] == "RAMP" else abs(v)
                v = max(target, v - rate)
            c["VMON"] = v
            c["IMON"] = v * self.LOAD * c.get("_leak", 1.0)

            # Overcurrent: tolerate it for TRIP seconds, then drop the channel.
            if c["_on"] and not c["_trip"] and c["IMON"] > c["ISET"]:
                c["_ovc"] += dt
                if c["_ovc"] >= c["TRIP"]:
                    c["_trip"] = True
            elif c["IMON"] <= c["ISET"]:
                c["_ovc"] = 0.0

            # A trip latched just above retargets the channel to 0 V, so the
            # ramp flags below have to be judged against the new target.
            target = c["VSET"] if (c["_on"] and not c["_trip"]) else 0.0
            st = 0
            if c["_on"] and not c["_trip"]:
                st |= 1 << 0                                  # ON
            if v < target - 0.5:
                st |= 1 << 1                                  # RUP
            elif v > target + 0.5:
                st |= 1 << 2                                  # RDW
            if c["IMON"] > c["ISET"]:
                st |= 1 << 3                                  # OVC
            if c["_trip"]:
                st |= 1 << 6                                  # TRIP
            if c["VSET"] > c["MAXV"]:
                st |= 1 << 15                                 # MAXV
            c["STATUS"] = st
        self.bd["BDALARM"] = "1" if any(c["_trip"] for c in self.ch) else "0"

    # -- protocol ----------------------------------------------------------

    def write(self, data):
        line = data.decode("ascii", "replace").strip()
        self._tick()
        self._out.append(self._handle(line))

    def _handle(self, line):
        m = re.match(r"^\$CMD:(\w+)(?:,CH:(\d+))?,PAR:([A-Za-z0-9_]+)"
                     r"(?:,VAL:(.*))?$", line)
        if not m:
            return "#CMD:ERR"
        attr, ch, par, val = m.group(1), m.group(2), m.group(3).upper(), m.group(4)
        if attr == "INFO":
            return "#CMD:OK,VAL:" + self.INFO.get(par, "0;0;;;;;;;;")
        if attr == "MON":
            return self._mon(ch, par)
        if attr == "SET":
            if self.bd["BDCTR"] != "REMOTE":
                return "#LOC:ERR"
            return self._set(ch, par, val)
        return "#CMD:ERR"

    def _pub(self, c):
        """The channel dict minus simulator-private keys."""
        return {k: v for k, v in c.items() if not k.startswith("_")}

    def _mon(self, ch, par):
        if ch is None:
            if par == "PARLIST":
                return "#CMD:OK,VAL:" + ";".join(self.bd)
            if par in self.bd:
                return f"#CMD:OK,VAL:{self.bd[par]}"
            return "#PAR:ERR"
        c = int(ch)
        if c > self.NCH:
            return "#CH:ERR"
        if par == "PARCHLIST":
            return "#CMD:OK,VAL:" + ";".join(self._pub(self.ch[0]))
        if par not in self._pub(self.ch[0]):
            return "#PAR:ERR"
        if c == self.NCH:  # group read
            return "#CMD:OK,VAL:" + ";".join(self._fmt(x, par) for x in self.ch)
        return f"#CMD:OK,VAL:{self._fmt(self.ch[c], par)}"

    @staticmethod
    def _fmt(c, par):
        v = c[par]
        return f"{v:.1f}" if isinstance(v, float) else str(v)

    def _set(self, ch, par, val):
        if ch is None:
            if par == "BDCLR":
                for c in self.ch:
                    c["_trip"], c["_ovc"] = False, 0.0
                return "#CMD:OK"
            if par not in self.bd_writable:
                return "#PAR:ERR"
            self.bd[par] = val or ""
            return "#CMD:OK"

        c = int(ch)
        if c >= self.NCH:
            return "#CH:ERR"
        chan = self.ch[c]
        if par in ("ON", "OFF"):
            chan["_on"] = (par == "ON")
            if par == "ON":                 # a fresh ON clears a latched trip
                chan["_trip"], chan["_ovc"] = False, 0.0
            self._tick()
            return "#CMD:OK"
        if par in ("PDWN", "IMRANGE"):
            allowed = self.INFO[par].split(";")[8:10]
            if val not in allowed:
                return "#VAL:ERR"
            chan[par] = val
            return "#CMD:OK"
        if par not in chan or par.startswith("_") or self.INFO.get(par, "0;0")\
                .split(";")[1] != "2":
            return "#PAR:ERR"
        try:
            f = float(val)
        except (TypeError, ValueError):
            return "#VAL:ERR"
        lo, hi = self.INFO[par].split(";")[2:4]
        if (lo and f < float(lo)) or (hi and f > float(hi)):
            return "#VAL:ERR"
        chan[par] = f
        return "#CMD:OK"

    def readline(self):
        time.sleep(0.002)
        return self._out.pop(0).encode()

    def describe(self): return "sim://DT8033"


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------

@dataclass
class ParamInfo:
    """One row of a module's parameter descriptor table (the INFO reply).

    `access` follows the 803x manual: 0 = read-only, 1 = write-only,
    2 = read/write.  A parameter with both `on_state` and `off_state` filled
    in is a two-valued enum (PDWN, IMRANGE) rather than a number.
    """

    ptype: int = 0
    access: int = 0
    minimum: str = ""
    maximum: str = ""
    decimals: str = ""
    resolution: str = ""
    exponent: str = ""
    unit: str = ""
    on_state: str = ""
    off_state: str = ""

    @property
    def writable(self) -> bool:
        return self.access in (1, 2)

    @property
    def choices(self) -> list[str]:
        return [s for s in (self.on_state, self.off_state) if s]

    @property
    def prec(self) -> int:
        try:
            return int(self.decimals)
        except ValueError:
            return 2

    def range_text(self) -> str:
        if self.choices:
            return " | ".join(self.choices)
        if self.minimum or self.maximum:
            return f"{self.minimum} .. {self.maximum} {self.unit}".strip()
        return self.unit

    def validate(self, text: str) -> str:
        """Normalise user input for this parameter or raise ValueError.

        Range checking here is a courtesy: the module enforces its own limits
        and answers VAL:ERR, which is the authority.
        """
        text = text.strip()
        if not text:
            raise ValueError("value required")
        if self.choices:
            for c in self.choices:
                if text.upper() == c.upper():
                    return c
            raise ValueError(f"must be one of {' | '.join(self.choices)}")
        try:
            f = float(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a number")
        for bound, ok in ((self.minimum, lambda x, b: x >= b),
                          (self.maximum, lambda x, b: x <= b)):
            if bound:
                try:
                    b = float(bound)
                except ValueError:
                    continue
                if not ok(f, b):
                    raise ValueError(
                        f"outside {self.minimum}..{self.maximum} {self.unit}")
        return f"{f:.{self.prec}f}" if self.prec else f"{f:g}"


class Device:
    def __init__(self, transport: Transport, verbose: bool = False):
        self.t = transport
        self.verbose = verbose
        self._nch: int | None = None
        self._group_ok: bool | None = None
        self._info_cache: dict[str, ParamInfo] = {}
        self._advertised: dict[bool, set[str] | None] = {}
        self._partial: set[bool] = set()
        self._power_par: str | None = None

    # -- low level ---------------------------------------------------------

    def txn(self, cmd: str) -> str:
        """Send one command, return the VAL payload (possibly empty)."""
        if not cmd.endswith("\r\n"):
            cmd += "\r\n"
        if self.verbose:
            print(f"  -> {cmd.strip()}", file=sys.stderr)
        self.t.write(cmd.encode("ascii"))

        # The module may emit banner/echo noise; only '#'-lines are replies.
        for _ in range(20):
            raw = self.t.readline().decode("ascii", "replace").strip()
            if self.verbose and raw:
                print(f"  <- {raw}", file=sys.stderr)
            if not raw or not raw.startswith("#"):
                continue
            m = RESP_RE.match(raw)
            if not m:
                raise HVError(f"unparseable reply: {raw!r}")
            if m.group("res") == "ERR":
                hdr = m.group("hdr")
                raise HVError(f"{hdr}:ERR - {ERR_TEXT.get(hdr, 'unknown error')}",
                              hdr)
            return m.group("val") or ""
        raise HVError("no valid reply after 20 lines")

    def mon_bd(self, par: str) -> str:
        return self.txn(f"$CMD:MON,PAR:{par}")

    def mon_ch(self, ch: int, par: str) -> str:
        return self.txn(f"$CMD:MON,CH:{ch},PAR:{par}")

    # -- writes ------------------------------------------------------------
    # Everything below can change the state of the supply.

    def set_bd(self, par: str, val: str | None = None) -> None:
        tail = f",VAL:{val}" if val is not None else ""
        self.txn(f"$CMD:SET,PAR:{par}{tail}")

    def set_ch(self, ch: int, par: str, val: str | None = None) -> None:
        tail = f",VAL:{val}" if val is not None else ""
        self.txn(f"$CMD:SET,CH:{ch},PAR:{par}{tail}")

    def channel_on(self, ch: int) -> None:
        self._power(ch, True)

    def channel_off(self, ch: int) -> None:
        self._power(ch, False)

    def _power(self, ch: int, on: bool) -> None:
        """Energise or de-energise a channel, whichever way this module works.

        Most 803x firmware takes `PAR:ON` and `PAR:OFF`.  Some instead exposes
        a single `Pw` parameter with On/Off states and knows nothing of ON or
        OFF.  Getting this wrong means a channel that will not switch off when
        asked, so both are tried: the advertised one first, the other if the
        module answers PAR:ERR.  Any other refusal -- LOCAL mode, a bad value
        -- is passed straight up, because it is an answer, not a misunderstanding.
        """
        cmd = "ON" if on else "OFF"
        ways: list[tuple[str, str | None]] = [(cmd, None)]
        pw = self.resolve_par(("PW",))
        if pw:
            info = self.try_info_ch(pw)
            # The module's own spelling of the state, e.g. "On" rather than "ON".
            ways.append((pw, (info.on_state if on else info.off_state)
                         or cmd.title()))
            if self.resolve_par((cmd,)) is None:
                ways.reverse()          # it lists Pw and not ON/OFF
        if self._power_par:             # already know which one this module took
            ways.sort(key=lambda w: w[0] != self._power_par)

        refusal = None
        for par, val in ways:
            try:
                self.set_ch(ch, par, val)
            except HVError as e:
                if not e.unknown_param:
                    raise
                refusal = e
            else:
                self._power_par = par
                return
        raise refusal or HVError("module accepts neither ON/OFF nor Pw", "PAR")

    def clear_alarm(self) -> None:
        """Board-wide alarm/trip clear (BDCLR)."""
        self.set_bd("BDCLR")

    # -- discovery ---------------------------------------------------------

    @property
    def nch(self) -> int:
        if self._nch is None:
            self._nch = int(float(self.mon_bd("BDNCH")))
        return self._nch

    def board_params(self) -> list[str]:
        return [p for p in self.mon_bd("PARLIST").split(";") if p]

    def channel_params(self, ch: int = 0) -> list[str]:
        return [p for p in self.mon_ch(ch, "PARCHLIST").split(";") if p]

    # A parameter known to work, used to tell a full list from a partial one.
    # Real modules do answer BDNCH while leaving it out of PARLIST.
    _LIST_PROBE = {True: "BDNCH", False: "VMON"}

    def advertised(self, board: bool = False) -> dict[str, str] | None:
        """What the module says it has: upper-cased name -> its own spelling.

        Firmware reports names in whatever case it likes -- VMon, RDwn, BdIlk
        -- so lookups are case-insensitive, but the module's own spelling is
        kept and used on the wire.  Assuming it will accept some other case is
        a gamble with nothing to win.

        Firmware that does not implement PARLIST/PARCHLIST is not evidence
        that a parameter is absent, so None means "no filtering possible" and
        is deliberately different from an empty mapping.
        """
        if board not in self._advertised:
            try:
                names = self.board_params() if board else self.channel_params()
                have = {n.strip().upper(): n.strip() for n in names if n.strip()}
            except (HVError, OSError):
                have = None
            else:
                if self._LIST_PROBE[board] not in have:
                    self._partial.add(board)
            self._advertised[board] = have
        return self._advertised[board]

    def lists_everything(self, board: bool = False) -> bool:
        """Whether absence from the list can be trusted to mean absence."""
        self.advertised(board)
        return board not in self._partial

    def resolve_par(self, names: Sequence[str], board: bool = False) -> str | None:
        """Pick the spelling this module answers to, out of a list of aliases.

        The name comes back in the module's own case, ready to put on the
        wire.  Returns None only when the module keeps a full list and none of
        the names is on it -- then the caller should drop that reading rather
        than ask for it once a second forever.  Otherwise the first name is
        returned and the module gets to answer for itself.
        """
        have = self.advertised(board)
        if have is None:
            return names[0]
        for name in names:
            if name.upper() in have:
                return have[name.upper()]
        return None if self.lists_everything(board) else names[0]

    def info_ch(self, par: str, ch: int = 0) -> ParamInfo:
        if par not in self._info_cache:
            fields = self.txn(f"$CMD:INFO,CH:{ch},PAR:{par}").split(";")
            fields += [""] * (10 - len(fields))
            self._info_cache[par] = ParamInfo(
                ptype=int(fields[0] or 0), access=int(fields[1] or 0),
                minimum=fields[2], maximum=fields[3], decimals=fields[4],
                resolution=fields[5], exponent=fields[6], unit=fields[7],
                on_state=fields[8], off_state=fields[9])
        return self._info_cache[par]

    def try_info_ch(self, par: str, ch: int = 0) -> ParamInfo:
        """info_ch that degrades to an empty descriptor instead of raising.

        Older firmware does not describe every parameter it accepts, and a
        missing descriptor should not make a field uneditable.
        """
        try:
            return self.info_ch(par, ch)
        except HVError:
            return self._info_cache.setdefault(par, ParamInfo(access=2))

    def is_remote(self) -> bool:
        try:
            return self.mon_bd("BDCTR").strip().upper() == "REMOTE"
        except HVError:
            return True     # not all models report BDCTR; assume writable

    # -- bulk reads --------------------------------------------------------

    def read_all(self, par: str) -> list[str]:
        """One value per channel.  Uses the group read (CH:<nch>) when the
        module supports it, which matters a lot over 9600 baud serial."""
        n = self.nch
        if self._group_ok is not False:
            try:
                parts = self.mon_ch(n, par).split(";")
            except HVError as e:
                # An unknown parameter says nothing about whether the module
                # can do group reads, and must not switch them off for every
                # other parameter -- on 9600 baud serial that is an 8x cost.
                if e.unknown_param:
                    raise
                self._group_ok = False
            else:
                if len(parts) == n:
                    self._group_ok = True
                    return parts
                self._group_ok = False
        return [self.mon_ch(c, par) for c in range(n)]

    def snapshot(self, params: Sequence[str]) -> list[dict]:
        """One dict per channel, keyed by the canonical names in `params`.

        A parameter this module does not have comes back empty rather than
        raising: a missing RDW should cost you that column, not the table.
        """
        cols: dict[str, list[str]] = {}
        for p in params:
            wire = self.resolve_par(PAR_ALIASES.get(p, (p,)))
            try:
                cols[p] = self.read_all(wire) if wire else []
            except HVError as e:
                if not e.unknown_param:
                    raise
                cols[p] = []
        return [{p: (vals[c] if c < len(vals) else "")
                 for p, vals in cols.items()} for c in range(self.nch)]


def transport_for_profile(prof: Profile, timeout: float = 3.0) -> Transport:
    if prof.is_sim:
        return SimTransport()
    if prof.is_serial:
        return SerialTransport(prof.device, prof.baud, timeout)
    return TcpTransport(prof.host, prof.port, timeout)


def device_for_profile(prof: Profile, timeout: float = 3.0,
                       verbose: bool = False) -> Device:
    """Connect to a saved profile.  Blocks; the TUI calls it off the UI thread."""
    return Device(transport_for_profile(prof, timeout), verbose=verbose)


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

class Palette:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code, s): return f"\033[{code}m{s}\033[0m" if self.on else s
    def red(self, s): return self._w("1;31", s)
    def yellow(self, s): return self._w("33", s)
    def green(self, s): return self._w("32", s)
    def dim(self, s): return self._w("2", s)
    def bold(self, s): return self._w("1", s)


def decode_status(raw: str) -> tuple[int, list[str]]:
    try:
        word = int(float(raw))
    except ValueError:
        return 0, [raw]
    return word, [name for bit, name in STATUS_BITS if word & (1 << bit)]


def fmt_status(flags: list[str], pal: Palette) -> str:
    if not flags:
        return pal.dim("OFF")
    out = []
    for f in flags:
        if f in ALARM_FLAGS:
            out.append(pal.red(f))
        elif f in BUSY_FLAGS:
            out.append(pal.yellow(f))
        elif f == "ON":
            out.append(pal.green(f))
        else:
            out.append(f)
    return " ".join(out)


def num(raw: str, width: int = 9, prec: int = 2) -> str:
    try:
        return f"{float(raw):>{width}.{prec}f}"
    except ValueError:
        return f"{raw:>{width}}"


STATUS_PARAMS = ["VSET", "VMON", "ISET", "IMON", "RUP", "RDW", "TRIP", "STATUS"]


def render_status(dev: Device, pal: Palette) -> str:
    rows = dev.snapshot(STATUS_PARAMS)
    v_unit = dev.info_ch("VMON").unit or "V"
    i_unit = dev.info_ch("IMON").unit or "uA"

    lines = []
    hdr = (f"{'CH':>2}  {'VSet/'+v_unit:>9} {'VMon/'+v_unit:>9} "
           f"{'ISet/'+i_unit:>9} {'IMon/'+i_unit:>9} "
           f"{'RUp':>5} {'RDwn':>5} {'Trip':>6}  Status")
    lines.append(pal.bold(hdr))
    lines.append(pal.dim("-" * len(hdr)))
    for c, r in enumerate(rows):
        _, flags = decode_status(r["STATUS"])
        lines.append(
            f"{c:>2}  {num(r['VSET'])} {num(r['VMON'])} "
            f"{num(r['ISET'])} {num(r['IMON'])} "
            f"{num(r['RUP'], 5, 0)} {num(r['RDW'], 5, 0)} "
            f"{num(r['TRIP'], 6, 1)}  {fmt_status(flags, pal)}"
        )
    return "\n".join(lines)


def status_json(dev: Device) -> dict:
    rows = dev.snapshot(STATUS_PARAMS)
    out = []
    for c, r in enumerate(rows):
        word, flags = decode_status(r["STATUS"])
        entry = {"channel": c, "status_word": word, "flags": flags,
                 "on": "ON" in flags}
        for p in STATUS_PARAMS:
            if p == "STATUS":
                continue
            try:
                entry[p.lower()] = float(r[p])
            except ValueError:
                entry[p.lower()] = r[p]
        out.append(entry)
    return {"timestamp": time.time(), "link": dev.t.describe(),
            "channels": out}


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

BOARD_SUMMARY = ["BDNAME", "BDNCH", "BDFREL", "BDSNUM", "BDCTR",
                 "BDILK", "BDILKM", "BDALARM", "BDHVMAX", "BDHIMAX"]


def cmd_info(dev, args, pal):
    if args.json:
        d = {}
        for p in BOARD_SUMMARY:
            try:
                d[p.lower()] = dev.mon_bd(p)
            except HVError:
                pass
        print(json.dumps(d, indent=2))
        return 0
    print(pal.dim(f"link: {dev.t.describe()}"))
    for p in BOARD_SUMMARY:
        try:
            val = dev.mon_bd(p)
        except HVError as e:
            val = pal.dim(f"({e})")
        print(f"  {p:<9} {val}")
    return 0


def cmd_status(dev, args, pal):
    if args.json:
        print(json.dumps(status_json(dev), indent=2))
    else:
        print(render_status(dev, pal))
    return 0


def cmd_monitor(dev, args, pal):
    try:
        while True:
            body = render_status(dev, pal)
            stamp = time.strftime("%H:%M:%S")
            sys.stdout.write("\033[H\033[2J" if pal.on else "\n")
            print(pal.dim(f"{dev.t.describe()}   {stamp}   "
                          f"refresh {args.interval}s   ctrl-c to quit"))
            print(body)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_get(dev, args, pal):
    par = args.param.upper()
    if args.channel is None:
        val = dev.mon_bd(par)
    else:
        val = dev.mon_ch(args.channel, par)
    if par == "STATUS" and not args.raw_value:
        word, flags = decode_status(val)
        print(f"{word}  {fmt_status(flags, pal)}")
    else:
        print(val)
    return 0


def cmd_params(dev, args, pal):
    print(pal.bold("Board parameters"))
    for p in dev.board_params():
        print(f"  {p}")
    print()
    print(pal.bold(f"Channel parameters (via CH:{args.channel})"))
    for p in dev.channel_params(args.channel):
        try:
            i = dev.info_ch(p, args.channel)
            acc = {0: "ro", 1: "wo", 2: "rw"}.get(i.access, "??")
            print(f"  {p:<10} {acc}  {i.range_text()}")
        except HVError:
            print(f"  {p:<10} {pal.dim('(no descriptor)')}")

    # The reason this subcommand exists: if a display column is missing here,
    # that is why the module answers PAR:ERR when something asks for it.
    missing = [p for p in STATUS_PARAMS + ["MAXV", "PDWN", "IMRANGE"]
               if dev.resolve_par(PAR_ALIASES.get(p, (p,))) is None]
    if missing:
        print()
        print(pal.bold("Not advertised by this module"))
        print(f"  {', '.join(missing)}")
        print(pal.dim("  These are skipped rather than polled; add the "
                      "module's own name to PAR_ALIASES if it uses another."))
    return 0


def cmd_set(dev, args, pal):
    par = args.param.upper()
    info = dev.try_info_ch(par, args.channel)
    try:
        val = info.validate(args.value)
    except ValueError as e:
        print(f"error: {par}: {e}", file=sys.stderr)
        return 2
    dev.set_ch(args.channel, par, val)
    print(f"CH{args.channel} {par} = {dev.mon_ch(args.channel, par)}")
    return 0


def cmd_power(dev, args, pal):
    turning_on = args.cmd == "on"
    if turning_on and not args.yes:
        reply = input(f"energise channel {args.channel}? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    (dev.channel_on if turning_on else dev.channel_off)(args.channel)
    _, flags = decode_status(dev.mon_ch(args.channel, "STATUS"))
    print(f"CH{args.channel}  {fmt_status(flags, pal)}")
    return 0


def cmd_clear(dev, args, pal):
    dev.clear_alarm()
    print("board alarm cleared")
    return 0


def cmd_tui(dev, args, pal):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from hvtui import run_tui
    except ImportError as e:
        print(f"error: the TUI needs textual: pip install textual ({e})",
              file=sys.stderr)
        return 1
    # dev is None when no connection was named: the TUI asks for one.
    return run_tui(dev, interval=args.interval, read_only=args.read_only,
                   store=args.store, timeout=args.timeout,
                   verbose=args.verbose)


def cmd_profiles(dev, args, pal):
    """List saved profiles.  They are created and edited in the TUI."""
    store = args.store
    if store.load_error:
        print(pal.red(store.load_error), file=sys.stderr)
    print(pal.dim(f"{store.path}"))
    if not store.profiles:
        print("  no profiles yet -- run `hvctl` and press n to add one")
        return 0
    width = max(len(p.name) for p in store.profiles)
    for p in store.profiles:
        mark = " (last used)" if p.name == store.last_used else ""
        print(f"  {p.name:<{width}}  {p.address}{pal.dim(mark)}")
    return 0


def cmd_raw(dev, args, pal):
    for cmd in args.command:
        if not cmd.startswith("$"):
            cmd = "$" + cmd
        try:
            print(dev.txn(cmd))
        except HVError as e:
            print(pal.red(str(e)))
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="hvctl",
        description="Monitor and control CAEN 803x / SMART HV supplies. "
                    "With no arguments, opens the TUI and asks which module "
                    "to connect to.")
    link = p.add_argument_group("connection")
    link.add_argument("--profile", metavar="NAME",
                      help="connect to a saved profile (see `hvctl profiles`)")
    link.add_argument("--host", default=os.environ.get("HVCTL_HOST"),
                      help="module IP (env HVCTL_HOST)")
    link.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    link.add_argument("--serial", metavar="DEV",
                      help="USB VCP device, e.g. /dev/ttyACM0")
    link.add_argument("--baud", type=int, default=9600)
    link.add_argument("--timeout", type=float, default=3.0)
    link.add_argument("--sim", action="store_true",
                      help="use a built-in fake DT8033 (no hardware needed)")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="trace protocol traffic on stderr")

    # No subcommand means the TUI, so its options need defaults on the parent
    # parser too.  A subparser that defines the same option wins over these.
    sub = p.add_subparsers(dest="cmd")
    p.set_defaults(cmd="tui", interval=1.0, read_only=False)

    sub.add_parser("info", help="board identity and interlock state")
    sub.add_parser("status", help="one-shot table of all channels")

    m = sub.add_parser("monitor", help="repeating status table")
    m.add_argument("-n", "--interval", type=float, default=2.0)

    t = sub.add_parser("tui", help="full-screen control panel (the default)")
    t.add_argument("-n", "--interval", type=float, default=1.0,
                   help="poll period in seconds (default 1)")
    t.add_argument("--read-only", action="store_true",
                   help="display only; refuse every SET")

    g = sub.add_parser("get", help="read a single parameter")
    g.add_argument("channel", nargs="?", type=int,
                   help="channel number; omit for a board parameter")
    g.add_argument("param")
    g.add_argument("--raw-value", action="store_true",
                   help="print STATUS as the bare integer")

    pl = sub.add_parser("params", help="list parameters the module advertises")
    pl.add_argument("--channel", type=int, default=0)

    s = sub.add_parser("set", help="write a channel parameter (changes state)")
    s.add_argument("channel", type=int)
    s.add_argument("param")
    s.add_argument("value")

    for name, verb in (("on", "energise"), ("off", "de-energise")):
        w = sub.add_parser(name, help=f"{verb} a channel (changes state)")
        w.add_argument("channel", type=int)
        w.add_argument("-y", "--yes", action="store_true",
                       help="skip the confirmation prompt")

    sub.add_parser("clear", help="clear the board alarm / latched trips")
    sub.add_parser("profiles", help="list saved connection profiles")

    r = sub.add_parser("raw", help="send literal protocol commands")
    r.add_argument("command", nargs="+")

    return p


HANDLERS = {"info": cmd_info, "status": cmd_status, "monitor": cmd_monitor,
            "get": cmd_get, "params": cmd_params, "raw": cmd_raw,
            "tui": cmd_tui, "set": cmd_set, "on": cmd_power, "off": cmd_power,
            "clear": cmd_clear, "profiles": cmd_profiles}


def open_transport(args, pal):
    """Resolve the connection flags to a transport.

    Returns (transport, exit_code).  A transport of None with code 0 means
    "nothing was named" -- only the TUI can go on from there, by asking.
    """
    store = args.store
    if args.profile:
        prof = store.get(args.profile)
        if prof is None:
            known = ", ".join(store.names()) or "none saved"
            print(f"error: no profile named {args.profile!r} ({known})",
                  file=sys.stderr)
            return None, 2
        return transport_for_profile(prof, args.timeout), 0
    if args.sim:
        return SimTransport(), 0
    if args.serial:
        return SerialTransport(args.serial, args.baud, args.timeout), 0
    if args.host:
        return TcpTransport(args.host, args.port, args.timeout), 0
    if args.cmd == "tui":
        return None, 0              # the TUI shows its profile picker
    # Fall back to whatever was connected last, so the read-only subcommands
    # work without flags once the TUI has been used at least once.
    prof = store.get(store.last_used)
    if prof is not None:
        print(pal.dim(f"using profile {prof.name} ({prof.address})"),
              file=sys.stderr)
        return transport_for_profile(prof, args.timeout), 0
    print("error: no connection. Give --host, --serial, --sim or --profile, "
          "or run `hvctl` with no arguments to save one in the TUI.",
          file=sys.stderr)
    return None, 2


def main(argv=None):
    args = build_parser().parse_args(argv)
    pal = Palette(sys.stdout.isatty() and not args.no_color and not args.json)
    args.store = ProfileStore()

    if args.cmd == "profiles":
        return cmd_profiles(None, args, pal)

    try:
        transport, code = open_transport(args, pal)
    except (OSError, HVError) as e:
        print(f"connection failed: {e}", file=sys.stderr)
        return 1
    if code:
        return code

    dev = Device(transport, verbose=args.verbose) if transport else None
    try:
        return HANDLERS[args.cmd](dev, args, pal)
    except HVError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if transport:
            transport.close()


if __name__ == "__main__":
    sys.exit(main())