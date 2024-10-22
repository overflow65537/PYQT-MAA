from typing import Union
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QIcon
from qfluentwidgets import (SettingCard, FluentIconBase, LineEdit)
from ..utils.tool import Read_Config, Save_Config

import os


class LineEditCard(SettingCard):
    """card with a push button"""

    text_change = pyqtSignal()

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        holderText: str,
        title: str,
        target: str = None,
        default: str = "",
        content=None,
        parent=None,
        custom=True,

    ):
        super().__init__(icon, title, content, parent)

        self.target = target

        self.lineEdit = LineEdit(self)
        self.lineEdit.setText(default)
        self.lineEdit.setPlaceholderText(holderText)

        self.hBoxLayout.addWidget(self.lineEdit, 0)
        self.hBoxLayout.addSpacing(16)
        self.lineEdit.textChanged.connect(self.text_change)

        if custom:
            pass
        else:
            self.lineEdit.textChanged.connect(self.__ontextChanged)

    def __ontextChanged(self):
        text = self.lineEdit.text()
        data = Read_Config(
            (os.path.join(os.getcwd(), "config", "custom_config.json")))
        data[self.target] = text
        Save_Config(
            (os.path.join(os.getcwd(), "config", "custom_config.json")), data)
