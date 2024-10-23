import os

from qfluentwidgets import (
    SettingCardGroup,
    SwitchSettingCard,
    OptionsSettingCard,
    PushSettingCard,
    HyperlinkCard,
    PrimaryPushSettingCard,
    ScrollArea,
    ComboBoxSettingCard,
    ExpandLayout,
    CustomColorSettingCard,
    setTheme,
    setThemeColor,
    ConfigItem,
)
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


class SettingInterface(ScrollArea):
    """Setting interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)

        global maa_pi_config_Path
        maa_pi_config_Path = os.path.join(os.getcwd(), "config", "maa_pi_config.json")
        # setting label
        self.settingLabel = QLabel(self.tr("Settings"), self)

        # ADB Group

        if os.path.exists(maa_pi_config_Path):
            pi_config = Read_Config(maa_pi_config_Path)
            Port_data = pi_config["adb"]["address"].split(":")[1]
            path_data = pi_config["adb"]["adb_path"]
        else:
            Port_data = "0"
            path_data = "./"

        self.ADB_Path_Port_Adjuster = SettingCardGroup(
            self.tr("ADB"), self.scrollWidget
        )
        self.ADBPort = LineEditCard(
            FIF.COMMAND_PROMPT,
            Port_data,
            title=self.tr("ADB Port"),
            parent=self.ADB_Path_Port_Adjuster,
        )
        self.ADBPath = PushSettingCard(
            self.tr("Choose ADB"),
            FIF.COMMAND_PROMPT,
            self.tr("ADB Path"),
            path_data,
            self.ADB_Path_Port_Adjuster,
        )

        # personalization
        self.personalGroup = SettingCardGroup(
            self.tr("Personalization"), self.scrollWidget
        )
        self.micaCard = SwitchSettingCard(
            FIF.TRANSPARENT,
            self.tr("Mica effect"),
            self.tr("Apply semi transparent to windows and surfaces"),
            cfg.micaEnabled,
            self.personalGroup,
        )
        self.themeCard = OptionsSettingCard(
            cfg.themeMode,
            FIF.BRUSH,
            self.tr("Application theme"),
            self.tr("Change the appearance of your application"),
            texts=[self.tr("Light"), self.tr("Dark"), self.tr("Use system setting")],
            parent=self.personalGroup,
        )
        self.themeColorCard = CustomColorSettingCard(
            cfg.themeColor,
            FIF.PALETTE,
            self.tr("Theme color"),
            self.tr("Change the theme color of you application"),
            self.personalGroup,
        )
        self.zoomCard = OptionsSettingCard(
            cfg.dpiScale,
            FIF.ZOOM,
            self.tr("Interface zoom"),
            self.tr("Change the size of widgets and fonts"),
            texts=[
                "100%",
                "125%",
                "150%",
                "175%",
                "200%",
                self.tr("Use system setting"),
            ],
            parent=self.personalGroup,
        )
        self.languageCard = ComboBoxSettingCard(
            cfg.language,
            FIF.LANGUAGE,
            self.tr("Language"),
            self.tr("Set your preferred language for UI"),
            texts=["简体中文", "繁體中文", "English", self.tr("Use system setting")],
            parent=self.personalGroup,
        )

        # 调试模式
        if os.path.exists(os.path.join(os.getcwd(), "config", "maa_option.json")):
            DEV_Config = Read_Config(
                os.path.join(os.getcwd(), "config", "maa_option.json")
            )["save_draw"]
        else:
            DEV_Config = False
        self.DEVGroup = SettingCardGroup(self.tr("DEV Mode"), self.scrollWidget)
        self.DEVmodeCard = SwitchSettingCard(
            FIF.ALBUM,
            self.tr("DEV mode switch"),
            self.tr(
                "When the debug mode is enabled, \
                    screenshots will be saved in ./debug/vision"
            ),
            configItem=ConfigItem(group="DEV", name="DEV", default=DEV_Config),
            parent=self.DEVGroup,
        )

        # update software
        self.updateSoftwareGroup = SettingCardGroup(
            self.tr("Software update"), self.scrollWidget
        )
        self.updateOnStartUpCard = SwitchSettingCard(
            FIF.UPDATE,
            self.tr("Check for updates when the application starts"),
            self.tr(
                "The new version will be more \
                    stable and have more features"
            ),
            configItem=cfg.checkUpdateAtStartUp,
            parent=self.updateSoftwareGroup,
        )

        # application
        self.aboutGroup = SettingCardGroup(self.tr("About"), self.scrollWidget)
        self.helpCard = HyperlinkCard(
            HELP_URL,
            self.tr("Open help page"),
            FIF.HELP,
            self.tr("Help"),
            self.tr("Discover new features and learn useful tips about PyQt-MAA"),
            self.aboutGroup,
        )
        self.feedbackCard = PrimaryPushSettingCard(
            self.tr("Provide feedback"),
            FIF.FEEDBACK,
            self.tr("Provide feedback"),
            self.tr("Help us improve PyQt-MAA by providing feedback"),
            self.aboutGroup,
        )
        self.aboutCard = PrimaryPushSettingCard(
            self.tr("Check update"),
            FIF.INFO,
            self.tr("About"),
            "© "
            + self.tr("Copyright")
            + f" {YEAR}, {AUTHOR}. "
            + self.tr("Version")
            + " "
            + VERSION,
            self.aboutGroup,
        )

        self.__initWidget()

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("settingInterface")

        # initialize style sheet
        self.scrollWidget.setObjectName("scrollWidget")
        self.settingLabel.setObjectName("settingLabel")
        StyleSheet.SETTING_INTERFACE.apply(self)

        self.micaCard.setEnabled(isWin11())

        # initialize layout
        self.__initLayout()
        self.__connectSignalToSlot()

    def __initLayout(self):
        self.settingLabel.move(36, 30)

        # add cards to group
        self.ADB_Path_Port_Adjuster.addSettingCard(self.ADBPort)
        self.ADB_Path_Port_Adjuster.addSettingCard(self.ADBPath)

        self.personalGroup.addSettingCard(self.micaCard)
        self.personalGroup.addSettingCard(self.themeCard)
        self.personalGroup.addSettingCard(self.themeColorCard)
        self.personalGroup.addSettingCard(self.zoomCard)
        self.personalGroup.addSettingCard(self.languageCard)

        self.DEVGroup.addSettingCard(self.DEVmodeCard)

        self.updateSoftwareGroup.addSettingCard(self.updateOnStartUpCard)

        self.aboutGroup.addSettingCard(self.helpCard)
        self.aboutGroup.addSettingCard(self.feedbackCard)
        self.aboutGroup.addSettingCard(self.aboutCard)

        # add setting card group to layout
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.ADB_Path_Port_Adjuster)
        self.expandLayout.addWidget(self.personalGroup)
        self.expandLayout.addWidget(self.DEVGroup)
        self.expandLayout.addWidget(self.updateSoftwareGroup)
        self.expandLayout.addWidget(self.aboutGroup)

    def __showRestartTooltip(self):
        """show restart tooltip"""
        InfoBar.success(
            self.tr("Updated successfully"),
            self.tr("Configuration takes effect after restart"),
            duration=1500,
            parent=self,
        )

    def __onADBPathCardClicked(self):
        """手动选择ADB.exe位置"""
        file_name, _ = QFileDialog.getOpenFileName(
            self, self.tr("Choose file"), "./", self.tr("All Files (*);;All Files (*)")
        )
        if not file_name:
            return

        data = Read_Config(maa_pi_config_Path)
        data["adb"]["adb_path"] = file_name
        Save_Config(maa_pi_config_Path, data)
        self.ADBPath.setContent(file_name)

    def __connectSignalToSlot(self):
        """connect signal to slot"""
        cfg.appRestartSig.connect(self.__showRestartTooltip)

        # ADB信号
        self.ADBPort.text_change.connect(self._onADBPortCardChange)
        self.ADBPath.clicked.connect(self.__onADBPathCardClicked)

        # 调试信号

        self.DEVmodeCard.checkedChanged.connect(self._onDEVmodeCardChange)
        # personalization
        cfg.themeChanged.connect(setTheme)
        self.themeColorCard.colorChanged.connect(lambda c: setThemeColor(c))
        self.micaCard.checkedChanged.connect(signalBus.micaEnableChanged)

        # about
        self.feedbackCard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(FEEDBACK_URL))
        )

    def _onADBPortCardChange(self):
        port = self.ADBPort.lineEdit.text()
        full_ADB_address = f"127.0.0.1:{port}"
        data = Read_Config(maa_pi_config_Path)
        data["adb"]["address"] = full_ADB_address
        Save_Config(maa_pi_config_Path, data)

    def _onDEVmodeCardChange(self):
        state = self.DEVmodeCard.isChecked()
        data = Read_Config(maa_pi_config_Path)
        data["save_draw"] = state
        Save_Config(maa_pi_config_Path, data)
