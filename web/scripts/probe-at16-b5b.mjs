/**
 * AT-16 B5b 前端探针（本地免费、不联网、不动真实任务）：**换模型重批路径可见可用**。
 *
 * 缺陷 D6 后半（真机实证）：批准后想换模型 / 改配置的用户看不到「撤销批准 → 改模型 → 批准」
 * 这条路，以为自己的资产确认 / 置换 / 提示词心血作废；而未知收单态又有人会被引导去撤销
 * （撤销会连「未知收单」标记一起清掉 → 可能重复付费提交）。
 *
 * 本探针锁四件事（后端一行不改，端点全是既有的 revoke / release / approve / submit）：
 *
 *  [A] 起态门 hubReworkGate（纯函数，抽屉撤销按钮与指引段只读它）
 *      · 允许态 = approved 且**无活跃提交、无 submit_unknown、dispatch 已完结** → 三步顺序
 *        （①撤销 → ②换模型 → ③批准出片）+ 心血不作废口径
 *      · 未知态（submit_unknown / dispatch=prepared|dispatched）→ 不引导撤销：
 *        allowed=false + showRelease=true（走既有 release）+ 有 taskId 才给续查
 *      · 提交在跑（active_submit）→ 连解除都不给（等结果）
 *      · 阻断顺序与后端 hub_api._submit_block_kind 一致：active → dispatch → unknown
 *      · 非 approved（待批准 / 生成中）→ none：待批准态配置本来就能直接改
 *
 *  [B] 无本地草稿边界 readApprovedDraft + 抽屉接线
 *      · 草稿丢了 → 展示「已批准配置基线」，不拿画布全局默认冒充任务配置
 *      · 连批准基线也没有 → 明确说这是占位（data-hub-gen-draft-boundary=none），不假装读到快照
 *
 *  [C] 静态接线：撤销按钮被起态门管住 / 解除阻断走既有 release（confirm:true）/ 无新端点 /
 *      非 awaiting_approval 态配置仍只读
 *
 * 跑法：cd web && node scripts/probe-at16-b5b.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b5b");
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

/** 提交诊断形状（B5a-1 的 HubSubmitView；后端 submit_view.build 同源）。 */
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

