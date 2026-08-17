# Fix Reason: Catch2 Testing Framework Integration

**原因 / Reason:**
CMake 在寻找 `Catch2` 测试框架时发生了失败或包含路径不正确，导致编译测试用例时出错。

**修改内容 / Modification:**
通过 Patch 注入了 `include(Catch)` 等指令，确保 Catch2 能够被正确发现和链接。
