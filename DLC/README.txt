DualSPHysics Studio 扩展工具目录
================================

“拓展功能”菜单会以极低开销扫描：

1. DLC 根目录中的 .exe；
2. DLC 各一级子目录根部的 .exe。

程序不会递归扫描更深目录，也不会自动运行任何工具。只有用户点击菜单项时，
Studio 才会直接启动对应 EXE；不会调用 BAT、命令行 Shell 或通过 PATH 搜索。

单文件工具可以直接放在 DLC 中。带 DLL、插件或许可文件的便携工具必须完整保留
原目录结构，并把整个文件夹放入 DLC。

Studio 本体不附带任何扩展程序。请从扩展程序各自的可信发布渠道单独获取，
并完整保留其说明、依赖和许可文件。Studio 不负责下载或更新扩展程序。

一级子目录可选放置 `dss-extension.json`。Studio 支持 schema_version 1，
并读取名称、版本、发布者、EXE 相对路径、许可文件、主页和支持的输入扩展名。
manifest 中的 EXE 和许可文件必须留在该子目录内；无效 manifest 会显示为
“不可用”，且不会静默退回到同目录的 legacy EXE。格式示例见
`dss-extension.example.json`。

请只运行来源可信的扩展程序。DualSPHysics Studio 不会自动读取扩展程序的输入、
输出或进度，也不会把当前 XML、STL、VTK 或其他文件传给扩展程序。每个扩展
程序在本次 Studio 会话中首次启动前都会显示来源与权限提醒。
