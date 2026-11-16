/**
 * S2.6 探针：拆解/SDGF 请求体的渠道口径（**双态**：单人 local / 服务端渠道 remote）。
 *
 * 为什么要有这一条：remote 模式下前端只拿到 masked key（`***abcd`），旧装配把它当明文发给上游
 * → 上游必 401 → 前端防御分支干脆把渠道整条丢掉 → 服务端回落单租户 env 密钥（事故根因）。
 * S2.6 起 remote 只发 `{channelId, model}`，key 由服务端按登录用户渠道解密注入。
 *
 * 判据：
 *  W3 单人模式（local）请求体与基线**逐字节一致**（带明文 key 的 channel 对象，零新字段）；
 *  W6 remote 请求体**不含任何 key 字段**（apiKey/key 都不出现，字符串里也不许出现 `***`）；
 *  防御分支不再误丢 remote 渠道（masked key / 服务端视图 → 走 channelId，而不是零字段）；
 *  渠道已删 / 模型已改名 → 两态都零字段（T7-R 防御不因 S2.6 松动）；
 *  SDGF 提交点（video.ts → channel-proxy）remote 零鉴权头、改道 /api/proxy/<channelId>/…。
 *
 * 跑法：node scripts/probe-s2-6-lapian-channel.mjs   （纯逻辑 + 源码文本级断言，不连 bridge、不花钱）
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "src");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-s2-6-"));

/** i18n / store 在 import 期就要 window/localStorage：先求值 shim 再进目标模块（同 b35 探针口径）。 */
writeFileSync(
    path.join(workdir, "shim.mjs"),
    `const storage = new Map();
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

writeFileSync(
    path.join(workdir, "entry.ts"),
    `import "./shim.mjs";
export * as lapianModel from ${JSON.stringify(path.join(src, "lib/canvas/lapian-model.ts").replace(/\\/g, "/"))};
export * as lapianExec from ${JSON.stringify(path.join(src, "lib/canvas/lapian-execute.ts").replace(/\\/g, "/"))};
export * as channelProxy from ${JSON.stringify(path.join(src, "services/api/channel-proxy.ts").replace(/\\/g, "/"))};
`,
);

const { build } = await import("esbuild");
const outfile = path.join(workdir, "bundle.mjs");
await build({
    entryPoints: [path.join(workdir, "entry.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "browser",
    logLevel: "silent",
    alias: { "@": src },
    define: { "process.env.NODE_ENV": '"test"' },
});
const bundle = await import(pathToFileURL(outfile).href);
const mod = bundle.lapianModel;
const exec = bundle.lapianExec;
const proxy = bundle.channelProxy;

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
    check(name, JSON.stringify(actual) === JSON.stringify(expected), `期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
}

const ID = "task-1";
const INTENT = "重点看产品镜头";
/** 单人模式渠道：带明文 key（localStorage 形态） */
const LOCAL = [
    { id: "ch-a", name: "渠道甲", baseUrl: "https://api.example-a.com", apiKey: "sk-a", apiFormat: "gemini", models: [{ name: "vision-model-1" }, { name: "text-model" }] },
];
/** 服务端渠道视图：key 只有 masked（GET /api/user/channels 形态） */
const REMOTE = [
    { id: "ch-r", name: "渠道远", baseUrl: "https://api.example-r.com", apiKey: "***abcd", apiFormat: "gemini", models: [{ name: "vision-model-1" }] },
];

// --------------------------------------------------------------------------- 1. 单人模式逐字节不变（W3）
equal("local：渠道齐备 → {model, channel{baseUrl,key,model}}（与基线逐字节一致）",
    mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", LOCAL), {
        id: ID, intent: INTENT, model: "vision-model-1",
        channel: { baseUrl: "https://api.example-a.com", key: "sk-a", model: "vision-model-1" },
    });
