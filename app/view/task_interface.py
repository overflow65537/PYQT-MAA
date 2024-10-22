from maa.toolkit import Toolkit

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import InfoBar, InfoBarPosition

from ..view.UI_task_interface import Ui_Task_Interface
from ..view.setting_interface import SettingInterface
from ..logic.notification import MyNotificationHandler
from ..logic.auto_detect_ADB_Thread import AutoDetectADBThread
from ..utils.tool import (Get_Values_list_Option, Get_Values_list, gui_init,
                          Save_Config, Read_Config, Get_Values_list2,
                          Get_Task_List)
import os
import threading


class TaskInterface(Ui_Task_Interface, QWidget):

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)

        # 资源文件位置
        global interface_Path
        global maa_pi_config_Path
        global resource_Path
        interface_Path = os.path.join(os.getcwd(), "interface.json")
        maa_pi_config_Path = os.path.join(
            os.getcwd(), "config", "maa_pi_config.json")
        resource_Path = os.path.join(os.getcwd(), "resource")
        # 初始化组件
        self.First_Start(interface_Path, maa_pi_config_Path, resource_Path)
        self.init_widget()

    def init_widget(self):

        self._auto_detect_adb_thread = AutoDetectADBThread(self)
        self.MyNotificationHandler = MyNotificationHandler(self)

        # 隐藏任务选项
        self.SelectTask_Combox_2.hide()
        self.SelectTask_Combox_3.hide()
        self.SelectTask_Combox_4.hide()

        # 隐藏任务标签
        self.TaskName_Title_2.hide()
        self.TaskName_Title_3.hide()
        self.TaskName_Title_4.hide()
        self.Topic_Text.hide()

        # 绑定信号
        self.MyNotificationHandler.callbackSignal.callback.connect(
            self.change_output)
        self._auto_detect_adb_thread.signal.adb_detected.connect(
            self.On_ADB_Detected)
        self.AddTask_Button.clicked.connect(self.Add_Task)
        self.Delete_Button.clicked.connect(self.Delete_Task)
        self.MoveUp_Button.clicked.connect(self.Move_Up)
        self.MoveDown_Button.clicked.connect(self.Move_Down)
        self.SelectTask_Combox_1.activated.connect(
            self.Add_Select_Task_More_Select)
        self.Resource_Combox.currentTextChanged.connect(self.Save_Resource)
        self.Control_Combox.currentTextChanged.connect(self.Save_Controller)
        self.AutoDetect_Button.clicked.connect(self.Start_ADB_Detection)
        self.S2_Button.clicked.connect(self.Start_Up)
        self.Autodetect_combox.currentTextChanged.connect(self.Save_ADB_Config)
        self.Task_List.currentRowChanged.connect(self.Task_List_Changed)

    def First_Start(self, interface_Path, maa_pi_config_Path, resource_Path):
        # 资源文件和配置文件全存在
        if (os.path.exists(resource_Path)
           and os.path.exists(interface_Path)
           and os.path.exists(maa_pi_config_Path)):
            # 填充数据至组件并设置初始值
            self.Task_List.addItems(
                Get_Values_list_Option(maa_pi_config_Path, "task"))
            self.Resource_Combox.addItems(
                Get_Values_list(interface_Path, key1="resource"))
            self.Control_Combox.addItems(
                Get_Values_list(interface_Path, key1="controller"))
            self.SelectTask_Combox_1.addItems(
                Get_Values_list(interface_Path, key1="task"))
            return_init = gui_init(
                resource_Path, maa_pi_config_Path, interface_Path)
            self.Resource_Combox.setCurrentIndex(
                return_init["init_Resource_Type"])
            self.Control_Combox.setCurrentIndex(
                return_init["init_Controller_Type"])

        # 配置文件不在

        elif (os.path.exists(resource_Path)
              and os.path.exists(interface_Path)
              and not (os.path.exists(maa_pi_config_Path))):
            # 填充数据至组件
            data = {"adb": {"adb_path": "", "address": "127.0.0.1:0",
                            "config": {}},
                    "controller": {"name": ""},
                    "gpu": -1,
                    "resource": "",
                    "task": [],
                    "win32": {"_placeholder": 0}}
            Save_Config(maa_pi_config_Path, data)
            self.Resource_Combox.addItems(
                Get_Values_list(interface_Path, key1="resource"))
            self.Control_Combox.addItems(
                Get_Values_list(interface_Path, key1="controller"))
            self.SelectTask_Combox_1.addItems(
                Get_Values_list(interface_Path, key1="task"))
            self.Save_Resource()
            self.Save_Controller()

        # 全不在
        else:
            InfoBar.error(
                title='错误',
                content="未检测到资源文件",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=-1,
                parent=self
            )

    def Start_Up(self):
        self.TaskOutput_Text.clear()
        threading.Thread(target=self._Start_Up, daemon=True).start()

    def _Start_Up(self):
        # notification_handler = MyNotificationHandler(callback.callback_sig)

        Toolkit.pi_run_cli(os.getcwd(), os.getcwd(), True,
                           notification_handler=self.MyNotificationHandler)

    def Task_List_Changed(self, msg):
        pass

    def Add_Task(self):
        # 添加任务
        Option = []
        Select_Target = self.SelectTask_Combox_1.currentText()
        MAA_Pi_Config = Read_Config(interface_Path)
        Option_list = []

        for i in MAA_Pi_Config["task"]:
            # 将所有带有option的键值存进Option_list
            if i.get("option") is not None:
                Option_list.append(i)
        for i in Option_list:
            # 检查当前选中任务的是否为option_list中的元素
            if Select_Target == i["name"]:
                loop_count = len(i["option"])
                options_dicts = []
                # 根据option的长度，循环添加选项到列表中
                for index in range(loop_count):
                    select_box_name = f"SelectTask_Combox_{index + 2}"
                    selected_value = getattr(
                        self, select_box_name).currentText()
                    options_dicts.append(
                        {"name": i["option"][index], "value": selected_value})
                Option.extend(options_dicts)
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["task"].append({"name": Select_Target, "option": Option})
        Save_Config(maa_pi_config_Path, MAA_Pi_Config)
        self.Task_List.clear()
        self.Task_List.addItems(
            Get_Values_list_Option(maa_pi_config_Path, "task"))

    def Delete_Task(self):

        Select_Target = self.Task_List.currentRow()

        self.Task_List.takeItem(Select_Target)
        Task_List = Get_Values_list2(maa_pi_config_Path, "task")
        try:
            del Task_List[Select_Target]
        except IndexError:
            InfoBar.error(
                title='错误',
                content="没有任务可以被删除",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
                parent=self
            )
        else:
            MAA_Pi_Config = Read_Config(maa_pi_config_Path)
            del MAA_Pi_Config["task"]
            MAA_Pi_Config.update({"task": Task_List})
            Save_Config(maa_pi_config_Path, MAA_Pi_Config)
        if Select_Target == 0:
            self.Task_List.setCurrentRow(Select_Target)
        elif Select_Target != -1:
            self.Task_List.setCurrentRow(Select_Target-1)

    def Move_Up(self):

        Select_Target = self.Task_List.currentRow()
        if Select_Target == 0:
            InfoBar.error(
                title='错误',
                content="已经是首位任务",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
                parent=self
            )
        elif Select_Target != -1:
            MAA_Pi_Config = Read_Config(maa_pi_config_Path)
            Select_Task = MAA_Pi_Config["task"].pop(Select_Target)
            MAA_Pi_Config["task"].insert(Select_Target-1, Select_Task)
            Save_Config(maa_pi_config_Path, MAA_Pi_Config)
            self.Task_List.clear()
            self.Task_List.addItems(
                Get_Values_list_Option(maa_pi_config_Path, "task"))
            self.Task_List.setCurrentRow(Select_Target-1)

    def Move_Down(self):

        Select_Target = self.Task_List.currentRow()
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        if Select_Target >= len(MAA_Pi_Config["task"])-1:
            InfoBar.error(
                title='错误',
                content="已经是末位任务",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
                parent=self
            )
        elif Select_Target < len(MAA_Pi_Config["task"]):
            Select_Task = MAA_Pi_Config["task"].pop(Select_Target)
            MAA_Pi_Config["task"].insert(Select_Target+1, Select_Task)
            Save_Config(maa_pi_config_Path, MAA_Pi_Config)
            self.Task_List.clear()
            self.Task_List.addItems(
                Get_Values_list_Option(maa_pi_config_Path, "task"))
            self.Task_List.setCurrentRow(Select_Target+1)

    def Save_Resource(self):
        Resource_Type_Select = self.Resource_Combox.currentText()
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["resource"] = Resource_Type_Select
        Save_Config(maa_pi_config_Path, MAA_Pi_Config)

    def Save_Controller(self):
        Controller_Type_Select = self.Control_Combox.currentText()
        interface_Controller = Read_Config(interface_Path)["controller"]

        for i in interface_Controller:
            if i["name"] == Controller_Type_Select:
                if i["type"] == "Adb":
                    Controller_target = i
                    del Controller_target['type']
                else:
                    Controller_target = i
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["controller"] = Controller_target
        Save_Config(maa_pi_config_Path, MAA_Pi_Config)

    def Add_Select_Task_More_Select(self):

        self.clear_extra_widgets()

        select_target = self.SelectTask_Combox_1.currentText()

        MAA_Pi_Config = Read_Config(interface_Path)

        for task in MAA_Pi_Config["task"]:
            if (task["name"] == select_target
               and task.get("option") is not None):
                option_length = len(task["option"])

                # 根据option数量动态显示下拉框和标签
                for i in range(option_length):
                    select_box = getattr(self, f"SelectTask_Combox_{i+2}")
                    label = getattr(self, f"TaskName_Title_{i+2}")
                    option_name = task["option"][i]

                    # 填充下拉框数据
                    select_box.addItems(list(Get_Task_List(option_name)))
                    select_box.show()

                    # 显示标签
                    label.setText(option_name)
                    label.show()

                break  # 找到匹配的任务后退出循环

    def clear_extra_widgets(self):
        for i in range(2, 5):
            select_box = getattr(self, f"SelectTask_Combox_{i}")
            select_box.clear()
            select_box.hide()

            label = getattr(self, f"TaskName_Title_{i}")
            label.setText("任务")
            label.hide()

    def change_output(self, msg):
        self.TaskOutput_Text.append(msg)

    def Start_ADB_Detection(self):
        # 检测ADB线程
        self._auto_detect_adb_thread.start()
        InfoBar.info(
            title='提示',
            content="正在检测模拟器",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2000,    # won't disappear automatically
            parent=self
        )

    def On_ADB_Detected(self, emu):
        global emu_data
        emu_data = emu
        if emu == []:
            InfoBar.error(
                title='错误',
                content="未检测到模拟器",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=-1,    # won't disappear automatically
                parent=self
            )
        else:
            processed_list = []
            for i in emu:
                processed_s = i["name"]
                processed_list.append(processed_s)

            InfoBar.success(
                title='成功',
                content=f'检测到{processed_list[0]}',
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
                parent=self
            )
            self.Autodetect_combox.addItems(processed_list)

    def Save_ADB_Config(self):
        target = self.Autodetect_combox.text()
        for i in emu_data:
            if i["name"] == target:
                result = i

        self.settingInterface = SettingInterface(self)

        port_data = Read_Config(os.path.join(
            os.getcwd(), "config", "maa_pi_config.json"))
        port_data["adb"]["adb_path"] = result["path"]
        self.settingInterface.ADBPath.setContent(result["path"])

        Save_Config(os.path.join(os.getcwd(), "config",
                    "maa_pi_config.json"), port_data)
        path_data = Read_Config(os.path.join(
            os.getcwd(), "config", "maa_pi_config.json"))
        path_data["adb"]["address"] = result["port"]
        self.settingInterface.ADBPort.setContent(result["port"])

        Save_Config(os.path.join(os.getcwd(), "config",
                    "maa_pi_config.json"), path_data)