const gateOf = (extra = {}) => ({ state: "approved", submit: V("none"), cloudTaskId: "", ...extra });

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "entry.ts");
writeFileSync(
    entry,
    'export { hubReworkGate, hubReworkUnknownSteps, readApprovedDraft, releaseHubTask, revokeHubTask, approveHubTask, submitHubTask } from "../../../src/pages/hub/hub-api";\n',
);
const aliasPlugin = {
    name: "at16b5b-alias",
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
check("[0] 真代码打包成功（hub-api / gen-settings 真模块）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

// 纯 node 里跑真前端模块：补最小浏览器全局（store 在 import 期读 localStorage）
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
const { hubReworkGate, hubReworkUnknownSteps, readApprovedDraft, releaseHubTask } = api;

// ════════════════════════════════════════════════════════════ [A] 起态门
console.log("1) hubReworkGate：允许态才引导撤销，未知态只给核对 / 解除 / 续查");

const allowed = hubReworkGate(gateOf());
check("[A] approved 且无任何阻断 → 允许态（可撤销重批）", allowed.mode === "allowed" && allowed.allowed === true && allowed.blockKind === "", JSON.stringify(allowed).slice(0, 200));
check("[A] 允许态给三步顺序：①撤销 → ②换模型 → ③批准出片", allowed.steps.length === 3
    && allowed.steps[0].includes("撤销批准") && allowed.steps[1].includes("模型") && allowed.steps[2].includes("批准出片"),
    JSON.stringify(allowed.steps));
check("[A] 允许态不给解除阻断（没有未知标记要解）", allowed.showRelease === false && allowed.canResume === false);
check("[A] 允许态带「心血不作废」口径（资产确认 / 上传流水 / 置换确认 / 提示词 / 行决策）",
    ["资产确认", "上传流水", "置换确认", "提示词", "行决策"].every((word) => allowed.keep.includes(word)), allowed.keep);
check("[A] 允许态标题直接点明「资产与提示词不会作废」", allowed.title.includes("不会作废"), allowed.title);

// 未知收单：不引导撤销，给既有 release + 续查
const unknown = hubReworkGate(gateOf({ submit: V("unknown", { submit_unknown: true, blocks_retry: true, conclusion: "结果未知：可能已收单，先核对", cloud_task_id: "task-abc", can_resume: true }) }));
check("[A] submit_unknown → 未知态且 allowed=false（不许引导撤销）", unknown.mode === "unknown" && unknown.allowed === false && unknown.blockKind === "unknown", JSON.stringify(unknown).slice(0, 240));
check("[A] 未知态给人工解除（走既有 release 端点）", unknown.showRelease === true);
check("[A] 未知态有云端 taskId → 给续查（只查不重提）", unknown.canResume === true && unknown.cloudTaskId === "task-abc", unknown.cloudTaskId);
check("[A] 未知态理由说清「撤销会清掉未知标记、可能重复扣费」",
    unknown.reason.includes("未知收单") && unknown.reason.includes("重复扣费") && unknown.reason.includes("核对"), unknown.reason);
check("[A] 未知态理由带上后端结论原文（不自己另写一套判断）", unknown.reason.includes("结果未知：可能已收单，先核对"), unknown.reason);
check("[A] 未知态标题明说「别撤销批准」", unknown.title.includes("别撤销批准"), unknown.title);
check("[A] 未知态没有「撤销 → 改模型 → 批准」三步（那是允许态的话术）", unknown.steps.length === 0 && unknown.keep === "");
const unknownSteps = hubReworkUnknownSteps(unknown);
check("[A] 未知态三步：核对 → 解除阻断 → 再谈配置", unknownSteps.length === 3
    && unknownSteps[0].includes("核对") && unknownSteps[1].includes("续查") && unknownSteps[2].includes("解除阻断"),
    JSON.stringify(unknownSteps));
check("[A] 未知态三步不提「先撤销批准」（撤销在这一态是被禁的路）", !unknownSteps.join("").includes("撤销"), unknownSteps.join(" | "));

// 没有 taskId 的未知态：不假装能续查
const unknownNoId = hubReworkGate(gateOf({ submit: V("unknown", { submit_unknown: true, blocks_retry: true }) }));
check("[A] 未知态没有云端 taskId → 不给续查（不假装能查）", unknownNoId.showRelease === true && unknownNoId.canResume === false);
check("[A] 没有 taskId 时三步里的续查步改成「先拿到 taskId」", hubReworkUnknownSteps(unknownNoId)[1].includes("先拿到云端 taskId"));

// dispatch 未完结：同样算未知态
for (const phase of ["prepared", "dispatched"]) {
    const g = hubReworkGate(gateOf({ submit: V("unknown", { dispatch_phase: phase, blocks_retry: true, cloud_task_id: "task-d" }) }));
    check(`[A] dispatch=${phase} → 未知态（blockKind=dispatch，不引导撤销）`,
        g.mode === "unknown" && g.allowed === false && g.blockKind === "dispatch" && g.showRelease === true,
        JSON.stringify(g).slice(0, 200));
    check(`[A] dispatch=${phase} 标题带上阶段（排障能对上后端 dispatch）`, g.title.includes(`dispatch=${phase}`), g.title);
}

// 提交在跑：连解除都不给
const active = hubReworkGate(gateOf({ submit: V("inflight", { active_submit: true, blocks_retry: true, cloud_task_id: "task-x" }) }));
check("[A] active_submit → blocked（allowed=false）", active.mode === "blocked" && active.allowed === false && active.blockKind === "active", JSON.stringify(active).slice(0, 200));
check("[A] 提交在跑时不给解除阻断（没有未知标记要解，等结果）", active.showRelease === false && active.canResume === false);
check("[A] 提交在跑的理由是「等结果」", active.reason.includes("等它出结果"), active.reason);

// 阻断顺序与后端 _submit_block_kind 一致：active → dispatch → unknown
const both = hubReworkGate(gateOf({ submit: V("unknown", { active_submit: true, submit_unknown: true, dispatch_phase: "dispatched" }) }));
check("[A] 三种阻断同时命中 → 先报 active（与后端 active→dispatch→unknown 同序）", both.blockKind === "active" && both.mode === "blocked", both.blockKind);
const dispatchOverUnknown = hubReworkGate(gateOf({ submit: V("unknown", { submit_unknown: true, dispatch_phase: "dispatched" }) }));
check("[A] dispatch + unknown 同时命中 → 报 dispatch（同序）", dispatchOverUnknown.blockKind === "dispatch", dispatchOverUnknown.blockKind);

// 旧 bridge 没有 submit 透出：用兜底事实判定（向后兼容）
const legacyUnknown = hubReworkGate({ state: "approved", submit: null, submitUnknown: true, cloudTaskId: "task-old" });
check("[A] 旧 bridge（无 submit）→ 用兜底事实仍判出未知态", legacyUnknown.mode === "unknown" && legacyUnknown.showRelease === true && legacyUnknown.canResume === true);
const legacyActive = hubReworkGate({ state: "approved", submit: null, activeSubmit: true });
check("[A] 旧 bridge（无 submit）→ 活跃提交仍判 blocked", legacyActive.mode === "blocked" && legacyActive.blockKind === "active");
const legacyDispatch = hubReworkGate({ state: "approved", submit: null, dispatchPhase: "prepared" });
check("[A] 旧 bridge（无 submit）→ dispatch=prepared 仍判未知态", legacyDispatch.mode === "unknown" && legacyDispatch.blockKind === "dispatch");
const legacyClean = hubReworkGate({ state: "approved", submit: null });
check("[A] 旧 bridge（无 submit）且无阻断 → 允许态（不误伤正常任务）", legacyClean.mode === "allowed" && legacyClean.allowed === true);

// 非 approved：没有「撤销重批」这条路
for (const st of ["awaiting_approval", "generating", "done", "failed", "rendering", "ingested"]) {
    const g = hubReworkGate({ state: st, submit: V("none") });
    check(`[A] ${st} → none（撤销重批入口不出现）`, g.mode === "none" && g.allowed === false && g.steps.length === 0, `${g.mode}/${g.blockKind}`);
}
check("[A] awaiting_approval 态：配置本来就能直接改，不需要撤销", hubReworkGate({ state: "awaiting_approval" }).allowed === false);

// 纯函数：不读网络 / 不读 localStorage（判定只吃入参）
const gateSrc = read("src/pages/hub/hub-api.ts");
const gateBlock = gateSrc.slice(gateSrc.indexOf("export function hubReworkGate("), gateSrc.indexOf("export function hubReworkUnknownSteps("));
check("[A] hubReworkGate 是纯函数（函数体内无 fetch / localStorage）", !gateBlock.includes("fetch(") && !gateBlock.includes("localStorage"), gateBlock.slice(0, 120));

// ════════════════════════════════════════════════════════════ [B] 无本地草稿边界
console.log("2) readApprovedDraft：草稿丢了显示「已批准配置」，别拿画布默认冒充");

const signature = (model) => JSON.stringify({
    channelId: "ch-sdgf",
    baseUrl: "https://ch-sdgf.example.com",
    apiFormat: "sdgf",
    "渠道名": "SDGF 主渠道",
    model,
    mode: "frames",
    params: { duration: 8, size: "16:9", vquality: "720", audio: true },
});
globalThis.localStorage.setItem("hub-gen-approved:auto-t1", signature("sdgf-video-1"));
const baseline = readApprovedDraft("auto-t1");
check("[B] 有批准基线 → 读回「已批准配置」（渠道 / 模型 / 参数）", baseline !== null
    && baseline.channelId === "ch-sdgf" && baseline.model === "sdgf-video-1" && baseline.draft.duration === 8,
    JSON.stringify(baseline));
check("[B] 基线里的渠道名也读得出来（id 变了也能对上渠道）", baseline.channelName === "SDGF 主渠道");
check("[B] 本机没有基线 → null（调用方必须明说，不许假装读到快照）", readApprovedDraft("auto-no-baseline") === null);
globalThis.localStorage.setItem("hub-gen-approved:auto-broken", "{不是 JSON");
check("[B] 畸形基线 → null（不拿坏数据猜配置）", readApprovedDraft("auto-broken") === null);

// ════════════════════════════════════════════════════════════ [C] 静态接线
console.log("3) 静态接线：撤销按钮受起态门管 / 解除走既有 release / 无新端点 / 只读态不变");

const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
const apiSrc = read("src/pages/hub/hub-api.ts");
const indexSrc = read("src/pages/hub/index.tsx");

check("[C] 抽屉用起态门判定撤销（hubReworkGate 单点）", drawerSrc.includes("const reworkGate: HubReworkGate = hubReworkGate({")
    && drawerSrc.includes("const revoke = () => {") && drawerSrc.includes("if (!reworkGate.allowed) {"));
check("[C] 撤销按钮只在允许态渲染（未知态不给撤销入口）",
    drawerSrc.includes("{reworkGate.allowed ? (") && drawerSrc.includes('data-hub-gen-revoke data-hub-gen-revoke-allowed="1"'));
check("[C] 指引段渲染形态/允许否/阻断种类钩子（真机可断言）",
    drawerSrc.includes("data-hub-gen-rework={reworkGate.mode}") && drawerSrc.includes('data-hub-gen-rework-allowed={reworkGate.allowed ? "1" : "0"}')
    && drawerSrc.includes("data-hub-gen-rework-block={reworkGate.blockKind}"));
check("[C] 允许态在界面上写出「心血不作废」承诺段", drawerSrc.includes("data-hub-gen-rework-keep") && drawerSrc.includes("{reworkGate.keep}"));
check("[C] 未知态给「已核对，解除阻断」按钮（走 releaseUnknown）",
    drawerSrc.includes("data-hub-gen-release") && drawerSrc.includes("onClick={releaseUnknown}"));
check("[C] 解除阻断调既有 releaseHubTask（不自己清标记）", drawerSrc.includes("await releaseHubTask(localId);"));
check("[C] release 客户端固定带 confirm:true（后端人工动作合同）",
    apiSrc.includes("confirm: true") && apiSrc.includes("`/api/hub/task/${encodeURIComponent(localId)}/release`"));
check("[C] 未知态续查复用同一执行点（runHubQuickAction，不新造链）",
    drawerSrc.includes("const runSubmitAction = () => {") && drawerSrc.includes("const resumeFromGate = () => {")
    && (drawerSrc.match(/runHubQuickAction\(localId, submitAction/g) || []).length === 1);
check("[C] 撤销仍走既有 revoke 端点（后端一行没改）",
    apiSrc.includes("`/api/hub/task/${encodeURIComponent(localId)}/revoke`") && drawerSrc.includes("await revokeHubTask(localId);"));
check("[C] 没有新造端点（无 override / force-approve / resubmit 之类）",
    !/override|force-approve|resubmit|re-approve/.test(apiSrc) && !/override|force-approve|resubmit/i.test(drawerSrc),
    (apiSrc.match(/override|force-approve|resubmit|re-approve/g) || []).join(","));
check("[C] 无本地草稿边界：抽屉区分 draft / approved / none 三态",
    drawerSrc.includes('data-hub-gen-draft-boundary={draftOrigin}') && drawerSrc.includes('setDraftOrigin(stored ? "draft" : approved ? "approved" : "none")'));
check("[C] 草稿丢了优先用「已批准配置」而不是画布全局默认",
    drawerSrc.includes("setDraft(stored ?? approved?.draft ?? defaultGenDraft(channel, genSeedOf(configNow), configNow))"));
check("[C] 无草稿边界文案按起态门分岔：改不了的态不引导撤销（改成核对 / 解除）",
    drawerSrc.includes("const draftBoundaryText =") && drawerSrc.includes("现在结果待核对，先按上面的指引核对 / 解除阻断，解除之后才能改配置"));
check("[C] 非 awaiting_approval 态配置仍只读（② 顺序前提没被破坏）",
    drawerSrc.includes('const readOnly = state !== "awaiting_approval";') && drawerSrc.includes("data-hub-gen-readonly={readOnly ? \"1\" : \"0\"}"));
check("[C] 配置控件仍逐个只读（渠道 / 画幅 / 清晰度 / 时长 / 声音 / 模式）",
    (drawerSrc.match(/disabled=\{readOnly\}/g) || []).length >= 8, `disabled={readOnly} 次数=${(drawerSrc.match(/disabled=\{readOnly\}/g) || []).length}`);
check("[C] 底部「提交生成」的三道禁用条件没被这次改动拿掉",
    drawerSrc.includes("disabled={busy !== null || needsReapproval || blocked.blocksRetry || sizeBlocked || !submitAction.enabled}"));
check("[C] 列表行内入口（B5a-2）没被这次改动破坏", indexSrc.includes("data-hub-quick-action={quick.kind}") && indexSrc.includes("hubRowQuickAction("));

// 执行侧：releaseHubTask 打到既有端点（用注入 fetch 断言，不发真请求）
const calls = [];
globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, state: "approved" }) };
};
const released = await releaseHubTask("auto-t9");
check("[C] releaseHubTask 请求打到 /api/hub/task/auto-t9/release（POST）",
    calls.length === 1 && calls[0].url === "/api/hub/task/auto-t9/release" && calls[0].method === "POST", JSON.stringify(calls));
check("[C] releaseHubTask 请求体带 confirm:true（后端无 confirm 一律 400）",
    JSON.parse(calls[0].body).confirm === true, calls[0].body);
check("[C] releaseHubTask 返回原样透出（ok/state 不改写）", released.ok === true && released.state === "approved", JSON.stringify(released));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b5b] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
