/**
 * S2-B3.5 IMPL-1 探针：**remote 模式成片地址不再拼 masked key、不再裸连上游**（免费，不连服务端）。
 *
 *   node scripts/probe-s2-b35-video-result-url.mjs
 *
 * esbuild 打包真模块 src/services/api/video.ts（axios 用 stub 替换，i18n/store 走真模块），
 * 直接驱动真实轮询函数 `pollVideoGenerationTask`（gemini / openai / sdgf 三条路径），
 * 断言它交给下载层的地址：
 *   * 服务端模式（channelMode=remote，apiKey 只有掩码 `***abcd`）：地址里**零 key**，
 *     同源成品改走 `/api/proxy/<channelId>/…`（由服务端注入 key）；
 *   * 上游签发的一次性直链（自含真凭据）/ 异源 CDN：原样直连（代转发会丢签名 / 打错 host）；
 *   * 本地模式：与基线字节级一致（真 key 照拼、走 withLocalProxy）。
 */
import { mkdtempSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function loadEsbuild() {
    try {
        return await import("esbuild");
    } catch {
        const store = path.join(root, "node_modules", ".pnpm");
        const dir = readdirSync(store).find((name) => /^esbuild@/.test(name));
        if (!dir) throw new Error("esbuild 不可用：请在 web/ 下执行 pnpm install");
        return import(pathToFileURL(path.join(store, dir, "node_modules", "esbuild", "lib", "main.js")).href);
    }
}
const esbuild = await loadEsbuild();

const workdir = mkdtempSync(path.join(tmpdir(), "probe-b35-video-"));

// ---- shim：必须先于目标模块求值 ------------------------------------------------------
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
globalThis.localStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
    clear: () => storage.clear(),
    key: (index) => Array.from(storage.keys())[index] ?? null,
    get length() { return storage.size; },
};
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.location = { origin: "http://localhost:3000", href: "http://localhost:3000/" };
globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
`,
);

// ---- axios stub：记录请求地址，按用例编排响应 ----------------------------------------
writeFileSync(
    path.join(workdir, "axios-stub.mjs"),
    `export const calls = [];
let handler = () => { throw new Error("no handler"); };
export function setHandler(next) { handler = next; calls.length = 0; }
async function request(method, url, a, b) {
    calls.push({ method, url });
    return handler(url, method === "post" ? a : b);
}
const axios = {
    get: (url, opts) => request("get", url, null, opts),
    post: (url, body, opts) => request("post", url, body, opts),
    put: (url, body, opts) => request("put", url, body, opts),
    delete: (url, opts) => request("delete", url, null, opts),
    isCancel: () => false,
};
export default axios;
export const get = axios.get;
export const post = axios.post;
export const put = axios.put;
export const del = axios.delete;
`,
);

writeFileSync(
    path.join(workdir, "entry.ts"),
    `import "./shim.mjs";
