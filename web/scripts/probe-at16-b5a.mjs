/**
 * AT-16 B5a-1 前端探针（本地免费、不联网、不动真实任务）：**提交结果可见与人话说明**。
 *
 * 缺陷 D6 展示面：提交失败/未知后列表与详情零信息、状态标签按 state 说「已批准·待提交」。
 * 后端那一半（四类形态判定 + 摘要生产）由 wanrepro/automation/probe_at16_b5a1.py 锁；
 * 本探针锁前端这一半：
 *
 *  [A] 纯函数：genIssue 只消费后端 form（不自己按 state+gen_error 重判）
 *      · unknown → 禁重提（blocksRetry=true）、标题是人话「结果未知…先核对」
 *      · rejected → 可改后重提（blocksRetry=false）
 *      · inflight → 禁重提 + 有 taskId 才给续查
 *      · failed → 可重试（blocksRetry=false）
 *      · 旧 bridge 无 submit → 既有语义逐条不变（active/unknown/dispatch/孤儿三步/none）
 *      · 有 submit 时孤儿三步③的「受控恢复」文案不被摘要口径覆盖（细粒度优先）
 *  [B] 真渲染（真组件 + SSR）：
 *      · HubStateTag：approved+unknown → 「结果未知·先核对」，不再说「已批准·待提交」
 *      · SubmitResultLine：结论 + 短错误码 + 「详情」入口；form=none → 不渲染
 *      · 三处同源：主表行内 / 详情头 / 抽屉顶部 → 标签文案与结论逐字一致（同一份 submit）
 *      · normalizeSubmitView 保真（causes/guidance 数组、raw、blocks_retry），两轮归一一致
 *  [C] 静态接线守卫：
 *      · 三处消费点都传 submit（index.tsx 状态列 / drawer 标题 / drawer 详情头）
 *      · 列表与详情走同一个 normalizeSubmitView（一个生产口径）
 *      · 没有为提交结果另造端点；主表没有直接重提按钮（禁盲目重提）
 *
 * 跑法：cd web && node scripts/probe-at16-b5a.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b5a");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 240)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

// ════════════════════════════════════════════════════════════ 夹具（后端 submit_view 的真实形状）
const V = (form, extra = {}) => ({
    form,
    label: "",
    conclusion: "",
    causes: [],
    guidance: [],
    error_code: "",
    raw: "",
    dispatch_phase: "",
    submit_unknown: false,
    active_submit: false,
    has_gen_error: false,
    cloud_task_id: "",
    blocks_retry: false,
    can_resume: false,
    ...extra,
});

const UNKNOWN = V("unknown", {
    label: "结果未知·先核对",
    conclusion: "结果未知：可能已收单，先核对",
    causes: [
        "可能：请求已经发出、渠道也收了单，只是回执丢了或不是可解析的 JSON。",
        "可能：请求根本没到渠道（断连 / 超时 / 并发闸挡住），本地无从证伪。",
    ],
    guidance: ["先到渠道控制台按时间 / 提示词核对有没有这一单。", "确认没单 → 走「解除阻断」后再提交。"],
    error_code: "HTTP 403",
    raw: "video_request_failed: HTTP 403 Forbidden（渠道拒绝）",
    dispatch_phase: "dispatched",
    submit_unknown: true,
    has_gen_error: true,
    blocks_retry: true,
    can_resume: false,
});
const REJECTED = V("rejected", {
    label: "已拒绝·可改后重提",
    conclusion: "已拒绝：可改后重提",
    causes: ["可能：渠道前置校验挡下（HTTP 400/409）。"],
    guidance: ["按原文改配置。", "改完重新批准 → 再提交。"],
    error_code: "HTTP 409",
    raw: "渠道前置拒绝（HTTP 409）：并发闸满",
    blocks_retry: false,
});
const INFLIGHT = V("inflight", {
    label: "等待超时·续查",
    conclusion: "云端处理中：按 taskId 续查",
    causes: ["云端任务仍在跑：远程模型通常 5–15 分钟。"],
    guidance: ["按云端 taskId task-abc 续查（只查不重提）。"],
    has_gen_error: true,
    cloud_task_id: "task-abc",
    blocks_retry: true,
    can_resume: true,
});
const FAILED = V("failed", {
    label: "失败·可重试",
    conclusion: "失败：可重试",
    causes: ["云端给出明确终态失败（不是超时）——可能：key 失效 / 额度用尽 / 渠道故障，以渠道原文为准。"],
    guidance: ["先按原文修配置再重新提交。"],
    error_code: "HTTP 502",
    raw: "HTTP 502 bad gateway",
    has_gen_error: true,
    blocks_retry: false,
});

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { HubStateTag, SubmitResultLine, hubStateMeta, HUB_SUBMIT_META } from "../../../src/pages/hub/hub-status";\n' +
        'import { normalizeSubmitView } from "../../../src/pages/hub/hub-api";\n' +
        'import { genIssue } from "../../../src/lib/hub/gen-settings";\n' +
        "export function renderTag(state: string, submit: any): string {\n" +
        "    return renderToStaticMarkup(<HubStateTag state={state} submit={submit} />);\n" +
        "}\n" +
        "export function renderLine(submit: any): string {\n" +
        "    return renderToStaticMarkup(<SubmitResultLine submit={submit} />);\n" +
        "}\n" +
        "export { hubStateMeta, HUB_SUBMIT_META, normalizeSubmitView, genIssue };\n",
);
const aliasPlugin = {
    name: "at16b5a-alias",
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
check("[B] 真渲染打包成功（hub-status / hub-api / gen-settings 真代码）",
    built.errors.length === 0, built.errors.map((err) => `${err.location?.file}:${err.text}`).join("; "));

// 纯 node 里跑真前端模块：补最小浏览器全局（i18n 在 import 期读 localStorage）
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

const ssr = await import(pathToFileURL(ssrOut).href + `?t=${Date.now()}`);

// ════════════════════════════════════════════════════════════ [A] genIssue 消费后端形态
console.log("1) genIssue：四类形态只消费后端 form（禁第二份判定）");

const iUnknown = ssr.genIssue({ state: "approved", submit: UNKNOWN });
check("[A] unknown → 禁重提 + 人话结论来自后端",
    iUnknown.blocksRetry === true && iUnknown.form === "unknown"
    && iUnknown.title === UNKNOWN.conclusion, JSON.stringify(iUnknown).slice(0, 200));
check("[A] unknown → 可能原因并列上屏（不是唯一原因）",
    iUnknown.detail.includes("可能") && iUnknown.detail.includes("回执丢了"), iUnknown.detail);
check("[A] unknown → 无 taskId 不给续查入口（不假装能续查）", iUnknown.canResume === false);

const iRejected = ssr.genIssue({ state: "awaiting_approval", submit: REJECTED });
check("[A] rejected → 可改后重提（blocksRetry=false）",
    iRejected.blocksRetry === false && iRejected.form === "rejected"
    && iRejected.title.includes("可改后重提"), JSON.stringify(iRejected).slice(0, 200));

const iInflight = ssr.genIssue({ state: "generating", cloud_task_id: "task-abc", submit: INFLIGHT });
check("[A] inflight → 禁重提 + 按 taskId 续查",
    iInflight.blocksRetry === true && iInflight.canResume === true && iInflight.title.includes("续查"),
    JSON.stringify(iInflight).slice(0, 200));

const iFailed = ssr.genIssue({ state: "failed", submit: FAILED });
check("[A] failed → 可重试（blocksRetry=false）",
    iFailed.blocksRetry === false && iFailed.form === "failed" && iFailed.title.includes("可重试"),
    JSON.stringify(iFailed).slice(0, 200));

const iNone = ssr.genIssue({ state: "approved", submit: V("none") });
check("[A] form=none → 无阻断面板（正常待提交不编造结果）", iNone.kind === "none" && iNone.blocksRetry === false);

// 旧 bridge（没有 submit 字段）：既有语义逐条不变
check("[A] 旧 bridge：active_submit → 提交进行中 + 禁重提",
    ssr.genIssue({ state: "generating", active_submit: true }).kind === "active"
    && ssr.genIssue({ state: "generating", active_submit: true }).blocksRetry === true);
check("[A] 旧 bridge：submit_unknown → 阻断三步指引",
    ssr.genIssue({ state: "approved", submit_unknown: { reason: "超时未知" } }).steps.join("").includes("解除阻断"));
check("[A] 旧 bridge：dispatch=dispatched → 阻断（可能已收单）",
    ssr.genIssue({ state: "approved", dispatch: { phase: "dispatched" } }).blocksRetry === true);
check("[A] 旧 bridge：孤儿三步③ → 受控恢复文案保留",
    ssr.genIssue({ state: "generating", submit_lost: { step: 3, submit_lost: true } }).steps.join("").includes("受控恢复"));
check("[A] 旧 bridge：generating+submit_lost=null → 不误判（既有语义）",
    ssr.genIssue({ state: "generating", submit_lost: null }).kind === "none");

// 有 submit 时，孤儿三步③的细粒度文案优先（不能被摘要口径抹成泛泛「续查」）
const iOrphan3 = ssr.genIssue({
    state: "generating",
    submit: INFLIGHT,
    submit_lost: { step: 3, submit_lost: true, reason: "无在途证据" },
});
check("[A] 有 submit 时孤儿三步③仍走「受控恢复」细文案（不被四类形态覆盖）",
    iOrphan3.kind === "orphan" && iOrphan3.steps.join("").includes("受控恢复"), JSON.stringify(iOrphan3).slice(0, 200));

// ════════════════════════════════════════════════════════════ [B] 真渲染
console.log("2) 真渲染：状态标签 + 提交结果行（三处同源）");

/** 取被标记元素的正文。 */
function textOf(html, marker) {
    const at = html.indexOf(marker);
    if (at < 0) return "";
    const gt = html.indexOf(">", at);
    if (gt < 0) return "";
    const end = html.indexOf("<", gt + 1);
    return html.slice(gt + 1, end < 0 ? undefined : end);
}

