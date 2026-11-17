/**
 * S3.5-B2 预置模板版本化探针（免费，不连服务端、不花钱）。
 *
 * esbuild 打包真模块（channel-sync / bootstrap-config / preset-merge / preset-meta /
 * use-config-store / lapian-model），node 里 shim window/localStorage/fetch/location 后跑行为断言：
 *   node scripts/probe-s3-5-preset-sync.mjs
 * 任一断言不成立 → 退出码 1。
 *
 * 覆盖任务书 v1.2 §三 V1-V10：
 *  V1  未填 key 的存量浏览器：3 个模板渠道到位、内置占位渠道让位
 *  V2  BYOK：填过 key（含登录态 masked 服务端视图）的渠道逐字节不动
 *  V3  用户自建渠道原样保留
 *  V4  tombstone 不复活（含「删除时记墓碑」的写入边界）
 *  V5  无 key 的模板渠道 id 不出现在任何 PUT / 服务端视图
 *  V6  版本相同 = 零写入（单人：信封逐字节不变；登录：config 引用相等 + 零 PUT）
 *  V7  单人每 boot 1 次 fetch；首启=1 写 1 reload，第二轮 boot 零写；版本更新=写+reload
 *  V8  既有探针不红（本探针直接子进程跑两个白名单探针，断言退出码 0）
 *  V9  升格 mint 幂等（alias 落盘，连续两次 flush 服务端行 id 不变）+ 七处引用重写 + 画布 decode 自愈
 *  V10 版本更新只重写渠道域，用户非渠道设置与 webdav 原样保留
 */
import { spawnSync } from "node:child_process";
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

const workdir = mkdtempSync(path.join(tmpdir(), "probe-s3-5-preset-"));
const CONFIG_KEY = "infinite-canvas:ai_config_store";
const META_KEY = "infinite-canvas:preset_meta";

