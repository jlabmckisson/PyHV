# Tests

Standard library only -- `unittest`, no pytest, so they run anywhere hvctl
runs.  From the repository root:

    python3 -m unittest discover -s tests -t .

or a single file:

    python3 -m unittest tests.test_params -v

`-t .` matters: it puts the repository root on `sys.path` so the tests can
import `hv`, `hvtui` and `hvprofiles`.

Nothing here touches hardware.  The device tests drive `SimTransport` and
subclasses of it that misbehave in specific ways (older parameter names,
missing parameters, no PARCHLIST), and the panel tests drive the real
Textual app through its `Pilot`.  Profiles are written to a temporary
directory via `$HVCTL_PROFILES`, never to `~/.config`.
