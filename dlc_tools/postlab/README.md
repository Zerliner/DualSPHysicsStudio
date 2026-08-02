# DSS PostLab

> **项目定位：DSS PostLab 首先作为 [DualSPHysics Studio](https://github.com/Zerliner/DualSPHysicsStudio) 的后处理 DLC 开发，同时也可作为独立 Windows 便携工具使用。**

DSS PostLab 是后处理配方中心。它不包含 DualSPHysics 官方二进制，只发现并
直接调用用户已有的 v5.4 Windows 工具。

## 使用方式

- **作为 Studio DLC：**把解压后的完整 `DSS PostLab-v1.0.0 (windows x64)`
  目录放入 DualSPHysics Studio 的 `DLC` 目录。Studio 会读取
  `dss-extension.json` 自动识别并在“拓展功能”菜单显示该工具。
- **独立使用：**保持便携目录完整，直接运行 `DSSPostLab.exe`；不要求安装
  DualSPHysics Studio。

不要只复制 EXE；README、许可文件和 `dss-extension.json` 应与 EXE 保持在
同一便携目录中。

## 支持发现

- PartVTK、PartVTKOut、BoundaryVTK
- MeasureTool、ComputeForces、FloatingInfo
- IsoSurface、FlowTool、BIFileInfo

程序只检查所选根目录的 `bin\windows`，不从 PATH、案例目录或当前工作目录
猜测 EXE。启动时使用参数数组和 `shell=False`，不执行 BAT 或命令 Shell。

## 配方和输出

- 配方只保存工具类型、名称和参数数组，不保存本机路径。
- 每行是一个独立参数，只允许 `{input}` 和 `{output}` 占位符。
- 配方必须把至少一个输出参数指向 `{output}`。
- 绝对路径和 `..` 路径被拒绝，避免配方把产物指向旧任务。
- 每次执行都建立唯一的 `DSS-PostLab-*` 目录，绝不复用旧目录。
- 输出目录包含 UTF-8 日志和原子的 `postlab-run.json`。
- 任务记录保存配方、官方工具 SHA-256、命令、状态和产物清单。
- 日志最多保留 64 MiB，产物清单最多 100,000 个文件；达到上限会明确标记。

内置的 PartVTK 模板与 Studio 使用相同的全粒子 VTK 变量集。其他官方工具
参数与具体工程定义高度相关，PostLab 不猜测参数；用户应按相应 v5.4 工具
说明逐项填写并在确认窗口核对。

`LICENSE` 适用于 PostLab 自有源码；`THIRD_PARTY_LICENSES.txt` 保留便携
Python/Tcl/Tk 运行时许可。

## 从源码构建

在 Windows PowerShell 中使用 Python 3.13 x64：

```powershell
.\dlc_tools\postlab\build_portable.ps1 -PythonExe C:\path\to\python.exe
```

构建结果写入 `outputs\DSS PostLab-v1.0.0 (windows x64)`。
