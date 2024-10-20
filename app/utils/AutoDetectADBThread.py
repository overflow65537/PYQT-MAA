from PyQt6.QtCore import QThread, pyqtSignal, QObject
from ..utils.tool import Read_Config, find_process_by_name, find_existing_file, check_port
import os


class AutoDetectADBSignal(QObject):  
# 检测ADB任务的信号,传回list
    adb_detected = pyqtSignal(list) 

    
class AutoDetectADBThread(QThread):  
    def __init__(self, parent=None):  
        super(AutoDetectADBThread, self).__init__(parent)  
        self.signal = AutoDetectADBSignal()  # 创建信号对象  
  
    def run(self):  
        emulator_result = []
        emulator_list = Read_Config(os.path.join(os.getcwd(), "config", "emulator.json"))
        for app in emulator_list:
            process_path = find_process_by_name(app["exe_name"])
            
            if process_path:
                # 判断程序是否正在运行,是进行下一步,否则放弃
                may_path = []
                for i in app["may_path"]:
                    may_path.append(os.path.join(*i))
                info_dict = {"exe_path":process_path,"may_path":may_path}
                ADB_path = find_existing_file(info_dict)
                if ADB_path:
                    
                    # 判断ADB地址是否存在,是进行下一步,否则放弃
                    port_data = check_port(app["port"])
                    if port_data:
                        # 判断端口是否存在,是则组合字典,否则放弃
                        emulator_result.extend([{"name":app["name"],"path":ADB_path,"port": item} for item in port_data])
        
        if emulator_result:
           
            self.signal.adb_detected.emit(emulator_result)
        else:
            None_ADB_data = []
            self.signal.adb_detected.emit(None_ADB_data)
