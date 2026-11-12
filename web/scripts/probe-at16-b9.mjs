/**
 * AT-16 B9 前端探针（本地免费、不联网、不动真实任务）：**解除阻断后换配置直达**。
 *
 * 缺陷 D10（所有者 2026-11-12 08:13 实测 auto-0043）：人工解除阻断（dispatch.phase=cleared）后
 *   · 任务仍是 approved → 模型 / 渠道下拉 `readOnly = state !== "awaiting_approval"` 锁死，配置一个改不了；
 *   · 主操作还是「提交生成」→ 原配置重交（上次正是配置问题才撞 502，必再撞一次）；
 *   · 真正的换配置路（B5b：撤销批准 → 回 awaiting_approval → 改 → 重批）只写在说明文字里，撞不到；
 *   · B5a-1 指引（解除后再提交）与 B5b 路径（换配置先撤销）互相断层。
 *
 * 本探针锁四件事（**复用 B5b 全链，不新造草稿状态** —— 红线 25；后端一行不改）：
 *
 *  [A] 判定 hubClearedDirect（纯函数，抽屉只读它；内部先跑起态门 hubReworkGate，不造第二套判据）
 *      · cleared + approved + 无任何阻断 → visible；三条指引（第三条=换配置先撤销批准）
 *      · 未知 / 在途 / dispatch 未完结 / 解除后又撞未知 → 一律不显示（那不是换配置的路）
 *      · awaiting_approval / generating / done / failed → 不显示（配置本来就能改 / 起态门 mode=none）
 *  [B] 真渲染（SSR 真组件）：cleared 夹具出直达条（标题 / 三条指引 / 心血不作废 / 一键按钮），
 *      未知夹具与待批准夹具渲染为空 —— 可见性由真组件决定，不是探针自己 if
 *  [C] 一键接线：按钮 onClick **就是**抽屉的 revoke（严格相等，不是另写一条撤销实现）；
 *      revoke 自身仍走既有 modal 确认 + revokeHubTask + clearApprovedSignature（B5b 的链一行没换）
 *  [D] 全链 payload（注入 fetch，不发真请求）：撤销 → 回 awaiting_approval（直达条自然退场）→
 *      改模型 → 批准（payload.config.model=新值）→ 提交（key 一次性；模型由这次批准固化）
 *  [E] 守卫：cleared 态「提交生成」保留（原配置重交合法）；配置仍只读；没新造状态 / 新端点
 *
 * 后端那一半（cleared 态 → 撤销 → 重批 → 提交冻结配置 model=新值、心血逐字节不变）由
 * wanrepro/automation/probe_at16_b9.py 锁；B9 不动 automation/ 与 ui/hub_api.py。
 *
 * 跑法：cd web && node scripts/probe-at16-b9.mjs      （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b9");
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

/** 提交诊断形状（B5a-1 的 HubSubmitView；后端 automation/submit_view.build 同源）。 */
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

/**
 * 夹具（全部是后端 submit_view.build 能真产出的形状）：
 *  · CLEARED = 人工 release 之后：submit_unknown / gen_error 已清，dispatch.phase=cleared，state 仍 approved
 */
const CLEARED = V("none", { dispatch_phase: "cleared" });
const UNKNOWN = V("unknown", {
    label: "结果未知·先核对",
    conclusion: "结果未知：可能已收单，先核对",
    dispatch_phase: "dispatched",
    submit_unknown: true,
    blocks_retry: true,
    error_code: "HTTP 502",
});
const PREPARED = V("unknown", { dispatch_phase: "prepared", blocks_retry: true });
/** 解除阻断后又撞了一次未知：cleared 阶段事实 + submit_unknown 标记 → 起态门 unknown，不该给换配置直达 */
const CLEARED_THEN_UNKNOWN = V("unknown", { dispatch_phase: "cleared", submit_unknown: true, blocks_retry: true });
const ACTIVE = V("inflight", { dispatch_phase: "prepared", active_submit: true, blocks_retry: true });
const PLAIN = V("none");

