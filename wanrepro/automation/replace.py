# -*- coding: utf-8 -*-
"""AT-4 置换接收器——员工把替换图交给 Agent，代上传+回填清单+回显确认。

P1（置换场所）所有者挂起中：本模块是「输入面 A=交 Agent」的实现；
若日后拍板 B（画布同步），输入面换数据源即可，下游合同不变
（清单回填完成 + assets_confirmed → rendering）。

桥合同（server.py 实读 2026-11-08）：
  POST /api/asset/upload：原始字节 + X-Filename（png/jpg/webp ≤20MB）
    → {url: 公网HTTPS直链}（uguu 图床；SDGF images 只收公网 HTTPS）
  清单文件：analysis/reverse/<bridge_id>/待确认清单.md 的 Markdown 表格
    （| # | 类别 | 模型看到什么 | 命名 | 资产图 | … |，# 为行号）

用法：
  py -3 -m automation.replace auto-0001 --assets <图目录或文件...> [--map "1=a.png,2=b.png"]
    [--name "1=我们的产品"]        # 写回显，状态留 awaiting_assets
  py -3 -m automation.replace auto-0001 --confirm  # 确认后 → rendering
缺省映射：按「必须换」行优先、其余配「主体」行序，回显供确认。
"""
import argparse
import re
import sys
from pathlib import Path

from . import bridge, config, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
LIST_NAME = "待确认清单.md"


class ReplaceError(RuntimeError):
    pass


def checklist_path(bridge_id: str) -> Path:
    # ROOT=wanrepro（automation 包的上一级）；桥任务目录 analysis/reverse/<id>/
    p = Path(__file__).resolve().parent.parent / "analysis" / "reverse" / bridge_id / LIST_NAME
    if not p.is_file():
        raise ReplaceError(f"找不到清单 {p}（任务是否已拆解？）")
    return p


def row_cells(raw: str) -> list[str]:
    """表格行 → 单元格（含首格 #）。容全角竖线，但**不把正文里的 ｜ 当分隔符**。

    半角竖线优先；只有半角切不出 ≥5 格、行里又有全角竖线时才按全角切。
    反过来的话，正文本身带「｜」的行（pop-3--7 品牌位一行写了三处包装字）会被切成错位的假列，
    「建议」「资产图」全部串位——那是把人手填的列当成了模型写的正文。
    """
    body = raw.strip().strip("|｜").strip()
    if "|" in body:
        cells = [c.strip() for c in body.split("|")]
        if len(cells) >= 5:
            return cells
    if "｜" in body:
        return [c.strip() for c in body.strip("｜").split("｜")]
    return [body]


def parse_rows(md_text: str) -> list[dict]:
    """读层归一：行号/类别/描述/命名/现有资产图列/建议。容全角竖线（红线：不假设格式）。"""
    rows = []
    for raw in md_text.splitlines():
        cells = row_cells(raw)
        if len(cells) < 5 or not re.fullmatch(r"\d+", cells[0] or ""):
            continue
        if cells[1] in ("类别", "---", "------"):
            continue
        rows.append({"no": int(cells[0]), "line": raw, "cat": cells[1],
                     "desc": cells[2],
                     "name": cells[3],
                     "cur_assets": cells[4],
                     "advice": cells[6] if len(cells) > 6 else ""})
    return rows


def fill_row(md_text: str, no: int, asset_urls: list[str], name: str | None,
             overwrite: bool = False) -> str:
    """按 # 写「资产图」列（与「命名」列）。返回新全文。行格式以解析到的原样为准。

    缺省（overwrite=False）与历史行为一致：资产图列**追加**（多图逗号隔开）。
    overwrite=True = 置换台定向重传/「沿用」清空：整格替换（asset_urls 为空即清空该行绑定）。
    """
    out = []
    hit = False
    for raw in md_text.splitlines():
        cells = row_cells(raw)
        if cells and re.fullmatch(r"\d+", cells[0] or "") and int(cells[0]) == no:
            while len(cells) < 8:
                cells.append("")
            joined = ",".join(asset_urls)
            if overwrite:
                cells[4] = joined
            elif joined:
                cells[4] = joined if not cells[4] else f"{cells[4]},{joined}"
            if name:
                cells[3] = name
            out.append("| " + " | ".join(cells) + " |")
            hit = True
        else:
            out.append(raw)
    if not hit:
        raise ReplaceError(f"清单里没有第 {no} 行")
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else "")


def set_row(bridge_id: str, no: int, *, image_urls: list[str] | None = None,
            name: str | None = None, overwrite: bool = False) -> dict:
    """定向写清单一行（AT-8 置换台写路径）：资产图列覆盖/清空 + 命名列。返回写后该行。

    写路径格式定死（数据合同 v3）：前端先走 uguu 拿 URL，再调这里写 md；不新增 multipart。
    """
    if image_urls is None and name is None:
        raise ReplaceError("没有要写的东西：给 image_urls 或 name 之一")
    path = checklist_path(bridge_id)
    before = path.read_text(encoding="utf-8")
    after = fill_row(before, no, list(image_urls or []), name, overwrite=overwrite)
    if after != before:
        path.write_text(after, encoding="utf-8")
    row = next((r for r in parse_rows(after) if r["no"] == no), None)
    if row is None:
        raise ReplaceError(f"清单里没有第 {no} 行")
    return row


