# Fix Reason: Disable Tests / Demos

**原因 / Reason:**
上游源码的 `CMakeLists.txt` 中直接写死了 `add_subdirectory(test)`。
在 OS 打包阶段（如 OBS），运行这些单元测试或者编译庞大的 Demo 会消耗大量时间，或者因为缺少特定的显示/硬件环境而报错导致打包失败。

**修改内容 / Modification:**
通过 Patch 强行注释掉了包含 test 或 demo 目录的 CMake 指令。

**注意事项 / Notice:**
这种 Patch 比较脆弱。建议未来重构工具链时，通过注入 `-DBUILD_TESTING=OFF` 等标准 CMake 参数来替代这种暴力的源码修改。