const directOf = (extra) => ({ state: "approved", submit: PLAIN, cloudTaskId: "", ...extra });

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "entry.tsx");
writeFileSync(
    entry,
    'import { renderToStaticMarkup } from "react-dom/server";\n' +
        'import { GenClearedBar, hubClearedDirect, HUB_CLEARED_RECONFIG_STEP } from "../../../src/pages/hub/gen-cleared";\n' +
        'import { hubReworkGate, hubQuickAction, revokeHubTask, approveHubTask, submitHubTask, readApprovedDraft } from "../../../src/pages/hub/hub-api";\n' +
        'import { buildApproveConfig, genDraftSignature, readApprovedSignature, saveApprovedSignature } from "../../../src/lib/hub/gen-settings";\n' +
        "export function renderBar(direct: any): string {\n" +
        "    return renderToStaticMarkup(<GenClearedBar direct={direct} busy={null} onRevoke={() => {}} />);\n" +
        "}\n" +
        "export function barElement(direct: any, busy: any, onRevoke: any): any {\n" +
        "    return GenClearedBar({ direct, busy, onRevoke });\n" +
        "}\n" +
        "export { GenClearedBar, hubClearedDirect, HUB_CLEARED_RECONFIG_STEP, hubReworkGate, hubQuickAction, revokeHubTask, approveHubTask, submitHubTask, readApprovedDraft, buildApproveConfig, genDraftSignature, readApprovedSignature, saveApprovedSignature };\n",
);
const aliasPlugin = {
    name: "at16b9-alias",
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
check("[0] 真代码打包成功（gen-cleared / hub-api / gen-settings 真模块）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

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
const { hubClearedDirect, HUB_CLEARED_RECONFIG_STEP, hubReworkGate } = api;

/** 可见正文（去掉标签壳）。 */
const textOf = (html) => html.replace(/<[^>]+>/g, "").trim();

// ════════════════════════════════════════════════════════════ [A] 判定纯函数
console.log("1) hubClearedDirect：只有 cleared + 无阻断才给换配置直达，其它态一律不显示");

const cleared = hubClearedDirect(directOf({ submit: CLEARED }));
check("[A] cleared + approved + 无阻断 → visible=true（阻断解除后这条路要露出来）", cleared.visible === true && cleared.phase === "cleared", JSON.stringify(cleared).slice(0, 200));
check("[A] 标题直说「已解除阻断」+「换模型 / 渠道重交」", cleared.title.includes("解除阻断") && cleared.title.includes("换模型") && cleared.title.includes("重交"), cleared.title);
check("[A] 指引三条，第三条=「要换模型/渠道：先撤销批准（配置解锁、资产与提示词不动）」",
    cleared.steps.length === 3 && cleared.steps[2] === HUB_CLEARED_RECONFIG_STEP
    && cleared.steps[2].includes("先撤销批准") && cleared.steps[2].includes("配置解锁") && cleared.steps[2].includes("资产与提示词不动"),
    JSON.stringify(cleared.steps));
check("[A] 第一条说清「原配置重交合法」（保留原路径，不逼所有人撤销）",
    cleared.steps[0].includes("原配置重交") && cleared.steps[0].includes("合法"), cleared.steps[0]);
check("[A] 第二条提醒「原配置会再撞同一个错」（D10 的代价讲明白）",
    cleared.steps[1].includes("模型") && cleared.steps[1].includes("渠道"), cleared.steps[1]);
check("[A] 三条指引互不相同（不是同一句话复制三遍）", new Set(cleared.steps).size === 3, JSON.stringify(cleared.steps));
check("[A] 一键按钮文案=换配置（不是「提交生成」那种重复入口）", cleared.action.includes("撤销批准") && cleared.action.includes("配置"), cleared.action);
check("[A] 心血不作废逐字取起态门 keep（B5b 同源，不另写一份）",
    cleared.keep === hubReworkGate(directOf({ submit: CLEARED })).keep
    && ["资产确认", "上传流水", "置换确认", "提示词", "行决策"].every((word) => cleared.keep.includes(word)),
    cleared.keep);
check("[A] hint 同时讲「原配置重交合法」与「可能再撞同一个错」",
    cleared.hint.includes("合法") && cleared.hint.includes("同一个错"), cleared.hint);

// 旧 bridge 没有 submit 透出：用兜底 dispatchPhase 事实仍能判出 cleared（向后兼容）
const legacyCleared = hubClearedDirect({ state: "approved", submit: null, dispatchPhase: "cleared" });
check("[A] 旧 bridge（无 submit）→ 用兜底 dispatchPhase=cleared 仍给直达条", legacyCleared.visible === true, JSON.stringify(legacyCleared.phase));

// 非 cleared / 有阻断：一律不显示
const hidden = [
    ["结果未知（submit_unknown）", directOf({ submit: UNKNOWN })],
    ["dispatch=prepared（请求可能已发出）", directOf({ submit: PREPARED })],
    ["解除后又撞未知（cleared + submit_unknown）", directOf({ submit: CLEARED_THEN_UNKNOWN })],
    ["提交在跑（active_submit）", directOf({ submit: ACTIVE })],
    ["没有 cleared 事实（普通待提交）", directOf({ submit: PLAIN })],
    ["待批准（配置本来就能改）", { state: "awaiting_approval", submit: CLEARED }],
    ["生成中", { state: "generating", submit: CLEARED }],
    ["已交付", { state: "done", submit: CLEARED }],
    ["失败", { state: "failed", submit: CLEARED }],
];
for (const [label, input] of hidden) {
    const d = hubClearedDirect(input);
    check(`[A] ${label} → 不显示（steps/action/keep 全空）`, d.visible === false && d.steps.length === 0 && d.action === "" && d.keep === "", JSON.stringify(d).slice(0, 160));
}
check("[A] 阻断优先级与起态门一致（cleared + submit_unknown → 起态门 unknown，直达条不得抢戏）",
    hubReworkGate(directOf({ submit: CLEARED_THEN_UNKNOWN })).mode === "unknown" && hubClearedDirect(directOf({ submit: CLEARED_THEN_UNKNOWN })).visible === false);

// 纯函数：不读网络 / 不读 localStorage / 不自造端点
const clearedSrc = read("src/pages/hub/gen-cleared.tsx");
const fnBlock = clearedSrc.slice(clearedSrc.indexOf("export function hubClearedDirect("), clearedSrc.indexOf("export function GenClearedBar("));
check("[A] hubClearedDirect 是纯函数（函数体内无 fetch / localStorage / 写盘）",
    !fnBlock.includes("fetch(") && !fnBlock.includes("localStorage") && !fnBlock.includes("await "), fnBlock.slice(0, 120));
check("[A] 判定复用起态门（内部先跑 hubReworkGate，禁造第二套阻断判据）",
    fnBlock.includes("hubReworkGate(input)") && !fnBlock.includes("submit_unknown") && !fnBlock.includes("dispatchStuck"), fnBlock.slice(0, 200));

// ════════════════════════════════════════════════════════════ [B] 真渲染
console.log("2) 真渲染（SSR 真组件）：cleared 出直达条，未知 / 待批准渲染为空");

const htmlCleared = api.renderBar(cleared);
check("[B] cleared 夹具渲染出直达条（data-hub-gen-cleared-direct=1）", htmlCleared.includes('data-hub-gen-cleared-direct="1"') && htmlCleared.includes('data-hub-gen-cleared-phase="cleared"'));
check("[B] 标题可见", textOf(htmlCleared).includes("上次提交已解除阻断——换模型 / 渠道重交"), textOf(htmlCleared).slice(0, 120));
check("[B] 三条指引真渲染出 li（data-hub-gen-cleared-step=1/2/3）",
    htmlCleared.includes('data-hub-gen-cleared-step="1"') && htmlCleared.includes('data-hub-gen-cleared-step="2"') && htmlCleared.includes('data-hub-gen-cleared-step="3"'));
check("[B] 第三条指引正文逐字可见（换配置先撤销批准）", textOf(htmlCleared).includes(HUB_CLEARED_RECONFIG_STEP), HUB_CLEARED_RECONFIG_STEP);
check("[B] 心血不作废在界面上真写出来（data-hub-gen-cleared-keep）",
    htmlCleared.includes("data-hub-gen-cleared-keep") && textOf(htmlCleared).includes("心血不作废"));
check("[B] 一键按钮真渲染（data-hub-gen-cleared-revoke）且文案=换配置", htmlCleared.includes("data-hub-gen-cleared-revoke") && textOf(htmlCleared).includes("撤销批准，换配置"));
check("[B] 可见正文里三段话都在（标题 + 指引 + 承诺），不是空壳 div",
    ["解除阻断", "先撤销批准", "心血不作废"].every((word) => textOf(htmlCleared).includes(word)), textOf(htmlCleared).slice(0, 200));

for (const [label, input] of [
    ["未知收单", directOf({ submit: UNKNOWN })],
    ["提交在跑", directOf({ submit: ACTIVE })],
    ["待批准", { state: "awaiting_approval", submit: CLEARED }],
]) {
    const html = api.renderBar(hubClearedDirect(input));
    check(`[B] ${label} → 真渲染为空（组件返回 null，不出现半截提示）`, html === "" || !html.includes("data-hub-gen-cleared-direct"), String(html).slice(0, 120));
}

// ════════════════════════════════════════════════════════════ [C] 一键接线
console.log("3) 一键接线：按钮 onClick 就是抽屉的 revoke（同一条撤销链，不是第二条实现）");

/** 在 React 元素树里找带某个 data-* 抓手的节点。 */
function findEl(node, attr, hit = null) {
    if (!node || typeof node !== "object") return hit;
    if (Array.isArray(node)) {
        for (const child of node) {
            hit = findEl(child, attr, hit);
            if (hit) return hit;
        }
        return hit;
    }
    const props = node.props || {};
    if (Object.prototype.hasOwnProperty.call(props, attr)) return node;
    return findEl(props.children, attr, hit);
}

let revokeCalls = 0;
const onRevoke = () => {
    revokeCalls += 1;
};
const el = api.barElement(cleared, null, onRevoke);
const btn = findEl(el, "data-hub-gen-cleared-revoke");
check("[C] 元素树里有直达条按钮", Boolean(btn), JSON.stringify(Object.keys(el?.props || {})).slice(0, 160));
check("[C] 按钮 onClick 严格等于抽屉传入的 onRevoke（B5b 的 revoke，不是另写一条）", Boolean(btn) && btn.props.onClick === onRevoke);
btn?.props.onClick?.();
check("[C] 点一下 → onRevoke 被调用恰好一次（一键即触发撤销链）", revokeCalls === 1, `calls=${revokeCalls}`);

const busyEl = api.barElement(cleared, "revoke", onRevoke);
const busyBtn = findEl(busyEl, "data-hub-gen-cleared-revoke");
check("[C] 撤销请求在跑（busy=revoke）→ 按钮 loading 且 disabled（防连点重复撤销）",
    Boolean(busyBtn) && busyBtn.props.loading === true && busyBtn.props.disabled === true,
    JSON.stringify({ loading: busyBtn?.props?.loading, disabled: busyBtn?.props?.disabled }));

const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");
check("[C] 抽屉把直达条接到既有 revoke（onRevoke={revoke}）", drawerSrc.includes("<GenClearedBar direct={clearedDirect} busy={busy} onRevoke={revoke} />"));
check("[C] 抽屉用 hubClearedDirect 判定（单点，不自己写 if 判 cleared）",
    drawerSrc.includes("const clearedDirect = hubClearedDirect({") && drawerSrc.includes("import { GenClearedBar, hubClearedDirect } from \"./gen-cleared\";"));
check("[C] revoke 仍是 B5b 那条链：modal 确认 + revokeHubTask + clearApprovedSignature（一行没换）",
    drawerSrc.includes("const revoke = () => {") && drawerSrc.includes("title: \"撤销批准？（改配置后重批）\"")
    && drawerSrc.includes("await revokeHubTask(localId);") && drawerSrc.includes("clearApprovedSignature(localId);"));
check("[C] revoke 先过起态门（未知态照样点不动）", drawerSrc.includes("if (!reworkGate.allowed) {"));
check("[C] 直达条渲染在生成设置区顶部（早于草稿边界 / 渠道控件）",
    drawerSrc.indexOf("<GenClearedBar direct={clearedDirect}") > 0
    && drawerSrc.indexOf("<GenClearedBar direct={clearedDirect}") < drawerSrc.indexOf("data-hub-gen-draft-boundary={draftOrigin}")
    && drawerSrc.indexOf("<GenClearedBar direct={clearedDirect}") < drawerSrc.indexOf("data-hub-gen-channel"));
check("[C] 只读提示条在 cleared 态点名这条路（不是继续叫人「先撤销批准」一句了事）",
    drawerSrc.includes("上次的阻断已解除：原配置可重交，换配置先撤销批准"));
check("[C] 没新造撤销实现（gen-cleared 只 import 起态门与类型，代码里没有 revoke/fetch/端点）",
    clearedSrc.includes('import { hubReworkGate, type HubReworkGate, type HubReworkInput } from "./hub-api";')
    && !/revokeHubTask|\/api\/hub|fetch\(/.test(clearedSrc.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "")),
    clearedSrc.slice(0, 80));

// ════════════════════════════════════════════════════════════ [D] 全链 payload
console.log("4) 全链 payload（注入 fetch）：撤销 → 改模型 → 批准（model=新值）→ 提交");

const calls = [];
globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, state: "awaiting_approval", snapshot_version: 2, job_id: "job-b9" }) };
};