export { setHandler, calls } from "./axios-stub.mjs";
export * as video from ${JSON.stringify(path.join(root, "src/services/api/video.ts").replace(/\\/g, "/"))};
export * as store from ${JSON.stringify(path.join(root, "src/stores/use-config-store.ts").replace(/\\/g, "/"))};
`,
);

const built = await esbuild.build({
    entryPoints: [path.join(workdir, "entry.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    alias: { "@": path.join(root, "src"), axios: path.join(workdir, "axios-stub.mjs") },
    define: { "process.env.NODE_ENV": '"test"' },
    logLevel: "silent",
});
const outFile = path.join(workdir, "bundle.mjs");
writeFileSync(outFile, built.outputFiles[0].text);

const bundle = await import(pathToFileURL(outFile).href);
const video = bundle.video;
const store = bundle.store;

// ---- 断言工具 -----------------------------------------------------------------------
let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}
function equal(name, actual, expected) {
    check(name, JSON.stringify(actual) === JSON.stringify(expected), `实际 ${JSON.stringify(actual)} / 期望 ${JSON.stringify(expected)}`);
}

const warnings = [];
const originalWarn = console.warn;
console.warn = (...args) => warnings.push(args.map(String).join(" "));
const originalInfo = console.info;
console.info = () => {};

/** 统一编排：状态查询返回给定 payload，成片下载一律失败（这样结果里留下的是「交给下载层的地址」）。 */
function serveStatus(payload) {
    bundle.setHandler(async (url, opts) => {
        if (opts && opts.responseType === "blob") throw new Error("download-blocked-in-probe");
        return { data: payload };
    });
}
function lastUrl() {
    const list = bundle.calls;
    return list.length ? list[list.length - 1].url : "";
}
function requestedUrls() {
    return bundle.calls.map((call) => call.url);
}

const MASKED = "***abcd";
const REAL_KEY = "sk-local-key";
/** pollVideoGenerationTask 会经 resolveModelRequestConfig 回到渠道里取 key —— 所以 key 必须挂在渠道上。 */
const channelWith = (apiKey) => ({ id: "ch-1", name: "渠道一", baseUrl: "https://api.example.com", apiKey, apiFormat: "openai", models: [{ name: "gpt-image-2", capability: "image" }] });
const remote = (extra = {}) => ({ ...store.defaultConfig, channelMode: "remote", channelId: "ch-1", model: "ch-1::gpt-image-2", baseUrl: "https://api.example.com", apiKey: MASKED, channels: [channelWith(MASKED)], ...extra });
const local = (extra = {}) => ({ ...store.defaultConfig, channelMode: "local", channelId: "ch-1", model: "ch-1::gpt-image-2", baseUrl: "https://api.example.com", apiKey: REAL_KEY, channels: [channelWith(REAL_KEY)], ...extra });

// ---------------------------------------------------------------------------------
// 1. gemini：remote 模式下 uri 不带 key → 改走代转发，前端零 key
// ---------------------------------------------------------------------------------
serveStatus({
    done: true,
    response: { generateVideoResponse: { generatedSamples: [{ video: { uri: "https://api.example.com/v1beta/files/abc:download?alt=media" } }] } },
});
let state = await video.pollVideoGenerationTask(remote(), { id: "operations/xyz", provider: "gemini", model: "veo-3" });
const geminiRemoteUrl = state.status === "completed" ? state.result.url || "" : "";
equal("1. gemini remote 成品走 /api/proxy 且不带 key", geminiRemoteUrl, "/api/proxy/ch-1/v1beta/files/abc:download?alt=media");
check("1. gemini remote 全程零 masked key", requestedUrls().every((url) => !url.includes(MASKED) && !url.includes("***")), JSON.stringify(requestedUrls()));

// 1b. 上游回了个自带**掩码** key 的 uri（本机 masked 回写造成的脏数据）→ 掩码被剥掉，仍走代理
serveStatus({
    done: true,
    response: { generateVideoResponse: { generatedSamples: [{ video: { uri: "https://api.example.com/v1beta/files/def:download?alt=media&key=***abcd" } }] } },
});
state = await video.pollVideoGenerationTask(remote(), { id: "operations/xyz2", provider: "gemini", model: "veo-3" });
const geminiMaskedUrl = state.status === "completed" ? state.result.url || "" : "";
equal("1b. 掩码 key 被剥掉（不发给上游）", geminiMaskedUrl, "/api/proxy/ch-1/v1beta/files/def:download?alt=media");
check("1b. 请求地址不含掩码", requestedUrls().every((url) => !url.includes("***")), JSON.stringify(requestedUrls()));

// 1c. 上游签发的一次性直链（自含真凭据）→ 原样直连，不再叠加、也不经代理
serveStatus({
    done: true,
    response: { generateVideoResponse: { generatedSamples: [{ video: { uri: "https://cdn.example.net/out/abc.mp4?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA" } }] } },
});
state = await video.pollVideoGenerationTask(remote(), { id: "operations/xyz3", provider: "gemini", model: "veo-3" });
equal("1c. 自含凭据直链原样直连（不经代理、不叠 key）", state.status === "completed" ? state.result.url : "", "https://cdn.example.net/out/abc.mp4?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA");

// 1d. 本地模式：与基线一致（真 key 照拼）
serveStatus({
    done: true,
    response: { generateVideoResponse: { generatedSamples: [{ video: { uri: "https://api.example.com/v1beta/files/ghi:download?alt=media" } }] } },
});
state = await video.pollVideoGenerationTask(local(), { id: "operations/xyz4", provider: "gemini", model: "veo-3" });
equal("1d. 本地模式仍拼本地真 key（基线不变）", state.status === "completed" ? state.result.url : "", "https://api.example.com/v1beta/files/ghi:download?alt=media&key=sk-local-key");

// 1e. 本地模式却只有掩码 key（服务端视图漏到本地态）→ 不拼 key，也不发掩码；留告警
serveStatus({
    done: true,
    response: { generateVideoResponse: { generatedSamples: [{ video: { uri: "https://api.example.com/v1beta/files/jkl:download?alt=media" } }] } },
});
warnings.length = 0;
state = await video.pollVideoGenerationTask(local({ channels: [channelWith(MASKED)] }), { id: "operations/xyz5", provider: "gemini", model: "veo-3" });
equal("1e. 本地态掩码 key 不拼进地址", state.status === "completed" ? state.result.url : "", "https://api.example.com/v1beta/files/jkl:download?alt=media");
check("1e. 掩码 key 有告警", warnings.some((line) => line.includes("掩码值")), JSON.stringify(warnings));

// ---------------------------------------------------------------------------------
// 2. openai 型：remote 同源成品 → 代转发；本地 → 原样
// ---------------------------------------------------------------------------------
serveStatus({ id: "v-1", status: "completed", url: "https://api.example.com/v1/videos/v-1/out.mp4" });
state = await video.pollVideoGenerationTask(remote({ apiFormat: "openai" }), { id: "v-1", provider: "openai", model: "sora-2" });
equal("2. openai remote 同源成品走 /api/proxy", state.status === "completed" ? state.result.url : "", "/api/proxy/ch-1/v1/videos/v-1/out.mp4");

serveStatus({ id: "v-2", status: "completed", url: "https://files.cdn.example/out/v-2.mp4?token=tok-123" });
state = await video.pollVideoGenerationTask(remote({ apiFormat: "openai" }), { id: "v-2", provider: "openai", model: "sora-2" });
equal("2b. 异源带 token 直链原样直连", state.status === "completed" ? state.result.url : "", "https://files.cdn.example/out/v-2.mp4?token=tok-123");

serveStatus({ id: "v-3", status: "completed", url: "https://api.example.com/v1/videos/v-3/out.mp4" });
state = await video.pollVideoGenerationTask(local({ apiFormat: "openai" }), { id: "v-3", provider: "openai", model: "sora-2" });
equal("2c. 本地模式成品地址与基线一致", state.status === "completed" ? state.result.url : "", "https://api.example.com/v1/videos/v-3/out.mp4");

// ---------------------------------------------------------------------------------
// 3. sdgf 型：remote 同源 metadata.url → 代转发（此前是裸连上游）
// ---------------------------------------------------------------------------------
serveStatus({ status: "completed", metadata: { url: "https://api.example.com/v1/videos/t-1/content" } });
state = await video.pollVideoGenerationTask(remote({ apiFormat: "sdgf" }), { id: "t-1", provider: "sdgf", model: "sd-2" });
equal("3. sdgf remote 同源成品走 /api/proxy", state.status === "completed" ? state.result.url : "", "/api/proxy/ch-1/v1/videos/t-1/content");

serveStatus({ status: "completed", metadata: { url: "https://oss.example.org/out/t-2.mp4?key=real-oss-key" } });
state = await video.pollVideoGenerationTask(remote({ apiFormat: "sdgf" }), { id: "t-2", provider: "sdgf", model: "sd-2" });
equal("3b. sdgf 异源自含凭据直链原样直连", state.status === "completed" ? state.result.url : "", "https://oss.example.org/out/t-2.mp4?key=real-oss-key");

// ---------------------------------------------------------------------------------
// 4. 全局：任何用例的请求里都不出现 masked key（「不把掩码发给上游」的兜底断言）
// ---------------------------------------------------------------------------------
check("4. 全部请求地址零掩码 key", requestedUrls().every((url) => !url.includes(MASKED)), JSON.stringify(requestedUrls()));

console.warn = originalWarn;
console.info = originalInfo;

console.log(`=== S2-B3.5 成片地址探针（IMPL-1）：${passed} passed / ${failures.length} failed ===`);
for (const item of failures) console.log(`FAIL | ${item}`);
console.log(`EXIT_CODE=${failures.length ? 1 : 0}`);
process.exit(failures.length ? 1 : 0);
