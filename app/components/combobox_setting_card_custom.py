from typing import Union

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from qfluentwidgets import (SettingCard, FluentIconBase, ComboBox)
from ..utils.tool import Read_Config, Save_Config

import os


class ComboBoxSettingCardCustom(SettingCard):
    """ Setting card with a combo box """

    def __init__(self,
                 icon: Union[str, QIcon, FluentIconBase],
                 title, target,
                 content=None,
                 texts=None,
                 parent=None):
        super().__init__(icon, title, content, parent)
        self.target = target
        self.comboBox = ComboBox(self)
        self.hBoxLayout.addWidget(
            self.comboBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self.comboBox.addItems(texts)
        CurrentText = Read_Config(os.path.join(
            os.getcwd(), "config", "custom_config.json"))[self.target]
        self.comboBox.setCurrentText(CurrentText)

        self.comboBox.currentIndexChanged.connect(self._onCurrentIndexChanged)

    def _onCurrentIndexChanged(self):
        text = self.comboBox.text()
        data = Read_Config(
            (os.path.join(os.getcwd(), "config", "custom_config.json")))
        data[self.target] = text
        Save_Config(
            (os.path.join(os.getcwd(), "config", "custom_config.json")), data)
