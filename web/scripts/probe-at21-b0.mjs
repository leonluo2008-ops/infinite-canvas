/**
 * AT-21 B0 前端探针（本地免费、不联网、不动真实任务）：**派生复用**的起态门与详情页反查入口接线。
 *
 * 断言两组：
 *  [A] 起态门/归一化/请求形状（纯函数，esbuild 打 hub-api.ts）
 *      · `hubForkGate` 对后端 `state.STATES` 每个状态都有明确答复：可派生=只有 done，其余给人话出口；
 *        active_submit 为真时 done 也不放行（后端 409 的第二条件）。
 *      · `hubPromptEditGate`：可手改=只有 awaiting_approval；未知状态不默认放行且点名状态。
 *      · `normalizeDerivedFrom/Versions`：只搬真值（缺 local_id → null / 条目丢弃），不补假 state。
 *      · `forkHubTask` POST `/api/hub/task/<id>/fork` 空体（派生不带任何「沿用上次配置」参数）；
 *        `saveHubPrompt` POST `/api/hub/task/<id>/prompt` 带 text + expect_text（乐观锁）。
 *  [B] 真渲染（esbuild + react-dom/server）
 *      · `VersionSection`：done → 「复用为新版本」可点；awaiting_approval → 置灰 + 原因；
 *        派生任务显示来源号 + 私有清单说明；有派生版本时逐条列出。
 *      · `PromptSection`：「手改正文」在 awaiting_approval 可点、done 置灰。
 *
 * 跑法：cd web && node scripts/probe-at21-b0.mjs --states <states.json>
 * （states.json 由 automation/probe_at21_b0.py 从 `state.STATES` 直出，不是手抄。）
 */
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at21-b0");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

const argv = process.argv.slice(2);
const arg = (name) => {
    const at = argv.indexOf(name);
    return at >= 0 ? argv[at + 1] : "";
};
const STATES_PATH = arg("--states");
const STATES = STATES_PATH && existsSync(STATES_PATH) ? JSON.parse(readFileSync(STATES_PATH, "utf-8")) : [];

let pass = 0;
let fail = 0;
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`[PASS] ${name}`);
    } else {
        fail += 1;
        console.log(`[FAIL] ${name}${detail ? ` —— ${detail}` : ""}`);
    }
}

// ════════════════════════════════════════════════ [A] 起态门 / 归一化 / 请求形状
// hub-api 会连带打包 gen-settings（读 localStorage 存草稿）：node 下给最小桩，只为 import 成功。
if (!globalThis.window) globalThis.window = globalThis;
if (!globalThis.localStorage) {
    globalThis.localStorage = { getItem: () => null, setItem: () => undefined, removeItem: () => undefined };
}
const apiBundle = path.join(CACHE, "hub-api.bundle.mjs");
const apiBuilt = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true, format: "esm", platform: "node", outfile: apiBundle, logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("[A] esbuild 打包 hub-api.ts 成功", apiBuilt.errors.length === 0,
    apiBuilt.errors.map((e) => e.text).join("; "));
const hubApi = await import(pathToFileURL(apiBundle).href + `?t=${Date.now()}`);

check("[A] 后端状态表拿到了（--states 由 python 直出 state.STATES）",
    Array.isArray(STATES) && STATES.length >= 9, JSON.stringify(STATES));

const forkGate = (s, opts) => hubApi.hubForkGate(s, opts);
check("[A] hubForkGate 是导出的纯函数", typeof hubApi.hubForkGate === "function");
const forkUnknown = STATES.filter((s) => {
    const g = forkGate(s);
    return !g || typeof g.enabled !== "boolean" || (g.enabled ? s !== "done" : !g.reason);
});
check("[A] 每个后端状态都有明确答复：可派生=只有 done，其余给一句人话原因",
    forkUnknown.length === 0, JSON.stringify(forkUnknown.map((s) => [s, forkGate(s)])));
check("[A] 可派生态恰是 done（不多不少）",
    STATES.filter((s) => forkGate(s).enabled).join(",") === "done",
    STATES.filter((s) => forkGate(s).enabled).join(","));
check("[A] 活跃提交占用时 done 也不放行（后端 409 的第二条件）",
    forkGate("done", { activeSubmit: true }).enabled === false
    && Boolean(forkGate("done", { activeSubmit: true }).reason));
check("[A] 未知状态不默认放行且点名状态",
    forkGate("weird_state").enabled === false && forkGate("weird_state").reason.includes("weird_state"),
    JSON.stringify(forkGate("weird_state")));
check("[A] 待审批/生成中/失败态都指了出口（不只是一句「不能」）", ["awaiting_approval", "generating", "failed"]
    .every((s) => /复用为新版本|撤销批准|重试|置换|等/.test(forkGate(s).reason)),
    JSON.stringify(["awaiting_approval", "generating", "failed"].map((s) => forkGate(s).reason)));

