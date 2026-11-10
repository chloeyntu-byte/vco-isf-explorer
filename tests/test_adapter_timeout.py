"""Regression tests for complete subprocess-tree timeout cleanup."""

import unittest
from unittest import mock

from isf_tool.core import adapters
from isf_tool.core.errors import AdapterExecutionError


class AdapterTimeoutTests(unittest.TestCase):
    def test_timeout_terminates_then_kills_posix_process_group(self):
        process = mock.Mock()
        process.pid = 4321
        process.communicate.side_effect = [
            adapters.subprocess.TimeoutExpired(["fake"], 0.1),
            adapters.subprocess.TimeoutExpired(["fake"], 2.0),
            ("final stdout", "final stderr"),
        ]
        with mock.patch.object(
            adapters, "_supports_posix_process_groups", return_value=True
        ), mock.patch.object(
            adapters.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(adapters.os, "killpg") as killpg:
            with self.assertRaises(AdapterExecutionError) as raised:
                adapters._default_runner(["fake"], None, {}, 0.1)

        self.assertEqual("ISF-E4004", raised.exception.code)
        self.assertEqual(
            "process_group", raised.exception.details["termination_scope"]
        )
        self.assertEqual("killed", raised.exception.details["termination"])
        self.assertEqual(
            [
                mock.call(4321, adapters.signal.SIGTERM),
                mock.call(4321, adapters.signal.SIGKILL),
            ],
            killpg.call_args_list,
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_timeout_has_direct_process_fallback_off_posix(self):
        process = mock.Mock()
        process.communicate.side_effect = [
            adapters.subprocess.TimeoutExpired(["fake"], 0.1),
            adapters.subprocess.TimeoutExpired(["fake"], 2.0),
            ("", ""),
        ]
        with mock.patch.object(
            adapters, "_supports_posix_process_groups", return_value=False
        ), mock.patch.object(
            adapters.subprocess, "Popen", return_value=process
        ) as popen:
            with self.assertRaises(AdapterExecutionError) as raised:
                adapters._default_runner(["fake"], None, {}, 0.1)

        self.assertEqual("process", raised.exception.details["termination_scope"])
        self.assertEqual("killed", raised.exception.details["termination"])
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertNotIn("start_new_session", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
