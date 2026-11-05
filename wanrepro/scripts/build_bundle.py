#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 wan-repro 整合包（可迁移 Windows 单目录，拷到任意客户机即可运行）。

用法（开发机上跑，需可出网下载 runtime，首次下载后有缓存）：
    py -3 scripts/build_bundle.py [--out E:/AI/wan-repro-bundle] [--skip-web-build]

产物结构：
    <out>/app/{ui/server.py, pipeline/, prompts/, templates/, library/, web-dist/}
    <out>/runtime/{python/, ffmpeg/}
    <out>/config/{token, channel-config.example.json}
    <out>/work/{projects/, uploads/}   <out>/logs/
    <out>/启动整合包.bat   <out>/docs/{DEPLOY-GUIDE.md, licenses/}
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANVAS_WEB = Path("E:/User/GitHub/infinite-canvas/web")
RUNTIME_DL = Path("E:/AI/wan-repro-bundle/runtime/_dl")  # 下载缓存（构建时复用）

CPYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
               "20241016/cpython-3.11.10+20241016-x86_64-pc-windows-msvc-shared-install_only.tar.gz")
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")

# 随 app/ 的仓库目录（拉片链运行所需；产品中立——projects/ 与原始资料绝不进包）
APP_DIRS = ["pipeline", "prompts", "templates", "library", "specs"]
APP_FILES = ["analyze.py", "ui/server.py"]


def sh(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(str(c) for c in cmd[:6]), "...")
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if r.returncode != 0:
        sys.exit(f"[x] 命令失败 exit={r.returncode}: {cmd}")


