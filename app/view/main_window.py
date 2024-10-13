# coding: utf-8
from PyQt6.QtGui import QIcon
from qfluentwidgets import FluentWindow                    
from qfluentwidgets import FluentIcon as FIF
from .task_interface import TaskInterface
# from .setting_interface import SettingInterface




class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        self.initWindow()
        # create sub interface
        self.TaskInterface = TaskInterface(self)
        # self.settingInterface = SettingInterface(self)
        # add items to navigation interface
        self.initNavigation()

    def initNavigation(self):
        # add navigation items
        self.addSubInterface(self.TaskInterface, FIF.CHECKBOX,self.tr('每日任务'))

    def initWindow(self):
        self.resize(960, 780)
        self.setMinimumWidth(760)
        self.setWindowIcon(QIcon(':/gallery/images/logo.png'))
        self.setWindowTitle('PyQt-Fluent-Widgets')



