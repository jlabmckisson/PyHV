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

from hv import STATUS_BITS, STATUS_HELP                        # noqa: E402
from hvprofiles import SIM_PROFILE, Profile, ProfileStore      # noqa: E402
from hvtui import (HELP_SECTIONS, HELP_WIDTH, ConnectScreen,   # noqa: E402
                   HVApp, HelpScreen, ModuleTabs, NameScreen,
                   ProfileEditScreen)
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

    async def second(self, pilot, name: str = "Bench sim"):
        """Open a second module, so the panel is holding two tabs.

        Another simulator rather than one of the saved addresses: a tab has to
        connect before there is anything to switch to, and nothing in these
        tests is reachable except the built-in module.  A `sim` profile is
        never written to the store, so adding one here leaves no trace.
        """
        self.app.store.profiles.append(Profile(name, kind="sim"))
        await pilot.press("m")
        await pilot.pause()
        names = [p.name for p in self.app.screen.rows]
        self.app.screen.query_one("#profiles", DataTable).move_cursor(
            row=names.index(name))
        await pilot.press("enter")
        await self.settle(pilot, lambda: len(self.app.modules) == 2
                          and self.app._rows_built and all(self.app.values))
        return self.app.modules[-1]

    def tab_bar(self) -> str:
        return self.app.query_one(ModuleTabs).render().plain


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


class ConnectingMoreModules(PanelTest):
    """`m` adds a module rather than replacing the one already connected."""

    async def test_another_module_opens_in_its_own_tab(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            await self.second(pilot)
            self.assertEqual(len(self.app.modules), 2)
            # The first is still connected and still polling: nothing about
            # opening a second supply should disturb the first.
            self.assertIsNotNone(self.app.modules[0].worker)
            self.assertFalse(worker._stop.is_set())
            self.assertIn("Simulator", self.tab_bar())
            self.assertIn("Bench sim", self.tab_bar())
            self.app.exit()

    async def test_nothing_is_disconnected_so_nothing_is_warned_about(self):
        """The old `m` dropped the link and had to ask.  This one does not."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("m")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertNotIn("stay energised", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_a_module_already_open_is_switched_to_not_dialled_twice(self):
        """Two tabs on one supply would double its poll traffic and then
        disagree with each other about what it is doing."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            await pilot.press("m")
            await pilot.pause()
            self.assertIn("open", self.screen_text())
            names = [p.name for p in self.app.screen.rows]
            self.app.screen.query_one("#profiles", DataTable).move_cursor(
                row=names.index("Simulator"))
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(self.app.modules), 2)
            self.assertEqual(self.app.active_index, 0)   # went to it instead
            self.assertIn("already open", self.log_text())
            self.app.exit()

    async def test_cancelling_the_picker_keeps_the_link(self):
        async with self.app.run_test() as pilot:
            worker = await self.connected(pilot)
            await pilot.press("m")
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
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=2)
            self.assertEqual(self.app.screen.selected.name, "Nowhere")
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(
                self.app.screen, ConnectScreen) and self.app.screen.is_active)
            await pilot.pause(0.2)
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.assertEqual(len(self.app.modules), 1)
            self.assertIs(self.app.worker, worker)
            self.app.exit()


