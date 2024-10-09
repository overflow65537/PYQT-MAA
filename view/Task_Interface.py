from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QWidget, QGraphicsDropShadowEffect
from qfluentwidgets import FluentIcon, setFont, InfoBarIcon
from .tool import *

from view.Ui_Task_Interface import Ui_Task_Interface

import os, threading
from maa.toolkit import Toolkit,NotificationHandler
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

class TaskInterface(Ui_Task_Interface, QWidget):

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
        # 初始化显示
        global interface_Path 
        global maa_pi_config_Path
        global resource_Path

        interface_Path = os.path.join(os.getcwd(),"interface.json")
        maa_pi_config_Path = os.path.join(os.getcwd(),"config","maa_pi_config.json")
        resource_Path = os.path.join(os.getcwd(),"resource")
        return_init = gui_init(resource_Path,maa_pi_config_Path,interface_Path)

        self.Task_List.addItems(Get_Values_list_Option(maa_pi_config_Path,"task"))
        
        self.Resource_Combox.addItems(Get_Values_list(interface_Path,key1 = "resource"))
        self.Resource_Combox.setCurrentIndex(return_init["init_Resource_Type"])
        self.Control_Combox.addItems(Get_Values_list(interface_Path,key1 = "controller"))
        self.Control_Combox.setCurrentIndex(return_init["init_Controller_Type"])
        self.SelectTask_Combox_1.addItems(Get_Values_list(interface_Path,key1 = "task"))
        # 隐藏任务选项
        self.SelectTask_Combox_2.hide()
        self.SelectTask_Combox_3.hide()
        self.SelectTask_Combox_4.hide()
        # 隐藏任务标签
        self.TaskName_Title_2.hide()
        self.TaskName_Title_3.hide()
        self.TaskName_Title_4.hide()
        # 绑定信号
        self.AddTask_Button.clicked.connect(self.Add_Task)
        self.Delete_Button.clicked.connect(self.Delete_Task)
        self.MoveUp_Button.clicked.connect(self.Move_Up)
        self.MoveDown_Button.clicked.connect(self.Move_Down)
        self.SelectTask_Combox_1.activated.connect(self.Add_Select_Task_More_Select)
        self.Resource_Combox.activated.connect(self.Save_Resource)
        self.Control_Combox.activated.connect(self.Save_Controller)
        self.AutoDetect_Button.clicked.connect(self.Auto_Detect_ADB)
        self.StartTask_Button.clicked.connect(self.Start_Up)

    def Start_Up(self):
        threading.Thread(target=self._Start_Up, daemon=True).start()

    def _Start_Up(self):

        Toolkit.pi_run_cli(os.getcwd(), os.getcwd(), True)

    def Add_Task(self):
        # 添加任务
        Option = []
        Select_Target = self.SelectTask_Combox_1.currentText()
        MAA_Pi_Config = Read_Config(interface_Path)
        Option_list = []

        for i in MAA_Pi_Config["task"]:
            # 将所有带有option的键值存进Option_list
            if i.get("option")!= None:
                Option_list.append(i)
        for i in Option_list:
            # 检查当前选中任务的是否为option_list中的元素
            if Select_Target == i["name"]:  
                l = len(i["option"])  
                options_dicts = []  
                # 根据option的长度，循环添加选项到列表中  
                for index in range(l):  
                    select_box_name = f"SelectTask_Combox_{index + 2}"  
                    selected_value = getattr(self, select_box_name).currentText()
                    options_dicts.append({"name": i["option"][index], "value": selected_value})  
                Option.extend(options_dicts)
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["task"].append({"name": Select_Target,"option": Option})
        Save_Config(maa_pi_config_Path,MAA_Pi_Config)
        self.Task_List.clear()
        self.Task_List.addItems(Get_Values_list_Option(maa_pi_config_Path,"task"))
        
    def Delete_Task(self):

        Select_Target = self.Task_List.currentRow()
        
        self.Task_List.takeItem(Select_Target)
        Task_List = Get_Values_list2(maa_pi_config_Path,"task")
        try:
            del Task_List[Select_Target]
        except IndexError:
            pass
        else:
            MAA_Pi_Config = Read_Config(maa_pi_config_Path)
            del MAA_Pi_Config["task"]
            MAA_Pi_Config.update({"task":Task_List})
            Save_Config(maa_pi_config_Path,MAA_Pi_Config)
        if Select_Target == 0:
            self.Task_List.setCurrentRow(Select_Target)
        elif Select_Target != -1:
            self.Task_List.setCurrentRow(Select_Target-1)
        

    def Move_Up(self):
        
        Select_Target = self.Task_List.currentRow()
        if Select_Target == 0:
            pass
        elif Select_Target != -1:
            MAA_Pi_Config = Read_Config(maa_pi_config_Path)
            Select_Task = MAA_Pi_Config["task"].pop(Select_Target)
            MAA_Pi_Config["task"].insert(Select_Target-1, Select_Task)
            Save_Config(maa_pi_config_Path,MAA_Pi_Config)
            self.Task_List.clear()
            self.Task_List.addItems(Get_Values_list_Option(maa_pi_config_Path,"task"))
            self.Task_List.setCurrentRow(Select_Target-1)
        

    def Move_Down(self):

        Select_Target = self.Task_List.currentRow()
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        if Select_Target >= len(MAA_Pi_Config["task"])-1:
            pass
        elif Select_Target < len(MAA_Pi_Config["task"]):
            Select_Task = MAA_Pi_Config["task"].pop(Select_Target)
            MAA_Pi_Config["task"].insert(Select_Target+1, Select_Task)
            Save_Config(maa_pi_config_Path,MAA_Pi_Config)
            self.Task_List.clear()
            self.Task_List.addItems(Get_Values_list_Option(maa_pi_config_Path,"task"))
            self.Task_List.setCurrentRow(Select_Target+1)
    
    def Save_Resource(self):
        Resource_Type_Select = self.Resource_Combox.currentText()
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["resource"] = Resource_Type_Select
        Save_Config(maa_pi_config_Path,MAA_Pi_Config)

    def Save_Controller(self):
        Controller_Type_Select = self.Control_Combox.currentText()
        interface_Controller = Read_Config(interface_Path)["controller"]
        
        for i in interface_Controller:
            if i["name"] == Controller_Type_Select:
                Controller_target = i
        MAA_Pi_Config = Read_Config(maa_pi_config_Path)
        MAA_Pi_Config["controller"] = Controller_target
        Save_Config(maa_pi_config_Path,MAA_Pi_Config)
    
    def Add_Select_Task_More_Select(self):

        self.clear_extra_widgets()

        select_target = self.SelectTask_Combox_1.currentText()

        MAA_Pi_Config = Read_Config(interface_Path)  

        for task in MAA_Pi_Config["task"]:  
                if task["name"] == select_target and task.get("option") is not None:  
                    option_length = len(task["option"])  
    
                    # 根据option数量动态显示下拉框和标签  
                    for i in range(option_length):  
                        select_box = getattr(self, f"SelectTask_Combox_{i+2}")  
                        label = getattr(self,f"TaskName_Title_{i+2}")  
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

            label = getattr(self,f"TaskName_Title_{i}") 
            label.setText("任务")
            label.hide()
    
    def Auto_Detect_ADB(self):
        # 使用threading启动一个新线程来执行更新操作  
        threading.Thread(target=self._Auto_Detect_ADB, daemon=True).start()
    def _Auto_Detect_ADB(self):
        emulator = [
            {
                "name":"BlueStacks",
                "exe_name":"HD-Player.exe",
                "may_path":["HD-Adb.exe","Engine\\ProgramFiles\\HD-Adb.exe"],
                "port":["127.0.0.1:5555","127.0.0.1:5556","127.0.0.1:5565","127.0.0.1:5575"]
            },
            {
                "name":"MuMuPlayer12",
                "exe_name":"MuMuPlayer.exe",
                "may_path":["vmonitor\\bin\\adb_server.exe","MuMu\\emulator\\nemu\\vmonitor\\bin\\adb_server.exe","adb.exe"],
                "port":["127.0.0.1:16384", "127.0.0.1:16416", "127.0.0.1:16448"]
            },
            {
                "name":"LDPlayer",
                "exe_name":"dnplayer.exe",
                "may_path":["adb.exe"],
                "port":["127.0.0.1:5555","127.0.0.1:5556"]
            },
            {
                "name":"Nox",
                "exe_name":"Nox.exe",
                "may_path":["nox_adb.exe"],
                "port":["127.0.0.1:62001", "127.0.0.1:59865"]
            },
            {
                "name":"MuMuPlayer6",
                "exe_name":"NemuPlayer.exe",
                "may_path":["vmonitor\\bin\\adb_server.exe","MuMu\\emulator\\nemu\\vmonitor\\bin\\adb_server.exe","adb.exe"],
                "port":["127.0.0.1:7555"]
            },
            {
                "name":"MEmuPlayer.exe",
                "exe_name":"MEmu",
                "may_path":["adb.exe"],
                "port":["127.0.0.1:21503"]
            },
            {
                "name":"ADV",
                "exe_name":"qemu-system.exe",
                "may_path":["..\\..\\..\\platform-tools\\adb.exe"],
                "port":["127.0.0.1:5555"]
            }
]       
        
        global emulator_result
        emulator_result = []
        for app in emulator:
            process_path = find_process_by_name(app["exe_name"])
            
            if process_path:
                # 判断程序是否正在运行,是进行下一步,否则放弃
                info_dict = {"exe_path":process_path,"may_path":app["may_path"]}
                ADB_path = find_existing_file(info_dict)
                if ADB_path:
                    
                    # 判断ADB地址是否存在,是进行下一步,否则放弃
                    port_data = check_port(app["port"])
                    if port_data:
                        # 判断端口是否存在,是则组合字典,否则放弃
                        emulator_result.extend([{"name":app["name"],"path":ADB_path,"port": item} for item in port_data])
        
        if emulator_result:
            processed_list = [] 
            print("提示","查找完成")
            for i in emulator_result:
                processed_s = i["name"]
                processed_list.append(processed_s)
            self.AutoDetect_Combox.addItems(processed_list)
        else:
            print("错误","未找到模拟器")

    def Replace_ADB_data(self):
        print(emulator_result[self.AutoDetect_Combox.currentText()]["port"])
        print(emulator_result[self.AutoDetect_Combox.currentText()]["path"])
