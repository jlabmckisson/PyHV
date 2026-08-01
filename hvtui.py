#!/usr/bin/env python3
"""
hvtui -- full-screen control panel for CAEN 803x ("SMART HV") power supplies.

Launched by `hv.py`, which needs no arguments: the TUI asks which module to
connect to, and remembers the ones you name as profiles.

All device I/O happens on a single background thread (`DeviceWorker`)
because the transports are blocking and, over a 9600 baud serial link, slow:
doing it inline would freeze the interface.  That thread is also the only
writer, so commands and polls can never interleave mid-transaction.

Writes are real.  Anything that energises a channel is confirmed first, and
`--read-only` refuses them outright.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             RichLog, Static)

from hv import (ALARM_FLAGS, BUSY_FLAGS, PAR_ALIASES, STATUS_BITS,
                STATUS_HELP, Device, HVError, ParamInfo, decode_status,
                device_for_profile)
from hvprofiles import (DEFAULT_BAUD, DEFAULT_PORT, MAX_LABEL, SIM_PROFILE,
                        Profile, ProfileStore, clean_baud, clean_device,
                        clean_host, clean_label, clean_name, clean_port,
                        looks_like_device)


# --------------------------------------------------------------------------
# What the table shows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    par: str            # canonical parameter name, and the display key
    title: str          # header text
    width: int
    editable: bool = False
    fast: bool = True   # re-read every poll, rather than occasionally
    align: str = "right"

    @property
    def names(self) -> tuple[str, ...]:
        """Every spelling worth trying on the wire, best first."""
        return PAR_ALIASES.get(self.par, (self.par,))


COLUMNS: list[Column] = [
    Column("VSET",    "VSet",   10, editable=True),
    Column("VMON",    "VMon",   10),
    Column("ISET",    "ISet",   10, editable=True),
    Column("IMON",    "IMon",   10),
    Column("RUP",     "RampUp",  7, editable=True, fast=False),
    Column("RDW",     "RampDn",  7, editable=True, fast=False),
    Column("TRIP",    "Trip",    6, editable=True, fast=False),
    Column("MAXV",    "MaxV",    7, editable=True, fast=False),
    Column("PDWN",    "PwDn",    6, editable=True, fast=False, align="center"),
    Column("IMRANGE", "IRange",  7, editable=True, fast=False, align="center"),
    Column("STATUS",  "Status",  30, align="left"),
]

# The column that absorbs whatever width the terminal has spare.  Every other
# one holds a number of known width; the status flags are an open-ended list,
# so extra room is worth more there than as blank space down the right edge.
# It never shrinks below its declared width -- at 80 columns the table scrolls,
# which is the narrowest terminal the panel is meant for.
STRETCH = "STATUS"

# The channel-name column.  Not a parameter -- no 803x has one -- so it is
# never polled, and it is only in the table once some channel has a name: a
# column of blanks is not worth the width on an 80-column terminal.  It sits
# immediately right of the channel number, where the two read as one label.
NAME_COL = "NAME"


BOARD_STATIC = ["BDNAME", "BDSNUM", "BDFREL", "BDHVMAX", "BDHIMAX", "BDILKM"]
BOARD_FAST = ["BDILK", "BDALARM"]       # safety-relevant, every poll
BOARD_SLOW = ["BDCTR"]                  # LOCAL/REMOTE, changes at the panel

SLOW_EVERY = 6      # poll the `fast=False` parameters once every N cycles


def fmt_channels(chs) -> str:
    """0,1,2,3,6 -> "0-3,6".

    A sixteen-channel selection spelled out in full does not fit a dialog
    title, and the whole point of naming the channels is that they are read
    before something is written to them.
    """
    out: list[str] = []
    chs = sorted(chs)
    i = 0
    while i < len(chs):
        j = i
        while j + 1 < len(chs) and chs[j + 1] == chs[j] + 1:
            j += 1
        out.append(str(chs[i]) if j == i else f"{chs[i]}-{chs[j]}")
        i = j + 1
    return ",".join(out)


class ChannelTable(DataTable):
    """The channel table, which refits its columns when the terminal changes.

    It has to notice the resize itself: `Resize` does not bubble, and by the
    time the App hears about its own resize the table has not been given its
    new region yet.
    """

    def on_resize(self) -> None:
        self.app.fit_columns()


# The marker each tab carries, so a module in a background tab can still say
# what it is doing.  A tab is a glance, not a diagnosis: the point of `!` is to
# get somebody to press the key and look, not to say what went wrong.
TAB_ALARM = "!"
TAB_LIVE = "●"
TAB_OFF = "○"
TAB_WAIT = "…"          # connected, nothing read back yet
TAB_PAUSED = "‖"        # its poll loop is stopped, so its reading is stale

# Said on the bar rather than only in the help: the tabs are the one part of
# the panel with no key of its own in the footer, which has no room left.
TAB_HINT = " [ ] switches, w closes"
HINT_WIDTH = len(TAB_HINT)
# What a tab costs besides its name: " 3 " + " " + marker + "  ".
TAB_FURNITURE = 7


class ModuleTabs(Static):
    """The row of connected modules, one line high.

    A single Static rather than a widget per module: the bar is repainted on
    every reading, and which tab a click landed in is a matter of which span
    the x falls in -- which is known from the painting anyway.  A widget each
    would mean mounting and unmounting on every connect.
    """

    def __init__(self) -> None:
        super().__init__(id="tabs")
        self.spans: list[tuple[int, int]] = []

    def on_click(self, event) -> None:
        for i, (start, end) in enumerate(self.spans):
            if start <= event.x < end:
                self.app.show_module(i)
                return

    def on_resize(self) -> None:
        # How much of each name fits depends on the width, and a paused panel
        # has no poll coming to redraw the bar.
        self.app._paint_tabs()


# --------------------------------------------------------------------------
# Device thread
# --------------------------------------------------------------------------

@dataclass
class Sample:
    channels: list[dict[str, str]] = field(default_factory=list)
    board: dict[str, str] = field(default_factory=dict)
    stamp: float = 0.0
    error: str | None = None


@dataclass
class Module:
    """One connected supply: its link, its I/O thread, and what was read back.

    Every field here used to sit on the App, because there was only ever one
    module.  A panel holding several needs one of each per module, and needs
    them to keep filling in while the operator is looking at a different tab:
    a trip on the supply nobody is watching is the one worth catching.

    Only the active module's state is on screen.  Everything else here is a
    tab away, which is why `cursor` and `selected` live with the module rather
    than with the table -- coming back to a tab should find it as it was left.
    """

    dev: Device
    profile: Profile | None
    interval: float
    worker: "DeviceWorker | None" = None
    nch: int = 0
    # What this particular module can supply, resolved once on connect.
    columns: list[Column] = field(default_factory=list)
    info: dict[str, ParamInfo] = field(default_factory=dict)
    board: dict[str, str] = field(default_factory=dict)
    flags: list[list[str]] = field(default_factory=list)
    values: list[dict[str, str]] = field(default_factory=list)
    selected: set[int] = field(default_factory=set)
    labels: dict[int, str] = field(default_factory=dict)
    ready: bool = False          # its columns are known, so a table can be built
    last_error: str | None = None
    stamp: float = 0.0           # when the last reading landed
    cursor: Coordinate = Coordinate(0, 0)

    @property
    def name(self) -> str:
        """What the tab bar and the log call it."""
        return self.profile.name if self.profile else self.dev.t.describe()

    @property
    def paused(self) -> bool:
        return self.worker.paused if self.worker else False

    @property
    def live(self) -> list[int]:
        """Channels reporting ON.  Empty also means "nothing read yet"."""
        return [c for c in range(self.nch) if c < len(self.flags)
                and "ON" in self.flags[c]]

    @property
    def known(self) -> bool:
        """Whether any channel has been read.  Silence is not "all off"."""
        return any(self.values)

    @property
    def alarmed(self) -> bool:
        """Anything the operator would want to be told about from another tab.

        Deliberately broad: a tab is a glance, not a diagnosis, and the cost of
        looking at a module that turns out to be fine is one keypress.
        """
        if self.last_error:
            return True
        if self.board.get("BDILK", "").upper() == "YES":
            return True
        if self.board.get("BDALARM", "0") not in ("0", ""):
            return True
        return any(set(f) & ALARM_FLAGS for f in self.flags)


class DeviceWorker(threading.Thread):
    """Owns the Device.  Nothing else may touch it.

    One per connected module, each polling on its own interval -- a module in
    a background tab is still read, so its tab can say when something has gone
    wrong on it.

    Queued commands always run before the next poll and force an immediate
    re-read afterwards, so an edit shows its effect without waiting out the
    poll interval.
    """

    def __init__(self, mod: Module, app: "HVApp"):
        super().__init__(daemon=True, name="hv-io")
        self.mod = mod
        self.dev = mod.dev
        self.app = app
        self.paused = False
        self._q: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._due = 0.0
        self._cycle = 0
        self._values: dict[str, list[str]] = {}
        self._board: dict[str, str] = {}
        # Filled in by _startup from what this particular module advertises.
        self.columns: list[Column] = []
        self._wire: dict[str, str] = {}         # canonical name -> module's name
        self._bd_fast: list[str] = []
        self._bd_slow: list[str] = []

    # -- public, called from the UI thread ---------------------------------

    def submit(self, label: str, fn) -> None:
        """Run fn(device) on the I/O thread and report the outcome."""
        self._q.put((label, fn))
        self._wake.set()

    def wire_name(self, par: str) -> str:
        """This module's own spelling of a parameter we display."""
        return self._wire.get(par, par)

    def refresh_now(self) -> None:
        self._due = 0.0
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        try:
            self._startup()
        except (HVError, OSError) as e:
            self._emit(Sample(error=f"startup failed: {e}"))
            return
        while not self._stop.is_set():
            self._drain()
            now = time.monotonic()
            if not self.paused and now >= self._due:
                self._poll()
                self._due = time.monotonic() + self.mod.interval
            timeout = 0.25 if self.paused else max(0.02, self._due - time.monotonic())
            self._wake.wait(timeout)
            self._wake.clear()

    def _to_app(self, fn, *a) -> None:
        """Hand a result to the UI thread, tolerating a shutting-down app."""
        if self._stop.is_set():
            return
        try:
            self.app.call_from_thread(self._dispatch, fn, a)
        except RuntimeError:
            self._stop.set()        # event loop is gone; we are quitting

    def _dispatch(self, fn, a) -> None:
        """Runs on the UI thread.  A superseded worker's late reading -- one
        already in flight when the tab it belongs to was closed -- is dropped
        rather than painted over some other module's channels."""
        if self._stop.is_set() or self.mod.worker is not self:
            return
        try:
            fn(*a)
        except NoMatches:
            # Quitting: the widgets are gone but the poll was already in the
            # air.  There is nothing left to paint and nothing to report.
            self._stop.set()

    def _startup(self) -> None:
        nch = self.dev.nch
        missing = self._resolve()
        infos = {c.par: self.dev.try_info_ch(self._wire[c.par])
                 for c in self.columns}
        for p in BOARD_STATIC + self._bd_slow:
            try:
                self._board[p] = self.dev.mon_bd(p)
            except HVError:
                self._board[p] = ""
        self._to_app(self.app.on_device_ready, self.mod, nch,
                     list(self.columns), infos, dict(self._board), missing)

    def _resolve(self) -> list[str]:
        """Match what we want to display against what the module has.

        Every module in the 803x family answers to a slightly different set of
        names, and a name it does not know costs a PAR:ERR on every poll that
        asks for it.  So ask once, up front, and never ask for the rest.
        Returns the parameters that were dropped, for the log.
        """
        missing: list[str] = []
        self.columns, self._wire = [], {}
        for col in COLUMNS:
            wire = self.dev.resolve_par(col.names)
            if wire is None:
                missing.append(col.par)
                continue
            self._wire[col.par] = wire
            self.columns.append(col)

        for want, into in ((BOARD_FAST, "_bd_fast"), (BOARD_SLOW, "_bd_slow")):
            keep = []
            for p in want:
                wire = self.dev.resolve_par(PAR_ALIASES.get(p, (p,)), board=True)
                if wire is None:
                    missing.append(p)
                    continue
                self._wire[p] = wire
                keep.append(p)
            setattr(self, into, keep)
        return missing

    def _drain(self) -> None:
        while True:
            try:
                label, fn = self._q.get_nowait()
            except queue.Empty:
                return
            try:
                fn(self.dev)
            except HVError as e:
                self._to_app(self.app.on_command_result, self.mod, label,
                             str(e))
            except OSError as e:
                self._to_app(self.app.on_command_result, self.mod, label,
                             f"link error: {e}")
            else:
                self._to_app(self.app.on_command_result, self.mod, label, None)
            self._cycle = 0     # a write may have moved a slow parameter
            self._due = 0.0

    def _poll(self) -> None:
        slow_turn = self._cycle % SLOW_EVERY == 0
        self._cycle += 1
        # A parameter the module rejects is dropped for good.  Anything else
        # -- a timeout, a closed socket -- is the link failing and ends the
        # cycle, because the next read would only fail the same way.
        dropped: list[str] = []
        try:
            for col in list(self.columns):
                if not (col.fast or slow_turn):
                    continue
                try:
                    self._values[col.par] = self.dev.read_all(self._wire[col.par])
                except HVError as e:
                    if not e.unknown_param:
                        raise
                    dropped.append(col.par)
            for p in list(self._bd_fast) + (list(self._bd_slow) if slow_turn
                                            else []):
                try:
                    self._board[p] = self.dev.mon_bd(self._wire[p])
                except HVError as e:
                    if not e.unknown_param:
                        raise
                    dropped.append(p)
        except (HVError, OSError) as e:
            self._emit(Sample(error=str(e), stamp=time.time()))
            return

        if dropped:
            self._drop(dropped)

        n = self.dev.nch
        rows = []
        for c in range(n):
            row = {}
            for col in self.columns:
                vals = self._values.get(col.par, [])
                row[col.par] = vals[c] if c < len(vals) else ""
            rows.append(row)
        self._emit(Sample(channels=rows, board=dict(self._board),
                          stamp=time.time()))

    def _drop(self, names: list[str]) -> None:
        """Stop polling parameters the module denies having, and say so once."""
        gone = set(names)
        self.columns = [c for c in self.columns if c.par not in gone]
        self._bd_fast = [p for p in self._bd_fast if p not in gone]
        self._bd_slow = [p for p in self._bd_slow if p not in gone]
        for name in gone:
            self._values.pop(name, None)
            self._board.pop(name, None)
        self._to_app(self.app.on_params_dropped, self.mod, sorted(gone))

    def _emit(self, sample: Sample) -> None:
        self._to_app(self.app.on_sample, self.mod, sample)


