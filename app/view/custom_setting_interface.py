# coding:utf-8
from qfluentwidgets import (SettingCardGroup, ScrollArea, ExpandLayout)
from qfluentwidgets import FluentIcon as FIF
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QLabel

from ..common.style_sheet import StyleSheet
from ..components.line_edit_card import LineEditCard
from ..components.custom_ComboBox_Setting_Card import CustomComboBoxSettingCard
from ..components.custom_Switch_Setting_Card import CustomSwitchSettingCard
from ..utils.tool import Read_Config, Save_Config
import os


class CustomSettingInterface(ScrollArea):
    """ Setting interface """

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)

        # setting label
        self.settingLabel = QLabel(self.tr("Custom Settings"), self)

        self.CustomSettingGroup = SettingCardGroup(
            self.tr("Setting"), self.scrollWidget)

        if os.path.exists(os.path.join(os.getcwd(), "config", "custom.json")):
            self.config_init()
            self.option_init()
        self.__initWidget()

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName('customsettingInterface')

        # initialize style sheet
        self.scrollWidget.setObjectName('scrollWidget')
        self.settingLabel.setObjectName('settingLabel')
        StyleSheet.SETTING_INTERFACE.apply(self)

        # initialize layout
        self.__initLayout()

    def __initLayout(self):
        self.settingLabel.move(36, 30)
        # add setting card group to layout
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.CustomSettingGroup)

    def CreateOption(self, dict: dict):
        if dict["optiontype"] == "combox":
            self.combox = CustomComboBoxSettingCard(
                icon=FIF.SETTING,
                title=dict["text"]["title"],
                content=dict["text"]["content"],
                texts=dict["optioncontent"],
                target=dict["optionname"],
                parent=self.CustomSettingGroup
            )
            self.CustomSettingGroup.addSettingCard(self.combox)

        elif dict["optiontype"] == "lineedit":
            try:
                text = Read_Config(
                    os.path.join(os.getcwd(), "config",
                                 "custom_config.json"))[dict["optionname"]]
            except FileNotFoundError:
                text = ""
            self.lineedit = LineEditCard(
                holderText=text,
                icon=FIF.COMMAND_PROMPT,
                title=dict["text"]["title"],
                content=dict["text"]["content"],
                parent=self.CustomSettingGroup,
                target=dict["optionname"],
                custom=False
            )
            self.CustomSettingGroup.addSettingCard(self.lineedit)

        elif dict["optiontype"] == "switch":
            self.Switch = CustomSwitchSettingCard(
                icon=FIF.COMMAND_PROMPT,
                title=dict["text"]["title"],
                content=dict["text"]["content"],
                target=dict["optionname"],
                parent=self.CustomSettingGroup
            )
            self.CustomSettingGroup.addSettingCard(self.Switch)

    def config_init(self):
        config_path = os.path.join(os.getcwd(), "config", "custom_config.json")
        custom_path = os.path.join(os.getcwd(), "config", "custom.json")
        if os.path.exists(config_path):
            pass
        else:
            dicts = {}
            config = Read_Config(custom_path)
            for i in config.items():
                try:
                    dicts[i[1]["optionname"]] = i[1]["optioncontent"]
                except FileNotFoundError:
                    dicts[i[1]["optionname"]] = ""
            Save_Config(config_path, dicts)

    def option_init(self):
        custom_path = os.path.join(os.getcwd(), "config", "custom.json")
        config = Read_Config(custom_path)
        for i in config.items():
            self.CreateOption(i[1])
