/**
 * AT-16 B5a-2 前端探针（本地免费、不联网、不动真实任务）：**共享重提 / 续查快捷入口**。
 *
 * 缺陷 D6 动作面（真机实证）：重提 / 续查入口埋在抽屉 4-5 屏深，列表上看不到、点不到。
 * 本探针锁三件事（后端一行不改，提交链仍是既有的 POST /api/hub/submit）：
 *
 *  [A] 准入判定 hubQuickAction（纯函数，两个入口共用）：
 *      · 凭据按**批准渠道**取 key：批准快照 channelId 与当前草稿渠道不同 → 用批准那条（不是草稿、不是全局默认）
 *      · 缺 key → enabled=false + needsConfig=true + 文案复用 genSubmitIssues（「设置 → 渠道」），**不空发**
 *      · 批准渠道已从画布配置消失 → 明确引导，**禁止**拿草稿/默认渠道顶上
 *      · active_submit（提交在跑）→ 重提与续查都禁用
 *      · blocks_retry（后端未知收单/在途）→ 重提禁用；有 taskId 时续查可用（只查不重提）
 *      · 画幅渠道不接（sizeBlocked，与抽屉同源 genResolveSize）→ 重提禁用
 *      · wan 允许空 key；sdgf 必须 key
 *      · 没有批准基线也没有草稿 → 无源可用（source=none，不从全局默认挑）
 *
 *  [B] 执行 runHubQuickAction：
 *      · POST 返回 → 只报 accepted（「提交处理中」），**不**说云端已收单
 *      · 轮询看到 generating / done 才报 confirmed（「云端已收单」）；failed 也如实回执失败
 *      · 轮询一直没转 generating → timeout（说「仍在处理中」，不是成功）
 *      · 同一任务重复点击 → rejected 且**不发第二个请求**
 *      · 续查也走同一个 submit 端点（不新造提交链）；提交失败（409）原样抛出
 *
 *  [C] 静态接线：
 *      · 列表行内（index.tsx）+ 抽屉顶部（task-detail-drawer.tsx）+ 抽屉底部 submitOnly 都调
 *        hubQuickAction / runHubQuickAction（同承接同凭据同守卫，没有第二份实现）
 *      · 没有为入口另造端点（仍是 /api/hub/submit）；行内不直接裸调 submitHubTask
 *      · active / blocksRetry / sizeBlocked 三道闸门都还在
 *
 * 跑法：cd web && node scripts/probe-at16-b5a2.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b5a2");
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

// ════════════════════════════════════════════════════════════ 夹具
/** 画布渠道（结构同 AiConfig.channels；apiKey 是提交时一次性透传的那份）。 */
const ch = (id, name, apiFormat, apiKey, baseUrl = `https://${id}.example.com`) => ({ id, name, baseUrl, apiFormat, apiKey });
const SDGF = ch("ch-sdgf", "SDGF 主渠道", "sdgf", "sk-sdgf-123");
const SDGF_NO_KEY = ch("ch-nokey", "SDGF 无 key", "sdgf", "");
const WAN = ch("ch-wan", "万相渠道", "wan", "");
const OTHER = ch("ch-other", "另一个 SDGF", "sdgf", "sk-other-999");
const CHANNELS = [SDGF, SDGF_NO_KEY, WAN, OTHER];

/** 批准时点签名（= gen-settings.genDraftSignature 的 JSON 形状）。 */
const signature = (channelId, extra = {}) =>
    JSON.stringify({
        channelId,
        baseUrl: `https://${channelId}.example.com`,
        apiFormat: "sdgf",
        "渠道名": CHANNELS.find((row) => row.id === channelId)?.name || "",
        model: "sdgf-video-1",
        mode: "frames",
        params: { duration: 6, size: "16:9", vquality: "720", audio: true },
        ...extra,
    });

/** 任务草稿（readGenDraft 的形状）。 */
const draftOf = (channelId, extra = {}) => ({
    channelId,
    model: "sdgf-video-1",
    mode: "frames",
    size: "16:9",
    rawSize: "",
    vquality: "720",
    duration: 6,
    audio: true,
    ...extra,
});

/** 后端 submit_view 的形态（B5a-1 合同）。 */
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

