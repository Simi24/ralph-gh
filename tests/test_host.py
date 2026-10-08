import http.server
import os
import stat
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from conductor.host import HostEnvironment
from tests.gitrepo import TempRepo


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        code = {"/ok": 200, "/moved": 302, "/down": 503}[self.path]
        self.send_response(code)
        self.send_header("Location", "/ok")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def serve() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class HostEnvironmentTest(unittest.TestCase):
    def test_health_probe(self) -> None:
        env = HostEnvironment(Path("."))
        with serve() as base:
            self.assertIsNone(env.probe_health(f"{base}/ok"))
            self.assertIsNone(env.probe_health(f"{base}/moved"))  # 3xx is not followed, counts as up
            self.assertIsNotNone(env.probe_health(f"{base}/down"))
        self.assertIsNotNone(env.probe_health(f"{base}/ok"))  # server stopped
        self.assertIsNotNone(env.probe_health("not a url"))

    def test_git_status_and_shell(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        env = HostEnvironment(repo.checkout)
        self.assertEqual(env.git_status(), [])
        (repo.checkout / "new.txt").write_text("x")
        self.assertEqual(env.git_status(), ["?? new.txt"])
        self.assertEqual(env.run_shell("test -f new.txt"), 0)
        self.assertNotEqual(env.run_shell("test -f missing.txt"), 0)
        self.assertTrue(env.fetch_base("main"))
        self.assertFalse(env.fetch_base("no-such-branch"))

    def stub_gh(self, script: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stub = Path(tmp.name) / "gh"
        stub.write_text(f"#!/bin/sh\n{script}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        patch = mock.patch.dict(os.environ, {"PATH": f"{tmp.name}:{os.environ['PATH']}"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_labels_unreadable_are_none_not_empty(self) -> None:
        self.stub_gh("echo boom >&2; exit 1")
        env = HostEnvironment(Path("."))
        self.assertIsNone(env.existing_labels())
        self.assertFalse(env.create_label("ralph:x", "000000", "d"))

    def test_labels_are_read_and_an_already_existing_label_counts_as_created(self) -> None:
        self.stub_gh('if [ "$2" = list ]; then printf "a\\nb\\n"; else echo "label already exists" >&2; exit 1; fi')
        env = HostEnvironment(Path("."))
        self.assertEqual(env.existing_labels(), {"a", "b", ""})
        self.assertTrue(env.create_label("a", "000000", "d"))

    def test_agent_roots_include_user_and_repo(self) -> None:
        roots = HostEnvironment(Path("/repo")).agent_roots()
        self.assertEqual(roots[-1], Path("/repo/.claude/agents"))
        self.assertEqual(roots[0].name, "agents")


if __name__ == "__main__":
    unittest.main()
