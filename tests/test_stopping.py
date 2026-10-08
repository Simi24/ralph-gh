"""The stop request itself: its state, the STOP file and the signal handlers.
What a stop does to sessions, tickets and the board is tested through run()
in test_stop_resume.py, and process-group kills in test_claude_agents.py."""
import signal
import tempfile
import unittest
from pathlib import Path

from conductor.signals import EXIT_SIGHUP, install_signal_handlers
from conductor.stopping import REASON_DRAIN, REASON_IMMEDIATE, StopState, write_stop_file


class StopStateTest(unittest.TestCase):
    def test_nothing_requested_means_no_result(self) -> None:
        stop = StopState()
        self.assertFalse(stop.draining)
        self.assertIsNone(stop.result())

    def test_a_drain_is_a_clean_stop_and_the_first_reason_wins(self) -> None:
        stop = StopState()
        stop.request_drain("usage limit", 3)
        stop.request_drain()
        self.assertTrue(stop.draining)
        self.assertFalse(stop.immediate)
        self.assertEqual((stop.result().exit_code, stop.result().reason), (3, "usage limit"))

    def test_the_first_sigint_drains_and_the_second_stops_immediately(self) -> None:
        stop = StopState()
        stop.escalate()
        self.assertEqual((stop.draining, stop.immediate, stop.result().reason), (True, False, REASON_DRAIN))
        stop.escalate()
        self.assertEqual((stop.immediate, stop.result().reason), (True, REASON_IMMEDIATE))

    def test_the_flag_is_set_before_any_session_is_killed(self) -> None:
        stop = StopState()
        seen: list[bool] = []
        stop.sessions.kill_all = lambda: seen.append(stop.immediate)  # type: ignore[method-assign]
        stop.request_immediate()
        self.assertEqual(seen, [True])

    def test_sessions_are_killed_once(self) -> None:
        stop = StopState()
        calls: list[int] = []
        stop.sessions.kill_all = lambda: calls.append(1)  # type: ignore[method-assign]
        stop.request_immediate()
        stop.request_immediate()
        self.assertEqual(calls, [1])


class StopFileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "state"

    def test_the_file_drains_once_and_is_consumed(self) -> None:
        stop = StopState(self.root / "STOP")
        stop.poll_file()
        self.assertFalse(stop.draining)
        write_stop_file(self.root)  # what `ralph-gh stop` does
        stop.poll_file()
        self.assertEqual(stop.result().reason, REASON_DRAIN)
        self.assertFalse((self.root / "STOP").exists())

    def test_a_stale_file_is_removed_at_startup(self) -> None:
        write_stop_file(self.root)
        stop = StopState(self.root / "STOP")
        stop.clear_stale_file()
        stop.poll_file()
        self.assertFalse(stop.draining)

    def test_without_a_file_path_nothing_happens(self) -> None:
        stop = StopState()
        stop.poll_file()
        stop.clear_stale_file()
        self.assertFalse(stop.draining)


class SignalHandlersTest(unittest.TestCase):
    def installed(self, stop: StopState):
        restore = install_signal_handlers(stop)
        self.addCleanup(restore)
        return lambda signum: signal.getsignal(signum)(signum, None)

    def test_sigint_twice_and_sigterm(self) -> None:
        stop = StopState()
        deliver = self.installed(stop)
        deliver(signal.SIGINT)
        self.assertEqual((stop.draining, stop.immediate), (True, False))
        deliver(signal.SIGINT)
        self.assertTrue(stop.immediate)
        term = StopState()
        self.installed(term)(signal.SIGTERM)
        self.assertEqual((term.immediate, term.result().reason), (True, REASON_IMMEDIATE))

    def test_sighup_is_an_immediate_stop_with_exit_129(self) -> None:
        stop = StopState()
        self.installed(stop)(signal.SIGHUP)
        self.assertEqual((stop.result().exit_code, stop.result().reason), (EXIT_SIGHUP, "killed (SIGHUP)"))

    def test_the_previous_handlers_are_restored(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        install_signal_handlers(StopState())()
        self.assertIs(signal.getsignal(signal.SIGINT), before)


if __name__ == "__main__":
    unittest.main()
