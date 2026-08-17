# Fix Reason: openEuler Headers Path / Upstream CMake Bug

**原因 / Reason:**
上游的 `usb-cam` 依赖 `ffmpeg` 库。上游 `CMakeLists.txt` 虽然正确使用了 `pkg_check_modules(avcodec REQUIRED libavcodec)`，但在 `target_include_directories` 中**漏写了**将查询到的 `${avcodec_INCLUDE_DIRS}` 包含进去。
在 Ubuntu 系统上，由于 ffmpeg 头文件碰巧在默认的 `/usr/include` 目录下，因此能侥幸编译通过。
但在 openEuler 系统上，ffmpeg 的头文件位于 `/usr/include/ffmpeg`，导致由于上游的疏忽而找不到头文件。

**修改内容 / Modification:**
通过 Patch 显式地在 CMake 中添加了 `include_directories(/usr/include/ffmpeg)`。

**注意事项 / Notice:**
这属于针对 openEuler 路径差异的强行适配。更好的做法应该是修改上游 CMake 添加 `${avcodec_INCLUDE_DIRS}`，并在未来向官方提交 PR 修复。
