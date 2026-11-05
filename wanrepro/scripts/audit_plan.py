#!/usr/bin/env python3
"""audit_plan.py — 第二路独立审计（技术方案对抗审查）

定位：标准工作流程 v3.2 §2「双路独立审计」的第二路。
第一路 = delegation 子代理（gpt-6-sol，带终端可核实事实）。
第二路 = 本脚本（gemini-3.8-flash，零工具纯文本对抗，不看第一路结论）。
两者 provider 均为 jxin-overseas，但模型族不同，且都 ≠ 主模型（deepseek-v4.1-flash）。

用法：
    py -3 scripts/audit_plan.py --file docs/架构与数据契约.md
    py -3 scripts/audit_plan.py --file plan.md --context docs/项目适配包.md
    py -3 scripts/audit_plan.py --file plan.md --focus "重点攻击 MVP 是否假闭环"
退出码：0 成功 / 1 参数或 key 缺失 / 2 API 调用失败
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

API_BASE = "https://api.jxinapi.com/v1"
MODEL = "gemini-3.8-flash"
KEY_ENV = "JXIN_API_KEY"
PROXY = ""
TIMEOUT = 300

SYSTEM_PROMPT = """你是独立方案审计员。你审的是一份要拿去执行的**技术方案/任务清单**，不是散文。
你不改写方案，只出判定。你看不到代码仓库，所以对「声称已存在/已验证」的条目，只能标记为「需核实」，不要当作事实。

必须逐条回答的六个维度：
1. 事实声称是否可核实：方案里所有「已实测/已验证/已存在/版本是X」的断言，逐条标【需核实】并写出核实方法（命令或文件）。
2. 假闭环：里程碑声称交付的能力，其前置代码/数据/凭证是否在本里程碑内真实存在。指出任何「名字叫能跑，内容却依赖尚不存在的模块」。
3. 判据可判定性：每条验收标准能否被机器或人工明确判 PASS/FAIL；模糊措辞（「基本一致」「明显改善」）逐条点名。
4. 原子性：任务能否一次提交完成；是否有任务混合了多个可独立验收的改动。
5. 内部矛盾：同一实体（文件/字段/版本号/路径）在两处描述不一致。
6. 遗漏：该阶段必需但方案里没有的任务（回滚、错误处理、回归样本、失败路径）。

输出格式（严格）：
## 结论
- 总评：BLOCKING / 可开工 / 可开工但需先补
- 现在能否开工：YES / NO（布尔，给一句理由）
- 是否已边际收益递减：YES / NO

## 逐条发现
| # | 维度 | 分级 | 指控（具体到段落/字段） | 证据或核实方法 | 建议方向 |
|---|---|---|---|---|---|
分级只能用：BLOCKING（不修不能开工）/ IMPL-FIXABLE（实施时顺手可修）/ POLISH（措辞与体例）

## 无法判断的部分
[列出你缺的信息，不要猜]

规则：不确定就写不确定；禁止编造方案里没有的句子当证据。"""


def load_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    env_path = Path(os.environ.get("HERMES_HOME", r"C:\Users\leon3\AppData\Local\hermes")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{KEY_ENV}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def call(api_key: str, user_message: str) -> dict:
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
        "max_tokens": 6000,
    }, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with opener.open(req, timeout=TIMEOUT) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return {"content": result["choices"][0]["message"]["content"], "usage": result.get("usage", {})}


def main():
    ap = argparse.ArgumentParser(description=f"第二路方案审计（{MODEL}）")
    ap.add_argument("--file", required=True, help="待审方案文件")
    ap.add_argument("--context", help="补充材料文件（可选，如项目适配包）")
    ap.add_argument("--focus", help="本次审计重点（可选）")
    ap.add_argument("--log-file", help="结论落盘 JSON（可选）")
    args = ap.parse_args()

    p = Path(args.file)
    if not p.exists():
        print(f"ERROR: 文件不存在: {p}", file=sys.stderr)
        sys.exit(1)
    key = load_key()
    if not key:
        print(f"ERROR: 缺 {KEY_ENV}", file=sys.stderr)
        sys.exit(1)

    msg = f"## 待审方案（来源：{p.name}）\n\n" + p.read_text(encoding="utf-8")
    if args.context:
        c = Path(args.context)
        if c.exists():
            msg += f"\n\n## 补充材料（{c.name}）\n\n" + c.read_text(encoding="utf-8")
    if args.focus:
        msg += f"\n\n## 本次审计重点\n{args.focus}"

    try:
        out = call(key, msg)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: API 调用失败: {exc}", file=sys.stderr)
        sys.exit(2)

    print(out["content"])
    print(f"\n---\n审计模型 {MODEL}｜usage {json.dumps(out.get('usage', {}), ensure_ascii=False)}", file=sys.stderr)
    if args.log_file:
        Path(args.log_file).write_text(json.dumps(
            {"model": MODEL, "file": str(p), "content": out["content"], "usage": out.get("usage", {})},
            ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
