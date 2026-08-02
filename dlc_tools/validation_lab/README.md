# DSS Validation Lab

> **项目定位：DSS Validation Lab 首先作为 [DualSPHysics Studio](https://github.com/Zerliner/DualSPHysicsStudio) 的仿真验证 DLC 开发，同时也可作为独立 Windows 便携工具使用。**

DSS Validation Lab 用于把参考/实测 CSV 与 DualSPHysics 后处理结果 CSV
对齐，并输出可追溯的验证资料。

## 使用方式

- **作为 Studio DLC：**把解压后的完整
  `DSS Validation Lab-v1.0.0 (windows x64)` 目录放入 DualSPHysics Studio
  的 `DLC` 目录。Studio 会读取 `dss-extension.json` 自动识别并在“拓展功能”
  菜单显示该工具。
- **独立使用：**保持便携目录完整，直接运行 `DSSValidationLab.exe`；不要求
  安装 DualSPHysics Studio。

不要只复制 EXE；README、许可文件和 `dss-extension.json` 应与 EXE 保持在
同一便携目录中。

## 主要能力

- 分别选择两组 CSV 的时间列、数值列、倍率和偏移。
- 只在两组数据的重叠时段内，以参考数据时间点为网格，对仿真数据作线性插值。
- 计算 MAE、RMSE、Bias、最大绝对误差、相关系数、归一化 RMSE 和绝对峰值误差。
- 识别无效行、非严格递增时间以及不同时间倍率，并把警告写入报告。
- 每次生成新的任务目录，内含 `validation.json`、`comparison.csv` 和无需联网的
  `report.html`。
- JSON 记录源文件名、SHA-256、列映射和换算参数，但不保存源文件绝对路径。

CSV 支持 UTF-8、GB18030 和 CP1252，文件上限为 256 MiB、数据行上限为
1,000,000。时间列在倍率换算后必须唯一。归一化 RMSE 使用参考数据的
`最大值 - 最小值`；参考范围为零时会明确标记为不可用。

本工具提供数据对齐与统计证据，不代替实验不确定度分析、网格/粒径收敛研究或
行业规范要求的验证程序。

`LICENSE` 适用于本工具自有源码；
`THIRD_PARTY_LICENSES.txt` 保留便携运行时许可。

## 从源码构建

在 Windows PowerShell 中使用 Python 3.13 x64：

```powershell
.\dlc_tools\validation_lab\build_portable.ps1 -PythonExe C:\path\to\python.exe
```

构建结果写入 `outputs\DSS Validation Lab-v1.0.0 (windows x64)`。