const promptGate = (s) => hubApi.hubPromptEditGate(s);
check("[A] hubPromptEditGate 是导出的纯函数", typeof hubApi.hubPromptEditGate === "function");
check("[A] 可手改态恰是 awaiting_approval（不多不少）",
    STATES.filter((s) => promptGate(s).enabled).join(",") === "awaiting_approval",
    STATES.filter((s) => promptGate(s).enabled).join(","));
check("[A] 其余每个状态都有原因（禁空的置灰）",
    STATES.filter((s) => !promptGate(s).enabled && !promptGate(s).reason).length === 0);
check("[A] 未知状态不默认放行且点名状态",
    promptGate("weird_state").enabled === false && promptGate("weird_state").reason.includes("weird_state"));

// 归一化：只搬真值，不补假 state
check("[A] normalizeDerivedFrom：缺 local_id → null（不显示「派生自（空）」）",
    hubApi.normalizeDerivedFrom({}) === null && hubApi.normalizeDerivedFrom(null) === null
    && hubApi.normalizeDerivedFrom({ local_id: "  " }) === null);
const dfOk = hubApi.normalizeDerivedFrom({ local_id: "auto-0001", bridge_id: "7", at: "2026-11-13T01:00:00+08:00",
    changed: ["描述", "", 3] });
check("[A] normalizeDerivedFrom：字符串真值逐字搬运 + changed 去空/去非串",
    dfOk && dfOk.local_id === "auto-0001" && dfOk.bridge_id === "7" && dfOk.changed.join(",") === "描述",
    JSON.stringify(dfOk));
const dv = hubApi.normalizeDerivedVersions([
    { local_id: "auto-0008", state: "awaiting_assets", at: "t2", has_output: false },
    { state: "done" },                                   // 缺 local_id → 丢弃
    "garbage",
    null,
]);
check("[A] normalizeDerivedVersions：坏条目丢弃、不补假 state",
    dv.length === 1 && dv[0].local_id === "auto-0008" && dv[0].state === "awaiting_assets",
    JSON.stringify(dv));
check("[A] normalizeDerivedVersions：非数组 → []",
    hubApi.normalizeDerivedVersions(undefined).length === 0 && hubApi.normalizeDerivedVersions({}).length === 0);

// 请求形状（stub fetch：只看 URL/方法/体，不联网）
const calls = [];
globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: (init && init.method) || "GET",
        body: init && init.body ? String(init.body) : "" });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, local_id: "auto-0009" }) };
};
await hubApi.forkHubTask("auto-0001");
const forkCall = calls.at(-1);
check("[A] forkHubTask → POST /api/hub/task/auto-0001/fork", forkCall
    && forkCall.method === "POST" && forkCall.url === "/api/hub/task/auto-0001/fork", JSON.stringify(forkCall));
check("[A] fork 请求体为空（派生不携带任何「沿用上次配置」参数）",
    forkCall && forkCall.body === "{}", forkCall && forkCall.body);
await hubApi.saveHubPrompt("auto-0001", "正文", "打开时的正文");
const promptCall = calls.at(-1);
check("[A] saveHubPrompt → POST /api/hub/task/auto-0001/prompt", promptCall
    && promptCall.method === "POST" && promptCall.url === "/api/hub/task/auto-0001/prompt", JSON.stringify(promptCall));
let promptBody = {};
try {
    promptBody = JSON.parse(promptCall.body);
} catch {
    promptBody = {};
}
check("[A] 正文请求带 text + expect_text（乐观锁）",
    promptBody.text === "正文" && promptBody.expect_text === "打开时的正文", JSON.stringify(promptBody));

const hubApiSrc = read("src/pages/hub/hub-api.ts");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
check("[B] 详情页接线：fork 入口 → forkHubTask；跳转走 onOpenTask；派生成功走 onForked",
    /forkHubTask\(localId\)/.test(drawerSrc) && /onOpenTask\(from\.local_id\)/.test(drawerSrc)
    && /onOpenTask\(item\.local_id\)/.test(drawerSrc) && /onForked\?\.\(/.test(drawerSrc));
check("[B] 手改正文走 saveHubPrompt（不写 override 文件、不在前端算快照）",
    /saveHubPrompt\(localId, next, expect\)/.test(drawerSrc)
    && !/override[._]?(file|path|prompt)/i.test(drawerSrc + hubApiSrc));
check("[B] 编辑器承载 = hub-text-editor 的 doc variant（不复用 model-script-editor）",
    /variant="doc"/.test(drawerSrc) && !/model-script-editor/.test(drawerSrc));

// ════════════════════════════════════════════════ [C] 真渲染
const STUB = path.join(CACHE, "streamdown-stub.tsx");
writeFileSync(STUB,
    "export function Streamdown({ children }: { children?: unknown }) {\n" +
    "    return <pre data-stub-streamdown>{children as string}</pre>;\n" +
    "}\n");
const stubPlugin = {
    name: "at21b0-stubs",
    setup(build) {
        build.onResolve({ filter: /^streamdown$/ }, () => ({ path: STUB, namespace: "at21b0-stub" }));
        build.onLoad({ filter: /.*/, namespace: "at21b0-stub" }, () => ({
            contents: readFileSync(STUB, "utf-8"), loader: "tsx", resolveDir: WEB,
        }));
        build.onResolve({ filter: /^@\// }, (args) => {
            const base = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
                try {
                    if (statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 试下一个后缀 */
                }
            }
            return { path: base };
        });
    },
};

const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
    'import { PromptSection, VersionSection } from "../../../src/pages/hub/task-detail-drawer";\n' +
    "export function renderVersion(detail: any): string {\n" +
    "    return renderToStaticMarkup(<VersionSection detail={detail} onOpenTask={() => undefined} onForked={() => undefined} />);\n" +
    "}\n" +
    "export function renderPrompt(detail: any): string {\n" +
    "    return renderToStaticMarkup(<PromptSection detail={detail} />);\n" +
    "}\n");