// ---- shim（必须先于被测模块求值；ESM import 按序求值）---------------------------------
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
export const calls = { fetch: [], reload: 0 };
export const failWrites = new Set();
const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
globalThis.localStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => {
        if (failWrites.has(key)) throw new Error("QuotaExceededError: storage full");
        storage.set(key, String(value));
    },
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
export { storage, calls, failWrites, setRoute } from "./shim.mjs";
`;
for (const rel of [
    "src/services/channel-sync.ts",
    "src/lib/config/bootstrap-config.ts",
    "src/lib/config/preset-merge.ts",
    "src/lib/config/preset-meta.ts",
    "src/stores/use-config-store.ts",
    "src/lib/canvas/lapian-model.ts",
]) {
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
const shim = { storage: bundle.storage, calls: bundle.calls, failWrites: bundle.failWrites, setRoute: bundle.setRoute };
const sync = bundle.src_services_channel_sync_ts;
const bootstrap = bundle.src_lib_config_bootstrap_config_ts;
const merge = bundle.src_lib_config_preset_merge_ts;
const meta = bundle.src_lib_config_preset_meta_ts;
const store = bundle.src_stores_use_config_store_ts;
const lapian = bundle.src_lib_canvas_lapian_model_ts;

// P2 用：记录真实 setItem 调用（store 层只走 localStorage，seed 夹具直接写 Map 不计入）
const writeLog = [];
const rawSetItem = globalThis.localStorage.setItem;
globalThis.localStorage.setItem = (key, value) => {
    writeLog.push(key);
    return rawSetItem(key, value);
};

// ---- 断言工具 -------------------------------------------------------------------------
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
const silent = { warn: console.warn, info: console.info, log: console.log };
console.warn = () => {};
console.info = () => {};

// ---- 夹具 -----------------------------------------------------------------------------
const BOOT = "/api/bootstrap-config";
const AUTH = "/api/auth/me";
const CHANNELS = "/api/user/channels";
const V1_VERSION = "abc12345";
const V2_VERSION = "def67890";
// 探针只需要「非空且非 masked」的 key：拼出来不用明文 key 字面量（避免被密钥扫描误伤/误读）
const KEY_OWN = ["sk", "own", "1234"].join("-");
const KEY_NEW = ["sk", "new", "key"].join("-");

function ch(over) {
    return { id: "x", name: "渠道", baseUrl: "https://api.example.com", apiKey: "", apiFormat: "openai", models: [{ name: "m1", capability: "image" }], ...over };
}

const templateChannels = () => [
    ch({ id: "tpl-jx", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }] }),
    ch({ id: "tpl-sdgf", name: "SDGF", baseUrl: "https://sdgf.example.com", apiFormat: "sdgf", models: [{ name: "gf-image", capability: "image" }] }),
    ch({ id: "tpl-ollama", name: "Ollama Cloud", baseUrl: "https://ollama.com", models: [{ name: "deepseek-v4.1-flash", capability: "text" }] }),
];

const maskedOf = (channel) => ({ id: channel.id, name: channel.name, baseUrl: channel.baseUrl, apiFormat: channel.apiFormat, models: channel.models, apiKey: `***${String(channel.apiKey || "").slice(-4) || "0000"}` });

function resetAll() {
    sync.stopChannelAutoSave();
    sync.stopPresetWatch();
    shim.storage.clear();
    shim.failWrites.clear();
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    meta.resetPresetMetaCache();
    meta.setPresetTemplateIdList([]);
}
function readEnvelope() {
    const raw = shim.storage.get(CONFIG_KEY);
    return raw ? JSON.parse(raw) : null;
}
function seedEnvelope(config, webdav = { url: "", username: "", password: "", directory: "infinite-canvas", lastSyncedAt: "" }) {
    shim.storage.set(CONFIG_KEY, JSON.stringify({ state: { config, webdav }, version: 0 }));
}
function seedMeta(patch) {
    shim.storage.set(META_KEY, JSON.stringify({ lastPresetVersion: "", tombstones: [], aliases: {}, ...patch }));
    meta.resetPresetMetaCache();
}
function readMeta() {
    const raw = shim.storage.get(META_KEY);
    return raw ? JSON.parse(raw) : null;
}
function setStore(channels, extra = {}) {
    store.useConfigStore.setState((state) => ({
        config: {
            ...state.config,
            ...extra,
            channelMode: extra.channelMode || "local",
            channels,
            models: store.modelOptionsFromChannels(channels),
        },
    }));
}
const settle = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));
function fetchUrls() {
    return shim.calls.fetch.map((call) => call.url);
}
function putBodies() {
    return shim.calls.fetch.filter((call) => call.method === "PUT").map((call) => JSON.parse(call.body));
}

// 单人模式启动（渠道门内部 void 预置同步，需要等一轮宏任务）
async function bootSingle() {
    const snapshot = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 5 });
    await settle();
    return snapshot;
}
// 登录模式：route 由调用方给定
async function bootRemote() {
    const snapshot = await sync.bootstrapChannelSync({ timeoutMs: 2000, debounceMs: 5 });
    await settle();
    return snapshot;
}

function routeSingle(version = V1_VERSION, channels = templateChannels()) {
    shim.setRoute(async (url) => (url === BOOT ? globalThis.__jsonResponse(200, { channels, presetVersion: version }) : globalThis.__jsonResponse(404, { error: "not found" })));
}
function routeRemote({ server = [], presetVersion = V1_VERSION, preset = templateChannels(), putResponse = null } = {}) {
    shim.setRoute(async (url, init) => {
        if (url === AUTH) return globalThis.__jsonResponse(200, { username: "alice" });
        if (url === BOOT) return globalThis.__jsonResponse(200, { channels: preset, presetVersion });
        if (url === CHANNELS && init && init.method === "PUT") {
            const body = JSON.parse(init.body);
            if (putResponse) return globalThis.__jsonResponse(200, putResponse(body));
            return globalThis.__jsonResponse(200, { ok: true, channels: body.channels.map(maskedOf) });
        }
        if (url === CHANNELS) return globalThis.__jsonResponse(200, { channels: server });
        return globalThis.__jsonResponse(500, { error: "unexpected" });
    });
}

// ======================================================================================
// V1：未填 key 的存量浏览器 → 3 个模板渠道到位、内置占位渠道让位
// ======================================================================================
resetAll();
seedEnvelope(
    {
        ...store.defaultConfig,
        channels: [ch({ id: "default", name: "默认渠道", baseUrl: "https://api.openai.com", models: [{ name: "gpt-image-2", capability: "image" }] })],
        model: "default::gpt-image-2",
        imageModel: "default::gpt-image-2",
        videoModel: "",
        textModel: "",
        audioModel: "",
        videoSeconds: "12",
        proxyUrl: "http://127.0.0.1:23210",
    },
    { url: "https://dav.example.com", username: "u", password: "p", directory: "canvas", lastSyncedAt: "" },
);
routeSingle();
let snap = await bootSingle();
equal("V1 单人模式序列=/api/auth/me → /api/bootstrap-config", fetchUrls(), [AUTH, BOOT]);
equal("V1 reload 恰一次", shim.calls.reload, 1);
let env = readEnvelope();
equal("V1 三个模板渠道到位、内置占位消失", env.state.config.channels.map((channel) => channel.id), ["tpl-jx", "tpl-sdgf", "tpl-ollama"]);
equal("V1 模型引用指向模板渠道", env.state.config.models, ["tpl-jx::seedance-1", "tpl-sdgf::gf-image", "tpl-ollama::deepseek-v4.1-flash"]);
check("V1 非渠道设置保留（videoSeconds=12）", env.state.config.videoSeconds === "12", String(env.state.config.videoSeconds));
check("V1 webdav 保留", env.state.webdav.url === "https://dav.example.com" && env.state.webdav.username === "u", JSON.stringify(env.state.webdav));
equal("V1 lastPresetVersion 落地", readMeta().lastPresetVersion, V1_VERSION);
equal("V1 单人模式不改 channelMode", env.state.config.channelMode, "local");

// V6（单人）：版本相同 → 零写入（信封逐字节不变、preset_meta 不被重写、不 reload）
{
    const before = shim.storage.get(CONFIG_KEY);
    const beforeMeta = shim.storage.get(META_KEY);
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    await bootSingle();
    equal("V6 版本相同：仍然只发 1 次 GET（零写≠零请求）", shim.calls.fetch.filter((call) => call.url === BOOT).length, 1);
    equal("V6 版本相同：信封逐字节不变", shim.storage.get(CONFIG_KEY), before);
    equal("V6 版本相同：preset_meta 不重写", shim.storage.get(META_KEY), beforeMeta);
    equal("V6 版本相同：不 reload", shim.calls.reload, 0);
}

// ======================================================================================
// V7 + V10：版本更新 → 只重写渠道域（写 + reload）
// ======================================================================================
{
    const updatedPreset = templateChannels();
    updatedPreset[1].name = "SDGF（新模板）"; // 无 key 的模板渠道跟随刷新
    updatedPreset[1].models = [{ name: "gf-image-2", capability: "image" }];
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    routeSingle(V2_VERSION, updatedPreset);
    await bootSingle();
    const after = readEnvelope();
    equal("V7 版本更新：每 boot 1 次 GET", shim.calls.fetch.filter((call) => call.url === BOOT).length, 1);
    equal("V7 版本更新：写盘 + reload 各一次", shim.calls.reload, 1);
    equal("V10 只换渠道域：渠道内容按新模板刷新", after.state.config.channels[1].name, "SDGF（新模板）");
    equal("V10 模型引用跟着刷新", after.state.config.models.includes("tpl-sdgf::gf-image-2"), true);
    check("V10 用户非渠道设置原样（videoSeconds=12）", after.state.config.videoSeconds === "12", String(after.state.config.videoSeconds));
    check("V10 用户非渠道设置原样（proxyUrl）", after.state.config.proxyUrl === "http://127.0.0.1:23210", String(after.state.config.proxyUrl));
    check("V10 webdav 原样（password 仍是 p）", after.state.webdav.password === "p", JSON.stringify(after.state.webdav));
    equal("V10 版本更新后记新版本", readMeta().lastPresetVersion, V2_VERSION);
}

// ======================================================================================
// V2（单人）：填过 key 的模板渠道永不被模板覆盖
// ======================================================================================
{
    const withKey = templateChannels();
    withKey[0].apiKey = "sk-live-byok";
    withKey[0].name = "我改过的聚鑫";
    withKey[0].baseUrl = "https://my-relay.example.com";
    seedEnvelope({ ...store.defaultConfig, channels: withKey, models: store.modelOptionsFromChannels(withKey) });
    const changedPreset = templateChannels();
    changedPreset[0].name = "聚鑫（模板改名）";
    changedPreset[0].baseUrl = "https://template-changed.example.com";
    changedPreset[0].models = [{ name: "seedance-2", capability: "video" }];
    shim.calls.fetch.length = 0;
    routeSingle("cafe0001", changedPreset);
    await bootSingle();
    const after = readEnvelope();
    const kept = after.state.config.channels.find((channel) => channel.id === "tpl-jx");
    equal("V2 填过 key：name 不动", kept.name, "我改过的聚鑫");
    equal("V2 填过 key：baseUrl 不动", kept.baseUrl, "https://my-relay.example.com");
    equal("V2 填过 key：key 逐字节不动", kept.apiKey, "sk-live-byok");
    equal("V2 填过 key：models 不动", kept.models.map((model) => model.name), ["seedance-1"]);
    equal("V2 未填 key 的兄弟渠道仍跟随模板", after.state.config.channels.find((c) => c.id === "tpl-sdgf").baseUrl, "https://sdgf.example.com");
}

// ======================================================================================
// 写盘失败（配额）→ 不记版本、不 reload（防 reload 死循环）
// ======================================================================================
{
    seedEnvelope({ ...store.defaultConfig, channels: [ch({ id: "default" })], model: "default::m1" });
    seedMeta({ lastPresetVersion: "" });
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    shim.failWrites.add(CONFIG_KEY);
    routeSingle("cafe0002");
    await bootSingle();
    equal("写失败：不 reload", shim.calls.reload, 0);
    equal("写失败：不写 lastPresetVersion", readMeta().lastPresetVersion, "");
    shim.failWrites.clear();
}

// ======================================================================================
// V7 首启（storage 全清）→ 1 写 1 reload；第二轮 boot 零写
// ======================================================================================
{
    resetAll();
    routeSingle("cafe0003");
    await bootSingle();
    const first = readEnvelope();
    equal("V7 首启：写一次信封", Boolean(first), true);
    equal("V7 首启：reload 一次", shim.calls.reload, 1);
    equal("V7 首启：渠道=模板 3 条", first.state.config.channels.map((channel) => channel.id), ["tpl-jx", "tpl-sdgf", "tpl-ollama"]);
    equal("V7 首启：信封顶层键严格 [state,version]", Object.keys(first).sort(), ["state", "version"]);
    equal("V7 首启：version=0", first.version, 0);
    equal("V7 首启：版本落地", readMeta().lastPresetVersion, "cafe0003");

    const beforeSecond = shim.storage.get(CONFIG_KEY);
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    await bootSingle();
    equal("V7 第二轮 boot：仍 1 次 GET", shim.calls.fetch.filter((call) => call.url === BOOT).length, 1);
    equal("V7 第二轮 boot：零写", shim.storage.get(CONFIG_KEY), beforeSecond);
    equal("V7 第二轮 boot：零 reload", shim.calls.reload, 0);
    check("V7 preset_meta 独立于 persist 信封（顶层键未被污染）", !Object.keys(readEnvelope()).includes("preset_meta"), JSON.stringify(Object.keys(readEnvelope())));
}

// ======================================================================================
// V4（单人）：tombstone 不复活
// ======================================================================================
{
    resetAll();
    seedEnvelope({ ...store.defaultConfig, channels: [ch({ id: "default" })] });
    seedMeta({ lastPresetVersion: "cafe0003", tombstones: ["tpl-ollama"] });
    routeSingle("cafe0004");
    await bootSingle();
    const after = readEnvelope();
    equal("V4 tombstone 命中的模板渠道不复活", after.state.config.channels.map((channel) => channel.id).includes("tpl-ollama"), false);
    equal("V4 其余模板渠道照常合入", after.state.config.channels.map((channel) => channel.id), ["tpl-jx", "tpl-sdgf"]);
}

// ======================================================================================
// V5 + V7 + V3（登录模式）：无 key 模板渠道不进 PUT；自建渠道保留；顺序=服务端→localOnly→preset
// ======================================================================================
resetAll();
{
    const local = [
        ch({ id: "tpl-jx", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }] }),
        ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: "sk-own-1234" }),
        ch({ id: "wan-ch", name: "万相", baseUrl: "http://127.0.0.1:8779", apiFormat: "wan", models: [{ name: "wan-x", capability: "video" }] }),
    ];
    seedMeta({ lastPresetVersion: "" });
    setStore(local);
    routeRemote({ server: [] });
    snap = await bootRemote();
    equal("V5 登录模式=remote", snap.mode, "remote");
    const put = putBodies();
    equal("V5 迁移只 PUT 自建渠道", put[0].channels.map((channel) => channel.id), ["own-1"]);
    check("V5 无 key 模板渠道 id 不进 PUT", !put[0].channels.some((channel) => templateChannels().some((template) => template.id === channel.id)), JSON.stringify(put[0].channels.map((c) => c.id)));
    const after = store.useConfigStore.getState().config;
    equal("V5 视图顺序=服务端视图 → localOnly → preset 队尾", after.channels.map((channel) => channel.id), ["own-1", "tpl-jx", "wan-ch", "tpl-sdgf", "tpl-ollama"]);
    check("V3 自建渠道原样保留（key 仍在）", after.channels[0].apiKey === "***1234", after.channels[0].apiKey);
    check("V5 模板渠道本地可见但模型不可选（仅本地）", !after.models.some((model) => model.startsWith("tpl-jx::") || model.startsWith("tpl-sdgf::")), JSON.stringify(after.models));
    check("V5 提示点名模板合入/仅本地保留", String(snap.notice).includes("聚鑫"), snap.notice);
    equal("V5 版本落地", readMeta().lastPresetVersion, V1_VERSION);
}

// ======================================================================================
// V6（登录）：版本相同 → mergePresetLayer 零改动、config 引用相等、零 PUT
// ======================================================================================
{
    const currentChannels = store.useConfigStore.getState().config.channels;
    const layer = sync.mergePresetLayer(currentChannels);
    equal("V6 登录：版本相同 mergePresetLayer 零改动（channels 引用原样返回）", [layer.changed, layer.channels === currentChannels], [false, true]);

    // 真实 boot：服务端视图与本地缓存完全一致 + 版本相同 → 不写 store、不排 PUT、不 reload
    resetAll();
    seedMeta({ lastPresetVersion: V1_VERSION });
    const stable = [
        ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: "***1234" }),
        ch({ id: "wan-ch", name: "万相", baseUrl: "http://127.0.0.1:8779", apiFormat: "wan", models: [{ name: "wan-x", capability: "video" }] }),
    ];
    setStore(stable, { channelMode: "remote" });
    routeRemote({ server: [maskedOf(ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: "sk-own-1234" }))] });
    await bootRemote();
    const refBefore = store.useConfigStore.getState().config;
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    await bootRemote();
    const refAfter = store.useConfigStore.getState().config;
    equal("V6 登录：版本相同 config 引用相等（subscribe 不触发写入）", refAfter === refBefore, true);
    equal("V6 登录：版本相同零 PUT", putBodies().length, 0);
    equal("V6 登录：版本相同零 reload", shim.calls.reload, 0);
    equal("V6 登录：版本比对仍发 1 次 GET", shim.calls.fetch.filter((call) => call.url === BOOT).length, 1);
}

// ======================================================================================
// V9：升格 mint（迁移入口）+ 幂等 + 七处引用重写
// ======================================================================================
resetAll();
{
    const local = [
        ch({
            id: "tpl-jx",
            name: "聚鑫",
            baseUrl: "https://sdapi.jxinai.com",
            apiKey: "sk-live-template",
            apiFormat: "sdgf",
            models: [{ name: "seedance-1", capability: "video" }],
        }),
    ];
    seedMeta({ lastPresetVersion: "" });
    setStore(local, {
        model: "tpl-jx::seedance-1",
        imageModel: "tpl-jx::seedance-1",
        videoModel: "tpl-jx::seedance-1",
        textModel: "tpl-jx::seedance-1",
        audioModel: "tpl-jx::seedance-1",
        lapianModels: ["tpl-jx::seedance-1"],
        lapianDefaultModel: "tpl-jx::seedance-1",
    });
    routeRemote({ server: [] });
    await bootRemote();

    const alias = readMeta().aliases["tpl-jx"];
    check("V9 mint 落盘 alias（templateId → mintedId）", Boolean(alias) && alias !== "tpl-jx", JSON.stringify(readMeta().aliases));
    const firstPut = putBodies()[0];
    equal("V9 PUT 用的是 mintedId（不是模板共享 id）", firstPut.channels.map((channel) => channel.id), [alias]);
    check("V9 V5：模板 id 未出现在服务端提交体", !firstPut.channels.some((channel) => templateChannels().some((template) => template.id === channel.id)), JSON.stringify(firstPut.channels.map((c) => c.id)));

    const config = store.useConfigStore.getState().config;
    equal("V9 渠道 id 已换新", config.channels.some((channel) => channel.id === alias), true);
    equal("V9 config 七处引用全部换新", [config.model, config.imageModel, config.videoModel, config.textModel, config.audioModel, config.lapianModels, config.lapianDefaultModel].map((value) => (Array.isArray(value) ? value : [value]).every((ref) => ref.startsWith(`${alias}::`))).every(Boolean), true);

    // 连续两次 flush：id 不变（alias 命中不再 mint）
    shim.calls.fetch.length = 0;
    store.useConfigStore.getState().updateConfig("channels", store.useConfigStore.getState().config.channels.map((channel) => ({ ...channel, name: "改个名" })));
    await settle(60);
    await sync.flushChannelSync();
    const secondPut = putBodies().pop();
    equal("V9 连续两次 flush：服务端行 id 不变", secondPut.channels.map((channel) => channel.id), [alias]);
    equal("V9 alias 仍只有一条（没有二次 mint）", Object.keys(readMeta().aliases), ["tpl-jx"]);

    // 画布/项目侧引用：decode 处 lazy 自愈
    equal("V9 画布 decode 自愈（store.decodeChannelModel）", store.decodeChannelModel("tpl-jx::seedance-1").channelId, alias);
    equal("V9 画布 decode 自愈（lapian-model）", lapian.decodeChannelModelRef("tpl-jx::seedance-1").channelId, alias);
    equal("V9 normalizeModelOptionValue 回写新引用", store.normalizeModelOptionValue("tpl-jx::seedance-1", store.useConfigStore.getState().config.channels), `${alias}::seedance-1`);
    equal("V9 请求上下文按新 id 路由", store.resolveModelRequestConfig(store.useConfigStore.getState().config, "tpl-jx::seedance-1").channelId, alias);
}

// ======================================================================================
// V9（store 写入边界）：给模板渠道填 key → 立即 mint（不经过迁移/分拣）
// **真防抖时序**（B2.5 IF7）：boot 用默认 800ms 防抖，填 key 后只等防抖窗口自然到期，
// 不再手调 flushChannelSync —— 手调会把「防抖定时器根本没排上/排错批次」的缺陷整段掩盖。
// ======================================================================================
resetAll();
{
    const local = [ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: KEY_OWN })];
    seedMeta({ lastPresetVersion: V1_VERSION });
    setStore(local);
    // 模板换版本：合并必须真正发生，模板渠道才会落到本地
    routeRemote({ server: [maskedOf({ ...local[0] })], presetVersion: "beef0003" });
    // 不传 debounceMs → 用真实 SAVE_DEBOUNCE_MS
    await sync.bootstrapChannelSync({ timeoutMs: 2000 });
    await settle();
    const merged = store.useConfigStore.getState().config.channels;
    check("写入边界：模板渠道已合入且无 key", merged.some((channel) => channel.id === "tpl-sdgf" && !channel.apiKey), JSON.stringify(merged.map((c) => [c.id, c.apiKey])));
    equal("写入边界：防抖窗口确实是默认 800ms", sync.SAVE_DEBOUNCE_MS, 800);

    shim.calls.fetch.length = 0;
    store.useConfigStore.getState().updateConfig("channels", store.useConfigStore.getState().config.channels.map((channel) => (channel.id === "tpl-sdgf" ? { ...channel, apiKey: KEY_NEW } : channel)));
    // 只等防抖（800ms）+ 余量；全程不调 flushChannelSync
    await settle(sync.SAVE_DEBOUNCE_MS + 200);
    const alias = readMeta().aliases["tpl-sdgf"];
    check("写入边界：填 key 即 mint（alias 落盘）", Boolean(alias), JSON.stringify(readMeta().aliases));
    equal("写入边界：store 里的渠道 id 已换新", store.useConfigStore.getState().config.channels.some((channel) => channel.id === alias), true);
    const bodies = putBodies();
    check("写入边界：防抖到期后 PUT 自己发出（没有手调 flush）", bodies.length > 0, JSON.stringify(bodies.map((body) => body.channels.map((c) => c.id))));
    check("写入边界：PUT 只带新 id（模板 id 不上服务端）", bodies.length > 0 && bodies.every((body) => !body.channels.some((channel) => templateChannels().some((template) => template.id === channel.id))), JSON.stringify(bodies.map((body) => body.channels.map((c) => c.id))));
    check("写入边界：PUT 含 mintedId", bodies.some((body) => body.channels.some((channel) => channel.id === alias)), JSON.stringify(bodies.map((body) => body.channels.map((c) => c.id))));
    equal("写入边界：mint 恰一次（alias 只有一条）", Object.keys(readMeta().aliases), ["tpl-sdgf"]);
}


// ======================================================================================
// V2（登录模式 masked）：服务端视图里的渠道永不被模板覆盖
// ======================================================================================
resetAll();
{
    const serverChannel = { id: "minted-1", name: "服务端旧名", baseUrl: "https://server-old.example.com", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }], apiKey: "***9999" };
    seedMeta({ lastPresetVersion: "", aliases: { "tpl-jx": "minted-1" } });
    setStore([ch({ id: "minted-1", name: "服务端旧名", baseUrl: "https://server-old.example.com", apiKey: "***9999", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }] })]);
    routeRemote({ server: [serverChannel], presetVersion: "beef0001" });
    await bootRemote();
    const channel = store.useConfigStore.getState().config.channels.find((item) => item.id === "minted-1");
    equal("V2 登录 masked：name 不动", channel.name, "服务端旧名");
    equal("V2 登录 masked：baseUrl 不动", channel.baseUrl, "https://server-old.example.com");
    equal("V2 登录 masked：key 保持 masked", channel.apiKey, "***9999");
    equal("V2 登录 masked：models 不动", channel.models.map((model) => model.name), ["seedance-1"]);
    equal("V2 登录 masked：没有按共享 id 再灌一条重复渠道", store.useConfigStore.getState().config.channels.filter((item) => item.id === "tpl-jx").length, 0);
}

// ======================================================================================
// V4（登录模式）：删模板渠道记墓碑，改版本后不复活
// ======================================================================================
resetAll();
{
    seedMeta({ lastPresetVersion: V1_VERSION });
    setStore([ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: "***1234" })], { channelMode: "remote" });
    routeRemote({ server: [maskedOf(ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com", apiKey: "sk-own-1234" }))], presetVersion: "beef0004" });
    await bootRemote();
    check("V4 模板渠道先合入", store.useConfigStore.getState().config.channels.some((channel) => channel.id === "tpl-ollama"), JSON.stringify(store.useConfigStore.getState().config.channels.map((c) => c.id)));

    // 用户在展示层删掉模板渠道 → 写入边界记墓碑
    store.useConfigStore.getState().updateConfig("channels", store.useConfigStore.getState().config.channels.filter((channel) => channel.id !== "tpl-ollama"));
    await settle(60);
    equal("V4 删除模板渠道记入 preset_meta.tombstones", readMeta().tombstones.includes("tpl-ollama"), true);

    // 模板换版本 → 该渠道不再复活
    routeRemote({ server: [], presetVersion: "beef0002" });
    shim.calls.fetch.length = 0;
    await bootRemote();
    equal("V4 新版本下 tombstone 渠道不复活", store.useConfigStore.getState().config.channels.some((channel) => channel.id === "tpl-ollama"), false);
}

// ======================================================================================
// 纯函数层：四分支 / 同名 strip 口径 / trim 空名 / alias 不重复合入 / 七处重写
// ======================================================================================
{
    const local = [
        ch({ id: "tpl-jx", name: "聚鑫", apiKey: "sk-keep" }), // key 非空 → 永不动
        ch({ id: "tpl-sdgf", name: "SDGF", apiKey: "" }), // 空 key → 跟随刷新
        ch({ id: "own-1", name: "自建" }), // 不在模板 → 零接触
        ch({ id: "tomb", name: "删过的" }), // tombstone
    ];
    const preset = [
        ch({ id: "tpl-jx", name: "聚鑫（新）", baseUrl: "https://new-jx.example.com" }),
        ch({ id: "tpl-sdgf", name: "SDGF（新）", baseUrl: "https://new-sdgf.example.com" }),
        ch({ id: "tomb", name: "删过的（新）" }),
        ch({ id: "fresh", name: "新模板渠道", baseUrl: "https://fresh.example.com" }),
        ch({ id: "dupe", name: "  自建  " }), // 同名（strip 后比较）→ 跳过合入
        ch({ id: "blank", name: "   " }), // trim 空名 → 跳过
    ];
    const merged = merge.mergePresetChannels(local, preset, { tombstones: ["tomb"] });
    equal("§2.2 四分支：合入队尾 / 跟随刷新 / 永不动 / 跳过", [merged.added, merged.updated, merged.skippedTombstones], [["fresh"], ["tpl-sdgf"], ["tomb"]]);
    equal("§2.2 BYOK：填过 key 的渠道内容逐字节不动", merged.channels.find((channel) => channel.id === "tpl-jx").name, "聚鑫");
    equal("§2.2 空 key 跟随刷新", merged.channels.find((channel) => channel.id === "tpl-sdgf").baseUrl, "https://new-sdgf.example.com");
    equal("§2.2 同名（strip 后）跳过合入并点名", merged.skippedSameName, ["dupe"]);
    equal("§2.2 trim 空名跳过（服务端整批 422）", merged.skippedEmptyName, ["blank"]);
    equal("§2.2 自建渠道零接触", merged.channels.some((channel) => channel.id === "own-1" && channel.name === "自建"), true);

    const aliased = merge.mergePresetChannels([ch({ id: "minted-1", apiKey: "sk-x" })], [ch({ id: "tpl-jx", name: "聚鑫" })], { aliases: { "tpl-jx": "minted-1" } });
    equal("§2.3 alias 命中：不再按共享 id 重复合入", [aliased.added, aliased.channels.length], [[], 1]);
    const aliasedMissing = merge.mergePresetChannels([], [ch({ id: "tpl-jx", name: "聚鑫" })], { aliases: { "tpl-jx": "minted-1" } });
    equal("§2.3 alias 存在但本地渠道已删：不复活共享 id", [aliasedMissing.added, aliasedMissing.skippedAliased], [[], ["tpl-jx"]]);

    const config = { model: "tpl-jx::m", imageModel: "tpl-jx::m", videoModel: "a::m", textModel: "tpl-jx::t", audioModel: "tpl-jx::a", lapianModels: ["tpl-jx::l", "other::l"], lapianDefaultModel: "tpl-jx::l", videoSeconds: "6" };
    const rewritten = merge.rewriteChannelRefs(config, { "tpl-jx": "minted-9" });
    equal("§2.3 七处引用重写", [rewritten.model, rewritten.imageModel, rewritten.videoModel, rewritten.textModel, rewritten.audioModel, rewritten.lapianModels, rewritten.lapianDefaultModel], ["minted-9::m", "minted-9::m", "a::m", "minted-9::t", "minted-9::a", ["minted-9::l", "other::l"], "minted-9::l"]);
    equal("§2.3 其它字段不动", rewritten.videoSeconds, "6");
}

// ======================================================================================
// B2.5 BL：提交前判重口径必须与服务端 UNIQUE(user_id,name) 一致（strip 后 BINARY 完全相等）
//  ① 大小写不同不是重名——两条都进提交体，只进提示；
//  ② 重名组里任一条是服务端已有行 → 整批不发 PUT（整集替换会把服务端那一行静默删掉）+ 错误通道点名；
//  ③ 只有纯本地草稿互相重名才单条跳过（它们本来就不在服务端表里，跳过不删任何行）。
// ======================================================================================
{
    const dupOfServer = sync.dedupeSavableChannels([ch({ id: "srv-1", name: "聚鑫" }), ch({ id: "draft-1", name: "聚鑫" })], ["srv-1"]);
    equal("BL② 重名组含服务端已有行 → 整组进 conflicts（savable/duplicates 皆空）", [dupOfServer.savable.length, dupOfServer.duplicates.length], [0, 0]);
    equal("BL② conflicts 组员逐条点名", dupOfServer.conflicts.map((c) => c.id), ["srv-1", "draft-1"]);

    const dupLocal = sync.dedupeSavableChannels([ch({ id: "draft-a", name: "新渠道" }), ch({ id: "draft-b", name: "新渠道" })], []);
    equal("BL③ 纯本地草稿重名 → 单条跳过（其余照常提交）", [dupLocal.savable.map((c) => c.id), dupLocal.duplicates.map((c) => c.id), dupLocal.conflicts.length], [["draft-a"], ["draft-b"], 0]);

    const casePair = sync.dedupeSavableChannels([ch({ id: "n1", name: "MyChan" }), ch({ id: "n2", name: "mychan" })], []);
    equal("BL① 大小写不同不是重名：两条都进提交体", [casePair.savable.map((c) => c.id), casePair.duplicates.length], [["n1", "n2"], 0]);
    equal("BL① 大小写不同只进提示（caseVariants）", casePair.caseVariants.map((c) => c.id), ["n1", "n2"]);

    const spaceDup = sync.dedupeSavableChannels([ch({ id: "s1", name: "聚鑫" }), ch({ id: "s2", name: " 聚鑫 " })], []);
    equal("BL① strip 后相等才算重名（服务端两边都 strip）", [spaceDup.savable.map((c) => c.id), spaceDup.duplicates.map((c) => c.id)], [["s1"], ["s2"]]);
}

// ---- 端到端 ①：重名撞服务端已有行 → 零 PUT、服务端行数不变、错误通道点名
resetAll();
{
    const serverRows = [
        maskedOf(ch({ id: "srv-1", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }] })),
        maskedOf(ch({ id: "srv-2", name: "另一个中转", baseUrl: "https://other.example.com" })),
    ];
    const serverSnapshot = JSON.stringify(serverRows);
    seedMeta({ lastPresetVersion: V1_VERSION });
    setStore(serverRows.map((row) => store.createModelChannel(row)), { channelMode: "remote" });
    shim.setRoute(async (url, init) => {
        if (url === AUTH) return globalThis.__jsonResponse(200, { username: "alice" });
        if (url === BOOT) return globalThis.__jsonResponse(200, { channels: templateChannels(), presetVersion: V1_VERSION });
        if (url === CHANNELS && init && init.method === "PUT") return globalThis.__jsonResponse(200, { channels: JSON.parse(init.body).channels.map(maskedOf) });
        if (url === CHANNELS) return globalThis.__jsonResponse(200, { channels: serverRows });
        return globalThis.__jsonResponse(500, {});
    });
    await bootRemote();
    // 用户新建一个与账号里已有「聚鑫」同名的渠道 → 整个待提交批次里出现同名两条（一条是服务端已有行）
    const withDup = [...store.useConfigStore.getState().config.channels, ch({ id: "local-dup", name: "聚鑫", baseUrl: "https://dup.example.com", apiKey: KEY_NEW })];
    store.useConfigStore.getState().updateConfig("channels", withDup);
    await settle(150);
    equal("BL② 服务端已有行被重名卷入：整批零 PUT", putBodies().length, 0);
    equal("BL② 服务端行数不变（零删除）", JSON.stringify(serverRows), serverSnapshot);
    check("BL② 错误通道显式报错并点名", String(sync.channelSyncSnapshot().error).includes("聚鑫") && String(sync.channelSyncSnapshot().error).includes("整批"), sync.channelSyncSnapshot().error);
    check("BL② 本地两条同名渠道都还在（不静默删本地行）", store.useConfigStore.getState().config.channels.filter((c) => c.name === "聚鑫").length >= 2, JSON.stringify(store.useConfigStore.getState().config.channels.map((c) => [c.id, c.name])));
    equal("BL② 卸载回写同样拒发（keepalive 也不许删行）", [sync.buildUnloadPayload(withDup).ok, sync.buildUnloadPayload(withDup).reason], [false, "conflict"]);
}

// ---- 端到端 ②：仅大小写不同的一对 → 两条都进提交体（服务端 BINARY 收得下）
resetAll();
{
    seedMeta({ lastPresetVersion: V1_VERSION });
    const baseRow = maskedOf(ch({ id: "n1", name: "MyChan", baseUrl: "https://a.example.com" }));
    setStore([store.createModelChannel(baseRow)], { channelMode: "remote" });
    routeRemote({ server: [baseRow], presetVersion: V1_VERSION });
    await bootRemote();
    store.useConfigStore.getState().updateConfig("channels", [...store.useConfigStore.getState().config.channels, ch({ id: "n2", name: "mychan", baseUrl: "https://b.example.com", apiKey: KEY_NEW })]);
    await settle(150);
    const body = putBodies().pop();
    const ids = body ? body.channels.map((channel) => channel.id).filter((id) => id === "n1" || id === "n2") : [];
    equal("BL① 大小写不同的一对都进提交体", ids, ["n1", "n2"]);
    check("BL① 提示点明大小写（不进 duplicates/错误通道）", String(sync.channelSyncSnapshot().error).includes("大小写"), sync.channelSyncSnapshot().error);
}

// ======================================================================================
// IF2：登录模式同样让内置占位渠道让位（与单人路径共用 dropPlaceholder 回调）
// ======================================================================================
resetAll();
{
    seedMeta({ lastPresetVersion: "" });
    setStore([ch({ id: "default", name: "默认渠道", baseUrl: "https://api.openai.com", apiKey: "", models: [{ name: "gpt-image-2", capability: "image" }] })]);
    routeRemote({ server: [], presetVersion: "cafe1001" });
    const snap2 = await bootRemote();
    const ids = store.useConfigStore.getState().config.channels.map((channel) => channel.id);
    equal("IF2 登录模式：内置占位渠道让位", ids.includes("default"), false);
    equal("IF2 登录模式：模板渠道全部到位", ids, ["tpl-jx", "tpl-sdgf", "tpl-ollama"]);
    check("IF2/P1 提示点名让位的占位渠道", String(snap2.notice).includes("默认渠道"), snap2.notice);
}

// ======================================================================================
// IF3：单人 rebuildChannelDomain 解析为空 → 回退「该能力的第一个可用模型」（与首启同口径）
// ======================================================================================
{
    const channels = [
        store.createModelChannel({
            id: "c1",
            name: "新渠道",
            baseUrl: "https://x.example.com",
            apiFormat: "openai",
            models: [
                { name: "img-1", capability: "image" },
                { name: "vid-1", capability: "video" },
                { name: "txt-1", capability: "text" },
            ],
        }),
    ];
    const stale = {
        ...store.defaultConfig,
        channels: [store.createModelChannel({ id: "gone", name: "删掉的渠道", baseUrl: "https://g.example.com", apiFormat: "openai", models: [] })],
        model: "gone::video-model-1",
        imageModel: "gone::img-1",
        videoModel: "gone::vid-1",
        textModel: "gone::txt-1",
        audioModel: "",
    };
    const rebuilt = bootstrap.rebuildChannelDomain(stale, channels);
    equal("IF3 解析为空按能力回退（image）", rebuilt.imageModel, "c1::img-1");
    equal("IF3 解析为空按能力回退（video）", rebuilt.videoModel, "c1::vid-1");
    equal("IF3 解析为空按能力回退（text）", rebuilt.textModel, "c1::txt-1");
    equal("IF3 model 回退按 guessCapability 推能力（video-model-1→video）", rebuilt.model, "c1::vid-1");
    equal("IF3 用户本来就没选的 audio 仍留空（不凭空选）", rebuilt.audioModel, "");
}

// ======================================================================================
// IF4：设备 B 没有 alias → 同名同址认领 alias，画布旧引用在 B 上 lazy 自愈
// ======================================================================================
{
    const claimed = merge.mergePresetChannels([ch({ id: "minted-on-A", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com" })], [ch({ id: "tpl-jx", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com" })], {});
    equal("IF4 同名+同址认领 alias", claimed.aliasClaims, [{ from: "tpl-jx", to: "minted-on-A" }]);
    equal("IF4 同名+同址仍按同名跳过（不灌第二条）", claimed.skippedSameName, ["tpl-jx"]);
    const otherAddr = merge.mergePresetChannels([ch({ id: "minted-on-A", name: "聚鑫", baseUrl: "https://another.example.com" })], [ch({ id: "tpl-jx", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com" })], {});
    equal("IF4 不同址不认领（防误映射）", otherAddr.aliasClaims, []);
}
resetAll();
{
    seedMeta({ lastPresetVersion: "", aliases: {} });
    const serverRow = maskedOf(ch({ id: "minted-on-A", name: "聚鑫", baseUrl: "https://sdapi.jxinai.com", apiFormat: "sdgf", models: [{ name: "seedance-1", capability: "video" }] }));
    setStore([]);
    routeRemote({ server: [serverRow], presetVersion: "cafe2002" });
    await bootRemote();
    equal("IF4 设备 B：登录合并后 alias 落盘", readMeta().aliases["tpl-jx"], "minted-on-A");
    equal("IF4 设备 B：画布旧引用 lazy 自愈", store.decodeChannelModel("tpl-jx::seedance-1").channelId, "minted-on-A");
}

// ======================================================================================
// IF5：mint 换 id 不能被预置 watch 误记成「用户删除」
// ======================================================================================
resetAll();
{
    seedMeta({ lastPresetVersion: V1_VERSION });
    setStore([], { channelMode: "remote" });
    routeRemote({ server: [maskedOf(ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com" }))], presetVersion: "cafe3003" });
    await bootRemote();
    check("IF5 前置：模板渠道已在本机", store.useConfigStore.getState().config.channels.some((c) => c.id === "tpl-ollama"), JSON.stringify(store.useConfigStore.getState().config.channels.map((c) => c.id)));
    const tombstonesBefore = readMeta().tombstones;
    sync.mintTemplateIdsBeforePut([ch({ id: "tpl-ollama", name: "Ollama Cloud", baseUrl: "https://ollama.com", apiKey: KEY_OWN })]);
    await settle(60);
    equal("IF5 mint 换 id 不被误记为用户删除（tombstones 不变）", readMeta().tombstones, tombstonesBefore);
    check("IF5 mint 后 store 换新 id", store.useConfigStore.getState().config.channels.some((c) => c.id === readMeta().aliases["tpl-ollama"]), JSON.stringify(store.useConfigStore.getState().config.channels.map((c) => c.id)));
}

// ======================================================================================
// P2：commitPresetVersion 版本相同不 setItem（V6「零写入」的毛边）
// ======================================================================================
resetAll();
{
    writeLog.length = 0;
    seedMeta({ lastPresetVersion: V1_VERSION });
    const row = maskedOf(ch({ id: "own-1", name: "我自己的中转", baseUrl: "https://mine.example.com" }));
    setStore([store.createModelChannel(row)], { channelMode: "remote" });
    routeRemote({ server: [row], presetVersion: V1_VERSION });
    await bootRemote();
    equal("P2 版本相同的启动：preset_meta 一次都不写", writeLog.filter((key) => key === META_KEY).length, 0);
}

// ======================================================================================
// V8：既有探针不红（白名单两处已同步更新）
// ======================================================================================
{
    for (const probe of ["probe-bootstrap-config.mjs", "probe-s2-b3-channel-sync.mjs"]) {
        const run = spawnSync(process.execPath, [path.join(root, "scripts", probe)], { encoding: "utf8", cwd: root });
        check(`V8 既有探针 ${probe} 退出码 0`, run.status === 0, `${run.status} / ${(run.stdout || "").trim().split("\n").slice(-3).join(" | ")} ${(run.stderr || "").trim().split("\n").slice(-2).join(" | ")}`);
    }
}

console.warn = silent.warn;
console.info = silent.info;

console.log(`=== S3.5-B2 预置模板版本化探针：${passed} passed / ${failures.length} failed ===`);
for (const item of failures) console.log(`FAIL | ${item}`);
console.log(`EXIT_CODE=${failures.length ? 1 : 0}`);
process.exit(failures.length ? 1 : 0);
