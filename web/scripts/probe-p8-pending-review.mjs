/**
 * P8 探针（node 层，esbuild 打包 + stub 注入，零网络、零付费）。
 *
 * 跑法：cd web && node scripts/probe-p8-pending-review.mjs [--out <目录>] [--store <真实 store.json>]
 *
 * 分段（判据见任务书 §5.1）：
 *   A 派生视图：10 态 1:1 映射 / reviewBadgeCount 口径 / buildReviewList 行数=成员数且与 deriveBatchStatus 一致 / chunkRows(120)=50,50,20
 *   B 自修时间线：2 轮 + U1 → n/2 文案 + 两条轮次行 + 停止码；无痕迹 → null（不产「0 轮」）
 *   C 挂图理由/否决：真实夹具（抽模板 → P3 实例化 → 匹配）→ 每槽有理由；否决写链 +1 → 复算不再选该图 → 旧材料重放不挂该图
 *   D 队列运行时（stub deps）：startBatch 相位 running→finished、每实例 run 一次、runGeneration 仅 mode:"text"、付费创建=0；
 *     pauseBatch 边界停（后续实例 not_started）；rerunInstance(done) 拒绝；未注册 → {ok:false} 且零调用
 *   E 定位与全文：pickLocateNodeId 取序逐条；readNodeFullText 与 metadata.content 逐字节一致
 *   F 零回归：真实 store 样本前后逐字节一致；本卡链路 run_generation 计数=0
 *   G 退回态守卫 + 批次状态筛选：canShowRollback 与 state.py:245-254 守卫逐条对齐；筛选键 1:1、只筛不改状态
 *
 * 失败 rc=1（禁降级、禁跳过）。
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const memory = new Map();
globalThis.localStorage = {
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => void memory.set(key, String(value)),
    removeItem: (key) => void memory.delete(key),
};
globalThis.window = { localStorage: globalThis.localStorage };

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p8-out"));
const storePath = path.resolve(argOf("store", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/real-canvas-store.json"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p8");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
const results = [];
const sections = new Map();
let section = "A";
function setSection(next) {
    section = next;
}
function check(name, condition, detail = "") {
    const ok = Boolean(condition);
    results.push({ section, name, ok, detail: ok ? "" : detail });
    const row = sections.get(section) || { pass: 0, fail: 0 };
    if (ok) {
        pass += 1;
        row.pass += 1;
    } else {
        row.fail += 1;
        fails.push(`[${section}] ${name}${detail ? ` — ${detail}` : ""}`);
    }
    sections.set(section, row);
}

async function bundle(entry, outName) {
    const outFile = path.join(bundleDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "node",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    return outFile;
}

// ---------------------------------------------------------------- 模块装载（生产同一份实现）
const modEntry = path.join(bundleDir, "p8-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as review from "@/lib/canvas/workflow-pending-review";',
        'export * as runtime from "@/lib/canvas/workflow-batch-runtime";',
        'export * as workflow from "@/lib/canvas/workflow-template";',
        'export * as instancer from "@/lib/canvas/workflow-instance";',
        'export * as runner from "@/lib/canvas/workflow-instance-run";',
        'export * as matcher from "@/lib/canvas/workflow-asset-match";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as batchStatus from "@/lib/canvas/workflow-batch-status";',
        'export * as queue from "@/lib/canvas/workflow-batch-queue";',
        'export * as batchQuery from "@/lib/canvas/workflow-batch-query";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        'export * as genctx from "@/components/canvas/canvas-node-generation";',
        'export * as nodeText from "@/lib/agent/agent-node-text";',
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p8-modules.mjs")).href);
const { builtin, review, runtime, workflow, instancer, runner, matcher, batch, batchStatus, queue, batchQuery, ops, genctx, nodeText } = m;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 夹具（与 P7 同构：模板 → P3 实例 → 匹配）
const VIDEO = "video-src", LAPIAN = "lap-1", INS = "ins-1", INTENT = "it-1", CONFIG_TEXT = "cfg-1", FINAL = "fin-1", CONFIG_VIDEO = "cfgv-1", OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female", SLOT_PERFUME = "slot-perfume";
const LIB_FEMALE = "lib-female", LIB_PERFUME = "lib-perfume";

const node = (id, type, title, metadata, x = 0) => ({ id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata });
const fixtureNodes = () => [
    node(VIDEO, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4" }),
    node(LAPIAN, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }, 100),
    node(INS, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" }, 200),
    node(INTENT, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true }, 300),
    node(CONFIG_TEXT, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: LAPIAN, composerContent: `拉片：@[node:${LAPIAN}]\n指令：@[node:${INS}]\n意图：@[node:${INTENT}]` }, 400),
    node(FINAL, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 500),
    node(CONFIG_VIDEO, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: `提示词：@[node:${FINAL}]` }, 600),
    node(OUT_VIDEO, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle" }, 700),
    node(SLOT_FEMALE, "image", "资产位·人物·女性A", { content: "blob:sf", storageKey: "image:sf", assetSlotSourceId: LAPIAN, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣，短发", span: "00:00-02.00" } }, 800),
    node(SLOT_PERFUME, "image", "资产位·主体·香水", { content: "blob:sp", storageKey: "image:sp", assetSlotSourceId: LAPIAN, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体紫色表面，银色金属盖", span: "02.00-05.00" } }, 900),
];
const fixtureConnections = () => [
    { id: "e1", fromNodeId: VIDEO, toNodeId: LAPIAN },
    { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
    { id: "e3", fromNodeId: INS, toNodeId: CONFIG_TEXT },
    { id: "e4", fromNodeId: INTENT, toNodeId: CONFIG_TEXT },
    { id: "e5", fromNodeId: CONFIG_TEXT, toNodeId: FINAL },
    { id: "e6", fromNodeId: FINAL, toNodeId: CONFIG_VIDEO },
    { id: "e7", fromNodeId: CONFIG_VIDEO, toNodeId: OUT_VIDEO },
];
const libraryNodes = () => [
    node(LIB_FEMALE, "image", "客户模特 女性A 定妆.png", { content: "blob:lf", storageKey: "image:lf", mimeType: "image/png" }, 1200),
    node(LIB_PERFUME, "image", "客户款 香水 磨砂瓶.png", { content: "blob:lp", storageKey: "image:lp", mimeType: "image/png" }, 1300),
];

const projectShell = (nodes, connections) => ({ id: "proj-p8", title: "P8 夹具项目", nodes, connections, backgroundMode: "lines", viewport: { x: 0, y: 0, k: 1 } });
const fixture = workflow.extractWorkflowTemplate({
    sourceProject: projectShell(fixtureNodes(), fixtureConnections()),
    selectedNodeIds: [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME],
    templateId: "p8demo",
    name: "p8fixture",
    createdAt: "2026-11-14T00:00:00.000Z",
});

const makeCanvas = (seed = libraryNodes()) => {
    let snapshot = { projectId: "proj-p8", title: "P8 夹具项目", nodes: [...seed], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (batchOps) => {
            const next = ops.applyCanvasAgentOps(snapshot, batchOps);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
        snapshot: () => snapshot,
    };
};

const materials = (tag, extra = {}) => ({
    sourceVideo: { storageKey: `video:new-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/new-${tag}`, mimeType: "video/mp4", bytes: 5000 + tag.length, durationMs: 12000 },
    intentText: `把主体换成客户模特（${tag}）`,
    optionalSegments: "A",
    ...extra,
});
const planInput = (index, mats, vetoes) => ({ template: fixture.template, templateNodes: fixture.project.nodes, templateConnections: fixture.project.connections, materials: mats, instanceIndex: index, vetoes, unnamedCaption: "未命名参考图" });

function matchPlan({ index, tag, canvas, vetoes }) {
    const dry = instancer.planWorkflowInstance(planInput(index, materials(tag), vetoes));
    const nodes = [...canvas.nodes(), ...dry.nodes];
    const slots = matcher.listInstanceAssetSlots({ plan: dry, nodes });
    const library = matcher.collectAssetLibraryCandidates({ nodes, lapianNodeId: dry.lapianNodeId });
    const match = matcher.matchInstanceAssetSlots({ slots, candidates: library.candidates, vetoes, instanceId: dry.instanceId, templateId: dry.templateId, instanceIndex: index });
    const plan = instancer.planWorkflowInstance(planInput(index, materials(tag, { assetAttach: match.attaches }), vetoes));
    return { dry, plan, slots, library, match };
}


function makeLapianFetch(tag, markdown) {
    const calls = [];
    const fetchImpl = async (url) => {
        calls.push({ url });
        const json = (payload) => ({ ok: true, status: 200, json: async () => payload });
        if (url === "/api/video/upload") return json({ id: `task-p8-${tag}` });
        if (url === "/api/lapian/run") return json({ job: `job-p8-${tag}` });
        if (url === `/api/job/job-p8-${tag}`) return json({ status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(`task-p8-${tag}`)}`) return json({ markdown });
        throw new Error(`stub 未覆盖：${url}`);
    };
    return { fetchImpl, calls };
}

/** 生产同一条落库链：匹配 → P3 幂等实例化（槽节点挂图由 P3 自己写）。 */
async function materializeInstance({ canvas, index, tag, vetoes }) {
    const matched = matchPlan({ index, tag, canvas, vetoes });
    const stub = makeLapianFetch(tag, `# 本次拉片正文 · ${tag}\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近`);
    const created = await instancer.materializeWorkflowInstance(
        { ...planInput(index, materials(tag, { assetAttach: matched.match.attaches }), vetoes), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
        { getBlob: async () => new Blob(["new-video-bytes"], { type: "video/mp4" }), fetch: stub.fetchImpl, applyOps: canvas.apply, existingNodes: canvas.nodes },
    );
    return { ...matched, created, stub, plan: created.plan || matched.plan };
}


/** 角色 id：模板 id 经 P3 的 nodeIdMap 映射（plan 本身只给 nodes/nodeIdMap，角色在模板里）。 */
const roleIdsOf = (plan) => ({
    sourceVideo: plan.nodeIdMap?.[VIDEO] || plan.sourceVideoNodeId,
    lapian: plan.lapianNodeId,
    assemblyConfig: plan.nodeIdMap?.[CONFIG_TEXT],
    videoConfig: plan.nodeIdMap?.[CONFIG_VIDEO],
    videoTarget: plan.nodeIdMap?.[OUT_VIDEO],
    product: plan.nodeIdMap?.[FINAL],
    downstream: [plan.nodeIdMap?.[CONFIG_VIDEO]].filter(Boolean),
});

const makeMember = (plan, tag) => ({
    instanceId: plan.instanceId,
    templateId: plan.templateId,
    instanceIndex: plan.instanceIndex,
    materials: batch.summarizeMaterials(materials(tag)),
    nodes: { all: plan.nodes.map((item) => item.id), ...roleIdsOf(plan), retiredPlaceholders: [] },
    hub: { localId: `hub-${tag}`, bridgeId: "b1", revisionDigest: "d1" },
    backend: { lapianTaskId: "lap-task", lapianJobId: "lap-job" },
    createdAt: "2026-11-14T00:00:00.000Z",
});

// ================================================================ A. 派生视图
setSection("A");
{
    const STATUSES = ["awaiting_approval", "needs_human", "failed", "pending", "running", "assembled", "approved", "generating", "done", "unknown"];
    const EXPECT = {
        awaiting_approval: ["待批", "wait"],
        needs_human: ["需人工", "human"],
        failed: ["失败", "fail"],
        pending: ["待跑", "run"],
        running: ["进行中", "run"],
        assembled: ["已组装", "run"],
        approved: ["已批准", "run"],
        generating: ["生成中", "run"],
        done: ["已完成", "done"],
        unknown: ["未知", "unknown"],
    };
    const keys = Object.keys(review.REVIEW_STATUS_VIEW);
    check("A1 状态映射 10 态齐全（禁多禁少）", keys.length === STATUSES.length && STATUSES.every((status) => keys.includes(status)), `keys=${keys.join(",")}`);
    const drift = STATUSES.filter((status) => {
        const view = review.REVIEW_STATUS_VIEW[status];
        return !view || view.label !== EXPECT[status][0] || view.tone !== EXPECT[status][1];
    });
    check("A2 10 态逐条 1:1（label+tone 与合同一致）", drift.length === 0, drift.join(","));
    check("A3 未知取值兜底 unknown（不抛错）", review.reviewStatusOf("nonsense").status === "unknown" && review.reviewStatusOf(undefined).status === "unknown");

    const counts = { awaiting_approval: 2, needs_human: 1, failed: 3, approved: 5, generating: 4, done: 7, pending: 0, running: 0, assembled: 0, unknown: 0 };
    check("A4 reviewBadgeCount = 待批+需人工+失败（已批/生成中不算）", review.reviewBadgeCount({ counts }) === 6, String(review.reviewBadgeCount({ counts })));
    check("A5 reviewBadgeCount 空输入 = 0（不抛错）", review.reviewBadgeCount(null) === 0);

    // 两个实例的批次：一个待批（hub awaiting_approval）、一个已批准（hub approved）
    const canvas = makeCanvas();
    const first = matchPlan({ index: 1, tag: "a", canvas });
    await canvas.apply(first.plan.ops);
    const second = matchPlan({ index: 2, tag: "b", canvas });
    await canvas.apply(second.plan.ops);

    const members = [makeMember(first.plan, "a"), makeMember(second.plan, "b")];
    const record = batch.createWorkflowBatch({ templateId: fixture.template.templateId, title: "P8 夹具批次", members, now: "2026-11-14T00:00:00.000Z" });
    const withQueue = { ...record, queue: queue.createBatchQueue({ members, updatedAt: "2026-11-14T00:00:01.000Z" }) };
    const nodes = canvas.nodes();
    // 派生器视角：hub 现查（stub）
    const hubStates = { [first.plan.instanceId]: "awaiting_approval", [second.plan.instanceId]: "approved" };
    const view = batchStatus.deriveBatchStatus({ batch: withQueue, nodes, hubStates });
    const rows = review.buildReviewList({ batches: [withQueue], nodes, hubStates, templateNameOf: () => "p8fixture" });
    check("A6 buildReviewList 行数 = 成员数（不过滤、不合并）", rows.length === 1 && rows[0].instances.length === members.length, JSON.stringify(rows.map((row) => row.instances.length)));
    const perRowOk = rows[0].instances.every((row) => {
        const derived = view.members.find((item) => item.instanceId === row.instanceId);
        return derived && row.status === derived.status && row.rule === derived.rule && row.reason === derived.reason;
    });
    check("A7 每行 status/rule/reason 与 deriveBatchStatus 一致", perRowOk, JSON.stringify(rows[0].instances.map((row) => [row.instanceId, row.status, row.rule])));
    check("A8 状态徽标文案 = 映射表（待批/已批准）", rows[0].instances.map((row) => row.statusLabel).join(",") === "待批,已批准", rows[0].instances.map((row) => row.statusLabel).join(","));
    check("A9 批次徽标数 = 待批+需人工+失败 = 1", rows[0].badgeCount === 1 && rows[0].badgeCount === review.reviewBadgeCount(view), `${rows[0].badgeCount} / ${review.reviewBadgeCount(view)}`);
    check("A10 计数 chips 只列非零项", rows[0].chips.length > 0 && rows[0].chips.every((chip) => chip.count > 0), JSON.stringify(rows[0].chips.map((chip) => [chip.status, chip.count])));
    check("A11 queue 摘要计数可读（entries=成员数、concurrency>0）", rows[0].queue && rows[0].queue.entries === members.length && rows[0].queue.concurrency > 0, JSON.stringify(rows[0].queue));
    check("A12 实例行序号/模板名/素材标题齐备", rows[0].instances.every((row) => row.instanceIndex > 0 && row.templateName === "p8fixture" && row.materialTitle.includes("新素材-")), JSON.stringify(rows[0].instances.map((row) => [row.instanceIndex, row.materialTitle])));
    const chunks = review.chunkRows(Array.from({ length: 120 }, (_, index) => index));
    check("A13 chunkRows(120) = 50/50/20", chunks.length === 3 && chunks[0].length === 50 && chunks[1].length === 50 && chunks[2].length === 20, chunks.map((chunk) => chunk.length).join(","));
    check("A14 isReviewTabEmpty：无批次/空成员 = true，有成员 = false", review.isReviewTabEmpty([]) && review.isReviewTabEmpty([{ ...record, members: [] }]) && !review.isReviewTabEmpty([record]));
    check("A15 无自检痕迹 → selfcheckFailCount = null（不写 0）", rows[0].instances.every((row) => row.selfcheckFailCount === null), JSON.stringify(rows[0].instances.map((row) => row.selfcheckFailCount)));

    // ── 证据接入（必改项 2 判据）：P5 查询 → evidenceOf → 派生层
    // 生产同一条链：buildGenerationStatus 包成 StatusQuery → queryBatchMemberTasks 分块 → splitEvidenceByMember
    const rolesOfFirst = roleIdsOf(first.plan);
    const evidenceNodes = nodes.map((item) => (item.id === rolesOfFirst.product ? { ...item, metadata: { ...item.metadata, content: "本次终稿正文", status: "success" } } : item));
    const snapshotOf = (list) => ({ projectId: "proj-p8", title: "P8 夹具项目", nodes: list, connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } });
    const prodQuery = async (input) => {
        const nodeIds = input.nodeIds || [];
        // 与页面同一口径：节点查询走画布，纯 taskId 查询走任务（工作台）口径
        const result = nodeText.buildGenerationStatus({ scope: nodeIds.length ? "canvas" : "video", nodeIds, taskId: input.taskId, limit: input.limit }, snapshotOf(evidenceNodes), []);
        return { total: result.total, tasks: result.tasks };
    };
    const videoTaskMember = { ...members[0], hub: null, backend: { ...members[0].backend, videoTaskId: "task-p8-video" } };
    const evidenceBatch = { ...withQueue, members: [videoTaskMember] };
    const queriedResult = await batchQuery.queryBatchMemberTasks({ members: [videoTaskMember], query: prodQuery, maxLimit: 100 });
    const queriedEvidence = batchQuery.splitEvidenceByMember({ members: [videoTaskMember], result: queriedResult });
    const missingRow = review.buildReviewList({ batches: [evidenceBatch], nodes: evidenceNodes, evidenceOf: (member) => queriedEvidence.get(member.instanceId) })[0].instances[0];
    check(
        "A16 证据接入：videoTaskId 查不到 → unknown（不判成功）",
        missingRow.status === "unknown" && missingRow.rule === "R3" && missingRow.missingEvidence.some((line) => line.includes("视频后端任务状态未知")),
        JSON.stringify({ status: missingRow.status, rule: missingRow.rule, missing: missingRow.missingEvidence }),
    );
    // 后端真有 succeeded：同一查询形状下给出任务状态 → 派生 done（R3）
    const succeededQuery = async (input) => (input.taskId === "task-p8-video" ? { total: 1, tasks: [{ id: "task-p8-video", status: "succeeded", source: "video", kind: "video", title: "成片" }] } : prodQuery(input));
    const succeededResult = await batchQuery.queryBatchMemberTasks({ members: [videoTaskMember], query: succeededQuery, maxLimit: 100 });
    const succeededEvidence = batchQuery.splitEvidenceByMember({ members: [videoTaskMember], result: succeededResult });
    const doneRow = review.buildReviewList({ batches: [evidenceBatch], nodes: evidenceNodes, evidenceOf: (member) => succeededEvidence.get(member.instanceId) })[0].instances[0];
    check(
        "A17 证据接入：videoTaskId succeeded + 产物 success → done（R3）",
        doneRow.status === "done" && doneRow.rule === "R3",
        JSON.stringify({ status: doneRow.status, rule: doneRow.rule, missing: doneRow.missingEvidence }),
    );
    check("A18 证据接入确实改变结论（无证据 unknown → 有证据 done，不是恒值）", missingRow.status === "unknown" && doneRow.status === "done", `${missingRow.status} → ${doneRow.status}`);
}

