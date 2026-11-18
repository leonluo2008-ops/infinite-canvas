/**
 * S2-B3 渠道服务端持久化探针（免费，不连服务端、不花钱）。
 *
 * esbuild 打包真模块 src/services/channel-sync.ts（含 zustand persist 依赖链），
 * node 里 shim window/localStorage/fetch 后跑行为断言：
 *   node scripts/probe-s2-b3-channel-sync.mjs
 * 任一断言不成立 → 退出码 1。
 *
 * 覆盖（对应任务书 v1.3 §二 B3.1-B3.5 与 V6/V12）：
 *  1. 单人模式（/api/auth/me 404）：不读 channels、不 PUT、store 零改动（channelMode 仍 local）
 *  2. 服务端模式登出态（401）：不拉 channels、仍 local
 *  3. 已登录 + 服务端非空：服务端权威（GET 原样进 store、不 PUT），channelMode=remote，key 只剩 masked
 *  4. 已登录 + 服务端为空：store 落地空列表（B0/B1：不再一次性迁移本地渠道）；
 *     变更 → 防抖 PUT 一次、回写 masked 视图（4b/4c）
 *  5. 保存失败：不丢本地数据、错误文案进 snapshot
 *  7. 请求层：/api/proxy/<channelId>/… 路径拼接 + 服务端模式零鉴权头 + 本地模式与基线一致
 *  8. B3.5：仅本地渠道判定/分拣（IMPL-3）、卸载回写分拣与 keepalive 体积上限（IMPL-2）
 *  9. B2（S3.9）：登录态不再做预置层合并/本地迁移——selectMigratableChannels /
 *     mergeLocalOnlyChannels 已删，服务端返回值是渠道的唯一来源
 */
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

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

const workdir = mkdtempSync(path.join(tmpdir(), "probe-s2-b3-"));
const configStoreKey = "infinite-canvas:ai_config_store";

