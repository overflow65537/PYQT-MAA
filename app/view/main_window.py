import os

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from qfluentwidgets import (
    NavigationItemPosition,
    FluentWindow,
    SplashScreen,
    SystemThemeListener,
    isDarkTheme,
)
from qfluentwidgets import FluentIcon as FIF

from .setting_interface import SettingInterface
from .task_interface import TaskInterface
from .custom_setting_interface import CustomSettingInterface
from ..common.config import cfg
from ..common.signal_bus import signalBus
from ..common import resource


class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        self.initWindow()

        # create system theme listener
        self.themeListener = SystemThemeListener(self)

        # create sub interface
        self.taskInterface = TaskInterface(self)
        self.settingInterface = SettingInterface(self)
        self.customsettingInterface = CustomSettingInterface(self)

        # enable acrylic effect
        self.navigationInterface.setAcrylicEnabled(True)

        self.connectSignalToSlot()

        # add items to navigation interface
        self.initNavigation()
        self.splashScreen.finish()

        # start theme listener
        self.themeListener.start()

    def connectSignalToSlot(self):
        signalBus.micaEnableChanged.connect(self.setMicaEffectEnabled)

    def initNavigation(self):
        # add navigation items

        self.navigationInterface.addSeparator()

        # add custom widget to bottom

        if os.path.exists(os.path.join(os.getcwd(), "config", "custom.json")):
            self.addSubInterface(self.taskInterface, FIF.CHECKBOX, self.tr("Task"))
            self.addSubInterface(
                self.customsettingInterface, FIF.IOT, self.tr("Custom Setting")
            )
            self.addSubInterface(
                self.settingInterface,
                FIF.SETTING,
                self.tr("Settings"),
                NavigationItemPosition.BOTTOM,
            )
        else:
            self.addSubInterface(self.taskInterface, FIF.CHECKBOX, self.tr("Task"))
            self.addSubInterface(
                self.settingInterface,
                FIF.SETTING,
                self.tr("Settings"),
                NavigationItemPosition.BOTTOM,
            )

    def initWindow(self):
        self.resize(960, 780)
        self.setMinimumWidth(760)
        self.setWindowIcon(QIcon(":/gallery/images/logo.png"))
        self.setWindowTitle("PyQt-MAA")

        self.setMicaEffectEnabled(cfg.get(cfg.micaEnabled))

        # create splash screen
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        desktop = QApplication.screens()[0].availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)
        self.show()
        QApplication.processEvents()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def closeEvent(self, e):
        self.themeListener.terminate()
        self.themeListener.deleteLater()
        super().closeEvent(e)

    def _onThemeChangedFinished(self):
        super()._onThemeChangedFinished()

        # retry
        if self.isMicaEffectEnabled():
            QTimer.singleShot(
                100,
                lambda: self.windowEffect.setMicaEffect(self.winId(), isDarkTheme()),
            )
