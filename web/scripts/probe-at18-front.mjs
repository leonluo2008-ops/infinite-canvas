/**
 * AT-18 前端探针（本地免费、不联网、不动真实任务）：**任务行的「是不是还活着」与「永远有出口」**。
 *
 * 现场缺陷：auto-0052 拆解进程从未跑起，状态永远 discussing，界面上一没心跳、二没出口，
 * 用户只能干等两小时再手抹目录。本卡把后端 AT-18 四键（heartbeat/activity/lost_run/failed）
 * 与两条人工出口（force-finish / failed 的 retry+delete）接到任务行上。
 *
 * 判据（方案 `方案-假运行态判定与出清-20261112.md` §4.5 / §4.6 / §5.1）：
 *  [A] 运行态行「最近活动」：activity 有 → 时间+来源+静默人话；activity 缺但有心跳 → 回落心跳；
 *      两者都缺 → **明说「活动时间不可用」**，绝不拿 created_at 反推。
 *  [B] 「疑似失联」红字只认 `lost_run.suspect === true`：缺字段 → 不显示（不按 state 猜）。
 *  [C] 「强制结束」按钮可见性 = discussing/rendering/generating（其余整颗不渲染），
 *      二次确认正文写清「不删任何文件 / 不杀进程」，取消零请求、确认才发。
 *  [D] failed 行：原因 `failed.reason` → `error` → 兜底句；「重试」不再吞错；「删除」走既有二键确认件
 *      （墓碑不可恢复提示原样），桥端拒绝同时进 toast 与**行内**文案。
 *  [E] 接口层真发请求（注入 fetch）：force-finish 的 body 是 confirm:true(布尔) + confirm_name=任务号；
 *      scan-lost-runs 默认 apply:false；409 原文不被吞成「已刷新」。
 *  [F] 接线守卫：卡片真渲染（SSR）里能看到心跳时间与按钮；白名单内改动；render.py / 抽屉 / 表格页零交集。
 *
 * 跑法：cd web && node scripts/probe-at18-front.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const REPO = path.resolve(WEB, "..");
const CACHE = path.join(WEB, "node_modules", ".cache", "at18-front");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");
const readRepo = (rel) => readFileSync(path.join(REPO, rel), "utf-8");

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 260)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

// ════════════════════════════════════════════════════════════ 打包真代码（SSR）
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    [
        'import { renderToStaticMarkup } from "react-dom/server";',
        'import { App } from "antd";',
        'import { TaskCard, RunningActivityLine, ForceFinishButton, ForceFinishConfirmBody, FailedReasonLine, hubFailedReason, hubDurationText, hubClockText, FORCE_FINISH_CONFIRM_TITLE, FORCE_FINISH_CONFIRM_SCOPE, FORCE_FINISH_CONFIRM_SIDE_EFFECT, FAILED_REASON_FALLBACK } from "../../../src/pages/hub/task-card";',
        'import { HUB_FORCE_FINISH_STATES, forceFinishGuard, deleteGuard } from "../../../src/pages/hub/hub-status";',
        'import { forceFinishHubTask, scanLostRuns, retryHubTask, normalizeHeartbeat, normalizeActivity, normalizeLostRun, normalizeFailedView, listHubTasks, getHubTask } from "../../../src/pages/hub/hub-api";',
        "export { HUB_FORCE_FINISH_STATES, forceFinishGuard, deleteGuard, forceFinishHubTask, scanLostRuns, retryHubTask };",
        "export { normalizeHeartbeat, normalizeActivity, normalizeLostRun, normalizeFailedView, listHubTasks, getHubTask };",
        "export { hubFailedReason, hubDurationText, hubClockText };",
        "export { FORCE_FINISH_CONFIRM_TITLE, FORCE_FINISH_CONFIRM_SCOPE, FORCE_FINISH_CONFIRM_SIDE_EFFECT, FAILED_REASON_FALLBACK };",
        "export function renderActivity(props: any): string { return renderToStaticMarkup(<RunningActivityLine {...props} />); }",
        "export function renderForceButton(props: any): string { return renderToStaticMarkup(<ForceFinishButton {...props} />); }",
        "export function renderConfirmBody(task: any): string { return renderToStaticMarkup(<ForceFinishConfirmBody task={task} />); }",
        "export function renderFailed(props: any): string { return renderToStaticMarkup(<FailedReasonLine {...props} />); }",
        "export function renderCard(task: any): string {",
        "    return renderToStaticMarkup(<App><TaskCard task={task} onStateChange={() => {}} onRefresh={() => {}} /></App>);",
        "}",
    ].join("\n"),
);
const aliasPlugin = {
    name: "at18front-alias",
    setup(build) {
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
const ssrOut = path.join(CACHE, "ssr.mjs");
const built = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: ssrOut,
    jsx: "automatic",
    packages: "external",
    plugins: [aliasPlugin],
    define: { "import.meta.env": "{}" },
    logLevel: "silent",
});
check("[0] 真代码打包成功（task-card / hub-status / hub-api）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

if (!globalThis.localStorage) {
    const store = new Map();
    globalThis.localStorage = {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: (key) => store.delete(key),
        clear: () => store.clear(),
    };
}
if (!globalThis.window) globalThis.window = globalThis;
if (!globalThis.window.matchMedia) globalThis.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });

const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);
const countOf = (source, needle) => (source || "").split(needle).length - 1;

// ════════════════════════════════════════════════════════════ [A] 运行态「最近活动」
console.log("1) 运行态行：最近心跳/产物更新时间（activity → heartbeat → 明说不可用）");

const ALIVE = { last_activity_at: "2026-11-12T15:29:41+08:00", source: "心跳", silence_seconds: 19 };
const HB = { at: "2026-11-12T15:29:41+08:00", age_seconds: 19, phase: "等待中：本地等待循环仍在运行", count: 14 };

const liveHtml = ssr.renderActivity({ activity: ALIVE, heartbeat: HB, lost_run: { suspect: false, silence_seconds: 19, threshold_seconds: 900, reason_code: "", reason: "" } });
check("[A] activity 有值 → data-hub-activity=ok", liveHtml.includes('data-hub-activity="ok"'), liveHtml.slice(0, 200));
check("[A] 来源逐字透出（心跳）", liveHtml.includes("（心跳）"), liveHtml);
check("[A] 静默时间给人话（19 秒）", liveHtml.includes("已静默 19 秒"), liveHtml);
check("[A] 心跳阶段文案在场（自由中文串原样透出）", liveHtml.includes("等待中：本地等待循环仍在运行"));
check("[A] 绝对时间在场（MM-DD HH:mm:ss，不是「刚刚」）", /\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/.test(liveHtml), liveHtml.match(/\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/)?.[0] || "");
check("[A] suspect=false → 不出红字（不无病呻吟）", !liveHtml.includes("data-hub-lost-run"), liveHtml);

const hbOnly = ssr.renderActivity({ activity: null, heartbeat: HB, lost_run: null });
check("[A] activity 缺、心跳在 → 回落心跳并标出来源", hbOnly.includes('data-hub-activity="ok"') && hbOnly.includes("已静默 19 秒"), hbOnly.slice(0, 200));

const noEvidence = ssr.renderActivity({ activity: null, heartbeat: null, lost_run: null });
check("[A] 两者皆缺 → 逐字「活动时间不可用」（不许拿 created_at 反推）", noEvidence.includes("活动时间不可用") && noEvidence.includes('data-hub-activity="missing"'), noEvidence);

// ════════════════════════════════════════════════════════════ [B] 疑似失联只认 lost_run.suspect
console.log("2) 疑似失联红字：只认 lost_run.suspect，不按 state 猜");

const LOST = { suspect: true, silence_seconds: 7320, threshold_seconds: 900, reason_code: "", reason: "拆解进程失联，可重试或删除" };
const lostHtml = ssr.renderActivity({ activity: { ...ALIVE, silence_seconds: 7320 }, heartbeat: HB, lost_run: LOST });
check("[B] suspect=true → 红字在场（data-hub-lost-run=suspect）", lostHtml.includes('data-hub-lost-run="suspect"'), lostHtml);
check("[B] 红字写清静默时长与阈值（2 小时 2 分 / 15 分钟）", lostHtml.includes("静默 2 小时 2 分") && lostHtml.includes("阈值 15 分钟"), lostHtml);
check("[B] 红字带后端原因原文（唯一文案源，前端不自造）", lostHtml.includes("拆解进程失联，可重试或删除"), lostHtml);
check("[B] 红字给出下一步（强制结束 → 重试/删除）", lostHtml.includes("可点「强制结束」"), lostHtml);

// ════════════════════════════════════════════════════════════ [C] 强制结束入口
console.log("3) 强制结束：三种运行态才可见，二次确认写清代价");

check("[C] 可见集逐字 = discussing/rendering/generating", JSON.stringify(ssr.HUB_FORCE_FINISH_STATES) === JSON.stringify(["discussing", "rendering", "generating"]), JSON.stringify(ssr.HUB_FORCE_FINISH_STATES));
for (const state of ["discussing", "rendering", "generating"]) {
    const html = ssr.renderForceButton({ task: { local_id: "auto-0052", state }, onAsk: () => {} });
    check(`[C] state=${state} → 按钮在场且带任务号抓手`, html.includes("强制结束") && html.includes('data-hub-force-finish="auto-0052"'), html.slice(0, 200));
    const guard = ssr.forceFinishGuard({ state });
    check(`[C] guard.state=${state} → visible && !disabled`, guard.visible === true && guard.disabled === false, JSON.stringify(guard));
}
for (const state of ["ingested", "awaiting_assets", "awaiting_approval", "approved", "done", "failed", ""]) {
    const html = ssr.renderForceButton({ task: { local_id: "auto-0052", state }, onAsk: () => {} });
    const guard = ssr.forceFinishGuard({ state });
    check(`[C] state=${state || "(空)"} → 按钮整颗不渲染 + guard 说明原因`, html === "" && guard.visible === false && guard.reason.includes("无需强制结束"), `${html.slice(0, 60)} / ${guard.reason}`);
}
const confirmHtml = ssr.renderConfirmBody({ local_id: "auto-0052", state: "discussing" });
check("[C] 二次确认标题=「强制结束这个任务？」", ssr.FORCE_FINISH_CONFIRM_TITLE === "强制结束这个任务？", ssr.FORCE_FINISH_CONFIRM_TITLE);
check("[C] 确认正文写清「不删任何文件」", confirmHtml.includes("不删除任何文件"), confirmHtml);
check("[C] 确认正文写清「不杀进程」", confirmHtml.includes("也不杀进程"), confirmHtml);
check("[C] 确认正文写清副作用（还活着 → 跑完但仍是失败 → 可重试/删除）", confirmHtml.includes("状态仍是失败") && confirmHtml.includes("重试"), confirmHtml);
check("[C] 确认正文点名任务号与当前状态（防误点）", confirmHtml.includes("auto-0052") && confirmHtml.includes("discussing"), confirmHtml);
check("[C] 文案单源：scope/side-effect 两个常量被正文逐字复用", ssr.FORCE_FINISH_CONFIRM_SCOPE.includes("不删除任何文件") && confirmHtml.includes(ssr.FORCE_FINISH_CONFIRM_SIDE_EFFECT), ssr.FORCE_FINISH_CONFIRM_SIDE_EFFECT);

// ════════════════════════════════════════════════════════════ [D] failed 行
console.log("4) failed 行：原因三段落 + 重试不吞错 + 删除走既有二键确认件");

const backend = ssr.renderFailed({ failed: { reason: "拆解进程失联，可重试或删除", at: "2026-11-12T14:37:11+08:00", evidence: { trigger: "auto_scan" } }, fallbackError: "旧 error 原文" });
check("[D] failed.reason 优先（后端文案胜出）", backend.includes("拆解进程失联，可重试或删除") && !backend.includes("旧 error 原文") && backend.includes('data-hub-failed-reason="backend"'), backend);
check("[D] 失败时间透出", backend.includes("失败时间"), backend);
const fallback = ssr.renderFailed({ failed: null, fallbackError: "DiscussError: bridge timeout" });
check("[D] 老任务回落 error（history 尾注）", fallback.includes("DiscussError: bridge timeout") && fallback.includes('data-hub-failed-reason="history"'), fallback);
const empty = ssr.renderFailed({ failed: null, fallbackError: "" });
check("[D] 两者皆空 → 兜底句逐字 = 现状文案", empty.includes(ssr.FAILED_REASON_FALLBACK) && ssr.FAILED_REASON_FALLBACK === "任务失败，错误原文未返回。", empty);
check("[D] hubFailedReason 三段优先级（纯函数）", ssr.hubFailedReason({ reason: "后端" }, "旧") === "后端" && ssr.hubFailedReason(null, "旧") === "旧" && ssr.hubFailedReason({ reason: "  " }, "  ") === ssr.FAILED_REASON_FALLBACK);

// ════════════════════════════════════════════════════════════ [E] 接口层（注入 fetch，真发请求）
console.log("5) 接口层：真发 POST（注入 fetch），body/URL/错误原文逐字核对");

const calls = [];
let nextResponse = { ok: true, status: 200, body: { ok: true, local_id: "auto-0052", state: "failed", state_was: "discussing", changed: true, already: false, reason: "人工强制结束（所有者操作）" } };
globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || "GET", body: init.body ? JSON.parse(String(init.body)) : null, headers: init.headers || {} });
    const res = nextResponse;
    return { ok: res.ok, status: res.status, text: async () => JSON.stringify(res.body) };
};

const forced = await ssr.forceFinishHubTask("auto-0052");
check("[E] force-finish URL 逐字", calls[0]?.url === "/api/hub/task/auto-0052/force-finish", calls[0]?.url);
check("[E] force-finish 方法 POST + JSON 头", calls[0]?.method === "POST" && String(calls[0]?.headers?.["Content-Type"] || "").includes("application/json"), JSON.stringify(calls[0]?.method));
check("[E] confirm 是布尔 true（不是字符串）", calls[0]?.body?.confirm === true, JSON.stringify(calls[0]?.body));
check("[E] confirm_name = 任务号（逐字）", calls[0]?.body?.confirm_name === "auto-0052", JSON.stringify(calls[0]?.body));
check("[E] 缺省 reason = 人工强制结束（所有者操作）", calls[0]?.body?.reason === "人工强制结束（所有者操作）", calls[0]?.body?.reason);
check("[E] 成功回包 state/state_was/changed 归一可用", forced.state === "failed" && forced.state_was === "discussing" && forced.changed === true, JSON.stringify(forced));

calls.length = 0;
nextResponse = { ok: false, status: 409, body: { error: "任务当前不是运行态（awaiting_approval），无需强制结束" } };
let captured = "";
try {
    await ssr.forceFinishHubTask("auto-0052");
} catch (error) {
    captured = String(error?.message || error);
}
check("[E] 409 原文透出（不吞、不改写）", captured === "任务当前不是运行态（awaiting_approval），无需强制结束", captured);

calls.length = 0;
nextResponse = { ok: false, status: 409, body: { error: "重试需 failed 状态，当前 discussing（只有失败任务可重试）" } };
captured = "";
try {
    await ssr.retryHubTask("auto-0052");
} catch (error) {
    captured = String(error?.message || error);
}
check("[E] retry 的 409 原文也透出（R5 根因：前端不许改写成「已刷新」）", captured.includes("重试需 failed 状态"), captured);

calls.length = 0;
nextResponse = { ok: true, status: 200, body: { ok: true, applied: false, threshold_seconds: 900 } };
await ssr.scanLostRuns();
check("[E] scan-lost-runs URL 逐字", calls[0]?.url === "/api/hub/maintenance/scan-lost-runs", calls[0]?.url);
check("[E] 默认 dry-run（apply=false，不偷偷降级）", calls[0]?.body?.apply === false, JSON.stringify(calls[0]?.body));
calls.length = 0;
await ssr.scanLostRuns(true);
check("[E] 显式 apply=true 才实跑", calls[0]?.body?.apply === true, JSON.stringify(calls[0]?.body));

// 读面归一：畸形值一律 null，不把半成品糊到界面
check("[E] heartbeat 归一：缺 at → null；畸形 age → null", ssr.normalizeHeartbeat({ phase: "x" }) === null && ssr.normalizeHeartbeat({ at: "t", age_seconds: "19" }).age_seconds === null);
check("[E] activity 归一：缺 last_activity_at → null", ssr.normalizeActivity({ source: "心跳" }) === null && ssr.normalizeActivity({ last_activity_at: "t", source: "心跳", silence_seconds: -5 }).silence_seconds === 0);
check("[E] lost_run 归一：suspect 只认布尔 true（字符串 \"true\" → false）", ssr.normalizeLostRun({ suspect: "true" }).suspect === false && ssr.normalizeLostRun({ suspect: true }).suspect === true);
check("[E] failed 归一：空对象 → null（前端回落到 error）", ssr.normalizeFailedView({}) === null && ssr.normalizeFailedView({ reason: "x" }).reason === "x");

calls.length = 0;
nextResponse = { ok: true, status: 200, body: { tasks: [{ local_id: "auto-0052", state: "discussing", activity: ALIVE, heartbeat: HB, lost_run: LOST, failed: null }] } };
const rows = await ssr.listHubTasks();
check("[E] 列表读面四键接上（同一归一函数）", rows[0]?.activity?.source === "心跳" && rows[0]?.heartbeat?.count === 14 && rows[0]?.lost_run?.suspect === true && rows[0]?.failed === null, JSON.stringify(rows[0]));

// ════════════════════════════════════════════════════════════ [F] 卡片真渲染 + 接线守卫
console.log("6) 卡片真渲染（SSR）：运行态行能看到心跳与按钮；failed 行有原因+重试+删除");

const runningCard = ssr.renderCard({ local_id: "auto-0052-probe-at9b1", state: "discussing", intent: "测试意图", created_at: "2026-11-12T12:35:02+08:00", video_name: "ref.mp4", gen_progress: null, has_output: false, activity: ALIVE, heartbeat: HB, lost_run: LOST });
check("[F] 运行态卡片：最近活动在场（时间 + 来源 + 静默）", runningCard.includes('data-hub-activity="ok"') && runningCard.includes("（心跳）") && runningCard.includes("已静默 19 秒"), runningCard.slice(0, 300));
check("[F] 运行态卡片：疑似失联红字在场", runningCard.includes('data-hub-lost-run="suspect"'));
check("[F] 运行态卡片：强制结束按钮在场且带任务号", runningCard.includes('data-hub-force-finish="auto-0052-probe-at9b1"'));
check("[F] 运行态卡片：无行内错误（没报错就不吓人）", !runningCard.includes("data-hub-action-error"));

const failedCard = ssr.renderCard({ local_id: "auto-0052-probe-at9b1", state: "failed", intent: "测试意图", created_at: "2026-11-12T12:35:02+08:00", video_name: "ref.mp4", gen_progress: null, has_output: false, failed: { reason: "拆解进程失联，可重试或删除", at: "2026-11-12T14:37:11+08:00", evidence: { trigger: "auto_scan" } } });
check("[F] failed 卡片：原因逐字 =「拆解进程失联，可重试或删除」", failedCard.includes("拆解进程失联，可重试或删除"), failedCard.slice(0, 400));
check("[F] failed 卡片：重试按钮在场", failedCard.includes('data-hub-retry="auto-0052-probe-at9b1"'));
check("[F] failed 卡片：删除按钮走既有二键确认件（data-hub-delete-trigger）", failedCard.includes('data-hub-delete-trigger="auto-0052-probe-at9b1"'));
check("[F] failed 卡片：不再渲染强制结束（非运行态无假出口）", !failedCard.includes("data-hub-force-finish"));

const doneCard = ssr.renderCard({ local_id: "auto-0006", state: "done", intent: "", created_at: "2026-11-12T10:00:00+08:00", video_name: "a.mp4", gen_progress: 100, has_output: true });
check("[F] 已交付卡片：既无活动行也无强制结束（不造无意义入口）", !doneCard.includes("data-hub-running-footer") && !doneCard.includes("data-hub-force-finish"), doneCard.slice(0, 200));

// ---- 源码级接线守卫（交互路径在真浏览器里跑，这里锁「只有一条路径」与「不吞错」）----
const cardSrc = read("src/pages/hub/task-card.tsx");
const apiSrc = read("src/pages/hub/hub-api.ts");
const statusSrc = read("src/pages/hub/hub-status.tsx");
const deleteSrc = read("src/pages/hub/task-delete-confirm.tsx");

check("[F] 取消零请求：Modal 的 onCancel 只关弹层（无 fetch/无接口调用）", /onCancel=\{\(\) => setForceOpen\(false\)\}/.test(cardSrc), "");
check("[F] 确认才发：force-finish 只从 Modal onOk 进（onOk → forceFinish）", /onOk=\{\(\) => void forceFinish\(\)\}/.test(cardSrc));
check("[F] 成功即刷行内状态：forceFinish 内 onStateChange(...data.state || \"failed\")", /onStateChange\(task\.local_id, data\.state \|\| "failed"\)/.test(cardSrc));
check("[F] 组件不直连 fetch（一律走 hub-api 接口层）", !/\bfetch\(/.test(cardSrc));
check("[F] 重试不再吞错：源码里已无 message.info(\"已刷新任务状态\") 这句", !/message\.info\(\s*"已刷新任务状态"\s*\)/.test(cardSrc));
check("[F] 重试失败走 errorText 原文（catch 里不再是空 catch）", /const text = errorText\(error\);\s*\n\s*setActionError\(`重试失败/.test(cardSrc));
check("[F] 重试失败进行内：setActionError(\"重试失败：…\")", /setActionError\(`重试失败：\$\{text\}`\)/.test(cardSrc));
check("[F] 强制结束失败进行内：setActionError(\"强制结束失败：…\")", /setActionError\(`强制结束失败：\$\{text\}`\)/.test(cardSrc));
check("[F] failed 行删除把拒绝原因交回行内（TaskDeleteButton onError）", /<TaskDeleteButton[\s\S]{0,200}?onError=\{\(text\) => setActionError\(`删除失败：\$\{text\}`\)\}/.test(cardSrc));
check("[F] 疑似失联只认 lost_run.suspect === true（不按 state 猜）", cardSrc.includes("lost_run?.suspect === true") && !/state === "discussing"[\s\S]{0,120}疑似失联/.test(cardSrc));
check("[F] 删除弹层的墓碑不可恢复提示原样保留", deleteSrc.includes("已写墓碑防任务号复用，不可恢复。"));
check("[F] 强制结束可见性只有一份实现（hub-status.forceFinishGuard）", cardSrc.includes("forceFinishGuard(task)") && !cardSrc.includes('"discussing", "rendering", "generating"'));
check("[F] 四键归一只有一份实现，列表与详情同源调用", countOf(apiSrc, "normalizeHeartbeat(") >= 3 && countOf(apiSrc, "normalizeLostRun(") >= 3);
check("[F] 删除守卫未被放宽（HUB_RUNNING_STATES 仍含 discussing/generating）", ssr.deleteGuard({ state: "discussing" }).disabled === true && ssr.deleteGuard({ state: "failed" }).disabled === false);

// 文案与时长格式化边界（纯函数）
check("[F] 时长人话边界：59 秒 / 60 分钟 / 2 小时整", ssr.hubDurationText(59) === "59 秒" && ssr.hubDurationText(3600) === "1 小时" && ssr.hubDurationText(7320) === "2 小时 2 分" && ssr.hubDurationText(null) === "—", [ssr.hubDurationText(59), ssr.hubDurationText(3600), ssr.hubDurationText(7320), ssr.hubDurationText(null)].join(" | "));
check("[F] 坏时间原样返回（不抛、不显示 Invalid Date）", ssr.hubClockText("not-a-date") === "not-a-date" && ssr.hubClockText("") === "");

// ════════════════════════════════════════════════════════════ [G] 改动边界（零交集）
console.log("7) 改动边界：白名单内改动，render.py / 抽屉 / 表格页零交集");

const git = (...args) => execFileSync("git", args, { cwd: REPO, encoding: "utf-8" }).trim();
// AT-18 B3b（后续卡）起 HEAD 不再是 B3 交付：表格页/抽屉由 t_a69f0e22 接入强制结束，
// 所以本探针的「本卡范围」锚定**诞生本探针的那个 commit**（B3 交付），而不是 HEAD——
// 否则后续卡一动 HEAD，这条范围守卫就会拿别人的文件清单来判 B3。
const probeCommit = git("log", "-1", "--diff-filter=A", "--format=%H", "--", "web/scripts/probe-at18-front.mjs");
const headFiles = probeCommit ? git("show", "--name-only", "--pretty=format:", probeCommit).split(/\r?\n/).filter(Boolean) : [];
const workingFiles = [...git("diff", "--name-only", "HEAD").split(/\r?\n/), ...git("ls-files", "--others", "--exclude-standard").split(/\r?\n/)].filter(Boolean);
const WHITELIST = /^web\/(src\/pages\/hub\/(task-card|hub-status|hub-api|task-delete-confirm)\.(tsx|ts)|scripts\/(probe-at18-front|probe-at16-b6)\.mjs)$/;
// 已提交 → 以 HEAD 那份 commit 的文件清单为准（本卡交付物范围）；未提交则看工作区。
const inCommit = headFiles.some((item) => WHITELIST.test(item)) ? headFiles : [];
const mine = (inCommit.length ? inCommit : workingFiles.filter((item) => WHITELIST.test(item)));
const scope = inCommit.length ? inCommit : mine;
check("[G] 本卡改动文件都在白名单内（6 个：4 前端件 + 2 探针）", mine.length === 6, mine.join(", "));
check("[G] commit 内 render.py 零交集", !scope.some((item) => item.includes("automation/render.py")), scope.filter((i) => i.includes("render.py")).join(", "));
check("[G] commit 内 pipeline / templates / prompts 零交集", !scope.some((item) => /wanrepro\/(pipeline|templates|prompts)\//.test(item)));
check("[G] commit 内抽屉文件零交集（B3 在改的文件本批不碰）", !scope.includes("web/src/pages/hub/task-detail-drawer.tsx"));
check("[G] commit 内表格页零交集（本卡只覆盖卡片行）", !scope.includes("web/src/pages/hub/index.tsx"));
check("[G] commit 内后端零改动（本卡纯前端）", !scope.some((item) => item.startsWith("wanrepro/")), scope.filter((i) => i.startsWith("wanrepro/")).join(", "));
const backendRunning = readRepo("wanrepro/ui/hub_api.py");
const runningMatch = /RUNNING_STATES\s*=\s*\(([^)]*)\)/.exec(backendRunning);
check("[G] 前端可见集 ⊆ 后端 RUNNING_STATES（不造后端不认的入口）", Boolean(runningMatch) && ssr.HUB_FORCE_FINISH_STATES.every((state) => runningMatch[1].includes(`"${state}"`)), runningMatch ? runningMatch[1].replace(/\s+/g, " ") : "未找到 RUNNING_STATES");

// ════════════════════════════════════════════════════════════ 收尾
console.log("");
if (fails.length) {
    console.log(`✗ AT-18 前端探针 FAIL：${pass}/${pass + fails.length} 通过；失败项：`);
    for (const name of fails) console.log(`   · ${name}`);
    process.exit(1);
}
console.log(`✓ AT-18 前端探针全过：${pass}/${pass} 条断言`);
