"""Parameter-name handling: the PAR:ERR that used to arrive every few seconds.

The panel asked for a fixed list of parameters without checking whether the
module had them.  A name the module did not know -- RDWN on firmware that
calls it RDW, MAXV on a module without one -- earned a PAR:ERR, and because
those columns are only polled every sixth cycle, it arrived periodically and
took the whole reading down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hv import Device, HVError, SimTransport                       # noqa: E402
from hvprofiles import ProfileStore                                # noqa: E402
from hvtui import COLUMNS, HVApp                                   # noqa: E402
from tests.fakes import (AdvertisesButRefuses, CountingTransport,  # noqa: E402
                         DropsGroupReads, LegacyNames, NoParamList, RealDUT)
from textual.widgets import DataTable                              # noqa: E402


def setUpModule():
    os.environ["HVCTL_PROFILES"] = os.path.join(tempfile.mkdtemp(), "p.json")


class ErrorHeaders(unittest.TestCase):
    """"No such parameter" has to be distinguishable from "the link died"."""

    def test_par_error_is_labelled(self):
        dev = Device(LegacyNames())
        with self.assertRaises(HVError) as caught:
            dev.read_all("RDW")
        self.assertEqual(caught.exception.header, "PAR")
        self.assertTrue(caught.exception.unknown_param)

    def test_link_failure_is_not_a_param_error(self):
        class Deaf(SimTransport):
            def readline(self):
                raise HVError("timeout waiting for reply")

        with self.assertRaises(HVError) as caught:
            Device(Deaf()).mon_bd("BDNCH")
        self.assertEqual(caught.exception.header, "")
        self.assertFalse(caught.exception.unknown_param)


class Resolution(unittest.TestCase):
    """Names are matched against what the module advertises, once."""

    def test_alias_is_found(self):
        dev = Device(LegacyNames())
        self.assertEqual(dev.resolve_par(("RDW", "RDWN")), "RDWN")

    def test_absent_parameter_resolves_to_nothing(self):
        dev = Device(LegacyNames())
        self.assertIsNone(dev.resolve_par(("MAXV",)))
        self.assertIsNone(dev.resolve_par(("IMRANGE", "IRANGE")))

    def test_present_parameter_keeps_its_name(self):
        dev = Device(CountingTransport())
        self.assertEqual(dev.resolve_par(("RDW", "RDWN")), "RDW")

    def test_the_list_is_only_asked_for_once(self):
        t = CountingTransport()
        dev = Device(t)
        for _ in range(5):
            dev.resolve_par(("VSET",))
            dev.resolve_par(("BDCTR",), board=True)
        self.assertEqual(t.count("PARCHLIST"), 1)
        self.assertEqual(t.count("PARLIST"), 1)

    def test_no_list_means_no_filtering(self):
        """Silence is not evidence of absence: try the name anyway."""
        dev = Device(NoParamList())
        self.assertEqual(dev.resolve_par(("RDW", "RDWN")), "RDW")
        self.assertEqual(dev.resolve_par(("NOSUCHTHING",)), "NOSUCHTHING")


class GroupReads(unittest.TestCase):
    """A bad parameter name must not disable bulk reads for everything else."""

    def test_par_error_leaves_group_reads_alone(self):
        dev = Device(LegacyNames())
        with self.assertRaises(HVError):
            dev.read_all("RDW")             # the name this module lacks
        self.assertIsNot(dev._group_ok, False)
        self.assertEqual(len(dev.read_all("VSET")), 8)
        self.assertIs(dev._group_ok, True)

    def test_channel_error_does_disable_them(self):
        t = DropsGroupReads()
        dev = Device(t)
        self.assertEqual(len(dev.read_all("VSET")), 8)
        self.assertIs(dev._group_ok, False)
        self.assertGreaterEqual(t.count("VSET"), 8)     # one per channel


class Snapshot(unittest.TestCase):
    """The CLI's status/monitor path survives a module missing a column."""

    def test_missing_parameter_is_blank_not_fatal(self):
        rows = Device(LegacyNames()).snapshot(["VSET", "RDW", "MAXV"])
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["MAXV"], "")

    def test_aliased_parameter_is_still_read(self):
        rows = Device(LegacyNames()).snapshot(["RDW"])
        self.assertEqual(float(rows[0]["RDW"]), 100.0)

    def test_status_renders_against_legacy_firmware(self):
        import hv
        text = hv.render_status(Device(LegacyNames()), hv.Palette(False))
        self.assertIn("VMon", text)
        self.assertEqual(len(text.splitlines()), 10)    # header, rule, 8 rows


