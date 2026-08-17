# Fix Reason: Python Version Hardcoding

**原因 / Reason:**
上游代码中针对特定 ROS 版本（如 Humble）硬编码了寻找特定版本的 Python（例如 `find_package(PythonLibs 3.10 EXACT REQUIRED)`）。
而不同的 openEuler 发行版自带的 Python 版本不同（例如 22.03 可能是 3.9，24.03 可能是 3.11），导致直接编译失败。

**修改内容 / Modification:**
通过 Patch 将硬编码的 Python 版本号改为了 openEuler 当前环境对应的版本（如 3.9）。

**注意事项 / Notice:**
这是一个**定时炸弹**。当 openEuler 升级基础 OS 版本（Python 大版本更新）或 ROS 升级时，此 Patch 极大概率会失效或导致运行时崩溃。未来应改为动态获取系统 Python 版本的写法。
