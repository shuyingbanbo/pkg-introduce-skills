# Fix Reason: Offline Build Support (OBS Network Isolation)

**原因 / Reason:**
上游的 ROS 2 Vendor 包在 `CMakeLists.txt` 中使用了 `ExternalProject_Add` 或 `FetchContent`，试图在编译期 (`%build` 阶段) 动态连接 GitHub 下载源码。
然而，openEuler 的 OBS (Open Build Service) 编译机环境是**完全断网**的，这会导致编译直接失败。

**修改内容 / Modification:**
1. 通过 Patch 修改 `CMakeLists.txt`，将 `GIT_REPOSITORY` 或联网下载逻辑修改为读取本地路径的 `.tar.gz`。
2. 在 `source.fix` 和 `prep.fix` 中将离线源码包作为 RPM 的 `SourceX` 引入。

**注意事项 / Notice:**
未来升级此包时，必须同步下载上游对应版本的新源码 tarball，并更新 Patch 和 `.fix` 文件中的版本号，不可盲目复用旧版 tarball。
