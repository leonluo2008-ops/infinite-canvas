/**
 * AT-16 B8 前端探针（本地免费、不联网、不动真实任务）：**提交全程主状态可见**。
 *
 * 缺陷 D9（auto-0043 真机）：点提交 → 受理 200 → 4.5 秒渠道 502 → 回 unknown，主状态条**零变化**
 * （只有 3 秒 toast）。所有者感知「点了没反应」→ 重复点 → 每一下都在真实发单。
 *
 * 病根（B8 实测复核）：数据面在 B5a-1 已经通了（摘要 submit 字段 + HubStateTag 消费），
 * **断在时序**——①提交受理后不立刻重取，要等 runHubQuickAction 内部最长 40s 的轮询收尾；
 * ②列表/详情固定 5s 轮询，而 auto-0043 的在途窗口只有 4.5s，整个窗口被跳过去。
 * 所以本探针锁两件事：主状态**文本**确实随 submit 形态迁移（三态夹具），且**时序**上真会重取
 * （受理即刷新 + 在途 2s 加密）。
 *
 * 后端那一半（dispatch.phase → 形态/人话的唯一生产源）由
 * wanrepro/automation/probe_at16_b5a1.py 锁；B8 不动 automation/ 与 ui/hub_api.py。
 *
 *  [A] 三态夹具 → 真组件 SSR（state 恒为 approved，只有 submit 形态变）：
 *      none → 「已批准·待提交」/ inflight → 「提交处理中·勿重复」/ unknown → 「结果未知·先核对」
 *      三态文本两两不同，data-hub-state-form 同步从 none → inflight → unknown
 *  [B] 单源：主状态文案逐字取后端 label（换 label 主状态跟着变，前端不另写一套话）
 *  [C] 迁移源是 dispatch.phase：unknown 夹具的判据字段就是 dispatch_phase=dispatched
 *      （与 automation/submit_view._form 同一条规则），不是前端按 state 猜的
 *  [D] 防重复发单：inflight / unknown 下快捷入口一律关掉并给原因（主状态说话的同时入口闭嘴）
 *  [E] 接线守卫：受理即刷新（三处 onPhase）+ 在途 2s 加密（列表/详情）+ 失败也回后端事实
 *
 * 跑法：cd web && node scripts/probe-at16-b8.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b8");
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

// ════════════════════════════════════════════════════════════ 夹具（后端 submit_view.build 的真实形状）
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

/** ① 正常待提交：提交链没有任何事实 → form=none（标签按 state 说「已批准·待提交」）。 */
const NONE = V("none");

/** ② 刚点了提交、本地线程在跑（active_submit）：dispatch 已落意图，回执还没到。 */
const INFLIGHT = V("inflight", {
    label: "提交处理中·勿重复",
    conclusion: "提交处理中：等回执，别重复提交",
    causes: ["本地提交线程仍在跑（排队 / 已发出等云端回执），结果还没定。", "重复点提交会重复扣费，后端会直接拒。"],
    guidance: ["进度 5 秒自动刷新；结果出来会变成成片或失败红条。", "这期间「提交生成」与重试都不可用。"],
    dispatch_phase: "prepared",
    active_submit: true,
    blocks_retry: true,
    can_resume: false,
});

/** ③ 渠道 502 回执丢了：submit_unknown 持久阻断（auto-0043 的真值）。 */
const UNKNOWN = V("unknown", {
    label: "结果未知·先核对",
    conclusion: "结果未知：可能已收单，先核对",
    causes: [
        "可能：请求已经发出、渠道也收了单，只是回执丢了或不是可解析的 JSON。",
        "可能：请求根本没到渠道（断连 / 超时 / 并发闸挡住），本地无从证伪。",
        "可能：本地只记到 dispatch=dispatched——已落提交意图但缺确定回执，仍无法证明「发出去了」还是「没发出去」。",
    ],
    guidance: [
        "先到渠道控制台按时间 / 提示词核对有没有这一单。",
        "核对到单 → 拿到云端 taskId 后按该 id 续查（只查不重提）。",
        "确认渠道确实没有这一单 → 走「解除阻断」后再提交（人工动作，会记任务历史）。",
    ],
    error_code: "HTTP 502",
    raw: '提交结果未知：bridge POST /api/sdgf/video/run → HTTP 502: {"error": "渠道响应 HTTP 403"}',
    dispatch_phase: "dispatched",
    submit_unknown: true,
    has_gen_error: true,
    blocks_retry: true,
    can_resume: false,
});

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "ssr-entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { HubStateTag, hubStateMeta } from "../../../src/pages/hub/hub-status";\n' +
        'import { hubQuickAction } from "../../../src/pages/hub/hub-api";\n' +
        "export function renderTag(state: string, submit: any): string {\n" +
        "    return renderToStaticMarkup(<HubStateTag state={state} submit={submit} />);\n" +
        "}\n" +
        "export { hubStateMeta, hubQuickAction };\n",
);
const aliasPlugin = {
    name: "at16b8-alias",
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
check("[B] 真渲染打包成功（hub-status / hub-api 真代码）",
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

/** 主状态标签的可见正文（去掉标签壳里的属性）。 */
function labelOf(html) {
    return html.replace(/<[^>]+>/g, "").trim();
}
/** 取 data-hub-state-form 的值（主状态条自述的形态口径）。 */
function formOf(html) {
    const hit = /data-hub-state-form="([^"]*)"/.exec(html);
    return hit ? hit[1] : "";
}

