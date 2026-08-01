# The 803x protocol, and what varies between modules

What hvctl sends and what comes back, plus the firmware differences it works
around.  The 803x User Manual ("Communication Protocol") is the reference; this
is what turned out to matter in practice.

Everything here can be watched live: `hvctl -v` echoes each line to and from the
module on stderr, and `hvctl raw '$CMD:...'` sends one by hand.

## Framing

    $CMD:<MON|SET|INFO>[,CH:<n>],PAR:<name>[,VAL:<v>]<CR><LF>
    #<CMD|LOC|VAL|CH|PAR>:<OK|ERR>[,VAL:<value>]<CR><LF>

ASCII, one command per line, one reply per command.  There is no unsolicited
traffic and no way to pipeline: a reply belongs to the last command sent, which
is why hvctl gives the device to a single thread and lets nothing else touch it.

| Verb | |
|---|---|
| `MON` | read a value |
| `SET` | write one, or invoke something (`BDCLR`, `ON`, `OFF`) |
| `INFO` | read a parameter's descriptor -- type, access, range, unit |

`CH:<n>` selects a channel; without it the parameter is a board one.

## Transports

**Ethernet.** TCP on port 1470.  One connection at a time on most firmware.

**USB.** A virtual COM port -- `/dev/ttyACM0`, `COM3` -- at 9600 8N1.  9600 baud
is the constraint behind several decisions here: a full eleven-column read of
sixteen channels one parameter at a time does not fit in a one-second poll, so
group reads matter (below), and hvctl re-reads the slow-moving columns once
every sixth cycle rather than every cycle.

## Replies

`#CMD:OK` for a write that landed.  `#CMD:OK,VAL:1499.88` for a read.  An error
comes back under a header that says what was wrong, and `HVError.header` carries
it:

| Header | Meaning | What hvctl does |
|---|---|---|
| `PAR` | no such parameter | permanent: stops asking, drops the column, works around it |
| `CH` | invalid channel number | reports it |
| `LOC` | module is in LOCAL; every `SET` is refused | tells the operator to switch the front panel to REMOTE |
| `VAL` | value out of range | reports it -- this is the module's own limit, and the authority |
| `CMD` | malformed or unsupported command | reports it |

The distinction that matters is `PAR` against the rest.  `PAR:ERR` is a
statement about the firmware and will not change on a retry, so hvctl remembers
it: the poll loop runs once a second forever, and a name nobody stops asking for
produces an error every few seconds for the life of the session.  Every other
error is about this attempt, and nothing is disabled because of one.

## Reads

    $CMD:MON,PAR:BDNCH              -> 8
    $CMD:MON,CH:0,PAR:VMON          -> 0.0

`CH:<nch>` -- one past the last channel -- is a **group read**, and answers with
every channel semicolon-separated:

    $CMD:MON,CH:8,PAR:VMON          -> 0.0;0.0;0.0;0.0;0.0;0.0;0.0;0.0

`Device.read_all` uses it when the module supports it and falls back to one
command per channel when it does not.  Only a non-`PAR` error may switch group
reads off: a bad parameter name says nothing about whether the module can do
them, and giving them up costs an eightfold slowdown over serial.

## Descriptors

    $CMD:INFO,CH:0,PAR:VSET         -> 0;2;0;4000;1;0.1;0;V;;
    $CMD:INFO,CH:0,PAR:PDWN         -> 2;2;;;;;;;RAMP;KILL

Ten semicolon-separated fields:

    type;access;min;max;decimals;resolution;exponent;unit;on-state;off-state

`access` is 0 read-only, 1 write-only, 2 read/write -- it is what makes a column
editable.  A parameter with both state fields filled in is a two-valued enum
rather than a number, which is why the panel offers `PDWN` and `IMRANGE` as
buttons instead of a text field the module would only reject.

Not all firmware describes every parameter it accepts.  A missing descriptor
makes a field no less editable, so `try_info_ch` degrades to an empty one.

## What a module has

    $CMD:MON,PAR:PARLIST            -> BDNAME;BDNCH;BDFREL;...
    $CMD:MON,CH:0,PAR:PARCHLIST     -> VSET;VMON;ISET;IMON;MAXV;RUP;RDW;...

**Neither list is reliably complete.**  One real module answers `BDNCH`
perfectly well while leaving it off `PARLIST`.  So `Device.advertised()` probes
with a name known to work -- `BDNCH` for board, `VMON` for channel -- and
records whether the list can be trusted to prove absence; `lists_everything()`
exposes that.  A list that fails the probe is used to choose between spellings
and for nothing else.

`hvctl --profile X params` is this, printed:

```
Channel parameters (via CH:0)
  VSET       rw  0 .. 4000 V
  VMON       ro  0 .. 4000 V
  MAXV       rw  0 .. 4000 V
  PDWN       rw  RAMP | KILL
  STATUS     ro  0 .. 65535
```

Run it against a module before assuming anything about it.

## Parameter names differ between modules

This is the thing to know.  **Never hardcode a parameter name and assume it
exists.**  Firmware varies three ways at once:

1. **Case.**  Names come back as `VMon`, `RDwn`, `BdIlk`, `IMRange`.  Match
   case-insensitively, but send back the module's own spelling.
2. **Spelling.**  Ramp-down is `RDW` in the manual and in the STATUS bit, and
   `RDwn` on real hardware.  `PAR_ALIASES` in [hv.py](../hv.py) maps hvctl's
   canonical name to every spelling worth trying, best first.
3. **Presence.**  A module may simply not have `MAXV`, or `BDALARM`, or `ON`.

`Device.resolve_par(names, board=False)` handles all three: it returns the
module's own spelling of the first name it has, or `None` when the module keeps
a complete list and none of them is on it.  A column whose parameter resolves to
nothing is left out of the table.

### Switching a channel on and off

Most firmware takes `PAR:ON` and `PAR:OFF`.  Some has neither, and exposes a
single `Pw` parameter with On/Off states instead -- the known DUT here is one.
`Device._power` tries the advertised form first and the other on `PAR:ERR`, then
remembers which one worked.  Any other refusal is passed straight up: LOCAL mode
is an answer, not a misunderstanding.

This is worth verifying against real hardware before it is trusted.  A channel
that will not switch off when asked is the worst failure this program has.

### Board-wide

`BDCTR` is REMOTE or LOCAL and decides whether any `SET` is accepted at all.
`BDILK` is the interlock, `BDALARM` the alarm word -- not every module has one,
and the panel says `alarm n/a` rather than green when it does not.  `BDCLR`
(a `SET` with no value) clears the alarm and latched trips.

## STATUS

A 16-bit word per channel, decoded by `decode_status` in [hv.py](../hv.py):

    0 ON     1 RUP    2 RDW    3 OVC     4 OVV    5 UNV    6 TRIP   7 OVP
    8 TWN    9 OVT   10 KILL  11 INTLK  12 ISDIS 13 FAIL  14 LOCK  15 MAXV

`hvctl get 0 STATUS --raw-value` prints the bare integer.  Board `BdStatus` is a
different word; its bit meanings are not known here, and nothing displays it.

## Adding support for a quirk

Put the new spelling in `PAR_ALIASES` rather than renaming anything -- the
canonical name is what the columns, the log and the tests are written in.

Then reproduce the firmware in [tests/fakes.py](../tests/fakes.py) as a
transport subclass that breaks exactly one assumption, the way `DropsGroupReads`,
`LegacyNames` and `AdvertisesButRefuses` do.  The simulator in `hv.py` should behave like real
firmware, not like the client: when the two disagreed about `RDWN`, the
simulator was agreeing with the client and hid the bug for as long as it
existed.
