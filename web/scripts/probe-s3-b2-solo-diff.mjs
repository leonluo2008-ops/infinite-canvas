/**
 * S3-B2.5 差分探针：**单人模式零改动**（HEAD 差分逐字节口径）。
 *
 * 做法（不是「读代码看没变」，是**真跑两份代码比字节**）：
 *  1. `git archive HEAD web/src` 取出基线代码树；
 *  2. 同一份脚本化场景（建项目 → 改内容/视口 → 导入 → 显式 flush → 删除并留墓碑 → 再 flush）
 *     分别在「基线树」和「工作区树」上跑（各自独立 node 进程，固定时钟 + 固定随机源保证可复现）；
 *  3. 对比两次落盘的**原始字节**（`infinite-canvas:canvas_store` 的字符串），以及载荷键集、lastFlush 形状。
 *
 * 任一不一致 → 退出码 1。单人模式（无登录态）下画布落盘必须与 HEAD 完全一样。
 *
 *   cd web && node scripts/probe-s3-b2-solo-diff.mjs
 */
import { execSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."); // web/
const repo = path.resolve(root, "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-solo-diff-"));

async function loadEsbuild() {
    try {
        return await import("esbuild");
    } catch {
        const { readdirSync } = await import("node:fs");
        const store = path.join(root, "node_modules", ".pnpm");
        const dir = readdirSync(store).find((name) => /^esbuild@/.test(name));
        if (!dir) throw new Error("esbuild 不可用：请在 web/ 下执行 pnpm install");
        return import(pathToFileURL(path.join(store, dir, "node_modules", "esbuild", "lib", "main.js")).href);
    }
}
const esbuild = await loadEsbuild();

/** Windows 上交给 native 程序（git/tar）的路径必须是正斜杠：反斜杠会被 shell 吃掉。 */
const fwd = (value) => value.replace(/\\/g, "/");

// ---- 1. 基线树（HEAD 的 web/src）----
const baseDir = path.join(workdir, "base");
mkdirSync(baseDir, { recursive: true });
// 逐个 blob 取出（不依赖 tar：本机 GNU tar 要求 MSYS 路径，而 git 要求 native 路径）
const headFiles = execSync(`git -C "${fwd(repo)}" ls-tree -r --name-only HEAD web/src`, { encoding: "utf8" })
    .trim()
    .split("\n")
    .filter(Boolean);
for (const file of headFiles) {
    const relative = file.replace(/^web\//, "");
    const target = path.join(baseDir, relative);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, execSync(`git -C "${fwd(repo)}" show "HEAD:${file}"`, { maxBuffer: 64 * 1024 * 1024 }));
}

const variants = [
    // 上面按 "web/" 前缀剥离写入，所以基线树根是 baseDir/src
    { name: "head", src: path.join(baseDir, "src") },
    { name: "workspace", src: path.join(root, "src") },
];

// ---- 2. 运行时垫片 + 固定脚本（两份代码跑同一份）----
const runnerPath = path.join(workdir, "runner.mjs");
writeFileSync(
    runnerPath,
    `import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const FIXED_NOW = 1731000000000;
const store = new Map();
globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
    key: (index) => Array.from(store.keys())[index] ?? null,
    get length() { return store.size; },
};
// 固定时钟：createProject/importProject/updateProject 里的 new Date() 全部落到同一毫秒
const FixedDate = class extends Date {
    constructor(...args) { if (args.length === 0) super(FIXED_NOW); else super(...args); }
    static now() { return FIXED_NOW; }
};
globalThis.Date = FixedDate;
// 固定随机源：nanoid 生成的 id 可复现（否则两次跑必然不同字节）；带计数器，保证多次调用 id 不撞
let randomCalls = 0;
Object.defineProperty(globalThis, "crypto", {
    value: {
        getRandomValues: (arr) => {
            randomCalls += 1;
            for (let i = 0; i < arr.length; i += 1) arr[i] = (randomCalls * 37 + i * 11) % 256;
            return arr;
        },
    },
    configurable: true,
    writable: true,
});
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.document = { visibilityState: "visible", addEventListener: () => {}, removeEventListener: () => {} };

const mod = await import(pathToFileURL(process.env.BUNDLE).href);
const storeModule = mod.store;
const useCanvasStore = storeModule.useCanvasStore;
const KEY = storeModule.CANVAS_STORE_KEY || "infinite-canvas:canvas_store";
const state = () => useCanvasStore.getState();

// ---- 同一份单人模式脚本（无任何登录态 / 无网络）----
const first = state().createProject("单人项目甲");
state().updateProject(first, { nodes: [{ id: "n1", type: "text" }], viewport: { x: 12, y: 34, k: 1.5 } });
const second = state().importProject({ title: "导入项目乙", nodes: [{ id: "m1", type: "text" }] });
await state().flushProjects("solo-diff-1");
state().renameProject(second, "改名后的乙");
await state().flushProjects("solo-diff-2");
state().deleteProjects([first]);
await state().flushProjects("solo-diff-3");

// localforage 的 localStorage 兜底驱动会给键加 "<name>/<storeName>/" 前缀，按后缀找真正那份载荷
const entryKey = Array.from(store.keys()).find((key) => key.endsWith(KEY)) || "";
const raw = entryKey ? store.get(entryKey) : "";
writeFileSync(process.env.OUT, JSON.stringify({
    // 整份存储（键 + 值）一起比：字节级差分口径
    entries: Array.from(store.entries()).sort((a, b) => (a[0] < b[0] ? -1 : 1)),
    entryCount: store.size,
    entryKey,
    rawBytes: raw,
    rawLength: raw.length,
    payloadStateKeys: (() => {
        if (!raw) return [];
        try {
            // localforage 会把字符串值再 JSON 序列化一层：解到对象为止
            let payload = JSON.parse(raw);
            if (typeof payload === "string") payload = JSON.parse(payload);
            return payload && payload.state ? Object.keys(payload.state).sort() : [];
        } catch (error) {
            return ["UNPARSABLE:" + String(error && error.message).slice(0, 60)];
        }
    })(),
    rawHead: raw.slice(0, 120),
    storeStateKeys: Object.keys(state()).sort(),
    lastFlushKeys: Object.keys(state().lastFlush || {}).sort(),
}, null, 2));
`,
);

