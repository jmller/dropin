"""LaunchAgent plist generation.

The plist is the trigger contract: `QueueDirectories` fires on arrival and
`StartInterval` is the fallback. Nothing here runs `launchctl`.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

from dropin import launchd
from dropin.macos.fake import FakeMacOS
from dropin.macos.real import RealMacOS


class PlistTest(unittest.TestCase):
    def render(self, **overrides) -> dict:
        arguments = dict(label="dev.dropin.drain",
                         program=("python3", "-m", "dropin", "drain"),
                         queue_directories=("/Users/me/Drop",), interval=900)
        arguments.update(overrides)
        return plistlib.loads(launchd.render_plist(**arguments).encode())

    def test_keys_and_values(self):
        plist = self.render()
        self.assertEqual(plist["Label"], "dev.dropin.drain")
        self.assertEqual(plist["ProgramArguments"],
                         ["python3", "-m", "dropin", "drain"])
        self.assertEqual(plist["QueueDirectories"], ["/Users/me/Drop"])
        self.assertEqual(plist["StartInterval"], 900)
        self.assertIs(plist["RunAtLoad"], False)

    def test_special_characters_are_escaped_not_interpolated(self):
        odd = "/Users/me/Drop & <stuff> \"quoted\""
        plist = self.render(queue_directories=(odd,), program=("p", odd))
        self.assertEqual(plist["QueueDirectories"], [odd])
        self.assertEqual(plist["ProgramArguments"][1], odd)

    def test_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            launchd.render_plist(label="x", program=("p",),
                                 queue_directories=("/d",), interval=0)

    def test_label_must_be_reverse_dns_like(self):
        for bad in ("", "has space", "semi;colon", "/slash"):
            with self.subTest(label=bad), self.assertRaises(ValueError):
                launchd.render_plist(label=bad, program=("p",),
                                     queue_directories=("/d",), interval=1)


class ProgramTest(unittest.TestCase):
    def test_program_invokes_this_interpreter_with_the_config(self):
        program = launchd.program_arguments(Path("/etc/dropin.toml"))
        self.assertEqual(program[:3], (sys.executable, "-m", "dropin"))
        self.assertEqual(program[3:], ("--config", "/etc/dropin.toml", "drain"))

    def test_default_agent_path_lives_in_the_user_library(self):
        path = launchd.default_agent_path("dev.dropin.drain",
                                          home=Path("/Users/me"))
        self.assertEqual(path, Path("/Users/me/Library/LaunchAgents/"
                                    "dev.dropin.drain.plist"))

    def test_bootstrap_command_targets_the_gui_domain(self):
        line = launchd.bootstrap_command(Path("/Users/me/Library/LaunchAgents/"
                                              "dev.dropin.drain.plist"), uid=501)
        self.assertEqual(line, "launchctl bootstrap gui/501 "
                               "/Users/me/Library/LaunchAgents/dev.dropin.drain.plist")


class InstallTest(unittest.TestCase):
    def test_install_goes_through_the_seam(self):
        macos = FakeMacOS()
        agent = launchd.install(macos, config_path=Path("/c.toml"),
                                drop_dir=Path("/Drop"), label="dev.dropin.drain",
                                interval=600, agent_path=Path("/tmp/x.plist"))
        self.assertEqual(macos.launch_agents, [agent])
        self.assertEqual(agent.queue_directories, ("/Drop",))
        self.assertEqual(agent.interval, 600)
        self.assertEqual(agent.program[-1], "drain")
        self.assertIn("--config", agent.program)

    def test_real_adapter_writes_a_parseable_plist(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "nested" / "dev.dropin.drain.plist"
            RealMacOS(platform="linux").write_launch_agent(
                target, label="dev.dropin.drain", program=("p", "q"),
                queue_directories=("/Drop & Co",), interval=5)
            plist = plistlib.loads(target.read_bytes())
            self.assertEqual(plist["QueueDirectories"], ["/Drop & Co"])
            self.assertEqual(plist["ProgramArguments"], ["p", "q"])
            self.assertEqual(os.path.basename(target), "dev.dropin.drain.plist")


if __name__ == "__main__":
    unittest.main()