# --------------------------------------------------------------------------
# Modals
# --------------------------------------------------------------------------

class ArrowFocus:
    """Makes left/right walk the dialog's buttons.

    The App-level `focus_next` action does not resolve from a screen binding,
    so each dialog exposes its own.
    """

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()


class ConfirmScreen(ArrowFocus, ModalScreen[bool]):
    """Yes/no gate in front of anything that changes the output state."""

    BINDINGS = [
        Binding("escape,n", "dismiss(False)", "Cancel", show=False),
        Binding("y", "dismiss(True)", "Confirm", show=False),
        Binding("left", "focus_previous", "", show=False),
        Binding("right", "focus_next", "", show=False),
    ]

    def __init__(self, title: str, detail: str, danger: bool = True,
                 default_yes: bool = False):
        super().__init__()
        self.title_text = title
        self.detail = detail
        self.danger = danger
        self.default_yes = default_yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.danger else ""):
            yield Label(self.title_text, id="dlg-title")
            yield Label(self.detail, id="dlg-detail")
            with Horizontal(id="dlg-buttons"):
                yield Button("Confirm", variant="error" if self.danger
                             else "primary", id="yes")
                yield Button("Cancel", variant="default", id="no")

    def on_mount(self) -> None:
        self.query_one("#yes" if self.default_yes else "#no", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class EditScreen(ArrowFocus, ModalScreen[str | None]):
    """Edit one parameter of one channel, or of every selected channel.

    Numeric parameters get a text field validated against the module's own
    INFO descriptor; two-valued ones (PDWN, IMRANGE) get a button per state,
    which avoids typing a string the module will only reject.

    The title names every channel that will be written to.  With more than one
    target the dialog is the only place that says so before the write goes
    out, so it says it plainly.
    """

    # left/right only ever reach the screen when a Button has focus -- a
    # focused Input binds them to cursor movement and consumes them first.
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("left", "focus_previous", "", show=False),
        Binding("right", "focus_next", "", show=False),
    ]

    def __init__(self, channels: list[int], col: Column, current: str,
                 info: ParamInfo, mixed: bool = False,
                 names: dict[int, str] | None = None):
        super().__init__()
        self.channels = channels
        self.col = col
        self.current = current
        self.info = info
        self.mixed = mixed          # the targets do not all hold one value
        self.choices = info.choices
        self.names = names or {}

    def compose(self) -> ComposeResult:
        n = len(self.channels)
        with Vertical(id="dialog"):
            title = f"CH {fmt_channels(self.channels)}"
            if n > 1:
                title += f"  ({n} channels)"
            elif self.names.get(self.channels[0]):
                # With one target there is room to say what it is, and this
                # dialog is the last thing seen before the write goes out.
                title += f"  {self.names[self.channels[0]]}"
            yield Label(f"{title}  •  {self.col.par}", id="dlg-title")
            hint = self.info.range_text() or "no descriptor from module"
            cur = "mixed" if self.mixed else (self.current or "--")
            yield Label(f"current {cur}    range {hint}", id="dlg-detail")
            if self.choices:
                with Horizontal(id="dlg-buttons"):
                    for i, choice in enumerate(self.choices):
                        yield Button(choice, id=f"choice-{i}",
                                     variant="primary"
                                     if not self.mixed and choice.upper()
                                     == self.current.upper()
                                     else "default")
            else:
                yield Input(value=self.current, id="value")
                yield Label("", id="dlg-error")
                with Horizontal(id="dlg-buttons"):
                    yield Button("Apply", variant="primary", id="apply")
                    yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.choices:
            self.query_one("#choice-0", Button).focus()
        else:
            field = self.query_one("#value", Input)
            field.focus()
            field.action_end()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("choice-"):
            self.dismiss(self.choices[int(bid.split("-")[1])])
        elif bid == "apply":
            self._submit()
        else:
            self.dismiss(None)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        raw = self.query_one("#value", Input).value
        try:
            self.dismiss(self.info.validate(raw))
        except ValueError as e:
            self.query_one("#dlg-error", Label).update(Text(str(e), "bold red"))


