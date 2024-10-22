from typing import Union
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from qfluentwidgets import (FluentIconBase, pyqtSignal,
                            SwitchButton, IndicatorPosition,
                            SettingCard)
from ..utils.tool import Read_Config, Save_Config
import os


class CustomSwitchSettingCard(SettingCard):

    checkedChanged = pyqtSignal(bool)

    def __init__(self,
                 icon: Union[str, QIcon, FluentIconBase],
                 title,
                 target: str,
                 content=None,
                 parent=None):
        super().__init__(icon, title, content, parent)
        self.target = target
        self.switchButton = SwitchButton(
            self.tr('Off'), self, IndicatorPosition.RIGHT)

        # add switch button to layout
        self.hBoxLayout.addWidget(
            self.switchButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)
        check_state = Read_Config(os.path.join(
            os.getcwd(), "config", "custom_config.json"))[self.target]
        self.switchButton.setChecked(check_state)

        self.switchButton.checkedChanged.connect(self.__onCheckedChanged)

    def __onCheckedChanged(self, isChecked: bool):
        data = Read_Config(os.path.join(
            os.getcwd(), "config", "custom_config.json"))
        data[self.target] = isChecked
        Save_Config(os.path.join(os.getcwd(), "config",
                    "custom_config.json"), data)