const tagUnknown = ssr.renderTag("approved", UNKNOWN);
check("[B] approved+unknown → 标签说「结果未知·先核对」，不再说「已批准·待提交」",
    tagUnknown.includes("结果未知·先核对") && !tagUnknown.includes("已批准·待提交"), tagUnknown.slice(0, 200));
const tagPlain = ssr.renderTag("approved", null);
check("[B] approved 无提交结果 → 仍按 state 说「已批准·待提交」（不误伤正常态）",
    tagPlain.includes("已批准·待提交"), tagPlain.slice(0, 200));
const tagNone = ssr.renderTag("awaiting_approval", V("none"));
check("[B] form=none 的 submit 不覆盖 state 标签", tagNone.includes("待审批"), tagNone.slice(0, 160));

const metaUnknown = ssr.hubStateMeta("approved", UNKNOWN);
check("[B] 标签配色按形态给（unknown=warning），文案取后端 label",
    metaUnknown.label === "结果未知·先核对" && metaUnknown.color === "warning", JSON.stringify(metaUnknown));
check("[B] 四类形态都有配色（unknown/rejected/inflight/failed）",
    ["unknown", "rejected", "inflight", "failed"].every((form) => ssr.HUB_SUBMIT_META[form]?.color));

const lineUnknown = ssr.renderLine(UNKNOWN);
check("[B] 提交结果行渲染结论（人话）",
    lineUnknown.includes("data-hub-submit-line=\"unknown\"") && textOf(lineUnknown, "data-hub-submit-conclusion").includes("可能已收单"),
    lineUnknown.slice(0, 240));