class MixedCaseNames(unittest.TestCase):
    """Firmware reports VMon, RDwn, BdIlk; it accepts any case on the way in."""

    def setUp(self):
        self.dev = Device(RealDUT())

    def test_the_modules_own_spelling_goes_on_the_wire(self):
        """Echo what it advertised rather than betting it ignores case."""
        self.assertEqual(self.dev.resolve_par(("VMON",)), "VMon")
        self.assertEqual(self.dev.resolve_par(("RDW", "RDWN")), "RDwn")
        self.assertEqual(self.dev.resolve_par(("IMRANGE", "IRANGE")), "IMRange")
        self.assertEqual(self.dev.resolve_par(("BDCTR",), board=True), "BdCtr")

    def test_an_unlisted_name_is_sent_as_written(self):
        self.assertEqual(self.dev.resolve_par(("BDNCH",), board=True), "BDNCH")

    def test_a_genuinely_absent_parameter_is_still_absent(self):
        self.assertIsNone(self.dev.resolve_par(("MAXV", "MAXVSET", "SVMAX")))


class PartialParameterLists(unittest.TestCase):
    """A list that omits something that works cannot prove anything absent."""

    def test_a_short_list_is_noticed(self):
        dev = Device(RealDUT())
        self.assertTrue(dev.lists_everything())             # PARCHLIST is full
        self.assertFalse(dev.lists_everything(board=True))  # PARLIST is not

    def test_unlisted_board_parameters_are_tried_anyway(self):
        """BDNCH is missing from PARLIST and answers perfectly well."""
        dev = Device(RealDUT())
        self.assertEqual(dev.resolve_par(("BDALARM",), board=True), "BDALARM")
        self.assertEqual(dev.nch, 8)

    def test_a_full_list_is_still_trusted(self):
        dev = Device(RealDUT())
        self.assertIsNone(dev.resolve_par(("NOSUCHPARAM",)))


class SwitchingChannels(unittest.TestCase):
    """Turning a channel on and off has to work on both command styles."""

    def test_a_module_with_only_pw(self):
        t = RealDUT()
        dev = Device(t)
        dev.channel_on(0)
        self.assertTrue(t.ch[0]["_on"])
        dev.channel_off(0)
        self.assertFalse(t.ch[0]["_on"])

    def test_the_working_form_is_remembered(self):
        """Do not pay for the failed ON attempt on every later switch."""
        t = RealDUT()
        dev = Device(t)
        dev.channel_on(0)
        first = t.count("ON")
        dev.channel_off(1)
        dev.channel_on(1)
        self.assertEqual(t.count("ON"), first)      # not retried
        self.assertTrue(t.ch[1]["_on"])

    def test_a_module_with_only_on_off(self):
        t = CountingTransport()
        dev = Device(t)
        dev.channel_on(0)
        self.assertTrue(t.ch[0]["_on"])
        self.assertEqual(t.count("PW"), 0)          # never went looking

    def test_local_mode_is_reported_not_worked_around(self):
        """A refusal is an answer; only PAR:ERR justifies trying the other form."""
        t = RealDUT()
        t.bd["BDCTR"] = "LOCAL"
        with self.assertRaises(HVError) as caught:
            Device(t).channel_off(1)
        self.assertEqual(caught.exception.header, "LOC")


# --------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def panel(transport, cycles: int = 14):
    """Run the panel against a module until it has polled `cycles` times.

    Enough turns to cross several `SLOW_EVERY` boundaries, which is where the
    periodic error used to appear.
    """
    app = HVApp(Device(transport), interval=0.05, store=ProfileStore())
    # Wide enough that the board line and every column render unwrapped.
    async with app.run_test(size=(120, 32)) as pilot:
        for _ in range(200):
            await pilot.pause(0.05)
            if app._rows_built and app.worker and app.worker._cycle > cycles:
                break
        try:
            yield app
        finally:
            app.exit()


def screen_text(app) -> str:
    return "\n".join(s.text for s in app.screen._compositor.render_strips())


