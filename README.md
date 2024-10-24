<div align="center">

# PYQT-MAA
**[简体中文](./README.md) | [English](./README-en.md)**

基于 **[PyQT6](https://doc.qt.io/qtforpython-6)** 的 **[MAAFramework](https://github.com/MaaXYZ/MaaFramework)** 通用 GUI 项目
</div>

## 开发环境
- Python 3.12

## 使用方法
- 将 MaaFramework 的资源文件夹 `resource` 和 `interface.json` 放入项目根目录
- `pip install -r requirements.txt`
- `python main.py`

## 特色功能
### Custom 程序配置
- 创建 `./config/custom.json`
- 内容为
```
{
    "option1":{
        "optionname":"option1",
        "optiontype":"combox",
        "text":{
            "title":"下拉框",
            "content":"这是一个下拉框"
        },
        "optioncontent":["content1","content2","content3"]

    },
    "option2":{
        "optionname":"option2",
        "optiontype":"switch",
        "text":{
            "title":"开关",
            "content":"这是一个开关"
        }

    },
    "option3":{
        "optionname":"option3",
        "optiontype":"lineedit",
        "text":{
            "title":"输入框",
            "content":"这是一个输入框"
        },
        "optioncontent":"content3"

    }
}
```
- 处理后的数据会保存至 `./config/custom_config.json`

## 许可证
**PyQt-MAA** 使用 **[GPL-3.0 许可证](./LICENSE)** 开源。

## 致谢
### 开源项目
- **[PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)**\
    A fluent design widgets library based on C++ Qt/PyQt/PySide. Make Qt Great Again.
- **[MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework)**\
    基于图像识别的自动化黑盒测试框架。

### 开发者
感谢所有为 **PyQt-MAA** 做出贡献的开发者。

<a href="https://github.com/overflow65537/PYQT-MAA/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=overflow65537/PYQT-MAA&max=1000" />
</a>
