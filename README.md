# PYQT-MAA

## 开发环境
- Python 3.12

## 使用方法
- 将 MaaFramework 的资源文件夹 `resource` 和 `interface.json` 放入项目根目录
- `pip install -r requirements.txt`
- `python main.py`

## 其他功能
### Custom 程序配置
- 创建 `./config/custom.json`
- 内容为
```
{
    "option1":{
        "optionname":"option1",
        "text":{
            "title":"下拉框",
            "content":"这是一个下拉框"
        },
        "optiontype":"combox",
        "optioncontent":["content1","content2","content3"]

    },
    "option3":{
        "optionname":"option2",
        "text":{
            "title":"开关",
            "content":"这是一个开关"
        },
        "optiontype":"switch"

    },
    "option2":{
        "optionname":"option3",
        "text":{
            "title":"输入框",
            "content":"这是一个输入框"
        },
        "optiontype":"lineedit",
        "optioncontent":"content3"

    }
}
```
- 处理后的数据会保存至 `./config/custom_config.json`

## 致谢
### 开源项目
- **[PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)**
    A fluent design widgets library based on C++ Qt/PyQt/PySide. Make Qt Great Again.
- **[MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework)**
    基于图像识别的自动化黑盒测试框架。

### 开发者
感谢所有为 **PyQt-MAA** 做出贡献的开发者

<a href="https://github.com/overflow65537/PYQT-MAA/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=overflow65537/PYQT-MAA&max=1000" />
</a>
