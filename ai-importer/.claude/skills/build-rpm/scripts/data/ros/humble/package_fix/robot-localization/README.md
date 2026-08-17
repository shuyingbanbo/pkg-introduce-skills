# robot-localization package fix

该目录用于修正 `robot_localization` 在 openEuler Humble 打包时与 `GeographicLib` 相关的两个问题：

1. **依赖语义拆分**
   - 上游 `package.xml` 中声明的是 `<depend>geographiclib</depend>`。
   - 在 openEuler RPM 体系中，这个依赖需要拆分为：
     - `BuildRequires: GeographicLib-devel`
     - `Requires: GeographicLib`
   - 因此使用本目录下的 `BuildRequires` 和 `Requires` 文件，对自动生成的依赖进行包级修正，而不是做全局粗暴替换。

2. **GeographicLib 的 CMake 查找方式兼容**
   - 上游 `CMakeLists.txt` 默认假设系统提供 `FindGeographicLib.cmake`，并尝试从 `/usr/share/cmake/geographiclib/` 查找。
   - openEuler 的 `GeographicLib-devel` 实际提供的是 config-mode 文件：
     - `/usr/lib64/cmake/GeographicLib/geographiclib-config.cmake`
   - 因此需要补丁 `0001-adapt-GeographicLib-discovery-for-openEuler.patch`，使其优先使用 `find_package(GeographicLib CONFIG QUIET)`，失败时再回退到旧的 module 路径逻辑。

该修复方案已在 EUR 项目 `wanminghu/openeuler-embedded-ib-robot` 中通过实际构建验证。