const base = (extra = {}) => ({
    localId: "auto-t1",
    state: "approved",
    submit: V("none"),
    channels: CHANNELS,
    approvedSignature: signature("ch-sdgf"),
    draft: draftOf("ch-sdgf"),
    ...extra,
});

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "entry.ts");
writeFileSync(
    entry,
    'export { hubQuickAction, hubQuickChannels, hubQuickPhaseText, runHubQuickAction, hubQuickRunning, submitHubTask } from "../../../src/pages/hub/hub-api";\n' +
        'export { hubRowQuickAction } from "../../../src/pages/hub/index";\n',
);
const aliasPlugin = {
    name: "at16b5a2-alias",
    setup(build) {
        build.onResolve({ filter: /^@\// }, (args) => {
            const basePath = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [basePath, `${basePath}.ts`, `${basePath}.tsx`, path.join(basePath, "index.ts"), path.join(basePath, "index.tsx")]) {
                try {
                    if (statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 试下一个后缀 */
                }
            }
            return { path: basePath };
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
check("[A] 真代码打包成功（hub-api / gen-settings / index.tsx 真模块）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

// 纯 node 里跑真前端模块：补最小浏览器全局（i18n / store 在 import 期读 localStorage）
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
const { hubQuickAction, hubQuickChannels, hubQuickPhaseText, runHubQuickAction, hubQuickRunning, hubRowQuickAction } = api;

// ════════════════════════════════════════════════════════════ [A] 准入判定 + 凭据
console.log("1) hubQuickAction：按批准渠道取 key / 缺 key 引导 / 三道守卫");

const okSubmit = hubQuickAction(base());
check("[A] approved + 批准渠道有 key → 可点，key 取批准渠道的", okSubmit.kind === "submit" && okSubmit.enabled === true && okSubmit.key === "sk-sdgf-123", JSON.stringify(okSubmit).slice(0, 240));
check("[A] 凭据来源标 approved（批准快照优先）", okSubmit.source === "approved" && okSubmit.channelId === "ch-sdgf", `${okSubmit.source}/${okSubmit.channelId}`);
check("[A] 可点时有渠道说明（人知道用的是哪条渠道）", okSubmit.hint.includes("SDGF 主渠道"), okSubmit.hint);

// 批准渠道 ≠ 当前草稿渠道：必须用批准那条（按批准渠道匹配，不从草稿/全局默认选）
const drift = hubQuickAction(base({ draft: draftOf("ch-other") }));
check("[A] 草稿渠道被改成别的 → 仍用批准渠道的 key（不跟草稿跑）", drift.channelId === "ch-sdgf" && drift.key === "sk-sdgf-123", `${drift.channelId}/${drift.source}`);

// 批准渠道 id 变了但身份没变（配置重导入）→ 按 identity 三元组仍能匹配上
const reId = hubQuickAction(
    base({
        approvedSignature: signature("ch-sdgf", { channelId: "ch-sdgf-gone" }),
        draft: draftOf("ch-other"),
    }),
);
check("[A] 批准渠道 id 失配 → 按身份三元组仍匹配到同一条渠道", reId.enabled === true && reId.channelId === "ch-sdgf", `${reId.channelId}/${reId.reason}`);

// 批准渠道彻底不在配置里 → 明确引导，不许拿草稿渠道顶上
const gone = hubQuickAction(
    base({
        approvedSignature: JSON.stringify({ channelId: "ch-deleted", baseUrl: "https://gone.example.com", apiFormat: "sdgf", "渠道名": "已删渠道", model: "m", mode: "frames", params: { duration: 6, size: "16:9", vquality: "720", audio: true } }),
        draft: draftOf("ch-other"),
    }),
);
check("[A] 批准渠道已不在配置 → 禁用 + 指向「设置 → 渠道」（不换渠道顶上）",
    gone.enabled === false && gone.needsConfig === true && gone.reason.includes("设置 → 渠道") && gone.channelId === "", gone.reason);

// 缺 key：sdgf 必须 key → 禁用 + 文案是生产者原文（设置 → 渠道）
const noKey = hubQuickAction(base({ approvedSignature: signature("ch-nokey"), draft: draftOf("ch-nokey") }));
check("[A] 缺 key → 禁用 + needsConfig + 引导到「设置 → 渠道」",
    noKey.enabled === false && noKey.needsConfig === true && noKey.reason.includes("API Key") && noKey.reason.includes("设置 → 渠道"), noKey.reason);
check("[A] 缺 key 时 key 为空串（不拿别处 key 顶上 → 不会空发）", noKey.key === "");

// wan 允许空 key
const wan = hubQuickAction(
    base({
        approvedSignature: JSON.stringify({ channelId: "ch-wan", baseUrl: "https://ch-wan.example.com", apiFormat: "wan", "渠道名": "万相渠道", model: "", mode: "", params: { duration: 6, size: "16:9", vquality: "720", audio: true } }),
        draft: null,
    }),
);
check("[A] wan 渠道允许空 key → 可点（不假报缺 key）", wan.enabled === true && wan.key === "" && wan.channelName === "万相渠道", JSON.stringify(wan).slice(0, 200));

// 没有任何凭据来源 → 不从全局默认挑
const noSource = hubQuickAction(base({ approvedSignature: "", draft: null }));
check("[A] 无批准快照 + 无落盘草稿 → 禁用 + 说清「本机没有批准配置」（不从全局默认渠道挑）",
    noSource.enabled === false && noSource.source === "none" && noSource.key === ""
    && noSource.reason.includes("本机没有这条任务的批准配置") && noSource.needsConfig === false, noSource.reason);

// active：提交在跑 → 重提与续查都禁
const activeSubmit = hubQuickAction(base({ submit: V("inflight", { active_submit: true, can_resume: true, cloud_task_id: "task-x" }) }));
check("[A] active_submit → 重提禁用（别重复点）", activeSubmit.enabled === false && activeSubmit.reason.includes("已有提交在跑"), activeSubmit.reason);
const activeResume = hubQuickAction(base({ state: "generating", submit: V("inflight", { active_submit: true, can_resume: true, cloud_task_id: "task-x" }) }));
check("[A] active_submit → 续查也禁用（等回执，别叠请求）", activeResume.kind === "resume" && activeResume.enabled === false, activeResume.reason);

// blocks_retry（未知收单）→ 重提禁用，文案带后端结论
const blocked = hubQuickAction(base({ submit: V("unknown", { conclusion: "结果未知：可能已收单，先核对", blocks_retry: true, label: "结果未知·先核对", cloud_task_id: "task-abc", can_resume: true }) }));
check("[A] blocks_retry=true → 重提禁用 + 文案来自后端结论", blocked.enabled === false && blocked.reason.includes("结果未知") && blocked.reason.includes("解除阻断后"), blocked.reason);

// 在途 + 有 taskId → 续查可用（只查不重提）
const resume = hubQuickAction(base({ state: "generating", submit: V("inflight", { blocks_retry: true, can_resume: true, cloud_task_id: "task-abc", conclusion: "云端处理中：按 taskId 续查" }) }));
check("[A] 在途 + 有云端 taskId → 续查可点，label=续查", resume.kind === "resume" && resume.enabled === true && resume.label === "续查" && resume.cloudTaskId === "task-abc", JSON.stringify(resume).slice(0, 200));
check("[A] 续查不受 blocks_retry 拦（只查不重提，后端 generating 分支不创建）", resume.enabled === true && resume.reason === "");

// 在途但没 taskId → 不给续查（不假装能查）
const noId = hubQuickAction(base({ state: "generating", submit: V("inflight", { blocks_retry: true, can_resume: true, cloud_task_id: "" }) }));
check("[A] 没有云端 taskId → 不摆续查入口（kind=none，不假装能查）", noId.kind === "none");

// sizeBlocked：画幅渠道不接 → 重提禁用（与抽屉同源 genResolveSize）
const bigSize = hubQuickAction(base({ approvedSignature: signature("ch-sdgf", { params: { duration: 6, size: "21:9", vquality: "720", audio: true } }), draft: null }));
check("[A] 画幅 21:9 在 sdgf 不接 → sizeBlocked=true 且禁用", bigSize.sizeBlocked === true && bigSize.enabled === false && bigSize.reason.includes("21:9"), bigSize.reason);
const wanSize = hubQuickAction(
    base({
        approvedSignature: JSON.stringify({ channelId: "ch-wan", baseUrl: "https://ch-wan.example.com", apiFormat: "wan", "渠道名": "万相渠道", model: "", mode: "", params: { duration: 6, size: "4:3", vquality: "720", audio: true } }),
        draft: null,
    }),
);
check("[A] 画幅 4:3 在 wan 不接 → 同样拦住", wanSize.enabled === false && wanSize.sizeBlocked === true, wanSize.reason);

// busy（本 UI 上一次点击未结束）
const busy = hubQuickAction(base({ busy: true }));
check("[A] busy=true → 禁用 + 「别重复点」", busy.enabled === false && busy.reason.includes("别重复点"), busy.reason);

// 其它状态：不摆无效入口
check("[A] discussing 等无动作状态 → kind=none（不摆无效按钮）", hubQuickAction(base({ state: "discussing" })).kind === "none");
check("[A] awaiting_approval（要重新批准，不能直接提交）→ kind=none", hubQuickAction(base({ state: "awaiting_approval" })).kind === "none");

// 渠道表归一（画布配置缺字段不猜）
const shaped = hubQuickChannels({ channels: [{ id: "a", name: "A", baseUrl: " https://a ", apiFormat: "sdgf", apiKey: " k " }, { name: "无名无 id" }, null] });
check("[A] hubQuickChannels 归一：只留带 id 的行，字段 trim", shaped.length === 1 && shaped[0].id === "a" && shaped[0].baseUrl === "https://a" && shaped[0].apiKey === "k", JSON.stringify(shaped));

// 行内入口与抽屉入口**同一份判定**（同一输入 → 同一结果，逐字段相等）
const rowAction = hubRowQuickAction(
    { local_id: "auto-t1", state: "approved", submit: V("none"), task_name: "t", intent: "", created_at: "", video_name: "", gen_progress: null, has_output: false },
    CHANNELS,
);
check("[A] 行内入口走同一份 hubQuickAction（结果逐字段一致）",
    JSON.stringify(rowAction) === JSON.stringify(hubQuickAction({ localId: "auto-t1", state: "approved", submit: V("none"), channels: CHANNELS, approvedSignature: "", draft: null })),
    JSON.stringify(rowAction).slice(0, 200));

// ════════════════════════════════════════════════════════════ [B] 执行节奏
console.log("2) runHubQuickAction：受理只报处理中 / 轮询确认才报收单 / 重复点击被拒");

function deps(script, calls) {
    let i = 0;
    return {
        submit: async (id, key) => {
            calls.push({ kind: "submit", id, key });
        },
        fetchTask: async () => {
            const state = script[Math.min(i, script.length - 1)];
            i += 1;
            calls.push({ kind: "fetch", state });
            return { state };
        },
        sleep: async () => undefined,
        intervalMs: 1,
        maxPolls: 6,
    };
}

const okAction = hubQuickAction(base());
const phases = [];
const calls1 = [];
const res1 = await runHubQuickAction("auto-t1", okAction, { ...deps(["approved", "approved", "generating"], calls1), onPhase: (phase) => phases.push(phase) });
check("[B] 受理后先报 accepted（提交处理中），不报成功", res1.phase === "confirmed" && phases[0] === "accepted", JSON.stringify(phases));
check("[B] 轮询转 generating → confirmed（云端已收单）", phases[1] === "confirmed" && res1.state === "generating", JSON.stringify(phases));
check("[B] 只发一次提交请求（POST 一次，不是每次轮询都发）", calls1.filter((call) => call.kind === "submit").length === 1, JSON.stringify(calls1));
check("[B] 提交带的是批准渠道的 key（一次性透传）", calls1[0].key === "sk-sdgf-123", calls1[0].key);
check("[B] accepted 文案=「提交处理中」，confirmed 文案=「云端已收单」，两句话不同",
    hubQuickPhaseText("accepted").text.includes("提交处理中") && hubQuickPhaseText("confirmed", "generating").text.includes("云端已收单")
    && hubQuickPhaseText("accepted").text !== hubQuickPhaseText("confirmed").text);
check("[B] accepted 是 info（不是 success——HTTP 200 不等于云端收单）", hubQuickPhaseText("accepted").level === "info" && hubQuickPhaseText("confirmed", "generating").level === "success");

const calls2 = [];
const phases2 = [];
const res2 = await runHubQuickAction("auto-t2", hubQuickAction(base({ localId: "auto-t2" })), { ...deps(["approved"], calls2), onPhase: (phase) => phases2.push(phase) });
check("[B] 一直没转 generating → timeout，不谎报已收单", res2.phase === "timeout" && phases2.join(",") === "accepted,timeout", JSON.stringify(phases2));
check("[B] timeout 文案说「仍在处理中」并给续查指引", hubQuickPhaseText("timeout").text.includes("仍在处理中") && hubQuickPhaseText("timeout").text.includes("续查"));

const calls3 = [];
const phases3 = [];
const res3 = await runHubQuickAction("auto-t3", hubQuickAction(base({ localId: "auto-t3" })), { ...deps(["approved", "done"], calls3), onPhase: (phase, state) => phases3.push(`${phase}:${state}`) });
check("[B] done 也算确认收单（已出片）", res3.phase === "confirmed" && phases3.includes("confirmed:done"), JSON.stringify(phases3));

// 重复点击：第一次还在跑，第二次直接拒，不发第二个请求
const calls4 = [];
const gate = deps(["approved", "approved", "approved"], calls4);
let release;
const hold = new Promise((resolve) => {
    release = resolve;
});
const gated = { ...gate, submit: async (id, key) => { calls4.push({ kind: "submit", id, key }); await hold; } };
const first = runHubQuickAction("auto-t4", hubQuickAction(base({ localId: "auto-t4" })), { ...gated, onPhase: () => undefined });
// 让第一次跑进 POST 里（在跑中标记已置位）
while (!hubQuickRunning("auto-t4")) await new Promise((resolve) => setTimeout(resolve, 1));
const second = await runHubQuickAction("auto-t4", hubQuickAction({ ...base({ localId: "auto-t4" }), busy: false }), { ...gated, onPhase: () => undefined });
release();
await first;
check("[B] 同任务重复点击 → rejected（提示别重复点）", second.phase === "rejected" && second.note.includes("别重复点"), second.note);
check("[B] 重复点击不发第二个提交请求（只有 1 次 POST）", calls4.filter((call) => call.kind === "submit").length === 1, JSON.stringify(calls4));
check("[B] 拒绝后释放：第二次跑完之后 hubQuickRunning=false（可以再点）", hubQuickRunning("auto-t4") === false);

// 续查动作：走的也是既有 submit 端点（只查不重提由后端 generating 分支保证）
const calls5 = [];
const resumeAction = hubQuickAction({ ...base({ localId: "auto-t5", state: "generating", submit: V("inflight", { can_resume: true, cloud_task_id: "task-abc", blocks_retry: true }) }) });
const res5 = await runHubQuickAction("auto-t5", resumeAction, deps(["generating"], calls5));
check("[B] 续查走同一个 submit 调用点（不新造提交链）", calls5.filter((call) => call.kind === "submit").length === 1 && res5.phase === "confirmed", JSON.stringify(calls5));

// 提交失败（后端 409）原样抛出
let thrown = "";
try {
    await runHubQuickAction("auto-t6", hubQuickAction(base({ localId: "auto-t6" })), {
        submit: async () => {
            throw new Error("这个任务已有提交在跑（排队/执行中）——别重复点，等它出结果");
        },
        fetchTask: async () => ({ state: "approved" }),
        sleep: async () => undefined,
    });
} catch (error) {
    thrown = String(error.message || error);
}
check("[B] 后端拒绝（409 原文）原样抛出，不吞不美化", thrown.includes("已有提交在跑"), thrown);
check("[B] 抛错后不残留「在跑」标记（还能再试）", hubQuickRunning("auto-t6") === false);

// 不可点的入口：连 POST 都不发
const calls7 = [];
const res7 = await runHubQuickAction("auto-t7", hubQuickAction(base({ localId: "auto-t7", submit: V("unknown", { blocks_retry: true, conclusion: "结果未知：可能已收单，先核对" }) })), deps(["approved"], calls7));
check("[B] 被阻断的入口点按 → rejected 且零请求（不空发）", res7.phase === "rejected" && calls7.length === 0, JSON.stringify(calls7));

// 静态守卫：默认 deps 用的就是既有 submitHubTask（POST /api/hub/submit）
const apiSrc = read("src/pages/hub/hub-api.ts");
check("[B] 默认执行器=既有 submitHubTask（同一提交链，未造新端点）",
    apiSrc.includes("deps.submit || ((id: string, key: string) => submitHubTask(id, key))")
    && apiSrc.includes('`/api/hub/submit/${encodeURIComponent(localId)}`'));

// ════════════════════════════════════════════════════════════ [C] 静态接线
console.log("3) 静态接线：两个入口 + 底部按钮同一份实现 / 无新端点 / 闸门全留");

const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");

check("[C] 列表行内：渲染快捷入口按钮（带 data-hub-quick-action 钩子）",
    indexSrc.includes("data-hub-quick-action={quick.kind}") && indexSrc.includes("hubRowQuickAction(task, channels"));
check("[C] 列表行内：点按走 runHubQuickAction + 阶段人话", indexSrc.includes("runHubQuickAction(task.local_id, action") && indexSrc.includes("hubQuickPhaseText(phase, state)"));
check("[C] 抽屉顶部：渲染同一入口（data-hub-drawer-quick-action）", drawerSrc.includes("data-hub-drawer-quick-action={quick.kind}") && drawerSrc.includes("void fireQuick(quick)"));
check("[C] 抽屉底部 submitOnly：也走 hubQuickAction + runHubQuickAction（同承接）",
    drawerSrc.includes("const submitAction = hubQuickAction({") && drawerSrc.includes("runHubQuickAction(localId, submitAction"));
check("[C] 新入口不自己拼 key/端点（行内零裸调；抽屉仅剩「批准后立刻提交」与既有孤儿续查两处旧链）",
    !indexSrc.includes("submitHubTask")
    && (drawerSrc.match(/await submitHubTask\(/g) || []).length === 2
    && drawerSrc.includes("await submitHubTask(localId, apiKey)")        // approveAndSubmit（既有）
    && drawerSrc.includes("await submitHubTask(detail.local_id, key)")  // GenIssueSection 既有孤儿续查（本卡未动）
    && !drawerSrc.includes("genSubmitIssues(draft, channel, apiKey)"),  // 旧的 submitOnly 手拼校验已收口到 hubQuickAction
    `index=${indexSrc.includes("submitHubTask")} drawer裸调次数=${(drawerSrc.match(/await submitHubTask\(/g) || []).length}`);
check("[C] 凭据按批准渠道匹配（两处入口都读 readApprovedSignature）",
    indexSrc.includes("readApprovedSignature(task.local_id)") && drawerSrc.includes("readApprovedSignature(localId)"));
check("[C] 凭据不吃面板里从全局默认派生的初始草稿（底部按钮也读落盘草稿）",
    drawerSrc.includes("draft: readGenDraft(localId),") && drawerSrc.includes("面板里那份从全局默认派生的初始草稿"));
check("[C] 不从全局默认渠道挑（入口链路无 genDefaultChannelId 调用）",
    !/genDefaultChannelId\(/.test(indexSrc) && !/genDefaultChannelId\(/.test(apiSrc));
check("[C] 闸门全留：active / blocks_retry / sizeBlocked 都在准入判定里",
    apiSrc.includes("const active = Boolean(submit?.active_submit)") && apiSrc.includes("submit?.blocks_retry") && apiSrc.includes("const sizeBlocked = Boolean(sizeInfo"));
check("[C] 底部按钮仍保留 needsReapproval / blocksRetry / sizeBlocked 三道禁用条件",
    drawerSrc.includes("disabled={busy !== null || needsReapproval || blocked.blocksRetry || sizeBlocked || !submitAction.enabled}"));
check("[C] 没有另造端点（无 quick-submit / submit-direct 之类路由）",
    !apiSrc.includes("quick-submit") && !apiSrc.includes("submit-direct") && !apiSrc.includes("/api/hub/submit-quick"));
check("[C] 续查不新造链（resume 与 submit 同一个执行函数、无 retry 端点）",
    !apiSrc.includes("retryHubTask(id)") && apiSrc.includes('if (kind === "resume")'));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b5a2] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
