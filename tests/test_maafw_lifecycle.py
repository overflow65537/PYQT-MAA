from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.runner.maafw import MaaFW


class _Pending:
    def __init__(self, *, succeeded: bool = True, on_wait=None):
        self.succeeded = succeeded
        self._on_wait = on_wait

    def wait(self):
        if self._on_wait is not None:
            self._on_wait()
        return self


class _Controller:
    def __init__(
        self, *, connected: bool = True, inactive_error: Exception | None = None
    ):
        self._connected = connected
        self._inactive_error = inactive_error
        self.sinks = []
        self.inactive_calls = 0

    def add_sink(self, sink):
        self.sinks.append(sink)

    def post_connection(self):
        return _Pending(succeeded=self._connected)

    def post_inactive(self):
        self.inactive_calls += 1
        if self._inactive_error is not None:
            raise self._inactive_error
        return _Pending()


class MaaFWLifecycleTests(unittest.TestCase):
    @staticmethod
    def _make_runtime() -> MaaFW:
        runtime = MaaFW.__new__(MaaFW)
        runtime._lifecycle_lock = threading.RLock()
        runtime._cleanup_condition = threading.Condition()
        runtime._cleanup_in_progress = False
        runtime.maa_controller_sink = object()
        runtime._embedded_controller_sinks = []
        runtime.controller = None
        runtime.tasker = None
        runtime.resource = None
        runtime.agents = []
        runtime.agent_threads = []
        runtime.agent_output_threads = []
        runtime.agent = None
        runtime.agent_thread = None
        runtime.agent_output_thread = None
        runtime.agent_data_raw = None
        runtime.agent_project_dir = None
        runtime.agent_env_vars = {}
        runtime.agent_connection_timeout_seconds = None
        runtime._last_agent_connect_timeout_seconds = None
        runtime._last_custom_root = None
        runtime._embedded_resource_sinks = []
        runtime._embedded_tasker_sinks = []
        runtime._embedded_context_sinks = []
        runtime._custom_sys_paths = []
        runtime._purge_modules_under_root = Mock()
        runtime._remove_custom_sys_paths = Mock()
        return runtime

    def test_cleanup_in_progress_is_reported_as_active(self):
        runtime = self._make_runtime()
        runtime._cleanup_in_progress = True

        self.assertTrue(runtime.has_active_runtime())

    def test_failed_connection_is_not_published(self):
        runtime = self._make_runtime()
        controller = _Controller(connected=False)

        self.assertFalse(runtime._connect_controller(controller, "failed"))

        self.assertIsNone(runtime.controller)
        self.assertEqual(1, controller.inactive_calls)
        self.assertEqual([runtime.maa_controller_sink], controller.sinks)

    def test_successful_connection_is_published(self):
        runtime = self._make_runtime()
        controller = _Controller()

        self.assertTrue(runtime._connect_controller(controller, "failed"))

        self.assertIs(controller, runtime.controller)
        self.assertEqual(0, controller.inactive_calls)

    def test_successful_connection_replaces_and_deactivates_previous_controller(self):
        runtime = self._make_runtime()
        previous = _Controller()
        controller = _Controller()
        runtime.controller = previous

        self.assertTrue(runtime._connect_controller(controller, "failed"))

        self.assertIs(controller, runtime.controller)
        self.assertEqual(1, previous.inactive_calls)

    def test_failed_previous_deactivation_keeps_previous_controller(self):
        runtime = self._make_runtime()
        previous = _Controller(inactive_error=RuntimeError("inactive failed"))
        controller = _Controller()
        runtime.controller = previous

        self.assertFalse(runtime._connect_controller(controller, "failed"))

        self.assertIs(previous, runtime.controller)
        self.assertEqual(1, previous.inactive_calls)
        self.assertEqual(1, controller.inactive_calls)

    def test_deactivate_controller_clears_current_controller(self):
        runtime = self._make_runtime()
        controller = _Controller()
        runtime.controller = controller

        runtime.deactivate_controller()

        self.assertIsNone(runtime.controller)
        self.assertEqual(1, controller.inactive_calls)

    def test_resource_cleanup_helpers_clear_current_resource(self):
        runtime = self._make_runtime()
        resource = SimpleNamespace(
            clear=Mock(),
            clear_custom_recognition=Mock(),
            clear_custom_action=Mock(),
        )
        runtime.resource = resource

        runtime.clear_resource_custom()
        runtime.clear_resource()

        resource.clear_custom_recognition.assert_called_once_with()
        resource.clear_custom_action.assert_called_once_with()
        resource.clear.assert_called_once_with()

    def test_teardown_agents_uses_lifecycle_operation(self):
        runtime = self._make_runtime()
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        teardown_done = threading.Event()

        def wait_for_stop():
            cleanup_started.set()
            self.assertTrue(allow_cleanup.wait(timeout=2))

        runtime.tasker = SimpleNamespace(
            post_stop=lambda: _Pending(on_wait=wait_for_stop),
            running=False,
            stopping=False,
        )
        runtime.controller = _Controller()
        runtime.resource = SimpleNamespace(clear=Mock())

        cleanup_thread = threading.Thread(target=runtime._cleanup_runtime)
        teardown_thread = threading.Thread(
            target=lambda: (runtime.teardown_agents(), teardown_done.set())
        )
        cleanup_thread.start()
        self.assertTrue(cleanup_started.wait(timeout=1))
        teardown_thread.start()
        time.sleep(0.05)
        self.assertFalse(teardown_done.is_set())

        allow_cleanup.set()
        cleanup_thread.join(timeout=2)
        teardown_thread.join(timeout=2)

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(teardown_thread.is_alive())
        self.assertTrue(teardown_done.is_set())

    def test_natural_finish_cleanup_skips_post_stop(self):
        runtime = self._make_runtime()
        post_stop_calls = []
        runtime.tasker = SimpleNamespace(
            post_stop=lambda: post_stop_calls.append(1) or _Pending(),
            running=False,
            stopping=False,
        )
        controller = _Controller()
        resource = SimpleNamespace(clear=Mock())
        runtime.controller = controller
        runtime.resource = resource
        runtime._teardown_agents = Mock()

        runtime._cleanup_runtime(post_stop=False)

        self.assertEqual([], post_stop_calls)
        self.assertIsNone(runtime.tasker)
        self.assertIsNone(runtime.controller)
        self.assertIsNone(runtime.resource)
        self.assertEqual(1, controller.inactive_calls)
        resource.clear.assert_called_once_with()
        runtime._teardown_agents.assert_called_once_with()

    def test_duplicate_cleanup_waits_and_does_not_clear_new_runtime(self):
        runtime = self._make_runtime()
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        second_cleanup_done = threading.Event()

        def wait_for_stop():
            cleanup_started.set()
            self.assertTrue(allow_cleanup.wait(timeout=2))

        runtime.tasker = SimpleNamespace(
            post_stop=lambda: _Pending(on_wait=wait_for_stop),
            running=False,
            stopping=False,
        )
        old_controller = _Controller()
        runtime.controller = old_controller
        old_resource = SimpleNamespace(clear=Mock())
        runtime.resource = old_resource

        first = threading.Thread(target=runtime._cleanup_runtime)
        second = threading.Thread(
            target=lambda: (runtime._cleanup_runtime(), second_cleanup_done.set())
        )
        first.start()
        self.assertTrue(cleanup_started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.assertFalse(second_cleanup_done.is_set())

        allow_cleanup.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_cleanup_done.is_set())
        self.assertIsNone(runtime.tasker)
        self.assertIsNone(runtime.controller)
        self.assertIsNone(runtime.resource)
        self.assertEqual(1, old_controller.inactive_calls)
        old_resource.clear.assert_called_once_with()

    def test_start_after_cleanup_request_waits_for_cleanup_completion(self):
        runtime = self._make_runtime()
        cleanup_requested = threading.Event()
        allow_cleanup = threading.Event()
        connection_done = threading.Event()

        runtime._lifecycle_lock.acquire()
        cleanup_thread = threading.Thread(target=runtime._cleanup_runtime)
        cleanup_thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with runtime._cleanup_condition:
                if runtime._cleanup_in_progress:
                    cleanup_requested.set()
                    break
            time.sleep(0.01)
        self.assertTrue(cleanup_requested.is_set())

        replacement = _Controller()
        connect_thread = threading.Thread(
            target=lambda: (
                runtime._connect_controller(replacement, "failed"),
                connection_done.set(),
            )
        )
        connect_thread.start()
        time.sleep(0.05)
        self.assertFalse(connection_done.is_set())

        runtime._lifecycle_lock.release()
        cleanup_thread.join(timeout=2)
        connect_thread.join(timeout=2)

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(connect_thread.is_alive())
        self.assertTrue(connection_done.is_set())
        self.assertIs(replacement, runtime.controller)

    def test_connection_waits_for_cleanup_before_publishing(self):
        runtime = self._make_runtime()
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        connection_done = threading.Event()

        def wait_for_stop():
            cleanup_started.set()
            self.assertTrue(allow_cleanup.wait(timeout=2))

        runtime.tasker = SimpleNamespace(
            post_stop=lambda: _Pending(on_wait=wait_for_stop),
            running=False,
            stopping=False,
        )
        runtime.controller = _Controller()
        runtime.resource = SimpleNamespace(clear=Mock())
        replacement = _Controller()

        cleanup_thread = threading.Thread(target=runtime._cleanup_runtime)
        connect_thread = threading.Thread(
            target=lambda: (
                runtime._connect_controller(replacement, "failed"),
                connection_done.set(),
            )
        )
        cleanup_thread.start()
        self.assertTrue(cleanup_started.wait(timeout=1))
        connect_thread.start()
        time.sleep(0.05)
        self.assertFalse(connection_done.is_set())
        self.assertIsNot(replacement, runtime.controller)

        allow_cleanup.set()
        cleanup_thread.join(timeout=2)
        connect_thread.join(timeout=2)

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(connect_thread.is_alive())
        self.assertTrue(connection_done.is_set())
        self.assertIs(replacement, runtime.controller)


if __name__ == "__main__":
    unittest.main()