equal("local：显式传 channelMode='local' 与缺省完全同形",
    mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", LOCAL, "local"),
    mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", LOCAL));
check("local：请求体不含 channelId 字段（旧契约零新字段）",
    !("channelId" in mod.buildLapianRunBody(ID, INTENT, "ch-a::vision-model-1", LOCAL)));
equal("local：未选模型 → 只 {id,intent}", mod.buildLapianRunBody(ID, INTENT, "", LOCAL), { id: ID, intent: INTENT });
equal("local：渠道无 key（bootstrap 预置）→ 零字段（不拿半截请求去撞上游）",
    mod.buildLapianRunBody(ID, INTENT, "ch-nokey::m", [{ id: "ch-nokey", name: "无key", baseUrl: "https://api.example.com", apiKey: "", apiFormat: "gemini", models: [{ name: "m" }] }]),
    { id: ID, intent: INTENT });

// --------------------------------------------------------------------------- 2. remote 零 key（W6）
const remoteBody = mod.buildLapianRunBody(ID, INTENT, "ch-r::vision-model-1", REMOTE, "remote");
equal("remote：只带 {channelId, model}，不带 baseUrl/key", remoteBody,
    { id: ID, intent: INTENT, model: "vision-model-1", channelId: "ch-r" });
const remoteText = JSON.stringify(remoteBody);
check("remote：请求体零 key 明文 / 零 masked（'***' 也不出现）", !remoteText.includes("***") && !remoteText.includes("abcd"), remoteText);
check("remote：请求体无 apiKey/key 字段", !("apiKey" in remoteBody) && !("key" in remoteBody) && !("channel" in remoteBody), remoteText);

// --------------------------------------------------------------------------- 3. masked 防御不再误丢 remote 渠道
equal("masked key 渠道（未显式给 channelMode）→ 仍走 channelId，不被防御分支丢弃",
    mod.buildLapianRunBody(ID, INTENT, "ch-r::vision-model-1", REMOTE),
    { id: ID, intent: INTENT, model: "vision-model-1", channelId: "ch-r" });
check("masked key 渠道：绝不把 masked 当明文发给上游",
    !JSON.stringify(mod.buildLapianRunBody(ID, INTENT, "ch-r::vision-model-1", REMOTE)).includes("***"));

// --------------------------------------------------------------------------- 4. T7-R 防御在两态都不松动
for (const mode of ["local", "remote"]) {
    const gone = mod.buildLapianRunBody(ID, INTENT, "ch-gone::m", LOCAL, mode);
    check(`${mode}：渠道已删 → 零字段（不发半截请求）`, !("model" in gone) && !("channel" in gone) && !("channelId" in gone));
    const renamed = mod.buildLapianRunBody(ID, INTENT, "ch-a::renamed-away", LOCAL, mode);
    check(`${mode}：模型已改名 → 零字段`, !("model" in renamed) && !("channel" in renamed) && !("channelId" in renamed));
    const bare = mod.buildLapianRunBody(ID, INTENT, "bare-model", LOCAL, mode);
    check(`${mode}：裸模型名（无渠道）→ 零字段（不猜渠道）`, !("model" in bare) && !("channelId" in bare));
}

// --------------------------------------------------------------------------- 5. 发起链：请求体真的这么发出去
function makeFetch() {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push({ url, init });
        return { json: async () => ({ job: "job-1" }) };
    };
    return { calls, fetchImpl };
}
const remoteCall = makeFetch();
const remoteRun = await exec.runLapianTask(
    { taskId: ID, intent: INTENT, config: { lapianDefaultModel: "ch-r::vision-model-1", channels: REMOTE, channelMode: "remote" } },
    { fetch: remoteCall.fetchImpl },
);
equal("runLapianTask remote：POST /api/lapian/run 体 = {id,intent,model,channelId}",
    JSON.parse(remoteCall.calls[0].init.body),
    { id: ID, intent: INTENT, model: "vision-model-1", channelId: "ch-r" });
check("runLapianTask remote：发出的 JSON 零 key", !remoteCall.calls[0].init.body.includes("***") && !remoteCall.calls[0].init.body.includes("apiKey"));
equal("runLapianTask remote：落盘模型引用非空（节点要记住选的模型）", remoteRun.lapianModel, "ch-r::vision-model-1");

