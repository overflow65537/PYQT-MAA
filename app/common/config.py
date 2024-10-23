import sys
from enum import Enum

from PyQt6.QtCore import QLocale
from qfluentwidgets import (
    qconfig,
    QConfig,
    ConfigItem,
    OptionsConfigItem,
    BoolValidator,
    OptionsValidator,
    RangeConfigItem,
    RangeValidator,
    Theme,
    FolderValidator,
    ConfigSerializer,
    __version__,
)
import os


class Language(Enum):
    """Language enumeration"""

    CHINESE_SIMPLIFIED = QLocale(QLocale.Language.Chinese, QLocale.Country.China)
    CHINESE_TRADITIONAL = QLocale(QLocale.Language.Chinese, QLocale.Country.HongKong)
    ENGLISH = QLocale(QLocale.Language.English)
    AUTO = QLocale()


class LanguageSerializer(ConfigSerializer):
    """Language serializer"""

    def serialize(self, language):
        return language.value.name() if language != Language.AUTO else "Auto"

    def deserialize(self, value: str):
        return Language(QLocale(value)) if value != "Auto" else Language.AUTO


def isWin11():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 22000


class Config(QConfig):
    """Config of application"""

    # MAA路径
    Maa_config = ConfigItem(
        "Maa", "Maa_config", os.path.join(os.getcwd(), "config", "maa_pi_config.json")
    )
    Maa_interface = ConfigItem(
        "Maa",
        "Maa_interface",
        os.path.join(os.getcwd(), "interface.json"),
    )
    Maa_resource = ConfigItem(
        "Maa", "Maa_resource", os.path.join(os.getcwd(), "resource"), FolderValidator()
    )
    maa_dev = ConfigItem(
        "Maa", "maa_dev", os.path.join(os.getcwd(), "config", "maa_option.json")
    )

    # main window
    micaEnabled = ConfigItem("MainWindow", "MicaEnabled", isWin11(), BoolValidator())

    dpiScale = OptionsConfigItem(
        "MainWindow",
        "DpiScale",
        "Auto",
        OptionsValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]),
        restart=True,
    )

    language = OptionsConfigItem(
        "MainWindow",
        "Language",
        Language.AUTO,
        OptionsValidator(Language),
        LanguageSerializer(),
        restart=True,
    )

    # Material
    blurRadius = RangeConfigItem(
        "Material", "AcrylicBlurRadius", 15, RangeValidator(0, 40)
    )

    # software update
    checkUpdateAtStartUp = ConfigItem(
        "Update", "CheckUpdateAtStartUp", True, BoolValidator()
    )


VERSION = __version__
REPO_URL = "https://github.com/overflow65537/PYQT-MAA/"
UPDATE_URL = "https://github.com/overflow65537/PYQT-MAA/releases/latest/"
FEEDBACK_URL = "https://github.com/overflow65537/PYQT-MAA/issues/"


cfg = Config()
cfg.themeMode.value = Theme.AUTO
qconfig.load("config/config.json", cfg)