check("[B] 提交结果行带短错误码（HTTP 403，不翻译原因）",
    textOf(lineUnknown, "data-hub-submit-code").includes("HTTP 403"), lineUnknown.slice(0, 240));
check("[B] 提交结果行有可展开入口（可能原因/原文）", lineUnknown.includes("data-hub-submit-more"));
check("[B] form=none → 结果行不渲染（不占位、不留空壳）", ssr.renderLine(V("none")) === "");

// 三处同源：同一份 submit 在三处（列表/详情头/抽屉顶部）渲染出的标签与结论逐字一致
const three = [
    { place: "主表状态列", tag: ssr.renderTag("approved", UNKNOWN), line: ssr.renderLine(UNKNOWN) },
    { place: "详情头", tag: ssr.renderTag("approved", UNKNOWN), line: ssr.renderLine(UNKNOWN) },
    { place: "抽屉顶部", tag: ssr.renderTag("approved", UNKNOWN), line: null },
];
const tagTexts = three.map((row) => row.tag.replace(/<[^>]+>/g, "").trim());
check("[B] 三处标签文案逐字一致（同一份 submit → 同一句话）",
    new Set(tagTexts).size === 1 && tagTexts[0] === "结果未知·先核对", JSON.stringify(tagTexts));
const lineTexts = three.filter((row) => row.line !== null).map((row) => textOf(row.line, "data-hub-submit-conclusion"));
check("[B] 列表与详情头的结论逐字一致", new Set(lineTexts).size === 1 && lineTexts[0].includes("可能已收单"),
    JSON.stringify(lineTexts));

