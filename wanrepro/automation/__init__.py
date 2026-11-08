# -*- coding: utf-8 -*-
"""automation —— 我方 Agent 执行器（任务自动化 AT 系列）。

定位（docs/ROADMAP.md 功能节点·任务自动化）：
  Agent（Hermes）通过 bridge API 直驱拉片全链，人工只出现在两个停点
  （资产置换、付费审批）。本包是执行器，不进客户整合包
  （scripts/bundle.mjs 的 MANIFEST 白名单不含 automation/）。

节点：
  AT-1 ingest.py      任务摄取器（zip → 校验 → 桥任务 → 任务.json）
  AT-2 state.py       状态机引擎（8 状态单向迁移 + 重启恢复）
  AT-3 …              （后续节点按 ROADMAP 顺序落此包）

纪律：
  · 零第三方依赖（标准库 urllib/json/zipfile），与主链口径一致。
  · 失败即失败：exit code 非 0 + stderr 原文错误，禁静默降级。
  · 产品中立：本包不出现任何产品名/品牌。
"""
