"""Conservative Chinese offline explanations for DualSPHysics v5.4 nodes."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .models import NodeExplanation
from .semantics import parse_integer
from .source import iter_elements, local_name


_ELEMENT_EXPLANATIONS: dict[str, tuple[str, str]] = {
    "case": ("案例", "DualSPHysics 案例定义的根节点。"),
    "constantsdef": ("物理常量", "定义重力、参考密度、声速和时间步相关常量。"),
    "gravity": ("重力加速度", "x、y、z 分量共同定义重力方向和大小。"),
    "rhop0": ("参考密度", "流体状态方程使用的参考密度。"),
    "definition": ("几何定义域", "dp 是初始粒子间距，pointmin/pointmax 是几何生成范围。"),
    "pointmin": ("最小点", "定义三维范围的最小 x、y、z 坐标。"),
    "pointmax": ("最大点", "定义三维范围的最大 x、y、z 坐标。"),
    "setmkfluid": ("选择流体 marker", "后续绘制命令使用该本地流体 marker。"),
    "setmkbound": ("选择边界 marker", "后续绘制命令使用该本地边界 marker。"),
    "drawbox": ("长方体", "使用 point 与 size 生成轴对齐长方体。"),
    "drawprism": ("棱柱", "前半和后半顶点分别描述两个对应截面。"),
    "shapeout": ("几何输出", "让 GenCase 将此前绘制的形状输出为独立 VTK。"),
    "floating": ("浮动刚体", "把 mkbound 对应的边界粒子声明为可运动刚体。"),
    "center": ("浮体中心", "浮体初始质心；整体图形变换时应同步更新。"),
    "parameter": ("执行参数", "通过 key/value 配置 DualSPHysics 求解阶段。"),
}

_PARAMETERS: dict[str, tuple[str, str, str | None]] = {
    "saveposdouble": ("位置精度", "控制粒子位置是否以双精度保存。", None),
    "stepalgorithm": ("时间积分算法", "1 表示 Verlet，2 表示 Symplectic。", None),
    "verletsteps": ("Verlet Euler 步数", "只在 StepAlgorithm=1 时生效。", "steps"),
    "kernel": ("核函数", "1 表示 Cubic Spline，2 表示 Wendland。", None),
    "visco": ("人工黏性", "ViscoTreatment=1 时使用的人工黏性系数。", None),
    "densitydt": ("密度扩散", "0 关闭，1 为 Molteni，2 为 Fourtakas。", None),
    "shifting": ("粒子 Shifting", "0 关闭，其余值选择不同边界处理方式。", None),
    "shiftcoef": ("Shifting 系数", "只在 Shifting 非 0 时参与计算。", None),
    "shifttfs": ("自由面阈值", "只在 Shifting 非 0 时参与自由面判定。", None),
    "timemax": ("模拟时长", "求解结束的物理时间。", "s"),
    "timeout": ("输出间隔", "相邻 Part 输出之间的物理时间。", "s"),
    "partsoutmax": ("越界粒子比例（弃用）", "v5.4 中已弃用，建议移除。", None),
    "rhopoutmin": ("最低有效密度", "低于该值的流体粒子被判定为异常。", "kg/m³"),
    "rhopoutmax": ("最高有效密度", "高于该值的流体粒子被判定为异常。", "kg/m³"),
}


def explain(element: ET.Element, root: ET.Element) -> NodeExplanation:
    name = local_name(element.tag)
    if name == "parameter":
        key = (element.get("key") or "").casefold()
        known = _PARAMETERS.get(key)
        if known:
            title, summary, units = known
            effective: bool | None = True
            notes: list[str] = []
            parameters = {
                (node.get("key") or "").casefold(): node
                for node in iter_elements(root, "parameter")
            }
            if key == "verletsteps":
                step = parameters.get("stepalgorithm")
                if step is not None and parse_integer(step.get("value")) != 1:
                    effective = False
                    notes.append("当前 StepAlgorithm 不是 1，因此该值不生效。")
            if key in ("shiftcoef", "shifttfs"):
                shifting = parameters.get("shifting")
                if shifting is not None and parse_integer(shifting.get("value")) == 0:
                    effective = False
                    notes.append("当前 Shifting=0，因此该值不生效。")
            if key == "partsoutmax":
                notes.append("DualSPHysics v5.4 已弃用此参数。")
            return NodeExplanation(title, summary, units, effective, notes=tuple(notes))

    known_element = _ELEMENT_EXPLANATIONS.get(name)
    if known_element:
        title, summary = known_element
        units = element.get("units_comment")
        notes: list[str] = []
        if element.get("auto", "false").strip().lower() == "true":
            notes.append("auto=true：数值由 GenCase 自动确定，0 通常只是占位值。")
        return NodeExplanation(title, summary, units, notes=tuple(notes))

    return NodeExplanation(
        name,
        "暂无可靠的 DualSPHysics v5.4 离线解释；已保留原始节点，请在源码中核对。",
        element.get("units_comment"),
        reliability="unknown",
    )

