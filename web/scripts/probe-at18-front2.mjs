/**
 * AT-18 B3b 前端探针（本地免费、不联网、不动真实任务）：**默认页面 `/hub` 表格行 + 详情抽屉的出口**。
 *
 * 现场缺口：AT-18 B3 只把「最近活动 / 强行结束 / failed 重试+删除」接到了 `?legacy=1` 卡片流，
 * 而所有者日常入口是表格页 /hub——坏任务在那里此前没有任何出口（auto-0052 只能干等或手抹目录）。
 * 本卡把同一批 B3 导出件接到两处落点，本探针锁的就是「接对了、没接出第二套」。
 *
 * 判据（方案 `方案-假运行态判定与出清-20261112.md` §4.5 / §4.6 / §5.1）：
 *  [A] 表格行真渲染（SSR 真代码）：running 行含活动读数 + 强制结束；failed 行含原因 + 重试 + 删除；
 *      done 行两者皆无；ingesting 不造假出口；活动缺失明说「活动时间不可用」。
 *  [E] 列表行失败原因收口（t_d7254b52）：行内只长后端原文**首行**（line-clamp-2）+ on_disk/missing 徽标，
 *      同帧云端原文只出现 1 次、［原始响应］JSON 不进单元格；完整原文（含三行附属）只在详情页
 *      `CloudFailurePanel` 出现（反向断言）。
 *  [B] 表格页接线：取消只关弹层（零请求）、确认才调 force-finish、成功即行内刷 failed、
 *      三条接口错误原文进行内、可见性与文案全部复用 B3 导出件（本页零第二套状态枚举/零文案复制）。
 *  [C] 抽屉：同一条按钮 + 同一段确认文案 + 成功后 onStateChange(localId,"failed") + onRefresh()。
 *  [D] 改动边界：只碰 index.tsx / task-detail-drawer.tsx + 探针。
 *
 * 跑法：cd web && node scripts/probe-at18-front2.mjs      （失败 rc=1）
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
const CACHE = path.join(WEB, "node_modules", ".cache", "at18-front2");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

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
        'import { HubRowStatusCell, HubRowActions, HubRowFailedReason, LapianStageCell, hubRowQuickAction } from "../../../src/pages/hub/index";',
        'import { DrawerForceFinishEntry, TaskDetailDrawer } from "../../../src/pages/hub/task-detail-drawer";',
        'import { FORCE_FINISH_CONFIRM_TITLE, FORCE_FINISH_CONFIRM_SCOPE, FORCE_FINISH_CONFIRM_SIDE_EFFECT, ForceFinishConfirmBody, hubFailedReason, failedRowSummary, CloudFailurePanel, FAILED_REASON_FALLBACK } from "../../../src/pages/hub/task-card";',
        'import { forceFinishHubTask, retryHubTask } from "../../../src/pages/hub/hub-api";',
        "export { DrawerForceFinishEntry, TaskDetailDrawer, LapianStageCell, hubRowQuickAction };",
        "export { forceFinishHubTask, retryHubTask };",
        "export { FORCE_FINISH_CONFIRM_TITLE, FORCE_FINISH_CONFIRM_SCOPE, FORCE_FINISH_CONFIRM_SIDE_EFFECT, hubFailedReason, failedRowSummary, FAILED_REASON_FALLBACK };",
        "const noop = () => {};",
        "const channels: any[] = [];",
        "export function renderCloudPanel(failed: any, fallbackError = \"\"): string {",
        "    return renderToStaticMarkup(<App><CloudFailurePanel failed={failed} fallbackError={fallbackError} /></App>);",
        "}",
        "export function renderStatus(task: any, actionError = \"\"): string {",
        '    return renderToStaticMarkup(<App><HubRowStatusCell task={task} actionError={actionError} onDismissActionError={noop} /></App>);',
        "}",
        "export function renderActions(task: any): string {",
        "    return renderToStaticMarkup(",
        "        <App>",
        "            <HubRowActions task={task} channels={channels} onOpen={noop} onQuick={noop} onForceAsk={noop} onRetry={noop} onDeleted={noop} onError={noop} />",
        "        </App>,",
        "    );",
        "}",
        "export function renderFailedReason(task: any): string {",
        "    return renderToStaticMarkup(<App><HubRowFailedReason task={task} /></App>);",
        "}",
        "export function renderDrawerEntry(task: any): string {",
        "    return renderToStaticMarkup(<App><DrawerForceFinishEntry task={task} onStateChange={noop} onRefresh={noop} onError={noop} /></App>);",
        "}",
        "export function renderConfirmBody(task: any): string {",
        "    return renderToStaticMarkup(<ForceFinishConfirmBody task={task} />);",
        "}",
    ].join("\n"),
);
const aliasPlugin = {
    name: "at18front2-alias",
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
check("[0] 真代码打包成功（index.tsx / task-detail-drawer.tsx / task-card 导出件）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

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

// ════════════════════════════════════════════════════════════ 测试样本
const ALIVE = { last_activity_at: "2026-11-12T15:29:41+08:00", source: "心跳", silence_seconds: 19 };
const HB = { at: "2026-11-12T15:29:41+08:00", age_seconds: 19, phase: "等待中：本地等待循环仍在运行", count: 14 };
const LOST = { suspect: true, silence_seconds: 7320, threshold_seconds: 900, reason_code: "", reason: "拆解进程失联，可重试或删除" };
const RUNNING = {
    local_id: "auto-0052",
    task_name: "auto-0052·ref.mp4",
    state: "discussing",
    intent: "测试意图",
    created_at: "2026-11-12T12:35:02+08:00",
    video_name: "ref.mp4",
    gen_progress: null,
    has_output: false,
    activity: ALIVE,
    heartbeat: HB,
    lost_run: LOST,
};
const FAILED = {
    ...RUNNING,
    state: "failed",
    activity: null,
    heartbeat: null,
    lost_run: null,
    failed: { reason: "拆解进程失联，可重试或删除", at: "2026-11-12T14:37:11+08:00", evidence: { trigger: "auto_scan" } },
};
const DONE = { ...RUNNING, state: "done", activity: null, heartbeat: null, lost_run: null, failed: null, gen_progress: 100, has_output: true };
const INGESTING = { ...RUNNING, state: "ingesting", activity: null, heartbeat: null, lost_run: null };

// ════════════════════════════════════════════════════════════ [A] 表格行真渲染
console.log("1) 表格行（SSR 真渲染）：running 有读数+出口，failed 有原因+重试+删除，done 两者皆无");

const runStatus = ssr.renderStatus(RUNNING);
check("[A] running 行状态列：最近活动在场（data-hub-activity=ok）", runStatus.includes('data-hub-activity="ok"'), runStatus.slice(0, 240));
check("[A] running 行状态列：静默人话 + 来源逐字透出", runStatus.includes("已静默 19 秒") && runStatus.includes("（心跳）"), runStatus.slice(0, 240));
check("[A] running 行状态列：疑似失联红字只认 lost_run.suspect", runStatus.includes('data-hub-lost-run="suspect"') && runStatus.includes("拆解进程失联，可重试或删除"), "");
check("[A] running 行状态列：不误报失败原因盒", !runStatus.includes("data-hub-failed-reason"), "");
check("[A] running 行状态列：拆解进度行仍在（B4a 既有件未被挤掉）", runStatus.includes("data-hub-lapian-cell"), "");

const runActions = ssr.renderActions(RUNNING);
check("[A] running 行操作列：强制结束按钮在场且带任务号", runActions.includes('data-hub-force-finish="auto-0052"') && runActions.includes("强制结束"), runActions.slice(0, 240));
check("[A] running 行操作列：非 failed 不给重试（不造无意义入口）", !runActions.includes("data-hub-retry"), "");
check("[A] running 行操作列：打开 + 删除两键仍在", /打\s?开/.test(runActions) && runActions.includes('data-hub-delete-trigger="auto-0052"'), runActions.slice(0, 240));

const failStatus = ssr.renderStatus(FAILED);
check("[A] failed 行状态列：后端原文首行逐字透出（data-hub-failed-first-line）+ 来源钩子 backend", failStatus.includes("拆解进程失联，可重试或删除") && failStatus.includes("data-hub-failed-first-line") && failStatus.includes('data-hub-failed-reason="backend"'), failStatus.slice(0, 300));
check("[A] failed 行状态列：单行 fixture 无换行 → 与后端 failed.reason 逐字相等（不多字不少字）", (failStatus.match(/data-hub-failed-first-line[^>]*>([\s\S]*?)<\/span>/) || [])[1] === "拆解进程失联，可重试或删除", (failStatus.match(/data-hub-failed-first-line[^>]*>([\s\S]*?)<\/span>/) || [])[1]);
check("[A] failed 行状态列：失败时间在场（三段式第二段）", failStatus.includes("失败时间") && /11-12\s+14:37:11/.test(failStatus), failStatus.slice(0, 300));
check("[A] failed 行状态列：不渲染活动读数（非运行态不假装活着）", !failStatus.includes("data-hub-activity"), "");

const failActions = ssr.renderActions(FAILED);
check("[A] failed 行操作列：重试按钮在场且带任务号", failActions.includes('data-hub-retry="auto-0052"') && /重\s?试/.test(failActions), failActions.slice(0, 240));
check("[A] failed 行操作列：删除按钮走既有二键确认件", failActions.includes('data-hub-delete-trigger="auto-0052"'), "");
check("[A] failed 行操作列：不再渲染强制结束（非运行态无假出口）", !failActions.includes("data-hub-force-finish"), "");

const doneStatus = ssr.renderStatus(DONE);
const doneActions = ssr.renderActions(DONE);
check("[A] done 行状态列：既无活动读数也无失败原因", !doneStatus.includes("data-hub-activity") && !doneStatus.includes("data-hub-failed-reason"), doneStatus.slice(0, 240));
check("[A] done 行操作列：既无强制结束也无重试", !doneActions.includes("data-hub-force-finish") && !doneActions.includes("data-hub-retry"), doneActions.slice(0, 240));
check("[A] done 行操作列：删除入口仍在（delivered 也可清理）", doneActions.includes('data-hub-delete-trigger="auto-0052"'), "");

const ingestedStatus = ssr.renderStatus(INGESTING);
const ingestedActions = ssr.renderActions(INGESTING);
check("[A] ingesting 行：强制结束不渲染（不在可见集，不造后端不认的入口）", !ingestedActions.includes("data-hub-force-finish") && !ingestedStatus.includes("data-hub-activity"), ingestedActions.slice(0, 200));

const noEvidence = ssr.renderStatus({ ...RUNNING, activity: null, heartbeat: null, lost_run: null });
check("[A] running 但无任何活动证据 → 逐字「活动时间不可用」", noEvidence.includes("活动时间不可用") && noEvidence.includes('data-hub-activity="missing"'), noEvidence.slice(0, 240));

const withError = ssr.renderStatus(FAILED, "强制结束失败：任务当前不是运行态（done），无需强制结束");
check("[A] 行内错误：接口原文留在行里（data-hub-row-error + 原文）", withError.includes('data-hub-row-error="auto-0052"') && withError.includes("任务当前不是运行态（done），无需强制结束"), withError.slice(0, 320));
check("[A] 行内错误：可主动关掉（「知道了」按钮在场）", withError.includes("知道了"), "");

// 老任务（无 failed.reason）回落详情 error 的顺序仍是三段式（纯函数，B3 交付件未改）
check("[A] 原因三段式顺序未变：failed.reason → error → 兜底句", ssr.hubFailedReason({ reason: "后端" }, "旧") === "后端" && ssr.hubFailedReason(null, "旧") === "旧" && ssr.hubFailedReason(null, "") === ssr.FAILED_REASON_FALLBACK);
check("[A] 兜底句逐字未变", ssr.FAILED_REASON_FALLBACK === "任务失败，错误原文未返回。", ssr.FAILED_REASON_FALLBACK);

// ════════════════════════════════════════════════════════════ [E] 列表行失败原因收口（t_d7254b52）
console.log("1b) 列表行收口：只长云端原文首行 + 落盘徽标；同帧原文 1 次；全文只在详情页");

const CLOUD_MSG = "HTTP 429 Too Many Requests：Current group upstream load is saturated, please try again later (request id: 2026111219311023663367qPhGp1bu)";
const RAW_JSON = '{"error":{"message":"Current group upstream load is saturated","type":"new_api_error"}}';
// 真机样本（auto-0055 的 failed_reason 就是这 4 行）：首行云端原文 + 三条诊断附属（末行是 raw JSON）
const LONG_REASON = [
    `云端原文：${CLOUD_MSG}`,
    "［附属］退出码 3｜错误类型 ApiError",
    "［转述］analyze.py 退出 3：ERROR 拆解失败（下游退出码 3），后面两步不再执行",
    `［原始响应］${RAW_JSON}`,
].join("\n");
const LONG_EVIDENCE = {
    trigger: "bridge_exit",
    exit_code: 3,
    cloud_error: { error_type: "ApiError", message: CLOUD_MSG, finish_reason: null, raw_response: RAW_JSON },
    cloud_raw: "on_disk",
};
const ROW_FAILED = { ...FAILED, failed: { reason: LONG_REASON, at: "2026-11-12T19:31:14+08:00", evidence: LONG_EVIDENCE } };
const rowLong = ssr.renderStatus(ROW_FAILED);
const firstLineOf = (html) => (html.match(/data-hub-failed-first-line[^>]*>([\s\S]*?)<\/span>/) || [])[1];

check("[E] 行内首行 = 后端原文第一行（逐字，含「云端原文：」前缀不改写）", firstLineOf(rowLong) === `云端原文：${CLOUD_MSG}`, firstLineOf(rowLong));
check("[E] 云端原文同帧只出现 1 次（不再被 CloudErrorEvidence 的 message 重复渲染）", countOf(rowLong, CLOUD_MSG) === 1, String(countOf(rowLong, CLOUD_MSG)));
check("[E] 证据块整体不再进单元格（data-hub-cloud-error 不在行内）", !rowLong.includes("data-hub-cloud-error"), "");
check("[E] ［原始响应］JSON 不进表格单元格（raw JSON 片段与 new_api_error 都不在）", !rowLong.includes("new_api_error") && !rowLong.includes(RAW_JSON.slice(0, 40)), "");
check("[E] 视觉上限 2 行：首行容器带 line-clamp-2", /data-hub-failed-first-line/.test(rowLong) && /line-clamp-2/.test(rowLong), "");
check("[E] 行内不再平铺多行原文（whitespace-pre-wrap 全文字段不在行内）", !rowLong.includes("whitespace-pre-wrap"), "");
check("[E] 落盘徽标 on_disk 在场且只认 evidence.cloud_raw", rowLong.includes('data-hub-cloud-raw="on_disk"') && rowLong.includes("on_disk 已落盘"), "");
check("[E] 行内给出口：悬停提示指向详情完整原文（不假装行内就是全部）", /title="[^"]*完整原文见「打开」详情/.test(rowLong), "");
check("[E] 失败时间不占视觉行：只进 title 悬停提示（详情页正文另有）", /title="失败时间 11-12 19:31:14 · 完整原文见/.test(rowLong) && !rowLong.includes("data-hub-failed-at"), "");

// 反向断言：同一份 failed.reason 送详情页 → 全文 + 三行附属都在（原文只在详情页出现）
const panelLong = ssr.renderCloudPanel(ROW_FAILED.failed, "");
check("[E] 反向：全文只在详情页（CloudFailurePanel 主展示区含完整云端原文）", panelLong.includes("data-hub-cloud-failure-main") && panelLong.includes(CLOUD_MSG), "");
check("[E] 反向：三行附属也只在详情页（行内一行都不许留）", ["［附属］退出码 3", "［转述］analyze.py 退出 3", "［原始响应］"].every((needle) => panelLong.includes(needle)) && !rowLong.includes("［附属］") && !rowLong.includes("［转述］") && !rowLong.includes("［原始响应］"), "");
check("[E] 反向：原文逐字可拼回（详情页 != 裁剪版；行内首行是它的真前缀）", ssr.hubFailedReason(ROW_FAILED.failed, "").startsWith(firstLineOf(rowLong)) && panelLong.includes("new_api_error") && !rowLong.includes("new_api_error"), "");

// 徽标三态 + 纯函数（缺/坏值不造占位）
const rowMissing = ssr.renderStatus({ ...ROW_FAILED, failed: { ...ROW_FAILED.failed, evidence: { ...LONG_EVIDENCE, cloud_raw: "missing" } } });
check("[E] cloud_raw=missing → missing 徽标（只认字面值）", rowMissing.includes('data-hub-cloud-raw="missing"') && rowMissing.includes("missing 未落盘"), "");
const rowNoRaw = ssr.renderStatus({ ...ROW_FAILED, failed: { ...ROW_FAILED.failed, evidence: { trigger: "auto_scan" } } });
check("[E] cloud_raw 缺失 → 不渲染徽标（不造假占位）", !rowNoRaw.includes("data-hub-cloud-raw"), "");
const rowNoEvidence = ssr.renderStatus({ ...ROW_FAILED, failed: { ...ROW_FAILED.failed, evidence: null } });
check("[E] evidence=null → 仍只长首行（不因缺证据退化成全文/空行）", firstLineOf(rowNoEvidence) === `云端原文：${CLOUD_MSG}`, firstLineOf(rowNoEvidence));

const sum1 = ssr.failedRowSummary(ROW_FAILED.failed, "");
check("[E] failedRowSummary：firstLine=后端起首行、cloudRaw=on_disk、不含换行", sum1.firstLine === `云端原文：${CLOUD_MSG}` && sum1.cloudRaw === "on_disk" && !sum1.firstLine.includes("\n"), JSON.stringify(sum1));
const sum2 = ssr.failedRowSummary(null, "DiscussError: bridge timeout\n尾注第二行");
check("[E] failedRowSummary：老任务回落 error 的首行（尾注第二行不进行内）", sum2.firstLine === "DiscussError: bridge timeout" && sum2.cloudRaw === "", JSON.stringify(sum2));
const sum3 = ssr.failedRowSummary({ reason: "某原因", at: "", evidence: { cloud_raw: "weird" } }, "");
check("[E] failedRowSummary：未知 cloud_raw 值 → 空串（不猜 on_disk/missing）", sum3.cloudRaw === "", JSON.stringify(sum3));
const sum4 = ssr.failedRowSummary(null, "");
check("[E] failedRowSummary：两者皆空 → 兜底句（与既有文案同一句）", sum4.firstLine === ssr.FAILED_REASON_FALLBACK, JSON.stringify(sum4));
const rowEmpty = ssr.renderStatus({ ...FAILED, failed: null });
check("[E] 无 failed.reason 也无 error → 行内兜底句（不空盒）", rowEmpty.includes(ssr.FAILED_REASON_FALLBACK) && rowEmpty.includes("data-hub-failed-row"), rowEmpty.slice(0, 240));

// ════════════════════════════════════════════════════════════ [B] 表格页接线
console.log("2) 表格页接线：取消零请求、确认才发、成功即刷行内状态、错误原文进「行内」");

const indexSrc = read("src/pages/hub/index.tsx");
check("[B] 取消路径只关弹层：onCancel={() => setForceTarget(\"\")}", /onCancel=\{\(\) => setForceTarget\(""\)\}/.test(indexSrc), "");
check("[B] 确认才发：Modal onOk 唯一指向 runForceFinish", /onOk=\{\(\) => void runForceFinish\(\)\}/.test(indexSrc), "");
check("[B] force-finish 调用点只有一处（不散落第二套请求）", countOf(indexSrc, "forceFinishHubTask(") === 1, String(countOf(indexSrc, "forceFinishHubTask(")));
check("[B] 组件不直连 fetch（一律走 hub-api 接口层）", !/\bfetch\(/.test(indexSrc), "");
check("[B] runForceFinish 定义在调用点之前（Modal 只作触发口）", indexSrc.indexOf("const runForceFinish") > 0 && indexSrc.indexOf("const runForceFinish") < indexSrc.indexOf("onOk={() => void runForceFinish()}"), "");
check("[B] 成功即刷行内状态：patchState(local_id, data.state || \"failed\")", /patchState\(task\.local_id, data\.state \|\| "failed"\)/.test(indexSrc), "");
check("[B] 成功即关弹层（setForceTarget(\"\") 在 try 内）", /const data = await forceFinishHubTask\(task\.local_id\);\s*\n\s*setForceTarget\(""\);/.test(indexSrc), "");
check("[B] 强制结束失败进「行内」：setRowError(`强制结束失败：${text}`)", /setRowError\(task\.local_id, `强制结束失败：\$\{text\}`\)/.test(indexSrc), "");
check("[B] 重试失败进「行内」：setRowError(`重试失败：${text}`)", /setRowError\(task\.local_id, `重试失败：\$\{text\}`\)/.test(indexSrc), "");
check("[B] 重试不再吞成「已刷新任务状态」（R5 根因）", !/已刷新任务状态/.test(indexSrc), "");
check("[B] 删除拒绝进行内：TaskDeleteButton onError → `删除失败：${text}`", /onError=\{\(text\) => onError\(`删除失败：\$\{text\}`\)\}/.test(indexSrc), "");
check("[B] 重试请求形状复用 hub-api（retryHubTask，无第二套 URL）", /await retryHubTask\(task\.local_id\)/.test(indexSrc) && !/\/api\/hub\//.test(indexSrc), "");
check("[B] 可见性只有一份实现：forceFinishGuard（页面不另写状态枚举）", indexSrc.includes("forceFinishGuard(task)") && !indexSrc.includes('"discussing", "rendering", "generating"'), "");
check("[B] 文案单源：表格页不含「不删除任何文件 / 也不杀进程」字面量", !indexSrc.includes("不删除任何文件") && !indexSrc.includes("也不杀进程"), "");
check("[B] 二次确认标题/正文直接 import 自 task-card", /import \{[^}]*FORCE_FINISH_CONFIRM_TITLE[^}]*ForceFinishConfirmBody[^}]*\} from "\.\/task-card"/.test(indexSrc), "");
check("[B] 弹层里渲染的是 B3 的 ForceFinishConfirmBody（不是第二套正文）", /<ForceFinishConfirmBody task=\{forceTask\} \/>/.test(indexSrc), "");
check("[B] 弹层标题用 FORCE_FINISH_CONFIRM_TITLE（逐字合同文案）", /title=\{FORCE_FINISH_CONFIRM_TITLE\}/.test(indexSrc), "");

// 接口层回归（注入 fetch 真发请求）：B3b 复用同一函数，形状不许被这卡改坏
const calls = [];
let nextResponse = { ok: true, status: 200, body: { ok: true, local_id: "auto-0052", state: "failed", state_was: "discussing", changed: true, already: false } };
globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || "GET", body: init.body ? JSON.parse(String(init.body)) : null });
    const res = nextResponse;
    return { ok: res.ok, status: res.status, text: async () => JSON.stringify(res.body) };
};
await ssr.forceFinishHubTask("auto-0052");
check("[B] 请求形状未变：POST /api/hub/task/{id}/force-finish + confirm:true + confirm_name=id", calls[0]?.url === "/api/hub/task/auto-0052/force-finish" && calls[0]?.method === "POST" && calls[0]?.body?.confirm === true && calls[0]?.body?.confirm_name === "auto-0052", JSON.stringify(calls[0]));
calls.length = 0;
nextResponse = { ok: false, status: 409, body: { error: "任务当前不是运行态（awaiting_approval），无需强制结束" } };
let captured = "";
try {
    await ssr.forceFinishHubTask("auto-0052");
} catch (error) {
    captured = String(error?.message || error);
}
check("[B] 409 原文可透出（页面把这段原文送进「行内」）", captured === "任务当前不是运行态（awaiting_approval），无需强制结束", captured);
calls.length = 0;
nextResponse = { ok: false, status: 409, body: { error: "重试需 failed 状态，当前 discussing（只有失败任务可重试）" } };
captured = "";
try {
    await ssr.retryHubTask("auto-0052");
} catch (error) {
    captured = String(error?.message || error);
}
check("[B] retry 的 409 原文同样可透出（行内「重试失败：…」）", captured.includes("重试需 failed 状态"), captured);

// ════════════════════════════════════════════════════════════ [C] 详情抽屉
console.log("3) 抽屉：同一颗按钮、同一段确认文案、成功后回调状态与刷新");

const drawerRun = ssr.renderDrawerEntry(RUNNING);
check("[C] 抽屉 running：按钮在场且带任务号（与行内同一颗 ForceFinishButton）", drawerRun.includes('data-hub-force-finish="auto-0052"') && drawerRun.includes("强制结束"), drawerRun.slice(0, 240));
check("[C] 抽屉 done：整颗不渲染（非运行态无假出口）", !ssr.renderDrawerEntry(DONE).includes("data-hub-force-finish"), ssr.renderDrawerEntry(DONE).slice(0, 120));
check("[C] 抽屉 generating：按钮在场（可见集三态都接上）", ssr.renderDrawerEntry({ ...RUNNING, state: "generating" }).includes('data-hub-force-finish="auto-0052"'), "");

const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
check("[C] 抽屉 import 同一段确认文案（FORCE_FINISH_CONFIRM_TITLE + ForceFinishConfirmBody 自 task-card）", /import \{[^}]*FORCE_FINISH_CONFIRM_TITLE[^}]*ForceFinishConfirmBody[^}]*\} from "\.\/task-card"/.test(drawerSrc), "");
check("[C] 抽屉弹层标题 = FORCE_FINISH_CONFIRM_TITLE", /title=\{FORCE_FINISH_CONFIRM_TITLE\}/.test(drawerSrc), "");
check("[C] 抽屉弹层正文 = B3 的 ForceFinishConfirmBody", /<ForceFinishConfirmBody task=\{task\} \/>/.test(drawerSrc), "");
check("[C] 抽屉取消只关弹层（零请求）：onCancel={() => setForceOpen(false)}", /onCancel=\{\(\) => setForceOpen\(false\)\}/.test(drawerSrc), "");
check("[C] 抽屉确认才发：onOk={() => void forceFinish()}", /onOk=\{\(\) => void forceFinish\(\)\}/.test(drawerSrc), "");
check("[C] 抽屉 force-finish 调用点唯一", countOf(drawerSrc, "forceFinishHubTask(") === 1, String(countOf(drawerSrc, "forceFinishHubTask(")));
check("[C] 组件不直连 fetch", !/\bfetch\(/.test(drawerSrc), "");
check("[C] 成功后回调状态：onStateChange(task.local_id, data.state || \"failed\")", /onStateChange\(task\.local_id, data\.state \|\| "failed"\)/.test(drawerSrc), "");
check("[C] 成功后刷新：onStateChange 之后紧跟 onRefresh()", /onStateChange\(task\.local_id, data\.state \|\| "failed"\);[\s\S]{0,240}?onRefresh\(\);/.test(drawerSrc), "");
check("[C] 失败原文进抽屉头部行内：onError(`强制结束失败：${text}`)", /onError\(`强制结束失败：\$\{text\}`\)/.test(drawerSrc), "");
check("[C] 抽屉不含「不删除任何文件 / 也不杀进程」字面量（文案单源）", !drawerSrc.includes("不删除任何文件") && !drawerSrc.includes("也不杀进程"), "");
check("[C] 抽屉可见性走 forceFinishGuard（不另写状态枚举）", drawerSrc.includes("forceFinishGuard(task)") && !drawerSrc.includes('"discussing", "rendering", "generating"'), "");
check("[C] 抽屉头部动作区确实渲染了这个入口", /<DrawerForceFinishEntry task=\{summary\}/.test(drawerSrc), "");
check("[C] 抽屉入口失败/成功都关弹层（不留悬挂确认框）", countOf(drawerSrc, "setForceOpen(false)") >= 2, String(countOf(drawerSrc, "setForceOpen(false)")));
check("[C] 抽屉头部行内错误块在场（data-hub-drawer-action-error）", drawerSrc.includes("data-hub-drawer-action-error"), "");

// 同一段文案的逐字核对（B3 常量 → 正文渲染）
const confirmHtml = ssr.renderConfirmBody(RUNNING);
check("[C] 确认正文 = 同一段文案（scope + side-effect 逐字）", confirmHtml.includes(ssr.FORCE_FINISH_CONFIRM_SCOPE) && confirmHtml.includes(ssr.FORCE_FINISH_CONFIRM_SIDE_EFFECT), confirmHtml.slice(0, 200));
check("[C] 确认标题逐字 =「强制结束这个任务？」", ssr.FORCE_FINISH_CONFIRM_TITLE === "强制结束这个任务？", ssr.FORCE_FINISH_CONFIRM_TITLE);
check("[C] 确认正文含「不删除任何文件」「也不杀进程」", confirmHtml.includes("不删除任何文件") && confirmHtml.includes("也不杀进程"), "");
check("[C] 确认正文点名任务号与当前状态（防误点）", confirmHtml.includes("auto-0052") && confirmHtml.includes("discussing"), "");

// ════════════════════════════════════════════════════════════ [D] 改动边界
console.log("4) 改动边界：只碰表格页 / 抽屉 / 探针，B3 四件零改动");

const git = (...args) => execFileSync("git", args, { cwd: REPO, encoding: "utf-8" }).trim();
const mineCommit = git("log", "-1", "--diff-filter=A", "--format=%H", "--", "web/scripts/probe-at18-front2.mjs");
const commitFiles = mineCommit ? git("show", "--name-only", "--pretty=format:", mineCommit).split(/\r?\n/).filter(Boolean) : [];
const workingFiles = [...git("diff", "--name-only", "HEAD").split(/\r?\n/), ...git("ls-files", "--others", "--exclude-standard").split(/\r?\n/)].filter(Boolean);
const B3B_ALLOW = /^web\/(src\/pages\/hub\/(index|task-detail-drawer)\.tsx|scripts\/(probe-at18-front2|probe-at18-front|probe-at16-b6)\.mjs)$/;
const scope = commitFiles.length ? commitFiles : workingFiles.filter((item) => B3B_ALLOW.test(item));
check("[D] 本卡改动文件都在白名单内（表格页 + 抽屉 + 探针）", scope.length > 0 && scope.every((item) => B3B_ALLOW.test(item)), scope.join(", "));
check("[D] commit 内 index.tsx 在清单里（主表落点）", scope.includes("web/src/pages/hub/index.tsx"), scope.join(", "));
check("[D] commit 内 task-detail-drawer.tsx 在清单里（抽屉落点）", scope.includes("web/src/pages/hub/task-detail-drawer.tsx"), scope.join(", "));
check("[D] B3 已交付四件零改动（task-card / hub-status / hub-api / task-delete-confirm）", !scope.some((item) => /pages\/hub\/(task-card|hub-status|hub-api|task-delete-confirm)\.(tsx|ts)$/.test(item)), scope.join(", "));
check("[D] 后端零改动（本卡纯前端）", !scope.some((item) => item.startsWith("wanrepro/")), "");
check("[D] 表格页既有导出未被拆掉（回归：hubRowQuickAction / LapianStageCell 仍在）", typeof ssr.hubRowQuickAction === "function" && typeof ssr.LapianStageCell === "function", "");
check("[D] 抽屉主组件仍导出（TaskDetailDrawer）", typeof ssr.TaskDetailDrawer === "function", "");
// probe-at16-b6 的唯一改动 = 那条「行内操作列渲染 TaskDeleteButton」的整行字面量断言放宽为语义断言
// （行内操作列抽成组件后字面量必然变），删除按钮与 stopPropagation 一字未变。
const b6Src = read("scripts/probe-at16-b6.mjs");
check("[D] probe-at16-b6 的放宽仍是「同一颗 TaskDeleteButton + 同一 stopPropagation」语义断言", b6Src.includes("<TaskDeleteButton") && b6Src.includes("size=\"small\"") && b6Src.includes("stopPropagation") && b6Src.includes("语义匹配"), "");

// ════════════════════════════════════════════════════════════ 收尾
console.log("");
if (fails.length) {
    console.log(`✗ AT-18 B3b 前端探针 FAIL：${pass}/${pass + fails.length} 通过；失败项：`);
    for (const name of fails) console.log(`   · ${name}`);
    process.exit(1);
}
console.log(`✓ AT-18 B3b 前端探针全过：${pass}/${pass} 条断言`);
