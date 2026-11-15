/**
 * 卡F 验收探针（方案 docs/方案-画布生成三缺陷根治-20261114.md §卡F）：
 * F2 相位信号（completed 分支拉 blob 前发 onPhase "downloading"）+ F3 提前探测（连续 5 次 pending≥90 提前拉成片）。
 * 行为层：esbuild 打包真 video.ts（axios 打 stub，不出网、不花钱）；接线层：源码断言钉住。
 *   node scripts/probe-video-phase.mjs
 * 任何一条不成立退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-video-phase-"));

// ---- 浏览器全局垫片（video.ts 传递依赖在 node 里的最低需求）----
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.indexedDB = { open: () => ({ set onerror(_) {}, set onsuccess(_) {}, set onupgradeneeded(_) {} }) };
if (typeof globalThis.window === "undefined") globalThis.window = globalThis;

// axios stub：按模式回放任务查询/成片下载
const calls = [];
let mode = "f2";
let f3Queries = 0;
const mp4Blob = () => new Blob([new Uint8Array([0x66, 0x74, 0x79, 0x70, 1, 2, 3, 4])], { type: "video/mp4" });
const axiosStub = {
    default: {
        get: async (url) => {
            calls.push("GET " + url);
            if (mode === "f2") {
                if (url.includes("/content")) return { data: mp4Blob(), headers: { "content-type": "video/mp4" } };
                if (url.includes("/videos/task_f2")) return { data: { id: "task_f2", status: "completed", metadata: {} } };
                return { data: {} };
            }
            // f3：查询永远 pending progress=90；提前探测打 content 端点
            if (url.includes("/videos/task_f3/content")) return { data: mp4Blob(), headers: { "content-type": "video/mp4" } };
            if (url.includes("/videos/task_f3")) { f3Queries += 1; return { data: { id: "task_f3", status: "in_progress", progress: 90, metadata: {} } }; }
            return { data: {} };
        },
        post: async () => ({ data: {} }),
        isAxiosError: (e) => Boolean(e && e.isAxiosError),
        isCancel: () => false,
    },
};
const axiosStubPlugin = {
    name: "axios-stub",
    setup(build) {
        build.onResolve({ filter: /^axios$/ }, (args) => ({ path: args.path, namespace: "stub" }));
        build.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
            contents: `const stub = ${JSON.stringify({ ok: true })};\nconst impl = globalThis.__AXIOS_STUB__;\nexport default impl.default;\nexport const isAxiosError = impl.default.isAxiosError;\n`,
            loader: "js",
        }));
    },
};

async function loadVideoModule() {
    const outfile = path.join(workdir, "video.mjs");
    await build({
        entryPoints: [path.join(root, "src/services/api/video.ts")],
        outfile,
        bundle: true,
        format: "esm",
        platform: "node",
        logLevel: "silent",
        alias: { "@": path.join(root, "src") },
        plugins: [axiosStubPlugin],
        define: { "import.meta.env.DEV": "false", "import.meta.env": "{}", process: '{"env":{}}' },
    });
    return import(pathToFileURL(outfile).href);
}

globalThis.__AXIOS_STUB__ = axiosStub;
const video = await loadVideoModule();

let passed = 0;
const failures = [];
function check(name, condition, detail = "") {
    if (condition) { passed += 1; console.log("PASS | " + name + (detail ? " | " + detail : "")); }
    else { failures.push(name); console.log("FAIL | " + name + (detail ? " | " + detail : "")); }
}

const config = { baseUrl: "http://stub.local", aiApi: "sdgf", apiKey: "probe", videoGenerateAudio: "true", videoMode: "frames", size: "16:9", videoSeconds: "4", videoWatermark: "false", vquality: "720", model: "ch1::stub-model", channels: [{ id: "ch1", name: "stub", baseUrl: "http://stub.local", apiKey: "probe", aiApi: "sdgf", models: ["stub-model"] }] };

// ---- F2：completed 后、拉 blob 前发 onPhase "downloading" ----
mode = "f2";
calls.length = 0;
const phases2 = [];
try {
    const result = await video.waitForVideoGenerationTask(config, { id: "task_f2", provider: "sdgf", model: config.model }, { onPhase: (p) => phases2.push(p), onProgress: () => {} });
    check("F2 返回成片结果", result && Boolean(result.blob), result ? "keys=" + Object.keys(result).join(",") : "undefined");
    check("F2 相位信号 = downloading", phases2.includes("downloading"), "phases=" + JSON.stringify(phases2));
    check("F2 先发相位后下载（调用序）", calls.findIndex((c) => c.includes("/content")) > -1, "calls=" + JSON.stringify(calls));
} catch (e) {
    check("F2 行为探针跑通", false, String(e && e.stack ? e.stack.split("\n").slice(0, 6).join(" ⏎ ") : e).slice(0, 400));
}

// ---- F3：永远 pending progress=90 → 连续 5 次后提前探测拉成片 ----
mode = "f3";
calls.length = 0;
try {
    const result = await video.waitForVideoGenerationTask(config, { id: "task_f3", provider: "sdgf", model: config.model }, { onProgress: () => {} });
    check("F3 提前探测拿到成片（站方从未报 completed）", result && Boolean(result.blob), result ? "keys=" + Object.keys(result).join(",") : "undefined");
    check("F3 确实打过 content 端点（提前探测发生）", calls.some((c) => c.includes("/videos/task_f3/content")), "content 调用=" + calls.filter((c) => c.includes("/content")).length);
    check("F3 达到 5 连击阈值才探测（查询次数≥5）", f3Queries >= 5, "f3Queries=" + f3Queries);
} catch (e) {
    check("F3 行为探针跑通", false, String(e && e.stack ? e.stack.split("\n").slice(0, 6).join(" ⏎ ") : e).slice(0, 400));
}

// ---- 接线层源码断言 ----
const read = (p) => readFileSync(path.join(root, p), "utf8");
const projectSrc = read("src/pages/canvas/project.tsx");
const nodeSrc = read("src/components/canvas/canvas-node.tsx");
const typesSrc = read("src/types/canvas.ts");
const waitSrc = read("src/lib/canvas/video-task-wait.ts");
const toolsSrc = read("src/lib/agent/agent-site-tools.ts");
const videoSrc = read("src/services/api/video.ts");
check("接线：project 两处透传 onPhase 写 videoPhase", (projectSrc.match(/onPhase:/g) || []).length >= 2 && projectSrc.includes("videoPhase"));
check("接线：完成/失败清 videoPhase", (projectSrc.match(/videoPhase: undefined/g) || []).length >= 2);
check("接线：canvas-node 渲染 downloading 分支", nodeSrc.includes("videoPhase") && nodeSrc.includes("videoPhaseDownloading"));
check("接线：types 补 videoPhase 可选字段", typesSrc.includes("videoPhase"));
check("接线：wait 层 onPhase 透传", waitSrc.includes("onPhase"));
check("F3 计数器：completed/failed 删除计数", videoSrc.includes("sdgfEarlyProbeStreaks.delete"));
check("E4：workbench 回执含全局变更明示", toolsSrc.includes("videoAudioGlobalApplied") || toolsSrc.includes("已修改全局默认"));
for (const [name, file, key] of [["i18n zh 徽标 key", "src/i18n/locales/zh-CN.ts", "audioBadgeOn"], ["i18n en 徽标 key", "src/i18n/locales/en-US.ts", "audioBadgeOn"], ["i18n zh 下载中 key", "src/i18n/locales/zh-CN.ts", "videoPhaseDownloading"], ["i18n en 下载中 key", "src/i18n/locales/en-US.ts", "videoPhaseDownloading"]]) {
    check(name, read(file).includes(key));
}
const instructions = read("../canvas-agent/agent-instructions.md");
check("契约：拆解路由到 run_workflow_batch", instructions.includes("run_workflow_batch") && instructions.includes("builtin-video-analysis"));
check("契约：禁冒充 + 停待批明报 + 音频默认", instructions.includes("冒充") && instructions.includes("待审批") && instructions.includes("generateAudio"));

console.log("===SUMMARY=== " + passed + " PASS / " + failures.length + " FAIL" + (failures.length ? " | FAIL: " + failures.join("; ") : ""));
process.exit(failures.length ? 1 : 0);