class PanelAdaptsToTheModule(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Textual's own work outruns asyncio's debug-mode slow-callback
        # threshold, and the warnings bury the results.
        asyncio.get_running_loop().set_debug(False)

    async def test_legacy_module_polls_cleanly(self):
        t = LegacyNames()
        async with panel(t) as app:
            shown = [c.par for c in app.columns]
            self.assertNotIn("MAXV", shown)
            self.assertNotIn("IMRANGE", shown)
            self.assertIn("RDW", shown)

            # Never asked for what the module said it did not have...
            self.assertEqual(t.count("MAXV"), 0)
            self.assertEqual(t.count("IMRANGE"), 0)
            # ...and asked for ramp-down under the module's own name.
            self.assertGreater(t.count("RDWN"), 1)

            self.assertIsNone(app._last_error)
            self.assertNotIn("link error", screen_text(app))

    async def test_legacy_module_still_shows_its_readings(self):
        async with panel(LegacyNames()) as app:
            self.assertEqual(app.nch, 8)
            self.assertTrue(app.values[1].get("RDW"))
            self.assertTrue(app.values[1].get("VMON"))

    async def test_table_matches_the_columns(self):
        async with panel(LegacyNames()) as app:
            table = app.query_one("#chans", DataTable)
            self.assertEqual(len(table.columns), len(app.columns) + 1)  # + CH
            self.assertEqual(table.row_count, 8)
            self.assertNotIn("MaxV", screen_text(app))

    async def test_dropped_parameters_are_mentioned_once(self):
        async with panel(LegacyNames()) as app:
            body = screen_text(app)
            self.assertEqual(body.count("not offered by this module"), 1)

    async def test_a_refused_parameter_is_abandoned(self):
        """Advertised, then refused: give up on it, do not ask every cycle."""
        t = AdvertisesButRefuses()
        async with panel(t) as app:
            self.assertLessEqual(t.count("MAXV"), 2)
            self.assertNotIn("MAXV", [c.par for c in app.columns])
            self.assertIsNone(app._last_error)
            self.assertGreater(app.worker._cycle, 10)   # polling carried on
            self.assertEqual(screen_text(app).count("no longer polled"), 1)

    async def test_module_without_a_parameter_list_still_works(self):
        t = NoParamList()
        async with panel(t) as app:
            self.assertEqual(app.nch, 8)
            self.assertIsNone(app._last_error)
            self.assertEqual(len(app.columns), len(COLUMNS))

    async def test_real_module_polls_without_errors(self):
        """The module this was all diagnosed against."""
        t = RealDUT()
        async with panel(t) as app:
            shown = [c.par for c in app.columns]
            self.assertNotIn("MAXV", shown)
            self.assertIn("RDW", shown)
            self.assertIn("IMRANGE", shown)
            self.assertEqual(t.count("MAXV"), 0)

            # BDALARM is absent but unlisted, so it is tried once and dropped.
            self.assertLessEqual(t.count("BDALARM"), 2)
            self.assertIsNone(app._last_error)
            self.assertNotIn("link error", screen_text(app))
            self.assertTrue(app.values[1].get("VMON"))

    async def test_a_missing_alarm_reads_as_unknown(self):
        async with panel(RealDUT()) as app:
            self.assertNotIn("BDALARM", app.board)
            self.assertIn("alarm n/a", screen_text(app))

    async def test_healthy_module_is_unaffected(self):
        t = CountingTransport()
        async with panel(t) as app:
            self.assertEqual(len(app.columns), len(COLUMNS))
            self.assertIsNone(app._last_error)
            self.assertIs(app.dev._group_ok, True)
            self.assertNotIn("no longer polled", screen_text(app))

    async def test_edits_use_the_modules_own_name(self):
        """A rename must reach the wire, or the write lands on nothing."""
        t = LegacyNames()
        async with panel(t) as app:
            before = t.count("RDWN")
            app.submit("test", lambda d: d.set_ch(
                0, app.worker.wire_name("RDW"), "120"))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if t.count("RDWN") > before:
                    break
            self.assertGreater(t.count("RDWN"), before)
            self.assertEqual(t.ch[0]["RDW"], 120.0)      # internal key


if __name__ == "__main__":
    unittest.main()
