/**
 * P11 探针：画布内替换图 → 槽绑定（显式指定优先于自动匹配）。
 *
 * 跑法：cd web && node scripts/probe-p11-canvas-asset-bind.mjs [--out <目录>]
 *
 * 被测对象 = **生产同一份实现**：`web/src/lib/canvas/workflow-batch-entry.ts`
 * 的 P11 解析层（`resolveCanvasAssetBindings` / `canvasAssetCandidates` / `templateAssetSlots`）
 * 与三工具调用壳；引擎 P2/P3/P4/P5/P6/P7/P8 原样调用（零改动）。
 *
 * 分段（判据见卡面「探针验收」，先单后批）：
 *   A 单实例先行：画布「1 源视频 + 1 人物图 + 1 产品图」→ run_workflow_instance 单实例
 *            → 人物图绑人物槽、产品图绑主体槽（回执含绑定理由）→ 实例链路画布可见 → 停待批 → 侧栏可见
 *   B 显式优先：同槽语义下画布指定图 ≠ 自动匹配首选 → 以画布指定图为准；自动匹配只补没指定的槽；
 *            选中图（无槽指代）也优先于未选中的图
 *   C 留空交人：语义模糊图（判不准槽）→ 该槽留空 + 回执说明，不硬塞
 *   D 否决链回归：否决过的图不再被绑定（唯一候选 → 留空交人；另有替代 → 换图不回头）
 *   E 批次场景：多视频 + 同组替换图 → 各实例同绑，图副本逐实例独立
 *   F 付费/幂等/自检：付费视频创建恒 0、只打桥侧拉片链、同批重复调用幂等、tsc 基线、
 *            引擎文件零改动（git 对账）、探针只碰自建夹具
 *
 * 失败 rc=1（禁降级、禁跳过）。
 */