// ---- 3. 各变体：打包 → 独立进程跑 → 收集输出 ----
const results = {};
for (const variant of variants) {
    const entryPath = path.join(workdir, `entry-${variant.name}.ts`);
    const storeFile = fwd(path.join(variant.src, "stores", "canvas", "use-canvas-store.ts"));
    writeFileSync(entryPath, `export * as store from ${JSON.stringify(storeFile)};\n`);
    const built = await esbuild.build({
        entryPoints: [entryPath],
        bundle: true,
        format: "esm",
        platform: "browser",
        write: false,
        alias: { "@": variant.src },
        nodePaths: [path.join(root, "node_modules")],
        define: { "process.env.NODE_ENV": '"test"' },
        logLevel: "silent",
    });
    const bundlePath = path.join(workdir, `bundle-${variant.name}.mjs`);
    writeFileSync(bundlePath, built.outputFiles[0].text);
    const outPath = path.join(workdir, `out-${variant.name}.json`);
    execSync(`"${process.execPath}" "${runnerPath}"`, {
        env: { ...process.env, BUNDLE: bundlePath, OUT: outPath },
        stdio: ["ignore", "ignore", "inherit"],
    });
    results[variant.name] = JSON.parse(readFileSync(outPath, "utf8"));
}

// ---- 4. 断言 ----
const failures = [];
let passed = 0;
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}

const head = results.head;
const work = results.workspace;
check("单人：落盘字节与 HEAD **逐字节完全一致**", head.rawBytes === work.rawBytes, `head=${head.rawLength}B workspace=${work.rawLength}B`);
check("单人：整份存储（键 + 值）与 HEAD 完全一致", JSON.stringify(head.entries) === JSON.stringify(work.entries), `head=${head.entryCount} 键 / workspace=${work.entryCount} 键`);
check("单人：只写了一份载荷键（没有 owner/dirty 之类的额外键）", work.entryCount === 1 && work.entryKey.endsWith("infinite-canvas:canvas_store"), `${work.entryCount} 键：${JSON.stringify(work.entryKey)}`);
check("单人：载荷 state 只有 projects/deletedProjects", JSON.stringify(work.payloadStateKeys) === JSON.stringify(["deletedProjects", "projects"]), JSON.stringify(work.payloadStateKeys) + " | head=" + work.rawHead);
check("单人：store 状态键集与 HEAD 一致（没有多出同步字段）", JSON.stringify(work.storeStateKeys) === JSON.stringify(head.storeStateKeys), `head=${JSON.stringify(head.storeStateKeys)} work=${JSON.stringify(work.storeStateKeys)}`);
check("单人：lastFlush 形状与 HEAD 一致", JSON.stringify(work.lastFlushKeys) === JSON.stringify(head.lastFlushKeys), `head=${JSON.stringify(head.lastFlushKeys)} work=${JSON.stringify(work.lastFlushKeys)}`);
check("单人：确实写到了盘（不是两边都空）", head.rawLength > 0 && work.rawLength > 0, `head=${head.rawLength} work=${work.rawLength}`);

console.log(`=== S3-B2.5 单人零改动差分探针（HEAD vs 工作区）：${passed} passed / ${failures.length} failed ===`);
console.log(`HEAD 载荷 ${head.rawLength} 字节 / 工作区 ${work.rawLength} 字节`);
for (const item of failures) console.log(`FAIL | ${item}`);
console.log(`EXIT_CODE=${failures.length ? 1 : 0}`);
process.exit(failures.length ? 1 : 0);