class SwitchingTabs(PanelTest):
    """Two modules at once: which one the panel is showing, and which one
    every other key acts on."""

    async def test_brackets_move_between_modules(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            self.assertEqual(self.app.active_index, 1)
            await pilot.press("left_square_bracket")
            await pilot.pause()
            self.assertEqual(self.app.active_index, 0)
            self.assertIn("Simulator", self.app.sub_title)
            await pilot.press("right_square_bracket")
            await pilot.pause()
            self.assertEqual(self.app.active_index, 1)
            self.assertIn("Bench sim", self.app.sub_title)
            self.app.exit()

    async def test_a_digit_goes_straight_to_a_module(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(self.app.active_index, 0)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(self.app.active_index, 1)
            await pilot.press("7")                  # no seventh module
            await pilot.pause()
            self.assertEqual(self.app.active_index, 1)
            self.app.exit()

    async def test_the_table_follows_the_module_on_screen(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            table = self.app.query_one("#chans", DataTable)
            self.assertEqual(table.row_count, self.app.modules[1].nch)
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(table.row_count, self.app.modules[0].nch)
            self.assertTrue(self.app._rows_built)
            self.app.exit()

    async def test_a_tab_is_found_as_it_was_left(self):
        """The cursor belongs to the module, not to the table: coming back to
        a supply should not have moved what the next edit would reach."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app.query_one("#chans", DataTable).focus()
            await pilot.press("down", "down", "right")
            await pilot.pause()
            where = self.app.query_one("#chans", DataTable).cursor_coordinate
            await self.second(pilot)
            self.assertEqual(self.app.query_one("#chans", DataTable)
                             .cursor_coordinate.row, 0)
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(self.app.query_one("#chans", DataTable)
                             .cursor_coordinate, where)
            self.app.exit()

    async def test_each_module_keeps_its_own_selection(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app.query_one("#chans", DataTable).focus()
            await pilot.press("a")                  # every channel, module 1
            await pilot.pause()
            self.assertEqual(self.app.selected, set(range(8)))
            await self.second(pilot)
            # Another module's channels are not these, so neither is a
            # selection made against them.
            self.assertEqual(self.app.selected, set())
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(self.app.selected, set(range(8)))
            self.app.exit()

    async def test_the_bar_says_what_each_module_is_doing(self):
        """A supply nobody is looking at is exactly the one worth marking."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            await self.settle(pilot, lambda: all(self.app.modules[0].values))
            # The simulator starts with channels energised, so both read live.
            self.assertEqual(self.tab_bar().count("●"), 2)
            await pilot.press("p")                  # pause the one on screen
            await pilot.pause()
            self.assertIn("‖", self.tab_bar())
            self.assertEqual(self.tab_bar().count("‖"), 1)
            self.app.exit()

    async def test_a_module_off_screen_can_still_raise_its_hand(self):
        """The whole point of holding several: a supply nobody is looking at
        is exactly the one that trips unnoticed."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            background = self.app.modules[0]
            background.worker.paused = True         # no poll to overwrite it
            await pilot.pause(0.3)                  # let one in flight land
            background.board["BDILK"] = "YES"
            self.app._paint_tabs()
            await pilot.pause()
            self.assertTrue(background.alarmed)
            self.assertIn("!", self.tab_bar())
            # And the board line is still the module on screen, which is fine.
            self.assertNotIn("interlock YES", self.screen_text())
            self.app.exit()


class ClosingATab(PanelTest):
    """`w` is the only thing in the panel that disconnects a supply."""

    async def test_closing_warns_while_channels_are_live(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("w")
            await pilot.pause()
            body = self.screen_text()
            self.assertIn("with channels live", body)
            self.assertIn("stay energised", body)
            self.app.exit()

    async def test_closing_warns_before_the_first_reading(self):
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
            await pilot.press("w")
            await pilot.pause()
            body = self.screen_text()
            self.assertIn("before the first reading", body)
            self.assertIn("not known here", body)
            self.app.exit()

    async def test_the_prompt_names_the_module(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            await pilot.press("w")
            await pilot.pause()
            self.assertIn("Close Bench sim", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(self.app.modules), 2)
            self.app.exit()

    async def test_a_closed_module_stops_polling_and_leaves_the_rest(self):
        async with self.app.run_test() as pilot:
            first = await self.connected(pilot)
            await self.second(pilot)
            second = self.app.worker
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("y")
            await self.settle(pilot, lambda: len(self.app.modules) == 1)
            self.assertTrue(second._stop.is_set())
            self.assertFalse(first._stop.is_set())
            self.assertEqual(self.app.active_index, 0)
            self.assertIn("Simulator", self.app.sub_title)
            self.assertIn("closed Bench sim", self.log_text())
            self.app.exit()

    async def test_closing_the_last_module_leaves_the_panel_disconnected(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("y")
            await self.settle(pilot, lambda: not self.app.modules)
            self.assertIsNone(self.app.dev)
            self.assertFalse(self.app._rows_built)
            self.assertIn("not connected", self.screen_text())
            self.assertEqual(
                self.app.query_one("#chans", DataTable).row_count, 0)
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

    async def test_a_module_arrives_with_nothing_selected(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(self.app.selected, set(range(8)))
            await pilot.press("m")
            await pilot.pause()
            # The picker can still be cancelled, and that changes nothing --
            # so the selection has to survive it.
            self.assertEqual(self.app.selected, set(range(8)))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.app.selected, set(range(8)))
            await self.second(pilot)
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

    async def test_lines_say_which_module_they_came_from(self):
        """One log for the whole session, so with several supplies open every
        line has to name the one it is about.  The log outlives the tab that
        was in front at the time, and "CH0 VSET = 42.0" against no named
        supply is not a record of anything."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.second(pilot)
            self.app.query_one("#chans", DataTable).focus()
            await pilot.press("right", "enter")           # CH0 VSet
            await pilot.pause()
            self.app.screen.query_one("#value", Input).value = "42.0"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.values[0]
                              .get("VSET", "").startswith("42"))
            self.assertIn("Bench sim ✓ CH0 VSET = 42.0", self.log_text())
            self.app.exit()

    async def test_one_module_is_not_labelled(self):
        """Naming the only supply there is would be noise on every line."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("s")
            await pilot.pause()
            self.assertIn("selected 0", self.log_text())
            self.assertNotIn("Simulator selected", self.log_text())
            self.app.exit()

    async def test_profile_edits_reach_the_panel_log(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("m")                    # back to the picker
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


class NamingChannels(PanelTest):
    """A channel can be given a name, which is kept with the module's profile.

    No 803x has a channel-name parameter, so nothing here goes on the wire:
    the name is ours, and it follows the module rather than the supply.
    """

    async def connected(self, pilot):
        worker = await super().connected(pilot)
        self.app.query_one("#chans", DataTable).focus()
        return worker

    def column_keys(self) -> list[str]:
        return [k.value for k in
                self.app.query_one("#chans", DataTable).columns]

    def cell(self, ch: int, key: str) -> str:
        return self.app.query_one("#chans", DataTable).get_cell(
            f"ch{ch}", key).plain

    def saved_labels(self, profile: str = "Lab rack A") -> dict:
        with open(self.path) as fh:
            for p in json.load(fh)["profiles"]:
                if p["name"] == profile:
                    return p.get("labels", {})
        return {}

    async def name(self, pilot, text: str) -> None:
        """Press n and fill the dialog in."""
        await pilot.press("n")
        await pilot.pause()
        self.assertIsInstance(self.app.screen, NameScreen)
        self.app.screen.query_one("#value", Input).value = text
        await pilot.press("enter")
        await pilot.pause()

    async def test_the_column_appears_next_to_the_channel_number(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.assertNotIn("NAME", self.column_keys())
            await self.name(pilot, "Endcap A")
            self.assertEqual(self.column_keys()[:2], ["CH", "NAME"])
            self.assertEqual(self.cell(0, "NAME"), "Endcap A")
            self.assertEqual(self.cell(1, "NAME"), "")
            self.assertEqual(self.app.labels, {0: "Endcap A"})
            self.app.exit()

    async def test_clearing_the_last_name_takes_the_column_away(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.name(pilot, "Endcap A")
            await self.name(pilot, "")
            self.assertNotIn("NAME", self.column_keys())
            self.assertEqual(self.app.labels, {})
            self.app.exit()

    async def test_a_rebuild_leaves_the_cursor_on_the_same_column(self):
        """The column it was on, not the index it was at -- otherwise the
        first name of the session shifts every reading one place right."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("right")               # VSet, at index 1
            table = self.app.query_one("#chans", DataTable)
            self.assertEqual(table.cursor_column, 1)
            await self.name(pilot, "Endcap A")
            self.assertEqual(self.column_keys()[table.cursor_column], "VSET")
            self.app.exit()

    async def test_the_column_widens_to_the_longest_name(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.name(pilot, "Endcap A")
            table = self.app.query_one("#chans", DataTable)
            narrow = next(c for k, c in table.columns.items()
                          if k.value == "NAME").width
            await pilot.press("down")
            await self.name(pilot, "Barrel ring 3 west")
            wide = next(c for k, c in table.columns.items()
                        if k.value == "NAME").width
            self.assertEqual(wide, len("Barrel ring 3 west"))
            self.assertGreater(wide, narrow)
            self.app.exit()

    async def test_enter_on_the_name_cell_opens_the_dialog(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.name(pilot, "Endcap A")
            table = self.app.query_one("#chans", DataTable)
            table.move_cursor(row=0, column=1)       # the Name column
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, NameScreen)
            self.assertEqual(self.app.screen.query_one("#value", Input).value,
                             "Endcap A")
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_a_name_is_not_something_to_step(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.name(pilot, "Endcap A")
            self.app.query_one("#chans", DataTable).move_cursor(row=0, column=1)
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(len(self.app.screen.query("#step")), 0)
            self.app.exit()

    async def test_a_name_is_saved_to_the_profile(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            # The panel is on the simulator, which is never written to disk.
            # Point it at a saved profile so what is under test is the path
            # that persists names, not the link that happens to be up.
            self.app.profile = self.app.store.get("Lab rack A")
            await self.name(pilot, "Endcap A")
            self.assertEqual(self.saved_labels(), {"0": "Endcap A"})
            self.assertEqual(
                self.app.store.get("Lab rack A").labels, {0: "Endcap A"})
            self.app.exit()

    async def test_a_name_survives_the_profile_being_edited(self):
        """Editing the profile builds a new one from the form, which has no
        name fields on it -- readdressing a module does not rewire it."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app.profile = self.app.store.get("Lab rack A")
            await self.name(pilot, "Endcap A")
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")                   # yes, the sim is live
            await pilot.pause()
            self.app.screen.query_one("#profiles", DataTable).move_cursor(row=0)
            await pilot.press("e")
            await pilot.pause()
            self.app.screen.query_one("#f-addr", Input).value = "10.9.9.9"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.saved_labels(), {"0": "Endcap A"})
            self.app.exit()

    async def test_the_simulator_says_a_name_will_not_be_kept(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await self.name(pilot, "Endcap A")
            self.assertEqual(self.cell(0, "NAME"), "Endcap A")
            self.assertIn("not saved", self.log_text())
            self.assertIn("simulator", self.log_text())
            self.app.exit()

    async def test_names_arrive_with_the_connection(self):
        SIM_PROFILE.labels = {1: "Endcap A", 99: "a module this size has no 99"}
        self.addCleanup(SIM_PROFILE.labels.clear)
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.assertIn("NAME", self.column_keys())
            self.assertEqual(self.cell(1, "NAME"), "Endcap A")
            # Kept, but not shown: a name past the end of the module belongs
            # to a cable, and nobody unplugged it by mistyping an address.
            self.assertIn(99, self.app.labels)
            self.assertNotIn(99, self.app.named)
            self.app.exit()

    async def test_each_module_shows_only_its_own_names(self):
        """Another module's channels are not these, and neither are its names:
        they come from that module's profile, not from the session."""
        SIM_PROFILE.labels = {1: "Endcap A"}
        self.addCleanup(SIM_PROFILE.labels.clear)
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.assertEqual(self.app.labels, {1: "Endcap A"})
            self.assertIn("NAME", self.column_keys())
            await self.second(pilot)
            self.assertEqual(self.app.labels, {})
            self.assertNotIn("NAME", self.column_keys())
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(self.app.labels, {1: "Endcap A"})
            self.assertIn("NAME", self.column_keys())
            self.app.exit()

    async def test_a_name_is_refused_before_it_is_saved(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("n")
            await pilot.pause()
            self.app.screen.query_one("#value", Input).value = "Endcap [red]A"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("[ or ]", self.error_text())
            self.assertIsInstance(self.app.screen, NameScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.app.labels, {})
            self.app.exit()


class NamesInTheRecord(PanelTest):
    """Once a channel has a name, everything that mentions it uses it.

    A log read after the fact should say which detector was switched off, and
    a dialog in front of a live supply should say what it is about to reach.
    """

    async def connected(self, pilot):
        worker = await super().connected(pilot)
        self.app.query_one("#chans", DataTable).focus()
        self.app.labels = {1: "Endcap A"}
        self.app._build_table()
        return worker

    async def test_naming_is_logged(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            self.app._set_label(2, "Barrel ring 3")
            await pilot.pause()
            self.assertIn("CH2 named Barrel ring 3", self.log_text())
            self.app._set_label(2, "")
            await pilot.pause()
            self.assertIn("CH2 name cleared", self.log_text())
            self.app.exit()

    async def test_a_write_is_logged_against_the_name(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "right", "right", "enter")   # CH1 VSet
            await pilot.pause()
            self.assertIn("Endcap A", self.screen_text())
            self.app.screen.query_one("#value", Input).value = "42.0"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.values[1]
                              .get("VSET", "").startswith("42"))
            self.assertIn("CH1 Endcap A VSET = 42.0", self.log_text())
            self.app.exit()

    async def test_the_off_prompt_names_the_channel(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "f")           # CH1 is on in the sim
            await pilot.pause()
            self.assertIn("channel 1 (Endcap A)", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_all_off_names_the_channels_it_will_reach(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("X")
            await pilot.pause()
            self.assertIn("CH1 Endcap A", self.screen_text())
            await pilot.press("escape")
            await pilot.pause()
            self.app.exit()

    async def test_the_adjust_plan_names_every_row(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("down", "s", "down", "s")   # channels 1 and 2
            await pilot.press("right", "right", "d")      # VSet
            await pilot.pause()
            self.app.screen.query_one("#step", Input).value = "50"
            await pilot.pause()
            self.assertIn("Endcap A", self.app.screen.query_one(
                "#plan", Static).render().plain)
            await pilot.press("escape")
            await pilot.pause()
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


class TheHelpScreen(PanelTest):
    """`?` explains the panel without leaving it."""

    async def test_question_mark_opens_it_from_the_picker(self):
        """The panel opens on the picker, so help has to work from there."""
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConnectScreen)
            await pilot.press("?")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConnectScreen)
            self.app.exit()

    async def test_it_opens_over_the_panel_and_closes_again(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("?")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertIn("Moving about", self.screen_text())
            # The rest is below the fold, so it is checked in the text itself.
            self.assertIn("Status flags", self.app.screen.body().plain)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(self.app.screen, HelpScreen)
            self.app.exit()

    async def test_reading_the_help_is_not_an_action(self):
        """The log records what was done to the supply.  Opening the help is
        not that, and a session's record should not fill up with it."""
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            before = self.log_text()
            await pilot.press("?")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.log_text(), before)
            self.app.exit()

    async def test_read_only_mode_says_so(self):
        async with self.app.run_test() as pilot:
            self.app.read_only = True
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()
            self.assertIn("READ-ONLY", self.screen_text())
            self.app.exit()


class WhatTheHelpSays(unittest.TestCase):
    """The text itself, checked without starting the app."""

    def rendered(self) -> list[str]:
        return HelpScreen("~/.config/hvctl/profiles.json").body().plain.split(
            "\n")

    def test_every_key_the_panel_answers_to_is_documented(self):
        """A key that switches high voltage and is described nowhere is the
        one that gets pressed by accident."""
        described = {k for _, rows in HELP_SECTIONS for row in rows
                     for k in row.keys}
        for binding in HVApp.BINDINGS:
            for key in binding.key.split(","):
                with self.subTest(key=key):
                    self.assertIn(key, described)

    def test_every_status_flag_is_explained(self):
        body = "\n".join(self.rendered())
        for _, flag in STATUS_BITS:
            with self.subTest(flag=flag):
                self.assertIn(flag, body)
                self.assertTrue(STATUS_HELP.get(flag))

    def test_nothing_is_wider_than_the_dialog(self):
        """Wrapped here rather than by the widget, so a line that runs over
        would be re-wrapped flush left and lose its key column."""
        for line in self.rendered():
            if "profiles.json" in line:
                continue        # one long word: it folds, or it does not
            self.assertLessEqual(len(line), HELP_WIDTH, line)


if __name__ == "__main__":
    unittest.main()
