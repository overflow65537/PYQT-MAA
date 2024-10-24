from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import (
    QSizePolicy,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFormLayout,
    QFrame,
    QAbstractItemView,
)
from qfluentwidgets import (
    PushButton,
    BodyLabel,
    ComboBox,
    ListWidget,
    LineEdit,
    TimeEdit,
)
from ..components.listwidge_menu_draggable import ListWidge_Menu_Draggable


class Ui_Scheduled_Interface(object):
    def setupUi(self, Scheduled_Interface):
        Scheduled_Interface.setObjectName("Scheduled_Interface")
        Scheduled_Interface.resize(900, 600)
        Scheduled_Interface.setMinimumSize(QSize(0, 0))
        # 主窗口

        # 切换配置布局

        self.Combox_layout = QHBoxLayout()
        self.Cfg_Combox = ComboBox(Scheduled_Interface)
        self.Cfg_Combox.setObjectName("Cfg_Combox")
        self.Add_cfg_Button = PushButton(Scheduled_Interface)
        self.Add_cfg_Button.setObjectName("Add_cfg_Button")
        self.Add_cfg_Button.setText("添加")
        self.Add_cfg_Button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.Delete_cfg_Button = PushButton(Scheduled_Interface)
        self.Delete_cfg_Button.setObjectName("Delete_cfg_Button")
        self.Delete_cfg_Button.setText("删除")
        self.Delete_cfg_Button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.Combox_layout.addWidget(self.Cfg_Combox)
        self.Combox_layout.addWidget(self.Add_cfg_Button)
        self.Combox_layout.addWidget(self.Delete_cfg_Button)

        # 列表布局

        self.List_layout = QVBoxLayout()
        self.List_widget = ListWidge_Menu_Draggable(Scheduled_Interface)
        self.List_widget.setObjectName("List_widget")
        self.List_widget.setDragEnabled(True)
        self.List_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.Add_cfg_Button.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum
        )
        self.List_layout.addLayout(self.Combox_layout)
        self.List_layout.addWidget(self.List_widget)

        # 计划任务布局

        self.Schedule_layout = QFormLayout()
        self.Schedule_name_title = BodyLabel(Scheduled_Interface)
        self.Schedule_name_title.setObjectName("Schedule_name_title")
        self.Schedule_name_title.setText("计划任务名称")
        self.Schedule_name_edit = LineEdit(Scheduled_Interface)
        self.Schedule_name_edit.setObjectName("Schedule_name_edit")
        self.Schedule_name_edit.setPlaceholderText("请输入计划任务名称")
        self.Schedule_layout.addRow(self.Schedule_name_title, self.Schedule_name_edit)

        self.Trigger_Time_layout = QGridLayout()
        self.Trigger_Time_title = BodyLabel(Scheduled_Interface)
        self.Trigger_Time_title.setObjectName("Trigger_Time_title")
        self.Trigger_Time_title.setText("触发时间")
        self.Trigger_Time_type = ComboBox(Scheduled_Interface)
        self.Trigger_Time_even_week = ComboBox(Scheduled_Interface)
        self.Trigger_Time_edit = TimeEdit(Scheduled_Interface)
        self.Trigger_Time_edit.setObjectName("Trigger_Time_edit")
        self.Trigger_Time_layout.addWidget(self.Trigger_Time_title, 0, 0)
        self.Trigger_Time_layout.addWidget(self.Trigger_Time_type, 0, 1)
        self.Trigger_Time_layout.addWidget(self.Trigger_Time_even_week, 0, 2)
        self.Trigger_Time_layout.addWidget(self.Trigger_Time_edit, 1, 1)

        self.use_cfg_layout = QFormLayout()
        self.use_cfg_title = BodyLabel(Scheduled_Interface)
        self.use_cfg_title.setObjectName("use_cfg_title")
        self.use_cfg_title.setText("使用配置")
        self.use_cfg_combo = ComboBox(Scheduled_Interface)
        self.use_cfg_combo.setObjectName("use_cfg_combo")
        self.use_cfg_layout.addRow(self.use_cfg_title, self.use_cfg_combo)

        # 总布局

        self.Schedule_layout_all = QVBoxLayout()
        self.cfg_list = ListWidget(Scheduled_Interface)
        self.cfg_list.setObjectName("cfg_list")

        self.Schedule_layout_all.addLayout(self.Schedule_layout)
        self.Schedule_layout_all.addLayout(self.Trigger_Time_layout)
        self.Schedule_layout_all.addLayout(self.use_cfg_layout)
        self.Schedule_layout_all.addWidget(self.cfg_list)

        self.all_layout = QHBoxLayout()

        self.line = QFrame()
        self.line.setFrameShape(QFrame.Shape.VLine)
        self.line.setFrameShadow(QFrame.Shadow.Plain)
        self.all_layout.addLayout(self.List_layout)
        self.all_layout.addWidget(self.line)
        self.all_layout.addLayout(self.Schedule_layout_all)

        Scheduled_Interface.setLayout(self.all_layout)