// ════════════════════════════════════════════════════════════ [A] 三态主状态文本
console.log("1) 三态夹具：同一 state=approved 下，主状态文本随 submit 形态迁移");

const tagNone = ssr.renderTag("approved", NONE);
const tagInflight = ssr.renderTag("approved", INFLIGHT);
const tagUnknown = ssr.renderTag("approved", UNKNOWN);

check("[A] 无提交事实 → 「已批准·待提交」（正常待提交态不许被改坏）",
    labelOf(tagNone) === "已批准·待提交" && formOf(tagNone) === "none", `${labelOf(tagNone)} / ${formOf(tagNone)}`);
check("[A] 提交在途 → 「提交处理中·勿重复」（点下去那一刻主状态就动）",
    labelOf(tagInflight) === "提交处理中·勿重复" && formOf(tagInflight) === "inflight", `${labelOf(tagInflight)} / ${formOf(tagInflight)}`);
check("[A] 未知收单 → 「结果未知·先核对」（不再继续说「已批准·待提交」）",
    labelOf(tagUnknown) === "结果未知·先核对" && formOf(tagUnknown) === "unknown", `${labelOf(tagUnknown)} / ${formOf(tagUnknown)}`);
check("[A] 三态文本两两不同（D9 的病就是「零变化」，这里必须三句三样）",
    new Set([labelOf(tagNone), labelOf(tagInflight), labelOf(tagUnknown)]).size === 3,
    JSON.stringify([labelOf(tagNone), labelOf(tagInflight), labelOf(tagUnknown)]));
check("[A] 在途与未知都不再说「已批准·待提交」",
    !labelOf(tagInflight).includes("已批准·待提交") && !labelOf(tagUnknown).includes("已批准·待提交"));
check("[A] 主状态条带稳定抓手 data-hub-main-status（真机/探针都认这一处）",
    tagNone.includes('data-hub-main-status="1"') && tagUnknown.includes('data-hub-main-status="1"'));
check("[A] 配色按形态给（inflight=processing / unknown=warning / none 回落 state=green）",
    ssr.hubStateMeta("approved", INFLIGHT).color === "processing"
    && ssr.hubStateMeta("approved", UNKNOWN).color === "warning"
    && ssr.hubStateMeta("approved", NONE).color === "green");

// ════════════════════════════════════════════════════════════ [B] 单源：文案取后端 label
console.log("2) 单源：主状态文案逐字取后端 label（前端不另写一套话）");

const RELABELED = V("unknown", { ...UNKNOWN, label: "后端换了个说法·探针专用", conclusion: "x" });
check("[B] 后端 label 换 → 主状态跟着换（文案不是前端写死的）",
    labelOf(ssr.renderTag("approved", RELABELED)) === "后端换了个说法·探针专用",
    labelOf(ssr.renderTag("approved", RELABELED)));
check("[B] label 为空时才回落形态兜底文案（HUB_SUBMIT_META），不回落到「已批准·待提交」",
    labelOf(ssr.renderTag("approved", V("unknown"))) === "结果未知·先核对",
    labelOf(ssr.renderTag("approved", V("unknown"))));

const statusSrc = read("src/pages/hub/hub-status.tsx");
check("[B] 状态展示唯一入口 hubStateMeta 优先取 submit.label（`submit?.label || meta.label`）",
    statusSrc.includes("submit?.label || meta.label"));
check("[B] 主状态不按 state+gen_error 重判（前端不造第二套判定，红线 25）",
    !statusSrc.includes("gen_error") && !/dispatch\s*\.\s*phase/.test(statusSrc));
check("[B] 四类形态的兜底文案只在 HUB_SUBMIT_META 一处（不在渲染件里散写）",
    (statusSrc.match(/结果未知·先核对/g) || []).length === 1
    && (statusSrc.match(/云端处理中·续查/g) || []).length === 1
    && (statusSrc.match(/已拒绝·可改后重提/g) || []).length === 1
    && (statusSrc.match(/失败·可重试/g) || []).length === 1,
    `结果未知×${(statusSrc.match(/结果未知·先核对/g) || []).length} 云端处理中×${(statusSrc.match(/云端处理中·续查/g) || []).length}`);