const TASK = "auto-b9";
const CHANNEL = { id: "ch-sdgf", name: "SDGF 主渠道", baseUrl: "https://sdapi.b9.example.com", apiKey: "sk-b9", apiFormat: "sdgf", models: [{ name: "model-a" }, { name: "model-b" }] };
const DRAFT_B = { channelId: "ch-sdgf", model: "model-b", mode: "frames", size: "16:9", rawSize: "", vquality: "720", duration: 8, audio: true };

// ① 撤销批准（一键触发的那条端点）
const revoked = await api.revokeHubTask(TASK);
check("[D] 撤销批准打到既有 POST /api/hub/task/auto-b9/revoke", calls.length === 1 && calls[0].url === `/api/hub/task/${TASK}/revoke` && calls[0].method === "POST", JSON.stringify(calls[0]));
check("[D] 撤销请求体带 note（撤销原因留痕）", JSON.parse(calls[0].body).note.includes("撤销批准"), calls[0].body);
check("[D] 撤销后 state=awaiting_approval（配置随之解锁 —— readOnly 判据是 state，不是本地标记）", revoked.state === "awaiting_approval", JSON.stringify(revoked));
check("[D] 回 awaiting_approval 后直达条自然退场（不靠本地隐藏标记）",
    hubClearedDirect({ state: "awaiting_approval", submit: CLEARED, dispatchPhase: "cleared" }).visible === false);