def download_runtime(dl: Path) -> tuple[Path, Path]:
    dl.mkdir(parents=True, exist_ok=True)
    cpy, ff = dl / "cpython.tar.gz", dl / "ffmpeg.zip"
    import urllib.request
    proxy = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy)
    for url, dst in [(CPYTHON_URL, cpy), (FFMPEG_URL, ff)]:
        if dst.exists() and dst.stat().st_size > 10_000_000:
            print(f"[=] 已有缓存 {dst.name}（{dst.stat().st_size // 1048576}MB）")
            continue
        print(f"[..] 下载 {url.rsplit('/', 1)[-1]} ...")
        with opener.open(url, timeout=600) as resp, open(dst, "wb") as f:
            shutil.copyfileobj(resp, f)
        print(f"[√] {dst.name} {dst.stat().st_size // 1048576}MB")
    return cpy, ff


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="E:/AI/wan-repro-bundle")
    ap.add_argument("--skip-web-build", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)

    # 1. 画布静态产物（vite build → dist）
    if not args.skip_web_build:
        sh(["node", "node_modules/vite/bin/vite.js", "build", "--logLevel", "warn"], cwd=CANVAS_WEB)
    dist = CANVAS_WEB / "dist"
    assert (dist / "index.html").exists(), "画布 build 产物缺失（dist/index.html）"

    # 2. runtime（下载缓存→解压）
    cpy, ff = download_runtime(RUNTIME_DL)
    runtime = out / "runtime"
    if not (runtime / "python" / "python.exe").exists():
        tmp = Path(tempfile.mkdtemp(prefix="wrp-bundle-"))
        with tarfile.open(cpy) as t:
            t.extractall(tmp)
        pydir = next(p for p in tmp.iterdir() if p.is_dir() and (p / "python.exe").exists())
        shutil.copytree(pydir, runtime / "python", dirs_exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        print("[√] runtime/python 就位")
    if not (runtime / "ffmpeg" / "ffmpeg.exe").exists():
        with zipfile.ZipFile(ff) as z:
            for name in z.namelist():
                base = Path(name).name
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    z.extract(name, runtime / "_ff-tmp")
                    src = runtime / "_ff-tmp" / name
                    (runtime / "ffmpeg").mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), runtime / "ffmpeg" / base)
        shutil.rmtree(runtime / "_ff-tmp", ignore_errors=True)
        print("[√] runtime/ffmpeg 就位")

    # 3. app/
    app = out / "app"
    for d in APP_DIRS:
        shutil.copytree(REPO / d, app / d, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    for f in APP_FILES:
        (app / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, app / f)
    shutil.rmtree(out / "app" / "web-dist", ignore_errors=True)
    shutil.copytree(dist, app / "web-dist")
    print("[√] app/ 就位（含 web-dist）")

    # 4. config / work / logs / docs
    cfg = out / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    if not (cfg / "token").exists():
        import secrets
        (cfg / "token").write_text(secrets.token_hex(8), encoding="utf-8")
    example = {
        "_说明": "复制为 channel-config.json 并填入 apiKey 后，客户浏览器首次打开画布会自动导入（只导入一次）。产品中立：勿写入产品名。",
        "channels": [
            {"name": "示例·聚鑫SDGF", "baseUrl": "https://sdapi.jxinai.com", "apiKey": "填我",
             "apiFormat": "sdgf",
             "models": [{"name": "按渠道模型清单填"}]},
        ],
    }
    (cfg / "channel-config.example.json").write_text(
        json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    # analyze.py 的产物目录（REPO_ROOT/analysis/reverse）——预建空目录
    for d in ["app/analysis/reverse"]:
        (out / d).mkdir(parents=True, exist_ok=True)
    for d in ["work/projects", "work/uploads", "logs"]:
        (out / d).mkdir(parents=True, exist_ok=True)
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "scripts/bundle/DEPLOY-GUIDE.md", docs / "DEPLOY-GUIDE.md")
    lic = docs / "licenses"
    lic.mkdir(exist_ok=True)
    lic.joinpath("THIRD-PARTY.md").write_text(
        "| 组件 | 版本 | 许可证 | 说明 |\n|---|---|---|---|\n"
        "| CPython (python-build-standalone) | 3.11.10 | PSF License | https://python.org/download/ |\n"
        "| FFmpeg (BtbN GPL build) | master | GPL-3.0 | https://github.com/BtbN/FFmpeg-Builds；本包仅作为独立命令行工具调用，未修改其源码 |\n",
        encoding="utf-8")
    # 管理器：ASCII 薄壳 bat + UTF-8 BOM 的 manage.ps1（双击中文界面零编码坑）
    shutil.copy2(REPO / "scripts/bundle/启动整合包.bat", out / "启动整合包.bat")
    raw = (REPO / "scripts/bundle/manage.ps1").read_text(encoding="utf-8")
    (out / "manage.ps1").write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
    print("[√] config/work/logs/docs 就位")

    # 5. 自检报告
    checks = {
        "runtime/python/python.exe": (runtime / "python" / "python.exe").exists(),
        "runtime/ffmpeg/ffmpeg.exe": (runtime / "ffmpeg" / "ffmpeg.exe").exists(),
        "runtime/ffmpeg/ffprobe.exe": (runtime / "ffmpeg" / "ffprobe.exe").exists(),
        "app/ui/server.py": (app / "ui" / "server.py").exists(),
        "app/web-dist/index.html": (app / "web-dist" / "index.html").exists(),
        "app/pipeline/reverse/analyze.py": (app / "pipeline" / "reverse" / "analyze.py").exists(),
        "app/specs/jimeng-lapian.md": (app / "specs" / "jimeng-lapian.md").exists(),
        "启动整合包.bat": (out / "启动整合包.bat").exists(),
        "manage.ps1": (out / "manage.ps1").exists(),
        "docs/DEPLOY-GUIDE.md": (docs / "DEPLOY-GUIDE.md").exists(),
        "config/token": (cfg / "token").exists(),
    }
    ok = all(checks.values())
    for k, v in checks.items():
        print(("  [√] " if v else "  [x] MISSING ") + k)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\n整合包: {out}  总大小约 {total // 1048576}MB")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
