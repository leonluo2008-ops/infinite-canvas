/**
 * AT-16 B10 前端探针（本地免费、不联网、不动真实任务）：**收尾 toast 与主状态条不再打架**。
 *
 * 缺陷（B8 施工中发现）：`runHubQuickAction` 的收尾判定只看 `state ∈ {generating, done, failed}`。
 * 提交落到 **unknown / rejected** 时 `state` 仍停在 approved / awaiting_approval → 循环跑满
 * 20 轮 × 2s = **40 秒**后落 `phase="timeout"`，两处入口都弹「提交仍在处理中」；而此时主状态条
 * （B8 之后）早就按后端 label 红了「结果未知·先核对」——同一个任务、同一时刻，两块 UI 两套话，
 * 所有者按 toast 理解就会去重试（后端随后 409/阻断）。
 *
 * 修法（单源，禁造第二套判定）：收尾判定补上后端已经给的结论——轮询到
 * `submit.form ∈ {unknown, rejected, failed}`（B5a-1 摘要/详情同一生产源
 * `automation/submit_view.build`）→ 立刻按该形态收尾，toast 文案逐字取后端 `conclusion`
 * （缺则 `label`，前端不新写劝退话）；`timeout` 只留给「state 仍在跑、submit 也没有结论」的真在途。
 *
 * 本探针锁四件事：
 *  [A] 唯一判定点 `hubQuickSubmitConclusion`：只认 three-form，文案逐字来自后端；inflight/none/畸形 → null
 *  [B] 三态夹具（inflight→unknown / inflight→rejected / inflight→failed）：≤2 轮收尾、note 逐字=后端
 *      conclusion、**不再落 timeout**（旧实现是 20 轮 / 40 秒）
 *  [C] 真在途不误伤：generating + inflight 仍轮询到 generating/done 报「云端已收单」；
 *      timeout 文案逐字不变；failed 无结论时仍走既有「云端已回执：失败」
 *  [D] 与主状态条同源不打架：同一份 submit 上，主状态条说后端 `label`、toast 说后端 `conclusion`，
 *      两个都是后端生产；toast 绝不等于 timeout 的「仍在处理中」那套话
 *  [E] 接线守卫：三处 onPhase 仍走同一份 `hubQuickPhaseText` + `note || line.text`；
 *      两个入口零第二套判定；轮询上限/间隔默认值未动
 *
 * 后端（automation/ 与 ui/hub_api.py）本卡一行不改——判据源仍由
 * `wanrepro/automation/probe_at16_b5a1.py` 锁。
 *
 * 跑法：cd web && node scripts/probe-at16-b10.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b10");
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

// ════════════════════════════════════════════════════════════ 夹具（后端 submit_view.build 的真实形状与逐字文案）
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

/** ③ inflight：刚点了提交、本地线程在跑（`_inflight_text` active_submit 分支）。 */
const INFLIGHT = V("inflight", {
    label: "提交处理中·勿重复",
    conclusion: "提交处理中：等回执，别重复提交",
    causes: [
        "本地提交线程仍在跑（排队 / 已发出等云端回执），结果还没定。",
        "重复点提交会重复扣费，后端会直接拒。",
    ],
    guidance: ["进度 5 秒自动刷新；结果出来会变成成片或失败红条。", "这期间「提交生成」与重试都不可用。"],
    dispatch_phase: "prepared",
    active_submit: true,
    blocks_retry: true,
});

/** ① unknown：auto-0043 真机形态——渠道 502、回执丢了，state 仍 approved（`_unknown_text` 逐字）。 */
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
});

