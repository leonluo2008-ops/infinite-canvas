#!/usr/bin/env node
/**
 * wan-repro 整合包 · 单命令打包器（npm run bundle）
 *
 * 定位：把本仓（fork infinite-canvas 交付分支）里的两处真源打成/同步进一个可迁移的
 * Windows 单目录整合包——`web/`（画布前端）+ `wanrepro/`（引擎运行时）。规则真源：
 * wan-repro 仓 `docs/项目适配包.md` §12；通用方法论：skill `portable-windows-bundle`。
 *
 * 用法（本机，仓库根或 web/ 下均可）：
 *   npm run bundle                      # 等价 node scripts/bundle.mjs
 *   npm run bundle:dry                  # 只算清单/体积，不写盘
 *   node scripts/bundle.mjs --out X:/somewhere --skip-web-build
 *   node scripts/bundle.mjs --runtime-from E:/path/to/runtime   # runtime 缺件时从别处补
 *
 * 入口唯一：根 package.json 与 web/package.json 的 `bundle` 都指向本文件；
 * `wanrepro/scripts/build_bundle.py` 是历史上的 python 实现（merge 拷贝、无保留名单），
 * 已停用，勿再扩改——两套打包器就是双轨漂移。
 *
 * 产物结构：
 *   <out>/app/{pipeline,prompts,templates,library,specs,ui,analyze.py,requirements.txt,web-dist}
 *   <out>/runtime/{python,ffmpeg}        ← 已有则原样复用，打包器绝不覆盖
 *   <out>/config/{token, channel-config.example.json}
 *   <out>/app/{.env,uploads,analysis,config}  ← 用户数据，保留不覆盖
 *   <out>/work/{projects,uploads}  <out>/logs/  <out>/docs/
 *   <out>/启动整合包.bat  <out>/manage.ps1  <out>/PACKAGE-INFO.json
 *
 * ⛔ 文件覆盖只能用 copyFileSync：本机 Node 24（Windows）上 fs.cpSync 覆盖**已存在**的文件会
 *    让进程静默崩掉（exit 127、无异常无堆栈）——实测矩阵：cpSync→新文件 OK，cpSync→同名旧文件
 *    必崩；copyFileSync / writeFileSync 覆盖都正常。目录拷贝仍用 cpSync，但目标先 rm -rf。
 *
 * 铁律（每条都是踩坑换来的）：
 * - 打包=拷贝+排除，不是清理源仓；源仓一个字节都不动。
 * - 代码目录先删后拷（不做 merge 拷贝）——否则上一轮删掉的老页/老规范会永远留在包里。
 * - 用户数据（.env / uploads / analysis / config / work / logs）打包器只读不写，绝不删。
 * - runtime 复用：python/ffmpeg 是 300MB+ 的固定件，已有就跳过。
 * - 自检任一 MISSING 即 exit 1；禁止静默通过。
 */
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  copyFileSync, cpSync, existsSync, mkdirSync, readdirSync, readFileSync,
  rmSync, statSync, writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const ENGINE = path.join(REPO, "wanrepro");
const WEB = path.join(REPO, "web");

// ---- 清单（改这里必须同步 self-check 项与 DEPLOY-GUIDE）--------------------
const MANIFEST = {
  // 引擎运行时目录（以「运行时调用图」为准，不以目录观感为准）
  dirs: ["pipeline", "prompts", "templates", "library", "specs", "ui", "spec", "wanrepro", "contracts", "docs"],
  // 根级散件：analyze.py 由 bridge `run_py(["analyze.py", …])` 相对 ROOT 调，漏了=首拆才炸
  files: ["analyze.py", "requirements.txt"],
  webDist: "web-dist",
};

// app/ 下的用户数据：打包器只保留、不覆盖、不删除
const PRESERVE_IN_APP = [".env", "uploads", "analysis", "config", "bridge.log", "output"];

// runtime 固定件（缺任一项视为 runtime 未就位）
const RUNTIME_REQUIRED = [
  "python/python.exe",
  "ffmpeg/ffmpeg.exe",
  "ffmpeg/ffprobe.exe",
];