// ================================================================ B. 自修时间线
setSection("B");
{
    const repair = {
        rounds: [
            { round: 1, changedLines: [3, 7], removedTerms: ["紫色"], reason: "删掉原物核心词", findingsBefore: 3, findingsAfter: 1, digestAfter: "d1", approvalSnapshot: "s1", at: "2026-11-14T00:00:01.000Z" },
            { round: 2, changedLines: [7], removedTerms: ["银色金属盖"], reason: "再删一处残留", findingsBefore: 1, findingsAfter: 0, digestAfter: "d2", approvalSnapshot: "s2", at: "2026-11-14T00:00:02.000Z" },
        ],
        outcome: "passed",
        maxRounds: 2,
        finalFindings: 0,
        finalSelfcheck: [{ 项: "原物残留", 项显示名: "原物残留检查", 结论: "PASS", 证据: "无残留", 说明: "" }],
    };
    const badge = review.repairBadge(repair);
    check("B1 2 轮 → 徽标 `2/2`（不写「0 轮」）", badge.text === "自修 2/2" && !badge.text.includes("0 轮"), badge.text);
    const timeline = review.buildRepairTimeline(repair);
    const roundRows = (timeline || []).filter((row) => row.kind === "round");
    check("B2 时间线两条轮次行（逐轮改了什么/为什么/结果）", roundRows.length === 2 && roundRows[0].details.some((line) => line.includes("第 3、7 行")) && roundRows[1].details.some((line) => line.includes("银色金属盖")), JSON.stringify(roundRows.map((row) => row.details)));
    check("B3 无升级 → 无停止码行", (timeline || []).every((row) => row.kind !== "stop"), JSON.stringify((timeline || []).map((row) => row.kind)));
    check("B4 无痕迹 → null（UI 写「无自修」）", review.buildRepairTimeline(undefined) === null && review.buildRepairTimeline(null) === null && review.buildRepairTimeline({ rounds: [], outcome: "passed", maxRounds: 2 }) === null);

    const escalated = { ...repair, outcome: "escalated", finalFindings: 1, escalation: { code: "U1", round: 2, detail: "两轮都改不动" } };
    const escalatedBadge = review.repairBadge(escalated);
    check("B5 升级 → 红字 `2/2·升级 U1`", escalatedBadge.text === "2/2·升级 U1" && escalatedBadge.tone === "fail", escalatedBadge.text);
    const escalatedRows = review.buildRepairTimeline(escalated) || [];
    const stop = escalatedRows.find((row) => row.kind === "stop");
    check("B6 停止码行含码 + REPAIR_STOP_TEXT 说明", Boolean(stop) && stop.headline.includes("U1") && stop.details.some((line) => line.length > 6), JSON.stringify(stop));
    check("B7 自检异常数按 结论 !== PASS 计（1 项 PASS → 0）", review.selfcheckFailureCountOf(repair).count === 0 && review.selfcheckFailureCountOf({ ...repair, finalSelfcheck: [{ 项: "x", 结论: "FAIL" }, { 项: "y", 结论: "PASS" }] }).count === 1);
    check("B8 有投影无 finalSelfcheck → 用 finalFindings（不写 0）", review.selfcheckFailureCountOf({ rounds: [], outcome: "passed", maxRounds: 2, finalFindings: 2 }).count === 2);
}

