# -*- coding: utf-8 -*-
"""wanrepro — 参考视频复刻引擎的工具层（MCP server）

设计原则：
- 这里只做**编排与门禁**，真正的重活都在 pipeline/reverse/*.py 里，不重复实现
- 每个工具都返回结构化结果 {ok, code, out, err, data}，调用方不用解析人类可读文本
- 写提示词必须先过 guide（读案例库），提交必须先过 gate（生成前审计）

启动：py -3 -m wanrepro.server        （stdio，MCP 客户端直接拉起）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY = sys.executable or "py"


def _run(args: list[str], timeout: int = 300) -> dict:
    """跑 pipeline 脚本，返回结构化结果"""
    proc = subprocess.run(
        [PY, *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "out": proc.stdout.strip(),
        "err": proc.stderr.strip(),
    }


def _json_tail(text: str):
    """从输出里捞最后一个 JSON 对象（脚本可能在前面打印人类可读内容）"""
    for m in re.finditer(r"\{[\s\S]*\}", text):
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- tools

def guide(task: str = "render", assets: int = 3, limit: int = 3) -> dict:
    """写提示词前必读：返回八段/七段规范 + 本次任务必读案例全文 + 案例库版本。

    调用后会把读取凭证写到 .wanrepro/guides/，gate 会核验。
    """
    args = ["pipeline/reverse/guide.py", "--task", task, "--limit", str(limit)]
    if assets:
        args += ["--assets", str(assets)]
    r = _run(args)
    r["data"] = {"library": None, "cases": []}
    m = re.search(r"lib-[0-9a-f]{8}", r["out"])
    if m:
        r["data"]["library"] = m.group(0)
    r["data"]["cases"] = re.findall(r"\b[bc]-[a-z0-9\-]+", r["out"])
    return r


def render(spec: str, product: str, out: str, assets_manifest: str | None = None) -> dict:
    """spec.json + 产品参数 → 万相提示词草稿（不做审计）"""
    args = ["pipeline/reverse/render_prompt.py", spec, "-p", product, "-o", out]
    if assets_manifest:
        args += ["--assets", assets_manifest]
    r = _run(args)
    p = ROOT / out
    r["data"] = {"path": str(p), "chars": len(p.read_text(encoding="utf-8")) if p.exists() else 0}
    return r


def lint(prompt: str, tech: str, assets: int | None = None, product: str = "") -> dict:
    """提示词体检：七段结构、负向减法、词表分级、@图片N 数量"""
    args = ["pipeline/reverse/prompt_lint.py", "--prompt", prompt, "--tech", tech]
    if product:
        args += ["--product", product]
    if assets is not None:
        args += ["--assets", str(assets)]
    r = _run(args)
    r["data"] = {
        "errors": len(re.findall(r"\bERROR\b", r["out"])),
        "warns": len(re.findall(r"\bWARN\b", r["out"])),
        "infos": len(re.findall(r"\bINFO\b", r["out"])),
    }
    return r


def gate(prompt: str, tech: str, images: str, resolution: str = "480P",
         duration: float | None = None, max_credits: float = 0, kind: str = "video") -> dict:
    """生成前审计门禁；全 PASS 才签发 token。不提交任何计费动作。"""
    args = ["pipeline/reverse/gate.py", "--prompt", prompt, "--tech", tech,
            "--images", images, "--kind", kind, "--resolution", resolution]
    if duration:
        args += ["--duration", str(duration)]
    if max_credits:
        args += ["--max-credits", str(max_credits)]
    r = _run(args)
    m = re.search(r"gt-[0-9a-f]{10}", r["out"])
    r["data"] = {"token": m.group(0) if m else None,
                 "fails": len(re.findall(r"\bFAIL\b", r["out"]))}
    return r


def status() -> dict:
    """账户灵感值 + 在途任务数（只读，不计费）"""
    r = _run(["pipeline/gen/wan_video.py", "--help"], timeout=60)   # 旧 gen_video 已删：提交只走 gen/
    credits = None
    try:
        proc = subprocess.run(["wan.cmd", "credits"], cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        m = re.search(r"(\d{2,6})", proc.stdout)
        credits = int(m.group(1)) if m else None
    except Exception as e:                                    # noqa: BLE001
        credits = f"unavailable: {e}"
    r["data"] = {"credits": credits}
    return r


def basis(rebuild: bool = False) -> dict:
    """读《提示词编写依据》：旧规则库已删除，重建后另立版本（本工具暂不可用）。"""
    builder = ROOT / "pipeline" / "basis" / "build_basis.py"
    if rebuild:
        if not builder.exists():
            raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
        _run(["pipeline/basis/build_basis.py"])
    p = ROOT / "spec" / "prompt_basis.json"
    if not p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    d = json.loads(p.read_text(encoding="utf-8"))
    lines = [f"# 提示词编写依据 {d['version']}（官方原文指纹 {d['official_sha256_16']}）",
             f"# 规则 {d['counts']['rules']} 条｜反例 {d['counts']['antipatterns']} 条", ""]
    for r in d["rules"]:
        src = r["source"]
        lines.append(f"[{r['id']}] {r['level']}/{r['enforcement']}｜{r['statement']}")
        lines.append(f"      出处 {src['file']}:{src['lines']}")
        lines.append(f"      原文 {src['quote'][:180]}")
    lines.append("")
    for a in d["antipatterns"]:
        lines.append(f"[{a['id']}] 反例·{a['name']}｜{a['symptom']} → {a['rule']}")
    return {"ok": True, "code": 0, "out": "\n".join(lines), "err": "",
            "data": {"version": d["version"], "rules": d["counts"]["rules"],
                     "antipatterns": d["counts"]["antipatterns"]}}


def check(prompt: str, tech: str = "", assets: int = 0) -> dict:
    """按《编写依据》体检提示词：旧规则库已删除，重建后另立版本（本工具暂不可用）。"""
    script = ROOT / "pipeline" / "basis" / "check_basis.py"
    if not script.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    args = ["pipeline/basis/check_basis.py", "--prompt", prompt]
    if tech:
        args += ["--tech", tech]
    if assets:
        args += ["--assets", str(assets)]
    r = _run(args)
    r["data"] = {"fails": len(re.findall(r"❌", r["out"])),
                 "warns": len(re.findall(r"⚠️", r["out"])),
                 "verdict": "pass" if r["code"] == 0 else "fail"}
    return r


def brand(project: str, color: str = "") -> dict:
    """按项目目录读品牌资产：project = projects/<id>/product.json 或该目录。

    工具层不含任何品牌信息；品牌色、字标、标语都来自项目配置。
    """
    p = Path(project)
    base = p if p.is_dir() else p.parent
    spec_p = base / "product.json"
    if not spec_p.exists():
        return {"ok": False, "code": 2, "out": f"未找到 {spec_p}", "err": "", "data": {}}
    spec = json.loads(spec_p.read_text(encoding="utf-8"))
    br = spec.get("brand", {})
    colors = br.get("colors", {})
    want = color or next(iter(colors), "")
    wp = base / br.get("wordmark_png", "")
    return {
        "ok": True, "code": 0, "out": "", "err": "",
        "data": {"project": spec.get("id"), "wordmark": str(wp), "wordmark_exists": wp.exists(),
                 "color": want, "color_hex": colors.get(want), "colors": colors,
                 "tagline": br.get("tagline"), "endcard": br.get("endcard")},
    }


TOOLS = {
    "basis": basis, "check": check,
    "guide": guide, "render": render, "lint": lint,
    "gate": gate, "status": status, "brand": brand,
}


def main() -> int:
    """无依赖的最小 MCP stdio 实现（JSON-RPC 2.0，工具面见 TOOLS）"""
    from wanrepro.mcp_stdio import serve
    return serve("wanrepro", "参考视频复刻引擎", TOOLS)


if __name__ == "__main__":
    raise SystemExit(main())