const COPY_SKIP = new Set(["__pycache__", ".DS_Store", "Thumbs.db"]);
const COPY_SKIP_EXT = [".pyc", ".pyo"];

const args = parseArgs(process.argv.slice(2));
const OUT = path.resolve(
  args.out || process.env.WAN_BUNDLE_OUT || path.join("E:", "AI", "wan-repro-bundle"),
);
const DRY = Boolean(args["dry-run"]);

// ---- 小工具 ---------------------------------------------------------------
function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (!a.startsWith("--")) fail(`看不懂的参数：${a}`);
    const key = a.slice(2);
    const boolKeys = new Set(["dry-run", "skip-web-build", "force", "json"]);
    if (boolKeys.has(key)) out[key] = true;
    else out[key] = argv[++i];
  }
  return out;
}

function fail(msg) {
  console.error(`[x] ${msg}`);
  process.exit(1);
}

function log(mark, msg) {
  console.log(`${mark} ${msg}`);
}

function isInside(child, parent) {
  const rel = path.relative(parent, child);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

function walk(dir, acc = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, acc);
    else if (e.isFile()) acc.push(p);
  }
  return acc;
}

function skipEntry(name) {
  return COPY_SKIP.has(name) || COPY_SKIP_EXT.includes(path.extname(name).toLowerCase());
}

function measure(entryPath) {
  if (!existsSync(entryPath)) return { files: 0, bytes: 0, missing: true };
  const st = statSync(entryPath);
  if (st.isFile()) return { files: 1, bytes: st.size, missing: false };
  let files = 0;
  let bytes = 0;
  const stack = [entryPath];
  while (stack.length) {
    const dir = stack.pop();
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      if (skipEntry(e.name)) continue;
      const p = path.join(dir, e.name);
      if (e.isDirectory()) stack.push(p);
      else {
        const s = statSync(p);
        if (s.isFile()) {
          files += 1;
          bytes += s.size;
        }
      }
    }
  }
  return { files, bytes, missing: false };
}

function copyFilter(src) {
  return !skipEntry(path.basename(src));
}

// 原生程序（tar / powershell）不吃反斜杠，也不吃 MSYS 的 /e/… ——统一正斜杠原生路径
function toPosix(p) {
  return String(p).replace(/\\/g, "/");
}

function sh(cmd, cmdArgs, cwd) {
  const r = spawnSync(cmd, cmdArgs, { cwd, stdio: "inherit", windowsHide: true });
  if (r.error) fail(`启动失败 ${cmd}: ${r.error.message}`);
  return r.status ?? 1;
}

// ---- 各步骤 ---------------------------------------------------------------
function guardOut() {
  if (isInside(OUT, REPO)) fail(`--out 不能落在源仓内部（${OUT}）——会把产物打进 git status`);
  if (!existsSync(OUT)) return { fresh: true };
  const looksLikeBundle = existsSync(path.join(OUT, "app")) || existsSync(path.join(OUT, "runtime"));
  if (!looksLikeBundle && !args.force) {
    fail(`${OUT} 已存在但不是整合包（没有 app/ 或 runtime/）。确认要写进去就加 --force`);
  }
  return { fresh: false };
}

function buildWeb() {
  if (args["skip-web-build"]) {
    log("[=]", "跳过 vite build（--skip-web-build）");
  } else {
    const vite = path.join(WEB, "node_modules", "vite", "bin", "vite.js");
    if (!existsSync(vite)) fail(`web 依赖没装：先在 ${WEB} 跑 npm install --legacy-peer-deps`);
    log("[..]", "vite build（画布前端）…");
    if (sh(process.execPath, [vite, "build", "--logLevel", "warn"], WEB) !== 0) fail("vite build 失败");
  }
  const index = path.join(WEB, "dist", "index.html");
  if (!existsSync(index)) fail("画布构建产物缺失：web/dist/index.html");
  return path.join(WEB, "dist");
}

function runtimeStatus() {
  return RUNTIME_REQUIRED.filter((rel) => !existsSync(path.join(OUT, "runtime", rel)));
}

