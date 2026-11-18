/**
 * S3.7 前端探针：渠道 baseUrl 自带路径（如 `/v1`）时代理 subpath 必须裁剪为「相对 baseUrl」。
 *
 * esbuild 打包真模块 src/services/api/channel-proxy.ts，node 里 shim window 后跑行为断言：
 *   node scripts/probe-s3-7-proxy-url.mjs
 * 任一断言不成立 → 退出码 1。
 *
 * 覆盖（方案 §三.3 / v1.1 §八 R4）：
 *  ① baseUrl 无路径 + path /models → /api/proxy/<cid>/v1/models（既有行为不变）
 *  ② baseUrl https://ollama.com/v1 + /models → /api/proxy/<cid>/models（本缺陷回归）
 *  ③ baseUrl https://host/openai/v1 + /models → /api/proxy/<cid>/models（多段前缀裁剪）
 *  ④ 无 channelId → 抛错（既有行为不回归）
 *  ⑤ channelMediaUrl：同源无凭据 media（baseUrl 带 /v1）→ viaProxy=true 且路径已裁剪
 *  ⑥ baseUrl 非法/无协议（"" / "ollama.com/v1"）→ 不裁剪、不抛错
 */
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."); // web/

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

const workdir = mkdtempSync(path.join(tmpdir(), "probe-s3-7-"));

// ---- shim：channel-proxy 只需要 window.location / i18n / useConfigStore ----
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
const mem = new Map();
globalThis.window = globalThis;
globalThis.localStorage = {
    getItem: (key) => (mem.has(key) ? mem.get(key) : null),
    setItem: (key, value) => mem.set(key, String(value)),
    removeItem: (key) => mem.delete(key),
    clear: () => mem.clear(),
    key: (index) => Array.from(mem.keys())[index] ?? null,
    get length() { return mem.size; },
};
globalThis.location = { origin: "http://localhost:3000", href: "http://localhost:3000/" };
`,
);

let entry = `import "./shim.mjs";\n`;
for (const rel of ["src/services/api/channel-proxy.ts", "src/stores/use-config-store.ts"]) {
    entry += `export * as ${rel.replace(/[^a-zA-Z0-9]/g, "_")} from ${JSON.stringify(path.join(root, rel).replace(/\\/g, "/"))};\n`;
}
writeFileSync(path.join(workdir, "entry.ts"), entry);

const result = await esbuild.build({
    entryPoints: [path.join(workdir, "entry.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    alias: { "@": path.join(root, "src") },
    define: { "process.env.NODE_ENV": '"test"' },
    logLevel: "silent",
});
const outFile = path.join(workdir, "bundle.mjs");
writeFileSync(outFile, result.outputFiles[0].text);

const bundle = await import(pathToFileURL(outFile).href);
const proxy = bundle.src_services_api_channel_proxy_ts;
const store = bundle.src_stores_use_config_store_ts;

// ---- 断言工具 ----
let passed = 0;
const failures = [];
function equal(name, actual, expected) {
    if (actual === expected) {
        passed += 1;
        console.log(`  ok ${name} -> ${actual}`);
        return;
    }
    failures.push(`${name} —— 实际 ${actual} / 期望 ${expected}`);
}

// 服务端模式 + 固定渠道 store（channelProxyUrl 走 channelIdOf 的注入 id，不依赖 model 解引用）
store.useConfigStore.setState({
    config: {
        ...store.useConfigStore.getState().config,
        channelMode: "remote",
        baseUrl: "https://api.jxinapi.com",
        apiKey: "sk-test",
    },
});
const CFG_BASE = { channelId: "cid", channelMode: "remote" };

// ① baseUrl 无路径：subpath 原样（保留 buildApiAbsoluteUrl 的 /v1）
equal("① no-path baseUrl + /models", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "https://api.jxinapi.com" }, "https://api.jxinapi.com/v1/models"), "/api/proxy/cid/v1/models");

// ② 本缺陷：baseUrl 带 /v1 → 裁掉
equal("② ollama /v1 + /models", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "https://ollama.com/v1" }, "https://ollama.com/v1/models"), "/api/proxy/cid/models");

// ③ 多段 base 路径
equal("③ /openai/v1 + /models", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "https://host.example/openai/v1" }, "https://host.example/openai/v1/models"), "/api/proxy/cid/models");

// ④ 无 channelId → 抛错
let threw = "";
try {
    proxy.channelProxyUrl({ channelMode: "remote", baseUrl: "https://ollama.com/v1" }, "https://ollama.com/v1/models");
} catch (error) {
    threw = String(error && error.message);
}
if (threw) {
    passed += 1;
    console.log(`  ok ④ no channelId throws (${threw})`);
} else {
    failures.push("④ no channelId —— 未抛错");
}

// ⑤ 成品下载链路：同源、无凭据、baseUrl 带 /v1 → 走代理且路径已裁剪
const media = proxy.channelMediaUrl({ ...CFG_BASE, baseUrl: "https://host.example/v1" }, "https://host.example/v1/files/a.mp4");
if (media.viaProxy === true && media.url === "/api/proxy/cid/files/a.mp4") {
    passed += 1;
    console.log(`  ok ⑤ channelMediaUrl -> viaProxy=true, ${media.url}`);
} else {
    failures.push(`⑤ channelMediaUrl —— 实际 ${JSON.stringify(media)}`);
}

// ⑥ 非法/无协议 baseUrl → 不裁剪、不抛错（退化为原 pathname 行为）
equal("⑥a baseUrl=''", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "" }, "http://localhost:3000/v1/models"), "/api/proxy/cid/v1/models");
equal("⑥b baseUrl='ollama.com/v1' (no scheme)", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "ollama.com/v1" }, "http://localhost:3000/v1/models"), "/api/proxy/cid/v1/models");

// gemini 形态：/v1beta 不被 /v1 误裁（startsWith 按 / 前缀匹配，天然安全）
equal("extra gemini /v1beta not stripped", proxy.channelProxyUrl({ ...CFG_BASE, baseUrl: "https://host.example/v1" }, "https://host.example/v1beta/models"), "/api/proxy/cid/v1beta/models");

// ⑦ config.baseUrl 为空时绝不回落 store：回落会让不同调用点语义不一致，
//    resolveModelRequestConfig 等调用方已自带 baseUrl。store 里残留别个渠道的 baseUrl 时，
//    空 baseUrl 应走 buildApiAbsoluteUrl 补 /v1 且不裁剪。
const prevBase = store.useConfigStore.getState().config.baseUrl;
store.useConfigStore.setState({
    config: { ...store.useConfigStore.getState().config, baseUrl: "https://store-only.example/v1" },
});
equal("⑦ empty baseUrl ignores store /v1", proxy.channelApiUrl({ ...CFG_BASE, model: "cid::m", baseUrl: "" }, "/models"), "/api/proxy/cid/v1/models");
store.useConfigStore.setState({
    config: { ...store.useConfigStore.getState().config, baseUrl: prevBase },
});

console.log(`\nS3.7 probe: ${passed} passed / ${failures.length} failed`);
if (failures.length) {
    for (const line of failures) console.error(`FAIL ${line}`);
    process.exit(1);
}
