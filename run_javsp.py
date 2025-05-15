#!/usr/bin/env python
import sys
import os

# 添加当前目录到 Python 路径
sys.path.insert(0, os.path.abspath('.'))

# 替换元数据获取函数以避免版本检查错误
import importlib.metadata
original_version = importlib.metadata.version

def mock_version(package_name):
    if package_name == 'javsp':
        return '999.999.999'  # 返回一个假版本号
    return original_version(package_name)

importlib.metadata.version = mock_version

# 导入并运行主程序
from javsp.__main__ import entry
entry()
