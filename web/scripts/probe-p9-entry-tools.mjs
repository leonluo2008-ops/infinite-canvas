/**
 * P9 探针（node 层，esbuild 打包 + stub deps 注入，零网络、零付费）。
 *
 * 跑法：cd web && node scripts/probe-p9-entry-tools.mjs [--out <目录>]
 *
 * 被测对象 = **生产同一份实现**：`web/src/lib/canvas/workflow-batch-entry.ts`
 * （三个画布 Agent 工具的调用壳），引擎 P2/P3/P4/P5/P6/P8 全部原样调用。
 *
 * 分段（判据见卡面「探针验收」）：
 *   A 抽模板：显式节点边界 → 五槽声明/冻结项/零图外引用；缺省边界 → 图外引用如实报告且标记「不冻结」；
 *             模板落盘到项目 workflowTemplate；空画布 → 人话拒绝
 *   B 批次：3 素材 → 建批次 → 3 实例全部到待审批；队列并发 3；侧栏 buildReviewList 出 3 行；
 *           付费动作提取 = 0（runGeneration 只 mode:"text"，零视频创建端点）
 *   C 单实例：单素材跑到待审批；同素材重复调用幂等（同实例 already_present、零新增生成、零冲突）；
 *           换素材撞同号实例 → 如实拒绝、绝不覆盖
 *   D 不冻结模板：建批次被拒绝（P2 契约「图外引用 → 实例化器须拒绝或补槽」）
 *   E 零回归：tsc 基线仍只有 1 条既有错误；探针自身无付费生成调用点；跨画布模板 id 被拒绝
 *
 * 失败 rc=1（禁降级、禁跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p9-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p9");
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
        // P12：内置模板经 lapian-prompt-templates 取默认指令正文（`...camera-contract.md?raw`）——
        // 探针要按 vite 的 `?raw` 语义把它当文本内联，否则打包失败
        loader: { ".md": "text" },
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    return outFile;
}

// ---------------------------------------------------------------- 模块装载（生产同一份实现）
const modEntry = path.join(bundleDir, "p9-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as entry from "@/lib/canvas/workflow-batch-entry";',
        'export * as runtime from "@/lib/canvas/workflow-batch-runtime";',
        'export * as review from "@/lib/canvas/workflow-pending-review";',
        'export * as workflow from "@/lib/canvas/workflow-template";',
        'export * as instancer from "@/lib/canvas/workflow-instance";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as batchStatus from "@/lib/canvas/workflow-batch-status";',
        'export * as queue from "@/lib/canvas/workflow-batch-queue";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p9-modules.mjs")).href);
const { builtin, entry, runtime, review, workflow, instancer, batch, batchStatus, queue, ops } = m;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 夹具（与 P8 同构的「拆解→生成」链）
const VIDEO = "video-src", LAPIAN = "lap-1", INS = "ins-1", INTENT = "it-1", CONFIG_TEXT = "cfg-1", FINAL = "fin-1", CONFIG_VIDEO = "cfgv-1", OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female", SLOT_PERFUME = "slot-perfume";
const CHAIN_IDS = [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME];

const node = (id, type, title, metadata, x = 0) => ({ id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata });
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
const materialNode = (id, key, index) => node(id, "video", `新素材-${index}.mp4`, { content: `blob:${key}`, storageKey: key, mimeType: "video/mp4", bytes: 4000 + index, durationMs: 9000 }, 2000 + index * 100);

/** 画布项目（可重置：每段夹具自建自清，不碰真实 store）。 */
let project;
const resetProject = (nodes = chainNodes(), connections = chainConnections()) => {
    project = {
        id: "proj-p9",
        title: "P9 夹具项目",
        nodes: [...nodes],
        connections: [...connections],
        backgroundMode: "lines",
        viewport: { x: 0, y: 0, k: 1 },
        workflowTemplate: undefined,
        workflowBatches: [],
        workflowAssetVetoes: [],
        workflowWanFileTasks: [],
    };
};
resetProject();

// ---------------------------------------------------------------- stub 依赖（零网络 / 零付费）
const flushes = [];
const logs = [];
const generationCalls = [];
const fetchCalls = [];
const hubCalls = { ingest: 0, upload: 0, getTask: 0 };
const identitySaves = [];
const identitiesSaved = [];
let uploadSeq = 0;
let generationSeq = 0;
let hubState = "awaiting_approval";