def collect_images(src: str) -> list[Path]:
    p = Path(src)
    if p.is_file():
        imgs = [p] if p.suffix.lower() in IMG_EXTS else []
    elif p.is_dir():
        imgs = sorted(q for q in p.iterdir() if q.suffix.lower() in IMG_EXTS)
    else:
        raise ReplaceError(f"资产图路径不存在: {src}")
    if not imgs:
        raise ReplaceError(f"{src} 里没有 png/jpg/webp 图片")
    return imgs


def default_mapping(rows: list[dict], images: list[Path]) -> list[tuple[int, Path]]:
    """缺省配对：「必须换」行优先 → 其余按「主体」行序。"""
    must = [r["no"] for r in rows if "必须换" in r["line"]]
    subjects = [r["no"] for r in rows if r["cat"] == "主体"]
    order = must + [n for n in subjects if n not in must]
    if len(images) > len(order):
        raise ReplaceError(f"图 {len(images)} 张多于可配行 {len(order)}（主体+必须换）——用 --map 显式指定")
    return list(zip(order, images))


def parse_map(spec: str) -> list[tuple[int, str]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ReplaceError(f"--map 项缺 '='：{part}（形态 1=图.png）")
        k, v = part.split("=", 1)
        if not k.strip().isdigit():
            raise ReplaceError(f"--map 行号必须是数字：{part}")
        out.append((int(k.strip()), v.strip()))
    if not out:
        raise ReplaceError("--map 是空的")
    return out


def run_replace(task_dir: Path, *, assets: list[str] | None, map_spec: str | None,
                name_spec: str | None, confirm: bool, verbose: bool = True) -> str:
    task = state.load(task_dir)
    cur = task["state"]
    if confirm:
        if cur != "awaiting_assets":
            raise ReplaceError(f"确认置换需 awaiting_assets，当前 {cur}")
        pending = task.get("pending_replace")
        if not pending:
            raise ReplaceError("没有待确认的置换（先跑 --assets 回填再 --confirm）")
        state.transition(task_dir, "rendering",
                         require={"assets_confirmed": True},
                         note="所有者确认置换", extra={"assets_confirmed": pending})
        if verbose:
            print(f"[OK] {task['local_id']}: 置换已确认 → rendering（回填 {len(pending)} 行）")
        return "rendering"

    if cur != "awaiting_assets":
        raise ReplaceError(f"回填需 awaiting_assets（先完成拆解），当前 {cur}")

    images: list[Path] = []
    for a in assets or []:
        images.extend(collect_images(a))
    if not images and not map_spec:
        raise ReplaceError("没有输入：--assets <目录/文件> 或 --map 必须给一个")

    md_path = checklist_path(task["bridge_id"])
    md = md_path.read_text(encoding="utf-8")
    rows = parse_rows(md)
    by_no = {r["no"]: r for r in rows}

    pairs: list[tuple[int, Path]] = []
    if map_spec:
        files = {p.name: p for p in images}
        for no, fname in parse_map(map_spec):
            if no not in by_no:
                raise ReplaceError(f"--map 行 #{no} 不在清单（行号见回显）")
            if fname not in files:
                raise ReplaceError(f"--map 引用的图不在输入里: {fname}")
            pairs.append((no, files[fname]))
    else:
        pairs = default_mapping(rows, images)

    names = dict(parse_map(name_spec)) if name_spec else {}

    filled = []
    for no, img in pairs:
        up = bridge.upload_asset(img)
        url = up.get("url") or ""
        if not url.startswith("https://"):
            raise ReplaceError(f"图 {img.name} 上传未回公网 HTTPS（SDGF 只收公网图）: {up}")
        md = fill_row(md, no, [url], names.get(no))
        filled.append({"no": no, "img": img.name, "url": url,
                       "name": names.get(no, ""), "desc": by_no[no]["desc"][:40]})

    md_path.write_text(md, encoding="utf-8")
    state.save(task_dir, {**task, "pending_replace": filled})
    if verbose:
        print(f"[回显] {task['local_id']} 置换配对（确认无误后跑 --confirm）：")
        for f in filled:
            n = f" 命名「{f['name']}」" if f["name"] else ""
            print(f"  #{f['no']:>2} ← {f['img']}{n}  （原描述: {f['desc']}…）")
    return "awaiting_assets"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-4 置换接收器")
    ap.add_argument("local_id")
    ap.add_argument("--assets", nargs="*", help="资产图目录或文件（可多个）")
    ap.add_argument("--map", help="显式配对：1=a.png,2=b.png")
    ap.add_argument("--name", help="命名回填：1=我们的产品")
    ap.add_argument("--confirm", action="store_true", help="确认置换 → rendering")
    ap.add_argument("--bridge")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    tdir = config.PROJECTS_DIR / args.local_id
    if not (tdir / "任务.json").is_file():
        raise SystemExit(f"找不到任务 {args.local_id}")
    try:
        run_replace(tdir, assets=args.assets, map_spec=args.map,
                    name_spec=args.name, confirm=args.confirm)
        return 0
    except (ReplaceError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
