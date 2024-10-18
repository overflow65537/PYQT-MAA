# coding:utf-8
from qfluentwidgets import (SettingCardGroup, SwitchSettingCard,
                            OptionsSettingCard, PushSettingCard,
                            HyperlinkCard, PrimaryPushSettingCard, ScrollArea,
                            ComboBoxSettingCard, ExpandLayout, CustomColorSettingCard,
                            setTheme, setThemeColor, ConfigItem)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import InfoBar
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QWidget, QLabel, QFileDialog

from ..common.config import cfg, HELP_URL, FEEDBACK_URL, AUTHOR, VERSION, YEAR, isWin11
from ..common.signal_bus import signalBus
from ..common.style_sheet import StyleSheet
from ..components.line_edit_card import LineEditCard
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



        self.config_init()
        self.__initWidget()
        self.option_init()

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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


    def CreateOption(self,dict:dict):
        if dict["optiontype"] == "combox":
            
            self.combox= ComboBoxSettingCard(
                configItem = cfg.language,
                icon = FIF.SETTING,
                title = dict["text"]["title"],
                content = dict["text"]["content"],
                texts=dict["optioncontent"],
                parent=self.CustomSettingGroup
            )
            self.CustomSettingGroup.addSettingCard(self.combox)

        elif dict["optiontype"] == "lineedit":
            try:
                text = Read_Config(os.path.join(os.getcwd(),"config","custom_config.json"))[dict["optionname"]]
            except:
                text = ""
            self.lineedit = LineEditCard(
                holderText = text,
                icon = FIF.COMMAND_PROMPT,
                title = dict["text"]["title"],
                content = dict["text"]["content"],
                parent=self.CustomSettingGroup,
                target= dict["optionname"],
                custom=False
            )
            self.CustomSettingGroup.addSettingCard(self.lineedit)
    def config_init(self):
        config_path = os.path.join(os.getcwd(),"config","custom_config.json")
        custom_path = os.path.join(os.getcwd(),"config","custom.json")
        if os.path.exists(config_path):
            pass
        else:
            dicts = {}
            config = Read_Config(custom_path)
            for i in config.items():
                print(i[1])
                try:
                    dicts[i[1]["optionname"]] = i[1]["optioncontent"]
                except:
                    dicts[i[1]["optionname"]] = ""
            Save_Config(config_path,dicts)

    def option_init(self):
        custom_path = os.path.join(os.getcwd(),"config","custom.json")
        config = Read_Config(custom_path)
        for i in config.items():
            self.CreateOption(i[1])
            