check("[D] 配置解锁判据仍是一行 state（`readOnly = state !== \"awaiting_approval\"`，B5b 未破）",
    drawerSrc.includes('const readOnly = state !== "awaiting_approval";'));

// ② 改模型 → 批准（payload.config.model=新值）
const approveConfig = api.buildApproveConfig(DRAFT_B, CHANNEL);
check("[D] 换模型后的批准配置 model=model-b（新值）", approveConfig.model === "model-b", JSON.stringify(approveConfig));
check("[D] 批准配置里没有 key（§2.1：key 只走 submit 一次性透传）", !JSON.stringify(approveConfig).includes("sk-b9"));
await api.approveHubTask(TASK, approveConfig);
const approveCall = calls[calls.length - 1];
const approveBody = JSON.parse(approveCall.body);
check("[D] 重批打到既有 POST /api/hub/approve/auto-b9", approveCall.url === `/api/hub/approve/${TASK}` && approveCall.method === "POST", approveCall.url);
check("[D] 重批 payload 的 config.model=model-b（**新值**，不是原配置）", approveBody.config.model === "model-b", JSON.stringify(approveBody).slice(0, 200));
check("[D] 重批 payload 带渠道身份与参数（固化快照 v2 的输入，前端不改合同）",
    Boolean(approveBody.config.渠道身份?.baseUrl) && approveBody.config.params?.duration === 8, JSON.stringify(approveBody.config).slice(0, 200));

