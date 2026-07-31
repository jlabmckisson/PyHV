"""The panel, driven through Textual's Pilot as a user would drive it.

Covers the module picker and profile dialog (create, edit, delete), and the
connection lifecycle: switching modules while channels are live, a connect
that fails, and quitting.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hvprofiles import Profile, ProfileStore                   # noqa: E402
from hvtui import ConnectScreen, HVApp, ProfileEditScreen      # noqa: E402
from textual.widgets import (DataTable, Input, Label,          # noqa: E402
                             RichLog, Static)

# A button ignores clicks while its press-flash is showing, so a test that
# clicks the same button repeatedly has to let the flash expire.
FLASH = 0.3


class PanelTest(unittest.IsolatedAsyncioTestCase):
    """Two saved modules, neither reachable, plus the simulator."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "profiles.json")
        os.environ["HVCTL_PROFILES"] = self.path
        store = ProfileStore(self.path)
        store.profiles = [Profile("Lab rack A", "192.168.0.250", 1470),
                          Profile("Test bench", "10.0.0.7", 1471)]
        store.last_used = "Test bench"
        store.save()
        self.app = HVApp(None, interval=0.2, store=ProfileStore(self.path))

    async def asyncSetUp(self):
        # Textual's own work outruns asyncio's debug-mode slow-callback
        # threshold, and the warnings bury the results.
        asyncio.get_running_loop().set_debug(False)

    def screen_text(self) -> str:
        return "\n".join(s.text for s in
                         self.app.screen._compositor.render_strips())

    def saved_names(self) -> list[str]:
        with open(self.path) as fh:
            return [p["name"] for p in json.load(fh)["profiles"]]

    def error_text(self) -> str:
        return self.app.screen.query_one("#dlg-error", Label).render().plain

    def log_text(self) -> str:
        return "\n".join(strip.text for strip
                         in self.app.query_one("#log", RichLog).lines)

    async def settle(self, pilot, done, tries: int = 40):
        for _ in range(tries):
            await pilot.pause(0.1)
            if done():
                return
        self.fail("timed out waiting for the panel")

    async def connected(self, pilot):
        """Bring the panel up on the simulator, with a reading in hand.

        Waiting for `_rows_built` alone is not enough: it means the table has
        its columns, which `on_device_ready` builds from the parameter list
        before any channel has been read.  `flags` and `values` stay empty
        until the first poll lands, and a test that acts in that window acts on
        a panel that does not yet know what the channels are doing -- no live
        channels to warn about, no setpoint to step from.  Both callbacks
        arrive in the same pause on a fast machine, which is why this only
        ever failed on CI.
        """
        await pilot.pause()
        await pilot.press("down", "down", "enter")         # the simulator
        await self.settle(pilot, lambda: self.app._rows_built
                          and all(self.app.values))
        return self.app.worker