function resolveDlDir() {
  // 下载缓存（cpython.tar.gz + ffmpeg.zip）可能的位置，按序取第一个齐全的
  const candidates = [
    args["dl-dir"] && path.resolve(args["dl-dir"]),
    path.join(OUT, "runtime", "_dl"),
    process.env.WAN_BUNDLE_DL,
    path.join("E:", "AI", "wan-repro-bundle", "runtime", "_dl"),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (!existsSync(dir)) continue;
    const has = ["cpython.tar.gz", "ffmpeg.zip"].every((n) => {
      const p = path.join(dir, n);
      return existsSync(p) && statSync(p).size > 10_000_000;
    });
    if (has) return dir;
  }
  return null;
}

function bootstrapRuntimeFromCache(runtimeDir) {
  const dl = resolveDlDir();
  if (!dl) {
    fail(
      "runtime 缺件且找不到下载缓存：加 --dl-dir <含 cpython.tar.gz 与 ffmpeg.zip 的目录>，"
      + "或用 --runtime-from <已就位的 runtime 目录>。\n"
      + "  cpython.tar.gz  python-build-standalone CPython 3.11（install_only）\n"
      + "  ffmpeg.zip      BtbN ffmpeg-master-latest-win64-gpl",
    );
  }
  const cpy = path.join(dl, "cpython.tar.gz");
  const ffzip = path.join(dl, "ffmpeg.zip");
  log("[=]", `下载缓存：${dl}`);
  const tmp = path.join(runtimeDir, "_tmp-extract");
  rmSync(tmp, { recursive: true, force: true });
  mkdirSync(tmp, { recursive: true });

  log("[..]", "解包 CPython（python-build-standalone）…");
  // 原生 tar：优先用系统 bsdtar（C:\Windows\System32\tar.exe）。git-bash 的 GNU tar 会把
  // `E:\…` 当成「远程主机 E:」而报 Cannot connect to E: — 传路径一律正斜杠。
  const systemTar = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
  const tarExe = existsSync(systemTar) ? systemTar : "tar";
  if (sh(tarExe, ["-xzf", toPosix(cpy), "-C", toPosix(tmp)]) !== 0) fail("CPython 解包失败（系统 tar 不可用？）");
  const pyDir = readdirSync(tmp, { withFileTypes: true })
    .map((e) => path.join(tmp, e.name))
    .find((p) => existsSync(path.join(p, "python.exe")))
    ?? readdirSync(tmp, { withFileTypes: true })
      .map((e) => path.join(tmp, e.name, "python"))
      .find((p) => existsSync(path.join(p, "python.exe")));
  if (!pyDir) fail("CPython 解包后找不到 python.exe");
  rmSync(path.join(runtimeDir, "python"), { recursive: true, force: true });
  cpSync(pyDir, path.join(runtimeDir, "python"), { recursive: true });

  log("[..]", "解包 ffmpeg/ffprobe（BtbN GPL build）…");
  const ffTmp = path.join(tmp, "_ff");
  const ps = `Expand-Archive -LiteralPath '${toPosix(ffzip).replace(/'/g, "''")}' -DestinationPath '${toPosix(ffTmp).replace(/'/g, "''")}' -Force`;
  if (sh("powershell", ["-NoProfile", "-NonInteractive", "-Command", ps]) !== 0) fail("ffmpeg 解包失败");
  mkdirSync(path.join(runtimeDir, "ffmpeg"), { recursive: true });
  for (const want of ["ffmpeg.exe", "ffprobe.exe"]) {
    const hit = walk(ffTmp).find((p) => path.basename(p).toLowerCase() === want);
    if (!hit) fail(`ffmpeg 包内找不到 ${want}`);
    copyFileSync(hit, path.join(runtimeDir, "ffmpeg", want));
  }
  rmSync(tmp, { recursive: true, force: true });
}

