from qfluentwidgets import ListWidget
from ..utils.tool import Read_Config, Save_Config
import os

class RewriteListWidget(ListWidget):
    def __init__(self,parent = None):
        super(RewriteListWidget,self).__init__(parent)
    def dropEvent(self,event):
        maa_pi_config_Path = os.path.join(os.getcwd(),"config","maa_pi_config.json")
        begin = self.currentRow()
        super(RewriteListWidget,self).dropEvent(event)
        end = self.currentRow()
        config = Read_Config(maa_pi_config_Path)
        need_to_move = config["task"].pop(begin)
        config["task"].insert(end,need_to_move)
        Save_Config(maa_pi_config_Path, config)