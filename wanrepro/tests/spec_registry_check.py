"""M2 验收：规范注册表断言（docs/方案-画布MVP-20261103.md §4）。

  1 列表含全部 6 套（jimeng-lapian + 五套生成）
  2 加 spec 文件 → 列表即现（加文件不改引擎）
  3 生成规范引用不存在目标 → 非零退出（双源漂移显式 FAIL）
  4 引用目标比 spec 新 → stderr 漂移 WARN（提示不拦截）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "pipeline" / "reverse" / "spec_registry.py"
WANT = {"jimeng-lapian", "lumi-4section", "employee-checklist", "seven-section",
        "timeline-narrative", "seedance-flow"}


def run(*argv: str) -> tuple[int, str, str]:
    r = subprocess.run([sys.executable, str(REG), *argv], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(ROOT))
    return r.returncode, r.stdout, r.stderr


def main() -> int:
    fails: list[str] = []
    # 1 六套全在
    code, out, _ = run("--json")
    import json
    specs = json.loads(out) if code == 0 else []
    names = {s["file"].removesuffix(".md") for s in specs}
    miss = WANT - names
    if code != 0 or miss:
        fails.append(f"[1] 注册表缺规范：{sorted(miss) or '（渲染失败 exit=%d）' % code}")
    # 2 加文件即现
    tmp_spec = ROOT / "specs" / "zz-test-spec.md"
    tmp_spec.write_text("---\n名称: 测试规范\n类型: 生成\n适配模型: 通用\n优先级: 测试\n"
                        "出处: 测试\n正文模板: templates/seedance-flow.md\n---\n# 测试\n",
                        encoding="utf-8")
    try:
        code2, out2, _ = run("--json")
        got2 = {s["file"].removesuffix(".md") for s in json.loads(out2)} if code2 == 0 else set()
        if "zz-test-spec" not in got2:
            fails.append("[2] 加 spec 文件后列表未出现 zz-test-spec")
        # 4 漂移提示：把引用目标 touch 成比 spec 新
        os.utime(ROOT / "templates" / "seedance-flow.md",
                 (time.time() + 5, time.time() + 5))
        _, _, err4 = run("--json")
        if "漂移" not in err4:
            fails.append("[4] 引用目标更新后无漂移 WARN")
    finally:
        tmp_spec.unlink(missing_ok=True)
    # 3 引用目标不存在 → 非零
    bad = ROOT / "specs" / "zz-bad-ref.md"
    bad.write_text("---\n名称: 坏引用\n类型: 生成\n适配模型: 通用\n优先级: 测试\n"
                   "出处: 测试\n正文模板: templates/不存在的模板.md\n---\n# 坏\n",
                   encoding="utf-8")
    try:
        code3, _, _ = run("--json")
        if code3 == 0:
            fails.append("[3] 引用不存在目标仍退出 0（双源漂移未拦截）")
    finally:
        bad.unlink(missing_ok=True)

    if fails:
        print(f"{len(fails)} FAIL / M2 验收未过")
        for f in fails:
            print(" -", f)
        return 1
    print("M2 验收：4 断言全过（6 套清单 / 加文件即现 / 坏引用拦截 / 漂移 WARN）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
