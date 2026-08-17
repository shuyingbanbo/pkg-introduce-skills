# Fix Reason: Multiple Definition (ODR) Error Workaround

**原因 / Reason:**
编译期间出现了多重定义 (Multiple Definition / ODR) 的错误，这通常是因为上游 CMake 构建目标设置不当，或者在不同模块间重复链接了相同的目标对象。

**修改内容 / Modification:**
通过 Patch 强行从 `CMakeLists.txt` 的构建目标中移除了部分 `src/*_main.cpp` 源文件。

**注意事项 / Notice:**
这属于应对编译报错的“暴力阉割”手段。每次升级上游包时需验证上游是否重构了 CMake 修复了 ODR 错误，若修复应尽早移除该 Patch。