// ---- shim：必须先于目标模块求值（ESM import 按序求值）--------------------------------
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
export const calls = { fetch: [], reload: 0 };
const jsonResponse = (status, body) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
});
globalThis.localStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
    clear: () => storage.clear(),
    key: (index) => Array.from(storage.keys())[index] ?? null,
    get length() { return storage.size; },
};
globalThis.window = globalThis;
const listeners = new Map();
globalThis.addEventListener = (name, handler) => { listeners.set(name, handler); };
globalThis.removeEventListener = (name) => { listeners.delete(name); };
globalThis.location = { origin: "http://localhost:3000", href: "http://localhost:3000/", reload: () => { calls.reload += 1; } };
let route = async () => jsonResponse(404, { error: "not found" });
export function setRoute(next) { route = next; }
globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : String(input && input.url);
    calls.fetch.push({ url, method: (init && init.method) || "GET", body: init && init.body });
    return route(url, init);
};
globalThis.__jsonResponse = jsonResponse;
`,
);

let entry = `
import { setRoute } from "./shim.mjs";
export { storage, calls, setRoute } from "./shim.mjs";
`;
for (const rel of ["src/services/channel-sync.ts", "src/services/api/channel-proxy.ts", "src/stores/use-config-store.ts"]) {
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
const shim = { storage: bundle.storage, calls: bundle.calls, setRoute: bundle.setRoute };
const sync = bundle.src_services_channel_sync_ts;
const proxy = bundle.src_services_api_channel_proxy_ts;
const store = bundle.src_stores_use_config_store_ts;

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

const originalWarn = console.warn;
console.warn = () => {};
const originalInfo = console.info;
console.info = () => {};

const localChannels = [
    { id: "srv-ready", name: "我的聚鑫", baseUrl: "https://api.jxinai.com", apiKey: "sk-live-12345678", apiFormat: "openai", models: [{ name: "gpt-image-2", capability: "image" }] },
    { id: "preset", name: "预置渠道", baseUrl: "https://preset.example.com", apiKey: "", apiFormat: "openai", models: [{ name: "gpt-5.5", capability: "text" }] },
    { id: "wan-ch", name: "万相", baseUrl: "http://127.0.0.1:8779", apiKey: "sk-wan-999", apiFormat: "wan", models: [{ name: "wan-omni2video", capability: "video" }] },
    { id: "http-ch", name: "明文渠道", baseUrl: "http://insecure.example.com", apiKey: "sk-http-555", apiFormat: "openai", models: [{ name: "flux-pro", capability: "image" }] },
];

function seedLocal(channels) {
    shim.storage.clear();
    shim.storage.set(
        configStoreKey,
        JSON.stringify({
            state: { config: { ...store.defaultConfig, channels, models: store.modelOptionsFromChannels(channels) }, webdav: {} },
            version: 0,
        }),
    );
    // 重新水合 store（persist 是同步 merge；测试里显式 set，等价于刷新页面后的内存态）
    store.useConfigStore.setState((state) => ({ config: { ...state.config, channels, channelMode: "local", apiKey: "sk-live-12345678" } }));
}

function reset() {
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    sync.stopChannelAutoSave();
}

const serverView = (channel) => ({
    id: channel.id,
    name: channel.name,
    baseUrl: channel.baseUrl,
    apiFormat: channel.apiFormat,
    models: channel.models,
    apiKey: `***${channel.apiKey.slice(-4)}`,
});

// ---------------------------------------------------------------------------
// 1. 单人模式：/api/auth/me 404 → 零改动
// ---------------------------------------------------------------------------
seedLocal(localChannels);
reset();
shim.setRoute(async (url, init) => {
    if (url === "/api/auth/me") return globalThis.__jsonResponse(404, { error: "not found" });
    return globalThis.__jsonResponse(404, { error: "not found" });
});
let snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
equal("1. 单人模式模式=local", snap.mode, "local");
// S3.5 白名单期望更新（任务书 V8）：单人模式每 boot 也会拉一次预置文件（版本比对，零写不零请求）
equal("1. 单人序列=/api/auth/me → /api/bootstrap-config", shim.calls.fetch.map((c) => c.url), ["/api/auth/me", "/api/bootstrap-config"]);
equal("1. 本地渠道一个不动", store.useConfigStore.getState().config.channels.map((c) => c.id), ["srv-ready", "preset", "wan-ch", "http-ch"]);
equal("1. store 模式仍 local", store.useConfigStore.getState().config.channelMode, "local");
equal("1. 本地 key 未被清", store.useConfigStore.getState().config.apiKey, "sk-live-12345678");

// 2. 服务端模式登出态：401 → 不拉 channels、仍 local
seedLocal(localChannels);
reset();
shim.setRoute(async (url) => (url === "/api/auth/me" ? globalThis.__jsonResponse(401, { error: "未登录" }) : globalThis.__jsonResponse(404, {})));
snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
equal("2. 登出态=local", snap.mode, "local");
check("2. 未拉 channels", !shim.calls.fetch.some((c) => c.url === "/api/user/channels"), JSON.stringify(shim.calls.fetch.map((c) => c.url)));

// ---------------------------------------------------------------------------
// 3. 已登录 + 服务端非空：服务端权威，GET 原样进 store，不 PUT（B0/B1 新语义）
// ---------------------------------------------------------------------------
seedLocal(localChannels);
reset();
const remoteChannels = [{ id: "srv-only", name: "服务端渠道", baseUrl: "https://api.example.com", apiFormat: "openai", models: [{ name: "gpt-image-2", capability: "image" }], apiKey: "***abcd" }];
shim.setRoute(async (url, init) => {
    if (url === "/api/auth/me") return globalThis.__jsonResponse(200, { username: "alice" });
    if (url === "/api/user/channels" && (!init || !init.method)) return globalThis.__jsonResponse(200, { channels: remoteChannels });
    return globalThis.__jsonResponse(500, { error: "unexpected" });
});
snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
const after3 = store.useConfigStore.getState().config;
equal("3. 模式=remote", snap.mode, "remote");
// B0/B1：store = 服务端返回值原样（GET 原样进 store），本地缓存与预置层不再参与合并
equal("3. 渠道=服务端返回值原样", after3.channels.map((c) => c.id), ["srv-only"]);
check("3. 预置层不参与合并（本地 preset/wan/http 渠道不残留）", !after3.channels.some((c) => ["preset", "wan-ch", "http-ch"].includes(c.id)), JSON.stringify(after3.channels.map((c) => c.id)));
check("3. notice 为空（无迁移/合并提示）", !String(snap.notice).trim(), snap.notice);
check("3. 未发 PUT", !shim.calls.fetch.some((c) => c.method === "PUT"), JSON.stringify(shim.calls.fetch.map((c) => `${c.method} ${c.url}`)));
sync.stopChannelAutoSave();

// ---------------------------------------------------------------------------
// 4. 已登录 + 服务端为空：store 落地空列表（B0/B1：不再一次性迁移）；变更 → 防抖 PUT 一次
// ---------------------------------------------------------------------------
seedLocal(localChannels);
reset();
let putBody = null;
shim.setRoute(async (url, init) => {
    if (url === "/api/auth/me") return globalThis.__jsonResponse(200, { username: "alice" });
    if (url === "/api/user/channels" && init && init.method === "PUT") {
        putBody = JSON.parse(init.body);
        return globalThis.__jsonResponse(200, { ok: true, channels: putBody.channels.map(serverView) });
    }
    if (url === "/api/user/channels") return globalThis.__jsonResponse(200, { channels: [] });
    return globalThis.__jsonResponse(500, {});
});
snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
const after4 = store.useConfigStore.getState().config;
equal("4. 模式=remote", snap.mode, "remote");
// B0/B1：服务端为空 → 渠道为空（本地渠道不迁移上服务端）
equal("4. store=空渠道列表（不做一次性迁移）", after4.channels.map((c) => c.id), []);
check("4. 未发迁移 PUT", !shim.calls.fetch.some((c) => c.method === "PUT"), JSON.stringify(shim.calls.fetch.map((c) => `${c.method} ${c.url}`)));
check("4. notice 为空（无迁移提示）", !String(snap.notice).trim(), snap.notice);

// 4b. 变更 → 防抖 PUT 一次
putBody = null;
shim.calls.fetch.length = 0;
const edited = [{ id: "new-1", name: "改名后的渠道", baseUrl: "https://api.example.com", apiKey: "sk-new-9999", apiFormat: "openai", models: [{ name: "m", capability: "image" }] }];
store.useConfigStore.getState().updateConfig("channels", edited);
await new Promise((resolve) => setTimeout(resolve, 120));
await sync.flushChannelSync();
const puts = shim.calls.fetch.filter((c) => c.method === "PUT");
equal("4b. 变更后只发一次 PUT", puts.length, 1);
equal("4b. 回写后 key 变 masked（服务端响应视图）", store.useConfigStore.getState().config.channels[0].apiKey, "***9999");
equal("4b. 回写后 store 与响应一致", store.useConfigStore.getState().config.channels[0].name, "改名后的渠道");

// 4c. 新渠道草稿（还没填 key）不参与 PUT：否则服务端整批 422，会拖死其它渠道的正常改动
shim.calls.fetch.length = 0;
const withDraft = [...store.useConfigStore.getState().config.channels, { id: "draft-new", name: "新渠道草稿", baseUrl: "https://draft.example.com", apiKey: "", apiFormat: "openai", models: [] }];
store.useConfigStore.getState().updateConfig("channels", withDraft);
await new Promise((resolve) => setTimeout(resolve, 120));
await sync.flushChannelSync();
const draftPuts = shim.calls.fetch.filter((c) => c.method === "PUT");
equal("4c. 只提交有 key 的渠道", JSON.parse(draftPuts[0].body).channels.map((c) => c.id), ["new-1"]);
check("4c. 草稿渠道留在本地继续编辑", store.useConfigStore.getState().config.channels.some((c) => c.id === "draft-new"), "草稿渠道被丢弃");
check("4c. 提示草稿渠道未保存", String(sync.channelSyncSnapshot().error).includes("新渠道草稿"), sync.channelSyncSnapshot().error);
sync.stopChannelAutoSave();

// ---------------------------------------------------------------------------
// 5. 保存失败：不丢本地数据、错误文案进 snapshot（B0/B1：无「迁移退回 local」路径）
// ---------------------------------------------------------------------------
seedLocal(localChannels);
reset();
shim.setRoute(async (url, init) => {
    if (url === "/api/auth/me") return globalThis.__jsonResponse(200, { username: "alice" });
    if (url === "/api/user/channels" && (!init || !init.method)) return globalThis.__jsonResponse(200, { channels: remoteChannels });
    return globalThis.__jsonResponse(500, {});
});
snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
store.useConfigStore.getState().updateConfig("channels", [{ id: "srv-only", name: "改名触发保存", baseUrl: "https://api.example.com", apiKey: "***abcd", apiFormat: "openai", models: [] }]);
await new Promise((resolve) => setTimeout(resolve, 120));
await sync.flushChannelSync();
equal("5. 保存失败模式仍 remote", sync.channelSyncSnapshot().mode, "remote");
check("5. 失败文案进 snapshot", String(sync.channelSyncSnapshot().error).length > 0, sync.channelSyncSnapshot().error);
sync.stopChannelAutoSave();

// ---------------------------------------------------------------------------
// 6. 读服务端渠道失败：不动本地数据、留错误、不迁移
// ---------------------------------------------------------------------------
seedLocal(localChannels);
reset();
shim.setRoute(async (url, init) => {
    if (url === "/api/auth/me") return globalThis.__jsonResponse(200, { username: "alice" });
    if (url === "/api/user/channels" && (!init || !init.method)) return globalThis.__jsonResponse(503, { error: "主密钥不可用" });
    return globalThis.__jsonResponse(500, {});
});
snap = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 10 });
equal("6. 读失败保持 local", snap.mode, "local");
equal("6. 读失败不动本地渠道", store.useConfigStore.getState().config.channels.map((c) => c.id), ["srv-ready", "preset", "wan-ch", "http-ch"]);
check("6. 读失败记错误文案", String(snap.error).includes("503"), snap.error);

// ---------------------------------------------------------------------------
// 7. 请求层：路径拼接 + 鉴权头
// ---------------------------------------------------------------------------
const remoteConfig = { ...store.defaultConfig, channelMode: "remote", channelId: "ch-1", model: "ch-1::gpt-image-2", baseUrl: "https://api.example.com/v1", apiKey: "" };
equal("7. remote 路径=baseUrl.path+/v1+path", proxy.channelApiUrl(remoteConfig, "/images/generations"), "/api/proxy/ch-1/v1/images/generations");
equal("7. baseUrl 已带 /v1 不重复", proxy.channelApiUrl({ ...remoteConfig, baseUrl: "https://api.example.com/v1" }, "/models"), "/api/proxy/ch-1/v1/models");
equal("7. 查询串透传", proxy.channelAbsoluteUrl(remoteConfig, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3:streamGenerateContent?alt=sse"), "/api/proxy/ch-1/v1beta/models/gemini-3:streamGenerateContent?alt=sse");
const remoteHeaders = proxy.channelAuthHeaders(remoteConfig, "application/json");
check("7. remote 不带 Authorization", !("Authorization" in remoteHeaders), JSON.stringify(remoteHeaders));
equal("7. remote 仍带 Content-Type", remoteHeaders["Content-Type"], "application/json");
check("7. remote gemini 不带 x-goog-api-key", !("x-goog-api-key" in proxy.geminiChannelHeaders(remoteConfig)), JSON.stringify(proxy.geminiChannelHeaders(remoteConfig)));
const localConfig = { ...store.defaultConfig, channelMode: "local", channelId: "ch-1", baseUrl: "https://api.example.com", apiKey: "sk-local-key" };
equal("7. local 路径与基线一致", proxy.channelApiUrl(localConfig, "/models"), "https://api.example.com/v1/models");
equal("7. local 仍带 Authorization", proxy.channelAuthHeaders(localConfig).Authorization, "Bearer sk-local-key");
equal("7. 模式缺省回落 store（未登录 local）", proxy.isServerChannelMode({}), false);

// 7b. 缺 channelId 时按模型引用串兜底；两者都没有 → 明确报错
equal("7b. 从 channelId::model 解出渠道", proxy.channelApiUrl({ ...remoteConfig, channelId: "", model: "ch-9::flux-pro" }, "/models"), "/api/proxy/ch-9/v1/models");
let threw = false;
try {
    proxy.channelApiUrl({ ...remoteConfig, channelId: "", model: "flux-pro" }, "/models");
} catch (error) {
    threw = String(error.message).length > 0;
}
check("7b. 无 channelId 时明确报错（不发裸上游请求）", threw, "未抛错");


// 8. B3.5：仅本地渠道（IMPL-3）/ 卸载回写分拣与体积上限（IMPL-2）
// ---------------------------------------------------------------------------
const ch = (over) => ({ id: "x", name: "渠道", baseUrl: "https://api.example.com", apiKey: "sk-good", apiFormat: "openai", models: [], ...over });
// 8a. 仅本地判定：wan / 无 key / 非 https 为真；masked key 的 https 渠道为假（那是服务端视图缓存）
equal("8a. 仅本地判定", [
    sync.isLocalOnlyChannel(ch({ id: "a", apiFormat: "wan" })),
    sync.isLocalOnlyChannel(ch({ id: "b", apiKey: "" })),
    sync.isLocalOnlyChannel(ch({ id: "c", baseUrl: "http://insecure.example.com" })),
    sync.isLocalOnlyChannel(ch({ id: "d", apiKey: "***1234" })),
    sync.isLocalOnlyChannel(ch({ id: "e" })),
], [true, true, true, false, false]);

// 8b. 分拣：仅本地渠道不进 PUT；masked key 的服务端渠道照常提交（服务端沿用密文）
const split = sync.splitSavableChannels([
    ch({ id: "keep-1" }),
    ch({ id: "keep-2", apiKey: "***5678" }),
    ch({ id: "skip-wan", apiFormat: "wan" }),
    ch({ id: "skip-nokey", apiKey: "" }),
    ch({ id: "skip-http", baseUrl: "http://insecure.example.com" }),
]);
equal("8b. 可提交渠道", split.savable.map((c) => c.id), ["keep-1", "keep-2"]);
equal("8b. 被跳过渠道", split.skipped.map((c) => c.id), ["skip-wan", "skip-nokey", "skip-http"]);
const skippedText = String(sync.describeSkippedChannels(split.skipped));
check(
    "8b. 跳过提示只点名「未填 key」，不点名本地地址/非 https（B2.7 去噪）",
    skippedText.includes("还没填写 API Key") && !skippedText.includes("指向本地地址") && !skippedText.includes("不是 https") && !skippedText.includes("本地模式"),
    skippedText,
);
// 8b-3. B2.7：只有 wan/非 https 被跳过时，保存链提示为空（不再每次保存都弹同一句警告）
const onlyLocalText = String(sync.describeSkippedChannels([ch({ id: "w", name: "万相", apiFormat: "wan", baseUrl: "http://127.0.0.1:8779" }), ch({ id: "h", name: "明文渠道", baseUrl: "http://insecure.example.com" })]));
equal("8b-3. 仅 wan/非 https 被跳过时保存链提示为空", onlyLocalText, "");
// 8b-2. 橙色警告不再点名无 key 渠道：只有「未填 Key」这一句，没有服务端不可达的说法
const onlyNoKey = String(sync.describeSkippedChannels([ch({ id: "nokey-only", name: "预置渠道", apiKey: "" })]));
check("8b-2. 无 key 渠道只报未填 key（不进橙色警告名单）", onlyNoKey.includes("还没填写 API Key") && !onlyNoKey.includes("服务器无法") && !onlyNoKey.includes("本地模式"), onlyNoKey);

// ---------------------------------------------------------------------------
// 8f. 渠道徽标：用户可见判定与门控分离（界面上没有「本地模式」这个概念）
// ---------------------------------------------------------------------------
const badgeOf = (channel, options) => sync.channelBadgeFor(channel, options);
const noKeyBadge = badgeOf(ch({ id: "ui-nokey", apiKey: "", baseUrl: "https://api.example.com" }));
const wanBadge = badgeOf(ch({ id: "ui-wan", apiFormat: "wan", baseUrl: "http://127.0.0.1:8779", apiKey: "" }));
const httpBadge = badgeOf(ch({ id: "ui-http", apiKey: "sk-probe-key", baseUrl: "http://insecure.example.com" }));
const scriptBadge = badgeOf(ch({ id: "ui-script", apiKey: "sk-probe-key", models: [{ name: "m", capability: "image", script: "return 1;" }] }), { serverMode: true });
const okBadge = badgeOf(ch({ id: "ui-ok", apiKey: "sk-probe-key" }));
equal("8f. 徽标种类（未填 Key / 服务器不可达×2 / 脚本渠道 / 无徽标）", [noKeyBadge, wanBadge, httpBadge, scriptBadge, okBadge].map((badge) => (badge ? badge.kind : null)), ["missingKey", "serverUnreachable", "serverUnreachable", "script", null]);
check("8f-2. 无 key https 渠道徽标=未填 Key 且不含「本地模式」", noKeyBadge.label === "未填 Key" && !`${noKeyBadge.label}${noKeyBadge.hint}`.includes("本地模式"), JSON.stringify(noKeyBadge));
check("8f-3. wan 渠道徽标是事实陈述（服务器不可达 + 说明访问不到本地地址）", wanBadge.label === "服务器不可达" && wanBadge.hint.includes("服务器无法代为访问"), JSON.stringify(wanBadge));
check("8f-4. 脚本渠道徽标说明服务端不下发 Key", scriptBadge.label === "脚本渠道" && scriptBadge.hint.includes("不会把 Key 下发给脚本"), JSON.stringify(scriptBadge));
// 8f-5. 徽标不影响门控：无 key 渠道徽标=未填 Key，但内部判定仍为「不进 PUT」
check("8f-5. 展示判定与路由门控各归各位", noKeyBadge.kind === "missingKey" && sync.isLocalOnlyChannel(ch({ id: "ui-nokey", apiKey: "", baseUrl: "https://api.example.com" })) === true, "徽标/门控耦合了");

// 8c. 卸载回写：草稿不入 body；超大 models 体跳过并留痕（keepalive 64KB 上限）
const unloadEmpty = sync.buildUnloadPayload([ch({ id: "draft", apiKey: "" })]);
equal("8c. 只有草稿时不发卸载回写", [unloadEmpty.ok, unloadEmpty.reason], [false, "empty"]);
const unloadOk = sync.buildUnloadPayload([ch({ id: "keep-1" }), ch({ id: "draft", apiKey: "" })]);
check("8c. 卸载回写 body 只含可提交渠道", unloadOk.ok && JSON.parse(unloadOk.body).channels.map((c) => c.id).join(",") === "keep-1", JSON.stringify(unloadOk));
const bigModels = Array.from({ length: 400 }, (_, index) => ({ name: `model-${index}-${"x".repeat(200)}`, capability: "image" }));
const unloadBig = sync.buildUnloadPayload([ch({ id: "keep-big", models: bigModels })]);
equal("8c. 超过 keepalive 上限时跳过回写", [unloadBig.ok, unloadBig.reason], [false, "too-large"]);
check("8c. 被跳过的体积确实超过 64KB", unloadBig.bytes > sync.KEEPALIVE_BODY_LIMIT, String(unloadBig.bytes));

console.warn = originalWarn;
console.info = originalInfo;

console.log(`=== S2-B3 渠道持久化探针：${passed} passed / ${failures.length} failed ===`);
for (const item of failures) console.log(`FAIL | ${item}`);
console.log(`EXIT_CODE=${failures.length ? 1 : 0}`);
process.exit(failures.length ? 1 : 0);