const localCall = makeFetch();
const localRun = await exec.runLapianTask(
    { taskId: ID, intent: INTENT, config: { lapianDefaultModel: "ch-a::vision-model-1", channels: LOCAL, channelMode: "local" } },
    { fetch: localCall.fetchImpl },
);
equal("runLapianTask local：请求体与基线逐字节一致（明文 channel）",
    localCall.calls[0].init.body,
    JSON.stringify({ id: ID, intent: INTENT, model: "vision-model-1", channel: { baseUrl: "https://api.example-a.com", key: "sk-a", model: "vision-model-1" } }));
equal("runLapianTask local：lapianModel 口径不变", localRun.lapianModel, "ch-a::vision-model-1");

const emptyCall = makeFetch();
const emptyRun = await exec.runLapianTask(
    { taskId: ID, intent: INTENT, config: { lapianDefaultModel: "", channels: LOCAL, channelMode: "remote" } },
    { fetch: emptyCall.fetchImpl },
);
equal("runLapianTask 未选模型：只 {id,intent}（两态同形）", emptyCall.calls[0].init.body, JSON.stringify({ id: ID, intent: INTENT }));
equal("runLapianTask 未选模型：lapianModel 空（不写假引用）", emptyRun.lapianModel, "");

// --------------------------------------------------------------------------- 6. SDGF 提交点（video.ts → channel-proxy）remote 零 key
const remoteCfg = { baseUrl: "https://api.example-r.com", apiKey: "***abcd", apiFormat: "sdgf", channelMode: "remote", channelId: "ch-r", model: "ch-r::sd2.5-720p-ap" };
equal("SDGF remote：提交地址改道 /api/proxy/<channelId>/v1/videos",
    proxy.channelApiUrl(remoteCfg, "/videos"), "/api/proxy/ch-r/v1/videos");
equal("SDGF remote：鉴权头不带 Authorization（key 由服务端注入）",
    proxy.channelAuthHeaders(remoteCfg, "application/json"), { "Content-Type": "application/json" });
const localCfg = { baseUrl: "https://api.example-a.com", apiKey: "sk-a", apiFormat: "sdgf", channelMode: "local" };
equal("SDGF local：地址与基线一致（不走代理前缀）", proxy.channelApiUrl(localCfg, "/videos"), "https://api.example-a.com/v1/videos");
equal("SDGF local：鉴权头照旧带 Bearer 明文 key",
    proxy.channelAuthHeaders(localCfg, "application/json"), { Authorization: "Bearer sk-a", "Content-Type": "application/json" });

const videoSrc = readFileSync(path.join(src, "services/api/video.ts"), "utf8");
check("SDGF 提交点仍委托 channel-proxy（aiApiUrl→channelApiUrl / aiHeaders→channelAuthHeaders，S2.6 无独立 key 通道）",
    /function aiApiUrl\([\s\S]{0,200}channelApiUrl\(config, path\)/.test(videoSrc)
    && /function aiHeaders\([\s\S]{0,200}channelAuthHeaders\(config, contentType\)/.test(videoSrc));
check("SDGF 提交体（createSdgfVideoTask 的 body）不含 apiKey/key 字段",
    /const body: Record<string, unknown> = \{[\s\S]*?\n    \};/.test(videoSrc)
    && !/const body: Record<string, unknown> = \{[\s\S]*?\n    \};/.exec(videoSrc)[0].includes("apiKey"));

// --------------------------------------------------------------------------- 汇总
if (failures.length) {
    for (const line of failures) console.error(`[FAIL] ${line}`);
    console.log(`=== S2.6 拉片/SDGF 渠道探针：${passed} passed / ${failures.length} failed ===`);
    process.exitCode = 1;
} else {
    console.log(`=== S2.6 拉片/SDGF 渠道探针：${passed} passed / 0 failed ===`);
}
