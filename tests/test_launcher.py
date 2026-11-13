"""Launcher tests: it is the process that holds Full Disk Access, so its contract matters.

    python3 -m unittest tests.test_launcher -v

Compiles launcher/launcher.c and drives it with small shell scripts. Skipped without a C compiler.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "launcher" / "launcher.c"


@unittest.skipUnless(shutil.which("cc"), "no C compiler")
class LauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dh-launcher-")).resolve()
        cls.bin = cls.tmp / "DiskHealth"
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.bin), str(SRC)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def script(self, name: str, body: str) -> Path:
        p = self.tmp / name
        p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        p.chmod(0o755)
        return p

    def run_it(self, *args, **kw):
        return subprocess.run([str(self.bin), *map(str, args)], capture_output=True, text=True, timeout=30, **kw)

    # ---------------------------------------------------------------- argument checking
    def test_no_arguments(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 64)
        self.assertIn("usage:", r.stderr)

    def test_relative_path_refused(self):
        r = self.run_it("sh")
        self.assertEqual(r.returncode, 78)
        self.assertIn("not an absolute path", r.stderr)

    def test_missing_program(self):
        r = self.run_it(self.tmp / "nope")
        self.assertEqual(r.returncode, 78)
        self.assertIn("cannot stat", r.stderr)

    def test_directory_refused(self):
        r = self.run_it(self.tmp)
        self.assertEqual(r.returncode, 78)
        self.assertIn("not a regular file", r.stderr)

    def test_non_executable_refused(self):
        p = self.tmp / "plain.txt"
        p.write_text("hello")
        p.chmod(0o644)
        r = self.run_it(p)
        self.assertEqual(r.returncode, 78)
        self.assertIn("not executable", r.stderr)

    # ---------------------------------------------------------------- pass-through
    def test_arguments_and_exit_code(self):
        s = self.script("echo_args.sh", '''
            echo "$@"
            exit 7
        ''')
        r = self.run_it(s, "-m", "diskhealth", "serve")
        self.assertEqual(r.stdout.strip(), "-m diskhealth serve")
        self.assertEqual(r.returncode, 7)

    def test_reports_death_by_signal(self):
        s = self.script("suicide.sh", '''
            kill -TERM $$
            sleep 5
        ''')
        r = self.run_it(s)
        self.assertEqual(r.returncode, 128 + signal.SIGTERM)

    # ---------------------------------------------------------------- environment
    def test_dyld_overrides_are_scrubbed_from_the_child(self):
        """The child must not inherit dynamic-linker overrides.

        DYLD_INSERT_LIBRARIES is deliberately not used here: dyld acts on it before main()
        runs, so it hits the launcher itself and no code of ours can stop that without a
        Developer ID signature. What the launcher does guarantee is that the child - Python,
        and every tool Python runs with Full Disk Access - starts without them.
        """
        s = self.script("show_env.sh", '''
            echo "FALLBACK=${DYLD_FALLBACK_LIBRARY_PATH-unset}"
            echo "FRAMEWORK=${DYLD_FRAMEWORK_PATH-unset}"
            echo "PRELOAD=${LD_PRELOAD-unset}"
            echo "KEPT=${DISK_HEALTH_DATA-unset}"
        ''')
        env = dict(os.environ, DYLD_FALLBACK_LIBRARY_PATH="/tmp", DYLD_FRAMEWORK_PATH="/tmp",
                   LD_PRELOAD="/tmp/evil.so", DISK_HEALTH_DATA="/tmp/data")
        r = self.run_it(s, env=env)
        self.assertIn("FALLBACK=unset", r.stdout)
        self.assertIn("FRAMEWORK=unset", r.stdout)
        self.assertIn("PRELOAD=unset", r.stdout)
        self.assertIn("KEPT=/tmp/data", r.stdout)   # unrelated variables survive

    def test_child_starts_with_no_blocked_signals(self):
        """The launcher blocks SIGTERM across the spawn; the child must not inherit that."""
        s = self.script("mask.sh", '''
            python3 -c "import signal; print('BLOCKED=%s' % sorted(signal.pthread_sigmask(signal.SIG_BLOCK, [])))"
        ''')
        r = self.run_it(s)
        self.assertIn("BLOCKED=[]", r.stdout)

    # ---------------------------------------------------------------- signals
    def test_sigterm_is_forwarded_to_the_child(self):
        marker = self.tmp / "got-term"
        s = self.script("trap.sh", f'''
            trap 'echo caught > "{marker}"; exit 0' TERM
            echo ready
            while true; do sleep 0.1; done
        ''')
        p = subprocess.Popen([str(self.bin), str(s)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 0)
        self.assertTrue(marker.exists(), "child never received SIGTERM")

    def _race_probe(self, binary: Path, tag: str, rounds: int = 8) -> int:
        """SIGTERM `binary` while it sits in its widened spawn window.

        Returns how many rounds the signal had no effect: the launcher still running, or its
        child still running, 3 s later. A lost signal does not orphan the child immediately -
        the launcher stays blocked in waitpid() on a child that was never told to stop, so
        neither process reacts to SIGTERM at all until launchd escalates to SIGKILL.
        """
        script = self.tmp / f"{tag}.sh"
        script.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nwhile true; do sleep 0.1; done\n")
        script.chmod(0o755)
        swallowed = 0
        try:
            for _ in range(rounds):
                p = subprocess.Popen([str(binary), str(script)])
                time.sleep(0.025)                     # inside the 50 ms hook window
                p.send_signal(signal.SIGTERM)
                deadline = time.time() + 3
                while time.time() < deadline and (p.poll() is None or self._alive(tag)):
                    time.sleep(0.02)
                if p.poll() is None or self._alive(tag):
                    swallowed += 1
                    p.kill()
                    p.wait(timeout=10)
                    subprocess.run(["pkill", "-f", tag], capture_output=True)
                    time.sleep(0.05)
        finally:
            subprocess.run(["pkill", "-f", tag], capture_output=True)
        return swallowed

    def test_sigterm_in_the_spawn_window_does_not_orphan_the_child(self):
        """The signal must not be lost between posix_spawn() and child_pid being set.

        The handler is installed before the spawn, but the pid is only known after it: a signal
        landing in between reaches a handler that still sees child_pid == 0, so nothing is
        forwarded. The launcher then blocks in waitpid() on a child that was never asked to
        stop, and `launchctl stop` / a logout hangs until launchd escalates to SIGKILL - which
        leaves the service orphaned. Measured, not assumed: the unblocked control below sits
        there for the full timeout on every round.

        Both binaries are built with DH_SPAWN_RACE_DELAY_US so the window is reproducible. The
        second one has the sigprocmask stripped out, which is the only thing that makes this
        test fail - without it the test would pass on broken code and prove nothing.
        """
        hook = ["-DDH_SPAWN_RACE_DELAY_US=50000"]
        fixed = self.tmp / "DiskHealth-window"
        subprocess.run(["cc", "-O2", "-o", str(fixed), str(SRC), *hook], check=True)

        unfixed_src = self.tmp / "launcher_unblocked.c"
        src = SRC.read_text().replace("sigprocmask(SIG_BLOCK, &block, &previous);",
                                      "(void)block; (void)previous;")
        self.assertNotEqual(src, SRC.read_text(), "could not build the unblocked variant")
        unfixed_src.write_text(src)
        unfixed = self.tmp / "DiskHealth-unblocked"
        subprocess.run(["cc", "-O2", "-o", str(unfixed), str(unfixed_src), *hook], check=True)

        self.assertGreater(self._race_probe(unfixed, "dh-race-unblocked"), 0,
                           "the probe never hit the window: this test would not catch a regression")
        self.assertEqual(self._race_probe(fixed, "dh-race-fixed"), 0,
                         "SIGTERM was swallowed: neither the launcher nor the service stopped")

    @staticmethod
    def _alive(tag: str) -> bool:
        return subprocess.run(["pgrep", "-f", tag], capture_output=True).returncode == 0


if __name__ == "__main__":
    unittest.main()
