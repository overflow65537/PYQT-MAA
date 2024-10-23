<div align="center">

# PYQT-MAA
**[简体中文](./README.md) | [English](./README-en.md)**

 **[MAAFramework](https://github.com/MaaXYZ/MaaFramework)** General GUI Project Based on **[PyQT6](https://doc.qt.io/qtforpython-6)**
</div>

## Dev Environment
- Python 3.12

## Usage
- Put the MaaFramework resource folder `resource` and `interface.json` into the project root directory.
- `pip install -r requirements.txt`
- `python main.py`

## Featrues
### Custom Program Configuration
- Create `./config/custom.json`
- The file should contain the following content:
```
{
    "option1":{
        "optionname":"option1",
        "optiontype":"combox",
        "text":{
            "title":"Combox",
            "content":"This is a Combox."
        },
        "optioncontent":["content1","content2","content3"]

    },
    "option2":{
        "optionname":"option2",
        "optiontype":"switch",
        "text":{
            "title":"Switch",
            "content":"This is a Switch."
        }

    },
    "option3":{
        "optionname":"option3",
        "optiontype":"lineedit",
        "text":{
            "title":"Lineedit",
            "content":"This is a Lineedit"
        },
        "optioncontent":"content3"

    }
}
```
- The processed data will be saved to `./config/custom_config.json`

## License
**PyQt-MAA** is licensed under **[GPL-3.0 License](./LICENSE)**.
>[!WARNING]
It should be noted that some open source projects that **PyQt-MAA** relies on are open sourced under **dual licenses**.\
For personal non-commercial use, you need to comply with the **[GPL-3.0 License]((./LICENSE))**;for commercial use, you need to purchase a commercial license from the relevant developer.\
For details, see **[PyQt6 Commercial Use](https://www.qt.io/qt-licensing)** and **[PyQt-Fluent-Widgets Commercial License](https://github.com/zhiyiYo/PyQt-Fluent-Widgets/blob/master/docs/README_zh.md#%E8%AE%B8%E5%8F%AF%E8%AF%81)**.

## Acknowledgments
### Open Source Libraries
- **[PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)**\
    A fluent design widgets library based on C++ Qt/PyQt/PySide. Make Qt Great Again.
- **[MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework)**\
    基于图像识别的自动化黑盒测试框架。

### Developers
Thanks to the following developers for their contributions to PyQt-MAA.

<a href="https://github.com/overflow65537/PYQT-MAA/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=overflow65537/PYQT-MAA&max=1000" />
</a>
