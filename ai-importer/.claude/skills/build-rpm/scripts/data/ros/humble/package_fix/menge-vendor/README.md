# Fix Reason: openEuler System Library (pkg-config vs find_library)

**原因 / Reason:**
上游使用 `pkg_check_modules(tinyxml REQUIRED tinyxml)` 来寻找 `tinyxml` 库，这依赖于系统提供 `.pc` (pkg-config) 文件。
但在 openEuler 系统中，`tinyxml` 库可能没有打包 `.pc` 文件，或者命名有差异，导致上游的 CMake 脚本找不到该库而编译失败。

**修改内容 / Modification:**
通过 Patch 将 `pkg_check_modules` 改为了更底层的 `find_library(tinyxml_LIBRARY NAMES tinyxml)`，直接在系统中查找动态链接库文件。

**注意事项 / Notice:**
这种由于各发行版打包规范（是否提供 pkg-config）引起的差异比较常见。未来若 openEuler 的 `tinyxml` 补充了 `.pc` 文件，此 Patch 可被废弃。