class NameScreen(ArrowFocus, ModalScreen["str | None"]):
    """Give one channel a name, or take its name away.

    Nothing is written to the module: no 803x has a channel-name parameter, so
    the name is kept with the connection profile and follows the module.  A
    blank field returns "" and clears it; cancelling returns None and changes
    nothing.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("left", "focus_previous", "", show=False),
        Binding("right", "focus_next", "", show=False),
    ]

    def __init__(self, ch: int, current: str, saved_to: str = ""):
        super().__init__()
        self.ch = ch
        self.current = current
        self.saved_to = saved_to        # profile name, or "" when unsaveable

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Name channel {self.ch}", id="dlg-title")
            where = (f"Saved with the profile {self.saved_to}."
                     if self.saved_to else
                     "This link has no profile, so the name lasts only as "
                     "long as this session.")
            yield Label(f"What is on the far end of the cable.  {where}  "
                        f"Blank removes the name.", id="dlg-detail")
            # Stopped at the limit rather than refused at the end of typing:
            # there is nothing to argue about, the column is only so wide.
            yield Input(value=self.current, placeholder="e.g. Endcap A top",
                        max_length=MAX_LABEL, id="value")
            yield Label("", id="dlg-error")
            with Horizontal(id="dlg-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        field = self.query_one("#value", Input)
        field.focus()
        field.action_end()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit()
        else:
            self.dismiss(None)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            self.dismiss(clean_label(self.query_one("#value", Input).value))
        except ValueError as e:
            self.query_one("#dlg-error", Label).update(Text(str(e), "bold red"))


class AdjustScreen(ArrowFocus, ModalScreen["dict[int, str] | None"]):
    """Shift one numeric parameter of every target channel by a step.

    A relative change lands on a different number in every channel, so the
    dialog writes out what each one will be set to rather than leaving the
    arithmetic to a reader standing in front of a live supply.  It returns
    those numbers, already absolute: the protocol has no relative SET.

    Only setpoints are editable, and a setpoint does not move on its own, so
    the readings this starts from stay true while the dialog is open.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("left", "focus_previous", "", show=False),
        Binding("right", "focus_next", "", show=False),
    ]

    def __init__(self, channels: list[int], col: Column, info: ParamInfo,
                 currents: dict[int, str],
                 names: dict[int, str] | None = None):
        super().__init__()
        self.channels = channels
        self.col = col
        self.info = info
        self.currents = currents
        self.names = names or {}

    def compose(self) -> ComposeResult:
        n = len(self.channels)
        with Vertical(id="dialog"):
            title = f"Adjust CH {fmt_channels(self.channels)}"
            if n > 1:
                title += f"  ({n} channels)"
            yield Label(f"{title}  •  {self.col.par}", id="dlg-title")
            hint = self.info.range_text() or "no descriptor from module"
            yield Label(f"step — 50 raises, -50 lowers    range {hint}",
                        id="dlg-detail")
            yield Input(placeholder="e.g. 50 or -25", id="step")
            with VerticalScroll(id="preview"):
                yield Static(id="plan")
            yield Label("", id="dlg-error")
            with Horizontal(id="dlg-buttons"):
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#step", Input).focus()
        self._repaint()

    @on(Input.Changed)
    def _changed(self) -> None:
        self._repaint()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self._submit()
        else:
            self.dismiss(None)

    def _step(self) -> float | None:
        """The step as typed, or None while there is nothing to apply yet."""
        raw = self.query_one("#step", Input).value.strip()
        if raw.startswith("+"):         # explicit sign, the same as none
            raw = raw[1:].strip()
        if not raw or raw == "-":       # mid-keystroke, not an error yet
            return None
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a number")

    def _plan(self) -> tuple[list[tuple[int, str, str, str]], str]:
        """([(channel, current, new, problem)], error) for the step as typed.

        A channel whose reading has not arrived, or whose result the module's
        own descriptor puts out of range, is a problem: with one step and many
        different starting points, the reader cannot be expected to have
        spotted the one channel it does not suit.
        """
        step = self._step()
        rows: list[tuple[int, str, str, str]] = []
        bad: list[int] = []
        for c in self.channels:
            cur = self.currents.get(c, "").strip()
            if step is None:
                rows.append((c, cur or "--", "", ""))
                continue
            try:
                base = float(cur)
            except ValueError:
                rows.append((c, cur or "--", "?", f"no {self.col.par} reading"))
                bad.append(c)
                continue
            new = self.info.format(base + step)
            try:
                rows.append((c, cur, self.info.validate(new), ""))
            except ValueError as e:
                rows.append((c, cur, new, str(e)))
                bad.append(c)
        if not bad:
            return rows, ""
        problem = next(r[3] for r in rows if r[3])
        return rows, f"CH {fmt_channels(bad)}: {problem}"

    def _repaint(self) -> None:
        try:
            rows, err = self._plan()
        except ValueError as e:
            rows, err = [], str(e)
        # The names of the targets, when they have any: a row of the plan says
        # what is about to change, and "Endcap A" is what the operator knows.
        width = max((len(self.names.get(c, "")) for c in self.channels),
                    default=0)
        body = Text()
        for i, (ch, cur, new, problem) in enumerate(rows):
            if i:
                body.append("\n")
            body.append(f"CH {ch:>2}  ")
            if width:
                body.append(f"{self.names.get(ch, ''):<{width}}  ", "dim")
            body.append(f"{cur:>10}", "dim")
            if new:
                body.append("  →  ")
                body.append(f"{new:>10}", "bold red" if problem else "bold")
        self.query_one("#plan", Static).update(body)
        self.query_one("#dlg-error", Label).update(
            Text(err, "bold red") if err else Text(""))

    def _submit(self) -> None:
        try:
            rows, err = self._plan()
        except ValueError as e:
            rows, err = [], str(e)
        if not err and not any(new for _, _, new, _ in rows):
            err = "step required"
        if err:
            self.query_one("#dlg-error", Label).update(Text(err, "bold red"))
            return
        self.dismiss({ch: new for ch, _cur, new, _problem in rows})


# --------------------------------------------------------------------------
# Connection profiles
# --------------------------------------------------------------------------

