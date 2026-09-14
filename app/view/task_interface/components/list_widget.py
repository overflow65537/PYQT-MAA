from PySide6.QtWidgets import (
    QListWidgetItem,
    QAbstractItemView,
    QGraphicsOpacityEffect,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QSizePolicy,
)
from PySide6.QtCore import (
    Qt,
    Signal,
    QPropertyAnimation,
    QParallelAnimationGroup,
    QEasingCurve,
    QTimer,
    QSize,
    QEvent,
    QPoint,
)
from qfluentwidgets import ListWidget, IndeterminateProgressRing, SimpleCardWidget

from app.core.core import  ServiceCoordinator
from app.core.item import TaskItem, ConfigItem
from app.view.task_interface.components.list_item import TaskListItem, ConfigListItem
from app.utils.logger import logger
from app.common.signal_bus import signalBus
from app.common.constants import _PRETASK_, _RESOURCE_, _CONTROLLER_, _SETTING_


class BaseListWidget(ListWidget):
    """基础列表组件，所有子类通用拖拽功能"""

    item_selected = Signal(str)  # 列表项选择信号
    _WHEEL_SCROLL_FACTOR = 0.35  # 鼠标滚轮滚动缩放，越小越慢
    _SCROLL_SINGLE_STEP = 8

    def __init__(self, service_coordinator: ServiceCoordinator, parent=None):
        super().__init__(parent)
        self.service_coordinator = service_coordinator
        # 调低滚动步幅，避免一次滚动移动过多
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(self._SCROLL_SINGLE_STEP)
        self.currentItemChanged.connect(self._on_item_selected)

    def _on_item_selected(self, current, previous):
        """选中项变化时发出 item_id 信号"""
        if current:
            widget = self.itemWidget(current)
            if widget and isinstance(widget, (TaskListItem, ConfigListItem)):
                self.item_selected.emit(widget.item.item_id)

    def select_item(self, item_id: str):
        """在列表中查找并选中指定 item_id 的项（通用方法）"""
        for i in range(self.count()):
            li = self.item(i)
            widget = self.itemWidget(li)
            if (
                widget
                and isinstance(widget, (TaskListItem, ConfigListItem))
                and widget.item.item_id == item_id
            ):
                self.setCurrentItem(li)
                self.scrollToItem(
                    li, QAbstractItemView.ScrollHint.PositionAtCenter
                )
                break

    def wheelEvent(self, event):
        """缩小滚轮滚动幅度，提升细腻度"""
        delta = event.pixelDelta().y()
        if delta == 0:
            delta = event.angleDelta().y()
        if delta != 0 and self.verticalScrollBar():
            step = int(delta * self._WHEEL_SCROLL_FACTOR)
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - step
            )
            event.accept()
            return
        super().wheelEvent(event)


class SkeletonBar(QWidget):
    """用于模拟骨架占位的矩形条"""

    def __init__(self, parent=None, height: int = 10):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "background-color: rgba(255, 255, 255, 0.25); border-radius: 5px;"
        )


