"""Modules that misbehave in the ways real 803x firmware does.

`SimTransport` is a well-behaved module and so cannot show up the problems
that only appear against a real one.  These subclasses each break a single
assumption, which is what makes them worth having.
"""

from __future__ import annotations

from hv import SimTransport


class CountingTransport(SimTransport):
    """A correct module that records what it was asked for.

    "Stopped asking" is the whole point of the parameter-resolution code, and
    it can only be tested by counting.
    """

    def __init__(self):
        super().__init__()
        self.asked: dict[str, int] = {}

    def write(self, data):
        line = data.decode("ascii", "replace").strip()
        if ",PAR:" in line:
            par = line.split(",PAR:")[1].split(",")[0]
            self.asked[par] = self.asked.get(par, 0) + 1
        super().write(data)

    def count(self, par: str) -> int:
        return self.asked.get(par, 0)


class LegacyNames(CountingTransport):
    """Firmware that calls ramp-down RDWN and has no MAXV or IMRANGE.

    The combination the panel used to trip over: one parameter under another
    name, two that simply do not exist.  The renaming happens on the wire
    only -- the simulated supply still ramps and trips on its own keys.
    """

    HIDDEN = ("MAXV", "IMRANGE")
    WIRE = {"RDWN": "RDW"}          # what this module calls it -> internal key

    def __init__(self):
        super().__init__()
        # Descriptors follow the wire names, so RampDn stays writable.
        self.INFO = dict(self.INFO)
        for wire, internal in self.WIRE.items():
            self.INFO[wire] = self.INFO[internal]
        for gone in self.HIDDEN:
            self.INFO.pop(gone, None)

    def _translate(self, par):
        """Wire name to internal key, or None if this module has no such thing."""
        if par in self.HIDDEN or par in self.WIRE.values():
            return None             # RDW is not a name this firmware knows
        return self.WIRE.get(par, par)

    def _mon(self, ch, par):
        if par == "PARCHLIST":
            back = {v: k for k, v in self.WIRE.items()}
            names = [back.get(n, n) for n in self._pub(self.ch[0])
                     if n not in self.HIDDEN]
            return "#CMD:OK,VAL:" + ";".join(names)
        if ch is None:
            return super()._mon(ch, par)
        internal = self._translate(par)
        return "#PAR:ERR" if internal is None else super()._mon(ch, internal)

    def _set(self, ch, par, val):
        if ch is None or par in ("ON", "OFF"):
            return super()._set(ch, par, val)
        internal = self._translate(par)
        return ("#PAR:ERR" if internal is None
                else super()._set(ch, internal, val))


class NoParamList(CountingTransport):
    """Firmware that will not enumerate its parameters.

    Then nothing can be resolved up front and the poll loop has to cope on
    its own.
    """

    def _mon(self, ch, par):
        if par in ("PARLIST", "PARCHLIST"):
            return "#PAR:ERR"
        return super()._mon(ch, par)


class AdvertisesButRefuses(CountingTransport):
    """Lists MAXV, then answers PAR:ERR when asked for it.

    Firmware does lie, and the list is not a contract, so the poll loop must
    still be able to give up on a parameter it was promised.
    """

    def _mon(self, ch, par):
        if par == "MAXV" and ch is not None:
            return "#PAR:ERR"
        return super()._mon(ch, par)


class RealDUT(CountingTransport):
    """Modelled on an actual module, from its own `hvctl params` output.

        Board:    BdIlk BdIlkm BdCtr BdStatus BDHVmax BDHImax BDcLR
        Channel:  VMon IMon Status VSet ISet RUp RDwn PDwn IMRange Trip Pw

    Three things this catches that the simulator cannot: names are reported
    in mixed case, MAXV and BDALARM do not exist, and channels are switched
    with `Pw` rather than ON/OFF.  PARLIST is also incomplete -- BDNCH works
    but is not on it -- so absence from that list proves nothing.
    """

    BOARD_LIST = ["BdIlk", "BdIlkm", "BdCtr", "BdStatus", "BDHVmax",
                  "BDHImax", "BDcLR"]
    CHANNEL_LIST = ["VMon", "IMon", "Status", "VSet", "ISet", "RUp", "RDwn",
                    "PDwn", "IMRange", "Trip", "Pw"]
    ABSENT = ("MAXV", "BDALARM")

    def __init__(self):
        super().__init__()
        self.INFO = dict(self.INFO)
        self.INFO["PW"] = "1;2;;;;;;;On;Off"
        self.INFO.pop("MAXV", None)
        self.bd = dict(self.bd)
        self.bd.pop("BDALARM", None)
        self.bd["BdStatus"] = "0"

    # What it calls things -> the key the simulated supply keeps them under.
    WIRE = {"RDWN": "RDW"}

    def _internal(self, par):
        """Accept only names this module actually publishes, in any case."""
        upper = par.upper()
        if upper in self.ABSENT:
            return None
        if upper not in {n.upper() for n in self.CHANNEL_LIST + self.BOARD_LIST}:
            return None if upper != "BDNCH" else "BDNCH"    # works, unlisted
        return self.WIRE.get(upper, upper)

    def _mon(self, ch, par):
        if par == "PARLIST":
            return "#CMD:OK,VAL:" + ";".join(self.BOARD_LIST)
        if par == "PARCHLIST":
            return "#CMD:OK,VAL:" + ";".join(self.CHANNEL_LIST)
        if par.upper() in self.ABSENT:
            return "#PAR:ERR"       # the running simulation keeps refilling it
        if ch is None:
            return super()._mon(ch, par)
        internal = self._internal(par)
        if internal is None:
            return "#PAR:ERR"
        if internal == "PW":
            on = self.ch[int(ch)]["_on"] if int(ch) < self.NCH else False
            return f"#CMD:OK,VAL:{'On' if on else 'Off'}"
        return super()._mon(ch, internal)

    def _set(self, ch, par, val):
        if ch is None:
            return super()._set(ch, par, val)
        internal = self._internal(par)      # ON and OFF are not published
        if internal is None:
            return "#PAR:ERR"
        if internal == "PW":
            if val not in ("On", "Off"):
                return "#VAL:ERR"
            return super()._set(ch, "ON" if val == "On" else "OFF", None)
        return super()._set(ch, internal, val)


class DropsGroupReads(CountingTransport):
    """Refuses the CH:<nch> group read, so every channel is read singly."""

    def _mon(self, ch, par):
        if ch is not None and int(ch) == self.NCH and par != "PARCHLIST":
            return "#CH:ERR"
        return super()._mon(ch, par)