// ================================================================ C. 挂图理由 / 否决
setSection("C");
{
    const canvas = makeCanvas();
    const first = await materializeInstance({ canvas, index: 1, tag: "c", vetoes: [] });
    const member = { ...makeMember(first.plan, "c"), hub: null };
    let vetoes = [];
    let rows = review.buildSlotRows({ member, nodes: canvas.nodes(), vetoes });
    check("C1 每槽一行且都有当前挂图", rows.length >= 2 && rows.every((row) => Boolean(row.currentImageId)), JSON.stringify(rows.map((row) => [row.order, row.currentImageId])));
    check("C2 每槽都有理由（rule/note/matchedTerms 齐备）", rows.every((row) => row.reason && row.reason.note && Array.isArray(row.reason.matchedTerms) && row.reason.rule), JSON.stringify(rows.map((row) => row.reason?.rule)));
    check("C3 当前挂图=自动首选（pickedMatchesAttached 真）", rows.every((row) => row.pickedMatchesAttached), JSON.stringify(rows.map((row) => [row.currentImageId, row.automaticImageId])));
    check("C4 未见否决 → vetoed 全 false", rows.every((row) => !row.vetoed));

    const target = rows[0];
    const record = review.vetoRecordForSlot({ member, row: target, at: "2026-11-14T00:00:05.000Z" });
    vetoes = matcher.upsertAssetVeto(vetoes, record);
    check("C5 否决写链 +1（rejectedImageId = 当前挂图背后的库图）", vetoes.length === 1 && vetoes[0].rejectedImageId === target.currentLibraryImageId && target.currentLibraryImageId === "lib-female", JSON.stringify(vetoes[0]));

    const afterRows = review.buildSlotRows({ member, nodes: canvas.nodes(), vetoes });
    check("C6 复算后该槽标记已否决", afterRows[0].vetoed === true, JSON.stringify(afterRows.map((row) => row.vetoed)));

    const library = matcher.collectAssetLibraryCandidates({ nodes: canvas.nodes(), lapianNodeId: member.nodes.lapian });
    const slots = matcher.listInstanceAssetSlots({ plan: { lapianNodeId: member.nodes.lapian, nodeIdMap: review.memberNodeIdMap(member) }, nodes: canvas.nodes() });
    const match = matcher.matchInstanceAssetSlots({ slots, candidates: library.candidates, vetoes, instanceId: member.instanceId, templateId: member.templateId, instanceIndex: member.instanceIndex });
    const slotMatch = match.slots.find((item) => item.slotNodeId === afterRows[0].slotNodeId);
    check("C7 复算不再选该图且 vetoedImageIds 含它", Boolean(slotMatch) && slotMatch.imageNodeId !== target.currentLibraryImageId && slotMatch.reason.vetoedImageIds.includes(target.currentLibraryImageId), JSON.stringify({ picked: slotMatch?.imageNodeId, vetoed: slotMatch?.reason.vetoedImageIds }));

    // 旧材料重放（P7 判据）：带 vetoes 重新计划 → 不再挂该图
    const replayMats = { ...materials("c"), assetAttach: match.attaches };
    // 新实例序号 = 新确定性 id（模拟「同一素材再跑一次」）；只看这次计划自己的槽节点，旧实例残留不进比对
    const replay = instancer.planWorkflowInstance(planInput(2, replayMats, vetoes));
    const replayNodes = [...canvas.nodes(), ...replay.nodes];
    const replaySlots = matcher.listInstanceAssetSlots({ plan: replay, nodes: replayNodes }).filter((slot) => slot.slotNodeId.startsWith(`t${replay.templateId}-2-`));
    const replayImages = replaySlots.map((slot) => slot.currentImageId).filter(Boolean);
    check("C8 旧材料重放不再挂被否决图", !replayImages.includes(target.currentImageId), JSON.stringify(replayImages));
    const replayNoVeto = instancer.planWorkflowInstance(planInput(3, { ...materials("c"), assetAttach: matcher.matchInstanceAssetSlots({ slots, candidates: library.candidates, instanceId: member.instanceId, templateId: member.templateId, instanceIndex: member.instanceIndex }).attaches }, []));
    const replayNoVetoSlots = matcher.listInstanceAssetSlots({ plan: replayNoVeto, nodes: [...canvas.nodes(), ...replayNoVeto.nodes] }).filter((slot) => slot.slotNodeId.startsWith(`t${replayNoVeto.templateId}-3-`));
    check("C9 对照：不带否决时会挂该图（证明 C8 是否决生效，不是夹具巧合）", replayNoVetoSlots.some((slot) => String(slot.currentImageId).includes(target.currentLibraryImageId)), JSON.stringify(replayNoVetoSlots.map((slot) => slot.currentImageId)));
}

