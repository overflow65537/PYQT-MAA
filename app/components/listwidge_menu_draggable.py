from qfluentwidgets import ListWidget, RoundMenu, Action, MenuAnimationType
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import InfoBar, InfoBarPosition
from ..utils.tool import (Get_Values_list_Option, Save_Config, Read_Config,
                          Get_Values_list2,)
from PyQt6.QtCore import Qt
import os


class ListWidge_Menu_Draggable(ListWidget):
    def __init__(self, parent=None):
        super(ListWidge_Menu_Draggable, self).__init__(parent)
        self.interface_Path = os.path.join(os.getcwd(), "interface.json")
        self.maa_pi_config_Path = os.path.join(
            os.getcwd(), "config", "maa_pi_config.json")
        self.resource_Path = os.path.join(os.getcwd(), "resource")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            item = self.itemAt(event.pos())
            if item:
                self.setCurrentItem(item)
        super(ListWidge_Menu_Draggable, self).mousePressEvent(event)

    def contextMenuEvent(self, e):
        menu = RoundMenu(parent=self)

        selected_row = self.currentRow()

        action_move_up = Action(FIF.UP, 'move up')
        action_move_down = Action(FIF.DOWN, 'move down')
        action_delete = Action(FIF.DELETE, 'delete')

        if selected_row == -1:
            action_move_up.setEnabled(False)
            action_move_down.setEnabled(False)
            action_delete.setEnabled(False)

        action_move_up.triggered.connect(self.Move_Up)
        action_move_down.triggered.connect(self.Move_Down)
        action_delete.triggered.connect(self.Delete_Task)

        menu.addAction(action_move_up)
        menu.addAction(action_move_down)
        menu.addAction(action_delete)

        menu.exec(e.globalPos(), aniType=MenuAnimationType.DROP_DOWN)

    def Delete_Task(self):

        Select_Target = self.currentRow()

        self.takeItem(Select_Target)
        Task_List = Get_Values_list2(self.maa_pi_config_Path, "task")
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
            MAA_Pi_Config = Read_Config(self.maa_pi_config_Path)
            del MAA_Pi_Config["task"]
            MAA_Pi_Config.update({"task": Task_List})
            Save_Config(self.maa_pi_config_Path, MAA_Pi_Config)
        if Select_Target == 0:
            self.setCurrentRow(Select_Target)
        elif Select_Target != -1:
            self.setCurrentRow(Select_Target-1)

    def Move_Up(self):

        Select_Target = self.currentRow()
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
            MAA_Pi_Config = Read_Config(self.maa_pi_config_Path)
            Select_Task = MAA_Pi_Config["task"].pop(Select_Target)
            MAA_Pi_Config["task"].insert(Select_Target-1, Select_Task)
            Save_Config(self.maa_pi_config_Path, MAA_Pi_Config)
            self.clear()
            self.addItems(
                Get_Values_list_Option(self.maa_pi_config_Path, "task"))
            self.setCurrentRow(Select_Target-1)

    def Move_Down(self):

        Select_Target = self.currentRow()
        MAA_Pi_Config = Read_Config(self.maa_pi_config_Path)
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
            Save_Config(self.maa_pi_config_Path, MAA_Pi_Config)
            self.clear()
            self.addItems(
                Get_Values_list_Option(self.maa_pi_config_Path, "task"))
            self.setCurrentRow(Select_Target+1)

    def dropEvent(self, event):
        maa_pi_config_Path = os.path.join(
            os.getcwd(), "config", "maa_pi_config.json")
        begin = self.currentRow()
        super(ListWidge_Menu_Draggable, self).dropEvent(event)
        end = self.currentRow()
        config = Read_Config(maa_pi_config_Path)
        need_to_move = config["task"].pop(begin)
        config["task"].insert(end, need_to_move)
        Save_Config(maa_pi_config_Path, config)
