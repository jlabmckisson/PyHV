"""Connection profiles and the command line that reads them.

hvctl takes no host argument: modules are named once, saved, and picked by
name afterwards.  These cover the store itself and the argument handling
that decides which module a subcommand talks to.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hv                                                   # noqa: E402
from hvprofiles import (DEFAULT_BAUD, DEFAULT_PORT, Profile,  # noqa: E402
                        ProfileStore, clean_baud, clean_device, clean_host,
                        clean_name, clean_port, looks_like_device)


class ProfileFile(unittest.TestCase):
    """The store on disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "profiles.json")
        os.environ["HVCTL_PROFILES"] = self.path

    def on_disk(self) -> dict:
        with open(self.path) as fh:
            return json.load(fh)

    def seeded(self) -> ProfileStore:
        s = ProfileStore()
        s.upsert(Profile("Lab rack A", "192.168.0.250", 1470))
        s.upsert(Profile("Test bench", "10.0.0.7", 1471))
        s.last_used = "Test bench"
        s.save()
        return s

    def test_missing_file_is_an_empty_store(self):
        s = ProfileStore()
        self.assertEqual(s.profiles, [])
        self.assertEqual(s.load_error, "")

    def test_round_trip(self):
        self.seeded()
        s = ProfileStore()
        self.assertEqual(s.names(), ["Lab rack A", "Test bench"])
        self.assertEqual(s.last_used, "Test bench")
        self.assertEqual(s.get("lab rack a").host, "192.168.0.250")
        self.assertEqual(s.get("Test bench").port, 1471)

    def test_simulator_is_offered_but_never_written(self):
        self.seeded()
        self.assertTrue(ProfileStore().get("Simulator").is_sim)
        saved = self.on_disk()["profiles"]
        self.assertNotIn("Simulator", [p["name"] for p in saved])

    def test_rename_keeps_its_place(self):
        s = self.seeded()
        s.upsert(Profile("Bench 2", "10.0.0.7", 1471), old_name="Test bench")
        self.assertEqual(s.names(), ["Lab rack A", "Bench 2"])
        self.assertEqual(s.last_used, "Bench 2")

    def test_delete_forgets_last_used(self):
        s = self.seeded()
        s.delete("Test bench")
        self.assertEqual(s.names(), ["Lab rack A"])
        self.assertEqual(s.last_used, "")

    def test_writes_are_atomic(self):
        self.seeded()
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_unreadable_file_is_reported_not_ignored(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        s = ProfileStore()
        self.assertIn("not valid profile JSON", s.load_error)
        self.assertEqual(s.profiles, [])

    def test_unreadable_file_is_kept_when_overwritten(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        s = ProfileStore()
        s.upsert(Profile("New", "1.2.3.4"))
        s.save()
        self.assertTrue(os.path.exists(self.path + ".bad"))
        self.assertEqual(self.on_disk()["profiles"][0]["name"], "New")


class FieldValidation(unittest.TestCase):
    """What the profile dialog will and will not accept."""

    def test_name_is_required(self):
        with self.assertRaises(ValueError):
            clean_name("   ")

    def test_simulator_name_is_reserved(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            clean_name("simulator")

    def test_duplicate_names_are_refused_case_insensitively(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            clean_name("lab rack a", ["Lab rack A"])

    def test_whitespace_is_collapsed(self):
        self.assertEqual(clean_name("  Lab   rack  A "), "Lab rack A")

    def test_host_may_carry_a_port(self):
        self.assertEqual(clean_host("192.168.0.9:1480"), ("192.168.0.9", 1480))

    def test_bare_host_has_no_port(self):
        self.assertEqual(clean_host("hv-rack.lab"), ("hv-rack.lab", None))

    def test_an_address_is_required(self):
        with self.assertRaisesRegex(ValueError, "address required"):
            clean_host("")

    def test_nonsense_host_is_refused(self):
        with self.assertRaises(ValueError):
            clean_host("not a host!")

    def test_empty_port_is_the_default(self):
        self.assertEqual(clean_port(""), DEFAULT_PORT)

    def test_port_range(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            clean_port("99999")


class SerialProfiles(unittest.TestCase):
    """USB modules are named the same way, by their port instead of an IP."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "profiles.json")
        os.environ["HVCTL_PROFILES"] = self.path

    def test_device_paths_are_told_from_hostnames(self):
        for text in ("/dev/ttyACM0", "/dev/tty.usbmodem1101", "COM3", "com12"):
            self.assertTrue(looks_like_device(text), text)
        for text in ("192.168.0.250", "hv-rack.lab", "10.0.0.7:1471", ""):
            self.assertFalse(looks_like_device(text), text)

    def test_a_serial_profile_round_trips(self):
        store = ProfileStore()
        store.upsert(Profile("Bench USB", kind="serial",
                             device="/dev/ttyACM0", baud=9600))
        store.save()
        back = ProfileStore().get("Bench USB")
        self.assertTrue(back.is_serial)
        self.assertEqual(back.device, "/dev/ttyACM0")
        self.assertEqual(back.baud, 9600)

    def test_only_the_relevant_fields_are_written(self):
        """The file is meant to be hand-edited; do not litter it."""
        store = ProfileStore()
        store.upsert(Profile("Bench USB", kind="serial", device="/dev/ttyACM0"))
        store.upsert(Profile("Rack", "192.168.0.250", 1470))
        store.save()
        with open(self.path) as fh:
            saved = json.load(fh)["profiles"]
        self.assertEqual(set(saved[0]), {"name", "kind", "device", "baud"})
        self.assertEqual(set(saved[1]), {"name", "kind", "host", "port"})

    def test_the_address_reads_sensibly_in_the_picker(self):
        self.assertEqual(
            Profile("x", kind="serial", device="/dev/ttyACM0", baud=9600).address,
            "/dev/ttyACM0 @9600")
        self.assertEqual(Profile("x", "10.0.0.7", 1471).address, "10.0.0.7:1471")

    def test_an_unplugged_port_is_still_saveable(self):
        """Hardware comes and goes; a profile for it should not."""
        self.assertEqual(clean_device("/dev/ttyACM9"), "/dev/ttyACM9")

    def test_a_hostname_is_not_a_serial_port(self):
        with self.assertRaisesRegex(ValueError, "not a serial port"):
            clean_device("192.168.0.250")

    def test_baud_defaults_and_range(self):
        self.assertEqual(clean_baud(""), DEFAULT_BAUD)
        self.assertEqual(clean_baud(" 115200 "), 115200)
        with self.assertRaisesRegex(ValueError, "out of range"):
            clean_baud("3")
        with self.assertRaisesRegex(ValueError, "not a baud rate"):
            clean_baud("fast")

    def test_the_transport_matches_the_kind(self):
        """Without pyserial this still has to fail for the right reason."""
        prof = Profile("Bench USB", kind="serial", device="/dev/ttyACM0")
        try:
            hv.transport_for_profile(prof)
        except hv.HVError as e:
            self.assertIn("pyserial", str(e))
        except OSError:
            pass            # pyserial present, no such device -- also correct
        else:
            self.fail("expected the missing device to be reported")


class ArgumentDefaults(unittest.TestCase):
    """No arguments means the panel."""

    def setUp(self):
        self.parser = hv.build_parser()

    def test_bare_invocation_opens_the_tui(self):
        args = self.parser.parse_args([])
        self.assertEqual(args.cmd, "tui")
        self.assertEqual(args.interval, 1.0)
        self.assertFalse(args.read_only)
        self.assertIsNone(args.profile)

    def test_a_subcommand_keeps_its_own_defaults(self):
        """monitor polls every 2s; the parent default must not win."""
        self.assertEqual(self.parser.parse_args(["monitor"]).interval, 2.0)

    def test_tui_flags_still_parse(self):
        args = self.parser.parse_args(["tui", "-n", "0.5", "--read-only"])
        self.assertEqual(args.interval, 0.5)
        self.assertTrue(args.read_only)

    def test_other_subcommands_are_unaffected(self):
        args = self.parser.parse_args(["--sim", "status"])
        self.assertEqual(args.cmd, "status")
        self.assertTrue(args.sim)


class CommandLineConnection(unittest.TestCase):
    """Which module a subcommand ends up talking to."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["HVCTL_PROFILES"] = os.path.join(self.dir, "profiles.json")
        s = ProfileStore()
        s.upsert(Profile("Lab rack A", "192.168.0.250", 1470))
        s.upsert(Profile("Test bench", "127.0.0.1", 9))   # discard port
        s.last_used = "Test bench"
        s.save()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hv.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_simulator_needs_no_profile(self):
        code, out, _ = self.run_cli(["--sim", "status"])
        self.assertEqual(code, 0)
        self.assertIn("VMon", out)

    def test_profiles_are_listed(self):
        code, out, _ = self.run_cli(["profiles"])
        self.assertEqual(code, 0)
        self.assertIn("Lab rack A", out)
        self.assertIn("last used", out)

    def test_unknown_profile_is_an_error(self):
        code, _, err = self.run_cli(["--profile", "nope", "status"])
        self.assertEqual(code, 2)
        self.assertIn("no profile named", err)

    def test_the_last_module_used_is_the_default(self):
        """Nothing named on the command line: fall back, and say so."""
        code, _, err = self.run_cli(["status"])
        self.assertEqual(code, 1)                 # 127.0.0.1:9 refuses
        self.assertIn("using profile Test bench", err)
        self.assertIn("connection failed", err)

    def test_no_profiles_at_all_explains_itself(self):
        os.environ["HVCTL_PROFILES"] = os.path.join(self.dir, "none.json")
        code, _, err = self.run_cli(["status"])
        self.assertEqual(code, 2)
        self.assertIn("run `hvctl` with no arguments", err)


if __name__ == "__main__":
    unittest.main()