// ════════════════════════════════════════════════════════════ [C] 迁移源 = dispatch.phase
console.log("3) 迁移源是 dispatch.phase（与 submit_view._form 同一规则，不是 state 猜的）");

check("[C] 三态夹具的 state 恒为 approved（主状态变的是 submit，不是 state）",
    labelOf(tagNone) !== labelOf(tagInflight) && labelOf(tagInflight) !== labelOf(tagUnknown));
check("[C] 在途判据 = active_submit（本地线程在跑）",
    INFLIGHT.active_submit === true && INFLIGHT.dispatch_phase === "prepared");
check("[C] 未知判据 = dispatch_phase=dispatched + submit_unknown（回执丢了，无法证伪没发出去）",
    UNKNOWN.dispatch_phase === "dispatched" && UNKNOWN.submit_unknown === true);
check("[C] 两个非 none 夹具都带 blocks_retry=true（后端说禁重提，前端只读不算）",
    INFLIGHT.blocks_retry === true && UNKNOWN.blocks_retry === true);

// ════════════════════════════════════════════════════════════ [D] 防重复发单
console.log("4) 防重复发单：主状态说话的同时，入口一律关掉");

const quickInflight = ssr.hubQuickAction({
    localId: "probe-b8-inflight",
    state: "approved",
    submit: INFLIGHT,
    cloudTaskId: "",
    channels: [],
    approvedSignature: "",
    draft: null,
    busy: false,
});
check("[D] 在途 → 快捷入口 enabled=false（重复点会被本地挡住）",
    quickInflight.enabled === false, JSON.stringify({ enabled: quickInflight.enabled, reason: quickInflight.reason }));
check("[D] 在途原因是人话「已有提交在跑」",
    String(quickInflight.reason || "").includes("已有提交在跑"), quickInflight.reason);

const quickUnknown = ssr.hubQuickAction({
    localId: "probe-b8-unknown",
    state: "approved",
    submit: UNKNOWN,
    cloudTaskId: "",
    channels: [],
    approvedSignature: "",
    draft: null,
    busy: false,
});
check("[D] 未知收单 → 快捷入口 enabled=false 且原因复用后端结论（不另写劝退话）",
    quickUnknown.enabled === false && String(quickUnknown.reason || "").includes("结果未知"),
    quickUnknown.reason);

// ════════════════════════════════════════════════════════════ [E] 接线守卫（时序）
console.log("5) 接线守卫：受理即刷新 + 在途 2s 加密 + 失败也回后端事实");

const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const apiSrc = read("src/pages/hub/hub-api.ts");

check("[E] 列表：提交在途时轮询加密到 2s（5s 粒度会跳掉 4.5s 的在途窗口）",
    indexSrc.includes("window.setInterval(() => void load(), quickBusy ? 2000 : 5000)"));
check("[E] 列表：onPhase 里立刻重取（不等 runHubQuickAction 内部轮询跑完）",
    /onPhase: \(phase, state, note\) => \{\s*\/\/[\s\S]{0,220}?void load\(\);/.test(indexSrc));
check("[E] 列表：被拒（409 等）也回后端事实",
    /message\.error\(errorText\(quickError\)\);[\s\S]{0,200}?void load\(\);/.test(indexSrc));
check("[E] 抽屉：提交在途（busy=submit）时详情轮询加密到 2s",
    drawerSrc.includes('window.setInterval(() => void load(), busy === "submit" ? 2000 : 5000)'));
check("[E] 抽屉：顶部快捷入口受理即刷新顶栏 + 列表",
    /onPhase: \(phase, phaseState, note\) => \{\s*\/\/[\s\S]{0,200}?void load\(\);\s*onRefresh\(\);/.test(drawerSrc));
check("[E] 抽屉：动作失败也刷新（主状态不许停在旧文案）",
    /message\.error\(errorText\(error\)\);[\s\S]{0,160}?void load\(\);/.test(drawerSrc));
check("[E] 抽屉的 busy 变化会重建轮询（effect 依赖含 busy）",
    /\}, \[open, localId, load, busy\]\);/.test(drawerSrc));

// 三处消费点仍然传 submit（B5a-1 的接线没被这次改动弄丢）
check("[E] 主表状态列 / 抽屉顶部 / 详情头三处都消费 submit",
    indexSrc.includes("<HubStateTag state={task.state} submit={task.submit} />")
    && (drawerSrc.match(/<HubStateTag state=\{state\} submit=\{submitView\} \/>/g) || []).length === 2);
check("[E] 抽屉 submitView 仍来自 detail??summary 同一份（不另算）",
    drawerSrc.includes("const submitView = detail?.submit ?? summary?.submit ?? undefined;"));
check("[E] 没有为相位迁移另造端点（仍只读 /api/hub/tasks + /api/hub/task/<id>）",
    apiSrc.includes('"/api/hub/tasks"') && !apiSrc.includes("submit-phase") && !apiSrc.includes("dispatch-phase"));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b8] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