function ensureRuntime() {
  const runtimeDir = path.join(OUT, "runtime");
  const missing = runtimeStatus();
  if (!missing.length) return "reused";
  log("[=]", `runtime 缺件：${missing.join(", ")} → 尝试补齐`);
  if (args["runtime-from"]) {
    const src = path.resolve(args["runtime-from"]);
    if (!existsSync(src)) fail(`--runtime-from 不存在：${src}`);
    for (const rel of missing) {
      const from = path.join(src, rel);
      if (!existsSync(from)) fail(`--runtime-from 里也没有 ${rel}`);
      mkdirSync(path.dirname(path.join(runtimeDir, rel)), { recursive: true });
      copyFileSync(from, path.join(runtimeDir, rel));
    }
    const after = runtimeStatus();
    if (after.length) fail(`runtime 仍缺：${after.join(", ")}`);
    return "copied";
  }
  bootstrapRuntimeFromCache(runtimeDir);
  const after = runtimeStatus();
  if (after.length) fail(`runtime 仍缺：${after.join(", ")}`);
  return "bootstrapped";
}

function syncApp(distDir) {
  const app = path.join(OUT, "app");
  mkdirSync(app, { recursive: true });
  const shipped = [];

  // 代码目录：先删后拷（不 merge——上一轮的文件残留就是「该删的还在」）
  for (const dir of MANIFEST.dirs) {
    const src = path.join(ENGINE, dir);
    if (!existsSync(src)) fail(`引擎目录缺失：wanrepro/${dir}`);
    const dst = path.join(app, dir);
    rmSync(dst, { recursive: true, force: true });
    cpSync(src, dst, { recursive: true, filter: copyFilter });
    shipped.push(`${dir}/`);
  }
  for (const file of MANIFEST.files) {
    const src = path.join(ENGINE, file);
    if (!existsSync(src)) fail(`引擎文件缺失：wanrepro/${file}`);
    copyFileSync(src, path.join(app, file));
    shipped.push(file);
  }
  const webDst = path.join(app, MANIFEST.webDist);
  rmSync(webDst, { recursive: true, force: true });
  cpSync(distDir, webDst, { recursive: true, filter: copyFilter });
  shipped.push(`${MANIFEST.webDist}/`);

  // app/ 下不属于本清单、也不是用户数据的残留 → 只告警不删（可能是在包里手放的东西）
  const owned = new Set([...MANIFEST.dirs, ...MANIFEST.files, MANIFEST.webDist, ...PRESERVE_IN_APP]);
  const unknown = readdirSync(app).filter((n) => !owned.has(n));
  if (unknown.length) {
    log("[!]", `app/ 里有不在清单内、也不在保留名单的条目（未改动）：${unknown.join("、")}`);
    log("[!]", "　　确认是残留就手动删，或加进 MANIFEST / PRESERVE_IN_APP");
  }
  return shipped;
}

function scaffold() {
  const app = path.join(OUT, "app");
  // 拉片产物落点：analyze.py 写 ROOT/analysis/reverse/<id>，目录不存在就写盘失败
  for (const rel of ["analysis/reverse"]) mkdirSync(path.join(app, rel), { recursive: true });
  for (const rel of ["work/projects", "work/uploads", "logs"]) {
    mkdirSync(path.join(OUT, rel), { recursive: true });
  }
  // 包根 config/token：server.py 读它当口令；已有就绝不重写（用户可能改过）
  mkdirSync(path.join(OUT, "config"), { recursive: true });
  const token = path.join(OUT, "config", "token");
  if (!existsSync(token)) writeFileSync(token, randomToken(), "utf8");

  // 渠道预置模板：server.py 读 app/config/channel-config.json（无 example 兜底），
  // 所以模板与真身同目录，部署时复制改名即可
  const appCfg = path.join(app, "config");
  mkdirSync(appCfg, { recursive: true });
  writeFileSync(
    path.join(appCfg, "channel-config.example.json"),
    JSON.stringify(channelExample(), null, 2) + "\n",
    "utf8",
  );
}

function randomToken() {
  return randomBytes(8).toString("hex");
}

function channelExample() {
  return {
    _说明: "复制为 app/config/channel-config.json 并填入 apiKey 后，客户浏览器首次打开画布会自动导入（只导入一次）。产品中立：勿写入产品名。",
    channels: [
      {
        name: "示例·聚鑫SDGF",
        baseUrl: "https://sdapi.jxinai.com",
        apiKey: "填我",
        apiFormat: "sdgf",
        models: [{ name: "按渠道模型清单填" }],
      },
    ],
  };
}

