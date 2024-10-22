from qfluentwidgets import ListWidget
from ..utils.tool import Read_Config, Save_Config
import os


class ListWidge_Menu_Draggable(ListWidget):
    def __init__(self, parent=None):
        super(ListWidge_Menu_Draggable, self).__init__(parent)

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
