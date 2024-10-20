from maa.notification_handler import NotificationHandler, NotificationType
from qfluentwidgets import TextEdit
from datetime import datetime  


class MyNotificationHandler(NotificationHandler):
    def __init__(self, TaskOutput_Text : TextEdit):  
        super().__init__()  
        self.TaskOutput_Text = TaskOutput_Text
    
    def on_resource_loading(
        self,
        noti_type: NotificationType,
        detail: NotificationHandler.ResourceLoadingDetail,
    ):
        pass


    def on_controller_action(
        self,
        noti_type: NotificationType,
        detail: NotificationHandler.ControllerActionDetail,
    ):
        now_time = datetime.now().strftime("%H:%M:%S") 
        if noti_type.value == 1:
            self.TaskOutput_Text.append(f"{now_time}"+" 连接中")
        elif noti_type.value == 2:
            self.TaskOutput_Text.append(f"{now_time}"+" 连接成功")
        elif noti_type.value == 3:
            self.TaskOutput_Text.append(f"{now_time}"+" 连接失败")
        else:
            self.TaskOutput_Text.append(f"{now_time}"+" 连接状态未知")

    def on_tasker_task(
        self, noti_type: NotificationType, detail: NotificationHandler.TaskerTaskDetail
            ):
        now_time = datetime.now().strftime("%H:%M:%S") 
        status_map = {  0: "未知",  1: "运行中",  2: "成功",  3: "失败"  }  
        self.TaskOutput_Text.append(f"{now_time}"+" "+f"{detail.entry}"+" "+f"{status_map[noti_type.value]}")