class ProfileEditScreen(ArrowFocus, ModalScreen["Profile | None"]):
    """Create or edit one module profile.

    Returns the profile; saving it to disk is the caller's business, so a
    cancelled dialog cannot leave a half-written store behind.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
    ]

    def __init__(self, store: ProfileStore, profile: Profile | None = None):
        super().__init__()
        self.store = store
        self.original = profile
        self.profile = profile or Profile(name="", host="", port=DEFAULT_PORT)
        self._serial = self.profile.is_serial   # which the second field means

    def compose(self) -> ComposeResult:
        p = self.profile
        with Vertical(id="dialog"):
            yield Label("Edit module" if self.original else "New module",
                        id="dlg-title")
            yield Label("A name you will recognise, and where the module "
                        "answers: an IP address or hostname for Ethernet, "
                        "a port like /dev/ttyACM0 for USB.", id="dlg-detail")
            with Horizontal(classes="field"):
                yield Label("Name", classes="flabel")
                yield Input(value=p.name, placeholder="Lab rack A", id="f-name")
            with Horizontal(classes="field"):
                yield Label("Address", classes="flabel")
                yield Input(value=p.device if p.is_serial else p.host,
                            placeholder="192.168.0.250  or  /dev/ttyACM0",
                            id="f-addr")
            with Horizontal(classes="field"):
                # Carries the baud rate for a serial profile; the label says which.
                yield Label("Baud" if p.is_serial else "Port", classes="flabel",
                            id="f-port-label")
                yield Input(value=str(p.baud if p.is_serial else p.port),
                            placeholder=str(DEFAULT_PORT), id="f-port")
            yield Label("", id="dlg-error")
            with Horizontal(id="dlg-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()

    @on(Input.Changed, "#f-addr")
    def _addr_changed(self, event: Input.Changed) -> None:
        """Follow what is being typed: a /dev path means baud, not port.

        The second field is relabelled rather than hidden, so the layout does
        not jump under the cursor while someone is still typing an address.
        """
        serial = looks_like_device(event.value)
        if serial == self._serial:
            return
        self._serial = serial
        self.query_one("#f-port-label", Label).update("Baud" if serial else "Port")
        default = DEFAULT_BAUD if serial else DEFAULT_PORT
        other = DEFAULT_PORT if serial else DEFAULT_BAUD
        field = self.query_one("#f-port", Input)
        field.placeholder = str(default)
        # Only rewrite a value the user has not chosen for themselves.
        if field.value.strip() in ("", str(other)):
            field.value = str(default)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self._save()

    def _save(self) -> None:
        text = {i.id: i.value for i in self.query(Input)}
        # Every other profile's name, so renaming to your own name is fine.
        taken = [p.name for p in self.store.profiles
                 if not (self.original and p.name == self.original.name)]
        # The channel names are not on this form, and readdressing a module --
        # or renaming it -- does not change what its channels are wired to.
        labels = dict(self.original.labels) if self.original else {}
        try:
            if looks_like_device(text["f-addr"]):
                device = clean_device(text["f-addr"])
                baud = clean_baud(text["f-port"])
                name = clean_name(text["f-name"], taken)
                profile = Profile(name=name, kind="serial", device=device,
                                  baud=baud, labels=labels)
            else:
                host, embedded = clean_host(text["f-addr"])
                port = (embedded if embedded is not None
                        else clean_port(text["f-port"]))
                name = clean_name(text["f-name"], taken)
                profile = Profile(name=name, host=host, port=port,
                                  labels=labels)
        except ValueError as e:
            self.query_one("#dlg-error", Label).update(Text(str(e), "bold red"))
            return
        self.dismiss(profile)


class ConnectScreen(ModalScreen["Profile | None"]):
    """Pick a module to talk to, or edit the list of them.

    Dismissing with a profile means "connect to this"; with None it means
    "carry on with whatever links I already have", which for a session that
    has none is the same as quitting.

    A profile already open in a tab is marked as such: connecting to it a
    second time would double that module's poll traffic and give two tabs that
    disagree, so the panel goes to the open one instead.
    """

    BINDINGS = [
        # No priority: a focused DataTable turns enter into RowSelected and a
        # focused Button presses itself, which is what you want in both cases.
        Binding("enter", "connect", "Connect"),
        Binding("n", "new", "New"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        # The panel opens on this screen, so its help has to be reachable from
        # here too -- a modal does not inherit the App's bindings.
        Binding("question_mark,f1", "help", "Help", show=False),
        Binding("escape,q", "back", "Back", show=False),
    ]

    def __init__(self, store: ProfileStore, current: str = "",
                 can_cancel: bool = False,
                 open_names: list[str] | None = None):
        super().__init__()
        self.store = store
        self.current = current
        self.can_cancel = can_cancel
        self.open_names = {n.casefold() for n in (open_names or [])}
        self.rows: list[Profile] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Connect to a module", id="dlg-title")
            yield Label("", id="dlg-detail")
            yield DataTable(id="profiles", cursor_type="row")
            yield Label("", id="dlg-error")
            with Horizontal(id="dlg-buttons"):
                yield Button("Connect", variant="primary", id="connect")
                yield Button("New", id="new")
                yield Button("Edit", id="edit")
                yield Button("Delete", variant="error", id="delete")
                yield Button("Cancel" if self.can_cancel else "Quit", id="back")
        # Its own footer, because the one underneath belongs to the panel and
        # lists keys this screen does not answer to.
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#profiles", DataTable)
        table.add_column("Name", width=20, key="name")
        # Wide enough for a full USB port path and its baud rate, which run
        # longer than any host:port.
        table.add_column("Address", width=32, key="addr")
        table.add_column("", width=10, key="note")
        self._reload(select=self.current or self.store.last_used)
        table.focus()

    # -- list --------------------------------------------------------------

    def _reload(self, select: str = "") -> None:
        table = self.query_one("#profiles", DataTable)
        table.clear()
        self.rows = list(self.store.profiles) + [SIM_PROFILE]
        for p in self.rows:
            # "open" first: which tab a name is already in decides what enter
            # will do to it, and that matters more than which was last used.
            open_here = p.name.casefold() in self.open_names
            note = Text("open", "cyan") if open_here else Text(
                "last used" if p.name == self.store.last_used else "", "dim")
            table.add_row(Text(p.name, "bold" if not p.is_sim else "dim"),
                          Text(p.address, "" if not p.is_sim else "dim"), note)
        for i, p in enumerate(self.rows):
            if select and p.name.casefold() == select.casefold():
                table.move_cursor(row=i)
                break
        detail = (f"{len(self.store.profiles)} saved  •  "
                  f"enter connects, n new, e edit, d delete, ? help")
        if not self.store.profiles:
            detail = "No modules saved yet — press n to add one, ? for help."
        self.query_one("#dlg-detail", Label).update(detail)
        if self.store.load_error:
            self._error(self.store.load_error)

    @property
    def selected(self) -> Profile | None:
        i = self.query_one("#profiles", DataTable).cursor_row
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _error(self, message: str) -> None:
        self.query_one("#dlg-error", Label).update(Text(message, "bold red"))

    def _log(self, markup: str) -> None:
        """Edits to the profile store go in the panel's log too -- it is the
        one place that records what the operator did this session, and the
        dialog itself is gone by the time anyone reads it."""
        log_line = getattr(self.app, "log_line", None)
        if log_line is not None:
            log_line(markup)

    def _persist(self) -> bool:
        try:
            self.store.save()
        except OSError as e:
            self._error(f"cannot write {self.store.path}: {e}")
            return False
        return True

    # -- actions -----------------------------------------------------------

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_connect()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()

    def action_connect(self) -> None:
        if self.selected is not None:
            self.dismiss(self.selected)

    def action_back(self) -> None:
        self.dismiss(None)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen(str(self.store.path),
                                        getattr(self.app, "read_only", False)))

    @work
    async def action_new(self) -> None:
        prof = await self.app.push_screen_wait(ProfileEditScreen(self.store))
        if prof is None:
            return
        self.store.upsert(prof)
        if self._persist():
            self._log(f"[cyan]profile {prof.name} added ({prof.address})"
                      f"[/cyan]")
            self._reload(select=prof.name)

    @work
    async def action_edit(self) -> None:
        prof = self.selected
        if prof is None or prof.is_sim:
            self._error("the simulator has nothing to configure")
            return
        edited = await self.app.push_screen_wait(
            ProfileEditScreen(self.store, prof))
        if edited is None:
            return
        self.store.upsert(edited, old_name=prof.name)
        if self._persist():
            what = (f"{prof.name} renamed to {edited.name}"
                    if edited.name != prof.name else f"{edited.name} edited")
            self._log(f"[cyan]profile {what} ({edited.address})[/cyan]")
            self._reload(select=edited.name)

    @work
    async def action_delete(self) -> None:
        prof = self.selected
        if prof is None or prof.is_sim:
            self._error("the simulator cannot be deleted")
            return
        ok = await self.app.push_screen_wait(ConfirmScreen(
            f"Delete profile {prof.name}?",
            f"{prof.address} will be forgotten.  The module itself is not "
            "touched.", danger=False, default_yes=False))
        if not ok:
            return
        self.store.delete(prof.name)
        if self._persist():
            self._log(f"[cyan]profile {prof.name} deleted[/cyan]")
            self._reload()


# --------------------------------------------------------------------------
# Help
# --------------------------------------------------------------------------

# How Textual spells a key, and how a person does.  The help screen is read in
# front of a live supply, so it shows `?` and `esc`, not `question_mark`.
KEY_NAMES = {"question_mark": "?", "escape": "esc", "equals_sign": "=",
             "plus": "+", "minus": "-", "up": "↑", "down": "↓", "left": "←",
             "right": "→", "enter": "enter", "space": "space",
             "left_square_bracket": "[", "right_square_bracket": "]"}


@dataclass(frozen=True)
class HelpKey:
    """One line of the key list.

    `keys` holds the names Textual binds, not the pretty forms, so that a test
    can check every binding the panel answers to is described here.  A key that
    does something in front of a supply and is documented nowhere is the one
    that gets pressed by accident.
    """
    keys: tuple[str, ...]
    text: str
    # For a run of keys that all do the same kind of thing.  The nine digits
    # spelled out fill the key column twice over and say nothing `1…9` does not.
    show_as: str = ""

    @property
    def pretty(self) -> str:
        return self.show_as or " ".join(KEY_NAMES.get(k, k) for k in self.keys)


HELP_SECTIONS: list[tuple[str, list[HelpKey]]] = [
    ("Moving about", [
        HelpKey(("up", "down", "left", "right"),
                "move the cursor.  The column it sits in is the parameter "
                "that enter and d act on"),
        HelpKey(("enter",),
                "edit the cell under the cursor — one value, written to every "
                "selected channel"),
    ]),
    ("Selection", [
        HelpKey(("s",), "select the channel under the cursor, or drop it"),
        HelpKey(("a",), "select every channel"),
        HelpKey(("u", "escape"), "select none"),
        HelpKey((), "With nothing selected an edit goes to the channel under "
                    "the cursor.  On, off and ALL OFF ignore the selection "
                    "entirely — X already switches everything off."),
    ]),
    ("Values", [
        HelpKey(("enter",), "set one value on every target"),
        HelpKey(("d",),
                "adjust: shift every target by one step from wherever it "
                "already sits.  The dialog lists each current → new pair, and "
                "refuses the whole step if it would put any target outside "
                "the module's range"),
        HelpKey(("n",),
                "name the channel.  Names are ours, not the module's: they "
                "are kept with the profile and never sent"),
    ]),
    ("High voltage", [
        HelpKey(("space",), "switch the channel under the cursor on or off"),
        HelpKey(("o", "f"), "on, off — when a toggle is not what you want"),
        HelpKey(("X",), "ALL OFF: every live channel on the module in front "
                        "of you.  Modules in other tabs are not touched"),
        HelpKey(("c",), "clear the board alarm and latched trips"),
        HelpKey((), "Every one of these asks first.  All of them need the "
                    "module in REMOTE; in LOCAL the front panel has it and "
                    "each SET comes back refused."),
    ]),
    ("Modules", [
        HelpKey(("m",), "connect another module — it opens in a new tab, and "
                        "nothing already connected is disturbed"),
        HelpKey(("right_square_bracket", "left_square_bracket"),
                "next tab, previous tab"),
        HelpKey(tuple("123456789"), "go straight to that tab, by the number "
                                    "the bar shows", show_as="1…9"),
        HelpKey(("w",),
                "close this tab.  It asks first if any channel is live: the "
                "module keeps its output after the panel stops watching it"),
        HelpKey((), "Every tab keeps polling, so the marker beside its name "
                    "stays true: ● live, ○ all off, ! something to look at, "
                    "‖ paused, … nothing read yet.  Everything else on this "
                    "list — every key, the log, the selection — acts on the "
                    "tab in front of you and no other."),
    ]),
    ("Session", [
        HelpKey(("r",), "read everything now, without waiting for the poll"),
        HelpKey(("p",), "pause polling on this module, or resume it"),
        HelpKey(("plus", "equals_sign", "minus"),
                "poll slower, faster.  The period doubles and halves between "
                "0.25s and 30s; = is + without the shift.  Each module keeps "
                "its own rate"),
        HelpKey(("question_mark", "f1"), "this help"),
        HelpKey(("q",),
                "quit.  Channels are left exactly as they are — quitting the "
                "panel does not switch anything off, on any module"),
    ]),
]

HELP_WIDTH = 64

HELP_READING = [
    ("VMon, IMon", "green within regulation, yellow while ramping, red when "
                   "a channel that is not ramping sits off its setpoint, dim "
                   "when the output is off"),
    ("VSet, ISet", "what the channel is asked for.  Editable"),
    ("Name", "yours, from n.  Only in the table once some channel has one"),
    ("Status", "the module's STATUS word, flag by flag"),
]


class HelpScreen(ModalScreen[None]):
    """The panel's keys and what the table is showing, without leaving it.

    Every key is here, including the ones the footer has no room for, and the
    STATUS legend is coloured the way the table colours it -- the point of the
    legend is to match what is on screen a line above it.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("q,question_mark,f1", "dismiss(None)", "Close", show=False),
    ]

    def __init__(self, store_path: str = "", read_only: bool = False):
        super().__init__()
        self.store_path = store_path
        self.read_only = read_only

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("hvctl — keys and readings", id="dlg-title")
            detail = "esc or ? closes  •  ↑ ↓ scrolls"
            if self.read_only:
                detail = "READ-ONLY: every SET is refused  •  " + detail
            yield Label(detail, id="dlg-detail")
            with VerticalScroll(id="help-body"):
                # One Static, not one per section: the blank line between
                # sections is then part of the text and stays one line.
                yield Static(self.body())
            with Horizontal(id="dlg-buttons"):
                yield Button("Close", variant="primary", id="close")
        # The panel's own footer lists the panel's keys, not this screen's.
        yield Footer()

    def body(self) -> Text:
        out = Text()
        for title, rows in HELP_SECTIONS:
            out.append_text(self._section(title, rows))
        out.append_text(self._reading())
        out.append_text(self._flags())
        out.append_text(self._notes())
        return out

    def on_mount(self) -> None:
        self.query_one("#help-body", VerticalScroll).focus()

    @on(Button.Pressed)
    def _pressed(self) -> None:
        self.dismiss(None)

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _heading(title: str) -> Text:
        return Text(title + "\n", "bold underline")

    def _section(self, title: str, rows: list[HelpKey]) -> Text:
        out = self._heading(title)
        for row in rows:
            if not row.keys:                # a note about the section above
                out.append("  " + _wrap(row.text, 2) + "\n", "dim")
                continue
            out.append(f"  {row.pretty:<12}", "bold cyan")
            out.append(_wrap(row.text, 14) + "\n")
        out.append("\n")
        return out

    def _reading(self) -> Text:
        out = self._heading("Reading the table")
        for name, text in HELP_READING:
            out.append(f"  {name:<12}", "bold")
            out.append(_wrap(text, 14) + "\n")
        out.append("\n")
        return out

    def _flags(self) -> Text:
        out = self._heading("Status flags")
        for _, flag in STATUS_BITS:
            style = ("bold red" if flag in ALARM_FLAGS else
                     "yellow" if flag in BUSY_FLAGS else
                     "bold green" if flag == "ON" else "dim")
            out.append(f"  {flag:<12}", style)
            out.append(_wrap(STATUS_HELP.get(flag, ""), 14) + "\n")
        out.append("\n")
        return out

    def _notes(self) -> Text:
        out = self._heading("Where things are kept")
        out.append("  Modules and channel names are saved in\n", "dim")
        out.append(f"    {_tilde(self.store_path) or 'the profile store'}\n")
        out.append("  " + _wrap(
            "A module reached by --host or --sim has no profile, so names "
            "given to it last only for this session.", 2) + "\n\n", "dim")
        out.append("  " + _wrap(
            "Not every module has every parameter.  A column the module "
            "denies having is dropped from the table, and the log says which.",
            2) + "\n", "dim")
        return out