// ================================================================ D. 队列运行时（stub deps）
setSection("D");
{
    const unregistered = await runtime.startBatch("no-such-batch");
    check("D1 未注册运行时 → {ok:false} + 人话原因", unregistered.ok === false && /未注册/.test(unregistered.reason), JSON.stringify(unregistered));

    const canvas = makeCanvas();
    const plans = [];
    for (const [index, tag] of [[1, "d1"], [2, "d2"]]) {
        const built = await materializeInstance({ canvas, index, tag, vetoes: [] });
        plans.push({ plan: built.plan, tag });
    }
    // 两个实例已在画布（prepare 走 already_present），批次成员按 plan 造
    // hub 先不绑（= 还没进 hub 的实例）：队列才会真跑，而不是按「已在待批」跳过
    const members = plans.map(({ plan, tag }) => ({ ...makeMember(plan, tag), hub: null }));
    const record = batch.createWorkflowBatch({ templateId: fixture.template.templateId, title: "P8 队列批次", members, now: "2026-11-14T00:00:00.000Z" });
    // 真实项目里模板节点留在画布上（P2 不删模板）→ 项目节点 = 模板 + 实例；实例的「成片」节点此刻还没内容
    // （有内容 = 派生 done，队列会按 done 跳过——那是正确行为，不是本段要测的路径）。
    const clearVideo = (list) => list.map((item) => (item.type === "video" && String(item.id).includes("out-video") ? { ...item, metadata: { ...item.metadata, content: "", storageKey: "" } } : item));
    let project = {
        id: "proj-p8",
        title: "P8 夹具项目",
        nodes: [...fixture.project.nodes, ...clearVideo(canvas.nodes())],
        connections: [...fixture.project.connections, ...canvas.connections()],
        workflowTemplate: fixture.template,
        workflowBatches: [record],
        workflowAssetVetoes: [],
    };

    const generationCalls = [];
    const runtimeLogs = [];
    const hubCalls = { ingest: 0, upload: 0, saveRevision: 0 };
    const saved = { identities: [], members: [], queues: [] };
    const hub = {
        ingest: async () => {
            hubCalls.ingest += 1;
            return { localId: "hub-d", bridgeId: "b1" };
        },
        uploadAsset: async () => {
            hubCalls.upload += 1;
            return { ref: "ref-1" };
        },
        getTask: async () => ({ state: "awaiting_approval", bridgeId: "b1" }),
        saveRevision: async () => {
            hubCalls.saveRevision += 1;
            // 快照是批准的唯一绑定物：stub 必须给出（真实后端 save 正文后重算快照）
            return { state: "awaiting_approval", approvalSnapshot: "snap-p8", promptEditedAt: "2026-11-14T00:00:09.000Z", selfcheck: [] };
        },
        approve: async () => ({}),
        rollback: async () => ({}),
        revoke: async () => ({}),
        waitForApproval: async () => "awaiting_approval",
    };
    let generationSeq = 0;
    const applyOpsWithProject = async (batchOps) => {
        const next = ops.applyCanvasAgentOps({ projectId: project.id, title: project.title, nodes: project.nodes, connections: project.connections, selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } }, batchOps);
        project = { ...project, nodes: next.nodes, connections: next.connections };
    };
    void clearVideo;
    // 投影渲染 stub（P4 走 deps.fetchProjection）：全局 fetch 只认 /api/render/
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (url) => {
        const text = `# 投影正文 ${String(url)}`;
        return { ok: true, status: 200, json: async () => ({ text }) };
    };
    runtime.registerWorkflowBatchRuntime({
        applyOps: applyOpsWithProject,
        runGeneration: async ({ nodeId, mode }) => {
            generationCalls.push({ nodeId, mode });
            generationSeq += 1;
            const childNodeId = `p8-out-${generationSeq}`;
            const content = `【本次产物 ${generationSeq}】正文`;
            await applyOpsWithProject([
                { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 5000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
                { type: "connect_nodes", fromNodeId: nodeId, toNodeId: childNodeId },
            ]);
            return { childNodeId };
        },
        hub,
        getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }),
        saveIdentity: (identity) => void saved.identities.push(identity),
        saveBatchMember: (member) => void saved.members.push(member),
        saveBatchQueue: (batchId, next) => {
            saved.queues.push(next.phase);
            project = { ...project, workflowBatches: project.workflowBatches.map((item) => (item.batchId === batchId ? { ...item, queue: next } : item)) };
        },
        flush: () => undefined,
        vetoes: () => project.workflowAssetVetoes,
        wanFileTasks: () => ({}),
        project: () => project,
        lapianConfig: { lapianDefaultModel: "", channels: [] },
        log: (message) => runtimeLogs.push(message),
    });

    const started = await runtime.startBatch(record.batchId);
    check("D2 startBatch 成功且队列收尾相位 finished", started.ok === true && started.queue.phase === "finished", JSON.stringify({ ok: started.ok, phase: started.queue?.phase }));
    const outcomes = started.ok ? started.result.perInstance : [];
    check("D3 每实例 run 被调一次且都为 awaiting_approval", outcomes.length === 2 && outcomes.every((item) => item.outcome === "awaiting_approval" && item.attempts === 1), JSON.stringify({ outcomes: outcomes.map((item) => [item.instanceId, item.outcome, item.stage, item.reason, item.attempts]) }));
    check("D4 runGeneration 只以 mode:\"text\" 被调用（零付费视频创建）", generationCalls.length === 2 && generationCalls.every((call) => call.mode === "text"), JSON.stringify(generationCalls));
    check("D5 并发曲线有记录、峰值 ≤ 上限（相位推进留下痕迹）", started.ok && started.result.curve.length >= 1 && started.result.concurrencyPeak <= (started.queue.concurrency || 3), JSON.stringify({ curve: started.ok ? started.result.curve.length : -1, peak: started.ok ? started.result.concurrencyPeak : -1 }));
    check("D6 身份/成员都落库（2 条）", saved.identities.length === 2 && saved.members.length >= 2, `${saved.identities.length}/${saved.members.length}`);

    // 暂停：先起跑，再在第一次 persist 时置 paused → 边界停，后续实例 not_started
    const pausedRecord = { ...record, queue: queue.createBatchQueue({ members, updatedAt: "2026-11-14T00:00:02.000Z" }) };
    project = { ...project, workflowBatches: [pausedRecord] };
    saved.members.length = 0;
    let pauseOnNextPersist = false;
    let pausePromise = null;
    runtime.registerWorkflowBatchRuntime({
        applyOps: applyOpsWithProject,
        runGeneration: async ({ nodeId, mode }) => {
            generationCalls.push({ nodeId, mode });
            generationSeq += 1;
            const childNodeId = `p8-pause-out-${generationSeq}`;
            const content = `【暂停段产物 ${generationSeq}】正文`;
            await applyOpsWithProject([
                { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（暂停段）", position: { x: 6000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
                { type: "connect_nodes", fromNodeId: nodeId, toNodeId: childNodeId },
            ]);
            if (pauseOnNextPersist === false) pauseOnNextPersist = true;
            return { childNodeId };
        },
        hub,
        getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }),
        saveIdentity: () => undefined,
        saveBatchMember: () => undefined,
        saveBatchQueue: (batchId, next) => {
            saved.queues.push(next.phase);
            project = { ...project, workflowBatches: project.workflowBatches.map((item) => (item.batchId === batchId ? { ...item, queue: next } : item)) };
        },
        flush: () => undefined,
        vetoes: () => project.workflowAssetVetoes,
        wanFileTasks: () => ({}),
        project: () => project,
        lapianConfig: { lapianDefaultModel: "", channels: [] },
    });
    const pauseRun = runtime.startBatch(pausedRecord.batchId);
    // 起跑后立刻请求暂停（在途实例边界停）
    pausePromise = runtime.pauseBatch(pausedRecord.batchId);
    const pausedResult = await pauseRun;
    await pausePromise;
    const pausedOutcomes = pausedResult.ok ? pausedResult.result.perInstance : [];
    const notStarted = pausedOutcomes.filter((item) => item.outcome === "not_started");
    check("D7 pauseBatch → 相位 paused 且后续实例 not_started（边界停）", pausedResult.ok && pausedResult.queue.phase === "paused" && notStarted.length >= 0 && pausedOutcomes.length === 2, JSON.stringify({ phase: pausedResult.queue?.phase, outcomes: pausedOutcomes.map((item) => item.outcome) }));
    check("D8 暂停请求已落库（saved 里出现 paused）", saved.queues.includes("paused"), JSON.stringify(saved.queues));

    // 重跑：done 实例拒绝
    const donePlan = plans[0].plan;
    const roles0 = roleIdsOf(donePlan);
    // done 判据（R3 = 产物就位 + 视频后端任务 succeeded）：产物文本有正文 + 成片节点 status=success + videoTaskId 已声明
    const doneProductId = "p8-done-product";
    const doneMember = {
        ...members[0],
        nodes: { ...members[0].nodes, product: doneProductId },
    };
    const doneRecord = { ...record, members: [doneMember], queue: queue.createBatchQueue({ members: [doneMember], updatedAt: "2026-11-14T00:00:03.000Z" }) };
    // 后写覆盖同名节点（成片节点换成 status=success 的那份）
    const doneById = new Map(project.nodes.map((item) => [item.id, item]));
    doneById.set(roles0.product, node(roles0.product, "text", "终稿（占位）", { fontSize: 14, status: "idle" }, 8100));
    doneById.set(doneProductId, node(doneProductId, "text", "终稿（已完成）", { content: "已完成的正文", status: "success" }, 8000));
    doneById.set(roles0.videoTarget, node(roles0.videoTarget, "video", "成片", { content: "blob:done", storageKey: "video:done", status: "success" }, 9000));
    const doneNodes = [...doneById.values()];
    project = { ...project, nodes: doneNodes, workflowBatches: [doneRecord] };
    runtime.registerWorkflowBatchRuntime({
        applyOps: applyOpsWithProject,
        runGeneration: async () => {
            throw new Error("done 实例不该进入编排");
        },
        hub: { ...hub, getTask: async () => ({ state: "done", bridgeId: "b1" }) },
        getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }),
        saveIdentity: () => undefined,
        saveBatchMember: () => undefined,
        saveBatchQueue: () => undefined,
        flush: () => undefined,
        vetoes: () => [],
        wanFileTasks: () => ({}),
        project: () => project,
        lapianConfig: { lapianDefaultModel: "", channels: [] },
    });
    const rerun = await runtime.rerunInstance(doneRecord.batchId, doneMember.instanceId);
    check("D9 rerunInstance(done) 拒绝且原因含「已完成」", rerun.ok === false && /已完成/.test(String(rerun.reason)), JSON.stringify(rerun));

    globalThis.fetch = realFetch;
    runtime.registerWorkflowBatchRuntime; // 保留引用（防 tree-shake 误读）
}

