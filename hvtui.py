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

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             RichLog, Static)

from hv import (ALARM_FLAGS, BUSY_FLAGS, PAR_ALIASES, Device, HVError,
                ParamInfo, decode_status, device_for_profile)
from hvprofiles import (DEFAULT_BAUD, DEFAULT_PORT, SIM_PROFILE, Profile,
                        ProfileStore, clean_baud, clean_device, clean_host,
                        clean_name, clean_port, looks_like_device)


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

# Column 0 of the table is the channel number, which is not a parameter.
CH_COL = 1

BOARD_STATIC = ["BDNAME", "BDSNUM", "BDFREL", "BDHVMAX", "BDHIMAX", "BDILKM"]
BOARD_FAST = ["BDILK", "BDALARM"]       # safety-relevant, every poll
BOARD_SLOW = ["BDCTR"]                  # LOCAL/REMOTE, changes at the panel

SLOW_EVERY = 6      # poll the `fast=False` parameters once every N cycles


# --------------------------------------------------------------------------
# Device thread
# --------------------------------------------------------------------------

@dataclass
class Sample:
    channels: list[dict[str, str]] = field(default_factory=list)
    board: dict[str, str] = field(default_factory=dict)
    stamp: float = 0.0
    error: str | None = None


class DeviceWorker(threading.Thread):
    """Owns the Device.  Nothing else may touch it.

    Queued commands always run before the next poll and force an immediate
    re-read afterwards, so an edit shows its effect without waiting out the
    poll interval.
    """

    def __init__(self, dev: Device, app: "HVApp", interval: float):
        super().__init__(daemon=True, name="hv-io")
        self.dev = dev
        self.app = app
        self.interval = interval
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
                self._due = time.monotonic() + self.interval
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
        already in flight when the user switched modules -- is dropped rather
        than painted over the new module's channels."""
        if self._stop.is_set() or self.app.worker is not self:
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
        self._to_app(self.app.on_device_ready, nch, list(self.columns), infos,
                     dict(self._board), missing)

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
                self._to_app(self.app.on_command_result, label, str(e))
            except OSError as e:
                self._to_app(self.app.on_command_result, label,
                             f"link error: {e}")
            else:
                self._to_app(self.app.on_command_result, label, None)
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
        self._to_app(self.app.on_params_dropped, sorted(gone))

    def _emit(self, sample: Sample) -> None:
        self._to_app(self.app.on_sample, sample)


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
    """Edit one parameter of one channel.

    Numeric parameters get a text field validated against the module's own
    INFO descriptor; two-valued ones (PDWN, IMRANGE) get a button per state,
    which avoids typing a string the module will only reject.
    """

    # left/right only ever reach the screen when a Button has focus -- a
    # focused Input binds them to cursor movement and consumes them first.
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("left", "focus_previous", "", show=False),
        Binding("right", "focus_next", "", show=False),
    ]

    def __init__(self, ch: int, col: Column, current: str, info: ParamInfo):
        super().__init__()
        self.ch = ch
        self.col = col
        self.current = current
        self.info = info
        self.choices = info.choices

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"CH {self.ch}  •  {self.col.par}", id="dlg-title")
            hint = self.info.range_text() or "no descriptor from module"
            yield Label(f"current {self.current or '--'}    range {hint}",
                        id="dlg-detail")
            if self.choices:
                with Horizontal(id="dlg-buttons"):
                    for i, choice in enumerate(self.choices):
                        yield Button(choice, id=f"choice-{i}",
                                     variant="primary"
                                     if choice.upper() == self.current.upper()
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
        try:
            if looks_like_device(text["f-addr"]):
                device = clean_device(text["f-addr"])
                baud = clean_baud(text["f-port"])
                name = clean_name(text["f-name"], taken)
                profile = Profile(name=name, kind="serial",
                                  device=device, baud=baud)
            else:
                host, embedded = clean_host(text["f-addr"])
                port = (embedded if embedded is not None
                        else clean_port(text["f-port"]))
                name = clean_name(text["f-name"], taken)
                profile = Profile(name=name, host=host, port=port)
        except ValueError as e:
            self.query_one("#dlg-error", Label).update(Text(str(e), "bold red"))
            return
        self.dismiss(profile)


class ConnectScreen(ModalScreen["Profile | None"]):
    """Pick the module to talk to, or edit the list of them.

    Dismissing with a profile means "connect to this"; with None it means
    "carry on with whatever link I already have", which for a session that
    has none is the same as quitting.
    """

    BINDINGS = [
        # No priority: a focused DataTable turns enter into RowSelected and a
        # focused Button presses itself, which is what you want in both cases.
        Binding("enter", "connect", "Connect"),
        Binding("n", "new", "New"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("escape,q", "back", "Back", show=False),
    ]

    def __init__(self, store: ProfileStore, current: str = "",
                 can_cancel: bool = False):
        super().__init__()
        self.store = store
        self.current = current
        self.can_cancel = can_cancel
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
            note = "last used" if p.name == self.store.last_used else ""
            table.add_row(Text(p.name, "bold" if not p.is_sim else "dim"),
                          Text(p.address, "" if not p.is_sim else "dim"),
                          Text(note, "dim"))
        for i, p in enumerate(self.rows):
            if select and p.name.casefold() == select.casefold():
                table.move_cursor(row=i)
                break
        detail = (f"{len(self.store.profiles)} saved  •  "
                  f"enter connects, n new, e edit, d delete")
        if not self.store.profiles:
            detail = "No modules saved yet — press n to add one."
        self.query_one("#dlg-detail", Label).update(detail)
        if self.store.load_error:
            self._error(self.store.load_error)

    @property
    def selected(self) -> Profile | None:
        i = self.query_one("#profiles", DataTable).cursor_row
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _error(self, message: str) -> None:
        self.query_one("#dlg-error", Label).update(Text(message, "bold red"))

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

    @work
    async def action_new(self) -> None:
        prof = await self.app.push_screen_wait(ProfileEditScreen(self.store))
        if prof is None:
            return
        self.store.upsert(prof)
        if self._persist():
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
            self._reload()


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class HVApp(App):
    TITLE = "hvctl"

    CSS = """
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
    ConfirmScreen, EditScreen, ConnectScreen, ProfileEditScreen {
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
    #dlg-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #dlg-buttons Button { margin-left: 2; }
    .field { height: auto; margin-bottom: 1; }
    .flabel { width: 8; padding-top: 1; color: $text-muted; }
    .field Input { width: 1fr; }
    #profiles { height: auto; max-height: 12; border: solid $primary-darken-2; }
    /* Five buttons have to fit an 80-column terminal. */
    ConnectScreen #dlg-buttons Button { min-width: 10; margin-left: 1; }
    """

    BINDINGS = [
        Binding("space", "toggle", "On/Off"),
        Binding("o", "power(True)", "On"),
        Binding("f", "power(False)", "Off"),
        Binding("X", "all_off", "ALL OFF"),
        Binding("c", "clear_alarm", "Clear alarm"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause"),
        Binding("m", "switch_module", "Module…"),
        Binding("plus,equals_sign", "slower", "Slower", show=False),
        Binding("minus", "faster", "Faster", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, dev: Device | None = None, interval: float = 1.0,
                 read_only: bool = False, store: ProfileStore | None = None,
                 timeout: float = 3.0, verbose: bool = False):
        super().__init__()
        self.dev = dev
        self.interval = interval
        self.read_only = read_only
        self.store = store if store is not None else ProfileStore()
        self.timeout = timeout
        self.verbose = verbose
        self.profile: Profile | None = None
        self.nch = 0
        # Which columns this module can actually supply; known only once the
        # worker has asked it, so the table is built then rather than at mount.
        self.columns: list[Column] = []
        self.info: dict[str, ParamInfo] = {}
        self.board: dict[str, str] = {}
        self.flags: list[list[str]] = []
        self.values: list[dict[str, str]] = []
        self.worker: DeviceWorker | None = None
        self._rows_built = False
        self._last_error: str | None = None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("connecting…", id="board")
        yield DataTable(id="chans", cursor_type="cell", zebra_stripes=True)
        yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        if self.read_only:
            self.log_line("[yellow]read-only mode: SET is disabled[/yellow]")
        if self.dev is not None:
            self._attach(self.dev, None)    # a link was named on the command line
        else:
            self.query_one("#board", Static).update(
                Text("not connected", "dim"))
            self.choose_module()

    def on_unmount(self) -> None:
        self._detach()

    # -- connection --------------------------------------------------------

    def _attach(self, dev: Device, profile: Profile | None) -> None:
        self.dev = dev
        self.profile = profile
        self.sub_title = (f"{profile.name} — {dev.t.describe()}" if profile
                          else dev.t.describe())
        self.log_line(f"[dim]link {dev.t.describe()}[/dim]")
        self.worker = DeviceWorker(dev, self, self.interval)
        self.worker.start()

    def _detach(self) -> None:
        """Drop the current link and empty the display of its readings.

        The I/O thread is not joined: it may be blocked in `call_from_thread`
        waiting on this very event loop, and a late result it delivers is
        discarded by DeviceWorker._dispatch anyway.
        """
        if self.worker:
            self.worker.stop()
            self.worker = None
        if self.dev:
            self.dev.t.close()
            self.dev = None
        self.profile = None
        self.nch = 0
        self.columns = []
        self.info, self.board = {}, {}
        self.flags, self.values = [], []
        self._rows_built = False
        self._last_error = None
        if self.is_running:
            # Columns go too: the next module may not have the same ones.
            self.query_one("#chans", DataTable).clear(columns=True)
            self.query_one("#board", Static).update(
                Text("not connected — press m to choose a module", "dim"))

    @work
    async def choose_module(self) -> None:
        """Ask for a profile, connect, and keep asking until one works."""
        while True:
            prof = await self.push_screen_wait(ConnectScreen(
                self.store, current=self.profile.name if self.profile else "",
                can_cancel=self.dev is not None))
            if prof is None:
                if self.dev is None:
                    self.exit()
                return
            if await self._connect(prof):
                return

    async def _connect(self, prof: Profile) -> bool:
        self.log_line(f"[dim]connecting to {prof.name} ({prof.address})…[/dim]")
        try:
            # Blocking connect, so off the UI thread it goes.
            dev = await asyncio.to_thread(device_for_profile, prof,
                                          self.timeout, self.verbose)
        except (OSError, HVError) as e:
            self.log_line(f"[red]{prof.name}: {e}[/red]")
            self.notify(str(e), title=f"cannot reach {prof.name}",
                        severity="error", timeout=8)
            return False
        self._detach()
        self._attach(dev, prof)
        self.log_line(f"[green]connected to {prof.name}[/green]")
        if not prof.is_sim:
            self.store.remember(prof.name)
        return True

    @work
    async def action_switch_module(self) -> None:
        live = [c for c in range(self.nch) if "ON" in self.flags[c]]
        if live:
            ok = await self.push_screen_wait(ConfirmScreen(
                "Disconnect with channels live?",
                f"Channels {', '.join(map(str, live))} stay energised — the "
                "module keeps its output when nothing is watching it.",
                danger=True, default_yes=False))
            if not ok:
                return
        self.choose_module()

    # -- callbacks from the I/O thread -------------------------------------

    def on_device_ready(self, nch: int, columns: list[Column],
                        infos: dict[str, ParamInfo], board: dict[str, str],
                        missing: list[str]) -> None:
        self.nch = nch
        self.columns = columns
        self.info = infos
        self.board.update(board)
        self.flags = [[] for _ in range(nch)]
        self.values = [{} for _ in range(nch)]
        table = self.query_one("#chans", DataTable)
        table.clear(columns=True)
        table.add_column(Text("CH", justify="right"), width=3, key="CH")
        for col in columns:
            table.add_column(Text(col.title, justify=col.align),
                             width=col.width, key=col.par)
        for c in range(nch):
            table.add_row(Text(str(c), justify="right"),
                          *["" for _ in columns], key=f"ch{c}")
        self._rows_built = True
        name = board.get("BDNAME", "?")
        self.log_line(f"[green]{name}[/green]  sn {board.get('BDSNUM', '?')}"
                      f"  fw {board.get('BDFREL', '?')}  {nch} channels")
        if missing:
            self.log_line(f"[dim]not offered by this module: "
                          f"{', '.join(missing)}[/dim]")
        if board.get("BDCTR", "").upper() != "REMOTE":
            self.log_line("[bold yellow]module is in LOCAL mode — it will "
                          "refuse every write until the front panel is set to "
                          "REMOTE[/bold yellow]")

    def on_sample(self, sample: Sample) -> None:
        if sample.error:
            self.query_one("#board", Static).update(
                Text(f"link error: {sample.error}", "bold white on red"))
            # A dead link repeats the same error every poll; log the change,
            # not the repetition.
            if sample.error != self._last_error:
                self.log_line(f"[red]{sample.error}[/red]")
                self._last_error = sample.error
            return
        if self._last_error:
            self.log_line("[green]link recovered[/green]")
            self._last_error = None
        self.board.update(sample.board)
        if self._rows_built:
            self._paint(sample)
        self._paint_board(sample.stamp)

    def on_params_dropped(self, names: list[str]) -> None:
        """The module rejected a parameter it had advertised (or never listed).

        It will not be asked for again, so this is said once rather than once
        a second, and the column goes with it -- a header over permanently
        blank cells reads as a broken link, which it is not.
        """
        self.columns = [c for c in self.columns if c.par not in set(names)]
        table = self.query_one("#chans", DataTable)
        for name in names:
            self.board.pop(name, None)      # a stale value would read as live
            try:
                table.remove_column(name)
            except Exception:
                pass        # a board parameter, which has no column
        self.log_line(f"[yellow]{', '.join(names)}: unknown to this module — "
                      f"no longer polled[/yellow]")

    def on_command_result(self, label: str, error: str | None) -> None:
        if error:
            self.log_line(f"[red]✗ {label}: {error}[/red]")
            self.notify(error, title=label, severity="error", timeout=6)
        else:
            self.log_line(f"[green]✓[/green] {label}")

    # -- rendering ---------------------------------------------------------

    def _paint(self, sample: Sample) -> None:
        table = self.query_one("#chans", DataTable)
        for c, row in enumerate(sample.channels):
            if c >= self.nch:
                break
            _, flags = decode_status(row.get("STATUS", "0"))
            self.flags[c] = flags
            self.values[c] = row
            for col in self.columns:
                table.update_cell(f"ch{c}", col.par,
                                  self._cell(col, row, flags))

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
        if paused:
            line.append("  PAUSED", "bold yellow")
        if self.read_only:
            line.append("  READ-ONLY", "bold cyan")
        self.query_one("#board", Static).update(line)

    def log_line(self, markup: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{stamp}[/dim] {markup}")

    # -- helpers -----------------------------------------------------------

    @property
    def cursor_channel(self) -> int:
        return self.query_one("#chans", DataTable).cursor_row

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
        if self.worker is None:
            return
        self.log_line(f"[dim]→ {label}[/dim]")
        self.worker.submit(label, fn)

    # -- actions -----------------------------------------------------------

    @on(DataTable.CellSelected)
    def _cell_selected(self, event: DataTable.CellSelected) -> None:
        self.edit_cell(event.coordinate)

    @work
    async def edit_cell(self, coord: Coordinate) -> None:
        if coord.column < CH_COL or coord.column - CH_COL >= len(self.columns):
            return
        col = self.columns[coord.column - CH_COL]
        if not col.editable:
            self.notify(f"{col.par} is read-only", severity="warning")
            return
        if not self._guard():
            return
        ch = coord.row
        info = self.info.get(col.par, ParamInfo(access=2))
        current = self.values[ch].get(col.par, "")
        value = await self.push_screen_wait(EditScreen(ch, col, current, info))
        if value is None:
            return
        # Write with the module's own name for the parameter, not ours.
        wire = self.worker.wire_name(col.par) if self.worker else col.par
        label = f"CH{ch} {col.par} = {value}"
        self.submit(label, lambda d: d.set_ch(ch, wire, value))

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
                f"Energise channel {ch}?",
                f"The channel will ramp to VSET = {vset} V.",
                danger=True, default_yes=False))
        else:
            ok = await self.push_screen_wait(ConfirmScreen(
                f"Switch channel {ch} off?",
                "The channel ramps down at RDWN (or drops immediately if "
                "PDWN is KILL).",
                danger=False, default_yes=True))
        if not ok:
            return
        label = f"CH{ch} {'ON' if turn_on else 'OFF'}"
        self.submit(label, (lambda d: d.channel_on(ch)) if turn_on
                    else (lambda d: d.channel_off(ch)))

    @work
    async def action_all_off(self) -> None:
        if not self._guard():
            return
        live = [c for c in range(self.nch) if "ON" in self.flags[c]]
        if not live:
            self.notify("no channels are on")
            return
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Switch off all {len(live)} live channels?",
            f"Channels {', '.join(map(str, live))} will be commanded off.",
            danger=True, default_yes=True))
        if not ok:
            return
        for ch in live:
            self.submit(f"CH{ch} OFF", lambda d, c=ch: d.channel_off(c))

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

    def action_refresh(self) -> None:
        if self.worker:
            self.worker.refresh_now()

    def action_pause(self) -> None:
        if not self.worker:
            return
        self.worker.paused = not self.worker.paused
        state = "paused" if self.worker.paused else "resumed"
        self.log_line(f"[yellow]polling {state}[/yellow]")
        self._paint_board(time.time())

    def action_slower(self) -> None:
        self._set_interval(min(30.0, self.interval * 2))

    def action_faster(self) -> None:
        self._set_interval(max(0.25, self.interval / 2))

    def _set_interval(self, value: float) -> None:
        self.interval = value
        if self.worker:
            self.worker.interval = value
            self.worker.refresh_now()
        self._paint_board(time.time())


def run_tui(dev: Device | None = None, interval: float = 1.0,
            read_only: bool = False, store: ProfileStore | None = None,
            timeout: float = 3.0, verbose: bool = False) -> int:
    """Run the panel.  With dev=None it opens on the module picker."""
    HVApp(dev, interval=interval, read_only=read_only, store=store,
          timeout=timeout, verbose=verbose).run()
    return 0