const ssrOut = path.join(CACHE, "ssr.mjs");
const ssrBuilt = await esbuild.build({
    entryPoints: [entry], bundle: true, platform: "node", format: "esm", outfile: ssrOut,
    jsx: "automatic", packages: "external", plugins: [stubPlugin],
    define: { "import.meta.env": "{}" }, logLevel: "silent",
});
check("[C] 真渲染打包成功（react/react-dom/antd 走宿主，仅 streamdown 打桩）",
    ssrBuilt.errors.length === 0, ssrBuilt.errors.map((e) => `${e.location?.file}:${e.text}`).join("; "));
const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

const BASE = { local_id: "auto-0001", state: "done", prompt_md: "", 自检: [], asset_cards: [], pair_lines: [], suggestions: [] };

const doneHtml = ssr.renderVersion({ ...BASE, derived_from: null, derived_versions: [] });
check("[C] done：出「复用为新版本」入口且可点（无 disabled）",
    /data-hub-fork-entry/.test(doneHtml) && !/disabled=""[^>]*data-hub-fork-entry|data-hub-fork-entry[^>]*disabled=""/.test(doneHtml));
check("[C] done 且无版本关系时也渲染该区（入口不能藏起来）", /data-hub-version-section/.test(doneHtml));

const waitingHtml = ssr.renderVersion({ ...BASE, state: "awaiting_approval" });
check("[C] 非 done 且无版本关系：不渲染该区（按 §四 每状态只渲染该表列出的动作）",
    !/data-hub-version-section/.test(waitingHtml), waitingHtml.slice(0, 160));

const childHtml = ssr.renderVersion({
    ...BASE, state: "awaiting_assets",
    derived_from: { local_id: "auto-0001", bridge_id: "7", at: "2026-11-13T01:00:00+08:00", changed: [] },
    checklist_private: true,
});
check("[C] 派生任务：显示「复用自 auto-0001」+ 私有清单说明",
    /data-hub-derived-from/.test(childHtml) && childHtml.includes("auto-0001")
    && childHtml.includes("私有") === false && /副本/.test(childHtml));
check("[C] 派生任务：来源可点（跳转入口真实存在）", /data-hub-derived-from-link/.test(childHtml));
check("[C] 派生任务（非 done）：派生入口置灰并写清原因（关系区照样显示）",
    /data-hub-fork-gate-reason/.test(childHtml) && /置换/.test(childHtml), childHtml.slice(0, 160));

const parentHtml = ssr.renderVersion({
    ...BASE,
    derived_versions: [
        { local_id: "auto-0008", state: "awaiting_assets", at: "2026-11-13T02:00:00+08:00", has_output: false },
        { local_id: "auto-0009", state: "done", at: "2026-11-13T01:00:00+08:00", has_output: true },
    ],
});
check("[C] 来源任务：逐条列出派生版本（含状态与「已有成片」）",
    /data-hub-derived-versions/.test(parentHtml) && parentHtml.includes("auto-0008")
    && parentHtml.includes("auto-0009") && /已有成片/.test(parentHtml));

const promptEdit = ssr.renderPrompt({ ...BASE, state: "awaiting_approval", prompt_md: "正文".repeat(120) });
check("[C] awaiting_approval 的提示词段：「手改正文」入口存在且可点",
    /data-hub-prompt-edit/.test(promptEdit) && !/disabled=""[^>]*data-hub-prompt-edit|data-hub-prompt-edit[^>]*disabled=""/.test(promptEdit));
const promptDone = ssr.renderPrompt({ ...BASE, state: "done", prompt_md: "正文".repeat(120) });
check("[C] done 的提示词段：「手改正文」置灰（正文是那次批准的凭证）",
    /data-hub-prompt-edit/.test(promptDone) && /disabled/.test(promptDone));

console.log(`\n[probe-at21-b0-front] ${pass}/${pass + fail} PASS`);
process.exit(fail === 0 ? 0 : 1);
