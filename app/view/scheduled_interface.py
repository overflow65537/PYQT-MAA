from PyQt6.QtWidgets import QWidget
from .UI_scheduled_interface import Ui_Scheduled_Interface


class ScheduledInterface(Ui_Scheduled_Interface, QWidget):

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setupUi(self)