class TaskSkeletonWidget(SimpleCardWidget):
    """qfluentwidgets 风格的骨架占位器，用于在大量任务加载时缓解卡顿"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(8)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(SkeletonBar(self, height=12))
        layout.addWidget(SkeletonBar(self, height=12))


class TaskDragListWidget(BaseListWidget):
    """任务拖拽列表组件：支持拖动排序、添加、修改、删除任务（基础任务禁止删除/拖动）"""

    _TASK_ITEM_HEIGHT = 44
    _DRAG_SCROLL_MARGIN = 40
    _DRAG_SCROLL_TIMER_INTERVAL_MS = 16
    _DRAG_SCROLL_MIN_STEP = 6
    _DRAG_SCROLL_MAX_STEP = 18
    _REORDER_ANIMATION_DURATION_MS = 180

    def __init__(
        self,
        service_coordinator: ServiceCoordinator,
        parent=None,
    ):
        super().__init__(service_coordinator, parent)
        self._persist_changes = True
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        # 标记是否处于批量刷新阶段，用于保持服务端给出的顺序
        self._bulk_updating: bool = False

        self._fade_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fade_effect)
        self._fade_effect.setOpacity(1.0)
        self._fade_out = QPropertyAnimation(self._fade_effect, b"opacity", self)
        self._fade_out.setDuration(80)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InQuad)
        self._fade_in = QPropertyAnimation(self._fade_effect, b"opacity", self)
        self._fade_in.setDuration(100)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_out.finished.connect(self._on_fade_out_finished)
        self._fade_in.finished.connect(self._on_fade_in_finished)
        self._pending_refresh = False
        # 资源/控制器变更触发列表刷新后，恢复对应任务的选中态
        self._pending_restore_item_id: str | None = None
        self._list_render_generation: int = 0
        # 刷新渲染期间抑制选中通知，避免中途落到 resource 再被 restore 拉回
        self._suppress_selection_notify: bool = False

        # 维护 task_id -> widget 的映射，避免重复遍历列表
        self._task_widgets: dict[str, TaskListItem] = {}
        self._skeleton_items: list[QListWidgetItem] = []
        self._pending_tasks: list[TaskItem] = []
        self._render_index: int = 0
        self._loading_tasks: bool = False
        # 维护待处理的任务状态（当 widget 还未创建时）
        self._pending_task_statuses: dict[str, str] = {}

        self._init_loading_overlay()

        self.item_selected.connect(self._on_item_selected_to_service)
        self.service_coordinator.view_signals.config_changed.connect(
            self._on_config_changed
        )
        # 监听资源变化信号，更新任务列表
        self.service_coordinator.view_signals.option_updated.connect(
            self._on_resource_changed
        )
        service_coordinator.view_signals.task_modified.connect(self.modify_task)
        service_coordinator.view_signals.task_removed.connect(self.remove_task)
        
        # 监听任务状态变化信号
        from app.common.signal_bus import signalBus
        signalBus.task_status_changed.connect(self._on_task_status_changed)
        
        self._bulk_toggle_queue: list[tuple[TaskListItem, bool]] = []
        self._bulk_toggle_timer = QTimer(self)
        self._bulk_toggle_timer.setSingleShot(True)
        self._bulk_toggle_timer.timeout.connect(self._process_bulk_toggle_step)
        
        # 拖拽时自动滚动相关
        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.timeout.connect(self._on_drag_scroll_timer)
        self._drag_scroll_direction = 0  # -1: 向上, 1: 向下, 0: 不滚动
        self._drag_scroll_step = self._DRAG_SCROLL_MIN_STEP
        self._is_dragging = False
        self._reorder_animation_group: QParallelAnimationGroup | None = None
        
        self.update_list()

    def _on_item_selected_to_service(self, item_id: str):
        if self._suppress_selection_notify or self._loading_tasks:
            return
        self.service_coordinator.select_task(item_id)

    def _on_item_selected(self, current, previous):
        """选中项变化时发出 item_id 信号；刷新渲染期间忽略，防止中途切到 resource。"""
        if self._suppress_selection_notify or self._loading_tasks:
            return
        super()._on_item_selected(current, previous)

    def _should_include(self, task: TaskItem) -> bool:
        """判断任务是否应显示在当前列表。

        注意：`task.is_hidden` 是“能力禁用”标记（由配置层/列表层根据 resource/controller 计算）。
        """
        if task.item_id == _SETTING_:
            return False
        if task.item_id == _PRETASK_ and not self.service_coordinator.tasks.has_visible_pretask():
            return False

        # View 仅计算是否展示，不回写 TaskItem 业务字段。
        should_show_by_resource = self._should_show_by_resource(task)
        should_show_by_controller = self._should_show_by_controller(task)
        capability_show = should_show_by_resource and should_show_by_controller
        return capability_show
    
    def _should_show_by_resource(self, task: TaskItem) -> bool:
        """根据当前选择的资源判断任务是否应该显示"""
        # 基础任务（资源、控制器、完成后操作）始终显示
        if task.is_base_task():
            return True
        
        # 获取当前配置中的资源
        try:
            # 从 Resource 任务中获取资源
            resource_task = self.service_coordinator.tasks.get_task(_RESOURCE_)
            if not resource_task:
                logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 没有 Resource 任务，显示所有任务")
                return True  # 如果没有 Resource 任务，显示所有任务
            
            current_resource_name = ""
            if isinstance(resource_task.task_option, dict):
                current_resource_name = resource_task.task_option.get("resource", "")
            
            if not current_resource_name:
                logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 没有选择资源，显示所有任务")
                return True  # 如果没有选择资源，显示所有任务
            
            # 获取 interface 中的任务定义
            try:
                interface = self.service_coordinator.tasks.interface
            except Exception:
                interface = {}
            if not interface:
                logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 没有 interface，显示所有任务")
                return True
            
            # 查找任务定义中的 resource 字段
            for task_def in interface.get("task", []):
                if task_def.get("name") == task.name:
                    task_resources = task_def.get("resource", [])
                    # 如果任务没有 resource 字段，或者 resource 为空列表，表示所有资源都可用
                    if not task_resources:
                        logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 没有 resource 字段，显示（所有资源可用）")
                        return True
                    # 如果任务的 resource 列表包含当前资源（使用 name 匹配），则显示
                    if current_resource_name in task_resources:
                        logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 当前资源 {current_resource_name} 在任务的 resource 列表中，显示")
                        return True
                    logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 当前资源 {current_resource_name} 不在任务的 resource 列表 {task_resources} 中，隐藏")
                    return False
            
            # 如果找不到任务定义，默认显示
            logger.debug(f"[_should_show_by_resource] 任务 {task.name}: 找不到任务定义，默认显示")
            return True
        except Exception as e:
            # 发生错误时，默认显示所有任务
            logger.warning(f"[_should_show_by_resource] 任务 {task.name}: 发生错误，默认显示: {e}")
            return True

    def _should_show_by_controller(self, task: TaskItem) -> bool:
        """根据当前选择的控制器类型判断任务是否应该显示。

        规则：
        - 基础任务始终显示
        - interface.task[*].controller 缺省/空：对所有控制器显示
        - 否则仅当当前 controller_type 命中 controller 列表才显示
        """
        if task.is_base_task():
            return True

        try:
            controller_task = self.service_coordinator.tasks.get_task(_CONTROLLER_)
            current_controller = ""
            if controller_task and isinstance(controller_task.task_option, dict):
                current_controller = controller_task.task_option.get("controller_type", "") or ""
            if not current_controller:
                # 若尚未配置控制器，默认显示全部任务
                return True
            current_controller_norm = str(current_controller).strip().lower()

            try:
                interface = self.service_coordinator.tasks.interface
            except Exception:
                interface = {}
            if not interface:
                return True

            for task_def in interface.get("task", []):
                if task_def.get("name") != task.name:
                    continue
                controllers = task_def.get("controller", None)

                # 缺省/空表示全部控制器可用
                if controllers in (None, "", [], {}):
                    return True

                allowed: list[str] = []
                if isinstance(controllers, str):
                    if controllers.strip():
                        allowed = [controllers.strip()]
                elif isinstance(controllers, list):
                    allowed = [
                        str(x).strip()
                        for x in controllers
                        if x is not None and str(x).strip()
                    ]
                else:
                    # 不支持的格式：兜底为“全部可用”
                    return True

                allowed_norm = {s.lower() for s in allowed if s}
                return current_controller_norm in allowed_norm

            # 找不到任务定义：默认显示
            return True
        except Exception as e:
            logger.warning(f"[_should_show_by_controller] 任务 {task.name}: 发生错误，默认显示: {e}")
            return True

    def dragMoveEvent(self, event):
        """拖拽移动事件：检测鼠标位置并触发自动滚动，同时允许滚轮滚动"""
        self._is_dragging = True
        super().dragMoveEvent(event)
        self._update_drag_scroll_state(event.pos())
    
    def _on_drag_scroll_timer(self):
        """拖拽自动滚动定时器回调"""
        if not self._is_dragging or self._drag_scroll_direction == 0:
            self._stop_drag_scroll()
            return

        scroll_bar = self.verticalScrollBar()
        if not scroll_bar:
            return

        new_value = scroll_bar.value() + (
            self._drag_scroll_step * self._drag_scroll_direction
        )
        new_value = max(scroll_bar.minimum(), min(scroll_bar.maximum(), new_value))
        if new_value == scroll_bar.value():
            self._stop_drag_scroll()
            return
        scroll_bar.setValue(new_value)

    def _update_drag_scroll_state(self, pos: QPoint) -> None:
        """根据拖拽位置更新自动滚动状态。"""
        if not self._is_dragging:
            self._stop_drag_scroll()
            return

        viewport_rect = self.viewport().rect()
        if not viewport_rect.contains(pos):
            self._stop_drag_scroll()
            return

        scroll_direction = 0
        step = self._DRAG_SCROLL_MIN_STEP
        pos_y = pos.y()
        viewport_height = viewport_rect.height()
        margin = max(1, min(self._DRAG_SCROLL_MARGIN, viewport_height // 2))

        if pos_y < margin:
            scroll_direction = -1
            strength = (margin - pos_y) / margin
            step = self._drag_scroll_step_for_strength(strength)
        elif pos_y > viewport_height - margin:
            scroll_direction = 1
            strength = (pos_y - (viewport_height - margin)) / margin
            step = self._drag_scroll_step_for_strength(strength)

        if scroll_direction == 0:
            self._stop_drag_scroll()
            return

        self._drag_scroll_direction = scroll_direction
        self._drag_scroll_step = step
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start(self._DRAG_SCROLL_TIMER_INTERVAL_MS)

    def _drag_scroll_step_for_strength(self, strength: float) -> int:
        strength = max(0.0, min(1.0, strength))
        scroll_range = self._DRAG_SCROLL_MAX_STEP - self._DRAG_SCROLL_MIN_STEP
        return self._DRAG_SCROLL_MIN_STEP + int(scroll_range * strength)

    def _stop_drag_scroll(self) -> None:
        self._drag_scroll_timer.stop()
        self._drag_scroll_direction = 0
        self._drag_scroll_step = self._DRAG_SCROLL_MIN_STEP
    
    def wheelEvent(self, event):
        """重写滚轮事件，确保在拖拽期间也能使用滚轮滚动"""
        # 即使在拖拽期间也允许滚轮滚动
        delta = event.pixelDelta().y()
        if delta == 0:
            delta = event.angleDelta().y()
        if delta != 0 and self.verticalScrollBar():
            step = int(delta * self._WHEEL_SCROLL_FACTOR)
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - step
            )
            if self._is_dragging:
                self._update_drag_scroll_state(event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)
    
    def dragLeaveEvent(self, event):
        """拖拽离开事件：停止自动滚动"""
        self._stop_drag_scroll()
        self._is_dragging = False
        super().dragLeaveEvent(event)
    
    def dropEvent(self, event):
        previous_tasks = self._collect_task_items()
        previous_order = [task.item_id for task in previous_tasks]
        previous_positions = self._capture_task_widget_positions()
        protected = self._protected_positions(previous_tasks)

        self._stop_drag_scroll()
        self._is_dragging = False

        if self._is_drop_target_protected(event.pos(), protected):
            event.ignore()
            return

        super().dropEvent(event)
        self._handle_drop_result(previous_tasks, previous_order, protected, previous_positions, event)

    def _handle_drop_result(
        self,
        previous_tasks: list[TaskItem],
        previous_order: list[str],
        protected: dict[int, str],
        previous_positions: dict[str, QPoint],
        event,
    ) -> None:
        """统一处理 drop 后的校验、回滚、持久化与动画。"""
        current_tasks = self._collect_task_items()
        if not current_tasks:
            self._restore_order(previous_order)
            event.ignore()
            return

        current_order = [task.item_id for task in current_tasks]
        if current_order == previous_order:
            return

        if not self._is_valid_drop_result(current_tasks, protected):
            self._restore_order(previous_order)
            event.ignore()
            return

        full_tasks = self.service_coordinator.tasks.get_tasks()
        reorder_seq = self._build_reorder_sequence(full_tasks, current_tasks)
        if reorder_seq == [task.item_id for task in full_tasks]:
            self._animate_reorder(previous_positions)
            return

        if not self.service_coordinator.reorder_tasks(reorder_seq):
            self._restore_order(previous_order)
            event.ignore()
            return

        self._animate_reorder(previous_positions)

    def _is_valid_drop_result(
        self, current_tasks: list[TaskItem], protected: dict[int, str]
    ) -> bool:
        """校验拖拽结果是否合法。"""
        return self._base_positions_intact(current_tasks, protected)

    def _is_drop_target_protected(
        self, pos: QPoint, protected: dict[int, str]
    ) -> bool:
        """在 drop 前快速判断目标行是否命中受保护槽位。"""
        if not protected:
            return False

        target_row = self.indexAt(pos).row()
        if target_row == -1:
            target_row = self.count() - 1
        return target_row in protected

    def _on_config_changed(self, config_id: str) -> None:
        """Reload tasks when user switches configuration."""
        if self._fade_out.state() == QPropertyAnimation.State.Running:
            self._pending_refresh = True
            return
        if self._fade_in.state() == QPropertyAnimation.State.Running:
            self._fade_in.stop()
        self._pending_refresh = True
        self._show_loading_overlay()
        self._fade_out.start()
        QTimer.singleShot(10, self.clearSelection)
    
    def _on_resource_changed(self, options: dict) -> None:
        """当选项变化时，更新任务列表显示"""
        # 资源或控制器变化时，刷新任务列表（会重新计算 is_hidden）
        if "resource" in options or "controller_type" in options:
            # 同 payload 含两者时控制器优先（换控制器可能顺带改兼容资源）
            if "controller_type" in options:
                self._pending_restore_item_id = _CONTROLLER_
            else:
                self._pending_restore_item_id = _RESOURCE_
            self.update_list()
        else:
            # 其他选项变化时，更新所有 TaskListItem 的选项显示
            # 使用 QTimer.singleShot 确保 task 更新完成后再更新显示
            QTimer.singleShot(10, self._update_all_task_option_displays)
    
    def _update_all_task_option_displays(self):
        """更新所有 TaskListItem 的选项显示"""
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem):
                widget._update_option_display()

    def _on_fade_out_finished(self) -> None:
        if not self._pending_refresh:
            self._fade_in.start()
            return
        pending = self._pending_refresh
        self._pending_refresh = False

        if pending:
            self.update_list()
        self._fade_in.start()

    def _on_fade_in_finished(self) -> None:
        self._hide_loading_overlay()

    def update_list(self):
        """刷新任务列表UI（先显示骨架占位，再逐项渲染）"""
        self._stop_drag_scroll()
        self._stop_reorder_animation()
        # 整段重建期间屏蔽选中信号，避免逐项变可选时短暂选中 resource
        self._suppress_selection_notify = True
        self.blockSignals(True)
        self.clear()
        self.setCurrentRow(-1)
        self._task_widgets.clear()
        self._skeleton_items.clear()
        # 不清除待处理状态，因为任务列表刷新后这些状态仍然有效
        all_tasks = self.service_coordinator.tasks.get_tasks()
        task_list = [t for t in all_tasks if self._should_include(t)]
        self._pending_tasks = task_list
        self._render_index = 0
        self._loading_tasks = bool(task_list)
        self._list_render_generation += 1
        render_generation = self._list_render_generation
        self._add_task_skeletons(len(task_list))
        if self._pending_tasks:
            QTimer.singleShot(
                5, lambda: self._render_pending_task(render_generation)
            )
        else:
            self._loading_tasks = False
            self._restore_pending_selection()

    def _restore_pending_selection(self) -> None:
        """列表刷新完成后，恢复控制器/资源选中态。

        服务层已是目标任务时仅恢复列表高亮，避免重复 select_task 触发
        options_loaded 重建右侧面板。
        """
        item_id = self._pending_restore_item_id
        self._pending_restore_item_id = None
        self._loading_tasks = False

        target_item = None
        if item_id:
            for i in range(self.count()):
                li = self.item(i)
                widget = self.itemWidget(li)
                if (
                    widget
                    and isinstance(widget, TaskListItem)
                    and widget.item.item_id == item_id
                ):
                    target_item = li
                    break

        current_task_id = self.service_coordinator.options.current_task_id
        if target_item is not None and current_task_id == item_id:
            # 仍保持屏蔽：只恢复高亮，不通知服务层
            self.setCurrentItem(target_item)
            self.scrollToItem(
                target_item, QAbstractItemView.ScrollHint.PositionAtCenter
            )
            self.blockSignals(False)
            self._suppress_selection_notify = False
            return

        self.blockSignals(False)
        self._suppress_selection_notify = False
        if target_item is not None and item_id:
            self.select_item(item_id)

    def _add_task_skeletons(self, count: int):
        """根据任务数量先添加骨架占位，避免一次性渲染卡顿"""
        for _ in range(count):
            list_item = QListWidgetItem()
            list_item.setSizeHint(QSize(0, self._TASK_ITEM_HEIGHT))
            list_item.setFlags(Qt.ItemFlag.NoItemFlags)
            skeleton = TaskSkeletonWidget(self)
            self.addItem(list_item)
            self.setItemWidget(list_item, skeleton)
            self._skeleton_items.append(list_item)

    def _render_pending_task(self, render_generation: int):
        """逐项替换骨架为真实任务组件，确保 UI 不被阻塞"""
        if render_generation != self._list_render_generation:
            return
        if self._render_index >= len(self._pending_tasks):
            self._pending_tasks = []
            self._skeleton_items.clear()
            self._restore_pending_selection()
            return

        task = self._pending_tasks[self._render_index]
        self._render_task_at_index(self._render_index, task)
        # 目标项一渲染完就先视觉选中（信号仍屏蔽），减少空白闪烁
        if (
            self._pending_restore_item_id
            and task.item_id == self._pending_restore_item_id
        ):
            widget = self._task_widgets.get(task.item_id)
            if widget is not None:
                for i in range(self.count()):
                    li = self.item(i)
                    if self.itemWidget(li) is widget:
                        self.setCurrentItem(li)
                        break
        self._render_index += 1
        QTimer.singleShot(
            5, lambda: self._render_pending_task(render_generation)
        )

    def _render_task_at_index(self, index: int, task: TaskItem):
        """将指定位置的骨架替换为实际的 `TaskListItem`"""
        try:
            interface = self.service_coordinator.tasks.interface
        except Exception:
            interface = None
        if index < len(self._skeleton_items):
            list_item = self._skeleton_items[index]
        else:
            list_item = QListWidgetItem()
            list_item.setSizeHint(QSize(0, self._TASK_ITEM_HEIGHT))
            self.addItem(list_item)
            self._skeleton_items.append(list_item)

        placeholder = self.itemWidget(list_item)
        if isinstance(placeholder, TaskSkeletonWidget):
            placeholder.deleteLater()

        task_widget = TaskListItem(
            task, interface=interface, service_coordinator=self.service_coordinator
        )
        task_widget.checkbox_changed.connect(self._on_task_checkbox_changed)

        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not task.is_base_task():
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        else:
            flags &= ~Qt.ItemFlag.ItemIsDragEnabled

        list_item.setFlags(flags)
        self.setItemWidget(list_item, task_widget)
        self._task_widgets[task.item_id] = task_widget
        
        # 检查是否有待处理的状态，如果有则立即应用
        if task.item_id in self._pending_task_statuses:
            pending_status = self._pending_task_statuses.pop(task.item_id)
            task_widget.update_status(pending_status)

    def modify_task(self, task: TaskItem):
        """添加或更新任务项到列表（如果存在同 id 的任务则更新，否则新增）。"""
        self._stop_reorder_animation()
        # 先尝试查找是否已有同 id 的项
        existing_widget = self._task_widgets.get(task.item_id)
        
        # 检查任务是否符合过滤条件
        if not self._should_include(task):
            # 如果任务不符合过滤条件，但已经存在于列表中，则移除它
            if existing_widget:
                for i in range(self.count()):
                    item = self.item(i)
                    widget = self.itemWidget(item)
                    if widget == existing_widget:
                        self.takeItem(i)
                        widget.deleteLater()
                        self._task_widgets.pop(task.item_id, None)
                        break
            return
        
        # 获取 interface 配置
        try:
            interface = self.service_coordinator.tasks.interface
        except Exception:
            interface = None
        # 如果已有同 id 的项，进行更新
        if existing_widget:
            existing_widget.task = task
            existing_widget.update_interface(interface)
            existing_widget.name_label.setText(existing_widget._get_display_name())
            # 更新复选框状态，反映任务的选中状态（可能因隐藏而改变）
            existing_widget.checkbox.blockSignals(True)
            existing_widget.checkbox.setChecked(task.is_checked)
            existing_widget.checkbox.blockSignals(False)
            return

        if self._loading_tasks:
            # 正在依次渲染骨架，不需要重复插入
            return

        # 否则按原有逻辑新增项
        list_item = QListWidgetItem()
        list_item.setSizeHint(QSize(0, self._TASK_ITEM_HEIGHT))
        task_widget = TaskListItem(
            task, interface=interface, service_coordinator=self.service_coordinator
        )
        # 复选框状态变更信号
        task_widget.checkbox_changed.connect(self._on_task_checkbox_changed)
        # 基础任务禁止拖动
        if task.is_base_task():
            list_item.setFlags(list_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        
        # 批量刷新时严格按传入顺序追加；单项新增时根据任务在完整列表中的位置插入
        if self._bulk_updating:
            self.addItem(list_item)
        else:
            # 获取完整任务列表，找到新任务在完整列表中的位置
            all_tasks = self.service_coordinator.tasks.get_tasks()
            task_index_in_all = -1
            for i, t in enumerate(all_tasks):
                if t.item_id == task.item_id:
                    task_index_in_all = i
                    break
            
            # 在 UI 列表中找到对应的插入位置
            # 需要找到在完整列表中，位于当前任务之前且符合过滤条件的最后一个任务在 UI 中的位置
            insert_index = self.count()  # 默认插入到最后
            if task_index_in_all >= 0:
                # 创建 UI 中任务 item_id 到索引的映射，提高查找效率
                ui_task_positions = {}
                for j in range(self.count()):
                    item = self.item(j)
                    widget = self.itemWidget(item)
                    if isinstance(widget, TaskListItem):
                        ui_task_positions[widget.task.item_id] = j
                
                # 从完整列表中，找到当前任务之前的所有任务
                # 然后在 UI 列表中找到这些任务中最后一个符合过滤条件的任务位置
                for i in range(task_index_in_all - 1, -1, -1):
                    prev_task = all_tasks[i]
                    # 检查这个任务是否在 UI 列表中
                    if prev_task.item_id in ui_task_positions:
                        # 找到了前一个任务在 UI 中的位置，插入到它后面
                        insert_index = ui_task_positions[prev_task.item_id] + 1
                        break
            
            # 如果没找到合适的位置，使用默认位置（倒数第二位，确保"完成后操作"始终在最后）
            if insert_index >= self.count():
                insert_index = max(0, self.count() - 1)
            
            self.insertItem(insert_index, list_item)
        self.setItemWidget(list_item, task_widget)
        self._task_widgets[task.item_id] = task_widget

    def remove_task(self, task_id: str):
        """移除任务项，基础任务不可移除"""
        self._stop_reorder_animation()
        # 检查是否为基础任务
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem) and widget.task.item_id == task_id:
                if widget.task.is_base_task():
                    # 基础任务不可删除，记录日志并显示提示
                    signalBus.info_bar_requested.emit(
                        "warning",
                        self.tr(
                            "Base tasks (Resource, Post-Task) cannot be deleted (ID: {id})"
                        ).format(id=task_id),
                    )
                    return
                self.takeItem(i)
                widget.deleteLater()
                self._task_widgets.pop(task_id, None)
                break

    def _collect_task_items(self) -> list[TaskItem]:
        """Collect TaskItem instances from current widgets for ordering checks."""
        tasks: list[TaskItem] = []
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem):
                tasks.append(widget.task)
        return tasks

    def _protected_positions(self, tasks: list[TaskItem]) -> dict[int, str]:
        """Record base task rows that must remain fixed during drag."""
        return {
            idx: task.item_id
            for idx, task in enumerate(tasks)
            if task.is_base_task()
        }

    def _base_positions_intact(
        self, tasks: list[TaskItem], protected: dict[int, str]
    ) -> bool:
        """Verify base tasks still occupy their original rows after a drop."""
        for idx, expected_id in protected.items():
            if idx < 0 or idx >= len(tasks):
                return False
            if tasks[idx].item_id != expected_id:
                return False
        return True

    def _restore_order(self, order: list[str]) -> None:
        """Restore original order if a drop violates base task constraints."""
        self._stop_reorder_animation()
        for target_index, task_id in enumerate(order):
            current_index = self._find_row_by_task_id(task_id)
            if current_index == -1 or current_index == target_index:
                continue
            list_item = self.item(current_index)
            widget = self.itemWidget(list_item)
            list_item = self.takeItem(current_index)
            if list_item is None:
                continue
            self.insertItem(target_index, list_item)
            if widget is not None:
                self.setItemWidget(list_item, widget)

    def _find_row_by_task_id(self, task_id: str) -> int:
        for row in range(self.count()):
            widget = self.itemWidget(self.item(row))
            if isinstance(widget, TaskListItem) and widget.task.item_id == task_id:
                return row
        return -1

    def _build_reorder_sequence(
        self, full_tasks: list[TaskItem], visible_tasks: list[TaskItem]
    ) -> list[str]:
        """
        根据当前可见任务的顺序生成完整的任务排序。
        因资源/控制器不可用的隐藏项不在列表中，需按完整列表穿插保留其位置。
        """
        visible_ids = iter([task.item_id for task in visible_tasks])
        ordered: list[str] = []
        for task in full_tasks:
            if self._should_include(task):
                try:
                    ordered.append(next(visible_ids))
                except StopIteration:
                    continue
            else:
                ordered.append(task.item_id)

        ordered.extend(list(visible_ids))
        return ordered

    def _capture_task_widget_positions(self) -> dict[str, QPoint]:
        """记录当前可见任务项的位置，用于 drop 后平滑过渡。"""
        positions: dict[str, QPoint] = {}
        for row in range(self.count()):
            item = self.item(row)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem):
                positions[widget.task.item_id] = widget.pos()
        return positions

    def _animate_reorder(self, previous_positions: dict[str, QPoint]) -> None:
        """对发生位移的可见任务项执行平滑移动动画。"""
        self._stop_reorder_animation()

        group = QParallelAnimationGroup(self)
        for row in range(self.count()):
            item = self.item(row)
            widget = self.itemWidget(item)
            if not isinstance(widget, TaskListItem):
                continue

            start_pos = previous_positions.get(widget.task.item_id)
            end_pos = widget.pos()
            if start_pos is None or start_pos == end_pos:
                continue

            animation = QPropertyAnimation(widget, b"pos", group)
            animation.setDuration(self._REORDER_ANIMATION_DURATION_MS)
            animation.setStartValue(start_pos)
            animation.setEndValue(end_pos)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(animation)

        if group.animationCount() == 0:
            group.deleteLater()
            return

        self._reorder_animation_group = group

        def _cleanup() -> None:
            if self._reorder_animation_group is group:
                self._reorder_animation_group = None
            group.deleteLater()

        group.finished.connect(_cleanup)
        group.start()

    def _stop_reorder_animation(self) -> None:
        """停止仍在进行的重排动画，避免刷新与拖拽互相覆盖。"""
        if self._reorder_animation_group is None:
            return
        group = self._reorder_animation_group
        self._reorder_animation_group = None
        group.stop()
        group.deleteLater()

    def _init_loading_overlay(self) -> None:
        self._loading_overlay = QWidget(self)
        self._loading_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._loading_overlay.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._loading_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 60); border-radius: 8px;"
        )
        layout = QHBoxLayout(self._loading_overlay)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._loading_indicator = IndeterminateProgressRing(self._loading_overlay)
        self._loading_indicator.setFixedSize(28, 28)
        layout.addWidget(self._loading_indicator)

        self._loading_overlay.hide()

    def _show_loading_overlay(self) -> None:
        self._loading_overlay.setGeometry(self.viewport().geometry())
        self._loading_overlay.show()
        self._loading_indicator.start()

    def _hide_loading_overlay(self) -> None:
        self._loading_overlay.hide()
        self._loading_indicator.stop()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_loading_overlay"):
            self._loading_overlay.setGeometry(self.viewport().geometry())

    def _on_task_checkbox_changed(self, task_id: str, is_checked: bool):
        """提交勾选命令；失败时无递归地恢复服务端状态。"""
        task = self.service_coordinator.tasks.get_task(task_id)
        if task is None or task.is_base_task():
            return

        previous_state = task.is_checked
        try:
            success = self.service_coordinator.update_task_checked(
                task_id,
                is_checked,
            )
        except Exception:
            success = False
            logger.exception("更新任务勾选状态失败: %s", task_id)

        if success:
            return

        widget = self._task_widgets.get(task_id)
        if widget is None:
            return
        widget.checkbox.blockSignals(True)
        try:
            widget.checkbox.setChecked(previous_state)
        finally:
            widget.checkbox.blockSignals(False)

    def select_all(self) -> None:
        """批量勾选当前列表中的任务（基础任务除外）。"""
        steps: list[tuple[TaskListItem, bool]] = []
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem):
                if not widget.task.is_base_task():
                    steps.append((widget, True))
        self._enqueue_bulk_toggle(steps)

    def deselect_all(self) -> None:
        """批量取消当前列表中的任务勾选（基础任务除外）。"""
        steps: list[tuple[TaskListItem, bool]] = []
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, TaskListItem):
                if widget.task.is_base_task():
                    continue
                steps.append((widget, False))
        self._enqueue_bulk_toggle(steps)

    def _enqueue_bulk_toggle(self, steps: list[tuple[TaskListItem, bool]]) -> None:
        if not steps:
            return
        self._bulk_toggle_queue = steps
        if not self._bulk_toggle_timer.isActive():
            self._bulk_toggle_timer.start(10)

    def _process_bulk_toggle_step(self) -> None:
        if not self._bulk_toggle_queue:
            return
        widget, target_state = self._bulk_toggle_queue.pop(0)
        if widget:
            if widget.checkbox.isChecked() != target_state:
                widget.checkbox.setChecked(target_state)
        if self._bulk_toggle_queue:
            self._bulk_toggle_timer.start(1)
    
    def _on_task_status_changed(self, task_id: str, status: str):
        """处理任务状态变化
        
        Args:
            task_id: 任务ID
            status: 状态字符串，可选值:
                "running", "completed", "failed", "restart_success",
                "waiting", "skipped", ""(清除状态)
        """
        # 从任务widget映射中查找对应的widget
        widget = self._task_widgets.get(task_id)
        if widget and isinstance(widget, TaskListItem):
            widget.update_status(status)
            # 清除待处理状态
            self._pending_task_statuses.pop(task_id, None)
        else:
            # 如果widget还没有创建，尝试在列表中查找
            found = False
            for i in range(self.count()):
                item = self.item(i)
                item_widget = self.itemWidget(item)
                if isinstance(item_widget, TaskListItem) and item_widget.task.item_id == task_id:
                    item_widget.update_status(status)
                    # 更新映射，以便下次直接找到
                    self._task_widgets[task_id] = item_widget
                    # 清除待处理状态
                    self._pending_task_statuses.pop(task_id, None)
                    found = True
                    break
            
            # 如果找不到 widget，保存状态待后续应用
            if not found:
                if status:
                    self._pending_task_statuses[task_id] = status
                else:
                    # 如果状态为空（清除状态），从待处理列表中移除
                    self._pending_task_statuses.pop(task_id, None)
    


class ConfigListWidget(BaseListWidget):
    """配置拖拽列表组件：只支持添加/删除配置项，无复选框"""
    _CONFIG_ITEM_HEIGHT = 44

    def __init__(self, service_coordinator: ServiceCoordinator, parent=None):
        # 注意：Qt 可能在构造期间触发 eventFilter，因此必须在 super().__init__ 前初始化 _locked
        self._locked: bool = False
        super().__init__(service_coordinator, parent)
        # 运行中需要屏蔽用户点击/键盘切换，但保留滚轮滚动查看
        self.installEventFilter(self)
        try:
            self.viewport().installEventFilter(self)
        except Exception:
            pass

        self.item_selected.connect(self._on_item_selected_to_service)

        self.service_coordinator.view_signals.config_added.connect(self.add_config)
        self.service_coordinator.view_signals.config_removed.connect(
            self.remove_config
        )
        self.service_coordinator.view_signals.config_changed.connect(
            self._on_config_changed
        )
        self.service_coordinator.view_signals.runtime_state_changed.connect(
            self._on_runtime_state_changed
        )
        self.update_list()

    def set_locked(self, locked: bool):
        """锁定后禁止用户通过点击/键盘切换配置，同时禁用配置项右键编辑入口。"""
        locked = bool(locked)
        if self._locked == locked:
            return
        self._locked = locked
        # 同步每个 ConfigListItem 的锁定状态（用于禁用右键重命名等）
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, ConfigListItem) and hasattr(widget, "set_locked"):
                try:
                    widget.set_locked(locked)
                except Exception:
                    pass

    def eventFilter(self, obj, event):
        if not self._locked:
            return super().eventFilter(obj, event)

        et = event.type()

        # 屏蔽“切换配置”的交互：仅拦截左键（保留右键菜单与滚轮）
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            try:
                btn = event.button()
            except Exception:
                btn = None
            if btn in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
                return True
        if et == QEvent.Type.MouseButtonDblClick:
            try:
                btn = event.button()
            except Exception:
                btn = None
            if btn == Qt.MouseButton.LeftButton:
                return True

        # 屏蔽键盘切换与删除类按键
        if et in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            try:
                key = event.key()
            except Exception:
                key = None
            if key in (
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
                Qt.Key.Key_PageUp,
                Qt.Key.Key_PageDown,
                Qt.Key.Key_Home,
                Qt.Key.Key_End,
                Qt.Key.Key_Delete,
                Qt.Key.Key_Backspace,
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
                Qt.Key.Key_Space,
            ):
                return True

        return super().eventFilter(obj, event)

    def _on_item_selected_to_service(self, item_id: str):
        if self._locked:
            # Multi-instance off: ignore user clicks; keep the current highlight.
            current_config_id = self.service_coordinator.configs.current_config_id
            if current_config_id and current_config_id != item_id:
                self._select_config_by_id(current_config_id)
            return
        if not self.service_coordinator.select_config(item_id):
            current_config_id = self.service_coordinator.configs.current_config_id
            if current_config_id:
                self._select_config_by_id(current_config_id)

    def update_list(self):
        """刷新配置列表UI"""
        self.clear()
        config_summaries = self.service_coordinator.configs.list_configs()
        for summary in config_summaries:
            if isinstance(summary, dict):
                config_id = summary.get("item_id")
            else:
                try:
                    config_id = summary.item_id
                except Exception:
                    config_id = None
            if config_id:
                cfg = self.service_coordinator.configs.get_config(config_id)
                if cfg:
                    self._add_config_to_list(cfg)
        
        # 选中当前配置
        current_config_id = self.service_coordinator.configs.current_config_id
        if current_config_id:
            self._select_config_by_id(current_config_id)

    def _add_config_to_list(self, config: ConfigItem):
        """添加单个配置项到列表"""
        list_item = QListWidgetItem()
        # 显式固定 item 高度，避免 hover/选中动画区域被 Qt 计算成更高
        list_item.setSizeHint(QSize(0, self._CONFIG_ITEM_HEIGHT))
        config_widget = ConfigListItem(config, self.service_coordinator)
        config_widget.set_runtime_state(
            self.service_coordinator.get_runtime_state(config.item_id)
        )
        self.addItem(list_item)
        self.setItemWidget(list_item, config_widget)

    def _on_runtime_state_changed(self, config_id: str, state: str) -> None:
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if (
                isinstance(widget, ConfigListItem)
                and widget.item.item_id == config_id
            ):
                widget.set_runtime_state(state)
                break

    def _select_config_by_id(self, config_id: str):
        """根据配置ID选中对应的列表项"""
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, ConfigListItem) and widget.item.item_id == config_id:
                self.setCurrentRow(i)
                break

    def _on_config_changed(self, config_id: str):
        """服务层配置切换时同步高亮配置项。"""
        self._select_config_by_id(config_id)
        signalBus.title_changed.emit()

    def add_config(self, config: ConfigItem):
        """添加配置项到列表"""
        self._add_config_to_list(config)
        # 新增时尝试选中它，保持UI当前配置与服务一致
        self._select_config_by_id(config.item_id)

    def remove_config(self, config_id: str):
        """移除配置项"""
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if isinstance(widget, ConfigListItem) and widget.item.item_id == config_id:
                self.takeItem(i)
                widget.deleteLater()
                break

    def set_current_config(self, config_id: str):
        """设置当前选中配置项"""
        self.select_item(config_id)