const applyOpsWithProject = async (batchOps) => {
    const next = ops.applyCanvasAgentOps(
        { projectId: project.id, title: project.title, nodes: project.nodes, connections: project.connections, selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } },
        batchOps,
    );
    project = { ...project, nodes: next.nodes, connections: next.connections };
};

const hub = {
    ingest: async () => {
        hubCalls.ingest += 1;
        const localId = `p9-local-${hubCalls.ingest}`;
        return { localId, bridgeId: "b1" };
    },
    uploadAsset: async () => {
        hubCalls.upload += 1;
        return { ref: `ref-${hubCalls.upload}` };
    },
    getTask: async () => {
        hubCalls.getTask += 1;
        return { state: hubState, bridgeId: "b1" };
    },
    saveRevision: async () => ({ state: hubState, approvalSnapshot: "snap-p9", promptEditedAt: "2026-11-14T00:00:09.000Z", selfcheck: [] }),
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

const runtimeDeps = () => ({
    applyOps: applyOpsWithProject,
    runGeneration: async ({ nodeId, mode, prompt }) => {
        generationCalls.push({ nodeId, mode, promptLength: String(prompt || "").length });
        generationSeq += 1;
        const childNodeId = `p9-out-${generationSeq}`;
        const content = `【本次产物 ${generationSeq}】正文`;
        await applyOpsWithProject([
            { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 5000 + generationSeq * 60, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
            { type: "connect_nodes", fromNodeId: nodeId, toNodeId: childNodeId },
        ]);
        return { childNodeId };
    },
    hub,
    getBlob: async (storageKey) => new Blob([`bytes-${storageKey}`], { type: "video/mp4" }),
    saveIdentity: (record) => {
        identitySaves.push(record);
        identitiesSaved.push(record);
    },
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

runtime.registerWorkflowBatchRuntime(runtimeDeps());

const entryDeps = () => ({
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
    findNode: (nodeId) => (project.nodes || []).find((item) => item.id === nodeId),
    log: (message) => logs.push(message),
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

// ================================================================ A. 抽模板
setSection("A");
{
    const emptyResult = await entry
        .extractWorkflowTemplateTool({ sourceNodeIds: [] }, { ...entryDeps(), project: () => ({ ...project, nodes: [], connections: [] }) })
        .then(() => null)
        .catch((error) => error);
    check("A1 空画布 → 人话拒绝（不抛未捕获异常、不建空模板）", emptyResult instanceof Error && /没有可抽象成模板的节点/.test(emptyResult.message), String(emptyResult && emptyResult.message));

    // 缺省边界（全项目可抽象子图）：生成产物/资产位不进默认子图 → 图外引用如实报告 + 不冻结
    const fallback = await entry.extractWorkflowTemplateTool({ name: "P9 缺省边界", templateId: "p9default" }, entryDeps());
    const refTargets = fallback.externalReferences.map((reference) => reference.targetId);
    check("A2 缺省边界：返回模板 id 与五槽声明", fallback.templateId === "p9default" && fallback.slots.length === 5, JSON.stringify({ id: fallback.templateId, slots: fallback.slots.length }));
    check("A3 缺省边界：图外引用逐条如实返回（不静默丢边）", fallback.externalReferences.length >= 1 && refTargets.includes(OUT_VIDEO), JSON.stringify(fallback.externalReferences));
    check("A4 缺省边界：frozenOk=false 且警告写明「不冻结」", fallback.frozenOk === false && fallback.warnings.some((line) => line.includes("不冻结")), JSON.stringify(fallback.warnings));
    check("A5 缺省边界：模板已落盘（项目 workflowTemplate 指向 p9default）", project.workflowTemplate?.templateId === "p9default" && flushes.some((reason) => reason === "p9-extract-template"), String(project.workflowTemplate?.templateId));

    // 显式边界（整链）：零图外引用 + 冻结项齐
    const explicit = await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: "P9 夹具模板", templateId: "p9demo" }, entryDeps());
    check("A6 显式边界：零图外引用 + frozenOk=true", explicit.externalReferences.length === 0 && explicit.frozenOk === true, JSON.stringify(explicit.externalReferences));
    check("A7 显式边界：五槽 target 落位（①源视频/②意图/③资产位/④生成参数/⑤可选段）", (() => {
        const byKind = new Map(explicit.slots.map((slot) => [slot.kind, slot.targets]));
        return (
            byKind.get("sourceVideo")?.includes(VIDEO) &&
            byKind.get("intentText")?.includes(INTENT) &&
            byKind.get("assetAttach")?.includes(SLOT_FEMALE) &&
            byKind.get("generationParams")?.includes(CONFIG_TEXT) &&
            byKind.get("optionalSegments")?.includes(LAPIAN)
        );
    })(), JSON.stringify(explicit.slots));
    check("A8 显式边界：冻结项摘要可读（拓扑 digest/边数/指令数/配置模式）", Boolean(explicit.frozen.topologyDigest) && explicit.frozen.nodeCount === CHAIN_IDS.length && explicit.frozen.edgeCount === 7 && explicit.frozen.generationModes.length === 2, JSON.stringify(explicit.frozen));
    check("A9 图外引用非空时的提示明确要求补节点（不是「大概能用」）", fallback.next.includes("补全图外引用") && explicit.next.includes("run_workflow_batch"), `${fallback.next} | ${explicit.next}`);
    void refTargets;
}

// ================================================================ D. 不冻结模板拒绝建批次（先于 B，避免覆盖冻结模板）
setSection("D");
{
    project = { ...project, workflowTemplate: undefined };
    await entry.extractWorkflowTemplateTool({ name: "P9 缺省边界", templateId: "p9default" }, entryDeps());
    // P12 D1 补差：显式指定不冻结模板（图外引用非空）仍被拒——缺省路径则因「P9 缺省边界」不含
    // 「视频拆解」不再选中它（意图对齐：不同名模板不参与缺省采用，直接用内置）
    const refused = await entry
        .runWorkflowBatchTool({ templateId: "p9default", materials: [{ sourceVideoNodeId: VIDEO }] }, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("D1 不冻结模板（有图外引用）建批次被拒绝，原因写明「不冻结」", refused instanceof Error && /不冻结/.test(refused.message) && /补全/.test(refused.message), String(refused && refused.message));

    const crossCanvas = await entry
        .runWorkflowBatchTool({ templateId: "not-on-this-canvas", materials: [{ sourceVideoNodeId: VIDEO }] }, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("D2 跨画布模板 id 被拒绝（不串批次）", crossCanvas instanceof Error && /不一致/.test(crossCanvas.message), String(crossCanvas && crossCanvas.message));

    // P12：没有项目模板不再报错引导，直接用系统内置「视频拆解」（模板=系统资产，抽取降级为自定义能力）
    const noTemplate = await entry.runWorkflowBatchTool({ materials: [{ sourceVideoNodeId: VIDEO }] }, { ...entryDeps(), project: () => ({ ...project, workflowTemplate: undefined }) });
    check("D3 没有项目模板时直接用系统内置「视频拆解」（templateFrom=builtin，不再引导先抽模板）", noTemplate.templateFrom === "builtin" && noTemplate.templateId === "builtin-video-analysis" && !/extract_workflow_template|先抽模板|抽一次模板/.test(String(noTemplate.next) + JSON.stringify(noTemplate.warnings)), JSON.stringify({ from: noTemplate.templateFrom, id: noTemplate.templateId }));
}

// ================================================================ B. 3 素材建批次跑到待审批
setSection("B");
{
    resetProject();
    // P12 D1 补差：夹具模板名含「视频拆解」——C 段的缺省单实例要命中这份项目模板（同名完整自定义优先于内置）
    const extracted = await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: "P9 夹具模板·视频拆解", templateId: "p9demo" }, entryDeps());
    check("B1 前置：冻结模板就位", extracted.frozenOk === true && project.workflowTemplate?.templateId === "p9demo");

    // 素材来自画布（3 个源视频节点）
    project = { ...project, nodes: [...project.nodes, materialNode("m1", "video:m1", 1), materialNode("m2", "video:m2", 2), materialNode("m3", "video:m3", 3)] };
    const generationBefore = generationCalls.length;

    const result = await entry.runWorkflowBatchTool(
        {
            templateId: "p9demo",
            title: "P9 探针批次",
            materials: [
                { sourceVideoNodeId: "m1", title: "素材一" },
                { sourceVideoNodeId: "m2", title: "素材二", optionalSegments: "A" },
                { sourceVideoNodeId: "m3", title: "素材三" },
            ],
        },
        entryDeps(),
    );

    check("B2 返回 batchId + 3 个实例（每实例 materialize 成功）", Boolean(result.batchId) && result.instances.length === 3 && result.instances.every((row) => row.materialized === "created"), JSON.stringify({ batchId: result.batchId, rows: result.instances.map((row) => [row.instanceId, row.materialized]) }));
    check("B3 3 个实例全部到待审批（P4 判据：终点=待审批）", result.instances.every((row) => row.status === "awaiting_approval" && row.outcome === "awaiting_approval"), JSON.stringify(result.instances.map((row) => [row.instanceId, row.status, row.outcome])));
    check("B4 队列收尾：相位 finished、并发=3（缺省）、3 个实例都跑到待批", result.queue?.phase === "finished" && result.queue?.concurrency === 3 && result.queue?.awaitingApproval === 3 && result.queue?.attempted === 3, JSON.stringify(result.queue));
    check("B5 每实例节点骨架可读（源视频/拉片/全量 id）", result.instances.every((row) => row.nodes && row.nodes.lapian && row.nodes.sourceVideo && row.nodes.all.length >= 8), JSON.stringify(result.instances.map((row) => row.nodes && row.nodes.all.length)));
    check("B6 付费动作提取 = 0：runGeneration 只以 mode:\"text\" 被调用（3 次=3 实例各一次组装）", generationCalls.length - generationBefore === 3 && generationCalls.slice(generationBefore).every((call) => call.mode === "text"), JSON.stringify(generationCalls.slice(generationBefore)));
    check("B7 零视频创建端点：整段只打了桥侧拉片链（upload/run/job/正文），没有生成/创建类请求", fetchCalls.every((url) => /^\/api\/(video\/upload|lapian\/(run|[^/]+)|job\/[^/]+|render\/)/.test(url)) && unexpectedFetch === 0, JSON.stringify([...new Set(fetchCalls)]));
    check("B8 回执 paidVideoCreations 恒 0（结构性：本工具不碰生成链）", result.paidVideoCreations === 0 && /待审批/.test(result.next), result.next);

    // 侧栏页签（P8）同一份数据源：buildReviewList 行数 = 实例数
    const batchRecord = batch.findWorkflowBatch(project.workflowBatches, result.batchId);
    const hubStates = {};
    for (const member of batchRecord?.members || []) hubStates[member.instanceId] = member.hub?.localId ? "awaiting_approval" : undefined;
    const rows = review.buildReviewList({ batches: [batchRecord], nodes: project.nodes, hubStates, templateNameOf: () => project.workflowTemplate?.name });
    check("B9 侧栏「待审批」页签立即可见：buildReviewList 出 3 行且全部待批", rows.length === 1 && rows[0].instances.length === 3 && rows[0].instances.every((row) => row.status === "awaiting_approval"), JSON.stringify(rows.map((row) => row.instances.map((item) => [item.instanceId, item.statusLabel]))));
    check("B10 批次成员都绑了 hub 身份（P4 真的走到绑定，不是画布自嗨）", (batchRecord?.members || []).length === 3 && (batchRecord?.members || []).every((member) => Boolean(member.hub?.localId)), JSON.stringify((batchRecord?.members || []).map((member) => member.hub?.localId || null)));
    check("B11 落盘链路走过（批次记录 + 队列都 flush 过）", flushes.includes("p9-batch-enqueue") && flushes.includes("p8-queue"), JSON.stringify([...new Set(flushes)]));

    // 派生状态表与队列结论一致（状态来自 C5 派生，不是回执自填）
    const derived = (batchRecord?.members || []).map((member) => batchStatus.deriveInstanceStatus({ member, nodes: project.nodes, hubState: "awaiting_approval" }));
    check("B12 派生状态与回执一致（C5：状态现查派生，不落库）", derived.every((view, index) => view.status === result.instances[index].status), JSON.stringify(derived.map((view) => [view.instanceId, view.status, view.rule])));

    // 交卷证据：把**真调产出的**批次落成 JSON，供 ego DOM 探针原样 seed（DOM 看到的数据不是手写夹具）
    writeFileSync(
        path.join(outDir, "p9-batch-fixture.json"),
        JSON.stringify({ batchId: result.batchId, templateId: "p9demo", project: { nodes: project.nodes, connections: project.connections, workflowTemplate: project.workflowTemplate, workflowBatches: project.workflowBatches } }, null, 2),
        "utf8",
    );
}

// ================================================================ C. 单实例（幂等 + 拒绝覆盖）
setSection("C");
{
    const generationBefore = generationCalls.length;
    const nodeCountBefore = project.nodes.length;

    // 同素材（= B 段实例 #1 的素材）重复调用：同实例、already_present、零新增生成
    const again = await entry.runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m1" } }, entryDeps());
    check("C1 单实例同素材：落到同一实例（already_present，不重建不复拉片）", again.instances.length === 1 && again.instances[0].materialized === "already_present", JSON.stringify(again.instances.map((row) => [row.instanceId, row.materialized])));
    check("C2 单实例同素材：实例仍在待审批、零新增组装调用、节点数不变（幂等零冲突）", again.instances[0].status === "awaiting_approval" && generationCalls.length === generationBefore && project.nodes.length === nodeCountBefore, JSON.stringify({ status: again.instances[0].status, generation: generationCalls.length - generationBefore, nodes: project.nodes.length - nodeCountBefore }));

    // 换素材撞同号实例：如实拒绝，绝不覆盖
    project = { ...project, nodes: [...project.nodes, materialNode("m4", "video:solo", 4)] };
    const conflict = await entry
        .runWorkflowInstanceTool({ material: { sourceVideoNodeId: "m4" } }, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("C3 换素材撞同号实例 → 拒绝覆盖并给人话原因", conflict instanceof Error && /已被别的素材占住/.test(conflict.message), String(conflict && conflict.message));
    check("C4 冲突路径零新增生成调用（拒绝发生在计划层，不花钱）", generationCalls.length === generationBefore, String(generationCalls.length - generationBefore));
}

// ================================================================ E. 零回归 / 探针自检
setSection("E");
{
    let tscErrors = [];
    try {
        const output = execFileSync(process.execPath, [path.resolve("node_modules/typescript/bin/tsc"), "-p", "tsconfig.json", "--noEmit"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
        tscErrors = output.split("\n").filter((line) => line.includes("error TS"));
    } catch (error) {
        tscErrors = `${error.stdout || ""}${error.stderr || ""}`.split("\n").filter((line) => line.includes("error TS"));
    }
    const newErrors = tscErrors.filter((line) => !line.startsWith("src/components/layout/model-script-editor.tsx"));
    check("E1 tsc 仅既有 1 条基线错误（model-script-editor.tsx）", tscErrors.length === 1 && newErrors.length === 0, tscErrors.join(" | "));

    const probeSource = (await import("node:fs")).readFileSync(new URL(import.meta.url), "utf8");
    check("E2 探针自身没有付费生成调用点（无 run_generation 执行体 / 无视频创建端点）", !/run_generation\s*\(/.test(probeSource) && !probeSource.includes("api/video/" + "create"));

    check("E3 真实任务零接触：本探针只操作自建夹具项目（proj-p9）", project.id === "proj-p9" && logs.every((line) => !line.includes("proj-") || line.includes("p9")));
    check("E4 付费闸结构：整段没有 paid_confirmation 类判定（三工具不入付费队列）", generationCalls.every((call) => call.mode === "text"));
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
        batchId: results.find((item) => item.name.startsWith("B2"))?.detail || "",
        generationCalls: generationCalls.map((call) => [call.nodeId, call.mode]),
        fetchTargets: [...new Set(fetchCalls)],
        hubCalls,
        flushes: [...new Set(flushes)],
        identitySaves: identitySaves.length,
    },
};
writeFileSync(path.join(outDir, "p9-probe-results.json"), JSON.stringify(summary, null, 2), "utf8");
console.log(`\nP9 探针：${pass} 通过 / ${fails.length} 失败（证据 ${path.join(outDir, "p9-probe-results.json")}）`);
for (const [name, row] of sections) console.log(`  ${name}: ${row.pass} 通过 / ${row.fail} 失败`);
if (fails.length) {
    console.log("\n失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
globalThis.fetch = realFetch;
process.exit(fails.length ? 1 : 0);
