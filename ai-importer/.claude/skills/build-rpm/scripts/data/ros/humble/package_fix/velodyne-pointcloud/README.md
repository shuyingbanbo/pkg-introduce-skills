# Fix Reason: openEuler System Library (pkg-config vs find_package)

**原因 / Reason:**
上游为了寻找 `yaml-cpp` 库，使用了 `pkg_check_modules(YAML_CPP REQUIRED yaml-cpp)`。但在 openEuler (以及许多其他 Linux 发行版) 中，`yaml-cpp` 原生提供了 CMake 的 config 查找机制，但可能缺少 pkg-config 支持，导致编译失败。

**修改内容 / Modification:**
通过 Patch 将寻找方式改回了标准的 `find_package(yaml-cpp REQUIRED)`。

**注意事项 / Notice:**
这是因为上游对 `yaml-cpp` 的寻找方式兼容性不够导致的。未来如果上游合并了相关的修复，则此 Patch 可以移除。
