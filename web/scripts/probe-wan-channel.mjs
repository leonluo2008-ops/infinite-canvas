/**
 * 万相渠道（wan）接入探针：provider 五值 / 渠道格式 / create body 拼装 / poll 三态映射 / file blob 拉取 / 刷新后重查。
 *
 * 直接转译真模块（esbuild 打包 src/services/api/video.ts + src/lib/canvas/video-task-wait.ts），
 * axios external 后在探针里替换传输层（node 里 axios 走 http 适配器，mock 打在 axios.post/get 上——
 * 不连 bridge、不花钱、不出网）；浏览器专属依赖（i18n/zustand store/localforage/image-storage）
 * 用忠实语义 stub 替身，被测逻辑（video.ts 的 wan 分支）是真代码。
 *   node scripts/probe-wan-channel.mjs
 * 任何一条不成立就退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import axios from "axios";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-wan-channel-"));

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
function readSrc(relativePath) {
    return readFileSync(path.join(root, relativePath), "utf8");
}

// ---------------------------------------------------------------------------
// localStorage 替身（在 import 被测模块前装好；wan 的 job→taskId 映射要活过"刷新"）
// ---------------------------------------------------------------------------
const storageData = new Map();
globalThis.localStorage = {
    getItem: (key) => (storageData.has(key) ? storageData.get(key) : null),
    setItem: (key, value) => storageData.set(key, String(value)),
    removeItem: (key) => storageData.delete(key),
    clear: () => storageData.clear(),
};

// ---------------------------------------------------------------------------
// 浏览器专属依赖替身：语义忠实，被测的 wan 分支不走它们的有状态路径
// ---------------------------------------------------------------------------
const STUBS = {
    "@/i18n": `const i18n = { t: (key) => key }; export default i18n;`,
    "@/stores/use-config-store": [
        `export function boolConfig(value, fallback) { return value ? value === "true" : fallback; }`,
        `export function buildApiUrl(baseUrl, path) { return baseUrl.replace(/\\/+$/, "") + "/v1" + path; }`,
        `export function modelOptionName(value) { const s = String(value); const i = s.indexOf("::"); return i >= 0 ? s.slice(i + 2) : s; }`,
        `export function resolveModelRequestConfig(config, value) { return { ...config, model: modelOptionName(value || config.model) }; }`,
        `export function resolveModelScript() { return ""; }`,
        `export function withLocalProxy(url) { return url; }`,
    ].join("\n"),
    "@/services/file-storage": [
        `export async function getMediaBlob() { throw new Error("stub: getMediaBlob"); }`,
        `export async function resolveMediaUrl() { throw new Error("stub: resolveMediaUrl"); }`,
        `export async function uploadMediaFile() { throw new Error("stub: uploadMediaFile"); }`,
    ].join("\n"),
    "@/services/image-storage": [
        `export async function imageToDataUrl(image) {`,
        `    if (typeof image?.dataUrl === "string" && image.dataUrl) return image.dataUrl;`,
        `    throw new Error("probe stub: 仅支持自带 dataUrl 的参考图");`,
        `}`,
    ].join("\n"),
    "./model-plugin": `export async function runModelPlugin() { throw new Error("stub: runModelPlugin"); }`,
};
const stubSuffixes = Object.keys(STUBS).map((name) => name.split("/").pop());
const stubPlugin = {
    name: "probe-wan-stub-deps",
    setup(builder) {
        const pattern = new RegExp(`^(?:@/(?:i18n|stores/use-config-store|services/file-storage|services/image-storage)|\\./model-plugin)$|src[/\\\\](?:i18n\\.ts|stores[/\\\\]use-config-store\\.ts|services[/\\\\]file-storage\\.ts|services[/\\\\]image-storage\\.ts)$`);
        builder.onResolve({ filter: pattern }, (args) => {
            const direct = STUBS[args.path];
            if (direct) return { path: args.path, namespace: "stub" };
            const hit = Object.keys(STUBS).find((name) => args.path.endsWith(name.replace(/^\.\//, "").split("/").pop()) || stubSuffixes.some((suffix) => args.path.endsWith(suffix)));
            return { path: hit || args.path, namespace: "stub" };
        });
        builder.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({ contents: STUBS[args.path] || `export default {};`, loader: "js" }));
    },
};

let buildSeq = 0;
async function loadVideoModule() {
    // bundle 落在 web/node_modules/.probe 下（node 才解析得到 external 的 axios）；?seq 换 URL = 新模块实例 = 模拟页面刷新
    const outfile = path.join(root, "node_modules", ".probe", `wan-video-${++buildSeq}.mjs`);
    await build({
        entryPoints: [path.join(root, "src", "services", "api", "video.ts")],
        outfile,
        bundle: true,
        format: "esm",
        platform: "node",
        logLevel: "silent",
        alias: { "@": path.join(root, "src") },
        external: ["axios"],
        plugins: [stubPlugin],
    });
    return import(pathToFileURL(outfile).href);
}
async function loadWaitModule() {
    const outfile = path.join(workdir, `video-task-wait-${++buildSeq}.mjs`);
    await build({ entryPoints: [path.join(root, "src", "lib", "canvas", "video-task-wait.ts")], outfile, bundle: true, format: "esm", platform: "node", logLevel: "silent", alias: { "@": path.join(root, "src") } });
    return import(pathToFileURL(outfile).href);
}

// ---------------------------------------------------------------------------
// axios 传输层替身：按路由应答，全量记录调用（URL/请求体）
// ---------------------------------------------------------------------------
function axiosErrorResponse(status, data) {
    return Object.assign(new Error(`mock http ${status}`), { isAxiosError: true, response: { status, data }, config: {} });
}
function installRoutes(routes) {
    const log = [];
    const find = (method, url) => routes.find((route) => route.method === method && (route.match instanceof RegExp ? route.match.test(url) : route.match === url));
    axios.post = async (url, body, config) => {
        log.push({ method: "POST", url, body, config });
        const route = find("POST", url);
        if (!route) throw new Error(`mock: POST 无路由 ${url}`);
        return { data: await route.reply({ url, body, config }) };
    };
    axios.get = async (url, config) => {
        log.push({ method: "GET", url, config });
        const route = find("GET", url);
        if (!route) throw new Error(`mock: GET 无路由 ${url}`);
        return { data: await route.reply({ url, config }) };
    };
    return log;
}
const mp4Blob = (bytes = [0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]) => new Blob([new Uint8Array(bytes)], { type: "video/mp4" });
const wanConfig = (overrides = {}) => ({
    baseUrl: "http://127.0.0.1:8779",
    apiKey: "",
    apiFormat: "wan",
    model: "chan-wan::wan-omni2video",
    videoSeconds: "6",
    size: "16:9",
    vquality: "720",
    videoGenerateAudio: undefined,
    videoWatermark: "false",
    videoMode: undefined,
    ...overrides,
});

const mod = await loadVideoModule();
const wait = await loadWaitModule();

// ---------------------------------------------------------------------------
// 1. 渠道格式（use-config-store / channel-editor-drawer 源码钉住）
// ---------------------------------------------------------------------------
const storeSrc = readSrc("src/stores/use-config-store.ts");
check("ApiCallFormat 含 wan", /export type ApiCallFormat = "openai" \| "gemini" \| "sdgf" \| "wan"/.test(storeSrc), "枚举缺 wan");
check("defaultBaseUrlForApiFormat(wan) 预填 127.0.0.1:8779", storeSrc.includes(`if (apiFormat === "wan") return "http://127.0.0.1:8779"`), "默认 bridge 地址缺失");
check("normalizeApiFormat 接受 wan（还原渠道不折叠回 openai）", /if \(apiFormat === "gemini" \|\| apiFormat === "sdgf" \|\| apiFormat === "wan"\) return apiFormat;/.test(storeSrc), "归一缺 wan");
check("isAiConfigReady：wan 渠道 key 可留空（不校验空）", storeSrc.includes(`match.channel.apiFormat === "wan" || match.channel.apiKey.trim()`), "wan key 豁免缺失");

const drawerSrc = readSrc("src/components/layout/channel-editor-drawer.tsx");
check("渠道编辑器下拉含 wan 选项（走 i18n）", drawerSrc.includes(`{ label: t("config.channelEditor.formats.wan"), value: "wan" }`), "下拉缺 wan");
check("选 wan 预填建议模型 wan-omni2video（视频能力）", drawerSrc.includes(`apiFormat === "wan" && !draft.models.length ? [{ name: "wan-omni2video", capability: "video" as const }]`), "模型预填缺失");

const zhSrc = readSrc("src/i18n/locales/zh-CN.ts");
const enSrc = readSrc("src/i18n/locales/en-US.ts");
check("zh：渠道格式「万相 CLI」", zhSrc.includes(`wan: "万相 CLI"`), "文案缺失");
check("en：渠道格式 Wan CLI", enSrc.includes(`wan: "Wan CLI"`), "文案缺失");
for (const key of ["wanBridgeRequired", "wanVideoCreateFailed", "wanVideoQueryFailed", "wanVideoFailed", "wanVideoNoJob", "wanVideoFileMissing"]) {
    check(`zh apiErrors 含 ${key}`, zhSrc.includes(`${key}: `), "文案缺失");
    check(`en apiErrors 含 ${key}`, enSrc.includes(`${key}: `), "文案缺失");
}

// ---------------------------------------------------------------------------
// 2. provider 五值透存不折叠（videoTaskProvider / 重查恢复）
// ---------------------------------------------------------------------------
const canvasTypes = readSrc("src/types/canvas.ts");
check("VideoTaskProvider 五值联合（openai/gemini/plugin/sdgf/wan）", /export type VideoTaskProvider = "openai" \| "gemini" \| "plugin" \| "sdgf" \| "wan";/.test(canvasTypes), "类型未扩五值");
for (const provider of ["openai", "gemini", "plugin", "sdgf", "wan"]) {
    equal(`storedVideoTaskProvider 透存 ${provider}`, wait.storedVideoTaskProvider(provider), provider);
}
equal("resumedVideoTaskProvider 恢复 wan", wait.resumedVideoTaskProvider("wan"), "wan");
equal("resumedVideoTaskProvider 脏数据回退 openai", wait.resumedVideoTaskProvider("junk"), "openai");
check("失败 wan 节点（videoTaskId+errorDetails）出重查入口", wait.canRecheckVideoTask({ type: "video", metadata: { videoTaskId: "job-1", errorDetails: "万相视频生成失败", status: "error" } }) === true, "重查入口未命中");

const videoSrc = readSrc("src/services/api/video.ts");
check("create 分派按 apiFormat === wan", videoSrc.includes(`if (requestConfig.apiFormat === "wan") return createWanVideoTask`), "create 分流缺失");
check("poll 分派按 provider === wan", videoSrc.includes(`if (task.provider === "wan")`), "poll 分流缺失");
check("wan 分流不走要求 apiKey 的 assertVideoConfig", /if \(task\.provider === "wan"\) \{[\s\S]{0,160}assertWanVideoConfig[\s\S]{0,160}\}[\s\S]{0,80}assertVideoConfig/.test(videoSrc), "wan 前置了 apiKey 校验");

// ---------------------------------------------------------------------------
// 3. create body 拼装（本机 bridge → vite proxy 相对路径）
// ---------------------------------------------------------------------------
const viteSrc = readSrc("vite.config.ts");
check("vite proxy 有 /api 规则（相对路径前提）", /"\/api"\s*:\s*"http:\/\/127\.0\.0\.1:8779"/.test(viteSrc), "proxy 规则缺失");

const refs = [
    { name: "香水.png", dataUrl: "data:image/png;base64,QUJD" },
    { name: "", dataUrl: "data:image/jpeg;base64,REVG" },
];
let log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({ job: "job-1", taskId: "file-1" }) }]);
let task = await mod.createVideoGenerationTask(wanConfig(), "一镜到底展示香水瓶", refs);
const runCall = log.find((entry) => entry.method === "POST");
check("run 走相对路径 /api/wan/video/run（vite proxy 同域）", runCall?.url === "/api/wan/video/run", `url ${runCall?.url}`);
equal("run 只发一次", log.length, 1);
const body = runCall?.body || {};
equal("body.prompt 原样透传", body.prompt, "一镜到底展示香水瓶");
equal("body.images 转 {name,dataUrl}（无名补 ref-N.png）", body.images, [
    { name: "香水.png", dataUrl: "data:image/png;base64,QUJD" },
    { name: "ref-2.png", dataUrl: "data:image/jpeg;base64,REVG" },
]);
equal("body.duration 取自 videoSeconds（数字）", body.duration, 6);
equal("body.ratio 取自 size", body.ratio, "16:9");
equal("body.resolution 取自 vquality（720→720p）", body.resolution, "720p");
equal("body.audio 缺省 true", body.audio, true);
equal("body.confirm 恒 true（两段式确认由画布流程承担）", body.confirm, true);
check("body 无 model/size/mode/aspect_ratio 等合同外字段", !["model", "size", "mode", "aspect_ratio", "watermark", "seconds"].some((key) => key in body), `多出字段 ${Object.keys(body).join(",")}`);
equal("run 返回 {job,taskId} → task.id=jobId（model 存编码原值，与 sdgf/openai 同口径）", task, { id: "job-1", provider: "wan", model: "chan-wan::wan-omni2video" });
// 空 apiKey 能创建成功（未报 apiKeyRequired）由上一条 equal 隐含证明——wan 分支不走要求 key 的 assertVideoConfig
const storedMap = JSON.parse(storageData.get("wan_video_file_tasks") || "{}");
check("job→taskId 映射已落 localStorage", storedMap["job-1"] === "file-1", `存储 ${storageData.get("wan_video_file_tasks")}`);

// 映射口径：SDGF 专用校验对 wan 不生效
log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({ job: "job-2", taskId: "file-2" }) }]);
task = await mod.createVideoGenerationTask(wanConfig({ videoSeconds: "40", size: "9:16", vquality: "high", videoGenerateAudio: "false" }), "x", []);
const body2 = log[0]?.body || {};
check("videoSeconds 40 不被 SDGF 4–30 硬拒（clamp 到上限即可）", body2.duration === 30, `duration ${body2.duration}`);
equal("size 9:16 → ratio 9:16", body2.ratio, "9:16");
equal("vquality high → 720p", body2.resolution, "720p");
equal("audio=false 透传", body2.audio, false);
log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({ job: "job-3", taskId: "file-3" }) }]);
await mod.createVideoGenerationTask(wanConfig({ size: "auto", vquality: "1080" }), "x", []);
equal("size auto → ratio 兜底 16:9", log[0]?.body?.ratio, "16:9");
equal("vquality 1080 → 1080p", log[0]?.body?.resolution, "1080p");
log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({ job: "job-4", taskId: "file-4" }) }]);
await mod.createVideoGenerationTask(wanConfig(), "文生视频无参考图", []);
equal("无参考图 images=[]（不报错）", log[0]?.body?.images, []);

// 回环/远程 bridge 地址 → 相对/绝对
log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({ job: "job-5", taskId: "file-5" }) }]);
await mod.createVideoGenerationTask(wanConfig({ baseUrl: "http://127.0.0.1:8779/" }), "x", []);
check("回环地址带尾斜杠仍走相对路径", log[0]?.url === "/api/wan/video/run", `url ${log[0]?.url}`);
log = installRoutes([{ method: "POST", match: /^http:\/\/10\.0\.0\.8:8779\/api\/wan\/video\/run$/, reply: () => ({ job: "job-6", taskId: "file-6" }) }]);
await mod.createVideoGenerationTask(wanConfig({ baseUrl: "http://10.0.0.8:8779" }), "x", []);
check("远程 bridge 走绝对地址直连", log[0]?.url === "http://10.0.0.8:8779/api/wan/video/run", `url ${log[0]?.url}`);

// create 失败透传（响应体 + 请求体快照；run 触发真实生成，状态不明勿重复提交）
log = installRoutes([
    {
        method: "POST",
        match: "/api/wan/video/run",
        reply: () => {
            throw axiosErrorResponse(500, { error: { message: "余额不足" } });
        },
    },
]);
let createError = null;
try {
    await mod.createVideoGenerationTask(wanConfig(), "一句提示词", refs);
} catch (error) {
    createError = error;
}
check("run 500 → 错误透传响应体（余额不足）", String(createError?.message).includes("余额不足"), createError?.message);
check("run 500 → 附响应体标记", String(createError?.message).includes("响应体"), "缺响应体段");
check("run 500 → 附请求体快照（prompt 截断标注）", String(createError?.message).includes("请求体") && String(createError?.message).includes("…(共5字)"), "缺请求体快照");
log = installRoutes([{ method: "POST", match: "/api/wan/video/run", reply: () => ({}) }]);
createError = null;
try {
    await mod.createVideoGenerationTask(wanConfig(), "x", []);
} catch (error) {
    createError = error;
}
equal("run 无 job → wanVideoNoJob 文案", createError?.message, "apiErrors.wanVideoNoJob");
createError = null;
try {
    await mod.createVideoGenerationTask(wanConfig({ baseUrl: "  " }), "x", []);
} catch (error) {
    createError = error;
}
equal("baseUrl 空 → wanBridgeRequired 文案", createError?.message, "apiErrors.wanBridgeRequired");

// ---------------------------------------------------------------------------
// 4. poll 三态映射（running→pending / failed→error 透传 / done→file blob 拉取）
// ---------------------------------------------------------------------------
const wanTask = { id: "job-1", provider: "wan", model: "wan-omni2video" };
log = installRoutes([{ method: "GET", match: "/api/wan/video/job/job-1", reply: () => ({ status: "running", stage: "提交远程生成", elapsed: 12 }) }]);
let state = await mod.pollVideoGenerationTask(wanConfig(), wanTask);
equal("running → pending", state, { status: "pending" });
check("job 查询走 job 端点（带 jobId）", log[0]?.url === "/api/wan/video/job/job-1", `url ${log[0]?.url}`);

log = installRoutes([{ method: "GET", match: "/api/wan/video/job/job-1", reply: () => ({ status: "failed", stage: "done", elapsed: 65, error: "生成端拉不到参考图" }) }]);
state = await mod.pollVideoGenerationTask(wanConfig(), wanTask);
equal("failed → error 透传上游原文", state, { status: "failed", error: "生成端拉不到参考图" });

log = installRoutes([{ method: "GET", match: "/api/wan/video/job/job-1", reply: () => ({ status: "failed" }) }]);
state = await mod.pollVideoGenerationTask(wanConfig(), wanTask);
equal("failed 无 error → 兜底文案", state.error, "apiErrors.wanVideoFailed");

log = installRoutes([
    { method: "GET", match: "/api/wan/video/job/job-1", reply: () => ({ status: "done", stage: "完成", elapsed: 120 }) },
    { method: "GET", match: "/api/wan/video/file/file-1", reply: () => mp4Blob() },
]);
state = await mod.pollVideoGenerationTask(wanConfig(), wanTask);
check("done → completed 且成片是 blob", state.status === "completed" && state.result?.blob instanceof Blob && state.result.blob.size > 0, `state ${JSON.stringify({ status: state.status })}`);
check("file 拉取用 taskId（不是 jobId）", log.some((entry) => entry.url === "/api/wan/video/file/file-1") && !log.some((entry) => entry.url === "/api/wan/video/file/job-1"), `调用 ${log.map((entry) => entry.url).join(" , ")}`);
check("poll 分派只打 wan 路由（provider 折叠会打 /videos → 无路由报错）", log.every((entry) => entry.url.includes("/api/wan/video/")), `调用 ${log.map((entry) => entry.url).join(" , ")}`);

log = installRoutes([{ method: "GET", match: "/api/wan/video/job/job-no-map", reply: () => ({ status: "done" }) }]);
state = null;
try {
    state = await mod.pollVideoGenerationTask(wanConfig(), { id: "job-no-map", provider: "wan", model: "wan-omni2video" });
} catch (error) {
    state = { thrown: error?.message };
}
equal("done 而无 taskId 映射 → wanVideoFileMissing 文案", state.thrown, "apiErrors.wanVideoFileMissing");

log = installRoutes([
    {
        method: "GET",
        match: "/api/wan/video/job/job-404",
        reply: () => {
            throw axiosErrorResponse(404, { msg: "job 不存在（bridge 重启过）" });
        },
    },
]);
let pollError = null;
try {
    await mod.pollVideoGenerationTask(wanConfig(), { id: "job-404", provider: "wan", model: "wan-omni2video" });
} catch (error) {
    pollError = error;
}
check("job 404 → 查询失败且透传 bridge 原文", String(pollError?.message).includes("job 不存在"), pollError?.message);

// ---------------------------------------------------------------------------
// 5. 刷新后重查（新模块实例 Map 为空，job→taskId 从 localStorage 恢复）
// ---------------------------------------------------------------------------
const reloaded = await loadVideoModule();
log = installRoutes([
    { method: "GET", match: "/api/wan/video/job/job-1", reply: () => ({ status: "done" }) },
    { method: "GET", match: "/api/wan/video/file/file-1", reply: () => mp4Blob([0x21, 0x22, 0x23]) },
]);
state = await reloaded.pollVideoGenerationTask(wanConfig(), { id: "job-1", provider: wait.resumedVideoTaskProvider("wan"), model: "wan-omni2video" });
check("刷新后重查：localStorage 恢复 taskId → 成片拉回", state.status === "completed" && state.result?.blob?.size === 3, `state ${JSON.stringify({ status: state.status })}`);

storageData.clear();
log = installRoutes([{ method: "GET", match: "/api/wan/video/job/job-9", reply: () => ({ status: "done" }) }]);
let reloadMiss = null;
try {
    await reloaded.pollVideoGenerationTask(wanConfig(), { id: "job-9", provider: "wan", model: "wan-omni2video" });
} catch (error) {
    reloadMiss = error;
}
equal("刷新后无映射（缓存被清）→ wanVideoFileMissing 文案", reloadMiss?.message, "apiErrors.wanVideoFileMissing");

// ---------------------------------------------------------------------------
console.log(`probe-wan-channel: ${passed} passed, ${failures.length} failed`);
rmSync(path.join(root, "node_modules", ".probe"), { recursive: true, force: true });
if (failures.length) {
    console.error(failures.map((line) => `  ✗ ${line}`).join("\n"));
    process.exit(1);
}
