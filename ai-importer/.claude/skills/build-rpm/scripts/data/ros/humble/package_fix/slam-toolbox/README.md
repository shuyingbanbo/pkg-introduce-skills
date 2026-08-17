# Fix Reason: TBB Library Linking Issue

**原因 / Reason:**
openEuler 环境下，TBB 库的 CMake package 配置或寻找方式与 Ubuntu 有差异，导致通过上游默认的 `TBB::tbb` 形式找不到并链接失败。

**修改内容 / Modification:**
修改 CMakeLists，将对 TBB 的查找和链接方式（从 `TBB::tbb` 强行改为 `tbb`）进行了 openEuler 适配。
