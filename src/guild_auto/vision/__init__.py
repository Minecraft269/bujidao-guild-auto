# -*- coding: utf-8 -*-
"""
vision 层 —— UI 识别模式(阶段 6 / US-007)
===========================================
职责:不靠图标模板匹配,而靠 **界面标题 + tooltip 文字语义 + 格子坐标网格**
完成"定位 → 扫掠采集 → 结果验证"。

所有外部能力(截图 / OCR / 按键 / 悬停 / 日志读取)一律通过参数注入,
因此本层零真实环境依赖,可被单元测试完全驱动。

模块:
  bindings.py      options.txt → hotbar 实际按键绑定(无任何内置默认键值)
  models.py        UIElement / MemberData(缺失字段用 UNREADABLE 哨兵)
  tooltip.py       tooltip 纯解析 + 标题/元素语义分类(只按文字)
  sweep.py         格子网格扫掠 + 停留等待 + 完整性自检
  navigator.py     按键试探与界面导航(成功后缓存键,每次导航后重扫)
  verifier.py      升/降/踢 的结果验证(踢出必须交叉验证)
  runner.py        定位 → 采集 → 转 records
  capabilities.py  最小能力判定(阶段 7 扩展为完整能力矩阵)

依赖方向:ui → vision → infrastructure;core/domain 不得 import 本包。
"""