class ModulePicker(PanelTest):

    async def test_it_opens_on_the_picker(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConnectScreen)
            table = self.app.screen.query_one("#profiles", DataTable)
            self.assertEqual(table.row_count, 3)     # two saved + simulator
            self.app.exit()

    async def test_it_starts_on_the_module_used_last(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(self.app.screen.selected.name, "Test bench")
            self.app.exit()

    async def test_it_shows_addresses_and_the_simulator(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            body = self.screen_text()
            self.assertIn("192.168.0.250:1470", body)
            self.assertIn("10.0.0.7:1471", body)
            self.assertIn("built-in simulator", body)
            self.assertIn("last used", body)
            self.app.exit()

    async def test_enter_connects(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down", "down")        # onto the simulator
            self.assertTrue(self.app.screen.selected.is_sim)
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app._rows_built)
            self.assertIsNotNone(self.app.dev)
            self.assertNotIsInstance(self.app.screen, ConnectScreen)
            self.assertEqual(self.app.nch, 8)
            self.assertIn("DT8033", self.screen_text())
            self.assertIn("Simulator", self.app.sub_title)
            self.app.exit()

    async def test_the_simulator_is_not_remembered(self):
        """Connecting to the fake module must not become the CLI default."""
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await self.settle(pilot, lambda: self.app._rows_built)
            self.assertEqual(ProfileStore(self.path).last_used, "Test bench")
            self.app.exit()

    async def test_q_quits_when_nothing_is_connected(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause(FLASH)
        self.assertFalse(self.app.is_running)
        self.assertIsNone(self.app.dev)


class CreatingProfiles(PanelTest):

    async def test_n_opens_an_empty_form(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ProfileEditScreen)
            self.assertEqual(
                self.app.screen.query_one("#f-name", Input).value, "")
            self.app.exit()

    async def test_a_new_profile_is_saved_and_selected(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen
            screen.query_one("#f-name", Input).value = "Rack B"
            screen.query_one("#f-addr", Input).value = "192.168.0.251:1480"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertEqual(self.saved_names(),
                             ["Lab rack A", "Test bench", "Rack B"])
            self.assertEqual(self.app.screen.selected.name, "Rack B")
            self.assertIn("192.168.0.251:1480", self.screen_text())
            self.app.exit()

    async def test_bad_input_is_caught_and_nothing_is_written(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen

            await pilot.click("#save")               # nothing filled in
            await pilot.pause(FLASH)
            self.assertIn("address required", self.error_text())

            screen.query_one("#f-addr", Input).value = "192.168.0.9"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIn("name required", self.error_text())

            screen.query_one("#f-name", Input).value = "test bench"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIn("already exists", self.error_text())

            screen.query_one("#f-name", Input).value = "Simulator"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIn("reserved", self.error_text())

            screen.query_one("#f-name", Input).value = "Rack C"
            screen.query_one("#f-port", Input).value = "99999"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIn("between 1 and 65535", self.error_text())

            self.assertEqual(self.saved_names(), ["Lab rack A", "Test bench"])

            screen.query_one("#f-port", Input).value = "1470"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertEqual(self.saved_names(),
                             ["Lab rack A", "Test bench", "Rack C"])
            self.app.exit()

    async def test_a_serial_port_is_recognised_as_it_is_typed(self):
        """No mode switch: the second field relabels itself."""
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen
            label = screen.query_one("#f-port-label", Label)
            self.assertEqual(str(label.render()), "Port")

            screen.query_one("#f-addr", Input).value = "/dev/ttyACM0"
            await pilot.pause()
            self.assertEqual(str(label.render()), "Baud")
            self.assertEqual(screen.query_one("#f-port", Input).value, "9600")

            screen.query_one("#f-addr", Input).value = "192.168.0.9"
            await pilot.pause()
            self.assertEqual(str(label.render()), "Port")
            self.assertEqual(screen.query_one("#f-port", Input).value, "1470")
            self.app.exit()

    async def test_a_typed_rate_is_not_overwritten(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen
            screen.query_one("#f-port", Input).value = "115200"
            screen.query_one("#f-addr", Input).value = "/dev/ttyACM0"
            await pilot.pause()
            self.assertEqual(screen.query_one("#f-port", Input).value, "115200")
            self.app.exit()

    async def test_a_serial_profile_is_saved_and_listed(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen
            screen.query_one("#f-name", Input).value = "Bench USB"
            screen.query_one("#f-addr", Input).value = "/dev/ttyACM0"
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIsInstance(self.app.screen, ConnectScreen)
            saved = self.app.store.get("Bench USB")
            self.assertTrue(saved.is_serial)
            self.assertEqual(saved.device, "/dev/ttyACM0")
            self.assertEqual(saved.baud, 9600)
            self.assertIn("/dev/ttyACM0 @9600", self.screen_text())
            self.app.exit()

    async def test_editing_a_serial_profile_opens_on_its_port(self):
        self.app.store.upsert(Profile("Bench USB", kind="serial",
                                      device="/dev/ttyACM0", baud=115200))
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            await pilot.press("e")
            await pilot.pause()
            screen = self.app.screen
            self.assertIsInstance(screen, ProfileEditScreen)
            self.assertEqual(screen.query_one("#f-addr", Input).value,
                             "/dev/ttyACM0")
            self.assertEqual(screen.query_one("#f-port", Input).value, "115200")
            self.assertEqual(
                str(screen.query_one("#f-port-label", Label).render()), "Baud")
            self.app.exit()

    async def test_a_bad_serial_rate_is_caught(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = self.app.screen
            screen.query_one("#f-name", Input).value = "Bench USB"
            screen.query_one("#f-addr", Input).value = "/dev/ttyACM0"
            await pilot.pause()
            screen.query_one("#f-port", Input).value = "9"
            await pilot.click("#save")
            await pilot.pause(FLASH)
            self.assertIn("out of range", self.error_text())
            self.assertEqual(self.saved_names(), ["Lab rack A", "Test bench"])
            self.app.exit()

    async def test_cancelling_leaves_the_store_alone(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.app.screen.query_one("#f-name", Input).value = "Discarded"
            self.app.screen.query_one("#f-addr", Input).value = "10.0.0.1"
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.saved_names(), ["Lab rack A", "Test bench"])
            self.app.exit()


class EditingProfiles(PanelTest):

    async def test_the_form_is_prefilled(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up")                  # Lab rack A
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(
                self.app.screen.query_one("#f-addr", Input).value,
                "192.168.0.250")
            self.app.exit()

    async def test_a_rename_keeps_the_row_in_place(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("up", "e")
            await pilot.pause()
            self.app.screen.query_one("#f-name", Input).value = "Lab rack A2"
            self.app.screen.query_one("#f-addr", Input).value = "192.168.0.9"
            await pilot.press("enter")               # submitting saves
            await pilot.pause()
            self.assertEqual(self.saved_names(), ["Lab rack A2", "Test bench"])
            self.app.exit()

    async def test_deleting_asks_first(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            self.assertIn("Delete profile", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(self.app.store.profiles), 2)
            self.app.exit()

    async def test_confirmed_deletion_persists(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(self.app.store.names(), ["Lab rack A"])
            self.assertEqual(self.saved_names(), ["Lab rack A"])
            self.assertEqual(
                self.app.screen.query_one("#profiles", DataTable).row_count, 2)
            self.app.exit()

    async def test_the_simulator_cannot_be_edited_or_deleted(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            self.assertTrue(self.app.screen.selected.is_sim)
            await pilot.press("d")
            await pilot.pause()
            self.assertIn("cannot be deleted", self.error_text())
            await pilot.press("e")
            await pilot.pause()
            self.assertIn("nothing to configure", self.error_text())
            self.app.exit()


class SwitchingModules(PanelTest):

    async def test_switching_warns_while_channels_are_live(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("m")
            await pilot.pause()
            self.assertIn("Disconnect with channels live", self.screen_text())
            self.app.exit()

    async def test_switching_warns_before_the_first_reading(self):
        """Nothing read is not nothing energised, and is not treated as it.

        The panel looks the same in both cases -- empty flags -- for as long as
        it takes the first poll to come back, which on a module answering at
        9600 baud is not instant.
        """
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app.worker.paused = True            # no poll to refill them
            await pilot.pause(0.3)                   # let one in flight land
            nch = self.app.nch
            self.app.flags = [[] for _ in range(nch)]     # what on_device_ready
            self.app.values = [{} for _ in range(nch)]    # leaves behind
            await pilot.press("m")
            await pilot.pause()
            body = self.screen_text()
            self.assertIn("Disconnect before the first reading", body)
            self.assertIn("not known here", body)
            self.app.exit()

    async def test_cancelling_the_picker_keeps_the_link(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")                   # yes, disconnect is fine
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertIn("Cancel", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNotNone(self.app.dev)
            self.assertIs(self.app.worker, worker)
            self.app.exit()

    async def test_a_failed_connect_leaves_the_old_link_running(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            self.app.store.upsert(Profile("Nowhere", "127.0.0.1", 9))
            self.app.timeout = 0.4
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            self.assertEqual(self.app.screen.selected.name, "Nowhere")
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(
                self.app.screen, ConnectScreen) and self.app.screen.is_active)
            await pilot.pause(0.2)
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertIs(self.app.worker, worker)
            self.app.exit()

    async def test_reconnecting_rebuilds_the_table(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.worker is not worker
                              and self.app._rows_built)
            self.assertTrue(worker._stop.is_set())
            self.assertEqual(self.app.nch, 8)
            self.assertEqual(
                self.app.query_one("#chans", DataTable).row_count, 8)
            self.app.exit()


class SelectingChannels(PanelTest):
    """Marking several channels so one edit reaches all of them."""

    async def connected(self, pilot):
        worker = await super().connected(pilot)
        self.app.query_one("#chans", DataTable).focus()
        return worker

    def ch_column(self) -> list[str]:
        table = self.app.query_one("#chans", DataTable)
        return [table.get_cell(f"ch{c}", "CH").plain
                for c in range(self.app.nch)]

    async def test_s_marks_the_channel_under_the_cursor(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("s", "down", "down", "s")
            await pilot.pause()
            self.assertEqual(self.app.selected, {0, 2})
            self.assertEqual(self.ch_column()[:3], ["*0", "1", "*2"])
            await pilot.press("s")                   # same channel again
            await pilot.pause()
            self.assertEqual(self.app.selected, {0})
            self.app.exit()

    async def test_a_and_u_select_and_unselect_every_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(self.app.selected, set(range(self.app.nch)))
            self.assertIn("sel 0-7", self.screen_text())
            await pilot.press("u")
            await pilot.pause()
            self.assertEqual(self.app.selected, set())
            self.assertNotIn("sel 0-7", self.screen_text())
            self.assertEqual(self.ch_column()[:2], ["0", "1"])
            self.app.exit()

    async def test_escape_also_unselects(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("a", "escape")
            await pilot.pause()
            self.assertEqual(self.app.selected, set())
            self.app.exit()

    async def test_the_dialog_names_every_target(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("s", "down", "s", "down", "s")
            await pilot.press("right", "enter")      # VSet on channel 2
            await pilot.pause()
            body = self.screen_text()
            self.assertIn("CH 0-2", body)
            self.assertIn("(3 channels)", body)
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_one_edit_writes_to_every_selected_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("s", "down", "down", "s")   # channels 0 and 2
            await pilot.press("right", "enter")           # VSet
            await pilot.pause()
            self.app.screen.query_one("#value", Input).value = "123.4"
            await pilot.press("enter")
            await self.settle(pilot, lambda: all(
                self.app.values[c].get("VSET", "").startswith("123.4")
                for c in (0, 2)))
            self.assertNotEqual(self.app.values[1].get("VSET"), "123.4")
            self.app.exit()

    async def test_without_a_selection_only_the_cursor_channel_is_written(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "right", "enter")   # CH1 VSet
            await pilot.pause()
            self.assertIn("CH 1", self.screen_text())
            self.assertNotIn("channels)", self.screen_text())
            self.app.screen.query_one("#value", Input).value = "77.5"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.values[1]
                              .get("VSET", "").startswith("77.5"))
            self.assertFalse(self.app.values[0].get("VSET", "")
                             .startswith("77.5"))
            self.app.exit()

    async def test_switching_modules_forgets_the_selection(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")                   # yes, disconnect is fine
            await pilot.pause()
            # The picker can still be cancelled, and that keeps the link --
            # so it has to keep the selection too.
            self.assertEqual(self.app.selected, set(range(8)))
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.worker is not worker
                              and self.app._rows_built)
            self.assertEqual(self.app.selected, set())
            self.app.exit()


class TheLog(PanelTest):
    """Everything the operator changes ends up in the log.

    The panel's log is the only record of a session in front of a live
    supply, so a reading of it afterwards should say what was done and to
    what -- not just the SETs, but what they were aimed at.
    """

    async def connected(self, pilot):
        worker = await super().connected(pilot)
        self.app.query_one("#chans", DataTable).focus()
        return worker

    async def test_selection_changes_are_recorded(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("s", "down", "s")
            await pilot.pause()
            self.assertIn("selected 0-1", self.log_text())
            await pilot.press("u")
            await pilot.pause()
            self.assertIn("selection cleared", self.log_text())
            self.app.exit()

    async def test_the_poll_rate_is_recorded_when_it_changes(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("plus")
            await pilot.pause()
            self.assertIn("poll every 0.4s", self.log_text())
            self.app.exit()

    async def test_a_rate_already_at_its_limit_is_not_recorded(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            for _ in range(3):        # 0.2s is already under the 0.25s floor
                await pilot.press("minus")
            await pilot.pause()
            self.assertEqual(self.app.interval, 0.25)
            self.assertEqual(self.log_text().count("poll every 0.25s"), 1)
            self.app.exit()

    async def test_a_write_is_recorded_with_its_channel_and_value(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "right", "enter")   # CH1 VSet
            await pilot.pause()
            self.app.screen.query_one("#value", Input).value = "42.0"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.values[1]
                              .get("VSET", "").startswith("42"))
            self.assertIn("CH1 VSET = 42.0", self.log_text())
            self.app.exit()

    async def test_profile_edits_reach_the_panel_log(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("m")                    # back to the picker
            await pilot.pause()
            await pilot.press("y")                    # yes, the sim is live
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=0)
            await pilot.press("d")                    # delete Lab rack A
            await pilot.pause()
            await pilot.press("y")
            await self.settle(pilot, lambda: "Lab rack A"
                              not in self.saved_names())
            await pilot.press("escape")               # keep the current link
            await pilot.pause()
            self.assertIn("profile Lab rack A deleted", self.log_text())
            self.app.exit()


class AdjustingChannels(PanelTest):
    """`d` shifts a setpoint by a step, from wherever each channel sits.

    The simulator starts with VSET 0, 1500, 1500, 2400 on channels 0-3, and
    its descriptor puts VSET in 0..4000 V.
    """

    async def connected(self, pilot):
        worker = await super().connected(pilot)
        self.app.query_one("#chans", DataTable).focus()
        return worker

    def preview(self) -> str:
        return self.app.screen.query_one("#plan", Static).render().plain

    async def test_it_previews_a_new_value_for_every_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "s", "down", "s")   # channels 1 and 2
            await pilot.press("right", "d")               # VSet column
            await pilot.pause()
            self.assertIn("(2 channels)", self.screen_text())
            self.app.screen.query_one("#step", Input).value = "50"
            await pilot.pause()
            plan = self.preview()
            self.assertIn("1550.0", plan)
            self.assertEqual(plan.count("→"), 2)
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_a_step_is_applied_to_every_selected_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "s", "down", "s")   # 1 and 2, both 1500
            await pilot.press("right", "d")
            await pilot.pause()
            self.app.screen.query_one("#step", Input).value = "-100"
            await pilot.press("enter")
            await self.settle(pilot, lambda: all(
                self.app.values[c].get("VSET", "").startswith("1400")
                for c in (1, 2)))
            self.assertTrue(self.app.values[3].get("VSET", "")
                            .startswith("2400"))
            self.app.exit()

    async def test_channels_keep_their_own_starting_points(self):
        """The point of a step: two different setpoints stay different."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "s", "down", "down", "s")   # 1 and 3
            await pilot.press("right", "d")
            await pilot.pause()
            self.app.screen.query_one("#step", Input).value = "100"
            await pilot.press("enter")
            await self.settle(pilot, lambda:
                              self.app.values[1].get("VSET", "")
                              .startswith("1600")
                              and self.app.values[3].get("VSET", "")
                              .startswith("2500"))
            self.app.exit()

    async def test_with_no_selection_it_adjusts_the_cursor_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "right", "d")      # CH1 VSet
            await pilot.pause()
            self.assertIn("Adjust CH 1", self.screen_text())
            self.assertNotIn("channels)", self.screen_text())
            self.app.screen.query_one("#step", Input).value = "25"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.values[1]
                              .get("VSET", "").startswith("1525"))
            self.assertTrue(self.app.values[2].get("VSET", "")
                            .startswith("1500"))
            self.app.exit()

    async def test_a_step_that_leaves_the_range_is_refused(self):
        """One step, many starting points: the channel it does not suit is
        named, and nothing is written -- not even to the channels it fits."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "s", "down", "down", "s")   # 1 and 3
            await pilot.press("right", "d")
            await pilot.pause()
            self.app.screen.query_one("#step", Input).value = "1700"
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertIn("CH 3", self.error_text())        # 2400 + 1700
            self.assertEqual(len(self.app.screen.query("#step")), 1)  # still up
            self.assertTrue(self.app.values[1].get("VSET", "")
                            .startswith("1500"))
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_a_read_only_column_is_not_adjustable(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("right", "right", "d")     # VMon
            await pilot.pause()
            self.assertEqual(len(self.app.screen.query("#step")), 0)
            self.app.exit()

    async def test_a_two_valued_column_is_not_adjustable(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            table = self.app.query_one("#chans", DataTable)
            col = next(i for i, c in enumerate(self.app.columns)
                       if c.par == "PDWN")
            table.move_cursor(column=col + 1)
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(len(self.app.screen.query("#step")), 0)
            self.app.exit()

    async def test_read_only_mode_refuses_to_adjust(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app.read_only = True
            await pilot.press("right", "d")
            await pilot.pause()
            self.assertEqual(len(self.app.screen.query("#step")), 0)
            self.app.exit()


class TableWidth(PanelTest):
    """The status column takes whatever the terminal has spare."""

    def widths(self) -> tuple[int, int, int]:
        """(status width, width used by every other column, table width)."""
        table = self.app.query_one("#chans", DataTable)
        status, used = 0, 0
        for key, column in table.columns.items():
            if key.value == "STATUS":
                status = column.width
            else:
                used += column.get_render_width(table)
        return status, used, table.scrollable_content_region.width

    async def test_a_wide_terminal_is_filled(self):
        async with self.app.run_test(size=(140, 26)) as pilot:
            await self.connected(pilot)
            status, used, width = self.widths()
            self.assertGreater(status, 30)
            self.assertEqual(used + status + 2, width)   # no wasted columns
            self.app.exit()

    async def test_eighty_columns_keeps_the_declared_widths(self):
        """The narrowest terminal the panel is meant for: it scrolls instead
        of squeezing numbers that have to stay readable."""
        async with self.app.run_test(size=(80, 26)) as pilot:
            await self.connected(pilot)
            self.assertEqual(self.widths()[0], 30)
            self.app.exit()

    async def test_it_follows_the_terminal_resizing(self):
        async with self.app.run_test(size=(140, 26)) as pilot:
            await self.connected(pilot)
            grown = self.widths()[0]
            await pilot.resize_terminal(80, 26)
            await pilot.pause(0.2)
            self.assertEqual(self.widths()[0], 30)
            await pilot.resize_terminal(180, 26)
            await pilot.pause(0.2)
            self.assertGreater(self.widths()[0], grown)
            status, used, width = self.widths()
            self.assertEqual(used + status + 2, width)
            self.app.exit()


if __name__ == "__main__":
    unittest.main()