// 归一保真（摘要 → 前端字段，5 秒轮询每轮都走这条）
const norm = ssr.normalizeSubmitView(UNKNOWN);
check("[B] normalizeSubmitView 保真：形态/结论/原因/指引/原文/blocks_retry 全在",
    norm.form === "unknown" && norm.conclusion === UNKNOWN.conclusion && norm.causes.length === 2
    && norm.guidance.length === 2 && norm.raw === UNKNOWN.raw && norm.blocks_retry === true,
    JSON.stringify(norm).slice(0, 240));
const norm2 = ssr.normalizeSubmitView(UNKNOWN);
check("[B] 两轮归一完全一致（轮询刷新不会把错误信息抹掉）", JSON.stringify(norm) === JSON.stringify(norm2));
check("[B] 畸形/旧 bridge 摘要 → form=none（不假装有提交结果）",
    ssr.normalizeSubmitView(null).form === "none"
    && ssr.normalizeSubmitView({ form: "weird" }).form === "none"
    && ssr.normalizeSubmitView({ form: "failed", causes: "not-array" }).causes.length === 0);

// ════════════════════════════════════════════════════════════ [C] 静态接线守卫
console.log("3) 静态接线：三处消费点 + 同源 + 无新端点 + 无盲目重提");

const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const apiSrc = read("src/pages/hub/hub-api.ts");
const statusSrc = read("src/pages/hub/hub-status.tsx");
const genSrc = read("src/lib/hub/gen-settings.ts");

check("[C] 主表状态列：HubStateTag 带 submit + 结果行",
    indexSrc.includes("<HubStateTag state={task.state} submit={task.submit} />")
    && indexSrc.includes("<SubmitResultLine submit={task.submit} />"));
check("[C] 抽屉顶部：标题标签带 submit",
    drawerSrc.includes("<HubStateTag state={state} submit={submitView} />"));
check("[C] 详情头：标签带 submit + 结果行就在头部区块",
    drawerSrc.includes("<HubStateTag state={state} submit={submitView} />")
    && drawerSrc.includes("<SubmitResultLine submit={submitView} />"));
check("[C] 抽屉的 submitView 来自 detail??summary 同一份（不另算）",
    drawerSrc.includes("detail?.submit ?? summary?.submit"));
check("[C] 列表与详情都走 normalizeSubmitView（一个归一口径）",
    apiSrc.includes("submit: normalizeSubmitView(row.submit)")
    && apiSrc.includes("submit: normalizeSubmitView(task.submit)"));
check("[C] 标签配色只在前端映射、文案取后端 label（不在前端另写一套话术）",
    statusSrc.includes("submit?.label || meta.label") && !statusSrc.includes("可能已收单"));
check("[C] 前端形态只从 detail.submit.form 取（不按 state+gen_error 重判）",
    genSrc.includes('const sv = detail.submit && detail.submit.form ? detail.submit : null')
    && genSrc.includes('const form = sv && sv.form !== "none" ? sv.form : ""'));
check("[C] 没有为提交结果另造端点（摘要仍只读 /api/hub/tasks，无 submit-result / submit-diag 路由）",
    apiSrc.includes('"/api/hub/tasks"') && !apiSrc.includes("submit-result") && !apiSrc.includes("submit-diag"));
check("[C] 主表没有直接重提按钮（禁盲目重提；重提入口是 B5a-2 的事）",
    !indexSrc.includes("submitHubTask"));
check("[C] 未知/在途仍由 blocksRetry 挡住重试按钮（既有闸门没被绕）",
    drawerSrc.includes("issue.blocksRetry") && drawerSrc.includes("<RetryButton localId={localId} busy={busy} act={act} disabled />"));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b5a] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