/** ② rejected：渠道前置拒绝 → 受控回退 awaiting_approval + gen_error（`_rejected_text` 逐字）。 */
const REJECTED = V("rejected", {
    label: "已拒绝·可改后重提",
    conclusion: "已拒绝：可改后重提",
    causes: [
        "可能：渠道前置校验挡下（HTTP 400/409——参数不被接受 / 并发闸满 / 状态不符）。",
        "可能：本地预检没过（缺配置、上传图读到 / 转 dataUrl 失败、批准快照失效）。",
        "本次没有拿到云端 taskId，属「没发出去」的回退态，不是未知收单。",
    ],
    guidance: ["按原文改配置（渠道 / 模型 / 画幅 / 时长 / key）。", "改完重新批准 → 再提交；本次不产生付费单。"],
    error_code: "HTTP 409",
    raw: '渠道前置拒绝：HTTP 409: {"error": "duration 不支持"}',
    has_gen_error: true,
});

/** ④ failed：云端明确终态失败（`_failed_text` 逐字）。 */
const FAILED = V("failed", {
    label: "失败·可重试",
    conclusion: "失败：可重试",
    causes: ["云端给出明确终态失败（不是超时）——可能：key 失效 / 额度用尽 / 渠道故障 / 内容被拒，具体以渠道原文为准。"],
    guidance: ["先按原文修配置（key / 额度 / 模型 / 内容）再重新提交。", "重提前确认渠道没有残留任务（有就按 taskId 续查，别重复扣费）。"],
    error_code: "video_request_failed",
    raw: "云端生成失败：generation_failed",
    has_gen_error: true,
});

const NONE = V("none");
/** 旧 bridge：只认 state+gen_error，没有 submit 字段（归一后 = none）。 */
const OLD_BRIDGE = undefined;

