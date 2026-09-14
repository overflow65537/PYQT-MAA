import asyncio
import inspect
import io
import os
import platform
import re
import shlex
import subprocess
import sys
import time as _time

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from PySide6.QtCore import QCoreApplication, QObject, QTimer
from app.common.constants import (
    _PRETASK_,
    POST_ACTION,
    _CONTROLLER_,
    _RESOURCE_,
)
from app.common.config import cfg

from maa.toolkit import Toolkit
from maa.define import (
    MaaWin32ScreencapMethodEnum,
    MaaAdbInputMethodEnum,
    MaaAdbScreencapMethodEnum,
    MaaMacOSScreencapMethodEnum,
    MaaMacOSInputMethodEnum,
)
from app.utils.notice import (
    NoticeTiming,
    send_all_enabled_channels,
    send_notice,
    send_thread,
)

from app.utils.logger import logger
from app.core.service.config_service import ConfigService
from app.core.utils.holiday import emit_holiday_startup_logs
from app.core.utils.win32_methods import (
    resolve_win32_input_method,
    resolve_win32_screencap_method,
)
from app.core.utils.resource_run_confirm import (
    acknowledge_resource_run,
    build_resource_run_identity,
    is_resource_run_acknowledged,
)
from app.core.utils.resource_pipeline_check import (
    check_resource_pipeline,
    format_pipeline_issue,
)
from app.core.service.task_service import TaskService
from app.core.speedrun import evaluate_speedrun, record_speedrun_runtime
from app.core.runner.embedded_agent_log_bridge import EmbeddedAgentLogBridge
from app.core.runner.maafw import (
    MaaFW,
    MaaFWError,
    should_start_external_agent,
)
from app.utils.controller_utils import ControllerHelper
from app.utils.screencap_lock import screencap_guard

from app.core.item import RunnerEvents, TaskItem
from app.core.builtin_task_loader import BuiltinTaskContext, BuiltinTaskLoader

# 进程级休眠阻止引用计数：多实例共享 SetThreadExecutionState，
# 仅当全部实例都停止后才清除 ES_SYSTEM_REQUIRED 标记。
import threading as _threading
_sleep_disable_counter: int = 0
_sleep_lock = _threading.Lock()


CACHED_IMAGE_ERROR = "Failed to get cached image."


class TaskFlowExecutionError(RuntimeError):
    """Raised after cleanup when one or more tasks failed."""


def _ndarray_to_png_bytes(ndarray) -> bytes | None:
    """将 BGR 格式的 numpy 截图转为 PNG 字节（用于随通知发送）。"""
    try:
        from PIL import Image

        # 控制器返回的通常是 BGR，转为 RGB
        if (
            hasattr(ndarray, "shape")
            and len(ndarray.shape) >= 3
            and ndarray.shape[2] >= 3
        ):
            pil = Image.fromarray(ndarray[..., ::-1])
        else:
            pil = Image.fromarray(ndarray)
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


