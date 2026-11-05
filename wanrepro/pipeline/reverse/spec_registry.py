"""规范注册表 —— 扫 specs/ 出清单，校验引用目标（防双源漂移）。

规范两类（docs/方案-画布MVP-20261103.md §1.2）：
  类型: 拉片投影   自带块定义表（render_lapian.py 消费）
  类型: 生成       frontmatter `正文模板:` 引用 templates/<name>.md（render.py 消费）
生成规范不复制模板正文——templates/ 是生成正文唯一真源；本表启动即校验引用存在，
引用目标比 spec 新时打漂移提示（mtime 只提示不拦截，人核）。

用法：
    py -3 pipeline/reverse/spec_registry.py            # 人看清单
    py -3 pipeline/reverse/spec_registry.py --json     # 机器（bridge/画布）用
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render import EXIT_INPUT, EXIT_TEMPLATE, Fail, ROOT, read_text, split_frontmatter  # noqa: E402

SPEC_DIR = ROOT / "specs"
VALID_TYPES = ("拉片投影", "生成")


def load_registry(spec_dir: Path = SPEC_DIR) -> list[dict]:
    if not spec_dir.is_dir():
        raise Fail(EXIT_INPUT, f"规范目录不存在：{spec_dir}")
    specs: list[dict] = []
    drift: list[str] = []
    for p in sorted(spec_dir.glob("*.md")):
        fm, _ = split_frontmatter(read_text(p, EXIT_TEMPLATE, f"规范 {p.name}"))
        typ = fm.get("类型", "").strip()
        if typ not in VALID_TYPES:
            raise Fail(EXIT_TEMPLATE,
                       f"specs/{p.name} 的「类型: {typ or '（空）'}」不合法（只认 {'/'.join(VALID_TYPES)}）")
        item = {
            "file": p.name,
            "名称": fm.get("名称", p.stem).strip(),
            "类型": typ,
            "适配模型": fm.get("适配模型", "").strip(),
            "优先级": fm.get("优先级", "").strip(),
            "出处": fm.get("出处", "").strip(),
            "正文模板": fm.get("正文模板", "").strip(),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        if typ == "生成":
            ref = item["正文模板"]
            if not ref:
                raise Fail(EXIT_TEMPLATE, f"specs/{p.name} 是生成规范但缺「正文模板:」引用")
            target = ROOT / ref
            if not target.is_file():
                raise Fail(EXIT_TEMPLATE,
                           f"specs/{p.name} 引用的「{ref}」不存在——双源漂移（模板被删/改名，spec 未跟）")
            if target.stat().st_mtime > p.stat().st_mtime:
                drift.append(f"specs/{p.name} 早于其引用的 {ref}（模板改过，spec 未跟）")
        specs.append(item)
    if not specs:
        raise Fail(EXIT_INPUT, f"{spec_dir} 里没有任何规范文件")
    for d in drift:
        print(f"[WARN] 漂移提示：{d}", file=sys.stderr)
    return specs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规范注册表：扫 specs/，校验引用")
    ap.add_argument("--json", action="store_true", help="stdout 打 JSON")
    args = ap.parse_args(argv)
    specs = load_registry()
    if args.json:
        print(json.dumps(specs, ensure_ascii=False, indent=1))
    else:
        lapian = [s for s in specs if s["类型"] == "拉片投影"]
        gen = [s for s in specs if s["类型"] == "生成"]
        print(f"规范 {len(specs)} 套（拉片投影 {len(lapian)} / 生成 {len(gen)}）：")
        for s in specs:
            tag = "（生成，引用 templates）" if s["类型"] == "生成" else "（拉片投影）"
            print(f"  - {s['名称']}｜适配：{s['适配模型'] or '—'}｜优先级：{s['优先级'] or '—'}{tag}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"FAIL {e}", file=sys.stderr)
        sys.exit(2)