function installManager() {
  const srcBat = path.join(ENGINE, "scripts", "bundle", "启动整合包.bat");
  const srcPs = path.join(ENGINE, "scripts", "bundle", "manage.ps1");
  if (!existsSync(srcBat) || !existsSync(srcPs)) fail("引擎内缺 scripts/bundle/{启动整合包.bat, manage.ps1}");
  copyFileSync(srcBat, path.join(OUT, "启动整合包.bat"));
  // PS 5.1 无 BOM 会按 ANSI 解析中文 → 必须 UTF-8 BOM；统一 CRLF 与现役包一致
  const body = readFileSync(srcPs, "utf8").replace(/\r?\n/g, "\r\n");
  writeFileSync(path.join(OUT, "manage.ps1"), "\uFEFF" + body, "utf8");

  const srcGuide = path.join(ENGINE, "scripts", "bundle", "DEPLOY-GUIDE.md");
  mkdirSync(path.join(OUT, "docs"), { recursive: true });
  if (existsSync(srcGuide)) copyFileSync(srcGuide, path.join(OUT, "docs", "DEPLOY-GUIDE.md"));
  mkdirSync(path.join(OUT, "docs", "licenses"), { recursive: true });
  writeFileSync(
    path.join(OUT, "docs", "licenses", "THIRD-PARTY.md"),
    "| 组件 | 版本 | 许可证 | 说明 |\n|---|---|---|---|\n"
      + "| CPython (python-build-standalone) | 3.11.10 | PSF License | https://python.org/download/ |\n"
      + "| FFmpeg (BtbN GPL build) | master | GPL-3.0 | https://github.com/BtbN/FFmpeg-Builds；本包仅作为独立命令行工具调用，未修改其源码 |\n",
    "utf8",
  );
}

function purgeBytecode() {
  const app = path.join(OUT, "app");
  let removed = 0;
  const stack = [app];
  while (stack.length) {
    const dir = stack.pop();
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (e.name === "__pycache__") {
          rmSync(p, { recursive: true, force: true });
          removed += 1;
        } else {
          stack.push(p);
        }
      } else if (path.extname(p).toLowerCase() === ".pyc") {
        rmSync(p, { force: true });
        removed += 1;
      }
    }
  }
  const left = walk(app).filter((p) => path.extname(p).toLowerCase() === ".pyc").length;
  if (left !== 0) fail(`app/ 内仍有 ${left} 个 .pyc（打包污染）`);
  if (removed) log("[=]", `清掉 ${removed} 处字节码（__pycache__/*.pyc）`);
}

function scanSecrets() {
  const roots = ["app/ui", "app/pipeline", "app/prompts", "app/templates", "app/specs", "app/library", "app/spec", "app/wanrepro", "docs", "config", "app/config"];
  const patterns = [
    [/sk-[A-Za-z0-9]{20,}/, "疑似 API key（sk-…）"],
    [/Bearer\s+[A-Za-z0-9._\-]{20,}/, "疑似 Bearer token"],
  ];
  const hits = [];
  for (const rel of roots) {
    const dir = path.join(OUT, rel);
    if (!existsSync(dir)) continue;
    for (const file of walk(dir)) {
      if ([".pyc", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".zip", ".gz"].includes(path.extname(file).toLowerCase())) continue;
      let text = "";
      try {
        text = readFileSync(file, "utf8");
      } catch {
        continue;
      }
      for (const [re, why] of patterns) {
        if (re.test(text)) hits.push(`${path.relative(OUT, file)} — ${why}`);
      }
    }
  }
  return hits;
}

