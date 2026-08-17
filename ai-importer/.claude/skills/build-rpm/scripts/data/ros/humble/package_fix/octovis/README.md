# Fix Reason: Qt / QMake Environment Differences

**原因 / Reason:**
上游 CMakeLists 中直接调用了 `qmake`。而在 openEuler (或其他部分发行版) 中，Qt5 的 qmake 二进制文件被重命名为 `qmake-qt5`，以和 Qt4 区分，这导致上游构建脚本找不到命令。

**修改内容 / Modification:**
通过 Patch 将 `CMakeModules/FindQGLViewer.cmake` 中的 `COMMAND qmake` 修改为了 `COMMAND qmake-qt5`。