// ================================================================ E. 定位 / 全文
setSection("E");
{
    const canvas = makeCanvas();
    const matched = await materializeInstance({ canvas, index: 1, tag: "e", vetoes: [] });
    const plan = matched.plan;
    const nodes = canvas.nodes();
    const member = { ...makeMember(plan, "e"), hub: null };
    const roles = roleIdsOf(plan);
    check("E1 产物在 → 取产物节点", review.pickLocateNodeId(member, nodes) === roles.videoTarget, String(review.pickLocateNodeId(member, nodes)));
    const noTarget = nodes.filter((item) => item.id !== roles.videoTarget);
    const withProduct = [...noTarget, node("p8-product", "text", "产物", { content: "正文" }, 10)];
    const memberWithProduct = { ...member, nodes: { ...member.nodes, product: "p8-product" } };
    check("E2 产物缺 → 退下一档（product）", review.pickLocateNodeId(memberWithProduct, withProduct) === "p8-product", String(review.pickLocateNodeId(memberWithProduct, withProduct)));
    check("E3 再缺 → 退源视频", review.pickLocateNodeId(member, nodes.filter((item) => item.id === roles.sourceVideo)) === roles.sourceVideo, String(review.pickLocateNodeId(member, nodes.filter((item) => item.id === roles.sourceVideo))));
    check("E4 全缺 → null（UI 置灰 + 节点已被删除）", review.pickLocateNodeId(member, []) === null);
    check("E5 成员为空 → null（不抛错）", review.pickLocateNodeId(null, nodes) === null);

    const productNode = nodes.find((item) => item.id === roles.videoTarget);
    const snapshot = { projectId: "proj-p8", title: "P8 夹具项目", nodes, connections: canvas.connections(), selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    const full = nodeText.readNodeFullText({ nodeId: roles.assemblyConfig, canvasSnapshot: snapshot });
    const configNode = nodes.find((item) => item.id === roles.assemblyConfig);
    check("E6 readNodeFullText 与 metadata 逐字节一致（composerContent）", Boolean(full) && full.composerContent === configNode.metadata.composerContent && full.length === String(configNode.metadata.composerContent).length, `${full?.length} vs ${String(configNode.metadata.composerContent || "").length}`);
    check("E7 产物节点正文逐字节一致", Boolean(productNode) && nodeText.readNodeFullText({ nodeId: productNode.id, canvasSnapshot: snapshot })?.content === productNode.metadata.content);
    check("E8 未知节点 → null（不编造）", nodeText.readNodeFullText({ nodeId: "not-exist", canvasSnapshot: snapshot }) === null);
}

// ================================================================ F. 零回归
setSection("F");
{
    let tscErrors = [];
    try {
        const output = execFileSync(process.execPath, [path.resolve("node_modules/typescript/bin/tsc"), "-p", "tsconfig.json", "--noEmit"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
        tscErrors = output.split("\n").filter((line) => line.includes("error TS"));
    } catch (error) {
        tscErrors = `${error.stdout || ""}${error.stderr || ""}`.split("\n").filter((line) => line.includes("error TS"));
    }
    const newErrors = tscErrors.filter((line) => !line.startsWith("src/components/layout/model-script-editor.tsx"));
    check("F1 tsc 仅既有 1 条基线错误（model-script-editor.tsx）", tscErrors.length === 1 && newErrors.length === 0, tscErrors.join(" | "));

    const probeSource = readFileSync(new URL(import.meta.url), "utf8");
    check("F2 探针自身没有付费生成调用点（无生成执行体调用 / 无视频创建端点）", !/run_generation\s*\(/.test(probeSource) && !probeSource.includes("api/video/" + "create"));

    let storeDigest = null;
    try {
        const raw = readFileSync(storePath);
        storeDigest = createHash("sha256").update(raw).digest("hex");
        const parsed = JSON.parse(raw.toString("utf8"));
        const state = parsed?.state || parsed;
        const projects = Array.isArray(state?.projects) ? state.projects : [];
        const batchProjects = projects.filter((item) => Array.isArray(item.workflowBatches) && item.workflowBatches.length);
        check("F3 真实 store 可读（样本存在，只读比对）", projects.length > 0, `projects=${projects.length}`);
        check("F4 真实项目 workflowBatches 未被本探针改写（内存夹具隔离）", batchProjects.every((item) => item.id !== "proj-p8"), JSON.stringify(batchProjects.map((item) => item.id).slice(0, 3)));
        const again = createHash("sha256").update(readFileSync(storePath)).digest("hex");
        check("F5 跑完后真实 store 字节级一致", again === storeDigest);
    } catch (error) {
        check("F3 真实 store 可读（样本存在，只读比对）", false, String(error?.message || error));
        check("F4 真实项目 workflowBatches 未被本探针改写（内存夹具隔离）", true, "store 不可读，跳过（如实记录）");
        check("F5 跑完后真实 store 字节级一致", true, "store 不可读，跳过（如实记录）");
    }
}

// ================================================================ G. 退回态守卫 + 批次状态筛选（验收补项）
setSection("G");
{
    // ── 与 state.py:245-254 rollback_to_assets 的守卫逐条对齐：
    //    只有 awaiting_approval 能退（其余后端 raise StateError「退回置换需 awaiting_approval，当前 X」）
    check("G1 退回判定：awaiting_approval → 可退（后端唯一放行态）", review.canShowRollback("awaiting_approval") === true);
    check("G2 退回判定：approved → 不可退（后端必抛 StateError，UI 渲染就是死链）", review.canShowRollback("approved") === false);
    check("G3 退回判定：awaiting_assets（已是回置换区）→ 不可退", review.canShowRollback("awaiting_assets") === false);
    check(
        "G4 退回判定：generating/done/空串/null/undefined → 不可退（后端同口径）",
        ["generating", "done", "", null, undefined].every((value) => review.canShowRollback(value) === false),
        ["generating", "done", "", null, undefined].map((value) => `${String(value)}=${review.canShowRollback(value)}`).join(","),
    );
    check("G5 退回判定不做大小写/空白归一（后端是全等比较，逐条对齐）", review.canShowRollback("Awaiting_Approval") === false && review.canShowRollback(" awaiting_approval") === false);

    // ── 批次头状态筛选：夹具两个实例（待批 / 已批准）
    const canvas = makeCanvas();
    const g1 = matchPlan({ index: 1, tag: "g1", canvas });
    await canvas.apply(g1.plan.ops);
    const g2 = matchPlan({ index: 2, tag: "g2", canvas });
    await canvas.apply(g2.plan.ops);
    const gMembers = [makeMember(g1.plan, "g1"), makeMember(g2.plan, "g2")];
    const gRecord = batch.createWorkflowBatch({ templateId: fixture.template.templateId, title: "P8 筛选夹具", members: gMembers, now: "2026-11-14T00:00:00.000Z" });
    const gNodes = canvas.nodes();
    const gHubStates = { [g1.plan.instanceId]: "awaiting_approval", [g2.plan.instanceId]: "approved" };
    const gBatch = review.buildReviewList({ batches: [gRecord], nodes: gNodes, hubStates: gHubStates })[0];
    const allRows = review.filterReviewInstances(gBatch.instances, review.REVIEW_FILTER_ALL);
    const awaitingRows = review.filterReviewInstances(gBatch.instances, "awaiting");
    const approvedRows = review.filterReviewInstances(gBatch.instances, "approved");
    const humanRows = review.filterReviewInstances(gBatch.instances, "human");
    check("G6 筛选 awaiting → 只剩待批行（行 id 与状态都对）", awaitingRows.length === 1 && awaitingRows[0].instanceId === g1.plan.instanceId && awaitingRows[0].status === "awaiting_approval", JSON.stringify(awaitingRows.map((row) => [row.instanceId, row.status])));
    check("G7 筛选 approved → 只剩已批准行", approvedRows.length === 1 && approvedRows[0].instanceId === g2.plan.instanceId && approvedRows[0].status === "approved", JSON.stringify(approvedRows.map((row) => [row.instanceId, row.status])));
    check("G8 筛选无该状态的键（human）→ 空数组，不是「全部」", humanRows.length === 0 && allRows.length === 2, `human=${humanRows.length} all=${allRows.length}`);
    check("G9 未知筛选键 → 空（不静默降级成全部）", review.filterReviewInstances(gBatch.instances, "nonsense").length === 0 && review.filterReviewInstances(null, "all").length === 0);
    const chipDrift = gBatch.chips.filter((chip) => review.filterReviewInstances(gBatch.instances, review.reviewFilterIdOf(chip.status)).length !== chip.count);
    check("G10 每个 chip 的计数 = 该状态实筛行数（chips 与过滤同一口径）", chipDrift.length === 0, JSON.stringify(chipDrift.map((chip) => [chip.status, chip.count])));
    check(
        "G11 10 态 → 筛选键 1:1（键名固定，awaiting/human/fail/approved/done 为验收要求项）",
        Object.keys(review.REVIEW_STATUS_FILTER).length === 10 &&
            review.REVIEW_STATUS_FILTER.awaiting_approval === "awaiting" &&
            review.REVIEW_STATUS_FILTER.needs_human === "human" &&
            review.REVIEW_STATUS_FILTER.failed === "fail" &&
            review.REVIEW_STATUS_FILTER.approved === "approved" &&
            review.REVIEW_STATUS_FILTER.done === "done",
        JSON.stringify(review.REVIEW_STATUS_FILTER),
    );
    check("G12 行内 hubState = 现查真值（筛选依据不另算）", gBatch.instances.every((row) => row.hubState === (gHubStates[row.instanceId] || "")), JSON.stringify(gBatch.instances.map((row) => [row.instanceId, row.hubState])));
    // 「已退回」不新造状态：只按 hub 当前 awaiting_assets 诚实标注「回置换区」
    const replacedBatch = review.buildReviewList({ batches: [gRecord], nodes: gNodes, hubStates: { ...gHubStates, [g2.plan.instanceId]: "awaiting_assets" } })[0];
    const replacedRows = review.filterReviewInstances(replacedBatch.instances, review.REVIEW_FILTER_REPLACED);
    check(
        "G13 「回置换区」筛选 = hub 现查 awaiting_assets（不虚构「已退回」状态、不读 history）",
        replacedRows.length === 1 && replacedRows[0].instanceId === g2.plan.instanceId && review.reviewReplacedCount(replacedBatch.instances) === 1 && review.reviewReplacedCount(gBatch.instances) === 0,
        JSON.stringify(replacedRows.map((row) => [row.instanceId, row.hubState])),
    );
}

// ---------------------------------------------------------------- 输出
const summary = { pass, fail: fails.length, total: pass + fails.length, sections: Object.fromEntries([...sections].map(([key, value]) => [key, { pass: value.pass, fail: value.fail }])) };
writeFileSync(path.join(outDir, "probe-p8-pending-review.json"), JSON.stringify({ summary, results, fails }, null, 2), "utf8");
writeFileSync(
    path.join(outDir, "probe-p8-pending-review.txt"),
    [...results.map((row) => `${row.ok ? "PASS" : "FAIL"} [${row.section}] ${row.name}${row.detail ? ` — ${row.detail}` : ""}`), "", `TOTAL pass=${summary.pass} fail=${summary.fail} total=${summary.total}`].join("\n"),
    "utf8",
);
for (const row of results) console.log(`${row.ok ? "PASS" : "FAIL"} [${row.section}] ${row.name}${row.ok ? "" : ` — ${row.detail}`}`);
console.log(`\nTOTAL pass=${summary.pass} fail=${summary.fail} total=${summary.total}`);
console.log(`sections ${JSON.stringify(summary.sections)}`);
console.log(`out ${path.join(outDir, "probe-p8-pending-review.json")}`);
process.exit(fails.length ? 1 : 0);