// ③ 提交（key 一次性；模型由这次批准固化 → 后端冻结 gen_channel.model=model-b，见 python 探针 [3]）
await api.submitHubTask(TASK, "sk-b9");
const submitCall = calls[calls.length - 1];
check("[D] 提交打到既有 POST /api/hub/submit/auto-b9", submitCall.url === `/api/hub/submit/${TASK}` && submitCall.method === "POST", submitCall.url);
check("[D] 提交 body 只有 key（不重复带 config：模型走批准快照，单源）",
    Object.keys(JSON.parse(submitCall.body)).join(",") === "key" && JSON.parse(submitCall.body).key === "sk-b9", submitCall.body);

// ④ 批准基线写成新配置 → 之后「提交生成」用的是新模型（不再回到 model-a）
api.saveApprovedSignature(TASK, api.genDraftSignature(DRAFT_B, CHANNEL));
const baseline = api.readApprovedDraft(TASK);
check("[D] 重批后本机批准基线=model-b（再次提交不会被旧配置顶回去）", baseline?.model === "model-b", JSON.stringify(baseline)?.slice(0, 160));
const quick = api.hubQuickAction({
    localId: `${TASK}-quick`,
    state: "approved",
    submit: PLAIN,
    cloudTaskId: "",
    channels: [CHANNEL],
    approvedSignature: api.readApprovedSignature(TASK),
    draft: null,
    busy: false,
});
check("[D] 提交入口取批准快照的渠道与 key（source=approved）", quick.source === "approved" && quick.enabled === true && quick.key === "sk-b9", JSON.stringify({ source: quick.source, enabled: quick.enabled }));