// 三态夹具：state 仍停在「批准/待批准」，只有 submit 形态在推进（正是旧实现空跑 40s 的那三帧）
const THREE = [
    { name: "unknown", form: "unknown", state: "approved", submit: UNKNOWN, expectNote: "结果未知：可能已收单，先核对" },
    { name: "rejected", form: "rejected", state: "awaiting_approval", submit: REJECTED, expectNote: "已拒绝：可改后重提" },
    { name: "failed", form: "failed", state: "failed", submit: FAILED, expectNote: "失败：可重试" },
];

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { HubStateTag, hubStateMeta } from "../../../src/pages/hub/hub-status";\n' +
        'import { hubQuickAction, hubQuickPhaseText, hubQuickSubmitConclusion, runHubQuickAction } from "../../../src/pages/hub/hub-api";\n' +
        "export function renderTag(state: string, submit: any): string {\n" +
        "    return renderToStaticMarkup(<HubStateTag state={state} submit={submit} />);\n" +
        "}\n" +
        "export { hubStateMeta, hubQuickAction, hubQuickPhaseText, hubQuickSubmitConclusion, runHubQuickAction };\n",
);
const aliasPlugin = {
    name: "at16b10-alias",
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
const out = path.join(CACHE, "bundle.mjs");
const built = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: out,
    jsx: "automatic",
    packages: "external",
    plugins: [aliasPlugin],
    define: { "import.meta.env": "{}" },
    loader: { ".css": "empty" },
    logLevel: "silent",
});
check("[0] 真代码打包成功（hub-api / hub-status 真模块，无第二份实现）", built.errors.length === 0,
    built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

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

const api = await import(pathToFileURL(out).href + `?t=${Date.now()}`);
const { hubStateMeta, hubQuickAction, hubQuickPhaseText, hubQuickSubmitConclusion, runHubQuickAction } = api;

/** 主状态标签的可见正文（去掉标签壳里的属性）。 */
const labelOf = (html) => html.replace(/<[^>]+>/g, "").trim();

// ════════════════════════════════════════════════════════════ [A] 唯一判定点
console.log("1) hubQuickSubmitConclusion：只认后端已给结论的三形态，文案逐字取后端");

for (const row of THREE) {
    const hit = hubQuickSubmitConclusion(row.submit);
    check(`[A] ${row.name} → 判定命中 form=${row.form}`, Boolean(hit) && hit.form === row.form, JSON.stringify(hit));
    check(`[A] ${row.name} → note 逐字等于后端 conclusion（前端不另写劝退话）`, hit && hit.note === row.submit.conclusion,
        `note=${hit && hit.note} | conclusion=${row.submit.conclusion}`);
}
check("[A] inflight（真在途）→ null（继续轮询，不算结论）", hubQuickSubmitConclusion(INFLIGHT) === null);
check("[A] none（没提交过）→ null", hubQuickSubmitConclusion(NONE) === null);
check("[A] 缺字段（旧 bridge）→ null（不假装有结论）", hubQuickSubmitConclusion(OLD_BRIDGE) === null && hubQuickSubmitConclusion(null) === null);
check("[A] 畸形 form（\"weird\"）→ null（归一成 none，不猜）", hubQuickSubmitConclusion(V("weird", { conclusion: "乱写的" })) === null);
check("[A] conclusion 为空时回落后端 label（仍是后端文案，不是前端话）",
    hubQuickSubmitConclusion(V("unknown", { label: "结果未知·先核对" }))?.note === "结果未知·先核对");

// ════════════════════════════════════════════════════════════ 打桩执行器
/** 帧序列 [{state, submit?}]：第 1 帧 = 受理后第一次轮询（在途），第 2 帧起 = 后端回执落地。 */
function deps(frames, calls = []) {
    let i = 0;
    return {
        submit: async (id, key) => {
            calls.push({ kind: "submit", id, key });
        },
        fetchTask: async () => {
            const frame = frames[Math.min(i, frames.length - 1)];
            i += 1;
            calls.push({ kind: "fetch", state: frame.state, form: frame.submit ? frame.submit.form : "" });
            return { state: frame.state, submit: frame.submit };
        },
        sleep: async () => undefined,
        intervalMs: 1,
        maxPolls: 20,          // = 生产的默认上限（20 轮 × 2s = 40s）——三态必须在 ≤2 轮内收尾
    };
}
const fetches = (calls) => calls.filter((call) => call.kind === "fetch").length;
const submits = (calls) => calls.filter((call) => call.kind === "submit").length;
const okAction = (localId) => hubQuickAction({
    localId,
    state: "approved",
    submit: NONE,
    channels: [{ id: "ch-sdgf", name: "SDGF 主渠道", baseUrl: "https://ch-sdgf.example.com", apiFormat: "sdgf", apiKey: "sk-sdgf-123" }],
    approvedSignature: JSON.stringify({
        channelId: "ch-sdgf", baseUrl: "https://ch-sdgf.example.com", apiFormat: "sdgf", "渠道名": "SDGF 主渠道",
        model: "sdgf-video-1", mode: "frames", params: { duration: 6, size: "16:9", vquality: "720", audio: true },
    }),
    draft: null,
});

// ════════════════════════════════════════════════════════════ [B] 三态夹具：≤2 轮收尾、不落 timeout
console.log("2) 三态夹具（inflight→unknown / →rejected / →failed）：≤2 轮收尾、note 逐字、不落 timeout");

for (const row of THREE) {
    const calls = [];
    const phases = [];
    const res = await runHubQuickAction(`auto-b10-${row.form}`, okAction(`auto-b10-${row.form}`), {
        ...deps([{ state: row.state === "failed" ? "approved" : row.state, submit: INFLIGHT }, { state: row.state, submit: row.submit }], calls),
        onPhase: (phase) => phases.push(phase),
    });
    check(`[B] ${row.name}: phase=concluded（不是 timeout/confirmed）`, res.phase === "concluded", JSON.stringify(res));
    check(`[B] ${row.name}: form=${row.form}`, res.form === row.form, String(res.form));
    check(`[B] ${row.name}: note 逐字等于后端 conclusion`, res.note === row.expectNote, `note=${res.note}`);
    check(`[B] ${row.name}: ≤2 轮内收尾（实测 ${fetches(calls)} 次轮询；旧实现跑满 20 轮 / 40s）`, fetches(calls) <= 2, JSON.stringify(calls));
    check(`[B] ${row.name}: 阶段序列 = accepted→concluded（不出现 timeout）`, phases.join(",") === "accepted,concluded", JSON.stringify(phases));
    check(`[B] ${row.name}: 只发一次提交请求（结论来自读详情，不是重发）`, submits(calls) === 1, JSON.stringify(calls));
    check(`[B] ${row.name}: toast 不是 timeout 那套「仍在处理中」`, res.note !== hubQuickPhaseText("timeout").text && !res.note.includes("仍在处理中"), res.note);
}

// 同场景对照：第一帧就拿到结论（回执落得比一轮轮询还快）→ 1 轮收尾
const quickCalls = [];
const quickRes = await runHubQuickAction("auto-b10-fast", okAction("auto-b10-fast"), deps([{ state: "approved", submit: UNKNOWN }], quickCalls));
check("[B] 后端结论比首轮轮询来得更快 → 第 1 轮就收尾（1 次轮询）", quickRes.phase === "concluded" && fetches(quickCalls) === 1, `phase=${quickRes.phase} fetches=${fetches(quickCalls)}`);

// ════════════════════════════════════════════════════════════ [C] 真在途不误伤
console.log("3) 真在途（state=generating、submit=inflight）仍走既有轮询；timeout 文案逐字不变");

const genCalls = [];
const genPhases = [];
const genRes = await runHubQuickAction("auto-b10-gen", okAction("auto-b10-gen"), {
    ...deps([{ state: "approved", submit: INFLIGHT }, { state: "generating", submit: INFLIGHT }, { state: "generating", submit: INFLIGHT }], genCalls),
    onPhase: (phase) => genPhases.push(phase),
});
check("[C] generating + inflight → confirmed(state=generating)，不误判成结论", genRes.phase === "confirmed" && genRes.state === "generating" && genRes.note === "", JSON.stringify(genRes));
check("[C] 真在途确实轮询了多轮（不是第一帧就收）", fetches(genCalls) === 2, JSON.stringify(genCalls));
check("[C] 阶段序列 = accepted→confirmed", genPhases.join(",") === "accepted,confirmed", JSON.stringify(genPhases));

const doneRes = await runHubQuickAction("auto-b10-done", okAction("auto-b10-done"), deps([{ state: "done", submit: NONE }]));
check("[C] done 仍报 confirmed（成片已出，文案含「成片已出」）",
    doneRes.phase === "confirmed" && hubQuickPhaseText("confirmed", doneRes.state).text.includes("成片已出"), JSON.stringify(doneRes));

const failLegacyRes = await runHubQuickAction("auto-b10-legacyfail", okAction("auto-b10-legacyfail"), deps([{ state: "failed", submit: NONE }]));
check("[C] failed 但 submit 没结论（旧 bridge）→ 沿用既有 confirmed +「云端已回执：失败」",
    failLegacyRes.phase === "confirmed" && hubQuickPhaseText("confirmed", "failed").text.includes("失败"), JSON.stringify(failLegacyRes));

const waitCalls = [];
const waitPhases = [];
const waitRes = await runHubQuickAction("auto-b10-wait", okAction("auto-b10-wait"), {
    ...deps([{ state: "approved", submit: INFLIGHT }], waitCalls),
    onPhase: (phase) => waitPhases.push(phase),
});
check("[C] state 没推进 + submit=inflight（真在途）→ 仍是 timeout，不慌报结论", waitRes.phase === "timeout" && waitRes.state === "" && waitRes.note === "", JSON.stringify(waitRes));
check("[C] 真在途跑满上限 20 轮才 timeout", fetches(waitCalls) === 20, `fetches=${fetches(waitCalls)}`);
check("[C] 阶段序列 = accepted→timeout", waitPhases.join(",") === "accepted,timeout", JSON.stringify(waitPhases));
check("[C] timeout 文案逐字不变（不误伤真在途）",
    hubQuickPhaseText("timeout").text === "提交仍在处理中：还没转成 generating——前端超时 ≠ 云端失败，5 秒刷新会自己跟上；按 taskId 可续查"
    && hubQuickPhaseText("timeout").level === "info", hubQuickPhaseText("timeout").text);

// ════════════════════════════════════════════════════════════ [D] 与主状态条同源：两块 UI 一套话
console.log("4) 同一份 submit 上：主状态条说后端 label、toast 说后端 conclusion——不再两套话");

for (const row of THREE) {
    const meta = hubStateMeta(row.state, row.submit);
    const tag = labelOf(api.renderTag(row.state, row.submit));
    const note = hubQuickSubmitConclusion(row.submit).note;
    check(`[D] ${row.name}: 主状态条文案 = 后端 label（逐字）`, meta.label === row.submit.label && tag === row.submit.label, `meta=${meta.label} tag=${tag}`);
    check(`[D] ${row.name}: toast 文案 = 后端 conclusion（逐字）`, note === row.submit.conclusion, note);
    check(`[D] ${row.name}: 两块 UI 同一形态（不再是「红条说未知 + toast 说仍在处理中」）`,
        meta.label.includes(row.submit.label.split("·")[0]) && note.startsWith(row.submit.conclusion.split("：")[0]), `${meta.label} / ${note}`);
    check(`[D] ${row.name}: toast 与 timeout 文案互不相同（打架的指纹）`, note !== hubQuickPhaseText("timeout").text);
}

// 形态与语气：结论类一律 warning（要人处理），不是 info（info 是「等着就好」）
check("[D] concluded 语气 = warning", hubQuickPhaseText("concluded").level === "warning");
check("[D] concluded 文案不在前端生产（空串，由后端 note 覆盖）", hubQuickPhaseText("concluded").text === "");

// ════════════════════════════════════════════════════════════ [E] 接线守卫
console.log("5) 接线守卫：单点判定 / 三处 onPhase 同一份人话 / 上限与间隔未动");

const apiSrc = read("src/pages/hub/hub-api.ts");
const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");

check("[E] HubQuickPhase 已扩 concluded 五态", /export type HubQuickPhase = "accepted" \| "confirmed" \| "concluded" \| "timeout" \| "rejected"/.test(apiSrc));
check("[E] 收尾判定只调一个纯函数（不许在循环里另写形态判断）",
    apiSrc.includes("const concluded = hubQuickSubmitConclusion(task?.submit);") && apiSrc.includes('return { phase: "concluded", state, note: concluded.note, form: concluded.form };'));
check("[E] 轮询默认上限/间隔未动（真在途仍 20 轮 × 2000ms，timeout 只留给它们）",
    apiSrc.includes("const intervalMs = deps.intervalMs ?? 2000;") && apiSrc.includes("const maxPolls = deps.maxPolls ?? 20;"));
check("[E] 轮询读的详情带 submit（默认 getHubTask 就透出，判据不是猜的）",
    apiSrc.includes("deps.fetchTask || ((id: string) => getHubTask(id))") && /fetchTask\?: \(localId: string\) => Promise<\{ state\?: string; submit\?: HubSubmitView \| null \}>/.test(apiSrc));

for (const [name, src] of [["列表行内", indexSrc], ["抽屉", drawerSrc]]) {
    const handlers = src.split("onPhase: (").slice(1);
    check(`[E] ${name}: onPhase 用同一份 hubQuickPhaseText（阶段人话单源）`, src.includes("hubQuickPhaseText(phase"));
    check(`[E] ${name}: 后端结论直接成为 toast（note || line.text）`, handlers.every((chunk) => chunk.slice(0, 800).includes("note || line.text")) && handlers.length > 0,
        `${handlers.length} 处`);
    check(`[E] ${name}: 没有第二套形态判定（零 form === 字面量比较）`, !/form === ["'](unknown|rejected|failed|inflight)["']/.test(src));
    check(`[E] ${name}: 没有另写「仍在处理中」这类劝退话`, !src.includes("仍在处理中"));
}
check("[E] 抽屉三处入口（顶部/底部/未知态续查）都走 runHubQuickAction",
    (drawerSrc.match(/runHubQuickAction\(/g) || []).length >= 2 && indexSrc.includes("runHubQuickAction(task.local_id, action"));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b10] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
