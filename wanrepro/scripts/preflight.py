#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付前自检：五件必须成立的事，缺一件就非 0 退出。

为什么要有它：一条命令能跑，不等于「换台机器也能跑」。
最常见的三种当场翻车——Python 版本太低、ffmpeg 不在 PATH、密钥没设——
它们的报错都长得像「模型失败」，实际一条都没调过网络。先花两秒查清楚，
别让人在 40 秒的拆解之后才发现。

查什么
------
1. Python ≥ 3.10（本项目用了 `X | None` 这种新语法）
2. ffmpeg / ffprobe 在 PATH，且能执行（不只是文件在，要真跑出版本号）
3. JXIN_API_KEY 已设（环境变量或 hermes 的 .env；**只报有没有、多长，不打印值**）
4. 四套模板能过 render.py 的校验器（frontmatter / 段落数 / 范例引文逐条命中）
5. library/examples/ 的 17 条范例齐（编号 01–17 一个不缺）

用法
----
    py -3 scripts/preflight.py

退出码
------
0 全部 PASS
1 至少一项 FAIL（哪项 FAIL 就修哪项，不看别的）
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "templates"
EXAMPLES_DIR = ROOT / "library" / "examples"
REVERSE_DIR = ROOT / "pipeline" / "reverse"

TEMPLATES = ("lumi-4section", "employee-checklist", "seven-section", "timeline-narrative",
             "seedance-flow")
EXAMPLE_COUNT = 17
MIN_PYTHON = (3, 10)

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"       {detail}")
    return ok


def load_module(name: str, path: Path):
    """按路径导入 pipeline/reverse 下的脚本（那个目录不是包），复用它的校验逻辑。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 1 Python 版本

def check_python() -> None:
    current = sys.version_info
    ok = (current.major, current.minor) >= MIN_PYTHON
    detail = f"运行解释器 {sys.version.split()[0]}｜{sys.executable}"
    if not ok:
        detail += f"｜要 ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    record("Python 版本", ok, detail)


# ---------------------------------------------------------------- 2 ffmpeg / ffprobe

def check_ffmpeg() -> None:
    missing = []
    versions = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            missing.append(tool)
            continue
        try:
            proc = subprocess.run([path, "-version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            missing.append(f"{tool}（在 {path} 但跑不起来：{exc}）")
            continue
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        if proc.returncode != 0 or not first:
            missing.append(f"{tool}（退出码 {proc.returncode}）")
            continue
        versions.append(f"{tool} {first[0].split()[2] if len(first[0].split()) > 2 else '?'}｜{path}")

    if missing:
        record("ffmpeg / ffprobe 在 PATH", False,
               "缺：" + "、".join(missing) + "｜装 ffmpeg 并把 bin 目录加进 PATH")
        return
    record("ffmpeg / ffprobe 在 PATH", True, "；".join(versions))


# ---------------------------------------------------------------- 3 密钥

def check_api_key() -> None:
    try:
        extract = load_module("_preflight_extract", REVERSE_DIR / "extract.py")
    except (ImportError, OSError) as exc:
        record("JXIN_API_KEY", False, f"载入 extract.py 失败，无法按它的规矩读密钥：{exc}")
        return

    key = extract.load_api_key()
    if not key:
        record("JXIN_API_KEY", False,
               "没读到密钥：设环境变量 JXIN_API_KEY，或写进 $HERMES_HOME/.env、~/.hermes/.env")
        return

    from_env = bool((os.environ.get(extract.KEY_ENV) or "").strip())
    source = "环境变量" if from_env else ".env 文件"
    record("JXIN_API_KEY", True, f"{source}｜已设置，长度 {len(key)}，值不打印")


# ---------------------------------------------------------------- 4 四套模板

def check_templates() -> None:
    try:
        render = load_module("_preflight_render", REVERSE_DIR / "render.py")
        corpus = render.ExampleCorpus(ROOT)
    except (ImportError, OSError) as exc:
        record("四套模板可解析", False, f"载入 render.py 失败：{exc}")
        return

    broken = []
    summary = []
    for name in TEMPLATES:
        path = TEMPLATES_DIR / f"{name}.md"
        if not path.is_file():
            broken.append(f"{name}：templates/{name}.md 不存在")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter, body = render.split_frontmatter(text)
            main_sections, optional_sections = render.parse_sections(body)
            render.validate_template(frontmatter, main_sections, optional_sections,
                                     path, ROOT, corpus, [])
        except render.Fail as err:
            broken.append(f"{name}：{err.msg}")
            continue
        except OSError as exc:
            broken.append(f"{name}：读不到模板（{exc}）")
            continue
        summary.append(f"{name} {len(main_sections)} 段")

    if broken:
        record("四套模板可解析", False, "｜".join(broken))
        return
    record("四套模板可解析", True,
           f"{len(summary)}/{len(TEMPLATES)} 通过校验（{ '、'.join(summary) }）"
           f"｜范例引文逐条命中 library/examples/")


# ---------------------------------------------------------------- 5 范例库

def check_examples() -> None:
    if not EXAMPLES_DIR.is_dir():
        record("library/examples/ 17 条范例", False, f"目录不存在：{EXAMPLES_DIR}")
        return

    numbered = sorted(p.name for p in EXAMPLES_DIR.glob("*.md") if re.match(r"^\d{2}-", p.name))
    missing = [f"{index:02d}" for index in range(1, EXAMPLE_COUNT + 1)
               if not any(name.startswith(f"{index:02d}-") for name in numbered)]
    extra = [name for name in numbered if int(name[:2]) > EXAMPLE_COUNT]
    if missing:
        record("library/examples/ 17 条范例", False,
               f"缺编号 {'、'.join(missing)}（现有 {len(numbered)} 条）")
        return
    if len(numbered) != EXAMPLE_COUNT or extra:
        record("library/examples/ 17 条范例", False,
               f"应有 {EXAMPLE_COUNT} 条，实际 {len(numbered)} 条"
               + (f"；多出 {extra}" if extra else ""))
        return
    others = sorted(p.name for p in EXAMPLES_DIR.glob("*") if p.is_file() and p.name not in numbered)
    record("library/examples/ 17 条范例", True,
           f"编号 01–{EXAMPLE_COUNT:02d} 齐全"
           + (f"｜另有 {'、'.join(others)}（索引，不算范例）" if others else ""))


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print("=" * 78)
    print("preflight · 交付前自检（只查环境，不调网络、不花钱）")
    print(f"仓库：{ROOT}")
    print("=" * 78)

    check_python()
    check_ffmpeg()
    check_api_key()
    check_templates()
    check_examples()

    failed = [name for name, ok, _ in results if not ok]
    print("-" * 78)
    print(f"合计 {len(results)} 项：{len(results) - len(failed)} PASS / {len(failed)} FAIL")
    if failed:
        print("FAIL：" + "、".join(failed))
        return 1
    print("全部通过：可以跑 `py -3 analyze.py <视频>`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