class TaskFlowRunner(QObject):
    """负责执行任务流的运行时组件"""

    def __init__(
        self,
        task_service: TaskService,
        config_service: ConfigService,
        runner_events: RunnerEvents | None = None,
        run_config_callback: Callable[[str], Awaitable[Any] | Any] | None = None,
        config_id: str | None = None,
        maafw: MaaFW | None = None,
        connect_maafw_signals: bool = True,
    ):
        super().__init__()
        self.task_service = task_service
        self.config_service = config_service
        self.runner_events = runner_events or RunnerEvents()
        self._run_config_callback = run_config_callback
        self.config_id = str(config_id or "")
        # 提供给主窗口退出清理使用：停止外部通知线程
        # 注意：send_thread 定义于 app.utils.notice，为全局单例
        self.send_thread = send_thread
        self.maafw = maafw if maafw is not None else MaaFW()
        if connect_maafw_signals:
            self.maafw.callback.connect(self.callback.emit)
            self.maafw.custom_info.connect(self._handle_maafw_custom_info)
            self.maafw.agent_info.connect(self._handle_agent_info)
        self._embedded_agent_log_bridge = EmbeddedAgentLogBridge()
        self.process = None

        self.need_stop = False
        self.monitor_need_stop = False
        self._is_running = False
        # 防止同一次任务流退出时重复发射"结束"信号（幂等保护）
        self._task_flow_finished_emitted: bool = False
        self._next_config_to_run: str | None = None
        self.adb_controller_raw: dict[str, Any] | None = None
        self.adb_activate_controller: str | None = None
        self.adb_controller_config: dict[str, Any] | None = None
        self._config_switch_delay = 0.5

        # bundle 相关：在任务流开始时根据当前配置初始化
        self.bundle_path: str = "./"

        # 默认 pipeline_override（来自 Resource 任务）
        self._default_pipeline_override: Dict[str, Any] = {}

        # 任务超时相关
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(False)  # 改为周期性定时器，每小时触发一次
        self._timeout_timer.timeout.connect(self._on_task_timeout)
        self._timeout_active_entry = ""
        # 当前正在执行的任务ID，用于超时处理
        self._current_running_task_id: str | None = None
        # 任务开始时间：task_id -> 开始时间戳
        self._task_start_times: dict[str, float] = {}
        # 是否处于单任务模式（单任务模式下不进行长期任务检查）
        self._is_single_task_mode: bool = False
        # 标记是否为"手动停止"，用于控制是否发送完成通知
        self._manual_stop = False
        # 任务结果摘要：task_id -> 状态字符串（running/completed/failed/waiting/skipped等）
        self._task_results: dict[str, str] = {}
        # 任务运行状态标记：每个任务开始前置为 True，收到 abort 信号时置为 False
        self._current_task_ok: bool = True
        # 日志收集列表：用于收集任务运行过程中的日志，供超时通知使用
        self._log_messages: list[tuple[str, str, str]] = []  # (level, text, timestamp)

        # 监听 MaaFW 回调信号，用于接收 abort 等特殊事件
        self.callback.connect(self._handle_maafw_callback)

        # 连接前置检查失败原因（用于在上层发送更明确的通知文案）
        self._connect_error_reason: str | None = None

        # 运行时阻止系统休眠相关
        self._es_handle: int | None = None

        # 控制器重连：任务流期间缓存连接参数，供 cached image 失败时复用
        self._active_controller_raw: Dict[str, Any] | None = None
        self._active_resource_target: str | None = None
        self._cached_image_error_seen: bool = False
        self._controller_reconnect_used: bool = False
        self._is_connecting_device: bool = False
        self._resource_run_confirm_future: asyncio.Future[bool] | None = None
        self._runtime_snapshot_ready = False
        self._runtime_config = None
        self._runtime_tasks: list[TaskItem] = []
        self._runtime_interface: Dict[str, Any] = deepcopy(
            getattr(self.task_service, "interface", {}) or {}
        )
        self._runtime_interface_manager = None
        self._runtime_setting_options: Dict[str, Any] = {}
        source_i18n = self.task_service.builtin_task_loader.i18n_service
        runtime_i18n = source_i18n.clone()
        self._builtin_task_loader = BuiltinTaskLoader(i18n_service=runtime_i18n)

    def _target_config_id(self) -> str:
        return self.config_id or str(self.config_service.current_config_id or "")

    def _prepare_runtime_snapshot(self) -> bool:
        config_id = self._target_config_id()
        config = self.config_service.get_config(config_id) if config_id else None
        if config is None:
            return False
        self._runtime_config = deepcopy(config)
        self._runtime_tasks = self._runtime_config.tasks
        self._runtime_snapshot_ready = True
        self.bundle_path = self.config_service.get_bundle_path_for_config(
            self._runtime_config
        )
        resource_task = next(
            (task for task in self._runtime_tasks if task.item_id == _RESOURCE_),
            None,
        )
        options = {}
        if resource_task is not None and isinstance(resource_task.task_option, dict):
            raw = resource_task.task_option.get("setting_options")
            if isinstance(raw, dict):
                options = deepcopy(raw)
        self._runtime_setting_options = options
        return True

    def _get_tasks(self) -> list[TaskItem]:
        if self._runtime_snapshot_ready:
            return self._runtime_tasks
        config_id = self._target_config_id()
        config = self.config_service.get_config(config_id) if config_id else None
        if config is not None:
            return list(config.tasks)
        # Compatibility for legacy runners constructed without a fixed id.
        return list(self.task_service.get_tasks())

    def _get_task(self, task_id: str) -> TaskItem | None:
        for task in self._get_tasks():
            if task.item_id == task_id:
                return task
        return None

    def _persist_runtime_task_update(self, task: TaskItem) -> bool:
        if self._runtime_snapshot_ready and self._runtime_config is not None:
            for index, current in enumerate(self._runtime_tasks):
                if current.item_id == task.item_id:
                    self._runtime_tasks[index] = task
                    break
            else:
                self._runtime_tasks.append(task)
            self._runtime_config.tasks = self._runtime_tasks

            # Persist only this task into the latest config. Writing the whole
            # startup snapshot would overwrite unrelated edits made while the
            # runtime is active.
            config_id = self._target_config_id()
            latest = self.config_service.get_config(config_id)
            if latest is None:
                return False
            latest = deepcopy(latest)
            persisted_task = deepcopy(task)
            for index, current in enumerate(latest.tasks):
                if current.item_id == task.item_id:
                    latest.tasks[index] = persisted_task
                    break
            else:
                latest.tasks.append(persisted_task)
            return bool(self.config_service.update_config(config_id, latest))
        return bool(self.task_service.update_task(task))

    def _get_task_execution_info(self, task_id: str) -> Dict[str, Any] | None:
        """Resolve execution data entirely from this runtime snapshot."""
        task = self._get_task(task_id)
        if task is None:
            return None
        if task.is_builtin_task():
            return {
                "builtin": True,
                "builtin_key": task.builtin_key,
                "pipeline_override": {},
            }

        interface_task = self._get_task_by_name(task.name)
        if not interface_task:
            return None
        entry = str(interface_task.get("entry", "") or "")
        if not entry:
            return None

        from app.core.utils.hotkey_keycode import resolve_controller_type
        from app.core.utils.pipeline_helper import (
            _deep_merge_dict,
            get_pipeline_override_from_task_option,
        )

        controller_name = ""
        controller_task = self._get_task(_CONTROLLER_)
        if controller_task and isinstance(controller_task.task_option, dict):
            raw_name = controller_task.task_option.get("controller_type", "")
            if isinstance(raw_name, dict):
                controller_name = str(raw_name.get("value", "") or "").strip()
            else:
                controller_name = str(raw_name or "").strip()
        controller_type = resolve_controller_type(
            self._runtime_interface, controller_name
        )
        option_override = get_pipeline_override_from_task_option(
            self._runtime_interface,
            task.task_option,
            task.item_id,
            (
                self._runtime_setting_options
                if task.item_id == _RESOURCE_
                else None
            ),
            controller_type=controller_type,
        )
        merged: Dict[str, Any] = {}
        raw_task_override = interface_task.get("pipeline_override", {})
        if isinstance(raw_task_override, dict):
            _deep_merge_dict(merged, deepcopy(raw_task_override))
        if isinstance(option_override, dict):
            _deep_merge_dict(merged, option_override)
        return {"entry": entry, "pipeline_override": merged}

    def _resolve_current_bundle_base(self) -> Path:
        bundle_base = Path(self.bundle_path or "./")
        if not bundle_base.is_absolute():
            bundle_base = (Path.cwd() / bundle_base).resolve()
        else:
            bundle_base = bundle_base.resolve()
        return bundle_base

    def _resolve_current_bundle_interface_path(self) -> Path | None:
        bundle_base = self._resolve_current_bundle_base()
        for file_name in ("interface.jsonc", "interface.json"):
            candidate = bundle_base / file_name
            if candidate.is_file():
                return candidate
        return None

    def _reload_interface_for_current_bundle(self) -> None:
        from app.core.service.interface_manager import InterfaceManager

        interface_path = self._resolve_current_bundle_interface_path()
        if interface_path is None:
            logger.warning(
                "当前 bundle 缺少 interface.jsonc/interface.json，跳过运行前 interface 刷新: %s",
                self._resolve_current_bundle_base(),
            )
            return

        logger.info("运行前按当前 bundle 刷新 interface: %s", interface_path)
        interface_manager = InterfaceManager.create_isolated(
            interface_path=interface_path
        )
        self._runtime_interface_manager = interface_manager
        self._runtime_interface = deepcopy(interface_manager.get_interface() or {})

    async def _confirm_resource_run_if_needed(self) -> bool:
        """首次运行当前资源包时请求用户确认；已确认过则直接通过。"""
        identity = build_resource_run_identity(self._runtime_interface)
        if is_resource_run_acknowledged(identity):
            return True
        if self.need_stop:
            return False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._resource_run_confirm_future = future
        payload = {
            "name": identity.get("name", ""),
            "contact": identity.get("contact", ""),
            "github": identity.get("github", ""),
            "github_owner": identity.get("github_owner", ""),
            "github_repo": identity.get("github_repo", ""),
        }
        self.runner_events.resource_run_confirmation_requested.emit(payload)
        try:
            if not future.done():
                await asyncio.sleep(0)
            if self.need_stop and not future.done():
                future.set_result(False)
            accepted = bool(await future)
        finally:
            if self._resource_run_confirm_future is future:
                self._resource_run_confirm_future = None

        if accepted:
            acknowledge_resource_run(identity)
            return True
        return False

    def submit_resource_run_confirmation(self, accepted: bool) -> None:
        """由 View 回写首次运行确认结果。"""
        future = self._resource_run_confirm_future
        if future is not None and not future.done():
            future.set_result(bool(accepted))


    @property
    def callback(self):
        return self.runner_events.callback

    @property
    def log_output(self):
        return self.runner_events.log_output

    @property
    def set_window_title(self):
        return self.runner_events.set_window_title

    @property
    def task_status_changed(self):
        return self.runner_events.task_status_changed

    @property
    def task_flow_finished(self):
        return self.runner_events.task_flow_finished

    @property
    def log_clear_requested(self):
        return self.runner_events.log_clear_requested

    @property
    def info_bar_requested(self):
        return self.runner_events.info_bar_requested

    def _is_admin_runtime(self) -> bool:
        """运行时检测是否具备管理员权限（优先用 cfg 标记，失败则在 Windows 上兜底检测）。"""
        try:
            is_admin = bool(cfg.get(cfg.is_admin))
        except Exception:
            is_admin = False

        if is_admin:
            return True

        if sys.platform.startswith("win32"):
            try:
                import ctypes

                is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                try:
                    cfg.set(cfg.is_admin, is_admin)
                except Exception:
                    pass
                return is_admin
            except Exception:
                return False

        return False

    def _handle_maafw_callback(self, payload: Dict[str, Any]):
        """处理来自 MaaFW 的通用回调信号（包括自定义的 abort 信号）。

        当前实现只负责更新内部状态变量，不直接控制任务流转。
        """
        try:
            name = payload.get("name", "")
            if name == "abort":
                # 收到 abort 信号：仅标记当前任务状态，等待 run_task 完成后再由调用方判断
                self._current_task_ok = False
        except Exception as exc:
            logger.warning(f"处理 MaaFW 回调信号时出错: {exc}")

    def _note_maafw_output(self, text: str) -> None:
        if CACHED_IMAGE_ERROR in text:
            self._cached_image_error_seen = True

    def _handle_agent_info(self, info: str):
        self._note_maafw_output(info)
        if "| WARNING |" in info:
            # 从warning开始截断
            info = info.split("| WARNING |")[1]
            self.log_output.emit("WARNING", info)
        elif "| ERROR |" in info:
            # 从error开始截断
            info = info.split("| ERROR |")[1]
            self.log_output.emit("ERROR", info)
        elif "| INFO |" in info:
            # 从info开始截断
            info = info.split("| INFO |")[1]
            self.log_output.emit("INFO", info)

    def _build_agent_env_vars(
        self, controller_cfg: TaskItem, resource_cfg: TaskItem
    ) -> Dict[str, str]:
        """构建 PI_* 环境变量，在启动 agent 子进程时注入。"""
        import json as _json

        from app.common.__version__ import __version__
        from app.core.service.interface_manager import InterfaceManager

        env_vars: Dict[str, str] = {}
        interface = self._runtime_interface or {}

        # PI_INTERFACE_VERSION: Client 实现的 PI 扩展能力版本
        env_vars["PI_INTERFACE_VERSION"] = "v2.10.1"

        # PI_CLIENT_NAME
        env_vars["PI_CLIENT_NAME"] = "MFW"

        # PI_CLIENT_VERSION
        env_vars["PI_CLIENT_VERSION"] = __version__

        # PI_CLIENT_LANGUAGE: 当前 UI 语言代码
        try:
            im = InterfaceManager()
            env_vars["PI_CLIENT_LANGUAGE"] = im._current_language or ""
        except Exception:
            env_vars["PI_CLIENT_LANGUAGE"] = ""

        # PI_CLIENT_MAAFW_VERSION
        try:
            from maa.library import Library

            env_vars["PI_CLIENT_MAAFW_VERSION"] = Library.version() or ""
        except Exception:
            env_vars["PI_CLIENT_MAAFW_VERSION"] = ""

        # PI_VERSION: interface.json 顶层 version 字段
        env_vars["PI_VERSION"] = str(interface.get("version", ""))

        # PI_CONTROLLER: 当前选中的 controller 完整对象（i18n 已解析），紧凑 JSON
        controller_name = ""
        try:
            controller_type_raw = (controller_cfg.task_option or {}).get(
                "controller_type"
            )
            if isinstance(controller_type_raw, str):
                controller_name = controller_type_raw.strip()
            elif isinstance(controller_type_raw, dict):
                controller_name = str(
                    controller_type_raw.get("value", "") or ""
                ).strip()
        except Exception:
            pass

        if controller_name:
            for ctrl in interface.get("controller", []):
                if isinstance(ctrl, dict) and ctrl.get("name") == controller_name:
                    env_vars["PI_CONTROLLER"] = _json.dumps(
                        ctrl, ensure_ascii=False, separators=(",", ":")
                    )
                    break

        # PI_RESOURCE: 当前选中的 resource 完整对象（i18n 已解析），紧凑 JSON
        resource_name = ""
        try:
            resource_name = str(
                (resource_cfg.task_option or {}).get("resource", "") or ""
            ).strip()
        except Exception:
            pass

        if resource_name:
            for res in interface.get("resource", []):
                if isinstance(res, dict) and res.get("name") == resource_name:
                    env_vars["PI_RESOURCE"] = _json.dumps(
                        res, ensure_ascii=False, separators=(",", ":")
                    )
                    break

        return env_vars

    def _handle_maafw_custom_info(self, error_code: int):
        try:
            error = MaaFWError(error_code)
            match error:
                case MaaFWError.RESOURCE_OR_CONTROLLER_NOT_INITIALIZED:
                    msg = self.tr("Resource or controller not initialized")
                case MaaFWError.AGENT_CONNECTION_FAILED:
                    msg = self.tr("Agent connection failed")
                case MaaFWError.AGENT_CONNECTION_TIMEOUT:
                    seconds = getattr(
                        self.maafw, "_last_agent_connect_timeout_seconds", None
                    )
                    if seconds is None:
                        seconds = MaaFW.DEFAULT_AGENT_CONNECT_TIMEOUT_SECONDS
                    msg = self.tr("Agent connection timed out ({}s)").format(seconds)
                case MaaFWError.AGENT_CONFIG_MISSING:
                    msg = self.tr("Agent configuration is missing")
                case MaaFWError.AGENT_CONFIG_EMPTY_LIST:
                    msg = self.tr("Agent configuration list is empty")
                case MaaFWError.AGENT_CONFIG_INVALID:
                    msg = self.tr("Agent configuration is invalid")
                case MaaFWError.AGENT_CHILD_EXEC_MISSING:
                    msg = self.tr("Agent child_exec is missing")
                case MaaFWError.AGENT_START_FAILED:
                    msg = self.tr("Failed to start agent process")
                case MaaFWError.TASKER_NOT_INITIALIZED:
                    msg = self.tr("Tasker not initialized")
                case _:
                    msg = self.tr("Unknown MaaFW error code: {}").format(error_code)
            self.log_output.emit("ERROR", msg)
        except ValueError:
            logger.warning(f"Received unknown MaaFW error code: {error_code}")
            self.log_output.emit(
                "WARNING", self.tr("Unknown MaaFW error code: {}").format(error_code)
            )

    async def _get_notice_screenshot_bytes(self) -> bytes | None:
        """若设置中开启「随通知发送截图」且控制器可用，则截屏并返回 PNG 字节，否则返回 None。"""
        if not cfg.get(cfg.notice_send_screenshot):
            return None
        if not getattr(self, "maafw", None) or not getattr(
            self.maafw, "controller", None
        ):
            return None
        try:
            img = await self.maafw.screencap_test()
            if img is not None:
                return _ndarray_to_png_bytes(img)
        except Exception:
            pass
        return None

    async def _get_telemetry_screenshot_bytes(self) -> bytes | None:
        """失败诊断附件用截图，不依赖通知截图开关。"""
        if not getattr(self, "maafw", None) or not getattr(
            self.maafw, "controller", None
        ):
            return None
        try:
            img = await self.maafw.screencap_test()
            if img is not None:
                return _ndarray_to_png_bytes(img)
        except Exception:
            logger.debug("采集失败诊断截图失败", exc_info=True)
        return None

    async def _save_stop_screenshot(self, manual: bool = False) -> None:
        """停止时主动截图保存到 debug/stop/ 目录，独立于通知截图设置。

        Args:
            manual: 是否为手动停止，用于文件名标记。
        """
        # 连接阶段的原生控制器尚未对外发布；同时避免与连接工作线程争用。
        if self._active_controller_raw is None or self._is_connecting_device:
            return
        if not getattr(self, "maafw", None) or not getattr(
            self.maafw, "controller", None
        ):
            return
        try:
            img = await self.maafw.screencap_test()
            if img is None:
                return

            from PIL import Image

            if (
                hasattr(img, "shape")
                and len(img.shape) >= 3
                and img.shape[2] >= 3
            ):
                pil = Image.fromarray(img[..., ::-1])
            else:
                pil = Image.fromarray(img)

            debug_dir = Path("debug") / "stop"
            debug_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = "_manual" if manual else ""
            filename = f"stop_{timestamp}{suffix}.png"
            filepath = debug_dir / filename

            pil.save(filepath, format="PNG")
            logger.info("停止截图已保存: %s", filepath)
        except Exception:
            logger.warning("保存停止截图失败", exc_info=True)

    def _recent_logs_contain_cached_image_error(self) -> bool:
        return any(
            CACHED_IMAGE_ERROR in text for _, text, _ in self._log_messages
        )

    def _recent_maafw_log_contains_cached_image_error(self) -> bool:
        log_path = Path("debug") / "maafw.log"
        if not log_path.is_file():
            return False
        try:
            with log_path.open("rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - 16384))
                tail = log_file.read().decode("utf-8", errors="ignore")
            return CACHED_IMAGE_ERROR in tail
        except Exception as exc:
            logger.debug("读取 maafw.log 失败: %s", exc)
            return False

    def _probe_cached_image_error(self) -> bool:
        controller = getattr(self.maafw, "controller", None)
        if controller is None:
            return False
        try:
            with screencap_guard():
                _ = controller.cached_image
        except RuntimeError as exc:
            return str(exc) == CACHED_IMAGE_ERROR
        except Exception:
            return False
        return False

    def _is_cached_image_failure(self) -> bool:
        return (
            self._cached_image_error_seen
            or self._recent_logs_contain_cached_image_error()
            or self._recent_maafw_log_contains_cached_image_error()
            or self._probe_cached_image_error()
        )

    async def _reconnect_controller(self) -> bool:
        controller_raw = self._active_controller_raw
        if not isinstance(controller_raw, dict):
            logger.error("无法重新连接控制器：缺少控制器配置")
            return False

        try:
            await asyncio.to_thread(self.maafw.deactivate_controller)
        except Exception as exc:
            logger.debug("断开旧控制器 post_inactive 异常: %s", exc)

        if self.maafw.agents:
            await asyncio.to_thread(self.maafw.teardown_agents)

        self._connect_error_reason = None
        connected = await self.connect_device(
            controller_raw,
            resource_target=self._active_resource_target,
        )
        if not connected:
            if not self._connect_error_reason:
                self._connect_error_reason = self.tr(
                    "Failed to reconnect to the device."
                )
            return False

        if self.maafw.tasker and self.maafw.resource and self.maafw.controller:
            tasker = self.maafw.tasker
            resource = self.maafw.resource
            controller = self.maafw.controller
            await asyncio.to_thread(
                lambda: tasker.bind(resource, controller)
            )
        return True

    async def _try_recover_from_cached_image_failure(
        self,
        entry: str,
        pipeline_override: dict,
        save_draw: bool,
    ) -> bool | None:
        """cached image 失败时尝试重连控制器并重试任务。

        Returns:
            True: 重连后任务重试成功
            False: 重连失败，应停止任务流
            None: 不适用或未恢复
        """
        if (
            self._controller_reconnect_used
            or self.need_stop
            or self._manual_stop
            or not self._is_cached_image_failure()
        ):
            return None

        self._controller_reconnect_used = True
        logger.info("检测到 %s，尝试重新连接控制器", CACHED_IMAGE_ERROR)
        self.log_output.emit(
            "INFO",
            self.tr("Cached image failed, reconnecting controller..."),
        )

        if not await self._reconnect_controller():
            logger.error("控制器重新连接失败，停止任务流")
            self.log_output.emit(
                "ERROR",
                self.tr("Controller reconnection failed, stopping task flow"),
            )
            await self.stop_task()
            return False

        logger.info("控制器重新连接成功，重试当前任务")
        self.log_output.emit(
            "INFO",
            self.tr("Controller reconnected, retrying current task"),
        )
        self._cached_image_error_seen = False
        self._start_task_timeout(entry)

        if await self.maafw.run_task(entry, pipeline_override, save_draw):
            self._stop_task_timeout()
            return True

        self._stop_task_timeout()
        return None

    async def run_tasks_flow(
        self,
        task_id: str | None = None,
        *,
        start_task_id: str | None = None,
    ):
        """任务完整流程：连接设备、加载资源、批量运行任务

        :param start_task_id: 可选，指定从某个任务开始执行，其前面的任务会被跳过。
        """
        if self._is_running:
            logger.warning("任务流已经在运行，忽略新的启动请求")
            return
        self._is_running = True
        # RuntimeContext resets stop intent before scheduling this coroutine. Preserve
        # a stop requested after create_task() but before the first timeslice.
        if not self.need_stop:
            self._manual_stop = False
        self._task_flow_finished_emitted = False
        self._active_controller_raw = None
        self._active_resource_target = None
        self._cached_image_error_seen = False
        self._is_connecting_device = False
        # 清空任务开始时间记录
        self._task_start_times.clear()
        # 跟踪任务流是否成功启动并执行了任务
        self._tasks_started = False
        # 重置本次任务流的结果摘要
        self._task_results.clear()
        # 重置超时状态
        self._reset_task_timeout_state()
        is_single_task_mode = task_id is not None
        self._is_single_task_mode = is_single_task_mode
        effective_start_task_id = None
        if not is_single_task_mode and start_task_id:
            current_tasks = self._get_tasks()
            for task in current_tasks:
                if task.item_id == start_task_id:
                    effective_start_task_id = start_task_id
                    break
            if effective_start_task_id is None:
                logger.warning(
                    "未找到起始任务 '%s'，将从头开始执行任务序列", start_task_id
                )
        else:
            effective_start_task_id = None

        # 注意：is_hidden 由配置层（TaskService/Coordinator/UI）负责刷新；
        # runner 仅消费 is_checked/is_hidden 来执行任务流

        # 初始化任务状态：仅在完整运行时将所有选中的任务设置为等待中
        # 单独运行时，只会在对应的任务处显示进行中/完成/失败，不显示等待图标
        # 使用 QTimer 延迟发送，确保任务列表 UI 已经准备好
        def set_waiting_status():
            # 只在完整运行模式（非单任务模式）时设置等待状态
            if not is_single_task_mode:
                all_tasks = self._get_tasks()
                start_reached = effective_start_task_id is None
                for task in all_tasks:
                    if effective_start_task_id and not start_reached:
                        if task.item_id == effective_start_task_id:
                            start_reached = True
                        else:
                            continue

                    if (
                        not task.is_base_task()
                        and task.is_checked
                        and not task.is_hidden
                    ):
                        # 完整运行时，设置当前起始任务及之后的选中任务为等待中
                        self.task_status_changed.emit(task.item_id, "waiting")

        # 延迟 200ms 发送，确保任务列表已经渲染完成
        QTimer.singleShot(200, set_waiting_status)

        # 初始化日志收集列表
        self._log_messages.clear()

        def collect_log(level: str, text: str):
            """收集日志信息（包含收到的时间戳）"""
            self._note_maafw_output(text)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self._log_messages.append((level, text, timestamp))

        # 连接日志输出信号
        self.log_output.connect(collect_log)

        # 节日彩蛋：检测当天节日并随机输出一组彩蛋文案
        flow_error: Exception | None = None
        was_cancelled = False
        try:
            await emit_holiday_startup_logs(
                lambda level, text: self.log_output.emit(level, text),
                lambda title: self.set_window_title.emit(title),
            )
            if not self._prepare_runtime_snapshot():
                message = self.tr(
                    "Configuration no longer exists; task flow was not started"
                )
                self.log_output.emit("ERROR", message)
                raise TaskFlowExecutionError(message)
            self.runner_events.start_button_status.emit(
                {"text": "STOP", "status": "disabled"}
            )
            # 先把按钮状态交还给 Qt 事件循环绘制，再继续启动阶段。
            await asyncio.sleep(0)
            if self.need_stop:
                return
            self._reload_interface_for_current_bundle()
            if self.need_stop:
                return
            if not await self._confirm_resource_run_if_needed():
                self._manual_stop = True
                logger.info("用户取消首次资源运行确认，已停止任务流")
                self.log_output.emit("INFO", self.tr("Resource run cancelled"))
                return
            logger.info("[RUN_RECORD] TASK_FLOW_START")
            send_notice(
                NoticeTiming.WHEN_FLOW_STARTED,
                self.tr("Task Flow Started"),
                self.tr("Task flow has been started."),
            )
            controller_cfg = self._get_task(_CONTROLLER_)
            if not controller_cfg:
                raise ValueError("未找到基础预配置任务")
            resource_cfg = self._get_task(_RESOURCE_)
            if not resource_cfg:
                raise ValueError("未找到资源设置任务")

            invalid_reason = self._validate_base_controller_and_resource()
            if invalid_reason is not None:
                self._reset_base_controller_and_resource_to_default()
                self.log_output.emit(
                    "ERROR",
                    self.tr(
                        "Controller or resource in current config does not exist in interface. They have been reset to default. Please check and run again."
                    ),
                )
                self.log_output.emit("ERROR", invalid_reason)
                await self.stop_task()
                raise TaskFlowExecutionError(invalid_reason)

            checkbox_reason = self._validate_checkbox_selection_limits(
                task_id if is_single_task_mode else None
            )
            if checkbox_reason is not None:
                self.log_output.emit("ERROR", checkbox_reason)
                self.info_bar_requested.emit("warning", checkbox_reason)
                await self.stop_task()
                raise TaskFlowExecutionError(checkbox_reason)

            # 先执行预任务（在资源加载和控制器连接之前）
            if not await self._execute_pretasks():
                if self.need_stop:
                    return
                raise TaskFlowExecutionError("前置任务执行失败")
            if self.need_stop:
                return

            # 先加载资源，再连接控制器
            logger.info("开始加载资源...")
            self.log_output.emit("INFO", self.tr("Starting to load resources..."))
            if not await self.load_resources(resource_cfg.task_option):
                raise TaskFlowExecutionError("资源加载失败")
            if self.need_stop:
                return
            logger.info("资源加载完成")

            # 构建默认 pipeline_override
            # 合并优先级（从低到高）：配置级 global_option < resource.option < controller.option
            # 任务级 override 在 run_task 中合并（最高优先级）
            from app.core.utils.pipeline_helper import (
                get_pipeline_override_from_task_option,
                get_controller_option_pipeline_override,
                _deep_merge_dict,
            )

            from app.core.service.interface_manager import InterfaceManager

            interface_manager = self._runtime_interface_manager
            if interface_manager is None:
                interface_manager = InterfaceManager.create_isolated()
            embedded_override: bool | None = None
            controller_option = controller_cfg.task_option or {}
            if (
                isinstance(controller_option, dict)
                and "agent_embedded" in controller_option
            ):
                embedded_override = bool(controller_option["agent_embedded"])
            embedded_ready = interface_manager.apply_agent_customization(
                embedded_override=embedded_override
            )
            runner_interface = deepcopy(interface_manager.get_interface() or self._runtime_interface)
            self._runtime_interface = runner_interface
            self.runner_events.telemetry.emit(
                {"event": "configure", "interface": runner_interface}
            )
            controller_type = self._get_controller_type(
                controller_cfg.task_option or {}
            )

            # 1. Setting 全局选项 + resource.option（已在函数内按优先级合并）
            self._default_pipeline_override = get_pipeline_override_from_task_option(
                runner_interface,
                resource_cfg.task_option,
                _RESOURCE_,
                self._runtime_setting_options,
                controller_type=controller_type,
            )

            # 2. controller.option（优先级高于 resource.option 和 global_option）
            controller_override = get_controller_option_pipeline_override(
                runner_interface, controller_cfg.task_option
            )
            if controller_override:
                _deep_merge_dict(self._default_pipeline_override, controller_override)

            embedded_error = str(
                runner_interface.get("__embedded_agent_error", "") or ""
            ).strip()
            if not embedded_ready:
                reason = embedded_error or self.tr("Unknown reason")
                message = self.tr("Embedded Agent prepare failed: ") + reason
                logger.error("嵌入式 Agent 准备失败: %s", reason)
                self.log_output.emit("ERROR", message)
                raise TaskFlowExecutionError(message)

            agent_config = runner_interface.get("agent", None)
            if should_start_external_agent(agent_config):
                bundle_base = Path(self.bundle_path or "./")
                if not bundle_base.is_absolute():
                    bundle_base = (Path.cwd() / bundle_base).resolve()
                self.maafw.agent_project_dir = bundle_base
                self.maafw.agent_data_raw = agent_config
                self.maafw.agent_connection_timeout_seconds = MaaFW._coerce_timeout_seconds(
                    controller_option.get("agent_timeout")
                )
                # v2.5.0: 构建 PI_* 环境变量供 agent 子进程使用
                self.maafw.agent_env_vars = self._build_agent_env_vars(
                    controller_cfg, resource_cfg
                )
                self.log_output.emit("INFO", self.tr("Agent Service Start"))

            embedded_custom = bool(
                runner_interface.get("__embedded_generated_custom")
            )
            if embedded_custom and runner_interface.get("custom") and self.maafw.resource:
                agent_root = runner_interface.get("custom", "")
                logger.info("检测到 embedded agent，准备加载自定义组件: %s", agent_root)
                self.log_output.emit(
                    "INFO", self.tr("Starting to load custom components...")
                )

                await self._clear_embedded_agent_custom()
                # 让“加载自定义模块”日志先完成布局和绘制。
                await asyncio.sleep(0)

                bundle_path_str = self.bundle_path or "./"
                base_dir = Path(bundle_path_str)
                if not base_dir.is_absolute():
                    base_dir = (Path.cwd() / base_dir).resolve()

                raw_agent = str(agent_root).replace("{PROJECT_DIR}", "")
                normalized_agent = raw_agent.lstrip("\\/")
                agent_path_obj = Path(normalized_agent)
                if agent_path_obj.is_absolute():
                    agent_root_path = agent_path_obj
                else:
                    agent_root_path = (base_dir / normalized_agent).resolve()

                agent_entry = runner_interface.get("__embedded_agent_entry")
                agent_entry_path = None
                if agent_entry:
                    raw_entry = str(agent_entry).replace("{PROJECT_DIR}", "")
                    normalized_entry = raw_entry.lstrip("\\/")
                    entry_path_obj = Path(normalized_entry)
                    if entry_path_obj.is_absolute():
                        agent_entry_path = entry_path_obj
                    else:
                        agent_entry_path = (base_dir / normalized_entry).resolve()

                logger.info(
                    "embedded agent 路径解析: bundle_path=%s, base_dir=%s, custom=%s, "
                    "agent_root=%s, agent_entry=%s",
                    bundle_path_str,
                    base_dir,
                    agent_root,
                    agent_root_path,
                    agent_entry_path,
                )

                if not await self._load_embedded_agent_custom(
                    agent_root_path,
                    agent_entry_path,
                ):
                    message = self.tr(
                        "Custom components loading failed, the flow is terminated."
                    )
                    logger.error("内置 agent 自定义组件加载失败")
                    self.log_output.emit("ERROR", message)
                    self.log_output.emit(
                        "ERROR", self.tr("please try to reset resource in setting")
                    )
                    raise TaskFlowExecutionError(message)

                if not self.maafw.load_embedded_aspect_ratio_sink():
                    message = self.tr(
                        "Embedded aspect ratio checker failed to load, "
                        "the flow is terminated."
                    )
                    logger.error("内置模式分辨率检查 sink 加载失败")
                    self.log_output.emit("ERROR", message)
                    raise TaskFlowExecutionError(message)

                # custom 根目录来自 interface（InterfaceManager 根据 agent.child_args 写入 custom）
                log_extra_roots: list[Path] = []
                if agent_entry_path is not None:
                    entry_parent = agent_entry_path.parent.resolve()
                    if entry_parent != agent_root_path.resolve():
                        log_extra_roots.append(entry_parent)
                attached = self._embedded_agent_log_bridge.attach(
                    lambda level, text: self.log_output.emit(level, text),
                    custom_root=agent_root_path,
                    extra_roots=log_extra_roots,
                )

                if attached:
                    logger.debug(
                        "embedded agent 日志已桥接到 UI（custom=%s），挂载 logger 数量: %s",
                        agent_root_path,
                        attached,
                    )
                else:
                    logger.warning(
                        "embedded agent 日志桥接未挂载任何 logger，custom 路径: %s",
                        agent_root_path,
                    )
            # 资源加载完成后连接控制器
            logger.info("开始连接设备...")
            self.log_output.emit("INFO", self.tr("Starting to connect device..."))
            self._connect_error_reason = None
            resource_target = (
                resource_cfg.task_option.get("resource")
                if resource_cfg
                and isinstance(getattr(resource_cfg, "task_option", None), dict)
                else None
            )
            connected = await self.connect_device(
                controller_cfg.task_option, resource_target=resource_target
            )
            if not connected:
                logger.error("设备连接失败")
                send_notice(
                    NoticeTiming.WHEN_CONNECT_FAILED,
                    self.tr("Device Connection Failed"),
                    self._connect_error_reason
                    or self.tr("Failed to connect to the device."),
                )
                raise TaskFlowExecutionError(
                    self._connect_error_reason or "设备连接失败"
                )
            self._active_controller_raw = controller_cfg.task_option
            self._active_resource_target = resource_target
            self.log_output.emit("INFO", self.tr("Device connected successfully"))
            logger.info("设备连接成功")
            image_bytes = await self._get_notice_screenshot_bytes()
            send_notice(
                NoticeTiming.WHEN_CONNECT_SUCCESS,
                self.tr("Device Connected Successfully"),
                self.tr("Device has been connected successfully."),
                image_bytes=image_bytes,
            )
            start_time = _time.time()
            await self.maafw.screencap_test()
            end_time = _time.time()
            self.callback.emit(
                {"name": "speed_test", "details": end_time - start_time}
            )
            tasks_to_run = self._collect_tasks_to_run(
                task_id=task_id,
                effective_start_task_id=effective_start_task_id,
                is_single_task_mode=is_single_task_mode,
            )
            if not tasks_to_run:
                return
            controller_name = self._get_controller_name(
                controller_cfg.task_option or {}
            )
            controller_info = next(
                (
                    controller
                    for controller in runner_interface.get("controller", [])
                    if isinstance(controller, dict)
                    and controller.get("name") == controller_name
                ),
                {"name": controller_name, "type": controller_type},
            )
            self.runner_events.telemetry.emit(
                {
                    "event": "run_start",
                    "tasks": [task.name for task in tasks_to_run if task],
                    "controller": controller_info,
                }
            )
            if is_single_task_mode:
                logger.debug(f"开始执行单个任务: {task_id}")
            self._tasks_started = True
            # 任务开始执行，阻止系统休眠
            self._es_handle = self._set_sleep_disabled()
            for task in tasks_to_run:
                if not task:
                    continue
                # 每个任务开始前，假定其可以正常完成
                self._current_task_ok = True
                # 记录当前正在执行的任务，用于超时处理
                self._current_running_task_id = task.item_id
                # 发送任务运行中状态
                self.task_status_changed.emit(task.item_id, "running")
                try:
                    task_result = await self.run_task(
                        task.item_id,
                        skip_speedrun=is_single_task_mode,
                    )
                    if task_result == "skipped":
                        # 因 speedrun 限制被跳过：记录结果并在列表中显示为"已跳过"
                        self._task_results[task.item_id] = "skipped"
                        self.task_status_changed.emit(task.item_id, "skipped")
                        continue
                    # 如果任务显式返回 False，视为致命失败，终止整个任务流
                    if task_result is False:
                        msg = f"任务执行失败: {task.name}, 返回 False，终止流程"
                        logger.error(msg)
                        # 记录任务结果
                        self._task_results[task.item_id] = "failed"
                        # 发送任务失败状态
                        self.task_status_changed.emit(task.item_id, "failed")
                        # 发送任务失败通知
                        if not self._manual_stop:
                            image_bytes = await self._get_notice_screenshot_bytes()
                            send_notice(
                                NoticeTiming.WHEN_TASK_FAILED,
                                self.tr("Task Failed"),
                                self.tr(
                                    "Task '{}' failed and the flow was terminated."
                                ).format(task.name),
                                image_bytes=image_bytes,
                            )
                        await self.stop_task()
                        break

                    # 任务运行过程中如果触发了 abort 信号，则认为该任务未成功完成，
                    # 但不中断整个任务流，直接切换到下一个任务。
                    if not self._current_task_ok:
                        logger.warning(
                            f"任务执行被中途中止(abort): {task.name}，切换到下一个任务"
                        )
                        # 记录任务结果并发送任务失败状态
                        self._task_results[task.item_id] = "failed"
                        self.task_status_changed.emit(task.item_id, "failed")
                        # 发送任务失败通知
                        if not self._manual_stop:
                            image_bytes = await self._get_notice_screenshot_bytes()
                            send_notice(
                                NoticeTiming.WHEN_TASK_FAILED,
                                self.tr("Task Failed"),
                                self.tr("Task '{}' was aborted.").format(task.name),
                                image_bytes=image_bytes,
                            )
                    else:
                        # 记录任务结果
                        status = "completed"
                        self._task_results[task.item_id] = status
                        self.task_status_changed.emit(task.item_id, status)
                        # 发送任务成功通知
                        image_bytes = await self._get_notice_screenshot_bytes()
                        send_notice(
                            NoticeTiming.WHEN_TASK_SUCCESS,
                            self.tr("Task Completed"),
                            self.tr(
                                "Task '{}' has been completed successfully."
                            ).format(task.name),
                            image_bytes=image_bytes,
                        )

                except Exception as exc:
                    logger.exception("任务执行失败: %s", task.name)
                    self._task_results[task.item_id] = "failed"
                    self.task_status_changed.emit(task.item_id, "failed")
                    if not self._manual_stop:
                        image_bytes = await self._get_notice_screenshot_bytes()
                        send_notice(
                            NoticeTiming.WHEN_TASK_FAILED,
                            self.tr("Task Failed"),
                            self.tr("Task '{}' failed with error: {}").format(
                                task.name, str(exc)
                            ),
                            image_bytes=image_bytes,
                        )
                finally:
                    # continue/exception 都必须清掉当前任务，避免状态泄漏到下一轮。
                    self._current_running_task_id = None

                if self.need_stop:
                    logger.debug("收到停止请求，流程终止")
                    break

            # 只有在任务流正常完成（非手动停止且没有失败任务）时才输出完成提示。
            if self._is_tasks_flow_completed_normally():
                self.log_output.emit(
                    "INFO", self.tr("All tasks have been completed")
                )

            failed_task_ids = [
                task_id
                for task_id, status in self._task_results.items()
                if status == "failed"
            ]
            if failed_task_ids and not self._manual_stop:
                raise TaskFlowExecutionError(
                    f"{len(failed_task_ids)} task(s) failed: "
                    + ", ".join(failed_task_ids)
                )

        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            flow_error = exc
            logger.exception("任务流程执行异常")
            self.log_output.emit("ERROR", self.tr("Task flow error: ") + str(exc))
        finally:
            # 任务流退出信号优先发出，让监控和按钮立即结束运行态展示。
            if not self._task_flow_finished_emitted:
                self._task_flow_finished_emitted = True
                try:
                    self.task_flow_finished.emit(
                        {
                            "manual_stop": bool(self._manual_stop),
                            "need_stop": bool(self.need_stop),
                            "single_task_mode": bool(is_single_task_mode),
                            "tasks_started": bool(self._tasks_started),
                        }
                    )
                except Exception:
                    logger.exception("发射 task_flow_finished 信号失败")

            try:
                self._embedded_agent_log_bridge.detach()
            except Exception as detach_error:
                logger.exception("断开 embedded agent 日志桥接失败")
                if flow_error is None:
                    flow_error = detach_error

            try:
                self.log_output.disconnect(collect_log)
            except (RuntimeError, TypeError) as disconnect_error:
                logger.exception("断开任务流日志收集器失败")
                if flow_error is None:
                    flow_error = disconnect_error

            stop_requested_before_cleanup = self.need_stop
            was_manual_stop = self._manual_stop
            terminal_post_action: str | None = None
            summary_image_bytes: bytes | None = None
            if (
                self._log_messages
                and not is_single_task_mode
                and not was_manual_stop
                and not was_cancelled
                and (flow_error is not None or not stop_requested_before_cleanup)
            ):
                # 清理控制器前保留通知截图；通知附件失败不能阻断运行时清理。
                try:
                    summary_image_bytes = await self._get_notice_screenshot_bytes()
                except Exception:
                    logger.exception("获取任务流汇总通知截图失败")

            try:
                post_task = self._get_task(POST_ACTION)
                post_cfg = (
                    post_task.task_option.get("post_action") if post_task else None
                )
                always_run_post_action = bool(
                    isinstance(post_cfg, dict) and post_cfg.get("always_run")
                )
                should_run_post_action = (
                    not is_single_task_mode
                    and self._tasks_started
                    and not was_manual_stop
                    and (
                        not stop_requested_before_cleanup
                        or always_run_post_action
                    )
                )
                if should_run_post_action:
                    terminal_post_action = await self._handle_post_action()
            except Exception as post_action_error:
                logger.exception("完成后操作执行失败")
                if flow_error is None:
                    flow_error = post_action_error

            # stop_task() 会把 need_stop 置为 True；最终状态必须使用清理前的停止意图。
            # 自然跑完跳过 post_stop，避免 MaaFramework release 构建假 ERR（#252）。
            should_post_stop = (
                was_manual_stop
                or was_cancelled
                or stop_requested_before_cleanup
            )
            try:
                await self.stop_task(post_stop=should_post_stop)
            except Exception as cleanup_error:
                logger.exception("任务流清理失败")
                if flow_error is None:
                    flow_error = cleanup_error
            finally:
                self._manual_stop = was_manual_stop
                self._is_running = False

            # 清除所有任务状态；UI 信号失败不影响业务执行结果。
            try:
                for task in self._get_tasks():
                    if not task.is_base_task():
                        self.task_status_changed.emit(task.item_id, "")
            except Exception:
                logger.exception("清除任务状态失败")

            has_failed_task = any(
                status == "failed" for status in self._task_results.values()
            )
            if flow_error is not None or has_failed_task:
                telemetry_event = "run_failed"
            elif was_manual_stop or was_cancelled or stop_requested_before_cleanup:
                telemetry_event = "run_cancelled"
            else:
                telemetry_event = "run_finished"
            telemetry_payload: dict[str, Any] = {"event": telemetry_event}
            if telemetry_event == "run_failed":
                if flow_error is not None:
                    telemetry_payload["error"] = str(flow_error)
                screenshot = None
                try:
                    screenshot = await self._get_telemetry_screenshot_bytes()
                except Exception:
                    logger.debug("采集失败诊断截图失败", exc_info=True)
                if screenshot:
                    telemetry_payload["attachments"] = [
                        {
                            "filename": "screenshot.png",
                            "content_type": "image/png",
                            "bytes": screenshot,
                        }
                    ]
            self.runner_events.telemetry.emit(telemetry_payload)

            # 取消不冒充完成；仅对最终成功或失败发送流程汇总通知。
            if (
                telemetry_event in {"run_finished", "run_failed"}
                and self._log_messages
                and not is_single_task_mode
            ):
                try:
                    send_notice(
                        NoticeTiming.WHEN_POST_TASK,
                        self.tr(
                            "Task Flow Failed"
                            if telemetry_event == "run_failed"
                            else "Task Flow Completed"
                        ),
                        self._get_collected_logs(),
                        image_bytes=summary_image_bytes,
                    )
                except Exception:
                    logger.exception("发送任务流汇总通知失败")

            # 退出软件和关机放在汇总通知入队之后，保证等待逻辑确实有消息可等。
            if terminal_post_action == "close_software":
                await self._close_software()
            elif terminal_post_action == "shutdown":
                await self._shutdown_system_after_notice()

            next_config = self._next_config_to_run
            self._next_config_to_run = None
            if next_config:
                logger.info(
                    "完成后自动启动配置: %s（等待 %.2f 秒）",
                    next_config,
                    self._config_switch_delay,
                )
                await asyncio.sleep(self._config_switch_delay)
                self._schedule_configuration_run(next_config)

            # 必须在 finally 内传播：否则 try 中的提前 return 会吞掉清理异常。
            if flow_error is not None:
                raise flow_error

    def _schedule_configuration_run(self, config_id: str) -> None:
        """把后续配置的启动交给其所属 RuntimeContext。"""
        if self._run_config_callback is None:
            logger.error("无法启动后续配置 %s：未配置运行时路由", config_id)
            return

        try:
            result = self._run_config_callback(config_id)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception as exc:
            logger.error("启动完成后配置 %s 失败: %s", config_id, exc)

    def _collect_tasks_to_run(
        self,
        task_id: str | None,
        effective_start_task_id: str | None,
        is_single_task_mode: bool,
    ) -> list[TaskItem]:
        """构建本次任务流要执行的任务列表"""
        tasks: list[TaskItem] = []
        if is_single_task_mode:
            if not task_id:
                return tasks
            task = self._get_task(task_id)
            if not task:
                logger.error(f"任务 ID '{task_id}' 不存在")
                return tasks
            # 执行层只关心"任务是否被禁用"，不展开禁用原因
            if self._is_task_disabled(task):
                return tasks
            if not task.is_checked:
                return tasks
            tasks.append(task)
            return tasks

        start_reached = effective_start_task_id is None
        for task in self._get_tasks():
            if effective_start_task_id and not start_reached:
                if task.item_id == effective_start_task_id:
                    start_reached = True
                else:
                    continue

            if task.item_id in (_PRETASK_, _CONTROLLER_, _RESOURCE_, POST_ACTION):
                continue

            if not task.is_checked:
                continue

            if self._is_task_disabled(task):
                continue

            tasks.append(task)

        return tasks

    @staticmethod
    def _is_pretask_entry_allowed(
        entry: dict, current_controller: str, current_resource: str
    ) -> bool:
        """按 pretask 条目的 controller/resource 白名单过滤。"""
        for key, current in (("controller", current_controller), ("resource", current_resource)):
            value = entry.get(key)
            if value in (None, "", [], {}):
                continue
            if not current:
                continue
            allowed: list[str] = []
            if isinstance(value, str):
                if value.strip():
                    allowed = [value.strip()]
            elif isinstance(value, list):
                allowed = [str(x).strip() for x in value if x is not None and str(x).strip()]
            else:
                continue
            if current.lower() not in {s.lower() for s in allowed if s}:
                return False
        return True

    def _get_current_pretask_filter_name(self, item_id: str) -> str:
        """获取当前选中的控制器或资源 name，用于 pretask 条目过滤。"""
        task = self._get_task(item_id)
        if not task or not isinstance(task.task_option, dict):
            return ""
        raw = task.task_option.get(
            "controller_type" if item_id == _CONTROLLER_ else "resource"
        )
        if isinstance(raw, str):
            return raw.strip()
        if isinstance(raw, dict):
            return str(raw.get("value", "") or "").strip()
        return ""

    async def _execute_pretasks(self) -> bool:
        """执行预任务：在资源加载和控制器连接之前，按序执行 interface.pretask 中定义的每个程序。

        Returns:
            True 表示所有预任务执行成功（或无预任务定义），False 表示执行失败需中止任务流。
        """
        import json

        pretask_task = self._get_task(_PRETASK_)
        if not pretask_task:
            return True

        interface = self._runtime_interface or {}
        pretask_entries: list = list(interface.get("pretask", []) or [])
        if not pretask_entries:
            return True

        current_controller = self._get_current_pretask_filter_name(_CONTROLLER_)
        current_resource = self._get_current_pretask_filter_name(_RESOURCE_)

        pretask_entries = [
            e for e in pretask_entries
            if self._is_pretask_entry_allowed(e, current_controller, current_resource)
        ]
        if not pretask_entries:
            return True

        task_option = pretask_task.task_option or {}
        pretask_option_entries: list = list(task_option.get("pretask_entries", []) or [])

        bundle_base = Path(self.bundle_path or "./")
        if not bundle_base.is_absolute():
            bundle_base = (Path.cwd() / bundle_base).resolve()

        for idx, entry in enumerate(pretask_entries):
            if not isinstance(entry, dict):
                continue
            exec_path = (entry.get("exec") or "").strip()
            if not exec_path:
                logger.warning("PreTask entry %d: exec 缺失，已跳过", idx)
                continue

            args = entry.get("args", [])
            if not isinstance(args, list):
                args = []
            args = [str(a) for a in args]

            entry_options: dict = {}
            if idx < len(pretask_option_entries) and isinstance(pretask_option_entries[idx], dict):
                entry_options = pretask_option_entries[idx].get("options", {}) or {}

            if entry_options:
                from app.core.utils.option_secret import decrypt_option_tree

                runtime_options = decrypt_option_tree(
                    entry_options, interface.get("option", {})
                )
                if not isinstance(runtime_options, dict):
                    runtime_options = entry_options
                payload = json.dumps(
                    runtime_options, ensure_ascii=False, separators=(",", ":")
                )
                args.append(payload)

            exec_target = Path(exec_path)
            if not exec_target.is_absolute():
                exec_target = (bundle_base / exec_path.lstrip("\\/")).resolve()

            exec_str = str(exec_target)
            logger.info("PreTask[%d] 执行: %s", idx, exec_str)
            self.log_output.emit("INFO", self.tr("Executing pretask: ") + exec_str)

            try:
                proc = await asyncio.create_subprocess_exec(
                    exec_str,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(bundle_base),
                )

                communicate_task = asyncio.create_task(proc.communicate())
                cancelled_by_user = False
                stdout = None
                stderr = None
                while True:
                    done, _ = await asyncio.wait(
                        [communicate_task], timeout=0.5
                    )
                    if communicate_task in done:
                        stdout, stderr = communicate_task.result()
                        break
                    if self.need_stop:
                        cancelled_by_user = True
                        communicate_task.cancel()
                        proc.kill()
                        await proc.wait()
                        break

                if cancelled_by_user:
                    logger.info("PreTask[%d] 已被用户终止", idx)
                    self.log_output.emit("INFO", self.tr("Pretask was stopped by user"))
                    return False

                if stdout:
                    for line in stdout.decode("utf-8", errors="replace").splitlines():
                        if line.strip():
                            self.log_output.emit("INFO", f"[PreTask{idx}] {line.strip()}")
                if stderr:
                    for line in stderr.decode("utf-8", errors="replace").splitlines():
                        if line.strip():
                            self.log_output.emit("WARNING", f"[PreTask{idx}] {line.strip()}")

                if proc.returncode != 0:
                    logger.error("PreTask[%d] 返回非零退出码: %d", idx, proc.returncode)
                    self.log_output.emit(
                        "ERROR",
                        self.tr("Pretask exited with code {code}: {path}").format(
                            code=proc.returncode, path=exec_str
                        ),
                    )
                    return False
            except FileNotFoundError:
                logger.error("PreTask[%d] 执行文件未找到: %s", idx, exec_str)
                self.log_output.emit(
                    "ERROR",
                    self.tr("Pretask executable not found: ") + exec_str,
                )
                return False
            except Exception as e:
                logger.error("PreTask[%d] 执行异常: %s", idx, e)
                self.log_output.emit(
                    "ERROR",
                    self.tr("Pretask execution failed: ") + str(e),
                )
                return False

        return True

    def _translate_log_level(self, level: str) -> str:
        """翻译日志级别"""
        level_upper = (level or "").upper()
        level_map = {
            "INFO": self.tr("INFO"),
            "WARNING": self.tr("WARNING"),
            "ERROR": self.tr("ERROR"),
            "CRITICAL": self.tr("CRITICAL"),
        }
        return level_map.get(level_upper, level)

    def _validate_checkbox_selection_limits(
        self, single_task_id: str | None = None
    ) -> str | None:
        """启动前校验 checkbox 的 min_count / max_count。"""
        from app.core.utils.option_checkbox import collect_checkbox_violations

        interface = self._runtime_interface or {}
        option_defs = interface.get("option", {})
        if not isinstance(option_defs, dict):
            return None

        option_maps: list[Any] = []
        resource_task = self._get_task(_RESOURCE_)
        if resource_task and isinstance(resource_task.task_option, dict):
            option_maps.append(resource_task.task_option)
        controller_task = self._get_task(_CONTROLLER_)
        if controller_task and isinstance(controller_task.task_option, dict):
            option_maps.append(controller_task.task_option)
        pretask_task = self._get_task(_PRETASK_)
        if pretask_task and isinstance(pretask_task.task_option, dict):
            option_maps.append(pretask_task.task_option)

        for task in self._get_tasks():
            if task.is_base_task() or task.is_hidden:
                continue
            if single_task_id:
                if task.item_id != single_task_id:
                    continue
            elif not task.is_checked:
                continue
            if isinstance(task.task_option, dict):
                option_maps.append(task.task_option)

        violations = []
        for option_map in option_maps:
            violations.extend(collect_checkbox_violations(option_map, option_defs))
        if not violations:
            return None

        first = violations[0]
        label = first.option_label or first.option_name
        if first.kind == "min":
            return self.tr(
                "Option \"{name}\" requires at least {count} selected items"
            ).format(name=label, count=first.min_count)
        return self.tr(
            "Option \"{name}\" allows at most {count} selected items"
        ).format(name=label, count=first.max_count)

    def _validate_base_controller_and_resource(self) -> str | None:
        """校验当前配置中的控制器/资源是否存在于 interface。"""
        interface = self._runtime_interface or {}
        controller_names = {
            str(item.get("name", "")).strip()
            for item in interface.get("controller", [])
            if isinstance(item, dict)
        }
        resource_defs = {
            str(item.get("name", "")).strip(): item
            for item in interface.get("resource", [])
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        }

        controller_task = self._get_task(_CONTROLLER_)
        resource_task = self._get_task(_RESOURCE_)
        if not controller_task or not resource_task:
            return self.tr("Base controller/resource task is missing.")

        controller_name = ""
        controller_type_raw = (controller_task.task_option or {}).get("controller_type")
        if isinstance(controller_type_raw, str):
            controller_name = controller_type_raw.strip()
        elif isinstance(controller_type_raw, dict):
            controller_name = str(controller_type_raw.get("value", "") or "").strip()

        resource_name = str((resource_task.task_option or {}).get("resource", "") or "").strip()

        if controller_name not in controller_names:
            self._emit_controller_setup_hint(
                reason=(
                    "controller_name_empty"
                    if not controller_name
                    else "controller_not_found"
                ),
                controller_name=controller_name,
                resource_name=resource_name,
            )
            return self.tr("Current controller does not exist in interface: {}" ).format(
                controller_name or self.tr("(empty)")
            )

        if resource_name not in resource_defs:
            return self.tr("Current resource does not exist in interface: {}" ).format(
                resource_name or self.tr("(empty)")
            )

        resource_def = resource_defs.get(resource_name) or {}
        if not self._is_allowed_by_name_list(
            resource_def.get("controller"),
            controller_name,
        ):
            return self.tr(
                "Current resource is not enabled for current controller: {} -> {}"
            ).format(
                controller_name or self.tr("(empty)"),
                resource_name or self.tr("(empty)"),
            )

        return None

    def _reset_base_controller_and_resource_to_default(self) -> None:
        """将基础控制器/资源重置到 interface 的首项（资源需满足控制器约束）。"""
        interface = self._runtime_interface or {}
        default_controller = ""
        default_resource = ""

        controllers = interface.get("controller", [])
        if isinstance(controllers, list):
            for item in controllers:
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "").strip()
                    if name:
                        default_controller = name
                        break

        resources = interface.get("resource", [])
        if isinstance(resources, list):
            for item in resources:
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "").strip()
                    if not name:
                        continue
                    # 资源可选控制器列表存在时，必须包含当前默认控制器
                    if self._is_allowed_by_name_list(
                        item.get("controller"),
                        default_controller,
                    ):
                        default_resource = name
                        break

        # 若没有找到满足控制器约束的资源，则回退为第一个资源（兜底）
        if not default_resource and isinstance(resources, list):
            for item in resources:
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "").strip()
                    if name:
                        default_resource = name
                        break

        controller_task = self._get_task(_CONTROLLER_)
        if controller_task and isinstance(controller_task.task_option, dict):
            controller_task.task_option["controller_type"] = default_controller
            self._persist_runtime_task_update(controller_task)

        resource_task = self._get_task(_RESOURCE_)
        if resource_task and isinstance(resource_task.task_option, dict):
            resource_task.task_option["resource"] = default_resource
            self._persist_runtime_task_update(resource_task)

        logger.warning(
            "检测到无效控制器/资源配置，已重置为默认。controller=%s, resource=%s",
            default_controller,
            default_resource,
        )

    def _is_allowed_by_name_list(self, value: Any, current: str) -> bool:
        """通用列表约束判断：空约束=允许，否则 current 必须命中。"""
        current_norm = str(current or "").strip().lower()
        if value in (None, "", [], {}):
            return True

        allowed: list[str] = []
        if isinstance(value, str):
            if value.strip():
                allowed = [value.strip()]
        elif isinstance(value, list):
            allowed = [
                str(x).strip()
                for x in value
                if x is not None and str(x).strip()
            ]
        else:
            # 非支持格式不拦截，避免误伤旧配置
            return True

        if not allowed:
            return True
        if not current_norm:
            return False

        return current_norm in {name.lower() for name in allowed}

    def _extract_configured_controller_name(
        self, controller_raw: Dict[str, Any] | None
    ) -> str:
        if not isinstance(controller_raw, dict):
            return ""
        controller_type_raw = controller_raw.get("controller_type")
        if isinstance(controller_type_raw, str):
            return controller_type_raw.strip()
        if isinstance(controller_type_raw, dict):
            return str(controller_type_raw.get("value", "") or "").strip()
        return ""

    def _get_current_resource_name(self) -> str:
        resource_task = self._get_task(_RESOURCE_)
        if not resource_task or not isinstance(resource_task.task_option, dict):
            return ""
        return str(resource_task.task_option.get("resource", "") or "").strip()

    def _get_resource_supported_controller_names(self, resource_name: str) -> list[str]:
        interface = self._runtime_interface or {}
        all_controller_names = [
            str(item.get("name", "") or "").strip()
            for item in interface.get("controller", [])
            if isinstance(item, dict) and str(item.get("name", "") or "").strip()
        ]

        resource_def = None
        for item in interface.get("resource", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("name", "") or "").strip() == resource_name:
                resource_def = item
                break
        if not resource_def:
            return all_controller_names

        allowed = resource_def.get("controller")
        if allowed in (None, "", [], {}):
            return all_controller_names
        if isinstance(allowed, str):
            return [allowed.strip()] if allowed.strip() else all_controller_names
        if isinstance(allowed, list):
            names = [
                str(item or "").strip()
                for item in allowed
                if str(item or "").strip()
            ]
            return names or all_controller_names
        return all_controller_names

    def _emit_controller_setup_hint(
        self,
        reason: str,
        controller_name: str = "",
        resource_name: str | None = None,
    ) -> None:
        current_resource = (
            str(resource_name or "").strip()
            if resource_name is not None
            else self._get_current_resource_name()
        )
        current_controller = (
            str(controller_name or "").strip()
            or self._extract_configured_controller_name(
                getattr(self._get_task(_CONTROLLER_), "task_option", None)
            )
        )
        self.runner_events.controller_setup_hint_requested.emit(
            {
                "reason": reason,
                "controller_name": current_controller,
                "resource_name": current_resource,
                "supported_controllers": self._get_resource_supported_controller_names(
                    current_resource
                ),
            }
        )

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_running_task_id(self) -> str | None:
        return self._current_running_task_id

    async def connect_device(
        self,
        controller_raw: Dict[str, Any],
        resource_target: str | None = None,
    ):
        """连接 MaaFW 控制器"""
        # 连接前置检查：若控制器需要管理员权限但当前不是管理员，则直接中止
        self._connect_error_reason = None
        controller_name = ""
        try:
            controller_type_raw = controller_raw.get("controller_type")
            if isinstance(controller_type_raw, str):
                controller_name = controller_type_raw.strip()
            elif isinstance(controller_type_raw, dict):
                controller_name = str(
                    controller_type_raw.get("value", "") or ""
                ).strip()
        except Exception:
            controller_name = ""

        if not controller_name:
            msg = self.tr(
                "Controller name is empty, please configure controller in settings"
            )
            self._connect_error_reason = msg
            logger.error("控制器名称为空，无法连接设备")
            self.log_output.emit("ERROR", msg)
            self._emit_controller_setup_hint(
                reason="controller_name_empty",
                controller_name=controller_name,
                resource_name=resource_target,
            )
            try:
                await self.stop_task()
            except Exception:
                pass
            return False

        interface_controller = self._get_interface_controller_entry(controller_name)
        if not interface_controller:
            msg = self.tr(
                "Controller '{}' not found, please reset controller in settings"
            ).format(controller_name)
            self._connect_error_reason = msg
            logger.error(f"未找到控制器名称: {controller_name}")
            self.log_output.emit("ERROR", msg)
            try:
                await self.stop_task()
            except Exception:
                pass
            return False

        controller_name = interface_controller.get("name", controller_name)

        # interface 中控制器可带 option，作为默认项与 task_option 合并（task 覆盖 interface）
        # option 可含 resource/controller 列表：仅当当前 resource/controller 在列表中时才应用（为空则全部显示）
        if isinstance(controller_name, str) and controller_name:
            for ctrl in (self._runtime_interface or {}).get("controller", []):
                if not isinstance(ctrl, dict) or ctrl.get("name") != controller_name:
                    continue
                opt = ctrl.get("option")
                if not isinstance(opt, dict):
                    break
                allow_res = opt.get("resource")
                allow_ctrl = opt.get("controller")
                if (
                    isinstance(allow_res, list)
                    and len(allow_res)
                    and resource_target not in allow_res
                ):
                    break
                if (
                    isinstance(allow_ctrl, list)
                    and len(allow_ctrl)
                    and controller_name not in allow_ctrl
                ):
                    break
                # 合并时排除 option 的 resource/controller 标记字段，不写入实际配置
                opt_effective = {
                    k: v for k, v in opt.items() if k not in ("resource", "controller")
                }
                controller_raw = {**opt_effective, **controller_raw}
                break

        # 首选：从"控制器子配置"读取（例如 controller_raw["Win32控制器"]["permission_required"]）
        permission_required = None
        display_short_side = None
        display_long_side = None
        display_raw = None
        if isinstance(controller_name, str) and controller_name:
            try:
                controller_cfg = controller_raw.get(controller_name)
                if isinstance(controller_cfg, dict):
                    permission_required = controller_cfg.get("permission_required")
                    display_short_side = controller_cfg.get("display_short_side")
                    display_long_side = controller_cfg.get("display_long_side")
                    display_raw = controller_cfg.get("display_raw")
            except Exception:
                permission_required = None

        # 兼容：如果配置里还没保存 permission_required（例如用户重启后未进入控制器设置界面）
        # 则从 interface.json 中按 controller_type 反查
        if permission_required is None:
            if isinstance(controller_name, str) and controller_name:
                try:
                    for ctrl in (self._runtime_interface or {}).get("controller", []):
                        if not isinstance(ctrl, dict):
                            continue
                        if ctrl.get("name") == controller_name:
                            permission_required = ctrl.get("permission_required")
                            display_short_side = ctrl.get("display_short_side")
                            display_long_side = ctrl.get("display_long_side")
                            display_raw = ctrl.get("display_raw")
                            break
                except Exception:
                    permission_required = None
                    display_short_side = None
                    display_long_side = None
                    display_raw = None

        if permission_required is True and (not self._is_admin_runtime()):
            msg = self.tr("this Controller requires admin permission to run")
            self._connect_error_reason = msg
            logger.error(msg)
            self.log_output.emit("ERROR", msg)
            # 立即停止任务流（而不是等待上层 finally）
            try:
                await self.stop_task()
            except Exception:
                pass
            return False

        try:
            controller_type = self._get_controller_type(controller_raw)
        except (TypeError, ValueError) as exc:
            msg = self.tr(
                "Controller configuration is invalid, please reset controller in settings"
            )
            self._connect_error_reason = msg
            logger.error(f"控制器配置无效: {exc}")
            self.log_output.emit("ERROR", msg)
            try:
                await self.stop_task()
            except Exception:
                pass
            return False
        # 连接阶段允许点击停止；此时不要带 device_ready，避免监控并行抢连 ADB
        self.runner_events.start_button_status.emit(
            {"text": "STOP", "status": "enabled"}
        )
        self._is_connecting_device = True
        try:
            if controller_type == "adb":
                controller = await self._connect_adb_controller(controller_raw)
            elif controller_type == "win32":
                controller = await self._connect_win32_controller(controller_raw)
            elif controller_type == "gamepad":
                controller = await self._connect_gamepad_controller(controller_raw)
            elif controller_type == "playcover":
                controller = await self._connect_playcover_controller(controller_raw)
            elif controller_type == "wlroots":
                controller = await self._connect_wlroots_controller(controller_raw)
            elif controller_type == "macos":
                controller = await self._connect_macos_controller(controller_raw)
            else:
                raise ValueError("不支持的控制器类型")
        finally:
            self._is_connecting_device = False

        if self.need_stop or not controller or not self.maafw.controller:
            return False

        if display_short_side or display_long_side:
            if display_short_side:
                self.maafw.controller.set_screenshot_target_short_side(
                    display_short_side
                )
                logger.debug(f"设置控制器分辨率: 短边 {display_short_side}")
            if display_long_side:
                self.maafw.controller.set_screenshot_target_long_side(display_long_side)
                logger.debug(f"设置控制器分辨率: 长边 {display_long_side}")
        elif display_raw:
            self.maafw.controller.set_screenshot_use_raw_size(display_raw)
            logger.debug("设置控制器分辨率: 原始大小")

        # 主任务流连上后再通知监控启动，避免与任务流并发 connect_adb（MuMu 会卡死）
        self.runner_events.start_button_status.emit(
            {"text": "STOP", "status": "enabled", "device_ready": True}
        )
        return True

    async def load_resources(self, resource_raw: Dict[str, Any]):
        """根据配置加载资源"""
        if self.maafw.resource:
            await asyncio.to_thread(self.maafw.clear_resource)

        resource_target = resource_raw.get("resource")
        resource_path = []
        resource_expected_hash = ""

        controller_cfg = self._get_task(_CONTROLLER_)
        gpu_idx = -1
        if controller_cfg:
            controller_type_raw = controller_cfg.task_option.get("controller_type", "")
            if isinstance(controller_type_raw, str):
                controller_name = controller_type_raw.strip()
            elif isinstance(controller_type_raw, dict):
                controller_name = str(
                    controller_type_raw.get("value", "") or ""
                ).strip()
            else:
                controller_name = ""
        else:
            msg = self.tr(
                "Controller config not found, please configure controller first"
            )
            logger.error("未找到控制器配置")
            self.log_output.emit("ERROR", msg)
            await self.stop_task()
            return False

        controller_entry = self._get_interface_controller_entry(controller_name)
        if not controller_entry:
            msg = self.tr(
                "Controller '{}' not found, please reset controller in settings"
            ).format(controller_name or "unknown")
            logger.error(f"未找到控制器名称: {controller_name}")
            self.log_output.emit("ERROR", msg)
            await self.stop_task()
            return False

        attach_resource_path: list = []
        raw_attach = controller_entry.get("attach_resource_path")
        if isinstance(raw_attach, list):
            attach_resource_path = [
                p for p in raw_attach if isinstance(p, str) and (p or "").strip()
            ]

        if not resource_target:
            msg = self.tr(
                "Resource target is empty, please configure resource in settings"
            )
            logger.error("未找到资源目标")
            self.log_output.emit("ERROR", msg)
            await self.stop_task()
            return False

        for resource in self._runtime_interface.get("resource", []):
            if resource["name"] == resource_target:
                logger.debug(f"加载资源: {resource['path']}")
                resource_path = resource["path"]
                resource_expected_hash = str(resource.get("hash", "") or "").strip()
                break

        if self.need_stop:
            return False

        if not resource_path:
            msg = self.tr(
                "Resource '{}' not found, please reset resource in settings"
            ).format(resource_target)
            logger.error(f"未找到目标资源: {resource_target}")
            self.log_output.emit("ERROR", msg)
            self.log_output.emit(
                "ERROR", self.tr("please try to reset resource in setting")
            )
            await self.stop_task()
            return False

        for path_item in resource_path:
            # 所有资源路径均为相对路径：优先相对于当前 bundle.path，再回落到项目根目录
            bundle_path_str = self.bundle_path or "./"

            # 先解析 bundle 基础目录为绝对路径
            bundle_base = Path(bundle_path_str)
            if not bundle_base.is_absolute():
                bundle_base = (Path.cwd() / bundle_base).resolve()

            # 兼容旧格式：移除占位符 {PROJECT_DIR}，并清理前导分隔符
            raw = str(path_item)
            raw = raw.replace("{PROJECT_DIR}", "")
            normalized = raw.lstrip("\\/")

            # 资源实际路径 = bundle 基础目录 / 相对资源路径
            resource = (bundle_base / normalized).resolve()
            if not resource.exists():
                logger.error(f"资源不存在: {resource}")
                self.log_output.emit(
                    "ERROR",
                    self.tr("Resource ")
                    + path_item
                    + self.tr(" not found in bundle: ")
                    + bundle_path_str,
                )
                self.log_output.emit(
                    "ERROR", self.tr("please try to reset resource in setting")
                )
                return False

            logger.debug(f"加载资源: {resource}")
            if not await self._precheck_resource_pipeline(resource):
                return False
            res_cfg = self._get_task(_RESOURCE_)
            gpu_idx = res_cfg.task_option.get("gpu", -1) if res_cfg else -1
            if not cfg.get(cfg.enable_gpu_acceleration):
                # -2 means forcing CPU inference mode in maafw
                gpu_idx = -2
            if not await self.maafw.load_resource(resource, gpu_idx):
                logger.error(f"资源加载失败: {resource}")
                self.log_output.emit(
                    "ERROR",
                    self.tr("Resource loading failed: {}").format(resource),
                )
                return False
            logger.debug(f"资源加载完成: {resource}")

        # v2.6.0：resource.hash 校验。hash 应为仅加载 resource.path 后的 MaaResourceGetHash 结果。
        # 不匹配时仅警告用户，不阻止继续使用，也不影响后续 attach_resource_path 加载。
        if resource_expected_hash and self.maafw.resource:
            actual_hash = getattr(self.maafw.resource, "hash", "")
            if callable(actual_hash):
                actual_hash = actual_hash()
            actual_hash = str(actual_hash or "").strip()
            if actual_hash != resource_expected_hash:
                logger.warning(
                    "资源 hash 校验不匹配: resource=%s, expected=%s, actual=%s",
                    resource_target,
                    resource_expected_hash,
                    actual_hash,
                )
                self.log_output.emit(
                    "WARNING",
                    self.tr(
                        "Resource hash mismatch, please consider redownloading the resource package."
                    ),
                )

        # v2.2.0：控制器 attach_resource_path，在 resource.path 加载完成后额外加载
        bundle_path_str = self.bundle_path or "./"
        bundle_base = Path(bundle_path_str)
        if not bundle_base.is_absolute():
            bundle_base = (Path.cwd() / bundle_base).resolve()
        for path_item in attach_resource_path:
            if self.need_stop:
                return False
            raw = str(path_item).replace("{PROJECT_DIR}", "").strip().lstrip("\\/")
            if not raw:
                continue
            resource = (bundle_base / raw).resolve()
            if not resource.exists():
                logger.warning(f"控制器附加资源不存在，已跳过: {resource}")
                continue
            logger.debug(f"加载控制器附加资源: {resource}")
            if not await self._precheck_resource_pipeline(resource):
                return False
            if not await self.maafw.load_resource(resource, gpu_idx):
                logger.error(f"控制器附加资源加载失败: {resource}")
                self.log_output.emit(
                    "ERROR",
                    self.tr("Attached resource loading failed: {}").format(resource),
                )
                return False
        return True

    async def _clear_embedded_agent_custom(self) -> None:
        """在线程池中清理 MaaResource 自定义注册，避免短暂阻塞 UI。"""
        await asyncio.to_thread(self.maafw.clear_resource_custom)

    async def _load_embedded_agent_custom(
        self,
        agent_root: Path,
        agent_entry: Path | None,
    ) -> bool:
        """在线程池中扫描并导入 embedded agent，避免阻塞 Qt 主线程。"""
        return await asyncio.to_thread(
            self.maafw.load_embedded_agent_custom,
            agent_root=agent_root,
            agent_entry=agent_entry,
        )

    async def _precheck_resource_pipeline(self, resource_dir: Path) -> bool:
        """加载前异步预检 pipeline；发现问题则输出日志并返回 False。"""
        issues = await asyncio.to_thread(check_resource_pipeline, resource_dir)
        if not issues:
            return True

        self.log_output.emit(
            "ERROR",
            self.tr("Pipeline check failed, resource loading aborted."),
        )
        for issue in issues:
            detail = format_pipeline_issue(issue)
            logger.error("pipeline 预检失败: %s", detail)
            if issue.kind == "parse_error":
                msg = self.tr("Pipeline check failed: invalid JSON in {}: {}").format(
                    issue.path, issue.message
                )
            elif issue.kind == "duplicate_in_file":
                msg = self.tr(
                    'Pipeline check failed: duplicate node "{}" in {}'
                ).format(issue.key, issue.path)
            elif issue.kind == "duplicate_across_files":
                msg = self.tr(
                    'Pipeline check failed: duplicate node "{}" in {} '
                    "(already defined in {})"
                ).format(issue.key, issue.path, issue.first_path)
            else:
                msg = self.tr("Pipeline check failed: {}").format(detail)
            self.log_output.emit("ERROR", msg)
        return False

    async def run_task(self, task_id: str, skip_speedrun: bool = False):
        """执行指定任务"""
        task = self._get_task(task_id)
        if not task:
            logger.error(f"任务 ID '{task_id}' 不存在")
            return
        # 执行层只关心"任务是否被禁用"，不展开禁用原因
        # 注意：即使 UI 未刷新也没关系，因为 run_tasks_flow 开始时会刷新一次 is_hidden；
        # 若未来有独立调用 run_task 的路径，可在此处补一次刷新。
        elif self._is_task_disabled(task):
            return
        elif not task.is_checked:
            return
        if task.is_builtin_task():
            return await self._run_builtin_task(task)
        speedrun_cfg = self._resolve_speedrun_config(task)
        # 仅依据任务自身的速通开关，不再依赖全局 speedrun_mode；单任务执行可跳过校验
        if (not skip_speedrun) and speedrun_cfg and speedrun_cfg.get("enabled"):
            condition = speedrun_cfg.get("condition", {})
            condition_type = condition.get("type", "run_count") if isinstance(condition, dict) else "run_count"
            action = speedrun_cfg.get("action", {})
            action_type = action.get("type", "normal_run") if isinstance(action, dict) else "normal_run"
            logger.debug(
                f"任务 '{task.name}' 开始速通条件评估: 条件类型={condition_type}, 执行类型={action_type}"
            )
            def _update_speedrun_task(t: TaskItem) -> None:
                self._persist_runtime_task_update(t)

            speedrun_result = evaluate_speedrun(
                task,
                speedrun_cfg,
                _update_speedrun_task,
                notify_system=lambda message: self.runner_events.focus_notification.emit(
                    str(message)
                ),
                notify_external=lambda title, text: send_all_enabled_channels(
                    str(title), str(text)
                ),
            )
            if not speedrun_result.should_run:
                self.log_output.emit(
                    "INFO",
                    self.tr("Task ")
                    + task.name
                    + self.tr(" follows speedrun limit, skipping this run: ")
                    + speedrun_result.reason,
                )
                return "skipped"
            else:
                logger.debug(
                    f"任务 '{task.name}' 速通条件评估通过, 允许执行: {speedrun_result.reason or '无条件通过'}"
                )

        raw_info = self._get_task_execution_info(task_id)
        logger.info(f"任务 '{task.name}' 的执行信息: {raw_info}")
        if raw_info is None:
            logger.error(f"无法获取任务 '{task.name}' 的执行信息")
            return

        entry = raw_info.get("entry", "") or ""
        task_pipeline_override = raw_info.get("pipeline_override", {})

        # 合并默认 override（global + resource + controller）和任务自身的 override
        # 任务 override 优先级最高，使用深度合并以正确处理嵌套字典
        import copy
        from app.core.utils.pipeline_helper import _deep_merge_dict

        pipeline_override = copy.deepcopy(self._default_pipeline_override)
        _deep_merge_dict(pipeline_override, task_pipeline_override)

        if not self.maafw.resource:
            logger.error("资源未初始化，无法执行任务")
            return

        self._cached_image_error_seen = False
        self._controller_reconnect_used = False
        self._start_task_timeout(entry)

        save_draw = cfg.get(cfg.save_screenshot)
        self.runner_events.telemetry.emit(
            {
                "event": "prepare_task",
                "name": entry,
                "display_name": task.name,
                "options": task.task_option,
            }
        )
        task_ok = await self.maafw.run_task(entry, pipeline_override, save_draw)
        if not task_ok:
            self._stop_task_timeout()
            recovery = await self._try_recover_from_cached_image_failure(
                entry, pipeline_override, save_draw
            )
            if recovery is True:
                task_ok = True
            elif recovery is False:
                return False

        if not task_ok:
            logger.error(f"任务 '{task.name}' 执行失败")
            # 发送任务失败通知
            if not self._manual_stop:
                image_bytes = await self._get_notice_screenshot_bytes()
                send_notice(
                    NoticeTiming.WHEN_TASK_FAILED,
                    self.tr("Task Failed"),
                    self.tr("Task '{}' execution failed.").format(task.name),
                    image_bytes=image_bytes,
                )
            return

        self._stop_task_timeout()
        # 仅在任务未被 abort 且正常完成时记录速通耗时
        if self._current_task_ok and speedrun_cfg and speedrun_cfg.get("enabled"):
            record_speedrun_runtime(task, speedrun_cfg, self._persist_runtime_task_update)

    async def _run_builtin_task(self, task: TaskItem):
        """执行 Core 外部模块声明的内置任务。"""
        definition = self._builtin_task_loader.get_by_key(task.builtin_key)
        if definition is None:
            logger.error("内置任务未找到或未加载: %s", task.builtin_key)
            return False

        context = self._create_builtin_task_context()
        try:
            result = await self._builtin_task_loader.execute(
                task.builtin_key,
                context,
                task.task_option if isinstance(task.task_option, dict) else {},
            )
        except Exception as exc:
            logger.error("内置任务执行异常 %s: %s", task.name, exc)
            self.log_output.emit("ERROR", self.tr("Builtin task failed: ") + str(exc))
            return False

        if result.message:
            level = "INFO" if result.success else "ERROR"
            self.log_output.emit(level, self.tr(result.message))
        return None if result.success else False

    def _create_builtin_task_context(self) -> BuiltinTaskContext:
        return BuiltinTaskContext(
            log=lambda level, text: self.log_output.emit(str(level), str(text)),
            sleep=self._builtin_sleep,
            is_stopping=lambda: bool(self.need_stop),
            notify_system=lambda message: self.runner_events.focus_notification.emit(
                str(message)
            ),
            notify_external=lambda title, text: send_all_enabled_channels(
                str(title), str(text)
            ),
            start_process=self._builtin_start_process,
            play_system_sound=self._builtin_play_system_sound,
            tr=lambda text: self._builtin_task_loader.i18n_service.translate_text(
                str(text)
            ),
        )

    async def _builtin_sleep(self, seconds: float) -> bool:
        """等待指定秒数，期间响应停止请求。"""
        try:
            remaining = max(0.0, float(seconds))
        except (TypeError, ValueError):
            remaining = 0.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + remaining
        while True:
            if self.need_stop:
                return False
            left = deadline - loop.time()
            if left <= 0:
                return True
            await asyncio.sleep(min(left, 0.2))

    async def _builtin_start_process(
        self, path: str, args: list[str] | str | None = None, wait: bool = False
    ) -> int | None:
        process = await asyncio.to_thread(self._start_process, path, args)
        if not wait:
            return None
        while process.poll() is None:
            if self.need_stop:
                try:
                    process.terminate()
                except Exception:
                    pass
                return process.poll()
            await asyncio.sleep(0.2)
        return process.returncode

    async def _builtin_play_system_sound(self) -> None:
        if sys.platform.startswith("win32"):
            try:
                import winsound

                await asyncio.to_thread(winsound.MessageBeep)
                return
            except Exception as exc:
                logger.debug("播放系统提示音失败: %s", exc)
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass

    async def stop_task(self, *, manual: bool = False, post_stop: bool = True):
        """停止当前正在运行的任务

        Args:
            manual: 是否为"手动停止"（由用户或外部调用显式触发）。
            post_stop: 是否向 Tasker 发送 post_stop。自然跑完应传 False。
        """
        if manual:
            # 在任何情况下都记录手动停止的意图，避免后续错误发送通知
            self._manual_stop = True
        already_stopping = self.need_stop
        if already_stopping and not self.maafw.has_active_runtime():
            return
        self.need_stop = True
        self._stop_task_timeout()
        self.log_output.emit("INFO", self.tr("Stopping task..."))
        self.runner_events.start_button_status.emit(
            {"text": "STOP", "status": "disabled"}
        )
        # 手动停止时截图保存到 debug/stop/（控制器仍可工作时执行）
        if manual:
            await self._save_stop_screenshot(manual=True)
        # 连接调用正在 MaaFW 工作线程中使用 controller。此时并发清理会与
        # 原生连接过程争用同一对象；只记录停止意图，由任务流在连接返回后的
        # finally 中统一清理。
        if self._is_connecting_device:
            logger.debug("设备仍在连接中，延后清理运行时")
            return
        await self.maafw.stop_task(post_stop=post_stop)
        self.runner_events.start_button_status.emit(
            {"text": "START", "status": "enabled"}
        )
        self._is_running = False
        # 写入稳定的任务流结束标记，供日志打包按"运行记录"切分（不依赖语言/文案）
        logger.info("[RUN_RECORD] TASK_FLOW_STOP manual=%s", bool(self._manual_stop))
        # 停止后恢复系统休眠
        self._restore_sleep()

    @staticmethod
    def _set_sleep_disabled() -> int | None:
        """阻止 Windows 系统进入睡眠状态。

        通过 ctypes 调用 kernel32.SetThreadExecutionState 阻止系统休眠。
        返回之前的状态句柄，用于后续恢复；非 Windows 平台返回 None。
        """
        if not sys.platform.startswith("win32"):
            return None
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
            if result:
                return result
            logger.warning("SetThreadExecutionState(阻止休眠) 返回 0")
            return None
        except Exception as exc:
            logger.warning("阻止系统休眠失败: %s", exc)
            return None

    @staticmethod
    def _restore_sleep() -> None:
        """恢复系统休眠许可（清除 ES_SYSTEM_REQUIRED 标记）。"""
        if not sys.platform.startswith("win32"):
            return
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as exc:
            logger.warning("恢复系统休眠失败: %s", exc)

    def shutdown_runtime_sync(self) -> None:
        """同步强制清理运行态，供窗口退出阶段兜底调用。"""
        self.need_stop = True
        self._manual_stop = True
        self._is_running = False
        self._stop_task_timeout()
        self.maafw.force_shutdown()
        self._restore_sleep()

    def _start_task_timeout(self, entry: str):
        """开始任务超时计时，每小时检查一次（单任务模式下不启动）"""
        # 单任务模式下不进行长期任务检查
        if self._is_single_task_mode:
            return

        entry_text = (entry or "").strip() or self.tr("Unknown Task Entry")
        # 如果entry不同，重置状态
        if entry_text != self._timeout_active_entry:
            self._timeout_active_entry = entry_text

        # 记录任务开始时间
        if self._current_running_task_id:
            self._task_start_times[self._current_running_task_id] = _time.time()

        # 每小时（3600秒）检查一次
        timeout_seconds = 3600
        self._timeout_timer.stop()
        self._timeout_timer.start(timeout_seconds * 1000)

    def _stop_task_timeout(self):
        """停止任务超时计时"""
        self._timeout_timer.stop()
        # 清除当前任务的开始时间
        if (
            self._current_running_task_id
            and self._current_running_task_id in self._task_start_times
        ):
            del self._task_start_times[self._current_running_task_id]

    def _reset_task_timeout_state(self):
        """重置任务超时状态"""
        self._timeout_timer.stop()
        self._timeout_active_entry = ""
        self._current_running_task_id = None
        # 清空所有任务开始时间记录
        self._task_start_times.clear()

    def _is_tasks_flow_completed_normally(self) -> bool:
        """判断任务流是否正常完成。"""
        return (
            not self.need_stop
            and not self._manual_stop
            and all(status != "failed" for status in self._task_results.values())
        )

    def _get_collected_logs(self) -> str:
        """获取收集到的任务日志内容"""
        if not self._log_messages:
            return ""

        # 将日志信息格式化为文本（包含收到的时间戳）
        # 格式：[时间][日志等级]日志内容
        log_text_lines: list[str] = []
        for level, text, timestamp in self._log_messages:
            translated_level = self._translate_log_level(level)
            log_text_lines.append(f"[{timestamp}][{translated_level}]{text}")

        return "\n".join(log_text_lines)

    def _on_task_timeout(self):
        """任务超时处理：每小时检查一次，如果任务运行超过1小时则发送通知（单任务模式下不执行）"""
        # 单任务模式下不进行长期任务检查
        if self._is_single_task_mode:
            self._timeout_timer.stop()
            return

        if not self._current_running_task_id:
            # 没有正在运行的任务，停止定时器
            self._timeout_timer.stop()
            return

        # 获取当前任务的开始时间
        task_start_time = self._task_start_times.get(self._current_running_task_id)
        if not task_start_time:
            # 没有开始时间记录，重新记录并继续
            self._task_start_times[self._current_running_task_id] = _time.time()
            return

        # 计算任务运行时间
        current_time = _time.time()
        elapsed_seconds = current_time - task_start_time
        elapsed_hours = elapsed_seconds / 3600

        # 如果运行时间超过1小时，发送通知
        if elapsed_hours >= 1.0:
            entry_text = self._timeout_active_entry or self.tr("Unknown Task Entry")

            # 格式化运行时间
            hours = int(elapsed_hours)
            minutes = int((elapsed_seconds % 3600) / 60)
            if hours > 0:
                time_str = self.tr("{} hours {} minutes").format(hours, minutes)
            else:
                time_str = self.tr("{} minutes").format(minutes)

            timeout_message = self.tr(
                "Task entry '{}' has been running for {}. This may indicate a problem. Please check the task status."
            ).format(entry_text, time_str)

            logger.warning(timeout_message)
            self.log_output.emit("WARNING", timeout_message)

            # 获取收集到的任务日志内容
            log_content = self._get_collected_logs()

            # 发送外部通知（类型为"任务超时"），内容为任务总结中的日志
            notice_content = log_content if log_content else timeout_message
            # 超时回调为同步，不在此处截屏；可后续改为异步调度
            send_notice(
                NoticeTiming.WHEN_TASK_TIMEOUT,
                self.tr("Task running time too long"),
                notice_content,
            )

        # 定时器会继续运行，一小时后再次检查

    async def _connect_adb_controller(self, controller_raw: Dict[str, Any]):
        """连接 ADB 控制器"""
        if not isinstance(controller_raw, dict):
            logger.error(
                f"控制器配置格式错误(ADB)，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        # 获取控制器类型和名称
        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        self.adb_controller_raw = controller_raw
        self.adb_activate_controller = activate_controller

        # 使用控制器名称作为键来获取配置（兼容旧配置：如果找不到则尝试使用控制器类型）
        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            # 兼容旧配置：迁移到控制器名称
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config
        self.adb_controller_config = controller_config

        # 提前读取并保存原始的 input_methods 和 screencap_methods
        # 仅当原配置里显式存在时才在设备发现后恢复，避免覆盖设备探测结果。
        has_raw_input_method = "input_methods" in controller_config
        has_raw_screen_method = "screencap_methods" in controller_config
        raw_input_method = int(
            controller_config.get(
                "input_methods", int(MaaAdbInputMethodEnum.Default.value)
            )
        )
        raw_screen_method = int(
            controller_config.get(
                "screencap_methods", int(MaaAdbScreencapMethodEnum.Default.value)
            )
        )

        logger.info("每次连接前自动搜索 ADB 设备...")
        self.log_output.emit("INFO", self.tr("Auto searching ADB devices..."))
        connected = await self._try_prepare_and_connect_adb(
            controller_raw=controller_raw,
            controller_type=controller_type,
            controller_name=controller_name,
            has_raw_input_method=has_raw_input_method,
            raw_input_method=raw_input_method,
            has_raw_screen_method=has_raw_screen_method,
            raw_screen_method=raw_screen_method,
            force_refresh=True,
        )
        if connected:
            # 连接成功后额外等待 5 秒，防止程序初始化未完成
            return await self._wait_after_controller_connect()
        if self.need_stop:
            return False
        if controller_config.get("emulator_path", ""):
            logger.info("尝试启动模拟器")
            self.log_output.emit("INFO", self.tr("try to start emulator"))
            emu_path = controller_config.get("emulator_path", "")
            emu_params = controller_config.get("emulator_params", "")
            wait_emu_start = int(controller_config.get("wait_time", 0))

            self.process = self._start_process(emu_path, emu_params)
            # 启动后轮询连接，连接成功则提前退出等待
            if wait_emu_start > 0:
                poll_ok = await self._poll_connect(
                    wait_emu_start,
                    self.tr("waiting for emulator start..."),
                    lambda: self._try_prepare_and_connect_adb(
                        controller_raw=controller_raw,
                        controller_type=controller_type,
                        controller_name=controller_name,
                        has_raw_input_method=has_raw_input_method,
                        raw_input_method=raw_input_method,
                        has_raw_screen_method=has_raw_screen_method,
                        raw_screen_method=raw_screen_method,
                    ),
                )
                if poll_ok:
                    # 轮询连接成功后额外等待 5 秒，防止程序初始化未完成
                    return await self._wait_after_controller_connect()
                if self.need_stop:
                    return False
            else:
                if await self._try_prepare_and_connect_adb(
                    controller_raw=controller_raw,
                    controller_type=controller_type,
                    controller_name=controller_name,
                    has_raw_input_method=has_raw_input_method,
                    raw_input_method=raw_input_method,
                    has_raw_screen_method=has_raw_screen_method,
                    raw_screen_method=raw_screen_method,
                ):
                    # 启动模拟器后首次直接连接成功时，额外等待 5 秒
                    return await self._wait_after_controller_connect()
        self.log_output.emit("ERROR", self.tr("Device connection failed"))
        return False

    async def _connect_win32_controller(self, controller_raw: Dict[str, Any]):
        """连接 Win32 控制器"""
        # 验证平台：Win32 只在 Windows 上支持
        if sys.platform != "win32":
            error_msg = self.tr("Win32 controller is only supported on Windows")
            logger.error("Win32 控制器仅在 Windows 上支持")
            self.log_output.emit("ERROR", error_msg)
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        # 获取控制器类型和名称
        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        # 使用控制器名称作为键来获取配置（兼容旧配置：如果找不到则尝试使用控制器类型）
        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            # 兼容旧配置：迁移到控制器名称
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config

        # 提前读取并保存原始的配置值
        raw_screencap_method = controller_config.get("win32_screencap_methods")
        raw_mouse_method = controller_config.get("mouse_input_methods")
        raw_keyboard_method = controller_config.get("keyboard_input_methods")

        def _restore_raw_methods():
            if raw_screencap_method is not None:
                controller_config["win32_screencap_methods"] = raw_screencap_method
            if raw_mouse_method is not None:
                controller_config["mouse_input_methods"] = raw_mouse_method
            if raw_keyboard_method is not None:
                controller_config["keyboard_input_methods"] = raw_keyboard_method

        interface_entry = self._get_interface_controller_entry(controller_name) or {}
        win32_cfg = interface_entry.get("win32")
        if not isinstance(win32_cfg, dict):
            win32_cfg = {}

        def _collect_win32_params():
            hwnd_raw = controller_config.get("hwnd", 0)
            try:
                hwnd_value = int(hwnd_raw)
            except (TypeError, ValueError):
                hwnd_value = 0
            # 配置缺失/0/-1 时回退到 interface.json 的 screencap/mouse/keyboard。
            # 旧实现把截图默认成 GDI(1)，Unity 窗口会一直停在第一帧。
            screencap = resolve_win32_screencap_method(
                raw_screencap_method
                if raw_screencap_method is not None
                else controller_config.get("win32_screencap_methods"),
                fallback=win32_cfg.get("screencap"),
            )
            mouse = resolve_win32_input_method(
                raw_mouse_method
                if raw_mouse_method is not None
                else controller_config.get("mouse_input_methods"),
                fallback=(
                    win32_cfg.get("mouse")
                    or win32_cfg.get("mouse_input")
                    or win32_cfg.get("input")
                ),
            )
            keyboard = resolve_win32_input_method(
                raw_keyboard_method
                if raw_keyboard_method is not None
                else controller_config.get("keyboard_input_methods"),
                fallback=(
                    win32_cfg.get("keyboard")
                    or win32_cfg.get("keyboard_input")
                    or win32_cfg.get("input")
                ),
            )
            return hwnd_value, screencap, mouse, keyboard

        logger.info("每次连接前自动搜索 Win32 窗口...")
        self.log_output.emit("INFO", self.tr("Auto searching Win32 windows..."))
        found_device = await self._auto_find_win32_window(
            controller_raw, controller_type, controller_name, controller_config
        )
        if found_device:
            self._save_device_to_config(controller_raw, controller_name, found_device)
            controller_config = controller_raw[controller_name]
            _restore_raw_methods()
            hwnd, screencap_method, mouse_method, keyboard_method = (
                _collect_win32_params()
            )
            if not hwnd:
                error_msg = self.tr(
                    "Window handle (hwnd) is empty, please configure window connection in settings"
                )
                logger.error("Win32 窗口句柄为空")
                self.log_output.emit("ERROR", error_msg)
                self._emit_controller_setup_hint(
                    reason="win32_hwnd_empty",
                    controller_name=controller_name,
                )
                return False

            # 需求：如果已搜索到窗口，则直接尝试连接并返回成功/失败（不再启动程序兜底）
            logger.info(
                "Win32 连接参数: hwnd=%s screencap=%s mouse=%s keyboard=%s",
                hwnd,
                screencap_method,
                mouse_method,
                keyboard_method,
            )
            connect_success = await self.maafw.connect_win32hwnd(
                hwnd,
                screencap_method,
                mouse_method,
                keyboard_method,
            )
            if not connect_success:
                self.log_output.emit("ERROR", self.tr("Device connection failed"))
            return bool(connect_success)

        # 需求：首次未搜索到窗口时，才检查是否配置了启动程序路径
        program_path = (controller_config.get("program_path") or "").strip()
        if not program_path:
            logger.error("Win32 控制器未匹配窗口且未配置启动程序")
            self.log_output.emit("ERROR", self.tr("Device connection failed"))
            self._emit_controller_setup_hint(
                reason="win32_program_path_empty",
                controller_name=controller_name,
            )
            return False

        # 启动程序+参数，轮询搜索窗口并连接
        self.log_output.emit("INFO", self.tr("try to start program"))
        logger.info("尝试启动程序")
        program_params = controller_config.get("program_params", "")
        wait_program_start = int(controller_config.get("wait_time", 0))
        self.process = self._start_process(program_path, program_params)

        async def _try_find_and_connect_win32():
            nonlocal controller_config
            found = await self._auto_find_win32_window(
                controller_raw, controller_type, controller_name, controller_config
            )
            if not found:
                return False
            self._save_device_to_config(controller_raw, controller_name, found)
            controller_config = controller_raw[controller_name]
            _restore_raw_methods()
            hwnd, screencap_method, mouse_method, keyboard_method = (
                _collect_win32_params()
            )
            if not hwnd:
                return False
            logger.info(
                "Win32 连接参数: hwnd=%s screencap=%s mouse=%s keyboard=%s",
                hwnd,
                screencap_method,
                mouse_method,
                keyboard_method,
            )
            return await self.maafw.connect_win32hwnd(
                hwnd, screencap_method, mouse_method, keyboard_method
            )

        if wait_program_start > 0:
            poll_ok = await self._poll_connect(
                wait_program_start,
                self.tr("waiting for program start..."),
                _try_find_and_connect_win32,
            )
            if poll_ok:
                return True
            if self.need_stop:
                return False
        else:
            if await _try_find_and_connect_win32():
                return True

        logger.error("启动程序后未找到与配置匹配的 Win32 窗口")
        self.log_output.emit("ERROR", self.tr("Device connection failed"))
        return False

    async def _connect_gamepad_controller(self, controller_raw: Dict[str, Any]):
        """连接 Gamepad 控制器（复用 Win32 的窗口查找能力，但连接逻辑独立）"""
        # 验证平台：Gamepad 只在 Windows 上支持
        if sys.platform != "win32":
            error_msg = self.tr("Gamepad controller is only supported on Windows")
            logger.error("Gamepad 控制器仅在 Windows 上支持")
            self.log_output.emit("ERROR", error_msg)
            return False

        if not isinstance(controller_raw, dict):
            logger.error(
                f"控制器配置格式错误(Gamepad)，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        # 获取控制器类型和名称
        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        # 使用控制器名称作为键来获取配置（兼容旧配置：如果找不到则尝试使用控制器类型）
        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config

        def _collect_gamepad_params():
            hwnd_raw = controller_config.get("hwnd", 0)
            try:
                hwnd_value = int(hwnd_raw)
            except (TypeError, ValueError):
                hwnd_value = 0
            gamepad_type_raw = controller_config.get("gamepad_type", 0)
            try:
                gamepad_type_value = int(gamepad_type_raw)
            except (TypeError, ValueError):
                gamepad_type_value = 0
            return hwnd_value, gamepad_type_value

        # 优先用 interface.json 里的默认截图方式（可选）
        def _resolve_gamepad_screencap_method() -> int:
            entry = self._get_interface_controller_entry(controller_name) or {}
            gamepad_cfg = entry.get("gamepad") or {}
            raw = gamepad_cfg.get("screencap")
            if isinstance(raw, int):
                return raw
            if raw and isinstance(raw, str):
                return int(MaaWin32ScreencapMethodEnum[raw].value)
            return int(MaaWin32ScreencapMethodEnum.DXGI_DesktopDup.value)

        screencap_method = _resolve_gamepad_screencap_method()

        logger.info("每次连接前自动搜索 Gamepad 窗口...")
        self.log_output.emit("INFO", self.tr("Auto searching desktop windows..."))
        found_device = await self._auto_find_win32_window(
            controller_raw, controller_type, controller_name, controller_config
        )
        if found_device:
            self._save_device_to_config(controller_raw, controller_name, found_device)
            controller_config = controller_raw[controller_name]
            hwnd, gamepad_type = _collect_gamepad_params()
            if not hwnd:
                error_msg = self.tr(
                    "Window handle (hwnd) is empty, please configure window connection in settings"
                )
                logger.error("Gamepad 窗口句柄为空")
                self.log_output.emit("ERROR", error_msg)
                self._emit_controller_setup_hint(
                    reason="gamepad_hwnd_empty",
                    controller_name=controller_name,
                )
                return False

            connect_success = await self.maafw.connect_gamepad(
                hwnd, gamepad_type, screencap_method
            )
            if not connect_success:
                self.log_output.emit("ERROR", self.tr("Device connection failed"))
            return bool(connect_success)

        # 若未搜索到窗口时，才检查是否配置了启动程序路径
        program_path = (controller_config.get("program_path") or "").strip()
        if not program_path:
            logger.error("Gamepad 控制器未匹配窗口且未配置启动程序")
            self.log_output.emit("ERROR", self.tr("Device connection failed"))
            self._emit_controller_setup_hint(
                reason="gamepad_program_path_empty",
                controller_name=controller_name,
            )
            return False

        self.log_output.emit("INFO", self.tr("try to start program"))
        logger.info("尝试启动程序")
        program_params = controller_config.get("program_params", "")
        wait_program_start = int(controller_config.get("wait_time", 0))
        self.process = self._start_process(program_path, program_params)
        async def _try_find_and_connect_gamepad():
            nonlocal controller_config
            found = await self._auto_find_win32_window(
                controller_raw, controller_type, controller_name, controller_config
            )
            if not found:
                return False
            self._save_device_to_config(controller_raw, controller_name, found)
            controller_config = controller_raw[controller_name]
            hwnd, gamepad_type = _collect_gamepad_params()
            if not hwnd:
                return False
            return await self.maafw.connect_gamepad(
                hwnd, gamepad_type, screencap_method
            )

        if wait_program_start > 0:
            poll_ok = await self._poll_connect(
                wait_program_start,
                self.tr("waiting for program start..."),
                _try_find_and_connect_gamepad,
            )
            if poll_ok:
                return True
            if self.need_stop:
                return False
        else:
            if await _try_find_and_connect_gamepad():
                return True

        logger.error("启动程序后未找到与配置匹配的窗口")
        self.log_output.emit("ERROR", self.tr("Device connection failed"))
        return False

    async def _connect_playcover_controller(self, controller_raw: Dict[str, Any]):
        """连接 PlayCover 控制器"""
        # 验证平台：PlayCover 只在 macOS 上支持
        if sys.platform != "darwin":
            error_msg = self.tr("PlayCover controller is only supported on macOS")
            logger.error("PlayCover 控制器仅在 macOS 上支持")
            self.log_output.emit("ERROR", error_msg)
            return False

        if not isinstance(controller_raw, dict):
            logger.error(
                f"控制器配置格式错误(PlayCover)，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        # 获取控制器类型和名称
        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        # 使用控制器名称作为键来获取配置（兼容旧配置：如果找不到则尝试使用控制器类型）
        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            # 兼容旧配置：迁移到控制器名称
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config

        # 从配置中读取 uuid 和 address
        uuid = controller_config.get("uuid", "")
        address = controller_config.get("address", "")

        # 检查 uuid 和 address 是否为空
        if not uuid:
            error_msg = self.tr(
                "PlayCover UUID is empty, please configure UUID in settings"
            )
            logger.error("PlayCover UUID 为空")
            self.log_output.emit("ERROR", error_msg)
            self._emit_controller_setup_hint(
                reason="playcover_uuid_empty",
                controller_name=controller_name,
            )
            return False

        if not address:
            error_msg = self.tr(
                "PlayCover connection address is empty, please configure address in settings"
            )
            logger.error("PlayCover 连接地址为空")
            self.log_output.emit("ERROR", error_msg)
            self._emit_controller_setup_hint(
                reason="playcover_address_empty",
                controller_name=controller_name,
            )
            return False

        logger.debug(f"PlayCover 参数: uuid={uuid}, address={address}")

        logger.info(f"正在连接 PlayCover: {address} (UUID: {uuid})")
        msg = self.tr("Connecting to PlayCover: {address} (UUID: {uuid})").format(
            address=address,
            uuid=uuid,
        )
        self.log_output.emit("INFO", msg)

        if await self.maafw.connect_playcover(address, uuid):
            logger.info("PlayCover 连接成功")
            self.log_output.emit(
                "INFO", self.tr("PlayCover connected successfully")
            )
            return True
        else:
            error_msg = self.tr("Failed to connect to PlayCover")
            logger.error("PlayCover 连接失败")
            self.log_output.emit("ERROR", error_msg)
            return False

    async def _connect_wlroots_controller(self, controller_raw: Dict[str, Any]):
        """连接 WlRoots 控制器"""
        if not sys.platform.startswith("linux"):
            error_msg = self.tr("WlRoots controller is only supported on Linux")
            logger.error("WlRoots 控制器仅在 Linux 上支持")
            self.log_output.emit("ERROR", error_msg)
            return False

        if not isinstance(controller_raw, dict):
            logger.error(
                f"控制器配置格式错误(WlRoots)，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config

        wlr_socket_path = (controller_config.get("wlr_socket_path") or "").strip()
        if not wlr_socket_path:
            error_msg = self.tr(
                "WlRoots Wayland socket path is empty, please configure it in settings"
            )
            logger.error("WlRoots Wayland Socket 路径为空")
            self.log_output.emit("ERROR", error_msg)
            self._emit_controller_setup_hint(
                reason="wlroots_socket_empty",
                controller_name=controller_name,
            )
            return False

        controller_entry = self._get_interface_controller_entry(controller_name) or {}
        wlroots_cfg = controller_entry.get("wlroots") or {}
        use_win32_vk_code = bool(wlroots_cfg.get("use_win32_vk_code", False))

        logger.info(
            f"正在连接 WlRoots: socket={wlr_socket_path}, use_win32_vk_code={use_win32_vk_code}"
        )
        msg = self.tr("Connecting to WlRoots: {socket}").format(socket=wlr_socket_path)
        self.log_output.emit("INFO", msg)

        if await self.maafw.connect_wlroots(wlr_socket_path, use_win32_vk_code):
            logger.info("WlRoots 连接成功")
            self.log_output.emit("INFO", self.tr("WlRoots connected successfully"))
            return True

        logger.error("WlRoots 连接失败")
        self.log_output.emit("ERROR", self.tr("Failed to connect to WlRoots"))
        return False

    async def _connect_macos_controller(self, controller_raw: Dict[str, Any]):
        """连接 MacOS 控制器"""
        if sys.platform != "darwin":
            error_msg = self.tr("MacOS controller is only supported on macOS")
            logger.error("MacOS 控制器仅在 macOS 上支持")
            self.log_output.emit("ERROR", error_msg)
            return False

        activate_controller = controller_raw.get("controller_type")
        if activate_controller is None:
            logger.error(f"未找到控制器配置: {controller_raw}")
            return False

        controller_type = self._get_controller_type(controller_raw)
        controller_name = self._get_controller_name(controller_raw)

        if controller_name in controller_raw:
            controller_config = controller_raw[controller_name]
        elif controller_type in controller_raw:
            controller_config = controller_raw[controller_type]
            controller_raw[controller_name] = controller_config
        else:
            controller_config = {}
            controller_raw[controller_name] = controller_config

        raw_screencap_method = controller_config.get("macos_screencap_methods")
        raw_input_method = controller_config.get("macos_input_methods")

        def _restore_raw_methods():
            if raw_screencap_method is not None:
                controller_config["macos_screencap_methods"] = raw_screencap_method
            if raw_input_method is not None:
                controller_config["macos_input_methods"] = raw_input_method

        def _collect_macos_params():
            window_id_raw = controller_config.get("window_id") or controller_config.get(
                "hwnd", 0
            )
            try:
                window_id_value = int(window_id_raw)
            except (TypeError, ValueError):
                window_id_value = 0
            screencap = (
                raw_screencap_method
                if raw_screencap_method is not None
                else controller_config.get("macos_screencap_methods")
            )
            input_method = (
                raw_input_method
                if raw_input_method is not None
                else controller_config.get("macos_input_methods")
            )

            def _safe_int(value: Any) -> int | None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            if _safe_int(screencap) in (None, 0):
                screencap = int(MaaMacOSScreencapMethodEnum.ScreenCaptureKit.value)
            if _safe_int(input_method) in (None, 0):
                input_method = int(MaaMacOSInputMethodEnum.GlobalEvent.value)
            return window_id_value, int(screencap), int(input_method)

        logger.info("每次连接前自动搜索 MacOS 窗口...")
        self.log_output.emit("INFO", self.tr("Auto searching MacOS windows..."))
        found_device = await self._auto_find_macos_window(
            controller_raw, controller_name, controller_config
        )
        if found_device:
            self._save_device_to_config(controller_raw, controller_name, found_device)
            controller_config = controller_raw[controller_name]
            _restore_raw_methods()
            window_id, screencap_method, input_method = _collect_macos_params()
            if not window_id:
                error_msg = self.tr(
                    "Window ID is empty, please configure window connection in settings"
                )
                logger.error("MacOS 窗口 ID 为空")
                self.log_output.emit("ERROR", error_msg)
                self._emit_controller_setup_hint(
                    reason="macos_window_id_empty",
                    controller_name=controller_name,
                )
                return False

            connect_success = await self.maafw.connect_macos(
                window_id, screencap_method, input_method
            )
            if not connect_success:
                self.log_output.emit("ERROR", self.tr("Device connection failed"))
            return bool(connect_success)

        program_path = (controller_config.get("program_path") or "").strip()
        if not program_path:
            logger.error("MacOS 控制器未匹配窗口且未配置启动程序")
            self.log_output.emit("ERROR", self.tr("Device connection failed"))
            self._emit_controller_setup_hint(
                reason="macos_program_path_empty",
                controller_name=controller_name,
            )
            return False

        self.log_output.emit("INFO", self.tr("try to start program"))
        logger.info("尝试启动程序")
        program_params = controller_config.get("program_params", "")
        wait_program_start = int(controller_config.get("wait_time", 0))
        self.process = self._start_process(program_path, program_params)

        async def _try_find_and_connect_macos():
            nonlocal controller_config
            found = await self._auto_find_macos_window(
                controller_raw, controller_name, controller_config
            )
            if not found:
                return False
            self._save_device_to_config(controller_raw, controller_name, found)
            controller_config = controller_raw[controller_name]
            _restore_raw_methods()
            window_id, screencap_method, input_method = _collect_macos_params()
            if not window_id:
                return False
            return await self.maafw.connect_macos(
                window_id, screencap_method, input_method
            )

        if wait_program_start > 0:
            poll_ok = await self._poll_connect(
                wait_program_start,
                self.tr("waiting for program start..."),
                _try_find_and_connect_macos,
            )
            if poll_ok:
                return True
            if self.need_stop:
                return False
        else:
            if await _try_find_and_connect_macos():
                return True

        logger.error("启动程序后未找到与配置匹配的 MacOS 窗口")
        self.log_output.emit("ERROR", self.tr("Device connection failed"))
        return False

    def _parse_address_components(self, address: str | None) -> tuple[str, str | None]:
        """提取 ADB 地址和端口"""
        raw_address = (address or "").strip()
        if not raw_address:
            return "", None
        if ":" in raw_address:
            host, port = raw_address.rsplit(":", 1)
            return host.strip(), port.strip() or None
        return raw_address, None

    def _extract_device_base_name(self, device_name: str) -> str:
        """从设备名称中提取基础名称

        例如：
        - "雷电模拟器-LDPlayer[0](emulator-5554)" -> "雷电模拟器-LDPlayer[0]"
        - "MuMu模拟器(127.0.0.1:7555)" -> "MuMu模拟器"
        - "雷电模拟器-LDPlayer[0]" -> "雷电模拟器-LDPlayer[0]"
        """
        # 只去掉 (address) 部分，保留 [index] 部分
        # 匹配格式：name[index](address) 或 name(address) 或 name[index]
        pattern = r"^(.+?)(?:\(.*?\))?$"
        match = re.match(pattern, device_name.strip())
        if match:
            return match.group(1).strip()
        return device_name.strip()

    def _extract_device_family_name(self, device_name: str) -> str:
        """提取忽略实例序号后的设备家族名称，用于雷电等动态 pid 场景匹配。"""
        base_name = self._extract_device_base_name(device_name)
        return re.sub(r"\[\d+\]", "", base_name).strip()

    def _get_ld_extras(self, device_config: Dict[str, Any] | None) -> Dict[str, Any]:
        """获取配置中的雷电 extras 信息。"""
        if not isinstance(device_config, dict):
            return {}
        extras = device_config.get("config", {}).get("extras", {})
        ld_config = extras.get("ld")
        return ld_config if isinstance(ld_config, dict) else {}

    def _uses_ld_extras(self, device_config: Dict[str, Any] | None) -> bool:
        """判断当前配置是否启用了雷电 extras。"""
        return bool(self._get_ld_extras(device_config))

    async def _try_prepare_and_connect_adb(
        self,
        controller_raw: Dict[str, Any],
        controller_type: str,
        controller_name: str,
        has_raw_input_method: bool,
        raw_input_method: int,
        has_raw_screen_method: bool,
        raw_screen_method: int,
        force_refresh: bool = False,
    ) -> bool:
        """执行一次 ADB 连接尝试，必要时先重新搜索并刷新设备配置。"""
        controller_config = controller_raw.get(controller_name)
        if not isinstance(controller_config, dict):
            if controller_type in controller_raw and isinstance(
                controller_raw.get(controller_type), dict
            ):
                controller_config = controller_raw[controller_type]
                controller_raw[controller_name] = controller_config
            else:
                controller_config = {}
                controller_raw[controller_name] = controller_config

        self.adb_controller_config = controller_config
        should_refresh = force_refresh or self._uses_ld_extras(controller_config)
        found_device = None
        if should_refresh:
            found_device = await self._auto_find_adb_device(
                controller_raw, controller_type, controller_config
            )
            if found_device:
                # 自动搜索会带回设备默认的截图/输入方式（如蓝叠 EmulatorExtras=64/8）。
                # 若用户已在高级设置中显式配置，则不要覆盖，否则重启后连接会失败。
                device_to_save = dict(found_device)
                if has_raw_input_method:
                    device_to_save.pop("input_methods", None)
                if has_raw_screen_method:
                    device_to_save.pop("screencap_methods", None)
                self._save_device_to_config(
                    controller_raw, controller_name, device_to_save
                )
                controller_config = controller_raw[controller_name]
                self.adb_controller_config = controller_config

        if has_raw_input_method:
            controller_config["input_methods"] = raw_input_method
        if has_raw_screen_method:
            controller_config["screencap_methods"] = raw_screen_method
        # 设备刷新后再次写回用户截图/输入方式，确保磁盘配置与本次连接一致
        if found_device and (has_raw_input_method or has_raw_screen_method):
            if controller_cfg := self._get_task(_CONTROLLER_):
                controller_cfg.task_option.update(controller_raw)
                self._persist_runtime_task_update(controller_cfg)

        adb_path = (controller_config.get("adb_path", "") or "").strip()
        raw_address = (controller_config.get("address", "") or "").strip()

        if not adb_path:
            error_msg = self.tr(
                "ADB path is empty, please configure ADB path in settings"
            )
            logger.error("ADB 路径为空")
            self.log_output.emit("ERROR", error_msg)
            self._emit_controller_setup_hint(
                reason="adb_path_empty",
                controller_name=controller_name,
            )
            return False

        address_missing = not raw_address
        port_missing = False
        if not address_missing and ":" in raw_address:
            _, port_part = raw_address.rsplit(":", 1)
            port_missing = not port_part.strip()

        if address_missing or port_missing:
            if address_missing:
                error_msg = self.tr(
                    "ADB connection address is empty, please configure device connection in settings"
                )
                logger.error("ADB 连接地址为空")
                self._emit_controller_setup_hint(
                    reason="adb_address_empty",
                    controller_name=controller_name,
                )
            else:
                error_msg = self.tr(
                    "ADB connection port is empty; use host:port (for example 127.0.0.1:5555) or pick a device after starting the emulator."
                )
                logger.error("ADB 连接端口为空")
                self._emit_controller_setup_hint(
                    reason="adb_port_empty",
                    controller_name=controller_name,
                )
            self.log_output.emit("ERROR", error_msg)
            return False

        address = raw_address

        def normalize_input_method(value: int) -> int:
            mask = (1 << 64) - 1
            value &= mask
            if value & (1 << 63):
                value -= 1 << 64
            return value

        input_method = normalize_input_method(raw_input_method)
        screen_method = normalize_input_method(raw_screen_method)
        config = controller_config.get("config", {})

        if self.need_stop:
            return False
        return await self.maafw.connect_adb(
            adb_path,
            address,
            screen_method,
            input_method,
            config,
        )

    def _should_use_new_adb_device(
        self,
        old_config: Dict[str, Any],
        new_device: Dict[str, Any] | None,
    ) -> bool:
        """判断自动搜索到的 ADB 设备是否和旧配置一致"""
        if not new_device:
            return False

        old_adb_path = (old_config.get("adb_path") or "").strip()
        new_adb_path = (new_device.get("adb_path") or "").strip()

        old_name = self._extract_device_base_name(old_config.get("device_name") or "")
        new_name = self._extract_device_base_name(new_device.get("device_name") or "")

        # 如果旧配置中 adb_path 或 device_name 为空，则使用新配置
        if not old_adb_path or not old_name:
            return True

        adb_path_match = old_adb_path == new_adb_path
        if not adb_path_match:
            return False

        if old_name == new_name:
            return True

        old_ld = self._get_ld_extras(old_config)
        new_ld = self._get_ld_extras(new_device)
        if old_ld or new_ld:
            old_family = self._extract_device_family_name(old_config.get("device_name") or "")
            new_family = self._extract_device_family_name(new_device.get("device_name") or "")
            if old_family and new_family and old_family == new_family:
                old_ld_path = str(old_ld.get("path") or "").strip().lower()
                new_ld_path = str(new_ld.get("path") or "").strip().lower()
                if old_ld_path and new_ld_path and old_ld_path != new_ld_path:
                    return False
                return True

        return False

    def _should_use_new_win32_window(
        self,
        old_config: Dict[str, Any],
        new_device: Dict[str, Any] | None,
    ) -> bool:
        """判断自动搜索到的 Win32 窗口是否属于旧配置"""
        if not new_device:
            return False

        old_name = (old_config.get("device_name") or "").strip()
        new_name = (new_device.get("device_name") or "").strip()

        # 如果旧配置没有设备名，只要有新设备名就使用
        if not old_name:
            return bool(new_name)
        # 如果旧配置有设备名，需要新设备名存在且与旧配置匹配
        elif new_name:
            return old_name == new_name
        else:
            return False

    def _get_interface_controller_entry(
        self, controller_name: str
    ) -> Dict[str, Any] | None:
        """根据控制器名称查找 interface 中的控制器定义"""
        if not controller_name:
            return None
        controller_lower = controller_name.strip().lower()
        for controller in self._runtime_interface.get("controller", []):
            if controller.get("name", "").lower() == controller_lower:
                return controller
        return None

    def _compile_win32_regex(
        self, pattern: str | None, label: str
    ) -> re.Pattern | None:
        """编译 Win32 过滤正则，失败时返回 None"""
        if not pattern:
            return None
        try:
            return re.compile(pattern)
        except re.error as exc:
            logger.warning(f"Win32 {label} 过滤正则编译失败: {exc}")
            return None

    def _get_win32_filter_patterns(
        self, controller_name: str
    ) -> tuple[re.Pattern | None, re.Pattern | None]:
        """从 interface 中提取 Win32 过滤正则"""
        controller_entry = self._get_interface_controller_entry(controller_name)
        if not controller_entry:
            return None, None
        win32_cfg = controller_entry.get("win32") or {}
        return (
            self._compile_win32_regex(win32_cfg.get("class_regex"), "类名"),
            self._compile_win32_regex(win32_cfg.get("window_regex"), "窗口名"),
        )

    def _get_gamepad_filter_patterns(
        self, controller_name: str
    ) -> tuple[re.Pattern | None, re.Pattern | None]:
        """从 interface 中提取 Gamepad 过滤正则（复用 Win32 的 regex 编译逻辑）"""
        controller_entry = self._get_interface_controller_entry(controller_name)
        if not controller_entry:
            return None, None
        gamepad_cfg = controller_entry.get("gamepad") or {}
        return (
            self._compile_win32_regex(gamepad_cfg.get("class_regex"), "类名"),
            self._compile_win32_regex(gamepad_cfg.get("window_regex"), "窗口名"),
        )

    def _get_macos_filter_pattern(self, controller_name: str) -> re.Pattern | None:
        """从 interface 中提取 MacOS 窗口标题过滤正则"""
        controller_entry = self._get_interface_controller_entry(controller_name)
        if not controller_entry:
            return None
        macos_cfg = controller_entry.get("macos") or {}
        return self._compile_win32_regex(macos_cfg.get("title_regex"), "窗口标题")

    def _window_matches_win32_filters(
        self,
        window_info: Dict[str, Any],
        class_pattern: re.Pattern | None,
        window_pattern: re.Pattern | None,
    ) -> bool:
        """检查窗口是否满足 Win32 过滤正则（类名+窗口名）"""
        if not class_pattern and not window_pattern:
            return True

        class_name = str(window_info.get("class_name") or "")
        window_name = str(window_info.get("window_name") or "")
        class_match = bool(class_pattern.search(class_name)) if class_pattern else True
        window_match = (
            bool(window_pattern.search(window_name)) if window_pattern else True
        )
        return class_match and window_match

    def _strip_bracket_content(self, text: str) -> str:
        """去除字符串中括号及括号内内容，用于窗口标题匹配消歧。

        例：
        - "雷电模拟器(123456)" -> "雷电模拟器"
        - "Foo（bar）[baz]" -> "Foo"
        """
        if not text:
            return ""
        # 支持英文/中文圆括号、方括号、中文方括号
        pattern = r"[\(\（\[\【].*?[\)\）\]\】]"
        return re.sub(pattern, "", str(text)).strip()

    def _start_process(
        self, entry: str | Path, argv: list[str] | tuple[str, ...] | str | None = None
    ) -> subprocess.Popen:
        """根据入口路径/命令开启子进程，返回 Popen 对象"""
        command = [str(entry)]
        if argv is not None:
            import shlex

            if isinstance(argv, (list, tuple)):
                # If argv is already a list/tuple, just append the arguments directly
                # Don't split them again as they're already parsed
                command.extend(str(arg) for arg in argv)
            else:
                # If argv is a string, split it properly
                command.extend(shlex.split(str(argv)))

        logger.debug(f"准备启动子进程: {command}")
        return subprocess.Popen(command)

    async def _wait_after_controller_connect(self, seconds: float = 5.0) -> bool:
        """连接成功后等待初始化，可被 need_stop 中断。"""
        intervals = max(1, int(seconds * 10))
        for _ in range(intervals):
            if self.need_stop:
                return False
            await asyncio.sleep(seconds / intervals)
        return True

    async def _poll_connect(
        self,
        wait_seconds: int,
        message: str,
        connect_coro_fn,
        retry_interval: int = 3,
    ) -> bool:
        """启动程序后按原倒计时规则等待，同时周期性尝试连接，连接成功则提前返回 True。

        :param connect_coro_fn: 无参可调用对象，返回 awaitable，结果为 True 表示连接成功
        :param retry_interval: 每次连接失败后等待的秒数
        """
        if wait_seconds <= 0:
            return False

        thresholds = [60, 30, 15, 10, 5, 4, 3, 2, 1]
        log_points = {wait_seconds}
        for point in thresholds:
            if wait_seconds >= point:
                log_points.add(point)

        since_last_try = retry_interval  # 首次立即尝试

        for remaining in range(wait_seconds, 0, -1):
            if remaining in log_points:
                self.log_output.emit(
                    "INFO",
                    message + str(remaining) + self.tr(" seconds"),
                )
            if self.need_stop:
                return False

            since_last_try += 1
            if since_last_try >= retry_interval:
                since_last_try = 0
                try:
                    if await connect_coro_fn():
                        self.log_output.emit(
                            "INFO", self.tr("Device connected successfully")
                        )
                        return True
                except Exception:
                    pass

            await asyncio.sleep(1)

        # 最后再尝试一次
        try:
            if await connect_coro_fn():
                self.log_output.emit(
                    "INFO", self.tr("Device connected successfully")
                )
                return True
        except Exception:
            pass

        return False

    async def _countdown_wait(self, wait_seconds: int, message: str) -> bool:
        """按指定阈值输出倒计时日志，返回 False 表示提前停止"""

        if wait_seconds <= 0:
            return True

        thresholds = [60, 30, 15, 10, 5, 4, 3, 2, 1]
        log_points = {wait_seconds}
        for point in thresholds:
            if wait_seconds >= point:
                log_points.add(point)

        for remaining in range(wait_seconds, 0, -1):
            if remaining in log_points:
                self.log_output.emit(
                    "INFO",
                    message + str(remaining) + self.tr(" seconds"),
                )
                log_points.remove(remaining)
            if self.need_stop:
                return False
            await asyncio.sleep(1)
        return True

    def _get_controller_name(self, controller_raw: Dict[str, Any]) -> str:
        """获取控制器名称"""
        if not isinstance(controller_raw, dict):
            raise TypeError(
                f"controller_raw 类型错误，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )

        controller_config = controller_raw.get("controller_type", {})
        if isinstance(controller_config, str):
            controller_name = controller_config
        elif isinstance(controller_config, dict):
            controller_name = controller_config.get("value", "")
        else:
            controller_name = ""

        # 验证控制器名称是否存在
        controller_name_lower = controller_name.lower()
        for controller in self._runtime_interface.get("controller", []):
            if controller.get("name", "").lower() == controller_name_lower:
                return controller.get("name", "")

        raise ValueError(f"未找到控制器名称: {controller_raw}")

    def _get_controller_type(self, controller_raw: Dict[str, Any]) -> str:
        """获取控制器类型"""
        if not isinstance(controller_raw, dict):
            raise TypeError(
                f"controller_raw 类型错误，期望 dict，实际 {type(controller_raw)}: {controller_raw}"
            )

        controller_config = controller_raw.get("controller_type", {})
        if isinstance(controller_config, str):
            controller_name = controller_config
        elif isinstance(controller_config, dict):
            controller_name = controller_config.get("value", "")
        else:
            controller_name = ""

        controller_name = controller_name.lower()
        for controller in self._runtime_interface.get("controller", []):
            if controller.get("name", "").lower() == controller_name:
                return controller.get("type", "").lower()

        raise ValueError(f"未找到控制器类型: {controller_raw}")

    async def _auto_find_adb_device(
        self,
        controller_raw: Dict[str, Any],
        controller_type: str,
        controller_config: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """自动搜索 ADB 设备并找到与旧配置一致的那一项"""
        try:
            if self.need_stop:
                return None

            # Toolkit 的设备探测是同步阻塞调用。模拟器未启动时，ADB 探测可能
            # 长时间等待；放到工作线程中，避免阻塞 Qt/asyncio 主事件循环，
            # 使连接阶段的“停止”操作仍能及时响应。
            devices = await asyncio.to_thread(Toolkit.find_adb_devices)
            if self.need_stop:
                return None
            if not devices:
                logger.warning("未找到任何 ADB 设备")
                return None

            all_device_infos = []
            for device in devices:
                if self.need_stop:
                    return None
                # 优先使用设备自身的 pid，如果没有则使用配置中的 pid
                device_ld_pid = (
                    (
                        (device.config or {})
                        if hasattr(device, "config") and isinstance(device.config, dict)
                        else {}
                    )
                    .get("extras", {})
                    .get("ld", {})
                    .get("pid")
                )
                if device_ld_pid is None:
                    device_ld_pid = (
                        controller_config.get("config", {})
                        .get("extras", {})
                        .get("ld", {})
                        .get("pid")
                    )
                device_index = ControllerHelper.resolve_emulator_index(
                    device, ld_pid=device_ld_pid
                )
                display_name = (
                    f"{device.name}[{device_index}]({device.address})"
                    if device_index is not None
                    else f"{device.name}({device.address})"
                )

                device_info = {
                    "adb_path": str(device.adb_path),
                    "address": device.address,
                    "screencap_methods": device.screencap_methods,
                    "input_methods": device.input_methods,
                    "config": device.config,
                    "device_name": display_name,
                }
                all_device_infos.append(device_info)
                if self._should_use_new_adb_device(controller_config, device_info):
                    return device_info
            logger.debug("ADB 设备列表均未满足与配置匹配的条件，跳过更新")
            logger.debug(f"所有 ADB 设备信息: {all_device_infos}")
            return None

        except Exception as e:
            logger.error(f"自动搜索 ADB 设备时出错: {e}")
            return None

    async def _auto_find_win32_window(
        self,
        controller_raw: Dict[str, Any],
        controller_type: str,
        controller_name: str,
        controller_config: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """自动搜索 Win32 窗口并找到与旧配置一致的那一项

        必须在 qasync/UI 线程调用 Toolkit.find_desktop_windows()。
        该 API 内部会对窗口 SendMessage(WM_GETTEXT)；放到其它线程且持有 GIL
        时，会与本进程 Qt 窗口形成死锁。
        """
        try:
            windows = Toolkit.find_desktop_windows()
            if not windows:
                logger.warning("未找到任何 Win32 窗口")
                return None

            all_window_infos = []
            if controller_type == "gamepad":
                class_pattern, window_pattern = self._get_gamepad_filter_patterns(
                    controller_name
                )
            else:
                class_pattern, window_pattern = self._get_win32_filter_patterns(
                    controller_name
                )
            matched_window_infos: list[Dict[str, Any]] = []
            for window in windows:
                window_info = {
                    "hwnd": str(window.hwnd),
                    "window_name": window.window_name,
                    "class_name": window.class_name,
                    "device_name": f"{window.window_name or 'Unknown Window'}({window.hwnd})",
                }
                all_window_infos.append(window_info)
                if not self._window_matches_win32_filters(
                    window_info, class_pattern, window_pattern
                ):
                    continue
                matched_window_infos.append(window_info)

            # 先只基于 class/window 正则过滤；如果只有一个候选，直接返回它
            if len(matched_window_infos) == 1:
                return matched_window_infos[0]

            # 若过滤出多个候选，再使用旧配置的 device_name 做消歧：
            # 去除括号及括号内内容后，与 window_name 对比，命中则返回。
            if len(matched_window_infos) > 1:
                old_device_name = (controller_config.get("device_name") or "").strip()
                old_title = self._strip_bracket_content(old_device_name)
                if old_title:
                    for win in matched_window_infos:
                        if (
                            self._strip_bracket_content(win.get("window_name") or "")
                            == old_title
                        ):
                            return win

                # 消歧失败时，保持行为确定性：返回第一个候选
                return matched_window_infos[0]
            logger.debug("Win32 窗口列表均未满足与配置匹配的条件，跳过更新")
            logger.debug(f"所有 Win32 窗口信息: {all_window_infos}")
            return None

        except Exception as e:
            logger.error(f"自动搜索 Win32 窗口时出错: {e}")
            return None

    async def _auto_find_macos_window(
        self,
        controller_raw: Dict[str, Any],
        controller_name: str,
        controller_config: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """自动搜索 MacOS 窗口并找到与旧配置一致的那一项"""
        try:
            windows = Toolkit.find_desktop_windows()
            if not windows:
                logger.warning("未找到任何 MacOS 窗口")
                return None

            window_pattern = self._get_macos_filter_pattern(controller_name)
            matched_window_infos: list[Dict[str, Any]] = []
            all_window_infos = []
            for window in windows:
                window_info = {
                    "window_id": str(window.hwnd),
                    "hwnd": str(window.hwnd),
                    "window_name": window.window_name,
                    "class_name": window.class_name,
                    "device_name": f"{window.window_name or 'Unknown Window'}({window.hwnd})",
                }
                all_window_infos.append(window_info)
                if window_pattern and not window_pattern.search(
                    str(window.window_name or "")
                ):
                    continue
                matched_window_infos.append(window_info)

            if len(matched_window_infos) == 1:
                return matched_window_infos[0]

            if len(matched_window_infos) > 1:
                old_device_name = (controller_config.get("device_name") or "").strip()
                old_title = self._strip_bracket_content(old_device_name)
                if old_title:
                    for win in matched_window_infos:
                        if (
                            self._strip_bracket_content(win.get("window_name") or "")
                            == old_title
                        ):
                            return win
                return matched_window_infos[0]

            logger.debug("MacOS 窗口列表均未满足与配置匹配的条件，跳过更新")
            logger.debug(f"所有 MacOS 窗口信息: {all_window_infos}")
            return None
        except Exception as e:
            logger.error(f"自动搜索 MacOS 窗口时出错: {e}")
            return None

    def _save_device_to_config(
        self,
        controller_raw: Dict[str, Any],
        controller_name: str,
        device_info: Dict[str, Any],
    ) -> None:
        """保存设备信息到配置

        Args:
            controller_raw: 控制器原始配置
            controller_name: 控制器名称（name）
            device_info: 设备信息字典
        """
        try:
            # 确保控制器配置存在（使用控制器名称作为键）
            if controller_name not in controller_raw:
                controller_raw[controller_name] = {}

            # 更新设备信息
            controller_raw[controller_name].update(device_info)

            # 获取预配置任务并更新
            if controller_cfg := self._get_task(_CONTROLLER_):
                controller_cfg.task_option.update(controller_raw)
                self._persist_runtime_task_update(controller_cfg)
                logger.info(f"设备配置已保存: {device_info.get('device_name', '')}")

        except Exception as e:
            logger.error(f"保存设备配置时出错: {e}")

    async def _handle_post_action(self) -> str | None:
        """
        统一处理完成后操作顺序（串行执行，避免动作未生效）：

        规则：
        - 关闭控制器、运行其他程序：优先执行，且会等待动作完成（尽力等待控制器真正关闭）
        - 切换配置：只要求前两者完成，不等待外部通知（因为不关软件）
        - 关机/退出软件：返回待执行动作，由任务流在最终通知入队后执行
        """
        post_task = self._get_task(POST_ACTION)
        if not post_task:
            return

        post_config = post_task.task_option.get("post_action")
        if not isinstance(post_config, dict):
            return

        # 1) 无动作：直接返回（不会与其他选项同时存在）
        if post_config.get("none"):
            logger.debug("完成后操作: 无动作")
            return

        # 2) 第一阶段：关闭控制器 / 运行其他程序（必须先完成）
        if post_config.get("close_controller"):
            logger.debug("完成后操作: 关闭控制器")
            await self._close_controller_and_wait()

        if post_config.get("run_program"):
            logger.debug("完成后操作: 运行其他程序")
            await self._run_program_from_post_action(
                post_config.get("program_path", ""),
                post_config.get("program_args", ""),
            )

        # 3) 第二阶段：切换配置（不等待外部通知）
        if post_config.get("run_other"):
            if target_config := (post_config.get("target_config") or "").strip():
                logger.info(
                    "完成后操作: 运行其他配置，等待 %.2f 秒再切换",
                    self._config_switch_delay,
                )
                await asyncio.sleep(self._config_switch_delay)
                await self._run_other_configuration(target_config)
            else:
                logger.warning("完成后运行其他配置开关被激活，但未配置目标配置")

        # 4) 退出/关机必须等最终状态和汇总通知确定后再执行。
        if post_config.get("close_software"):
            logger.debug("完成后操作: 已安排退出软件")
            return "close_software"
        if post_config.get("shutdown"):
            logger.debug("完成后操作: 已安排关机")
            return "shutdown"
        return None

    async def _run_program_from_post_action(
        self, program_path: str, program_args: str
    ) -> None:
        """根据配置启动指定程序，等待退出"""
        executable = (program_path or "").strip()
        if not executable:
            logger.warning("完成后程序未填写路径，跳过")
            return

        args_list = self._parse_program_args(program_args)
        try:
            process = await asyncio.to_thread(
                self._start_process, executable, args_list or None
            )
        except Exception as exc:
            logger.error(f"启动完成后程序失败: {exc}")
            return

        logger.debug(f"完成后程序已启动: {executable}")
        try:
            return_code = await asyncio.to_thread(process.wait)
            logger.debug(f"完成后程序已退出，返回码: {return_code}")
        except Exception as exc:
            logger.error(f"等待完成后程序退出时失败: {exc}")

    def _parse_program_args(self, args: str) -> list[str]:
        """解析完成后程序的参数字符串"""
        trimmed = (args or "").strip()
        if not trimmed:
            return []

        try:
            return shlex.split(trimmed, posix=os.name != "nt")
        except ValueError as exc:
            logger.warning(f"解析完成后程序参数失败，退回简单分割: {exc}")
            return [item for item in trimmed.split() if item]

    async def _run_other_configuration(self, config_id: str) -> None:
        """尝试切换到指定的配置"""
        config_service = self.config_service
        if not config_service:
            logger.warning("配置服务未初始化，跳过运行其他配置")
            return

        target_config = config_service.get_config(config_id)
        if not target_config:
            logger.warning(f"完成后操作指定的配置不存在: {config_id}")
            return

        # 当前 Runner 仍在完成清理，不能在这里切换共享配置服务；
        # finally 完成后由 Coordinator 激活目标 RuntimeContext。
        logger.debug(f"已安排完成后运行配置: {config_id}")
        self._next_config_to_run = config_id

    async def _close_software(self) -> None:
        """发出退出信号让程序自身关闭"""
        app = QCoreApplication.instance()
        if not app:
            logger.warning("完成后关闭软件: 无法获取 QCoreApplication 实例")
            return

        logger.debug("完成后关闭软件: 等待通知发送完成")
        await self._wait_for_notice_delivery()
        logger.debug("完成后关闭软件: 退出应用")
        app.quit()

    async def _wait_for_notice_delivery(self, timeout: float = 10.0) -> None:
        """等待通知线程将当前队列中的消息发送完毕"""
        try:
            if not send_thread.is_idle():
                self.info_bar_requested.emit(
                    "info",
                    self.tr(
                        "Notifications are being sent, please wait up to {} seconds"
                    ).format(int(timeout)),
                )
            completed = await asyncio.to_thread(send_thread.wait_until_idle, timeout)
            if not completed:
                logger.warning(
                    "等待通知发送完成超时: %s 秒，仍有未完成的通知任务", timeout
                )
        except Exception as exc:
            logger.warning("等待通知发送完成时出错: %s", exc)

    async def _shutdown_system_after_notice(self) -> None:
        """关机前等待外部通知发送完成（避免通知丢失）"""
        logger.debug("完成后关机: 等待通知发送完成")
        await self._wait_for_notice_delivery()
        logger.debug("完成后关机: 执行关机命令")
        self._shutdown_system()

    async def _close_controller_and_wait(self, timeout: float = 10.0) -> None:
        """关闭控制器，并尽力等待控制器真正退出（避免紧接着退出软件/关机时关闭动作未生效）"""
        # 先发起关闭动作（原逻辑）
        self._close_controller()

        # 再尽力等待真正关闭（仅对可检测的场景做等待；失败/超时不影响后续动作）
        try:
            controller_cfg = self._get_task(_CONTROLLER_)
            if not controller_cfg or not isinstance(controller_cfg.task_option, dict):
                return
            controller_raw = controller_cfg.task_option

            try:
                controller_type = self._get_controller_type(controller_raw)
            except Exception:
                return

            if controller_type == "win32":
                controller_name = self._get_controller_name(controller_raw)
                win32_config = None
                if controller_name in controller_raw:
                    win32_config = controller_raw.get(controller_name)
                elif "win32" in controller_raw:
                    win32_config = controller_raw.get("win32")
                if not isinstance(win32_config, dict):
                    return

                hwnd_raw = win32_config.get("hwnd", 0)
                if not hwnd_raw:
                    return
                await self._wait_win32_window_closed(hwnd_raw, timeout=timeout)
                return

            if controller_type == "adb":
                # 通过当前记录的 ADB address 尽力判断设备是否仍存在
                adb_cfg = self.adb_controller_config
                if not isinstance(adb_cfg, dict):
                    return
                address = (adb_cfg.get("address") or "").strip()
                if not address:
                    return
                await self._wait_adb_device_disconnected(address, timeout=timeout)
                return
        except Exception as exc:
            logger.debug("等待控制器关闭时出错（忽略）: %s", exc)

    async def _wait_win32_window_closed(
        self, hwnd: int | str, timeout: float = 10.0
    ) -> None:
        """等待 Win32 窗口关闭（短超时轮询）"""
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd_value = int(hwnd) if isinstance(hwnd, str) else int(hwnd)
        except Exception:
            return
        if hwnd_value <= 0:
            return

        def _is_window(hwnd_int: int) -> bool:
            import ctypes

            user32 = ctypes.windll.user32
            return bool(user32.IsWindow(hwnd_int))

        start = _time.time()
        while True:
            exists = await asyncio.to_thread(_is_window, hwnd_value)
            if not exists:
                logger.debug("完成后关闭控制器: Win32 窗口已关闭 (hwnd=%s)", hwnd_value)
                return
            if _time.time() - start >= timeout:
                logger.warning(
                    "完成后关闭控制器: 等待 Win32 窗口关闭超时 (hwnd=%s, timeout=%.1fs)",
                    hwnd_value,
                    timeout,
                )
                return
            await asyncio.sleep(0.2)

    async def _wait_adb_device_disconnected(
        self, address: str, timeout: float = 10.0
    ) -> None:
        """等待 ADB 设备断开（短超时轮询，尽力而为）"""
        normalized = (address or "").strip()
        if not normalized:
            return

        def _still_exists(addr: str) -> bool:
            try:
                devices = Toolkit.find_adb_devices() or []
                for dev in devices:
                    try:
                        dev_addr = str(dev.address).strip()
                    except Exception:
                        dev_addr = ""
                    if dev_addr == addr:
                        return True
                return False
            except Exception:
                return False

        start = _time.time()
        while True:
            exists = await asyncio.to_thread(_still_exists, normalized)
            if not exists:
                logger.debug("完成后关闭控制器: ADB 设备已断开 (%s)", normalized)
                return
            if _time.time() - start >= timeout:
                logger.warning(
                    "完成后关闭控制器: 等待 ADB 设备断开超时 (%s, timeout=%.1fs)",
                    normalized,
                    timeout,
                )
                return
            await asyncio.sleep(0.3)

    def _close_controller(self) -> None:
        """关闭控制器 - 根据当前运行的控制器类型执行不同的关闭操作"""
        controller_cfg = self._get_task(_CONTROLLER_)
        if not controller_cfg:
            logger.warning("未找到控制器配置，无法关闭控制器")
            return

        controller_raw = controller_cfg.task_option
        if not isinstance(controller_raw, dict):
            logger.warning("控制器配置格式错误，无法关闭控制器")
            return

        try:
            controller_type = self._get_controller_type(controller_raw)
        except Exception as exc:
            logger.warning(f"获取控制器类型失败: {exc}")
            return

        if controller_type == "adb":
            # 关闭 ADB 控制器：运行原本的关闭模拟器逻辑
            if self.adb_controller_config is None:
                logger.warning("ADB 控制器配置不存在，无法关闭")
                return

            adb_address = self.adb_controller_config.get("address", "")
            if ":" in adb_address:
                adb_port = adb_address.split(":")[-1]
            elif "-" in adb_address:
                adb_port = adb_address.split("-")[-1]
            else:
                adb_port = None
            adb_path = self.adb_controller_config.get("adb_path")

            device_name = self.adb_controller_config.get("device_name", "")
            device_name_lower = device_name.lower()
            extras = (
                self.adb_controller_config.get("config", {}) or {}
            ).get("extras", {}) or {}
            mumu_extra = extras.get("mumu") or {}
            ld_extra = extras.get("ld") or {}

            # extras 优先；名称兼容 MuMuPlayer / MuMuPlayer12 / MuMuPlayer v5+ 等
            is_mumu = bool(mumu_extra.get("enable")) or "mumu" in device_name_lower
            is_ld = bool(ld_extra.get("enable")) or "ldplayer" in device_name_lower

            if is_mumu:
                ControllerHelper.close_mumu(
                    adb_path,
                    adb_port,
                    index=mumu_extra.get("index"),
                    mumu_install_path=mumu_extra.get("path"),
                )
            elif is_ld:
                ControllerHelper.close_ldplayer(adb_path, ld_extra.get("pid"))
            else:
                logger.warning(f"未找到对应的模拟器: {device_name}")
        elif controller_type == "win32":
            # 关闭 Win32 控制器：通过 hwnd 关闭窗口
            controller_name = self._get_controller_name(controller_raw)
            if controller_name in controller_raw:
                win32_config = controller_raw[controller_name]
            elif controller_type in controller_raw:
                win32_config = controller_raw[controller_type]
            else:
                logger.warning("未找到 Win32 控制器配置")
                return

            hwnd_raw = win32_config.get("hwnd", 0)
            if not hwnd_raw:
                logger.warning("Win32 控制器窗口句柄为空，无法关闭")
                return

            # 调用 ControllerHelper 关闭窗口
            ControllerHelper.close_win32_window(hwnd_raw)
        elif controller_type == "playcover":
            # 关闭 PlayCover 控制器：什么都不做
            logger.debug("PlayCover 控制器无需关闭操作")
        elif controller_type in ("wlroots", "macos"):
            logger.debug("%s 控制器无需额外关闭操作", controller_type)
        else:
            logger.warning(f"未知的控制器类型: {controller_type}")

    def shutdown(self):
        """
        关机
        """
        shutdown_commands = {
            "Windows": "shutdown /s /t 1",
            "Linux": "shutdown now",
            "Darwin": "sudo shutdown -h now",  # macOS
        }
        os.system(shutdown_commands.get(platform.system(), ""))

    def _shutdown_system(self) -> None:
        """执行系统关机命令，兼容 Windows/macOS/Linux"""
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
            elif sys.platform == "darwin":
                subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
            else:
                subprocess.run(["shutdown", "-h", "now"], check=False)
            logger.debug("完成后执行关机命令")
        except Exception as exc:
            logger.error(f"执行关机命令失败: {exc}")

    def _resolve_speedrun_config(self, task: TaskItem) -> Dict[str, Any] | None:
        """Merge speedrun settings from the runtime interface and snapshot."""
        try:
            from app.core.service.task_service import DEFAULT_SPEEDRUN_CONFIG
            from app.core.speedrun.config import normalize_speedrun_config
            from app.core.utils.pipeline_helper import _deep_merge_dict

            if not isinstance(task.task_option, dict):
                task.task_option = {}
            interface_task = self._get_task_by_name(task.name)
            interface_cfg = (
                interface_task.get("speedrun", {})
                if isinstance(interface_task, dict)
                else {}
            )
            merged = deepcopy(DEFAULT_SPEEDRUN_CONFIG)
            legacy_condition = bool(interface_cfg) and not isinstance(
                interface_cfg.get("condition"), dict
            )
            if isinstance(interface_cfg, dict):
                _deep_merge_dict(merged, deepcopy(interface_cfg))
            existing = task.task_option.get("_speedrun_config")
            if isinstance(existing, dict):
                _deep_merge_dict(merged, deepcopy(existing))
            normalized = normalize_speedrun_config(
                merged, force_legacy_condition=legacy_condition
            )
            if existing != normalized:
                task.task_option["_speedrun_config"] = normalized
                self._persist_runtime_task_update(task)
            return normalized
        except Exception as exc:
            logger.warning(f"合成速通配置失败，使用 interface 数据: {exc}")
            interface_task = self._get_task_by_name(task.name)
            return (
                interface_task.get("speedrun")
                if interface_task and isinstance(interface_task, dict)
                else {}
            )

    def _get_task_by_name(self, name: str) -> Dict[str, Any]:
        interface = self._runtime_interface
        tasks = interface.get("task")

        if not isinstance(tasks, list):
            return {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if task.get("name") == name:
                return task

        return {}

    def _is_task_disabled(self, task: TaskItem) -> bool:
        """统一的"任务是否被禁用"判断（执行层只关心结论，不关心原因）。

        禁用来源可能包括：
        - UI 侧标记的 is_hidden
        - 配置层（TaskService）计算出的 resource/controller 约束
        """
        # 约定：任务流执行前由配置层（TaskService/Coordinator/UI）刷新过 is_hidden
        return bool(task.is_hidden)