function selfCheck(shipped) {
  const checks = [
    ...RUNTIME_REQUIRED.map((rel) => [`runtime/${rel}`, existsSync(path.join(OUT, "runtime", rel))]),
    ["app/ui/server.py", existsSync(path.join(OUT, "app", "ui", "server.py"))],
    ["app/analyze.py", existsSync(path.join(OUT, "app", "analyze.py"))],
    ["app/web-dist/index.html", existsSync(path.join(OUT, "app", "web-dist", "index.html"))],
    ["app/pipeline/reverse/analyze.py", existsSync(path.join(OUT, "app", "pipeline", "reverse", "analyze.py"))],
    ["app/pipeline/reverse/render_lapian.py", existsSync(path.join(OUT, "app", "pipeline", "reverse", "render_lapian.py"))],
    ["app/pipeline/gen/wan_video.py", existsSync(path.join(OUT, "app", "pipeline", "gen", "wan_video.py"))],
    ["app/pipeline/gen/wan_submit.py", existsSync(path.join(OUT, "app", "pipeline", "gen", "wan_submit.py"))],
    ["app/pipeline/_nowindow/sitecustomize.py", existsSync(path.join(OUT, "app", "pipeline", "_nowindow", "sitecustomize.py"))],
    ["app/specs/jimeng-lapian.md", existsSync(path.join(OUT, "app", "specs", "jimeng-lapian.md"))],
    ["app/templates/seedance-flow.md", existsSync(path.join(OUT, "app", "templates", "seedance-flow.md"))],
    ["app/contracts/camera-contract.md", existsSync(path.join(OUT, "app", "contracts", "camera-contract.md"))],
    // 2026-11-08：docs/ 是模板字符串路径引用的数据（seedance-flow 的原文依据），漏了=首渲 seedance-flow 才炸
    ["app/docs/原提示词-单段流式.md", existsSync(path.join(OUT, "app", "docs", "原提示词-单段流式.md"))],
    ["app/analysis/reverse/", statSync(path.join(OUT, "app", "analysis", "reverse"), { throwIfNoEntry: false })?.isDirectory() === true],
    ["启动整合包.bat", existsSync(path.join(OUT, "启动整合包.bat"))],
    ["manage.ps1", existsSync(path.join(OUT, "manage.ps1"))],
    ["config/token", existsSync(path.join(OUT, "config", "token"))],
    ["docs/DEPLOY-GUIDE.md", existsSync(path.join(OUT, "docs", "DEPLOY-GUIDE.md"))],
  ];
  for (const [label, ok] of checks) console.log(ok ? `  [√] ${label}` : `  [x] MISSING ${label}`);
  return checks.every(([, ok]) => ok);
}

function packageInfo(shipped, runtimeMode) {
  const files = walk(OUT).filter((p) => !p.includes(`${path.sep}runtime${path.sep}_dl${path.sep}`));
  const bytes = files.reduce((n, p) => n + statSync(p).size, 0);
  const info = {
    product: "wan-repro-bundle",
    version: readText(path.join(REPO, "VERSION"), "unknown").trim(),
    commit_hash: git(["rev-parse", "HEAD"]),
    git_describe: git(["describe", "--tags", "--always", "--dirty"]),
    branch: git(["rev-parse", "--abbrev-ref", "HEAD"]),
    built_at: new Date().toISOString(),
    built_by: "npm run bundle（fork scripts/bundle.mjs）",
    source_repo: REPO,
    runtime_mode: runtimeMode,
    runtime_download_cache: existsSync(path.join(OUT, "runtime", "_dl")) ? "runtime/_dl（构建缓存，交付可删）" : null,
    shipped_code: shipped,
    preserved_user_data: PRESERVE_IN_APP.filter((n) => existsSync(path.join(OUT, "app", n))),
    file_count: files.length,
    size_mb: Math.round(bytes / 1048576),
    web_build: !args["skip-web-build"],
  };
  writeFileSync(path.join(OUT, "PACKAGE-INFO.json"), JSON.stringify(info, null, 2) + "\n", "utf8");
  return info;
}

function readText(p, fallback) {
  try {
    return readFileSync(p, "utf8");
  } catch {
    return fallback;
  }
}

function git(gitArgs) {
  const r = spawnSync("git", gitArgs, { cwd: REPO, encoding: "utf8", windowsHide: true });
  return r.status === 0 ? r.stdout.trim() : null;
}

function looksLikeService() {
  // 8776 已在跑 = 包里是旧代码在内存里，打完必须重启（不替用户停服务）
  const r = spawnSync(
    process.execPath,
    ["-e", "const s=require('net').connect(8776,'127.0.0.1');s.on('connect',()=>{s.destroy();process.exit(0)});s.on('error',()=>process.exit(1));s.setTimeout(1500,()=>{s.destroy();process.exit(1)})"],
    { windowsHide: true },
  );
  return r.status === 0;
}

