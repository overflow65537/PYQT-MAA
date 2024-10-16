import os
import json
import psutil
import socket

def Read_Config(paths):
    # 打开json并传入MAA_data
    if os.path.exists(paths):
        with open(paths,"r",encoding='utf-8') as MAA_Config:
            MAA_data = json.load(MAA_Config)
            return MAA_data
    else:
        return False
    
def Save_Config(paths,data):
     # 打开json并写入data内数据
    with open(paths,"w",encoding='utf-8') as MAA_Config:
        json.dump(data,MAA_Config,indent=4,ensure_ascii=False)

def gui_init(resource_Path,maa_pi_config_Path,interface_Path):
    if not os.path.exists(resource_Path):
        return False

    elif os.path.exists(resource_Path) and os.path.exists(maa_pi_config_Path) and os.path.exists(interface_Path):
        #获取初始resource序号
        Add_Resource_Type_Select_Values = []
        for a in Read_Config(interface_Path)["resource"]:
            Add_Resource_Type_Select_Values.append(a["name"])
        Resource_Type = Read_Config(maa_pi_config_Path)["resource"]
        if Resource_Type != "":
            Resource_count = 0
            for b in Add_Resource_Type_Select_Values:
                if b == Resource_Type:
                    break
                else:
                    Resource_count+=1
        

        #获取初始Controller序号
        Add_Controller_Type_Select_Values = []
        for c in Read_Config(interface_Path)["controller"]:
            Add_Controller_Type_Select_Values.append(c["name"])
        Controller_Type = Read_Config(maa_pi_config_Path)["controller"] ["name"]

        if Controller_Type != "":
            Controller_count = 0
            for d in Add_Controller_Type_Select_Values:
                if d == Controller_Type:
                    break
                else:
                    Controller_count+=1


        #初始显示
        init_ADB_Path = Read_Config(maa_pi_config_Path)["adb"]["adb_path"]
        init_ADB_Address = Read_Config(maa_pi_config_Path)["adb"]["address"]
        init_Resource_Type = Resource_count
        init_Controller_Type = Controller_count
        return_init = {"init_ADB_Path":init_ADB_Path,"init_ADB_Address":init_ADB_Address,"init_Resource_Type":init_Resource_Type,"init_Controller_Type":init_Controller_Type}
        return return_init
        
    

def Get_Values_list2(path,key1):
    List = []
    for i in Read_Config(path)[key1]:
        List.append(i)
    return List

def Get_Values_list(path,key1):
    #获取组件的初始参数
    List = []
    for i in Read_Config(path)[key1]:
        List.append(i["name"])
    return List
        
def Get_Values_list_Option(path,key1):
    #获取组件的初始参数
    List = []
    for i in Read_Config(path)[key1]:
        if i["option"]!=[]:
            Option_text = str(i["name"])+" "
            Option_Lens = len(i["option"])
            for t in range(0,Option_Lens,1):
                Option_text+=str(i["option"][t]["value"])+" "
            List.append(Option_text)
        else:
            List.append(i["name"])
    return List

def Get_Task_List(target):
    #输入option名称来输出一个包含所有该option中所有cases的name列表
    #具体逻辑为 interface.json文件/option键/选项名称/cases键/键为空,所以通过len计算长度来选择最后一个/name键
    lists = []
    Task_Config = Read_Config(os.path.join(os.getcwd(),"interface.json"))["option"][target]["cases"]
    Lens = len(Task_Config)-1
    for i in range(Lens,-1,-1):
        lists.append(Task_Config[i]["name"])
    return lists
def find_process_by_name(process_name):  
    # 遍历所有程序找到指定程序
    for proc in psutil.process_iter(["name","exe"]):  
        try:  
            if proc.info["name"].lower() == process_name.lower():  
                #如果一样返回可执行文件的绝对路径
                return proc.info["exe"]  
        except:  
            return False  
def find_existing_file(info_dict):  
    #输入一个包含可执行文件的绝对路径和可能存在ADB的相对路径,输出ADB文件的绝对地路径
    exe_path = info_dict.get("exe_path").rsplit(os.sep,1)[0]
    may_paths = info_dict.get("may_path", [])
    if not exe_path or not may_paths:  
        return False   
    
    # 遍历所有可能的相对路径  
    for path in may_paths:  
        
        # 拼接完整路径  
        full_path = os.path.join(exe_path, path)  
        # 检查文件是否存在  
        if os.path.exists(full_path):  
            return full_path  
    
    # 如果没有找到任何存在的文件  
    return False   
def check_port(port):  
    port_result = []
    for p in port:
        p = int(p.rsplit(":",1)[1])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  
        try:  
            # 尝试连接到127.0.0.1的指定端口  
            result = s.connect_ex(('127.0.0.1', p))  
            # 如果connect_ex返回0，表示连接成功，即端口开启  
            if result == 0:
                port_result.append("127.0.0.1:"+str(p))  
        except socket.error:  
            pass
        finally:  
            s.close() 
    return port_result