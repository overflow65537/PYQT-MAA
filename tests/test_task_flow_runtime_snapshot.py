from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PySide6.QtCore import QCoreApplication

from app.common.constants import _CONTROLLER_, _RESOURCE_
from app.core.item import ConfigItem, RunnerEvents, TaskItem
from app.core.runner.task_flow import TaskFlowExecutionError, TaskFlowRunner
from app.core.service.i18n_service import I18nService
from app.core.service.interface_manager import InterfaceManager


class _Signal:
    def __init__(self):
        self.connect = Mock()


class _ConfigService:
    def __init__(self, configs: dict[str, ConfigItem], bundle_paths: dict[str, str]):
        self.current_config_id = "config-a"
        self.configs = configs
        self.bundle_paths = bundle_paths
        self.update_calls: list[tuple[str, ConfigItem]] = []

    def get_config(self, config_id: str):
        return self.configs.get(config_id)

    def get_bundle_path_for_config(self, config: ConfigItem) -> str:
        return self.bundle_paths[config.item_id]

    def update_config(self, config_id: str, config: ConfigItem) -> bool:
        self.configs[config_id] = config
        self.update_calls.append((config_id, config))
        return True


class TaskFlowRuntimeSnapshotTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.bundle_a = root / "bundle-a"
        self.bundle_b = root / "bundle-b"
        self.bundle_a.mkdir()
        self.bundle_b.mkdir()
        self.interface_a = {
            "name": "Bundle A",
            "version": "1.0.0",
            "controller": [{"name": "CtrlA", "type": "adb"}],
            "resource": [{"name": "ResA"}],
            "task": [
                {
                    "name": "Task A",
                    "entry": "EntryA",
                    "pipeline_override": {"Base": {"action": "ActionA"}},
                },
                {"name": "Other A", "entry": "OtherEntryA"},
            ],
            "option": {},
        }
        self.interface_b = {
            "name": "Bundle B",
            "version": "2.0.0",
            "controller": [{"name": "CtrlB", "type": "win32"}],
            "resource": [{"name": "ResB"}],
            "task": [{"name": "Task B", "entry": "EntryB"}],
            "option": {},
        }
        (self.bundle_a / "interface.json").write_text(
            json.dumps(self.interface_a, ensure_ascii=False), encoding="utf-8"
        )
        (self.bundle_b / "interface.json").write_text(
            json.dumps(self.interface_b, ensure_ascii=False), encoding="utf-8"
        )

        self.config_a = ConfigItem(
            name="A",
            item_id="config-a",
            bundle="bundle-a",
            know_task=[],
            tasks=[
                TaskItem(
                    name="Controller",
                    item_id=_CONTROLLER_,
                    is_checked=True,
                    task_option={"controller_type": {"value": "CtrlA"}},
                ),
                TaskItem(
                    name="Resource",
                    item_id=_RESOURCE_,
                    is_checked=True,
                    task_option={
                        "resource": {"value": "ResA"},
                        "setting_options": {"quality": {"value": "A"}},
                    },
                ),
                TaskItem(
                    name="Task A",
                    item_id="task-a",
                    is_checked=True,
                    task_option={},
                ),
                TaskItem(
                    name="Other A",
                    item_id="other-a",
                    is_checked=True,
                    task_option={"value": "initial"},
                ),
            ],
        )
        self.config_b = ConfigItem(
            name="B",
            item_id="config-b",
            bundle="bundle-b",
            know_task=[],
            tasks=[
                TaskItem(
                    name="Controller",
                    item_id=_CONTROLLER_,
                    is_checked=True,
                    task_option={"controller_type": {"value": "CtrlB"}},
                ),
                TaskItem(
                    name="Resource",
                    item_id=_RESOURCE_,
                    is_checked=True,
                    task_option={
                        "resource": {"value": "ResB"},
                        "setting_options": {"quality": {"value": "B"}},
                    },
                ),
                TaskItem(
                    name="Task B",
                    item_id="task-b",
                    is_checked=True,
                    task_option={},
                ),
            ],
        )
        self.config_service = _ConfigService(
            {"config-a": self.config_a, "config-b": self.config_b},
            {
                "config-a": str(self.bundle_a),
                "config-b": str(self.bundle_b),
            },
        )
        source_i18n = I18nService(language="zh_cn")
        source_i18n.set_translations("zh_cn", {"hello": "A translation"})
        self.task_service = SimpleNamespace(
            interface=deepcopy(self.interface_a),
            builtin_task_loader=SimpleNamespace(i18n_service=source_i18n),
            get_tasks=Mock(return_value=[]),
            update_task=Mock(return_value=True),
        )
        maafw = SimpleNamespace(
            callback=_Signal(),
            custom_info=_Signal(),
            agent_info=_Signal(),
        )
        self.runner = TaskFlowRunner(
            task_service=self.task_service,
            config_service=self.config_service,
            runner_events=RunnerEvents(),
            config_id="config-a",
            maafw=maafw,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_snapshot_remains_bound_to_config_a_after_ui_switch(self):
        self.assertTrue(self.runner._prepare_runtime_snapshot())
        self.runner._reload_interface_for_current_bundle()

        self.config_service.current_config_id = "config-b"

        self.assertEqual("task-a", self.runner._get_task("task-a").item_id)
        self.assertIsNone(self.runner._get_task("task-b"))
        self.assertEqual(str(self.bundle_a), self.runner.bundle_path)
        self.assertEqual(
            {"quality": {"value": "A"}},
            self.runner._runtime_setting_options,
        )
        self.assertEqual("Bundle A", self.runner._runtime_interface["name"])
        execution = self.runner._get_task_execution_info("task-a")
        self.assertEqual("EntryA", execution["entry"])
        self.assertEqual(
            {"Base": {"action": "ActionA"}}, execution["pipeline_override"]
        )

    def test_ui_edits_do_not_mutate_snapshot_and_persistence_merges_latest_config(self):
        self.assertTrue(self.runner._prepare_runtime_snapshot())
        snapshot_task = self.runner._get_task("task-a")

        live_a = self.config_service.configs["config-a"]
        next(task for task in live_a.tasks if task.item_id == "task-a").task_option[
            "ui_edit"
        ] = True
        next(task for task in live_a.tasks if task.item_id == "other-a").task_option[
            "value"
        ] = "edited while running"
        self.config_service.current_config_id = "config-b"

        self.assertNotIn("ui_edit", snapshot_task.task_option)

        runtime_update = deepcopy(snapshot_task)
        runtime_update.task_option["runtime_update"] = True
        self.assertTrue(self.runner._persist_runtime_task_update(runtime_update))

        persisted_a = self.config_service.configs["config-a"]
        persisted_b = self.config_service.configs["config-b"]
        self.assertTrue(
            next(task for task in persisted_a.tasks if task.item_id == "task-a")
            .task_option["runtime_update"]
        )
        self.assertEqual(
            "edited while running",
            next(task for task in persisted_a.tasks if task.item_id == "other-a")
            .task_option["value"],
        )
        self.assertEqual("Task B", persisted_b.tasks[-1].name)
        self.assertEqual("config-a", self.config_service.update_calls[-1][0])

    async def test_snapshot_failure_is_reported_after_cleanup(self):
        self.runner._prepare_runtime_snapshot = Mock(return_value=False)
        self.runner.stop_task = AsyncMock()
        telemetry = []
        self.runner.runner_events.telemetry.connect(telemetry.append)

        with (
            patch("app.core.runner.task_flow.send_notice"),
            patch(
                "app.core.runner.task_flow.emit_holiday_startup_logs",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaises(TaskFlowExecutionError):
                await self.runner.run_tasks_flow()

        self.runner.stop_task.assert_awaited_once_with(post_stop=False)
        self.assertFalse(self.runner._is_running)
        self.assertEqual("run_failed", telemetry[-1]["event"])

    async def test_snapshot_exception_is_reported_after_cleanup(self):
        self.runner._prepare_runtime_snapshot = Mock(
            side_effect=RuntimeError("snapshot failed")
        )
        self.runner.stop_task = AsyncMock()
        telemetry = []
        self.runner.runner_events.telemetry.connect(telemetry.append)

        with (
            patch("app.core.runner.task_flow.send_notice"),
            patch(
                "app.core.runner.task_flow.emit_holiday_startup_logs",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                await self.runner.run_tasks_flow()

        self.runner.stop_task.assert_awaited_once_with(post_stop=False)
        self.assertFalse(self.runner._is_running)
        self.assertEqual("run_failed", telemetry[-1]["event"])
        self.assertEqual("snapshot failed", telemetry[-1].get("error"))

    async def test_cleanup_failure_sets_final_telemetry_to_failed(self):
        self.runner.need_stop = True
        self.runner.stop_task = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        telemetry = []
        self.runner.runner_events.telemetry.connect(telemetry.append)

        with (
            patch("app.core.runner.task_flow.send_notice"),
            patch(
                "app.core.runner.task_flow.emit_holiday_startup_logs",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await self.runner.run_tasks_flow()

        self.assertFalse(self.runner._is_running)
        self.assertEqual("run_failed", telemetry[-1]["event"])

    async def test_stop_before_first_timeslice_stays_cancelled(self):
        self.runner.need_stop = True
        self.runner._manual_stop = True
        self.runner.stop_task = AsyncMock()
        telemetry = []
        self.runner.runner_events.telemetry.connect(telemetry.append)

        with (
            patch("app.core.runner.task_flow.send_notice") as send_notice_mock,
            patch(
                "app.core.runner.task_flow.emit_holiday_startup_logs",
                new=AsyncMock(),
            ),
        ):
            await self.runner.run_tasks_flow()

        self.assertTrue(self.runner._manual_stop)
        self.assertFalse(self.runner._is_running)
        self.assertEqual("run_cancelled", telemetry[-1]["event"])
        send_notice_mock.assert_not_called()

    def test_builtin_loader_translation_is_frozen_per_runner(self):
        source_i18n = self.task_service.builtin_task_loader.i18n_service
        self.assertEqual(
            "A translation",
            self.runner._builtin_task_loader.i18n_service.translate_text("$hello"),
        )

        source_i18n.set_translations("zh_cn", {"hello": "B translation"})

        self.assertEqual(
            "A translation",
            self.runner._builtin_task_loader.i18n_service.translate_text("$hello"),
        )
        self.assertEqual("B translation", source_i18n.translate_text("$hello"))

    def test_isolated_interface_managers_do_not_share_reload_state(self):
        singleton_before = InterfaceManager()
        manager_a = InterfaceManager.create_isolated(self.bundle_a / "interface.json")
        manager_b = InterfaceManager.create_isolated(self.bundle_b / "interface.json")

        self.assertIsNot(manager_a, manager_b)
        self.assertEqual("Bundle A", manager_a.get_interface()["name"])
        self.assertEqual("Bundle B", manager_b.get_interface()["name"])
        self.assertEqual("Bundle A", manager_a.get_interface()["name"])
        self.assertIs(singleton_before, InterfaceManager())


if __name__ == "__main__":
    unittest.main()