// ---- 主流程 ---------------------------------------------------------------
function main() {
  console.log(`\n=== wan-repro 整合包 · 打包器${DRY ? "（dry-run：只算不写）" : ""} ===`);
  console.log(`源仓: ${REPO}`);
  console.log(`产物: ${OUT}\n`);

  if (!existsSync(ENGINE)) fail("找不到引擎目录 wanrepro/（本脚本必须在 fork 仓根运行）");
  guardOut();

  // 1) 清单预演（dry-run 也走这一步）
  const distDir = path.join(WEB, "dist");
  const plan = [];
  for (const dir of MANIFEST.dirs) plan.push([`wanrepro/${dir}/`, measure(path.join(ENGINE, dir))]);
  for (const file of MANIFEST.files) plan.push([`wanrepro/${file}`, measure(path.join(ENGINE, file))]);
  plan.push([`web/dist/（vite build 产物）`, measure(distDir)]);

  console.log("— 随包清单 —");
  let planFiles = 0;
  let planBytes = 0;
  for (const [label, m] of plan) {
    planFiles += m.files;
    planBytes += m.bytes;
    console.log(`  ${m.missing ? "[!] 待构建" : "[√]"} ${label}  ${m.files} 文件  ${(m.bytes / 1048576).toFixed(1)}MB`);
  }
  console.log(`  合计 ${planFiles} 文件 / ${(planBytes / 1048576).toFixed(1)}MB`);
  console.log("— 保留（用户数据，不覆盖不删除）—");
  console.log(`  app/${PRESERVE_IN_APP.join("  app/")}`);
  console.log("  work/  logs/  runtime/  config/token\n");

  const missingRuntime = runtimeStatus();
  console.log(`— runtime — ${missingRuntime.length ? `缺件：${missingRuntime.join(", ")}` : "已就位（复用）"}\n`);

  if (DRY) {
    const missingSrc = plan.filter(([, m]) => m.missing).map(([label]) => label);
    if (missingSrc.length) log("[!]", `源缺件（真跑前会先补建）：${missingSrc.join("、")}`);
    log("[√]", "dry-run 通过：清单已算出，未写盘、未构建");
    return;
  }

  // 2) 画布构建 → 3) runtime → 4) app 代码 → 5) 骨架 → 6) 管理器
  const builtDist = buildWeb();
  const runtimeMode = ensureRuntime();
  log("[√]", `runtime ${runtimeMode === "reused" ? "复用" : runtimeMode === "copied" ? "从 --runtime-from 补入" : "由缓存解包"}`);
  const shipped = syncApp(builtDist);
  log("[√]", `app/ 代码就位（${shipped.length} 项，含 web-dist）`);
  scaffold();
  log("[√]", "app/analysis/reverse · work/ · logs/ · config/token 就位");
  installManager();
  log("[√]", "manage.ps1（UTF-8 BOM+CRLF）· 启动整合包.bat · docs/ 就位");
  purgeBytecode();

  // 7) 自检
  console.log("\n— 自检 —");
  const ok = selfCheck(shipped);
  const leaks = scanSecrets();
  if (leaks.length) {
    console.log("\n— 密钥扫描 —");
    for (const h of leaks) console.log(`  [x] ${h}`);
  }

  // 8) 产物信息
  const info = packageInfo(shipped, runtimeMode);
  console.log(`\nPACKAGE-INFO.json：${info.version} · ${info.commit_hash ?? "?"} · ${info.file_count} 文件 · ${info.size_mb}MB`);
  if (looksLikeService()) {
    log("[!]", "8776 上现在有服务在跑——包里已是新代码但内存里还是旧的，打完在管理菜单按 [3] 重启");
  }
  if (!ok || leaks.length) {
    log("[x]", `自检未通过${leaks.length ? "（含密钥扫描命中）" : ""}`);
    process.exit(1);
  }
  log("[√]", `打包完成：${OUT}`);
  log("[√]", "双击 启动整合包.bat → [1] 启动服务 → http://127.0.0.1:8776");
}

main();