// ════════════════════════════════════════════════════════════ [E] 守卫 / 白名单
console.log("5) 守卫：cleared 态「提交生成」保留 / 配置只读不变 / 没新造状态与端点");

check("[E] cleared 态「提交生成」按钮仍在（原配置重交合法）", drawerSrc.includes("data-hub-gen-submit") && drawerSrc.includes("onClick={submitOnly}"));
check("[E] 「提交生成」的三道禁用条件没被这次改动拿掉",
    drawerSrc.includes("disabled={busy !== null || needsReapproval || blocked.blocksRetry || sizeBlocked || !submitAction.enabled}"));
check("[E] approved 态仍给「撤销批准（改配置）」底部按钮（直达条只是显眼化，不是唯一入口）", drawerSrc.includes("data-hub-gen-revoke"));
check("[E] 非 awaiting_approval 态配置仍只读（dropdown 没被放开）",
    (drawerSrc.match(/disabled=\{readOnly\}/g) || []).length >= 8, `disabled={readOnly} 次数=${(drawerSrc.match(/disabled=\{readOnly\}/g) || []).length}`);
check("[E] 没新造假状态：抽屉/新模块里没有 draft 态机、没有 setState 之类绕过后端", !/state\s*===\s*"draft"/.test(drawerSrc) && !/state\s*===\s*"cleared"/.test(drawerSrc), "仅读 dispatch 事实");
check("[E] 没新造端点：gen-cleared 里零 /api/hub 字样", !clearedSrc.includes("/api/hub"));
check("[E] 后端一行没改（B9 白名单外零改动由主代理对 git diff 验；探针侧只读前端）",
    !drawerSrc.includes("submit_unknown = ") && !drawerSrc.includes("dispatch.phase ="));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b9] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