def _tilde(path: str) -> str:
    """`~/.config/hvctl/profiles.json` rather than the whole thing: the help
    is 64 columns wide and a home directory is not news to the reader."""
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def _wrap(text: str, indent: int, width: int = HELP_WIDTH) -> str:
    """Wrap a description under a key column `indent` wide.

    Wrapped here rather than by the widget: Rich would re-wrap the overflow
    hard against the left margin, which loses the hanging indent that makes
    the key column readable.  `width` is what fits `#help-body` inside a 76
    column dialog once its border, padding and scrollbar are taken out.
    """
    room = max(20, width - indent)
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > room:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    return ("\n" + " " * indent).join(lines)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class HVApp(App):
    TITLE = "hvctl"

    CSS = """
    /* One line, and only there once something is connected: on a 24-row
       terminal the header, board line, log and footer already cost ten. */
    #tabs {
        height: 1;
        padding: 0 1;
        background: $panel-darken-1;
    }
    #tabs.gone { display: none; }
    #board {
        height: auto;
        padding: 0 1;
        background: $panel;
        border-bottom: solid $primary;
    }
    #chans { height: 1fr; }
    #log {
        height: 7;
        border-top: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    ConfirmScreen, EditScreen, AdjustScreen, NameScreen, ConnectScreen,
    ProfileEditScreen, HelpScreen {
        align: center middle;
    }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #dialog.wide { width: 76; }
    #dialog.danger { border: thick $error; }
    #dlg-title { text-style: bold; width: 100%; }
    #dlg-detail { color: $text-muted; width: 100%; margin-bottom: 1; }
    #dlg-error { width: 100%; height: 1; }
    /* Sixteen channels would push the buttons off a short terminal, so the
       plan scrolls once it is taller than half a dozen lines. */
    #preview {
        height: auto;
        max-height: 6;
        margin-top: 1;
        padding: 0 1;
        background: $panel;
    }
    #dlg-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #dlg-buttons Button { margin-left: 2; }
    .field { height: auto; margin-bottom: 1; }
    .flabel { width: 8; padding-top: 1; color: $text-muted; }
    .field Input { width: 1fr; }
    #profiles { height: auto; max-height: 12; border: solid $primary-darken-2; }
    /* The help is longer than any terminal, so its dialog takes what height
       there is and scrolls inside it rather than growing off the screen. */
    HelpScreen #dialog { height: 90%; }
    #help-body { height: 1fr; padding: 0 1; background: $panel; }
    /* Five buttons have to fit an 80-column terminal. */
    ConnectScreen #dlg-buttons Button { min-width: 10; margin-left: 1; }
    """

    BINDINGS = [
        Binding("space", "toggle", "On/Off"),
        Binding("o", "power(True)", "On"),
        Binding("f", "power(False)", "Off"),
        Binding("X", "all_off", "ALL OFF"),
        Binding("s", "select_toggle", "Select"),
        Binding("n", "name_channel", "Name"),
        # Enter sets one value on every target; `d` shifts each target from
        # wherever it already is, which a single SET cannot express.
        Binding("d", "adjust", "Adjust ±"),
        # The footer already has more keys than an 80-column terminal shows;
        # the board line names `u` instead, and only while there is a
        # selection to clear.
        Binding("u", "select_none", "Unselect all", show=False),
        Binding("a", "select_all", "Select all", show=False),
        # Escape clears the selection only here: a modal defines its own keys,
        # so it still means "cancel this dialog" wherever one is open.
        Binding("escape", "select_none", "", show=False),
        Binding("c", "clear_alarm", "Clear alarm"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause"),
        Binding("m", "add_module", "Connect…"),
        # Tabs.  Unshifted and off the arrow keys, which the table has.
        Binding("right_square_bracket", "next_module", "Next module",
                show=False),
        Binding("left_square_bracket", "prev_module", "Previous module",
                show=False),
        Binding("w", "close_module", "Close module", show=False),
        Binding("plus,equals_sign", "slower", "Slower", show=False),
        Binding("minus", "faster", "Faster", show=False),
        Binding("question_mark,f1", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ] + [
        # One binding per digit rather than a key handler: the help screen is
        # checked against BINDINGS, and a key that is not declared here is a
        # key nothing makes sure is documented.
        Binding(str(n), f"goto_module({n})", f"Module {n}", show=False)
        for n in range(1, 10)
    ]

    def __init__(self, dev: Device | None = None, interval: float = 1.0,
                 read_only: bool = False, store: ProfileStore | None = None,
                 timeout: float = 3.0, verbose: bool = False):
        super().__init__()
        # Every module the panel is holding, in tab order, and which one is on
        # screen.  All of them keep polling; only the active one is painted.
        self.modules: list[Module] = []
        self.active_index = -1
        self._first_dev = dev           # a link named on the command line
        self.default_interval = interval
        self.read_only = read_only
        self.store = store if store is not None else ProfileStore()
        self.timeout = timeout
        self.verbose = verbose

    # -- the module on screen ----------------------------------------------
    # Everything below used to be an attribute of the App, back when there was
    # only ever one module.  They stay spelled the same way -- the panel is
    # written throughout in terms of "the module in front of me" -- but they
    # now read through to whichever tab is active.

    @property
    def active(self) -> Module | None:
        if 0 <= self.active_index < len(self.modules):
            return self.modules[self.active_index]
        return None

    @property
    def dev(self) -> Device | None:
        return self.active.dev if self.active else None

    @property
    def worker(self) -> DeviceWorker | None:
        return self.active.worker if self.active else None

    @property
    def nch(self) -> int:
        return self.active.nch if self.active else 0

    @property
    def columns(self) -> list[Column]:
        return self.active.columns if self.active else []

    @property
    def info(self) -> dict[str, ParamInfo]:
        return self.active.info if self.active else {}

    @property
    def board(self) -> dict[str, str]:
        return self.active.board if self.active else {}

    @property
    def flags(self) -> list[list[str]]:
        return self.active.flags if self.active else []

    @flags.setter
    def flags(self, value: list[list[str]]) -> None:
        if self.active:
            self.active.flags = value

    @property
    def values(self) -> list[dict[str, str]]:
        return self.active.values if self.active else []

    @values.setter
    def values(self, value: list[dict[str, str]]) -> None:
        if self.active:
            self.active.values = value

    @property
    def selected(self) -> set[int]:
        """Channels an edit applies to.  Empty means "the one under the
        cursor", which is how the panel behaved before it had a selection."""
        return self.active.selected if self.active else set()

    @selected.setter
    def selected(self, value: set[int]) -> None:
        if self.active:
            self.active.selected = set(value)

    @property
    def labels(self) -> dict[int, str]:
        """Channel number -> name, from the profile.  Ours, not the module's:
        the protocol has nowhere to keep such a thing."""
        return self.active.labels if self.active else {}

    @labels.setter
    def labels(self, value: dict[int, str]) -> None:
        if self.active:
            self.active.labels = dict(value)

    @property
    def profile(self) -> Profile | None:
        return self.active.profile if self.active else None

    @profile.setter
    def profile(self, value: Profile | None) -> None:
        if self.active:
            self.active.profile = value

    @property
    def interval(self) -> float:
        """The active module's poll rate.  Each keeps its own -- a module on a
        9600 baud serial link cannot be read as often as one on Ethernet."""
        return self.active.interval if self.active else self.default_interval

    @property
    def _rows_built(self) -> bool:
        return self.active.ready if self.active else False

    @property
    def _last_error(self) -> str | None:
        return self.active.last_error if self.active else None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ModuleTabs()
        yield Static("connecting…", id="board")
        yield ChannelTable(id="chans", cursor_type="cell",
                           zebra_stripes=True)
        yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        if self.read_only:
            self.log_line("[yellow]read-only mode: SET is disabled[/yellow]")
        self._paint_tabs()
        if self._first_dev is not None:
            self._attach(self._first_dev, None)   # named on the command line
        else:
            self.query_one("#board", Static).update(
                Text("not connected", "dim"))
            self.choose_module()

    def on_unmount(self) -> None:
        for mod in list(self.modules):
            self._detach(mod)

    # -- connection --------------------------------------------------------

    def _attach(self, dev: Device, profile: Profile | None) -> Module:
        """Add a module and put it on screen.  Its poll loop starts here."""
        mod = Module(dev=dev, profile=profile, interval=self.default_interval,
                     labels=dict(profile.labels) if profile else {})
        self.modules.append(mod)
        mod.worker = DeviceWorker(mod, self)
        self.log_line(f"[dim]link {dev.t.describe()}[/dim]", mod)
        # On screen before the poll loop starts: `on_device_ready` builds the
        # table only for the active module, and a fast link can deliver it
        # before the next statement would have run.
        self.show_module(len(self.modules) - 1)
        mod.worker.start()
        return mod

    def _detach(self, mod: Module) -> None:
        """Stop a module's poll loop and drop its link.

        The I/O thread is not joined: it may be blocked in `call_from_thread`
        waiting on this very event loop, and a late result it delivers is
        discarded by `DeviceWorker._dispatch` -- which is why the worker is
        cleared off the module first.
        """
        if mod.worker:
            mod.worker.stop()
            mod.worker = None
        mod.dev.t.close()

    def _forget(self, mod: Module) -> None:
        """Close a module's tab, leaving the panel on whatever is next."""
        self._detach(mod)
        at = self.modules.index(mod)
        keep = None if mod is self.active else self.active
        self.modules.remove(mod)
        # Nothing is active while the list is being rearranged: `show_module`
        # saves the outgoing module's cursor, and the index it would read is
        # about to mean a different module or none at all.
        self.active_index = -1
        if not self.modules:
            self._show_nothing()
        elif keep is not None:
            # A tab closed from under another one: the rest shift left.
            self.show_module(self.modules.index(keep))
        else:
            self.show_module(min(at, len(self.modules) - 1))

    def _show_nothing(self) -> None:
        self.sub_title = ""
        if self.is_running:
            self.query_one("#chans", DataTable).clear(columns=True)
            self.query_one("#board", Static).update(
                Text("not connected — press m to choose a module, ? for help",
                     "dim"))
            self._paint_tabs()

    def find_module(self, name: str) -> Module | None:
        """The open tab for a profile, if it is already on screen."""
        return next((m for m in self.modules if m.profile is not None
                     and m.profile.name.casefold() == name.casefold()), None)

    @work
    async def choose_module(self) -> None:
        """Ask for a profile, connect it as a new tab, and keep asking until
        one works.  Cancelling leaves whatever is already open alone."""
        while True:
            prof = await self.push_screen_wait(ConnectScreen(
                self.store, current=self.profile.name if self.profile else "",
                can_cancel=bool(self.modules),
                open_names=[m.name for m in self.modules]))
            if prof is None:
                if not self.modules:
                    self.exit()
                return
            # Dialling the same supply twice would double its poll traffic and
            # give two tabs that disagree with each other.  Go to it instead.
            already = self.find_module(prof.name)
            if already is not None:
                self.show_module(self.modules.index(already))
                self.log_line(f"[cyan]{escape(prof.name)} is already open"
                              f"[/cyan]")
                return
            if await self._connect(prof):
                return

    async def _connect(self, prof: Profile) -> bool:
        self.log_line(f"[dim]connecting to {escape(prof.name)} "
                      f"({prof.address})…[/dim]")
        try:
            # Blocking connect, so off the UI thread it goes.
            dev = await asyncio.to_thread(device_for_profile, prof,
                                          self.timeout, self.verbose)
        except (OSError, HVError) as e:
            self.log_line(f"[red]{escape(prof.name)}: {e}[/red]")
            self.notify(str(e), title=f"cannot reach {escape(prof.name)}",
                        severity="error", timeout=8)
            return False
        self._attach(dev, prof)
        self.log_line(f"[green]connected to {escape(prof.name)}[/green]")
        if not prof.is_sim:
            self.store.remember(prof.name)
        return True

    def action_add_module(self) -> None:
        """`m` opens the picker.  Connecting adds a tab rather than replacing
        the current link -- nothing here disconnects anything, so there is
        nothing to warn about."""
        self.choose_module()

    # -- tabs --------------------------------------------------------------

    def show_module(self, index: int) -> None:
        """Put a module on screen, keeping the one leaving as it was.

        The table is rebuilt rather than swapped for a second one: a different
        module has different columns, and `_build_table` already does exactly
        this job when the Name column comes and goes.
        """
        if not 0 <= index < len(self.modules):
            return
        if self.active is not None and index != self.active_index:
            self._remember_cursor()
        self.active_index = index
        mod = self.modules[index]
        self.sub_title = (f"{mod.profile.name} — {mod.dev.t.describe()}"
                          if mod.profile else mod.dev.t.describe())
        if mod.ready:
            self._build_table(mod.cursor)
        else:
            # Still resolving its parameters; `on_device_ready` builds it.
            self.query_one("#chans", DataTable).clear(columns=True)
        self._paint_board(mod.stamp or time.time())
        self._paint_tabs()

    def _remember_cursor(self) -> None:
        """Keep where the cursor was, so coming back to a tab finds it there."""
        try:
            table = self.query_one("#chans", DataTable)
        except NoMatches:
            return
        if self.active is not None and self.active.ready:
            self.active.cursor = table.cursor_coordinate

    def action_next_module(self) -> None:
        if len(self.modules) > 1:
            self.show_module((self.active_index + 1) % len(self.modules))

    def action_prev_module(self) -> None:
        if len(self.modules) > 1:
            self.show_module((self.active_index - 1) % len(self.modules))

    def action_goto_module(self, n: int) -> None:
        """`1`..`9` -- the numbers the tab bar shows."""
        if n <= len(self.modules):
            self.show_module(n - 1)

    @work
    async def action_close_module(self) -> None:
        """Close this tab.  The module keeps doing whatever it was doing.

        This is where the old `m` warning went: switching tabs disconnects
        nothing, but closing one does, and a supply that is still energised
        after the panel stops watching it is worth a sentence.
        """
        mod = self.active
        if mod is None:
            return
        live = mod.live
        # Profile names are not checked for console markup the way channel
        # names are, and a dialog renders it.
        where = escape(mod.name)
        # An empty `flags` is how a module looks both when every channel is off
        # and when nothing has been read yet.  Silence from a supply that may
        # well have its outputs up is not the same answer as "nothing is on",
        # so the unknown asks too, and says which of the two it is asking.
        if live or not mod.known:
            ok = await self.push_screen_wait(ConfirmScreen(
                f"Close {where} with channels live?" if live else
                f"Close {where} before the first reading?",
                f"{', '.join(self.ch_label(c) for c in live)} stay energised — "
                "the module keeps its output when nothing is watching it."
                if live
                else "No channel has been read yet, so whether any output is "
                     "energised is not known here — and the module keeps its "
                     "output when nothing is watching it.",
                danger=True, default_yes=False))
            if not ok:
                return
            if mod not in self.modules:
                return          # closed from elsewhere while the dialog was up
        self.log_line(f"[cyan]closed {escape(mod.name)}[/cyan]")
        self._forget(mod)

    def _paint_tabs(self) -> None:
        """Redraw the tab bar, and record where each tab starts for clicks."""
        try:
            bar = self.query_one(ModuleTabs)
        except NoMatches:
            return
        bar.set_class(not self.modules, "gone")
        if not self.modules:
            bar.spans = []
            return
        # A tab that has been scrolled off the edge is a module the operator
        # cannot see the state of, so names are shortened to keep every tab on
        # the line rather than letting the last ones fall off it.
        width = bar.content_size.width or max(20, self.size.width - 2)
        room = width - HINT_WIDTH if len(self.modules) > 1 else width
        cap = max(4, room // len(self.modules) - TAB_FURNITURE)

        line, spans = Text(), []
        for i, mod in enumerate(self.modules):
            start = len(line.plain)
            active = i == self.active_index
            line.append(f" {i + 1} ", "bold" if active else "dim")
            name = mod.name
            line.append(name if len(name) <= cap else name[:cap - 1] + "…",
                        "bold reverse" if active else "")
            line.append(" ")
            line.append_text(self._tab_mark(mod))
            line.append("  ")
            spans.append((start, len(line.plain)))
        if len(self.modules) > 1 and len(line.plain) + HINT_WIDTH <= width:
            line.append(TAB_HINT, "dim")
        bar.spans = spans
        bar.update(line)

    @staticmethod
    def _tab_mark(mod: Module) -> Text:
        if mod.alarmed:
            mark = Text(TAB_ALARM, "bold red")
        elif not mod.ready or not mod.known:
            mark = Text(TAB_WAIT, "dim")
        elif mod.live:
            mark = Text(TAB_LIVE, "bold green")
        else:
            mark = Text(TAB_OFF, "dim")
        if mod.paused:
            mark.append(TAB_PAUSED, "yellow")
        return mark

    # -- callbacks from the I/O thread -------------------------------------

    def on_device_ready(self, mod: Module, nch: int, columns: list[Column],
                        infos: dict[str, ParamInfo], board: dict[str, str],
                        missing: list[str]) -> None:
        mod.nch = nch
        mod.columns = columns
        mod.info = infos
        mod.board.update(board)
        mod.flags = [[] for _ in range(nch)]
        mod.values = [{} for _ in range(nch)]
        mod.selected.clear()
        mod.ready = True
        if mod is self.active:
            self._build_table()
        name = board.get("BDNAME", "?")
        self.log_line(f"[green]{name}[/green]  sn {board.get('BDSNUM', '?')}"
                      f"  fw {board.get('BDFREL', '?')}  {nch} channels", mod)
        if missing:
            self.log_line(f"[dim]not offered by this module: "
                          f"{', '.join(missing)}[/dim]", mod)
        if board.get("BDCTR", "").upper() != "REMOTE":
            self.log_line("[bold yellow]module is in LOCAL mode — it will "
                          "refuse every write until the front panel is set to "
                          "REMOTE[/bold yellow]", mod)
        self._paint_tabs()

    def on_sample(self, mod: Module, sample: Sample) -> None:
        """A reading, from any tab.  Only the active one is painted; the rest
        is kept so switching to a tab shows what that module is doing now."""
        if sample.error:
            if mod is self.active:
                self.query_one("#board", Static).update(
                    Text(f"link error: {sample.error}", "bold white on red"))
            # A dead link repeats the same error every poll; log the change,
            # not the repetition.
            if sample.error != mod.last_error:
                self.log_line(f"[red]{sample.error}[/red]", mod)
                mod.last_error = sample.error
                self._paint_tabs()
            return
        if mod.last_error:
            self.log_line("[green]link recovered[/green]", mod)
            mod.last_error = None
        mod.board.update(sample.board)
        mod.stamp = sample.stamp
        for c, row in enumerate(sample.channels):
            if c >= mod.nch:
                break
            _, mod.flags[c] = decode_status(row.get("STATUS", "0"))
            mod.values[c] = row
        if mod is self.active:
            if mod.ready:
                self._paint(sample)
            self._paint_board(sample.stamp)
        self._paint_tabs()

    def on_params_dropped(self, mod: Module, names: list[str]) -> None:
        """The module rejected a parameter it had advertised (or never listed).

        It will not be asked for again, so this is said once rather than once
        a second, and the column goes with it -- a header over permanently
        blank cells reads as a broken link, which it is not.
        """
        mod.columns = [c for c in mod.columns if c.par not in set(names)]
        for name in names:
            mod.board.pop(name, None)       # a stale value would read as live
        if mod is self.active:
            table = self.query_one("#chans", DataTable)
            for name in names:
                try:
                    table.remove_column(name)
                except Exception:
                    pass    # a board parameter, which has no column
            self.fit_columns()      # the width it held is now free
        self.log_line(f"[yellow]{', '.join(names)}: unknown to this module — "
                      f"no longer polled[/yellow]", mod)

    def on_command_result(self, mod: Module, label: str,
                          error: str | None) -> None:
        if error:
            self.log_line(f"[red]✗ {label}: {error}[/red]", mod)
            # A write that failed on a tab nobody is looking at needs to say
            # which module refused it -- the label alone names a channel.
            title = (label if mod is self.active
                     else f"{escape(mod.name)}: {label}")
            self.notify(error, title=title, severity="error", timeout=6)
        else:
            self.log_line(f"[green]✓[/green] {label}", mod)

    # -- rendering ---------------------------------------------------------

    @property
    def named(self) -> dict[int, str]:
        """The names that belong to channels this module actually has.

        A profile keeps every name it was given.  Pointing it at a smaller
        module hides the ones past the end rather than dropping them: the
        cable was not unplugged because somebody mistyped an address.
        """
        return {c: n for c, n in self.labels.items() if 0 <= c < self.nch}

    def _build_table(self, cursor: Coordinate | None = None) -> None:
        """(Re)build the channel table for the module now on screen.

        Called again when the Name column appears or goes, because Textual can
        only append columns and a name added at the right-hand edge would sit
        nowhere near the channel it belongs to -- and on every tab switch,
        since a different module has different columns.  The cursor is put back
        on the column it was on rather than the index it was at, so a rebuild
        does not move it sideways under the operator's hand.  `cursor` is where
        the arriving module left it; without one, wherever the table is now.
        """
        table = self.query_one("#chans", DataTable)
        was = [k.value for k in table.columns]
        if cursor is None:
            cursor = table.cursor_coordinate
            held = was[cursor.column] if 0 <= cursor.column < len(was) else None
        else:
            # Arriving from another tab, so the column keys on screen belong to
            # the module leaving.  Take the key from the one arriving.
            keys = ["CH"] + ([NAME_COL] if self.named else []) + \
                   [c.par for c in self.columns]
            held = (keys[cursor.column] if 0 <= cursor.column < len(keys)
                    else None)

        table.clear(columns=True)
        # One wider than the channel number needs, for the selection mark.
        table.add_column(Text("CH", justify="right"), width=4, key="CH")
        if self.named:
            table.add_column(Text("Name"), width=self._name_width(),
                             key=NAME_COL)
        for col in self.columns:
            table.add_column(Text(col.title, justify=col.align),
                             width=col.width, key=col.par)
        for c in range(self.nch):
            table.add_row(*self._row_cells(c), key=f"ch{c}")
        self.fit_columns()

        now = [k.value for k in table.columns]
        if held in now:
            table.move_cursor(row=min(cursor.row, max(self.nch - 1, 0)),
                              column=now.index(held))

    def _name_width(self) -> int:
        """As wide as the longest name, and never wider than one can be."""
        return min(MAX_LABEL,
                   max([len("Name")] + [len(n) for n in self.named.values()]))

    def _row_cells(self, c: int) -> list:
        cells = [self._ch_cell(c)]
        if self.named:
            cells.append(self._name_cell(c))
        row = self.values[c] if c < len(self.values) else {}
        # Before the first reading there is nothing to render, and an empty
        # STATUS would come out as OFF -- which is not what silence means.
        cells += ([self._cell(col, row, self.flags[c]) for col in self.columns]
                  if row else ["" for _ in self.columns])
        return cells

    def _name_cell(self, ch: int) -> Text:
        return Text(self.labels.get(ch, ""))

    def _paint(self, sample: Sample) -> None:
        """Render the active module's latest reading.  `on_sample` has already
        stored it -- every module's readings are kept, painted or not."""
        table = self.query_one("#chans", DataTable)
        for c, row in enumerate(sample.channels):
            if c >= self.nch:
                break
            for col in self.columns:
                table.update_cell(f"ch{c}", col.par,
                                  self._cell(col, row, self.flags[c]))

    def fit_columns(self) -> None:
        """Hand the width the table is not using to the STRETCH column.

        Textual has no public way to resize a column after the fact, so the
        width is set on the Column record and the table is told its dimensions
        are stale -- the same flag its own `add_column` sets.
        """
        if not self._rows_built:
            return
        table = self.query_one("#chans", DataTable)
        pad = 2 * table.cell_padding
        stretch, used = None, 0
        for key, column in table.columns.items():
            if key.value == STRETCH:
                stretch = column
            else:
                used += column.get_render_width(table)
        if stretch is None:      # this module has no status column
            return
        base = next((c.width for c in COLUMNS if c.par == STRETCH), 30)
        want = max(base, table.scrollable_content_region.width - used - pad)
        if want == stretch.width:
            return
        stretch.width = want
        table._require_update_dimensions = True
        table.refresh()

    def _fit_names(self) -> None:
        """Follow the longest name the Name column now holds.

        Same trick as `fit_columns`: Textual has no public way to resize a
        column after the fact, so the width goes on the Column record and the
        table is told its dimensions are stale.
        """
        table = self.query_one("#chans", DataTable)
        for key, column in table.columns.items():
            if key.value == NAME_COL:
                want = self._name_width()
                if want != column.width:
                    column.width = want
                    table._require_update_dimensions = True
                break
        self.fit_columns()      # what it gained or gave up is STATUS's

    def _ch_cell(self, ch: int) -> Text:
        """The channel-number cell, carrying the selection mark.

        A glyph rather than only a colour: on a monochrome terminal, or over a
        zebra stripe, colour alone is not enough to tell what a write is about
        to reach.
        """
        if ch in self.selected:
            return Text(f"*{ch}", "bold cyan", justify="right")
        return Text(str(ch), justify="right")

    def _paint_selection(self) -> None:
        if not self._rows_built:
            return
        table = self.query_one("#chans", DataTable)
        for c in range(self.nch):
            table.update_cell(f"ch{c}", "CH", self._ch_cell(c))
        self._paint_board(time.time())      # the board line carries the count

    def _cell(self, col: Column, row: dict[str, str], flags: list[str]) -> Text:
        raw = row.get(col.par, "")
        if col.par == "STATUS":
            return self._status_text(flags)

        info = self.info.get(col.par, ParamInfo())
        if info.choices:
            return Text(raw, "cyan" if raw.upper() == info.on_state.upper()
                        else "", justify=col.align)
        try:
            text = Text(f"{float(raw):.{info.prec}f}", "", justify=col.align)
        except ValueError:
            return Text(raw, justify=col.align)

        if col.par == "VMON":
            text.style = self._monitor_style(row, flags, "VMON", "VSET",
                                             0.01, 2.0)
        elif col.par == "IMON":
            text.style = "bold red" if "OVC" in flags else ""
        elif col.par == "VSET" and "MAXV" in flags:
            text.style = "bold red"
        return text

    @staticmethod
    def _monitor_style(row: dict[str, str], flags: list[str], mon_par: str,
                       set_par: str, frac: float, floor: float) -> str:
        """Colour a monitored value by how far it sits from its setpoint."""
        if "ON" not in flags:
            return "dim"
        if set(flags) & BUSY_FLAGS:
            return "yellow"         # ramping, so a gap is expected
        try:
            mon, want = float(row[mon_par]), float(row[set_par])
        except (KeyError, ValueError):
            return ""
        if abs(mon - want) > max(abs(want) * frac, floor):
            return "bold red"       # out of regulation while not ramping
        return "green"

    def _status_text(self, flags: list[str]) -> Text:
        if not flags:
            return Text("OFF", "dim")
        out = Text()
        for i, f in enumerate(flags):
            if i:
                out.append(" ")
            if f in ALARM_FLAGS:
                out.append(f, "bold red")
            elif f in BUSY_FLAGS:
                out.append(f, "yellow")
            elif f == "ON":
                out.append(f, "bold green")
            else:
                out.append(f, "dim")
        return out

    def _paint_board(self, stamp: float) -> None:
        if self.active is None:
            return
        b = self.board
        remote = b.get("BDCTR", "").upper() == "REMOTE"
        ilk = b.get("BDILK", "").upper()
        alarm = b.get("BDALARM")        # None when this module has no such thing

        line = Text()
        line.append(b.get("BDNAME", "?"), "bold")
        line.append(f"  sn {b.get('BDSNUM', '?')}  fw {b.get('BDFREL', '?')}",
                    "dim")
        line.append("   ctrl ")
        line.append("REMOTE" if remote else "LOCAL",
                    "green" if remote else "bold yellow")
        line.append("   interlock ")
        line.append(ilk or "?", "bold red" if ilk == "YES" else "green")
        line.append("   alarm ")
        if alarm is None:
            # Not reported is not the same as none: say so rather than green.
            line.append("n/a", "dim")
        else:
            line.append(alarm, "bold red" if alarm not in ("0", "") else "green")
        paused = self.worker.paused if self.worker else False
        line.append(f"   {time.strftime('%H:%M:%S', time.localtime(stamp))}",
                    "dim")
        line.append(f"  {self.interval:g}s", "dim")
        if self.selected:
            line.append(f"  sel {fmt_channels(self.selected)}", "bold cyan")
            line.append(" (u clears)", "dim")
        if paused:
            line.append("  PAUSED", "bold yellow")
        if self.read_only:
            line.append("  READ-ONLY", "bold cyan")
        self.query_one("#board", Static).update(line)

    def log_line(self, markup: str, mod: "Module | None" = None) -> None:
        """One line of the session's record.

        With more than one module open every line says which one it came from:
        the log outlives the tab that was in front at the time, and "CH3 OFF"
        with no supply named is not a record of anything.  Module names come
        from profile names, which are not checked for console markup, so they
        are escaped here.
        """
        stamp = time.strftime("%H:%M:%S")
        where = ""
        if mod is not None and len(self.modules) > 1:
            where = f"[dim]{escape(mod.name)}[/dim] "
        self.query_one("#log", RichLog).write(
            f"[dim]{stamp}[/dim] {where}{markup}")

    # -- helpers -----------------------------------------------------------

    @property
    def cursor_channel(self) -> int:
        return self.query_one("#chans", DataTable).cursor_row

    def ch_label(self, ch: int) -> str:
        """The channel as `CH3`, or `CH3 Endcap A` once it has been named.

        Every mention of a channel outside the table goes through here or
        `ch_phrase`.  The log is the only record a session in front of a live
        supply leaves behind, and it should say which detector was switched
        off -- not which row of a table nobody can see any more.
        """
        name = self.labels.get(ch, "")
        return f"CH{ch} {name}" if name else f"CH{ch}"

    def ch_phrase(self, ch: int) -> str:
        """The same thing in prose: `channel 3 (Endcap A)`."""
        name = self.labels.get(ch, "")
        return f"channel {ch} ({name})" if name else f"channel {ch}"

    def _column_key(self, index: int) -> str | None:
        """The key of the table column at a position, or None if out of range.

        Positions are looked up rather than counted from a constant: the Name
        column comes and goes, and an off-by-one here would edit the wrong
        parameter of a live supply.
        """
        keys = list(self.query_one("#chans", DataTable).columns)
        return keys[index].value if 0 <= index < len(keys) else None

    def _column_at(self, index: int) -> Column | None:
        """The displayed parameter at a position -- None for CH and Name."""
        key = self._column_key(index)
        return next((c for c in self.columns if c.par == key), None)

    def _guard(self) -> bool:
        """True when writes are permitted right now."""
        if self.read_only:
            self.notify("read-only mode", severity="warning")
            return False
        if self.worker is None:
            self.notify("not connected — press m to choose a module",
                        severity="warning")
            return False
        if not self._rows_built:
            self.notify("still connecting", severity="warning")
            return False
        if self.board.get("BDCTR", "REMOTE").upper() != "REMOTE":
            self.notify("module is in LOCAL mode; switch the front panel to "
                        "REMOTE", title="write refused", severity="error",
                        timeout=6)
            return False
        return True

    def submit(self, label: str, fn) -> None:
        mod = self.active
        if mod is None or mod.worker is None:
            return
        self.log_line(f"[dim]→ {label}[/dim]", mod)
        mod.worker.submit(label, fn)

    # -- actions -----------------------------------------------------------

    @on(DataTable.CellSelected)
    def _cell_selected(self, event: DataTable.CellSelected) -> None:
        self.edit_cell(event.coordinate)

    @work
    async def edit_cell(self, coord: Coordinate) -> None:
        if self._column_key(coord.column) == NAME_COL:
            await self._name_channel(coord.row)     # a name is not a parameter
            return
        col = self._column_at(coord.column)
        if col is None:
            return
        if not col.editable:
            self.notify(f"{col.par} is read-only", severity="warning")
            return
        if not self._guard():
            return
        ch = coord.row
        # A selection is what an edit applies to; with none, the cell under the
        # cursor.  The dialog names every channel it is about to write to.
        targets = sorted(self.selected) if self.selected else [ch]
        info = self.info.get(col.par, ParamInfo(access=2))
        # Prefill from a channel that is actually a target, which the cursor
        # need not be once a selection exists.
        ref = ch if ch in targets else targets[0]
        current = self.values[ref].get(col.par, "")
        mixed = len({self.values[c].get(col.par, "").upper()
                     for c in targets}) > 1
        value = await self.push_screen_wait(
            EditScreen(targets, col, current, info, mixed, self.labels))
        if value is None:
            return
        self._write(col, {c: value for c in targets})

    @work
    async def action_adjust(self) -> None:
        """Shift the column under the cursor by a step, on every target.

        The parameter comes from the cursor rather than a dialog: the column
        is already what the cursor picks for an edit, and one selection is
        often adjusted a few times in a row.
        """
        if not self._rows_built:
            return
        table = self.query_one("#chans", DataTable)
        if self._column_key(table.cursor_column) == NAME_COL:
            self.notify("a name is not a number — press enter to set it",
                        severity="warning")
            return
        col = self._column_at(table.cursor_column)
        if col is None:
            return
        info = self.info.get(col.par, ParamInfo(access=2))
        if not col.editable:
            self.notify(f"{col.par} is read-only", severity="warning")
            return
        if info.choices:
            # RAMP/KILL and HIGH/LOW have nothing to add a step to.
            self.notify(f"{col.par} is not a number — press enter to set it",
                        severity="warning")
            return
        if not self._guard():
            return
        targets = (sorted(self.selected) if self.selected
                   else [self.cursor_channel])
        plan = await self.push_screen_wait(AdjustScreen(
            targets, col, info, {c: self.values[c].get(col.par, "")
                                 for c in targets}, self.labels))
        if plan:
            self._write(col, plan)

    def _write(self, col: Column, plan: dict[int, str]) -> None:
        """One SET per channel -- the protocol has no multi-channel write."""
        # Write with the module's own name for the parameter, not ours.
        wire = self.worker.wire_name(col.par) if self.worker else col.par
        for c, value in plan.items():
            self.submit(f"{self.ch_label(c)} {col.par} = {value}",
                        lambda d, c=c, v=value: d.set_ch(c, wire, v))

    # -- naming ------------------------------------------------------------
    # Not behind `_guard`: a channel name is ours, so it can be set in
    # read-only mode and while the module is in LOCAL.

    @work
    async def action_name_channel(self) -> None:
        if self._rows_built:
            await self._name_channel(self.cursor_channel)

    async def _name_channel(self, ch: int) -> None:
        name = await self.push_screen_wait(NameScreen(
            ch, self.labels.get(ch, ""), self._saveable()))
        if name is not None:
            self._set_label(ch, name)

    def _saveable(self) -> str:
        """The profile these names would be saved to, or "" if there is none."""
        if self.profile is None or self.profile.is_sim:
            return ""
        return self.profile.name

    def _set_label(self, ch: int, name: str) -> None:
        if name == self.labels.get(ch, ""):
            return
        had_column = bool(self.named)
        if name:
            self.labels[ch] = name
        else:
            self.labels.pop(ch, None)
        if bool(self.named) != had_column:
            self._build_table()         # the column has just appeared or gone
        elif self.named:
            self.query_one("#chans", DataTable).update_cell(
                f"ch{ch}", NAME_COL, self._name_cell(ch))
            self._fit_names()

        why = self._save_labels()
        what = f"named {name}" if name else "name cleared"
        note = f"  [dim]({escape(why)})[/dim]" if why else ""
        self.log_line(f"[cyan]CH{ch} {what}[/cyan]{note}", self.active)

    def _save_labels(self) -> str:
        """Write the names to the profile.  Returns why not, if not.

        The store's copy is the one written, not the object this panel
        connected with: editing a profile while connected replaces it, and
        saving the detached one would quietly lose the change.
        """
        if self.profile is None:
            return "not saved: this link has no profile"
        if self.profile.is_sim:
            return "not saved: the simulator has no stored profile"
        target = self.store.get(self.profile.name) or self.profile
        target.labels = dict(self.labels)
        self.profile.labels = dict(self.labels)
        try:
            self.store.save()
        except OSError as e:
            return f"not saved: cannot write {self.store.path}: {e}"
        return ""

    # Selection is not a write, so it is not behind `_guard`: it stays usable
    # in read-only mode and while the module is in LOCAL.

    def action_select_toggle(self) -> None:
        if not self._rows_built:
            return
        ch = self.cursor_channel
        self.selected.symmetric_difference_update({ch})
        self._paint_selection()
        self._log_selection()

    def action_select_all(self) -> None:
        if not self._rows_built:
            return
        self.selected = set(range(self.nch))
        self._paint_selection()
        self._log_selection()

    def action_select_none(self) -> None:
        if not self.selected:
            return
        self.selected.clear()
        self._paint_selection()
        self._log_selection()

    def _log_selection(self) -> None:
        """The selection decides what the next write lands on, so it belongs
        in the same record as the writes themselves."""
        if self.selected:
            self.log_line(f"[cyan]selected {fmt_channels(self.selected)}"
                          f"[/cyan]", self.active)
        else:
            self.log_line("[cyan]selection cleared[/cyan]", self.active)

    def action_toggle(self) -> None:
        if not self._rows_built:
            return
        on_now = "ON" in self.flags[self.cursor_channel]
        self.action_power(not on_now)

    @work
    async def action_power(self, turn_on: bool) -> None:
        if not self._guard():
            return
        ch = self.cursor_channel
        vset = self.values[ch].get("VSET", "?")
        if turn_on:
            ok = await self.push_screen_wait(ConfirmScreen(
                f"Energise {self.ch_phrase(ch)}?",
                f"The channel will ramp to VSET = {vset} V.",
                danger=True, default_yes=False))
        else:
            ok = await self.push_screen_wait(ConfirmScreen(
                f"Switch {self.ch_phrase(ch)} off?",
                "The channel ramps down at RDWN (or drops immediately if "
                "PDWN is KILL).",
                danger=False, default_yes=True))
        if not ok:
            return
        label = f"{self.ch_label(ch)} {'ON' if turn_on else 'OFF'}"
        self.submit(label, (lambda d: d.channel_on(ch)) if turn_on
                    else (lambda d: d.channel_off(ch)))

    @work
    async def action_all_off(self) -> None:
        """Every live channel on the module in front of you -- not on the ones
        in the other tabs.  `X` meant one supply before the panel could hold
        several, and widening it silently would de-energise supplies that are
        not on screen.  The prompt names the module for the same reason."""
        if not self._guard():
            return
        mod = self.active
        live = mod.live
        where = escape(mod.name)        # a dialog renders console markup
        if not live:
            self.notify(f"no channels are on {where}")
            return
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Switch off all {len(live)} live channels on {where}?",
            f"{', '.join(self.ch_label(c) for c in live)} will be commanded "
            f"off.  Any other module stays as it is.",
            danger=True, default_yes=True))
        if not ok:
            return
        for ch in live:
            self.submit(f"{self.ch_label(ch)} OFF",
                        lambda d, c=ch: d.channel_off(c))

    @work
    async def action_clear_alarm(self) -> None:
        if not self._guard():
            return
        ok = await self.push_screen_wait(ConfirmScreen(
            "Clear the board alarm?",
            "Latched trips are reset.  A channel whose fault is still present "
            "will simply trip again.",
            danger=False, default_yes=True))
        if ok:
            self.submit("BDCLR", lambda d: d.clear_alarm())

    def action_help(self) -> None:
        """Not logged: reading the help changes nothing about the supply."""
        self.push_screen(HelpScreen(str(self.store.path), self.read_only))

    def action_refresh(self) -> None:
        mod = self.active
        if mod and mod.worker:
            self.log_line("[dim]refresh[/dim]", mod)
            mod.worker.refresh_now()

    def action_pause(self) -> None:
        """Pause the module on screen.  The others keep polling: pausing one
        supply is not a reason to stop watching the rest."""
        mod = self.active
        if not mod or not mod.worker:
            return
        mod.worker.paused = not mod.worker.paused
        state = "paused" if mod.worker.paused else "resumed"
        self.log_line(f"[yellow]polling {state}[/yellow]", mod)
        self._paint_board(time.time())
        self._paint_tabs()

    def action_slower(self) -> None:
        self._set_interval(min(30.0, self.interval * 2))

    def action_faster(self) -> None:
        self._set_interval(max(0.25, self.interval / 2))

    def _set_interval(self, value: float) -> None:
        # `+` at 30s and `-` at 0.25s are no-ops; saying so beats a log line
        # that claims a change nobody made.
        mod = self.active
        if mod is None or value == mod.interval:
            return
        mod.interval = value
        self.log_line(f"[yellow]poll every {value:g}s[/yellow]", mod)
        if mod.worker:
            mod.worker.refresh_now()
        self._paint_board(time.time())


def run_tui(dev: Device | None = None, interval: float = 1.0,
            read_only: bool = False, store: ProfileStore | None = None,
            timeout: float = 3.0, verbose: bool = False) -> int:
    """Run the panel.  With dev=None it opens on the module picker."""
    HVApp(dev, interval=interval, read_only=read_only, store=store,
          timeout=timeout, verbose=verbose).run()
    return 0
