/**
 * 首启预置导入探针（免费，不连 bridge、不花钱）。
 *
 * esbuild 打包真模块 src/lib/config/bootstrap-config.ts（含 zustand persist 依赖链），
 * node 里 shim window/localStorage/fetch/location 后跑行为断言：
 *   node scripts/probe-bootstrap-config.mjs
 * 任何一条不成立就退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-bootstrap-config-"));
const target = path.join(root, "src", "lib", "config", "bootstrap-config.ts");

// shim 必须先于目标模块求值（ESM import 按序求值）——window/localStorage/fetch/location 全部可控。
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `export const storage = new Map();
export const calls = { fetch: [], reload: 0, info: [] };
globalThis.localStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
};
globalThis.window = globalThis;
globalThis.location = { reload: () => { calls.reload += 1; } };
globalThis.fetch = async (url) => {
    calls.fetch.push(String(url));
    const next = globalThis.__nextFetchResponse;
    if (typeof next === "function") return next();
    return next;
};
`,
);
writeFileSync(
    path.join(workdir, "entry.mjs"),
    `import "./shim.mjs";
import { maybeImportBootstrapConfig } from ${JSON.stringify(target.replace(/\\/g, "/"))};
export { maybeImportBootstrapConfig };
`,
);

const outfile = path.join(workdir, "bundle.mjs");
await build({
    entryPoints: [path.join(workdir, "entry.mjs")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    logLevel: "silent",
    alias: { "@": path.join(root, "src") },
});

const { maybeImportBootstrapConfig } = await import(pathToFileURL(outfile).href);
const shim = await import(pathToFileURL(path.join(workdir, "shim.mjs")));

const KEY = "infinite-canvas:ai_config_store";

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

const infoLogs = [];
const originalInfo = console.info;
console.info = (...args) => {
    infoLogs.push(args.map(String).join(" "));
};

// 每个场景重置 shim 状态
function reset({ existingKey = null, respond = null } = {}) {
    shim.storage.clear();
    shim.calls.fetch.length = 0;
    shim.calls.reload = 0;
    infoLogs.length = 0;
    globalThis.__nextFetchResponse = respond;
    if (existingKey !== null) shim.storage.set(KEY, existingKey);
}

// ---------------------------------------------------------------------------
// 1. 幂等：key 已存在 → 不 fetch、不写、不 reload
// ---------------------------------------------------------------------------
reset({ existingKey: '{"state":{"config":{"channels":[]}},"version":0}' });
await maybeImportBootstrapConfig();
equal("1. key 存在时不 fetch", shim.calls.fetch, []);
check("1. key 存在时不写", !shim.storage.has(KEY) || shim.storage.get(KEY).includes('"channels":[]'), "原值被改动");
equal("1. key 存在时不 reload", shim.calls.reload, 0);

// ---------------------------------------------------------------------------
// 2. 404 / 网络失败 / 坏载荷 → 静默放弃
// ---------------------------------------------------------------------------
reset({ respond: { ok: false, status: 404 } });
await maybeImportBootstrapConfig();
equal("2. 404 不写", Array.from(shim.storage.keys()), []);
equal("2. 404 不 reload", shim.calls.reload, 0);

reset({ respond: () => Promise.reject(new Error("offline")) });
let threw = false;
try {
    await maybeImportBootstrapConfig();
} catch {
    threw = true;
}
check("2. 网络失败不抛出", !threw);
equal("2. 网络失败不写", Array.from(shim.storage.keys()), []);
equal("2. 网络失败不 reload", shim.calls.reload, 0);

reset({ respond: { ok: true, json: async () => ({}) } });
await maybeImportBootstrapConfig();
equal("2. 空载荷不写", Array.from(shim.storage.keys()), []);

reset({ respond: { ok: true, json: async () => ({ channels: [] }) } });
await maybeImportBootstrapConfig();
equal("2. 空渠道列表不写", Array.from(shim.storage.keys()), []);

// ---------------------------------------------------------------------------
// 3. 正常导入：写回 persist 结构、渠道归一、模型选择指向预置渠道、reload 一次
// ---------------------------------------------------------------------------
reset({
    respond: {
        ok: true,
        json: async () => ({
            channels: [
                {
                    id: "ch-alpha",
                    name: "Preset A",
                    baseUrl: "https://relay.example/v1/",
                    apiKey: "sk-test",
                    apiFormat: "sdgf",
                    models: [
                        { name: "video-s", capability: "video" },
                        { name: "image-s", capability: "image" },
                    ],
                },
                {
                    id: "ch-beta",
                    name: "Preset B",
                    baseUrl: "https://relay-b.example",
                    apiKey: "sk-b",
                    apiFormat: "bogus-format",
                    models: ["text-s", "tts-s"],
                },
                null,
                "junk-entry",
            ],
        }),
    },
});
await maybeImportBootstrapConfig();

equal("3. fetch 相对路径", shim.calls.fetch, ["/api/bootstrap-config"]);
equal("3. reload 恰一次", shim.calls.reload, 1);
equal("3. console.info 恰一行", infoLogs.length, 1);
check("3. info 含渠道数", infoLogs[0]?.includes("2 channel"), infoLogs[0] ?? "无日志");

const raw = shim.storage.get(KEY);
check("3. key 已写入", Boolean(raw));
const envelope = JSON.parse(raw);
equal("3. envelope 顶层键", Object.keys(envelope).sort(), ["state", "version"]);
equal("3. version=0", envelope.version, 0);
equal("3. state 字段", Object.keys(envelope.state).sort(), ["config", "webdav"]);

const config = envelope.state.config;
equal("3. 渠道数（垃圾元素丢弃）", config.channels.length, 2);
equal("3. 渠道 id 保留", config.channels.map((channel) => channel.id), ["ch-alpha", "ch-beta"]);
equal("3. baseUrl 按预置保留（尾斜杠由请求层 buildApiUrl 剥）", config.channels[0].baseUrl, "https://relay.example/v1/");
equal("3. 非法 apiFormat 归一为 openai", config.channels[1].apiFormat, "openai");
equal("3. sdgf apiFormat 保留", config.channels[0].apiFormat, "sdgf");
equal("3. 字符串模型猜能力", config.channels[1].models.map((model) => [model.name, model.capability]), [
    ["text-s", "text"],
    ["tts-s", "audio"],
]);
equal("3. models 汇总", config.models, ["ch-alpha::video-s", "ch-alpha::image-s", "ch-beta::text-s", "ch-beta::tts-s"]);
equal("3. imageModel 指向预置渠道", config.imageModel, "ch-alpha::image-s");
equal("3. videoModel 指向预置渠道", config.videoModel, "ch-alpha::video-s");
equal("3. textModel 指向预置渠道", config.textModel, "ch-beta::text-s");
equal("3. audioModel 指向预置渠道", config.audioModel, "ch-beta::tts-s");
equal("3. model 主选择回退 imageModel", config.model, "ch-alpha::image-s");
check("3. defaultConfig 其余字段保留（videoSeconds）", config.videoSeconds === "6", String(config.videoSeconds));
check("3. defaultConfig 其余字段保留（channelMode）", config.channelMode === "local", String(config.channelMode));
equal("3. webdav 为默认结构", envelope.state.webdav, {
    url: "",
    username: "",
    password: "",
    directory: "infinite-canvas",
    lastSyncedAt: "",
});

// ---------------------------------------------------------------------------
// 4. 导入后二次调用：不 fetch、不 reload（幂等闭环）
// ---------------------------------------------------------------------------
await maybeImportBootstrapConfig();
equal("4. 二次调用不 fetch", shim.calls.fetch, ["/api/bootstrap-config"]);
equal("4. 二次调用不追加 reload", shim.calls.reload, 1);

console.info = originalInfo;

if (failures.length) {
    console.error(`probe-bootstrap-config: ${failures.length} 项失败`);
    for (const failure of failures) console.error(`  ✗ ${failure}`);
    process.exit(1);
}
console.info(`probe-bootstrap-config: ${passed} 项断言全过`);