import { execFileSync } from "node:child_process";
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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p11-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p11");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
const results = [];
const sections = new Map();
let section = "A";
const setSection = (next) => {
    section = next;
};
function check(name, condition, detail = "") {
    const ok = Boolean(condition);
    results.push({ section, name, ok, detail: ok ? "" : String(detail) });
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
const modEntry = path.join(bundleDir, "p11-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as entry from "@/lib/canvas/workflow-batch-entry";',
        'export * as runtime from "@/lib/canvas/workflow-batch-runtime";',
        'export * as review from "@/lib/canvas/workflow-pending-review";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        'export * as matcher from "@/lib/canvas/workflow-asset-match";',
        'export * as ids from "@/lib/canvas/workflow-template-ids";',
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p11-modules.mjs")).href);
const { builtin, entry, runtime, review, batch, ops, matcher, ids } = m;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 夹具（「拆解→生成」链 + 两个资产位 + 一张骨架参考图）
const VIDEO = "video-src";
const LAPIAN = "lap-1";
const INS = "ins-1";
const INTENT = "it-1";
const CONFIG_TEXT = "cfg-1";
const FINAL = "fin-1";
const CONFIG_VIDEO = "cfgv-1";
const OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female";
const SLOT_PERFUME = "slot-perfume";
/** 模板骨架里的参考图（语义能命中人物槽）——必须作为「模板骨架」被排除，绝不当替换素材。 */
const REF_IMAGE = "ref-img";
const CHAIN_IDS = [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME, REF_IMAGE];

const node = (id, type, title, metadata, x = 0) => ({ id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata });
const imageNode = (id, title, key, x = 0) => node(id, "image", title, { content: `blob:${key}`, storageKey: `image:${key}`, mimeType: "image/png", naturalWidth: 1024, naturalHeight: 1024 }, x);

const chainNodes = () => [
    node(VIDEO, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4" }),
    node(LAPIAN, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }, 100),
    node(INS, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" }, 200),
    node(INTENT, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true }, 300),
    node(CONFIG_TEXT, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: LAPIAN, composerContent: `拉片：@[node:${LAPIAN}]\n指令：@[node:${INS}]\n意图：@[node:${INTENT}]` }, 400),
    node(FINAL, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 500),
    node(CONFIG_VIDEO, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: `提示词：@[node:${FINAL}]` }, 600),
    node(OUT_VIDEO, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle", videoTaskId: "task-out", videoProgress: 100 }, 700),
    node(SLOT_FEMALE, "image", "资产位·人物·女性A", { content: "blob:sf", storageKey: "image:sf", assetSlotSourceId: LAPIAN, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣", span: "00:00-02.00" } }, 800),
    node(SLOT_PERFUME, "image", "资产位·主体·香水", { content: "blob:sp", storageKey: "image:sp", assetSlotSourceId: LAPIAN, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体", span: "02.00-05.00" } }, 900),
    imageNode(REF_IMAGE, "女性-参考图.png", "ref", 1000),
];
const chainConnections = () => [
    { id: "e1", fromNodeId: VIDEO, toNodeId: LAPIAN },
    { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
    { id: "e3", fromNodeId: INS, toNodeId: CONFIG_TEXT },
    { id: "e4", fromNodeId: INTENT, toNodeId: CONFIG_TEXT },
    { id: "e5", fromNodeId: CONFIG_TEXT, toNodeId: FINAL },
    { id: "e6", fromNodeId: FINAL, toNodeId: CONFIG_VIDEO },
    { id: "e7", fromNodeId: CONFIG_VIDEO, toNodeId: OUT_VIDEO },
];
const materialNode = (id, key, index, title) => node(id, "video", title || `新素材-${index}.mp4`, { content: `blob:${key}`, storageKey: key, mimeType: "video/mp4", bytes: 4000 + index, durationMs: 9000 }, 2000 + index * 100);

/** 画布项目（每段夹具自建自清，不碰真实 store）。 */
let project;
let selection = [];
const resetProject = (nodes = chainNodes(), connections = chainConnections(), extra = {}) => {
    project = {
        id: "proj-p11",
        title: "P11 夹具项目",
        nodes: [...nodes],
        connections: [...connections],
        backgroundMode: "lines",
        viewport: { x: 0, y: 0, k: 1 },
        workflowTemplate: undefined,
        workflowBatches: [],
        workflowAssetVetoes: [],
        workflowWanFileTasks: [],
        ...extra,
    };
    selection = [];
};
resetProject();
const nodeById = (id) => (project.nodes || []).find((item) => item.id === id);

// ---------------------------------------------------------------- stub 依赖（零网络 / 零付费）
const flushes = [];
const logs = [];
const generationCalls = [];
const fetchCalls = [];
const hubCalls = { ingest: 0, upload: 0, getTask: 0 };
const identitiesSaved = [];
let uploadSeq = 0;
let generationSeq = 0;
const hubState = "awaiting_approval";

const applyOpsWithProject = async (batchOps) => {
    const next = ops.applyCanvasAgentOps(
        { projectId: project.id, title: project.title, nodes: project.nodes, connections: project.connections, selectedNodeIds: selection, viewport: { x: 0, y: 0, k: 1 } },
        batchOps,
    );
    project = { ...project, nodes: next.nodes, connections: next.connections };
};

const hub = {
    ingest: async () => {
        hubCalls.ingest += 1;
        return { localId: `p11-local-${hubCalls.ingest}`, bridgeId: "b11" };
    },
    uploadAsset: async () => {
        hubCalls.upload += 1;
        return { ref: `ref-${hubCalls.upload}` };
    },
    getTask: async () => {
        hubCalls.getTask += 1;
        return { state: hubState, bridgeId: "b11" };
    },
    saveRevision: async () => ({ state: hubState, approvalSnapshot: "snap-p11", promptEditedAt: "2026-11-14T00:00:09.000Z", selfcheck: [] }),
    approve: async () => ({}),
    rollback: async () => ({}),
    revoke: async () => ({}),
    waitForApproval: async () => "awaiting_approval",
};

const saveMember = (member) => {
    const list = project.workflowBatches || [];
    const owner = list.find((item) => (item.members || []).some((row) => row.instanceId === member.instanceId));
    if (!owner) return;
    project = { ...project, workflowBatches: batch.upsertBatchMember(list, { templateId: owner.templateId, batchId: owner.batchId, title: owner.title }, member) };
};

runtime.registerWorkflowBatchRuntime({
    applyOps: applyOpsWithProject,
    runGeneration: async ({ nodeId, mode, prompt }) => {
        generationCalls.push({ nodeId, mode, promptLength: String(prompt || "").length });
        generationSeq += 1;
        const childNodeId = `p11-out-${generationSeq}`;
        const content = `【本次产物 ${generationSeq}】正文`;
        await applyOpsWithProject([
            { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 5000 + generationSeq * 60, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
            { type: "connect_nodes", fromNodeId: nodeId, toNodeId: childNodeId },
        ]);
        return { childNodeId };
    },
    hub,
    getBlob: async (storageKey) => new Blob([`bytes-${storageKey}`], { type: "video/mp4" }),
    saveIdentity: (record) => void identitiesSaved.push(record),
    saveBatchMember: saveMember,
    saveBatchQueue: (batchId, next) => {
        project = { ...project, workflowBatches: (project.workflowBatches || []).map((item) => (item.batchId === batchId ? { ...item, queue: next } : item)) };
    },
    flush: (reason) => void flushes.push(reason),
    vetoes: () => project.workflowAssetVetoes || [],
    wanFileTasks: () => ({}),
    project: () => project,
    lapianConfig: { lapianDefaultModel: "", channels: [] },
    saveTemplate: (template) => {
        project = { ...project, workflowTemplate: template };
    },
    saveBatches: (batches) => {
        project = { ...project, workflowBatches: batches };
    },
    log: (message) => logs.push(message),
});

const entryDeps = (overrides = {}) => ({
    project: () => project,
    applyOps: applyOpsWithProject,
    getBlob: async (storageKey) => new Blob([`bytes-${storageKey}`], { type: "video/mp4" }),
    hub,
    lapianConfig: { lapianDefaultModel: "", channels: [] },
    vetoes: () => project.workflowAssetVetoes || [],
    identities: () => identitiesSaved,
    saveTemplate: (template) => {
        project = { ...project, workflowTemplate: template };
    },
    saveBatches: (batches) => {
        project = { ...project, workflowBatches: batches };
    },
    flush: (reason) => void flushes.push(reason),
    runBatch: (batchId) => runtime.startBatch(batchId),
    findNode: (nodeId) => nodeById(nodeId),
    selectedNodeIds: () => selection,
    log: (message) => logs.push(message),
    ...overrides,
});

// global fetch：只认桥侧拉片链（stub），其余一律记「未覆盖」并按失败处理（防真网络）
const realFetch = globalThis.fetch;
let unexpectedFetch = 0;
globalThis.fetch = async (url) => {
    const target = String(url);
    fetchCalls.push(target);
    const json = (payload, status = 200) => ({ ok: status < 400, status, json: async () => payload });
    if (target === "/api/video/upload") {
        uploadSeq += 1;
        return json({ id: `task-${uploadSeq}` });
    }
    if (target === "/api/lapian/run") return json({ job: `job-${uploadSeq}` });
    if (target.startsWith("/api/job/")) return json({ status: "done" });
    if (target.startsWith("/api/lapian/")) return json({ markdown: `# 本次拉片正文 ${target}\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近` });
    if (target.startsWith("/api/render/")) return json({ text: `# 投影正文 ${target}` });
    unexpectedFetch += 1;
    throw new Error(`stub 未覆盖的网络请求：${target}`);
};

// ---------------------------------------------------------------- 夹具工具
/** 铺一份具名模板（默认带骨架参考图）。 */
const seedTemplate = async ({ templateId, name = "视频拆解", nodes = chainNodes(), connections = chainConnections() } = {}) => {
    resetProject(nodes, connections);
    await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name, templateId }, entryDeps());
};

const slotId = (templateId, instanceIndex, templateSlotId) => ids.templateInstanceNodeId(templateId, instanceIndex, templateSlotId);
const copyId = (templateId, instanceIndex, canvasImageId) => ids.templateInstanceNodeId(templateId, instanceIndex, canvasImageId);
const imageOfCopy = (copyNodeId) => {
    const copy = nodeById(copyNodeId);
    return { copy, source: copy ? String(copy.metadata?.assetLibrarySourceId || "") : "" };
};

const bindingOf = (result, slotTemplateId) => result.assetBindings.find((row) => row.slotTemplateNodeId === slotTemplateId);

// ================================================================ A. 单实例先行（核心）
setSection("A");
{
    await seedTemplate({ templateId: "wp11a" });
    project = {
        ...project,
        nodes: [
            ...project.nodes,
            materialNode("m1", "video:m1", 1, "素材一.mp4"),
            imageNode("img-person", "人物-女性.png", "person", 3000),
            imageNode("img-product", "主体-香水.png", "product", 3100),
        ],
    };
    const generationBefore = generationCalls.length;
    const fetchBefore = fetchCalls.length;
    const nodesBefore = project.nodes.length;

    // 用户话：「用视频拆解工作流处理这个视频，人物和产品用画布上这两张图」→ Agent 只给素材，替换图由画布图自动解析
    const result = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" } }, entryDeps());

    const female = bindingOf(result, SLOT_FEMALE);
    const perfume = bindingOf(result, SLOT_PERFUME);
    check("A1 人物图绑人物槽：回执逐条给「图→槽」", female?.status === "bound" && female?.imageNodeId === "img-person" && female?.slotName === "女性A", JSON.stringify(female));
    check("A2 产品图绑主体槽：回执逐条给「图→槽」", perfume?.status === "bound" && perfume?.imageNodeId === "img-product" && perfume?.slotName === "香水", JSON.stringify(perfume));
    check(
        "A3 绑定理由可核对：命中词+分数+来源（按槽语义自动匹配）",
        female?.source === "canvas_semantic" && female?.matchedTerms.includes("女性") && female?.score >= 6 && /槽语义命中/.test(String(female?.reason)) && perfume?.matchedTerms.includes("香水"),
        JSON.stringify({ female: female && [female.source, female.matchedTerms, female.score, female.reason], perfume: perfume && [perfume.source, perfume.matchedTerms, perfume.score] }),
    );
    check("A4 回执摘要写清绑定几个槽（含理由）", /画布替换图绑定 2\/2 个槽/.test(String(result.assetBindingSummary)) && String(result.assetBindingSummary).includes("人物-女性.png"), String(result.assetBindingSummary));

    // 绑定落地 = 实例挂图副本（P4 语义：槽 attachedImageId → 副本 → 溯源 assetLibrarySourceId）
    const femaleSlotNode = nodeById(slotId("wp11a", 1, SLOT_FEMALE));
    const perfumeSlotNode = nodeById(slotId("wp11a", 1, SLOT_PERFUME));
    const femaleCopy = imageOfCopy(copyId("wp11a", 1, "img-person"));
    const perfumeCopy = imageOfCopy(copyId("wp11a", 1, "img-product"));
    check(
        "A5 绑图落到实例槽：槽上 attachedImageId = 本实例图副本",
        String(femaleSlotNode?.metadata?.attachedImageId || "") === copyId("wp11a", 1, "img-person") && String(perfumeSlotNode?.metadata?.attachedImageId || "") === copyId("wp11a", 1, "img-product"),
        JSON.stringify({ female: femaleSlotNode?.metadata?.attachedImageId, perfume: perfumeSlotNode?.metadata?.attachedImageId }),
    );
    check(
        "A6 图副本可溯源：副本 metadata.assetLibrarySourceId = 画布原图（否决/复核判据）",
        femaleCopy.source === "img-person" && perfumeCopy.source === "img-product" && Boolean(femaleCopy.copy?.metadata?.storageKey),
        JSON.stringify({ female: femaleCopy.source, perfume: perfumeCopy.source }),
    );
    const femaleGraft = project.nodes.find((item) => String(item.metadata?.assetGraftForSlotId || "") === slotId("wp11a", 1, SLOT_FEMALE));
    check("A7 挂图对照节点（P4 语义）也在画布上，且指向本次挂的图", Boolean(femaleGraft) && String(femaleGraft?.metadata?.assetGraftOfImageId || "") === copyId("wp11a", 1, "img-person"), JSON.stringify(femaleGraft && [femaleGraft.id, femaleGraft.metadata?.assetGraftOfImageId]));

    check(
        "A8 回执每实例列出挂图（槽←副本←原图），可逐实例核对",
        result.instances.length === 1 && (result.instances[0].assetAttaches || []).length === 2 && (result.instances[0].assetAttaches || []).every((row) => ["img-person", "img-product"].includes(String(row.sourceImageNodeId))),
        JSON.stringify(result.instances[0]?.assetAttaches),
    );
    check(
        "A9 实例链路画布可见 + 停待批（终点=待审批，P4 判据）",
        project.nodes.length > nodesBefore && result.instances[0].status === "awaiting_approval" && result.instances[0].outcome === "awaiting_approval",
        JSON.stringify({ nodesBefore, nodesAfter: project.nodes.length, status: result.instances[0].status, outcome: result.instances[0].outcome }),
    );

    // 侧栏（P8 同一份数据源）：真调产出的批次 → 1 行待审批 + 挂图位/理由/缩略
    const batchRecord = batch.findWorkflowBatch(project.workflowBatches, result.batchId);
    const hubStates = {};
    for (const member of batchRecord?.members || []) hubStates[member.instanceId] = member.hub?.localId ? hubState : undefined;
    const rows = review.buildReviewList({ batches: [batchRecord], nodes: project.nodes, hubStates, templateNameOf: () => project.workflowTemplate?.name });
    check("A10 侧栏「待审批」立即出 1 行且待审批（实例链路可见）", rows.length === 1 && rows[0].instances.length === 1 && rows[0].instances[0].status === "awaiting_approval", JSON.stringify(rows.map((row) => [row.templateName, row.instances.map((item) => item.statusLabel)])));
    const slotRows = review.buildSlotRows({ member: batchRecord.members[0], nodes: project.nodes });
    check(
        "A11 侧栏挂图位读到的就是本次绑定（当前挂图=副本、溯源=原图、复算一致）",
        slotRows.length === 2 &&
            slotRows.every((row) => Boolean(row.currentImageId) && row.currentLibraryImageId && row.pickedMatchesAttached) &&
            slotRows.some((row) => row.currentLibraryImageId === "img-person") &&
            slotRows.some((row) => row.currentLibraryImageId === "img-product") &&
            slotRows.every((row) => Boolean(row.reason?.note)),
        JSON.stringify(slotRows.map((row) => [row.slotKey, row.currentImageId, row.currentLibraryImageId, row.pickedMatchesAttached, row.reason?.note])),
    );

    // 候选池与排除清单：模板骨架里的参考图（语义命中人物槽）绝不能被当替换素材
    check(
        "A12 模板骨架图不进候选池（语义能命中也排除，带理由）",
        !(result.assetCandidates || []).some((row) => row.imageNodeId === REF_IMAGE) &&
            (result.assetCandidateExclusions || []).some((row) => row.nodeId === REF_IMAGE && /模板骨架/.test(row.reason)),
        JSON.stringify({ candidates: result.assetCandidates, exclusions: (result.assetCandidateExclusions || []).filter((row) => row.nodeId === REF_IMAGE) }),
    );
    check(
        "A13 资产位节点/其它非素材图不进候选池（槽不是图，图副本不是素材）",
        !(result.assetCandidates || []).some((row) => [SLOT_FEMALE, SLOT_PERFUME].includes(row.imageNodeId)) &&
            (result.assetCandidateExclusions || []).some((row) => row.nodeId === SLOT_FEMALE && /资产位节点本身/.test(row.reason)),
        JSON.stringify((result.assetCandidateExclusions || []).slice(0, 6)),
    );

    check("A14 付费口径：本工具内视频创建恒 0（paidVideoCreations=0，runGeneration 只 mode:text）", result.paidVideoCreations === 0 && generationCalls.slice(generationBefore).every((call) => call.mode === "text"), JSON.stringify(generationCalls.slice(generationBefore)));
    check("A15 零生成端点：整段只打桥侧拉片链，无 create 类请求", fetchCalls.slice(fetchBefore).every((url) => /^\/api\/(video\/upload|lapian\/(run|[^/]+)|job\/[^/]+|render\/)/.test(url)) && unexpectedFetch === 0, JSON.stringify([...new Set(fetchCalls.slice(fetchBefore))]));

    // 交卷证据：把**生产代码真跑**出来的实例（含挂图副本/挂图对照/批次记录）落成 JSON，供 ego DOM 探针原样 seed
    writeFileSync(
        path.join(outDir, "p11-single-instance.json"),
        JSON.stringify(
            {
                batchId: result.batchId,
                receipt: { assetBindings: result.assetBindings, assetBindingSummary: result.assetBindingSummary, instances: result.instances },
                slotRows: slotRows.map((row) => ({ slotKey: row.slotKey, currentLibraryImageId: row.currentLibraryImageId, pickedMatchesAttached: row.pickedMatchesAttached, note: row.reason?.note })),
                project: { nodes: project.nodes, connections: project.connections, workflowTemplate: project.workflowTemplate, workflowBatches: project.workflowBatches, workflowAssetVetoes: project.workflowAssetVetoes },
            },
            null,
            2,
        ),
        "utf8",
    );
}

// ================================================================ B. 显式优先
setSection("B");
{
    // 同槽语义下两张都能命中「女性」槽：A 是自动首选（标题序在前），B 是用户指定的那张
    const nodesWithTwo = () => [
        ...chainNodes(),
        materialNode("m1", "video:m1", 1, "素材一.mp4"),
        imageNode("img-person-a", "人物-女性-A.png", "personA", 3000),
        imageNode("img-person-b", "人物-女性-B.png", "personB", 3100),
        imageNode("img-product", "主体-香水.png", "product", 3200),
    ];

    // B0 基线：同夹具下不指定 → 自动首选（用来证明「指定 ≠ 自动首选」，否则 B1 断言不成立）
    await seedTemplate({ templateId: "wp11b0", nodes: nodesWithTwo() });
    const autoPlan = entry.resolveCanvasAssetBindings({ project, template: project.workflowTemplate, templateNodes: entry.templatePlanInput(project, project.workflowTemplate).nodes, instanceIndex: 1, selectedNodeIds: [] });
    const autoFemale = autoPlan.rows.find((row) => row.slotTemplateNodeId === SLOT_FEMALE);
    const autoPick = String(autoFemale?.imageNodeId || "");
    check("B0 基线：不指定时自动匹配首选是 A（标题序在前）", autoPick === "img-person-a" && autoFemale?.source === "canvas_semantic", JSON.stringify(autoFemale));

    // B1 显式指定 → 以画布指定图为准（哪怕自动首选是另一张）
    await seedTemplate({ templateId: "wp11b1", nodes: nodesWithTwo() });
    const templatePlan = entry.templatePlanInput(project, project.workflowTemplate);
    const explicitPlan = entry.resolveCanvasAssetBindings({
        project,
        template: project.workflowTemplate,
        templateNodes: templatePlan.nodes,
        instanceIndex: 1,
        selectedNodeIds: [],
        bindings: [{ slotName: "女性A", imageNodeId: "img-person-b", reason: "人物换成这张" }],
    });
    const explicitFemale = explicitPlan.rows.find((row) => row.slotTemplateNodeId === SLOT_FEMALE);
    check(
        "B1 显式优先：同槽语义下画布指定图 ≠ 自动首选 → 以指定为准（并给原话与替代候选）",
        explicitFemale?.imageNodeId === "img-person-b" && explicitFemale?.source === "owner_specified" && /人物换成这张/.test(String(explicitFemale?.reason)) && (explicitFemale?.alternatives || []).some((item) => item.imageNodeId === "img-person-a"),
        JSON.stringify(explicitFemale),
    );
    check("B2 自动匹配只补没指定的槽（产品槽照常自动绑）", explicitPlan.rows.find((row) => row.slotTemplateNodeId === SLOT_PERFUME)?.imageNodeId === "img-product" && explicitPlan.rows.find((row) => row.slotTemplateNodeId === SLOT_PERFUME)?.source === "canvas_semantic", JSON.stringify(explicitPlan.rows));

    // B3 真的跑一遍：挂图副本溯源 = 指定节点（不是自动首选）
    const explicitRun = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" }, assetBindings: [{ slotName: "女性A", imageNodeId: "img-person-b", reason: "人物换成这张" }] }, entryDeps());
    const bFemaleCopy = imageOfCopy(copyId("wp11b1", 1, "img-person-b"));
    check(
        "B3 落地断言：挂图副本来源 = 画布指定节点（B），自动首选节点（A）没被绑",
        String(nodeById(slotId("wp11b1", 1, SLOT_FEMALE))?.metadata?.attachedImageId || "") === copyId("wp11b1", 1, "img-person-b") &&
            bFemaleCopy.source === "img-person-b" &&
            !project.nodes.some((item) => String(item.metadata?.assetLibrarySourceId || "") === "img-person-a" && String(item.metadata?.attachedImageId || "")),
        JSON.stringify({ attached: nodeById(slotId("wp11b1", 1, SLOT_FEMALE))?.metadata?.attachedImageId, copySource: bFemaleCopy.source }),
    );
    check("B4 显式优先在回执里标明来源（你指定）", bindingOf(explicitRun, SLOT_FEMALE)?.source === "owner_specified", JSON.stringify(bindingOf(explicitRun, SLOT_FEMALE)));

    // B5 选中图（无槽指代）也优先：选中 B 时即便 A 是标题序首选，也绑 B
    await seedTemplate({ templateId: "wp11b5", nodes: nodesWithTwo() });
    selection = ["img-person-b"];
    const selectedRun = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" } }, entryDeps());
    check(
        "B5 选中图优先于未选中的图（槽指代缺省时按语义绑选中的那张）",
        bindingOf(selectedRun, SLOT_FEMALE)?.imageNodeId === "img-person-b" && bindingOf(selectedRun, SLOT_FEMALE)?.source === "canvas_selection" && imageOfCopy(copyId("wp11b5", 1, "img-person-b")).source === "img-person-b",
        JSON.stringify(bindingOf(selectedRun, SLOT_FEMALE)),
    );
    selection = [];
}

// ================================================================ C. 留空交人
setSection("C");
{
    await seedTemplate({ templateId: "wp11c" });
    project = {
        ...project,
        nodes: [...project.nodes, materialNode("m1", "video:m1", 1, "素材一.mp4"), imageNode("img-person", "人物-女性.png", "person", 3000), imageNode("img-unknown", "IMG_2041.png", "unknown", 3100)],
    };
    const result = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" } }, entryDeps());
    const perfume = bindingOf(result, SLOT_PERFUME);
    check(
        "C1 语义模糊图判不准 → 该槽留空 + 回执说明（不硬塞）",
        perfume?.status === "empty" && !perfume?.imageNodeId && /留空交人|不硬塞/.test(String(perfume?.reason)) && perfume?.source === "none",
        JSON.stringify(perfume),
    );
    check("C2 留空的槽真的没挂图（槽上无 attachedImageId、无挂图对照节点）", !String(nodeById(slotId("wp11c", 1, SLOT_PERFUME))?.metadata?.attachedImageId || "") && !project.nodes.some((item) => String(item.metadata?.assetGraftForSlotId || "") === slotId("wp11c", 1, SLOT_PERFUME)), JSON.stringify(nodeById(slotId("wp11c", 1, SLOT_PERFUME))?.metadata));
    check("C3 另一槽照常绑定（留空只影响判不准的那个槽）", bindingOf(result, SLOT_FEMALE)?.imageNodeId === "img-person" && (result.instances[0].assetAttaches || []).length === 1, JSON.stringify({ female: bindingOf(result, SLOT_FEMALE), attaches: result.instances[0].assetAttaches }));
    check("C4 摘要如实写「留空交人」并说明原因", /留空交人/.test(String(result.assetBindingSummary)) && /没有匹配这张槽语义的图/.test(String(perfume?.reason)), String(result.assetBindingSummary));
}

// ================================================================ D. 否决链回归（P7 口径）
setSection("D");
{
    // D1 否决唯一候选 → 该槽留空（否决不复用，不硬挂）
    await seedTemplate({ templateId: "wp11d" });
    project = {
        ...project,
        nodes: [...project.nodes, materialNode("m1", "video:m1", 1, "素材一.mp4"), imageNode("img-person", "人物-女性.png", "person", 3000), imageNode("img-product", "主体-香水.png", "product", 3100)],
        workflowAssetVetoes: [
            { schemaVersion: 1, instanceId: "wp11d#1", templateId: "wp11d", instanceIndex: 1, slotNodeId: slotId("wp11d", 1, SLOT_FEMALE), slotKey: "人物|女性A", rejectedImageId: "img-person", rejectedImageTitle: "人物-女性.png", vetoedAt: "2026-11-14T00:00:00.000Z" },
        ],
    };
    const vetoedRun = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" } }, entryDeps());
    const vetoedFemale = bindingOf(vetoedRun, SLOT_FEMALE);
    check(
        "D1 否决过的图不再被绑定（该槽留空交人）",
        vetoedFemale?.status === "empty" && !vetoedFemale?.imageNodeId && !project.nodes.some((item) => String(item.metadata?.assetLibrarySourceId || "") === "img-person" && String(item.metadata?.attachedImageId || "")),
        JSON.stringify(vetoedFemale),
    );
    check("D2 否决的可见痕迹在回执里（否决/留空原因可查）", /否决/.test(String(vetoedFemale?.reason)) && vetoedRun.warnings.some((line) => /否决/.test(line)), JSON.stringify({ reason: vetoedFemale?.reason, warnings: vetoedRun.warnings }));

    // D3 有替代候选时：否决的图不复用，换另一张（不回头）
    await seedTemplate({ templateId: "wp11d3" });
    project = {
        ...project,
        nodes: [...project.nodes, materialNode("m1", "video:m1", 1, "素材一.mp4"), imageNode("img-person-a", "人物-女性-A.png", "personA", 3000), imageNode("img-person-b", "人物-女性-B.png", "personB", 3100)],
        workflowAssetVetoes: [
            { schemaVersion: 1, instanceId: "wp11d3#1", templateId: "wp11d3", instanceIndex: 1, slotNodeId: slotId("wp11d3", 1, SLOT_FEMALE), slotKey: "人物|女性A", rejectedImageId: "img-person-a", rejectedImageTitle: "人物-女性-A.png", vetoedAt: "2026-11-14T00:00:00.000Z" },
        ],
    };
    const templatePlanD3 = entry.templatePlanInput(project, project.workflowTemplate);
    const afterVeto = entry.resolveCanvasAssetBindings({ project, template: project.workflowTemplate, templateNodes: templatePlanD3.nodes, instanceIndex: 1, selectedNodeIds: [], vetoes: project.workflowAssetVetoes });
    const d3Female = afterVeto.rows.find((row) => row.slotTemplateNodeId === SLOT_FEMALE);
    check("D3 否决后换替代图（否决图零出现）", d3Female?.imageNodeId === "img-person-b" && d3Female?.imageNodeId !== "img-person-a", JSON.stringify(d3Female));
}

// ================================================================ E. 批次场景（多视频 + 同组替换图）
setSection("E");
{
    await seedTemplate({ templateId: "wp11e" });
    project = {
        ...project,
        nodes: [
            ...project.nodes,
            materialNode("m1", "video:m1", 1, "素材一.mp4"),
            materialNode("m2", "video:m2", 2, "素材二.mp4"),
            materialNode("m3", "video:m3", 3, "素材三.mp4"),
            imageNode("img-person", "人物-女性.png", "person", 3000),
            imageNode("img-product", "主体-香水.png", "product", 3100),
        ],
    };
    const result = await entry.runWorkflowBatchTool({}, entryDeps());
    const copies = [1, 2, 3].map((index) => ({
        person: copyId("wp11e", index, "img-person"),
        product: copyId("wp11e", index, "img-product"),
    }));
    check(
        "E1 多视频 + 同组替换图 → 各实例同绑（每实例两条挂图）",
        result.instances.length === 3 && result.instances.every((row) => (row.assetAttaches || []).length === 2 && (row.assetAttaches || []).every((attach) => ["img-person", "img-product"].includes(String(attach.sourceImageNodeId)))),
        JSON.stringify(result.instances.map((row) => [row.instanceId, (row.assetAttaches || []).map((item) => [item.slotNodeId, item.sourceImageNodeId])])),
    );
    check(
        "E2 图副本逐实例独立：三实例的副本 id 互不相同、槽上都挂自己那份",
        [1, 2, 3].every((index) => String(nodeById(slotId("wp11e", index, SLOT_FEMALE))?.metadata?.attachedImageId || "") === copies[index - 1].person && String(nodeById(slotId("wp11e", index, SLOT_PERFUME))?.metadata?.attachedImageId || "") === copies[index - 1].product) &&
            new Set(copies.map((item) => item.person)).size === 3,
        JSON.stringify(copies),
    );
    check(
        "E3 三份副本都溯源同一张画布原图（同组替换图，一份原图三份副本）",
        copies.every((item) => imageOfCopy(item.person).source === "img-person" && imageOfCopy(item.product).source === "img-product"),
        JSON.stringify(copies.map((item) => [imageOfCopy(item.person).source, imageOfCopy(item.product).source])),
    );
    check("E4 批次回执给绑定摘要（逐槽写明图与来源，可核对）", /画布替换图绑定 2\/2 个槽/.test(String(result.assetBindingSummary)) && String(result.assetBindingSummary).includes("人物-女性.png") && String(result.assetBindingSummary).includes("主体-香水.png"), String(result.assetBindingSummary));
    check("E5 3 实例全部到待审批（终点不变）", result.instances.every((row) => row.status === "awaiting_approval"), JSON.stringify(result.instances.map((row) => row.status)));

    // ---------------------------------------------------------- F. 幂等 / 付费 / 自检
    setSection("F");
    const generationBefore = generationCalls.length;
    const nodesBefore = project.nodes.length;
    const again = await entry.runWorkflowBatchTool({}, entryDeps());
    check("F1 幂等：同一句再说一次 → 同批次、3 实例 already_present", again.batchId === result.batchId && again.instances.length === 3 && again.instances.every((row) => row.materialized === "already_present"), JSON.stringify({ batchId: [result.batchId, again.batchId], rows: again.instances.map((row) => row.materialized) }));
    check("F2 幂等零副作用：零新增组装、零新增节点", generationCalls.length === generationBefore && project.nodes.length === nodesBefore, JSON.stringify({ generation: generationCalls.length - generationBefore, nodes: project.nodes.length - nodesBefore }));
    check(
        "F3 续跑回执如实报既有挂图（不冒充本次重新绑定）",
        again.assetBindings.length === 2 && again.assetBindings.every((row) => row.status === "bound" && row.source === "existing" && ["img-person", "img-product"].includes(String(row.imageNodeId))) && /既有实例挂图/.test(String(again.assetBindingSummary)),
        JSON.stringify({ rows: again.assetBindings, summary: again.assetBindingSummary }),
    );
    check("F4 付费口径：批次内视频创建恒 0", result.paidVideoCreations === 0 && again.paidVideoCreations === 0 && generationCalls.every((call) => call.mode === "text"), JSON.stringify(generationCalls.slice(-5)));
    check("F5 零生成端点（整段只打桥侧拉片链）", unexpectedFetch === 0 && fetchCalls.every((url) => /^\/api\/(video\/upload|lapian\/(run|[^/]+)|job\/[^/]+|render\/)/.test(url)), JSON.stringify([...new Set(fetchCalls)]));

    let tscErrors = [];
    try {
        const output = execFileSync(process.execPath, [path.resolve("node_modules/typescript/bin/tsc"), "-p", "tsconfig.json", "--noEmit"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
        tscErrors = output.split("\n").filter((line) => line.includes("error TS"));
    } catch (error) {
        tscErrors = `${error.stdout || ""}${error.stderr || ""}`.split("\n").filter((line) => line.includes("error TS"));
    }
    const newErrors = tscErrors.filter((line) => !line.startsWith("src/components/layout/model-script-editor.tsx"));
    check("F6 tsc 仅既有 1 条基线错误（model-script-editor.tsx）", tscErrors.length === 1 && newErrors.length === 0, tscErrors.join(" | "));

    // 引擎文件零改动（git 对账：工作区 + 本提交改动都不许碰引擎与匹配器）
    const ENGINE_FILES = [
        "web/src/lib/canvas/workflow-template.ts",
        "web/src/lib/canvas/workflow-template-ids.ts",
        "web/src/lib/canvas/workflow-instance.ts",
        "web/src/lib/canvas/workflow-instance-run.ts",
        "web/src/lib/canvas/workflow-batch.ts",
        "web/src/lib/canvas/workflow-batch-queue.ts",
        "web/src/lib/canvas/workflow-batch-runtime.ts",
        "web/src/lib/canvas/workflow-batch-status.ts",
        "web/src/lib/canvas/workflow-asset-match.ts",
        "web/src/lib/canvas/lapian-assets.ts",
    ];
    const repoRoot = path.resolve("..");
    let touched = [];
    try {
        touched = execFileSync("git", ["-C", repoRoot, "status", "--porcelain"], { encoding: "utf8" })
            .split("\n")
            .map((line) => line.slice(3).trim())
            .filter(Boolean);
    } catch (error) {
        touched = [`git 失败：${error.message}`];
    }
    const engineTouched = touched.filter((file) => ENGINE_FILES.some((engine) => file.replace(/\\/g, "/") === engine));
    check("F7 引擎文件零改动（工作区改动里没有引擎/匹配器文件）", engineTouched.length === 0, JSON.stringify({ engineTouched, touched: touched.slice(0, 20) }));

    const probeSource = readFileSync(new URL(import.meta.url), "utf8");
    check("F8 探针自身没有付费生成调用点（无 run_generation 执行体 / 无视频创建端点）", !/run_generation\s*\(/.test(probeSource) && !probeSource.includes("api/video/" + "create"));
    check("F9 真实任务零接触：只操作自建夹具项目（proj-p11）", project.id === "proj-p11" && result.batchId.startsWith("batch-"));
}

// ================================================================ 汇总
setSection("汇总");
const summary = {
    at: new Date().toISOString(),
    pass,
    fail: fails.length,
    sections: Object.fromEntries(sections),
    fails,
    results,
    evidence: {
        generationCalls: generationCalls.map((call) => [call.nodeId, call.mode]),
        fetchTargets: [...new Set(fetchCalls)],
        hubCalls,
        flushes: [...new Set(flushes)],
        identitySaves: identitiesSaved.length,
        unexpectedFetch,
    },
};
writeFileSync(path.join(outDir, "p11-probe-results.json"), JSON.stringify(summary, null, 2), "utf8");
console.log(`\nP11 探针：${pass} 通过 / ${fails.length} 失败（证据 ${path.join(outDir, "p11-probe-results.json")}）`);
for (const [name, row] of sections) console.log(`  ${name}: ${row.pass} 通过 / ${row.fail} 失败`);
if (fails.length) {
    console.log("\n失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
globalThis.fetch = realFetch;
process.exit(fails.length ? 1 : 0);
