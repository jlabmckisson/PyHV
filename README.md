# hvctl

A control panel and command line for CAEN 803x ("SMART HV") power supplies --
DT8031/2/3/4, N803x and R803x -- over Ethernet or USB.

> These are real high voltage supplies: up to 4 kV and a few mA into detector
> strings.  `set`, `on`, `off` and the panel's editing keys change the state of
> the supply and can energise channels.  Everything that does so asks first, and
> `hvctl tui --read-only` refuses all of it.  See [Safety](#safety).

```
DT8033 8 CH 4KV/3mA  sn 24-0417  fw 1.09   ctrl REMOTE   interlock NO   alarm 0 …
────────────────────────────────────────────────────────────────────────────────
  CH  Name                VSet        VMon        ISet        IMon   RampUp   Ra…
   0  Endcap A top         0.0         0.0       500.0         0.0       50    …
  *1  Endcap A bot      1500.0      1499.9       500.0        43.1       50    …
  *2  Barrel 1          1500.0       881.5       500.0        22.4       50    …
   3                    2400.0      2399.9       500.0        52.3       50    …
```

`*` marks the selected channels; the rest of the columns, ending in Status,
continue to the right.

## Getting it

Download a binary from the [releases
page](https://github.com/jlabmckisson/PyHV/releases) -- `hvctl` for Linux
x86\_64, macOS arm64 and Intel, and `hvctl.exe` for Windows.  They are built by
[GitHub Actions](.github/workflows/build.yml) with PyInstaller and need no
Python on the machine, which is the point: a box in front of a supply should not
need a virtualenv.

From source:

    git clone git@github.com:jlabmckisson/PyHV.git
    cd PyHV
    pip install -r requirements.txt      # textual, and pyserial for --serial
    python3 hv.py

The read-only subcommands need nothing but the standard library; `textual` is
for the panel and `pyserial` only for `--serial`.  Built on Python 3.12, and
run on 3.13.

Below, `hvctl` means either the binary or `python3 hv.py`.

## First run

    hvctl

opens the panel with no module connected and asks which one to talk to.  Add
one with `n`, give it a name and an address, and it is saved -- an address only
ever has to be typed once.  Nothing auto-connects: the module used last is
preselected, and you still press Enter.

To try it without any hardware:

    hvctl --sim tui

The simulator is a fake DT8033 with eight channels that ramp, trip and refuse
things the way firmware does.  It cannot be edited or deleted, and connecting to
it does not become the default module for the other subcommands.

The Address field takes either kind: `192.168.0.250` (or `192.168.0.250:1470`,
which fills the port in for you) for Ethernet, `/dev/ttyACM0` or `COM3` for USB.
Anything starting with `/`, or shaped like `COM3`, is treated as a serial port,
and the field beside it relabels itself Port or Baud as you type.  No hostname
starts with a slash, so there is no mode to get wrong.

## The panel

Press `?` for the keys, the STATUS legend and where things are kept, without
leaving the panel.  The same list:

| Key | What it does |
|---|---|
| `↑ ↓ ← →` | move the cursor.  The column it is in is what `enter` and `d` act on |
| `enter` | edit the cell under the cursor |
| `s` | select the channel under the cursor, or drop it |
| `a` / `u`, `esc` | select every channel / none |
| `d` | adjust every target by a step |
| `n` | name the channel |
| `space` | switch the channel under the cursor on or off |
| `o` / `f` | on / off |
| `X` | ALL OFF -- every channel on the module |
| `c` | clear the board alarm and latched trips |
| `r` | read everything now |
| `p` | pause polling on this module, or resume it |
| `+` / `-` | poll slower / faster (0.25 s to 30 s) |
| `m` | connect another module -- it opens in a new tab |
| `]` / `[` | next tab / previous tab |
| `1`…`9` | go straight to that tab |
| `w` | close this tab |
| `?`, `F1` | this list, in the panel |
| `q` | quit -- channels are left exactly as they are |

The board line along the top carries the module's name, serial and firmware, its
REMOTE/LOCAL state, the interlock, the alarm, the time of the last reading and
the poll period.  The log along the bottom is the record the session leaves
behind: every write, what it was aimed at, the poll rate, pauses, refreshes, the
link itself and edits to the profile store.

### Several modules at once

`m` connects a module without disturbing the ones already connected; each gets a
tab on the line under the title bar.  `]` and `[` walk them, `1`…`9` go straight
to one, and clicking a tab works too.

    1 Endcap rack ●   2 Barrel rack ○   3 Test bench !

Every tab keeps polling, whether or not you are looking at it, so the marker
beside each name stays true: `●` something is live, `○` every channel is off,
`!` something worth looking at -- an alarm, a tripped channel, an asserted
interlock or a link that has stopped answering -- `‖` its polling is paused, `…`
nothing read back yet.  A tab is a glance, not a diagnosis; press the key and
look.

Everything else in the panel acts on the tab in front of you and no other.
`X` switches off every live channel on **this** module and names it in the
prompt; the other supplies are not touched.  The selection, the cursor, the poll
rate and the channel names all belong to their own module, so coming back to a
tab finds it as you left it.

The log is shared, and once more than one module is connected every line says
which one it came from -- the log outlives whichever tab was in front at the
time.

`w` closes a tab.  It is the only thing in the panel that disconnects anything,
and it asks first if any channel is live: the module keeps its output after the
panel stops watching it.  Connecting to a module that is already open goes to
its tab instead of dialling it twice, which would double that module's traffic
and give two tabs that disagree.

### Selecting channels

`s` marks the channel under the cursor with a `*`, `a` marks all of them.  An
edit then writes to every marked channel -- one `SET` each, since the protocol
has no multi-channel write -- and the dialog names every channel it is about to
write to.  With nothing selected, an edit goes to the channel under the cursor,
which is how the panel behaves before anything is selected.

On, off and ALL OFF deliberately ignore the selection: `X` already switches
everything off on the module in front of you, and "off, but only these three" is
not worth the ambiguity in front of a live supply.

### Adjusting by a step

`d` takes one step -- `+50`, `-100` -- and shifts every target from wherever it
already sits, which a single `SET` cannot express.  The dialog lists each
`current -> new` pair, because with one step and several starting points nobody
should have to do that arithmetic in front of a live supply.  A step that would
put any target outside the module's own range refuses the whole apply rather
than writing to the channels it happens to suit.

### Naming channels

`n`, or Enter on the Name cell, names a channel `Endcap A top`.  No 803x has a
channel-name parameter, so the name is ours: it is kept with the connection
profile, never sent to the module, and can be set in read-only mode and while
the module is in LOCAL.  Names appear in the table, in the dialogs and in the
log, so the record says which detector was switched off rather than which row of
a table nobody can see any more.

A module reached by `--host` or `--sim` has no profile, so names given to it last
only for the session; the panel says so in the log.

### What the table shows

The columns are resolved per module: a module that has no `MAXV` gets no MaxV
column.  A parameter the module denies having is dropped, and the log says which.

| Column | |
|---|---|
| VMon, IMon | green in regulation, yellow while ramping, red when a channel that is not ramping sits off its setpoint, dim when the output is off |
| VSet, ISet | what the channel is asked for.  Editable |
| Status | the STATUS word, flag by flag |

Status flags, red for alarms, yellow while ramping:

| | | | |
|---|---|---|---|
| `ON` output is on | `RUP` ramping up | `RDW` ramping down | `OVC` over current |
| `OVV` over voltage | `UNV` under voltage | `TRIP` tripped off | `OVP` over power |
| `TWN` temperature warning | `OVT` over temperature | `KILL` KILL input | `INTLK` interlock |
| `ISDIS` output disabled | `FAIL` internal failure | `LOCK` front panel locked | `MAXV` VSet above the limit |

The module is the authority on what tripped it; a flag is worth looking up in
the 803x manual before it is cleared.

## The command line

Every subcommand takes the same connection flags: `--profile NAME`, `--host
IP [--port]`, `--serial DEV [--baud]`, or `--sim`.  With none of them, hvctl
falls back to the profile used last and says so on stderr.

| | |
|---|---|
| `info` | board identity, interlock, alarm |
| `status` | one table of every channel |
| `monitor -n 2` | the same table, repeating |
| `params` | what this module says it has, with ranges |
| `get [CH] PAR` | read one parameter; omit CH for a board one |
| `set CH PAR VAL` | write one **(changes state)** |
| `on CH` / `off CH` | energise / de-energise **(changes state)**, `-y` skips the prompt |
| `clear` | clear the board alarm and latched trips |
| `tui` | the panel (what you get with no subcommand) |
| `profiles` | list saved modules |
| `name [CH] [NAME...]` | name a channel, or list the names |
| `raw` | send literal protocol commands |

```
hvctl --profile DUT status
hvctl --profile DUT params                     # before assuming a parameter exists
hvctl --profile DUT name 3 Endcap A top
hvctl --profile DUT set 3 VSET 1500
hvctl --profile DUT on 3
hvctl --profile DUT --json status | jq '.channels[].vmon'
hvctl -v --profile DUT get 3 VMON              # -v echoes the wire traffic
hvctl --sim raw '$CMD:MON,PAR:BDNCH'
```

`--json` and `-v` are global flags: they go before the subcommand, not after it.
`profiles` and `name` are about the saved list rather than a module, so they open
no connection at all.

Exit status is 0 for success, 1 when the link failed or the module refused
(`CH:ERR`, `PAR:ERR`, LOCAL mode), and 2 when hvctl could not make sense of the
request in the first place -- an unknown profile, a value outside the range the
module describes.

## Profiles

`~/.config/hvctl/profiles.json`, or wherever `$HVCTL_PROFILES` points.  Managed
from the panel (`n`, `e`, `d` on the module picker) and readable by hand:

```json
{
  "last_used": "DUT",
  "profiles": [
    {"name": "DUT", "host": "192.168.0.250", "port": 1470,
     "labels": {"0": "Endcap A", "1": "Endcap B"}}
  ]
}
```

A profile is `tcp` (host + port), `serial` (device + baud) or `sim`, and only
the fields that apply are written.  The file is written atomically, and one that
will not parse is never silently overwritten -- it is moved to
`profiles.json.bad` first and the panel says so.

## Safety

- Every energise, de-energise, ALL OFF and trip-clear asks first.
- ALL OFF reaches the module in front of you and names it in the prompt.  A
  supply in another tab is never switched by a key aimed at this one.
- Closing a tab while any channel on it is live warns that the channels stay
  energised after the link is dropped.  Switching tabs disconnects nothing.
- Quitting the panel switches nothing off, on any module.  Neither does closing
  the terminal.
- Nothing auto-connects.
- `hvctl tui --read-only` disables `SET` entirely -- worth using for a panel
  that is only being watched.
- The module must be in REMOTE for any write.  In LOCAL the front panel has it
  and every `SET` comes back refused; the panel says which.
- Range checks in the dialogs are a courtesy.  The module enforces its own
  limits and answers `VAL:ERR`; that is the authority.

## When something is wrong

**"module is in LOCAL mode"** -- the front panel has control.  Switch it to
REMOTE.  Reads work either way.

**A column is missing from the table, or `params` does not list something.**
Firmware differs: names, spelling and presence all vary between modules, and a
module may simply not have `MAXV` or `BDALARM`.  `hvctl --profile X params` is
the authority on what a given module has.  `PARLIST` is not always complete --
one real module answers `BDNCH` perfectly well while leaving it off the list --
so hvctl probes rather than trusting the list to prove absence.  See
[docs/protocol.md](docs/protocol.md).

**Nothing happens when a channel is switched on.**  Check the interlock on the
board line, and `Pw`/`ON` support -- channels on some modules switch with `Pw`,
not `ON`.  hvctl tries the advertised form first and the other on `PAR:ERR`.

**"the TUI needs textual"** -- `pip install -r requirements.txt`.  The
subcommands still work without it.

**Serial: permission denied on `/dev/ttyACM0`** -- add yourself to `dialout`
(Linux) and log in again.

**A profile disappeared** -- look for `profiles.json.bad` next to the store.
hvctl moves a file it cannot parse aside rather than overwriting it.

**A reading looks wrong.**  `-v` echoes every line to and from the module, which
settles what was asked and what came back.

## Development

    python3 tests/run.py                     # the whole suite, ~25s
    python3 tests/run.py tests.test_tui.NamingChannels
    python3 -m unittest discover -s tests -t .    # the same tests, ~2min

Standard library only -- `unittest`, no pytest -- so the tests run wherever
hvctl does.  `tests/run.py` is a parallel runner, not a framework; the panel
tests are almost pure waiting, so a pool of processes turns two minutes into
half a one.  `-t .` matters: it puts the repository root on `sys.path`.  Nothing
in the suite touches hardware.  More in [tests/README.md](tests/README.md).

| File | |
|---|---|
| [hv.py](hv.py) | protocol, transports, `Device`, the CLI, and the simulator |
| [hvtui.py](hvtui.py) | the panel, its dialogs and the I/O thread |
| [hvprofiles.py](hvprofiles.py) | saved modules and channel names |
| [docs/protocol.md](docs/protocol.md) | the wire protocol, and what varies between modules |
| [CLAUDE.md](CLAUDE.md) | the same ground for anyone (or anything) changing the code |
