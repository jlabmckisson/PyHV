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
from textual.widgets import DataTable, Input, Label            # noqa: E402

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

    async def settle(self, pilot, done, tries: int = 40):
        for _ in range(tries):
            await pilot.pause(0.1)
            if done():
                return
        self.fail("timed out waiting for the panel")


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

    async def connected(self, pilot):
        """Bring the panel up on the simulator."""
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await self.settle(pilot, lambda: self.app._rows_built)
        return self.app.worker

    async def test_switching_warns_while_channels_are_live(self):
        async with self.app.run_test() as pilot:
            await self.connected(pilot)
            await pilot.press("m")
            await pilot.pause()
            self.assertIn("Disconnect with channels live", self.screen_text())
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


if __name__ == "__main__":
    unittest.main()
