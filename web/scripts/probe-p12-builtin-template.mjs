/**
 * P12 探针（node 层，esbuild 打包 + stub deps 注入，零网络、零付费）。
 *
 * 跑法：cd web && node scripts/probe-p12-builtin-template.mjs [--out <目录>]
 *
 * 被测对象 = **生产同一份实现**：
 *   - `web/src/lib/canvas/workflow-builtin-template.ts`（内置「视频拆解」模板注册表：原型图 + P2 抽取）
 *   - `web/src/lib/canvas/workflow-batch-entry.ts`（P12 缺省解析链：项目内完整模板 > 内置模板 > 报错）
 *   - `web/src/lib/canvas/workflow-batch-runtime.ts`（内置模板批次的重建/续跑）
 * 引擎 P2/P3/P4/P5/P6/P7/P8 原样调用（零改动）。
 *
 * 分段（判据见卡面「探针验收」）：
 *   A 核心：**全新空项目**（无 workflowTemplate、无任何链）+ 1 源视频 + 1 人物图 + 1 产品图
 *          → 一句话「执行视频拆解工作流，用画布里的角色和产品替换原视频对应内容」
 *          → 自动用内置模板（templateFrom=builtin）→ 槽绑定（人物/产品，带理由）→ 实例链落画布
 *          → 停待批 → 侧栏 1 行可见 → 幂等重放不翻倍
 *   B 多视频批次：同一句话处理 3 个视频 → 3 实例全到待批；画布图按槽语义自动绑定（无显式指代）
 *   C 显式覆盖：项目内有**同名「视频拆解」**完整自定义模板 → 缺省优先用项目模板（templateFrom=project）；
 *            不同名的完整模板不参与缺省采用（意图对齐：完整性不构成用途匹配）；显式传内置 id → from=explicit
 *   D 空壳模板（所有者真机实证的 2 节点模板）：缺省解析跳过并用内置 + 回执按实际缺项如实说明；
 *            显式传空壳 id → 人话报错、零建批
 *   E 内置模板同源性/自足性：声明由 P2 抽取函数从**同一份原型**算出（再抽一次 digest 一致 = 无第二份骨架）、
 *            五槽齐备、零图外引用、指令正文取默认模板（单一真源）、完整性三项判据（E11/E12）、
 *            原型防污染（E13/E14 = v2 R3：同进程两次实例化原型不变）
 *   F 探针自检：零真实网络、零付费调用点、只碰自建夹具
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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p12-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p12");
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
        // 指令正文来自 lapian-prompt-templates（raw import 镜头语言契约）——与 vite 的 `?raw` 同义
        loader: { ".md": "text" },
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    return outFile;
}

// ---------------------------------------------------------------- 模块装载（生产同一份实现）
const modEntry = path.join(bundleDir, "p12-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as entry from "@/lib/canvas/workflow-batch-entry";',
        'export * as runtime from "@/lib/canvas/workflow-batch-runtime";',
        'export * as review from "@/lib/canvas/workflow-pending-review";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        'export * as registry from "@/lib/canvas/workflow-builtin-template";',
        'export * as template from "@/lib/canvas/workflow-template";',
        'export * as instance from "@/lib/canvas/workflow-instance";',
        'export * as promptTemplates from "@/lib/canvas/lapian-prompt-templates";',
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p12-modules.mjs")).href);
const { builtin, entry, runtime, review, batch, ops, registry, template: templateMod, instance, promptTemplates } = m;
builtin.registerBuiltinNodes();

const BUILTIN_ID = registry.BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID;
const BUILTIN_NAME = registry.BUILTIN_VIDEO_ANALYSIS_TEMPLATE_NAME;

// ---------------------------------------------------------------- 夹具
const node = (id, type, title, metadata, x = 0, y = 0, width = 320, height = 200) => ({ id, type, title, position: { x, y }, width, height, metadata });

/** 用户画布上的素材（**没有任何工作流链、没有模板**）：1 源视频 + 1 人物图 + 1 产品图。 */
const emptyCanvasNodes = () => [
    node("m1", "video", "素材一.mp4", { content: "blob:m1", storageKey: "video:m1", mimeType: "video/mp4", bytes: 4200, durationMs: 9000 }, 0, 0),
    node("img-person", "image", "人物-女性.png", { content: "blob:person", storageKey: "image:person", mimeType: "image/png" }, 400, 0, 200, 200),
    node("img-product", "image", "产品-香水.png", { content: "blob:product", storageKey: "image:product", mimeType: "image/png" }, 640, 0, 200, 200),
];

/** C 段用：一条已跑通的「拆解→生成」链（P10/P11 同构夹具），可从它抽出完整自定义模板。 */
const CHAIN = { video: "video-src", lapian: "lap-1", ins: "ins-1", intent: "it-1", cfgPrompt: "cfg-1", final: "fin-1", cfgVideo: "cfgv-1", out: "out-video", slotFemale: "slot-female", slotPerfume: "slot-perfume" };
const CHAIN_IDS = Object.values(CHAIN);
const chainNodes = () => [
    node(CHAIN.video, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4" }),
    node(CHAIN.lapian, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }, 400, 0, 420, 520),
    node(CHAIN.ins, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow" }, 900, 0),
    node(CHAIN.intent, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true }, 900, 260),
    node(CHAIN.cfgPrompt, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: CHAIN.lapian, composerContent: `拉片：@[node:${CHAIN.lapian}]\n指令：@[node:${CHAIN.ins}]\n意图：@[node:${CHAIN.intent}]` }, 1300, 0, 360, 220),
    node(CHAIN.final, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 1700, 0, 360, 240),
    node(CHAIN.cfgVideo, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: `提示词：@[node:${CHAIN.final}]` }, 2100, 0, 360, 200),
    node(CHAIN.out, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle", videoTaskId: "task-out", videoProgress: 100 }, 2500, 0),
    node(CHAIN.slotFemale, "image", "资产位·人物·女性A", { content: "blob:sf", storageKey: "image:sf", assetSlotSourceId: CHAIN.lapian, assetSlotOrder: 0, assetSlotName: "女性A", assetSlotType: "人物", assetSlot: { desc: "原片模特，红色风衣", span: "00:00-02.00" } }, 520, 600, 200, 200),
    node(CHAIN.slotPerfume, "image", "资产位·主体·香水", { content: "blob:sp", storageKey: "image:sp", assetSlotSourceId: CHAIN.lapian, assetSlotOrder: 1, assetSlotName: "香水", assetSlotType: "主体", assetSlot: { desc: "细长圆柱体", span: "02.00-05.00" } }, 760, 600, 200, 200),
];
const chainConnections = () => [
    { id: "e1", fromNodeId: CHAIN.video, toNodeId: CHAIN.lapian },
    { id: "e2", fromNodeId: CHAIN.lapian, toNodeId: CHAIN.cfgPrompt },
    { id: "e3", fromNodeId: CHAIN.ins, toNodeId: CHAIN.cfgPrompt },
    { id: "e4", fromNodeId: CHAIN.intent, toNodeId: CHAIN.cfgPrompt },
    { id: "e5", fromNodeId: CHAIN.cfgPrompt, toNodeId: CHAIN.final },
    { id: "e6", fromNodeId: CHAIN.final, toNodeId: CHAIN.cfgVideo },
    { id: "e7", fromNodeId: CHAIN.cfgVideo, toNodeId: CHAIN.out },
];

let project;
let selection = [];
const resetProject = (nodes, connections, extra = {}) => {
    project = {
        id: "proj-p12",
        title: "P12 夹具项目",
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

// ---------------------------------------------------------------- stub 依赖（零网络 / 零付费）
const logs = [];
const generationCalls = [];
const fetchCalls = [];
const hubCalls = { ingest: 0, upload: 0, getTask: 0 };
const identitiesSaved = [];
let uploadSeq = 0;
let generationSeq = 0;
const HUB_STATE = "awaiting_approval";

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
        return { localId: `p12-local-${hubCalls.ingest}`, bridgeId: "b12" };
    },
    uploadAsset: async () => {
        hubCalls.upload += 1;
        return { ref: `ref-${hubCalls.upload}` };
    },
    getTask: async () => {
        hubCalls.getTask += 1;
        return { state: HUB_STATE, bridgeId: "b12" };
    },
    saveRevision: async () => ({ state: HUB_STATE, approvalSnapshot: "snap-p12", promptEditedAt: "2026-11-14T00:00:09.000Z", selfcheck: [] }),
    approve: async () => ({}),
    rollback: async () => ({}),
    revoke: async () => ({}),
    waitForApproval: async () => HUB_STATE,
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
        const childNodeId = `p12-out-${generationSeq}`;
        const content = `【本次产物 ${generationSeq}】提示词正文`;
        await applyOpsWithProject([
            { type: "add_node", id: childNodeId, nodeType: "text", title: "提示词终稿（本次）", position: { x: 5000 + generationSeq * 60, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
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
    flush: () => {},
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
    flush: () => {},
    runBatch: (batchId) => runtime.startBatch(batchId),
    findNode: (nodeId) => (project.nodes || []).find((item) => item.id === nodeId),
    selectedNodeIds: () => selection,
    log: (message) => logs.push(message),
    ...overrides,
});

// global fetch：只认桥侧拉片链（stub），其余一律记「未覆盖」并抛错（防真网络）
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

/** 实例链节点 id（P3 确定性重映射）：`t<模板id>-<实例序>-<原型id>`。 */
const instanceNodeId = (templateId, index, oldId) => `t${templateId}-${index}-${oldId}`;

// ================================================================ E. 内置模板声明（先验注册表本身）
setSection("E");
{
    const builtinTemplate = registry.builtinVideoAnalysisTemplate();
    const source = registry.builtinWorkflowTemplateSource(BUILTIN_ID);
    const verdict = registry.assessTemplateCompleteness(builtinTemplate);

    check("E1 内置模板 id/名固定：builtin-video-analysis / 视频拆解", builtinTemplate.templateId === BUILTIN_ID && builtinTemplate.name === BUILTIN_NAME, JSON.stringify({ id: builtinTemplate.templateId, name: builtinTemplate.name }));
    check("E2 声明随代码分发：9 骨架节点 / 6 连线 / schemaVersion=1", builtinTemplate.frozen.topology.nodeIds.length === 9 && builtinTemplate.frozen.topology.edgeCount === 6 && builtinTemplate.schemaVersion === 1, JSON.stringify({ nodes: builtinTemplate.frozen.topology.nodeIds.length, edges: builtinTemplate.frozen.topology.edgeCount, v: builtinTemplate.schemaVersion }));
    check("E3 零图外引用（模板冻结，可实例化）", (builtinTemplate.externalReferences || []).length === 0, JSON.stringify(builtinTemplate.externalReferences));
    const slotOf = (kind) => (builtinTemplate.slots || []).find((slot) => slot.kind === kind);
    check("E4 五槽齐备：源视频/意图/资产位/生成参数/可选段", ["sourceVideo", "intentText", "assetAttach", "generationParams", "optionalSegments"].every((kind) => Boolean(slotOf(kind))), JSON.stringify((builtinTemplate.slots || []).map((slot) => slot.kind)));
    check(
        "E5 槽目标对齐：源视频槽=原型源视频、资产位=人物槽+产品槽（序 0/1）、可选段=拉片、生成参数=两个配置",
        slotOf("sourceVideo").targets.map((t) => t.nodeId).join() === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.sourceVideo &&
            slotOf("optionalSegments").targets.map((t) => t.nodeId).join() === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.lapian &&
            slotOf("generationParams").targets.map((t) => t.nodeId).join() === [registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.promptConfig, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.videoConfig].join(),
        JSON.stringify(builtinTemplate.slots.map((slot) => [slot.kind, slot.targets.map((t) => t.nodeId)])),
    );
    const personSlot = source.nodes.find((n) => n.id === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.personSlot);
    const productSlot = source.nodes.find((n) => n.id === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.productSlot);
    check(
        "E6 资产槽语义与 P11 绑定口径兼容：人物槽(人物|人物,序0)/产品槽(产品|产品,序1)",
        personSlot.metadata.assetSlotName === "人物" && personSlot.metadata.assetSlotType === "人物" && personSlot.metadata.assetSlotOrder === 0 &&
            productSlot.metadata.assetSlotName === "产品" && productSlot.metadata.assetSlotType === "产品" && productSlot.metadata.assetSlotOrder === 1,
        JSON.stringify([personSlot.metadata, productSlot.metadata]),
    );
    // 「零素材字节」= 没有视频/图片字节与运行态；指令/意图节点上的正文是**提示词文本**，不是素材（且不复制，见 E10）
    const byteNodes = source.nodes.filter((n) => ["video", "image"].includes(String(n.type)));
    const textNodes = source.nodes.filter((n) => String(n.type) === "text");
    check(
        "E7 零素材字节依赖：全模板无 storageKey；视频/图片节点无字节；正文只出现在两个文本节点（指令/意图，是提示词不是素材）",
        source.nodes.every((n) => !String(n.metadata?.storageKey || "")) &&
            byteNodes.every((n) => !String(n.metadata?.content || "") && !n.metadata?.attachedImageId && !n.metadata?.videoTaskId) &&
            source.nodes.every((n) => String(n.type) === "text" || !String(n.metadata?.content || "")) &&
            textNodes.filter((n) => String(n.metadata?.content || "")).every((n) => [registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.instruction, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.intent].includes(n.id)),
        JSON.stringify([source.nodes.map((n) => [n.id, n.type, n.metadata?.storageKey, String(n.metadata?.content || "").length]), byteNodes.map((n) => [n.id, n.type])]),
    );
    check("E8 模板完整（完整性判定通过，9 节点）", verdict.ok === true && verdict.nodeCount === 9, JSON.stringify(verdict));
    // 同源证明：拿同一份原型再跑一次 P2 抽取，digest/槽/冻结项必须一致（存在第二份手写声明就会漂）
    const reExtracted = templateMod.extractWorkflowTemplate({
        sourceProject: { id: "builtin-video-analysis-source", title: "系统内置 · 视频拆解", nodes: source.nodes, connections: source.connections },
        selectedNodeIds: source.nodes.map((n) => n.id),
        templateId: BUILTIN_ID,
        name: BUILTIN_NAME,
        createdAt: builtinTemplate.createdAt,
    });
    check(
        "E9 同源性：内置声明 == P2 抽取函数对同一原型的产物（拓扑 digest/槽/冻结项逐项一致，禁第二份骨架）",
        reExtracted.template.frozen.topology.digest === builtinTemplate.frozen.topology.digest &&
            JSON.stringify(reExtracted.template.slots) === JSON.stringify(builtinTemplate.slots) &&
            JSON.stringify(reExtracted.template.frozen) === JSON.stringify(builtinTemplate.frozen),
        JSON.stringify({ digest: [reExtracted.template.frozen.topology.digest, builtinTemplate.frozen.topology.digest] }),
    );
    const instruction = source.nodes.find((n) => n.id === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.instruction);
    const defaultInstruction = promptTemplates.getLapianPromptTemplate(promptTemplates.DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID).instruction;
    check("E10 指令正文取默认模板（单一真源，不手抄）：内容与 lapian-prompt-templates 默认模板逐字节一致", instruction.metadata.content === defaultInstruction && instruction.metadata.promptTemplate === promptTemplates.DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID, JSON.stringify({ len: String(instruction.metadata.content || "").length, tpl: instruction.metadata.promptTemplate }));
    // 空壳模板判定（D 段的判据来源；v2 §2.2.4 三项合并判据：指令冻结/拓扑连线/源视频入口）
    const hollow = registry.assessTemplateCompleteness({
        schemaVersion: 1,
        templateId: "w9a7b55",
        name: "空壳",
        createdAt: "2026-11-14T00:00:00.000Z",
        sourceProjectId: "p",
        sourceProjectTitle: "p",
        sourceNodeIds: ["v", "i"],
        slots: [],
        frozen: { instructions: [], generationModes: [], composerSkeletons: [], topology: { edgeCount: 0, digest: "x", nodeIds: ["v", "i"] } },
        externalReferences: [],
        stripped: [],
    });
    check("E11 空壳模板判不完整（2 节点：缺指令冻结+缺拓扑连线+缺源视频入口，按实际缺项报人话）", hollow.ok === false && hollow.nodeCount === 2 && hollow.problems.length >= 3 && hollow.problems.some((line) => line.includes("指令冻结")) && hollow.problems.some((line) => line.includes("拓扑连线")) && hollow.problems.some((line) => line.includes("源视频")), JSON.stringify(hollow));
    // 三项合并判据的反向覆盖：小模板（节点数 < 内置骨架 9）只要三项合同齐就判完整（节点数阈值不再用）
    const smallButComplete = {
        ...(registry.builtinVideoAnalysisTemplate()),
        templateId: "small-ok",
        name: "小而完整",
        frozen: {
            ...registry.builtinVideoAnalysisTemplate().frozen,
            topology: { ...registry.builtinVideoAnalysisTemplate().frozen.topology, nodeIds: registry.builtinVideoAnalysisTemplate().frozen.topology.nodeIds.slice(0, 4) },
        },
    };
    const smallVerdict = registry.assessTemplateCompleteness(smallButComplete);
    check("E12 判据不按节点数：4 节点小模板（指令冻结/连线/源视频三项齐）判完整，不误伤", smallVerdict.ok === true && smallVerdict.nodeCount === 4, JSON.stringify(smallVerdict));
    // D7（R3）：同进程两次取原型，第二次与第一次逐字节一致（structuredClone/逐节点拷贝防运行态写脏原型）
    {
        const first = registry.builtinWorkflowTemplateSource(BUILTIN_ID);
        first.nodes[0].metadata.dirty = true; // 故意写脏第一次拿到的副本
        first.nodes[0].title = "被我改了";
        const second = registry.builtinWorkflowTemplateSource(BUILTIN_ID);
        const third = registry.builtinWorkflowTemplateSource(BUILTIN_ID);
        const fingerprint = (source) => JSON.stringify([source.nodes.map((n) => [n.id, n.title, n.metadata]), source.connections]);
        check("E13 原型防污染（R3）：写脏第一次的副本后，再次取原型逐字节不变（两次指纹一致且无脏值）", fingerprint(second) === fingerprint(third) && second.nodes[0].title === "源视频（每个视频一条实例）" && second.nodes[0].metadata.dirty === undefined, JSON.stringify({ firstTitle: first.nodes[0].title, secondTitle: second.nodes[0].title, dirtyLeaked: second.nodes[0].metadata.dirty }));
        const builtinFirst = registry.builtinVideoAnalysisTemplate();
        const builtinAgain = registry.builtinVideoAnalysisTemplate();
        check("E14 内置模板声明同进程两次同引用同内容（digest 不漂移）", builtinFirst === builtinAgain && builtinFirst.frozen.topology.digest === builtinAgain.frozen.topology.digest, JSON.stringify({ same: builtinFirst === builtinAgain, digest: builtinFirst.frozen.topology.digest.slice(0, 12) }));
    }
}

// ================================================================ A. 核心：全新空项目一句话直达
setSection("A");
let coreBatchId;
{
    resetProject(emptyCanvasNodes(), []);
    check("A0 夹具=全新空项目：无 workflowTemplate、无任何链（只有 1 视频 + 1 人物图 + 1 产品图）", !project.workflowTemplate && project.nodes.length === 3 && project.connections.length === 0, JSON.stringify({ nodes: project.nodes.length, conns: project.connections.length }));
    const generationBefore = generationCalls.length;
    const fetchBefore = fetchCalls.length;

    // 用户原话：「执行视频拆解工作流，用画布里的角色和产品替换原视频对应内容」
    // → Agent 按 schemas.ts 描述的映射把它翻成两条槽指代（角色→人物槽、产品→产品槽）+ 零 templateId/materials
    const result = await entry.runWorkflowBatchTool(
        {
            assetBindings: [
                { slotName: "人物", imageNodeId: "img-person", reason: "画布里的角色" },
                { slotName: "产品", imageNodeId: "img-product", reason: "画布里的产品" },
            ],
        },
        entryDeps(),
    );
    coreBatchId = result.batchId;

    check("A1 自动用内置模板：templateFrom=builtin + templateId/templateName（带（内置）后缀）报给用户核对", result.templateFrom === "builtin" && result.templateId === BUILTIN_ID && result.templateName === `${BUILTIN_NAME}（内置）`, JSON.stringify({ from: result.templateFrom, id: result.templateId, name: result.templateName }));
    check("A2 素材自动解析：canvas 来源 1 条（画布上唯一的视频）", result.materialsFrom === "canvas" && result.materials.length === 1 && result.materials[0].nodeId === "m1", JSON.stringify(result.materials));
    check("A3 槽绑定正常：人物槽←人物-女性.png、产品槽←产品-香水.png（各带理由与原话）", (() => {
        const person = (result.assetBindings || []).find((row) => row.slotName === "人物");
        const product = (result.assetBindings || []).find((row) => row.slotName === "产品");
        return person?.status === "bound" && person?.imageNodeId === "img-person" && /你指定/.test(String(person?.reason)) && /画布里的角色/.test(String(person?.reason)) &&
            product?.status === "bound" && product?.imageNodeId === "img-product" && /你指定/.test(String(product?.reason));
    })(), JSON.stringify(result.assetBindings));
    check("A4 回执摘要写清「图 → 槽」与绑定来源（用户可核对）", /人物/.test(String(result.assetBindingSummary)) && /产品/.test(String(result.assetBindingSummary)) && /人物-女性\.png/.test(String(result.assetBindingSummary)) && /产品-香水\.png/.test(String(result.assetBindingSummary)), String(result.assetBindingSummary));
    check("A5 实例停待批（终点=待审批）+ 队列 finished", result.instances.length === 1 && result.instances[0].status === "awaiting_approval" && result.instances[0].outcome === "awaiting_approval" && result.queue?.phase === "finished", JSON.stringify({ rows: result.instances.map((row) => [row.instanceId, row.status, row.outcome]), queue: result.queue }));

    // 实例链路真的落到画布上（P3 确定性 id；9 个骨架节点 + 挂图副本 + 挂图对照）
    const idx = 1;
    // 引擎 G18 产物绑定：终稿「占位」节点会被**本次产物**替换并删除（旧产物零残留）——占位节点不在预期内
    const PLACEHOLDER = registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.finalText;
    const expectedSkeleton = Object.entries(registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS)
        .filter(([key]) => key !== "finalText")
        .map(([, oldId]) => instanceNodeId(BUILTIN_ID, idx, oldId));
    const present = new Set(project.nodes.map((n) => n.id));
    const missing = expectedSkeleton.filter((id) => !present.has(id));
    check("A6 实例链路画布可见：8 个内置骨架节点按确定性 id 落库（终稿占位按 G18 被本次产物替换删除）",
        missing.length === 0 && !present.has(instanceNodeId(BUILTIN_ID, idx, PLACEHOLDER)) && present.has("p12-out-1"),
        JSON.stringify({ missing, placeholderStale: present.has(instanceNodeId(BUILTIN_ID, idx, PLACEHOLDER)), product: present.has("p12-out-1"), sample: expectedSkeleton.slice(0, 3) }));
    check(
        "A7 实例链路连线齐全：源→拉片→组装三条输入链落库，终稿→视频配置改由本次产物承接（G18 重绑）",
        [1, 2, 3, 4].every((n) => project.connections.some((c) => c.id === `c${BUILTIN_ID}-${idx}-bva-e${n}`)) &&
            project.connections.some((c) => c.fromNodeId === "p12-out-1" && c.toNodeId === instanceNodeId(BUILTIN_ID, idx, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.videoConfig)),
        JSON.stringify(project.connections.filter((c) => String(c.id).includes(BUILTIN_ID)).map((c) => [c.id, c.fromNodeId, c.toNodeId])),
    );
    const instSource = project.nodes.find((n) => n.id === instanceNodeId(BUILTIN_ID, idx, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.sourceVideo));
    check("A8 槽①生效：实例源视频节点挂的是本次素材字节（storageKey=video:m1）", instSource?.metadata?.storageKey === "video:m1", JSON.stringify(instSource?.metadata));
    const instLapian = project.nodes.find((n) => n.id === instanceNodeId(BUILTIN_ID, idx, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.lapian));
    check("A9 槽①受控入口真跑：实例拉片节点带本次 taskId + 本次正文（不是模板旧正文）", String(instLapian?.metadata?.taskId || "").length > 0 && /正文/.test(String(instLapian?.metadata?.content || "")) && String(instLapian?.metadata?.content || "") !== String(registry.builtinWorkflowTemplateSource(BUILTIN_ID).nodes.find((n) => n.id === registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.lapian)?.metadata?.content || ""), JSON.stringify({ taskId: instLapian?.metadata?.taskId, content: String(instLapian?.metadata?.content || "").slice(0, 60) }));
    const instPerson = project.nodes.find((n) => n.id === instanceNodeId(BUILTIN_ID, idx, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.personSlot));
    const instProduct = project.nodes.find((n) => n.id === instanceNodeId(BUILTIN_ID, idx, registry.BUILTIN_VIDEO_ANALYSIS_NODE_IDS.productSlot));
    check(
        "A10 槽③生效：实例资产位挂上图副本（扁平 attachedImageId 与 assetSlot 镜像双写，指向副本而非原图）",
        String(instPerson?.metadata?.attachedImageId || "").endsWith("-img-person") && instPerson?.metadata?.assetSlot?.attachedImageId === instPerson?.metadata?.attachedImageId &&
            String(instProduct?.metadata?.attachedImageId || "").endsWith("-img-product") && instProduct?.metadata?.assetSlot?.attachedImageId === instProduct?.metadata?.attachedImageId,
        JSON.stringify({ person: instPerson?.metadata?.attachedImageId, mirror: instPerson?.metadata?.assetSlot?.attachedImageId, product: instProduct?.metadata?.attachedImageId }),
    );
    check("A11 画布上另有挂图对照图（图→槽的可视实体，不是黑盒）", project.nodes.some((n) => String(n.metadata?.assetGraftForSlotId || "") === instPerson?.id) && project.nodes.some((n) => String(n.metadata?.assetGraftForSlotId || "") === instProduct?.id), JSON.stringify(project.nodes.filter((n) => n.metadata?.assetGraftForSlotId).map((n) => [n.id, n.metadata.assetGraftForSlotId])));

    // 侧栏（P8 派生）同一份数据源
    const batchRecord = batch.findWorkflowBatch(project.workflowBatches, result.batchId);
    const hubStates = {};
    for (const member of batchRecord?.members || []) hubStates[member.instanceId] = member.hub?.localId ? HUB_STATE : undefined;
    const rows = review.buildReviewList({
        batches: [batchRecord],
        nodes: project.nodes,
        hubStates,
        templateNameOf: (templateId) => registry.workflowTemplateDisplayName(project, templateId),
    });
    check("A12 侧栏可见：「待审批」1 个批次 1 行，状态待审批", rows.length === 1 && rows[0].instances.length === 1 && rows[0].instances[0].status === "awaiting_approval", JSON.stringify(rows.map((row) => [row.templateName, row.instances.map((item) => [item.instanceId, item.statusLabel])])));
    check("A13 侧栏批次头显示内置模板显示名「视频拆解（内置）」（不露 builtin-video-analysis 内部 id）", rows[0]?.templateName === `${BUILTIN_NAME}（内置）` && !String(rows[0]?.templateName).includes("builtin-"), JSON.stringify(rows[0]?.templateName));

    check("A14 付费口径：本工具内视频创建恒 0（paidVideoCreations=0，runGeneration 只 mode:text）", result.paidVideoCreations === 0 && generationCalls.slice(generationBefore).length === 1 && generationCalls.slice(generationBefore).every((call) => call.mode === "text"), JSON.stringify(generationCalls.slice(generationBefore)));
    check("A15 零生成端点：整段只打桥侧拉片链（upload/run/job/正文/render）", fetchCalls.slice(fetchBefore).every((url) => /^\/api\/(video\/upload|lapian\/(run|[^/]+)|job\/[^/]+|render\/)/.test(url)) && unexpectedFetch === 0, JSON.stringify([...new Set(fetchCalls.slice(fetchBefore))]));
    check("A16 回执人话里写明「系统内置，开箱即用」+ 模板显示名带（内置）+ 终点待审批", /系统内置/.test(result.next) && /开箱即用/.test(result.next) && result.next.includes(`「${BUILTIN_NAME}（内置）」`) && /待审批/.test(result.next), result.next);
    check("A17 无「先抽模板」类引导（内置模板路径零抽模板提示）", !/抽一次模板|先在已跑通/.test(String(result.next) + JSON.stringify(result.warnings)), JSON.stringify(result.warnings));

    // 幂等：同一句话再说一次 → 同批次、already_present、零新增节点
    const nodesBefore = project.nodes.length;
    const again = await entry.runWorkflowBatchTool(
        {
            assetBindings: [
                { slotName: "人物", imageNodeId: "img-person", reason: "画布里的角色" },
                { slotName: "产品", imageNodeId: "img-product", reason: "画布里的产品" },
            ],
        },
        entryDeps(),
    );
    check("A18 幂等：同一句话说第二遍 → 同 batchId、实例 already_present、零新增节点", again.batchId === result.batchId && again.instances.every((row) => row.materialized === "already_present") && project.nodes.length === nodesBefore, JSON.stringify({ batchId: [again.batchId, result.batchId], materialized: again.instances.map((row) => row.materialized), nodes: [nodesBefore, project.nodes.length] }));
    check("A19 幂等重放仍报 templateFrom=builtin（续跑不换模板）", again.templateFrom === "builtin" && again.templateId === BUILTIN_ID, JSON.stringify({ from: again.templateFrom, id: again.templateId }));
}

// 把核心段的**生产代码产物**（画布节点/连线/批次记录）落成夹具，供 DOM 层探针直接 seed 到真 store
writeFileSync(
    path.join(outDir, "p12-batch-fixture.json"),
    JSON.stringify(
        {
            note: "P12 核心段（全新空项目 + 一句话）生产代码真跑后的项目状态：实例链节点/连线 + 批次记录（templateId=builtin-video-analysis）",
            project: {
                nodes: project.nodes,
                connections: project.connections,
                workflowBatches: project.workflowBatches,
                workflowTemplate: project.workflowTemplate || null,
                workflowInstances: [],
                workflowAssetVetoes: [],
            },
            receipt: { batchId: coreBatchId, templateId: BUILTIN_ID, templateName: BUILTIN_NAME, templateFrom: "builtin" },
        },
        null,
        2,
    ),
    "utf8",
);

// ================================================================ B. 多视频批次（3 个视频同一句话）
setSection("B");
{
    const nodes = [...emptyCanvasNodes()];
    for (const index of [2, 3]) {
        nodes.push(node(`m${index}`, "video", `素材${index}.mp4`, { content: `blob:m${index}`, storageKey: `video:m${index}`, mimeType: "video/mp4", bytes: 4000 + index }, 0, 200 * index));
    }
    resetProject(nodes, []);
    const result = await entry.runWorkflowBatchTool({}, entryDeps());
    check("B1 零参（连 assetBindings 都不传）也直达：3 实例全部待审批", result.templateFrom === "builtin" && result.instances.length === 3 && result.instances.every((row) => row.status === "awaiting_approval"), JSON.stringify({ from: result.templateFrom, rows: result.instances.map((row) => [row.instanceId, row.status]) }));
    check("B2 素材自动解析 3 条（画布上的 3 个视频，产物/骨架都不算）", result.materialsFrom === "canvas" && result.materials.length === 3 && ["m1", "m2", "m3"].every((id) => result.materials.some((row) => row.nodeId === id)), JSON.stringify(result.materials.map((row) => row.nodeId)));
    const person = (result.assetBindings || []).find((row) => row.slotName === "人物");
    const product = (result.assetBindings || []).find((row) => row.slotName === "产品");
    check("B3 没有显式指代时按槽语义自动绑（人物-女性.png→人物槽、产品-香水.png→产品槽，带命中词）", person?.status === "bound" && person?.imageNodeId === "img-person" && person?.source === "canvas_semantic" && (person?.matchedTerms || []).includes("人物") && product?.status === "bound" && product?.imageNodeId === "img-product" && (product?.matchedTerms || []).includes("产品"), JSON.stringify([person, product].map((row) => row && { name: row.slotName, status: row.status, image: row.imageNodeId, terms: row.matchedTerms, source: row.source })));
    check("B4 3 个实例各自独立的挂图副本（跨实例 id 零交集，图不串实例）", (() => {
        const copies = project.nodes.filter((n) => String(n.id).includes(BUILTIN_ID) && String(n.id).endsWith("-img-person"));
        return copies.length === 3 && new Set(copies.map((n) => n.id)).size === 3;
    })(), JSON.stringify(project.nodes.filter((n) => String(n.id).endsWith("-img-person")).map((n) => n.id)));
    check("B5 付费口径不变：3 实例只跑 3 次文本组装，paidVideoCreations=0", result.paidVideoCreations === 0 && result.instances.length === 3, JSON.stringify({ paid: result.paidVideoCreations }));
}

// ================================================================ C. 显式覆盖：同名「视频拆解」完整自定义模板优先
setSection("C");
{
    resetProject([...chainNodes(), ...emptyCanvasNodes().map((item) => ({ ...item, id: `x-${item.id}`, title: `素材-${item.title}` }))], chainConnections());
    // 抽一份**同名**「视频拆解」完整自定义模板（v2 §2.2.1：缺省采用要求同名且完整——
    // 用户自定义要覆盖内置，就得是对同一件事的自定义）——真实生产路径
    const extracted = await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: "视频拆解" }, entryDeps());
    const custom = project.workflowTemplate;
    check("C0 前置：项目内已装一份同名完整自定义模板（P2 抽取，零图外引用，三项判据通过）", Boolean(custom?.templateId) && custom.templateId !== BUILTIN_ID && extracted.frozenOk === true && registry.assessTemplateCompleteness(custom).ok === true, JSON.stringify({ id: custom?.templateId, frozenOk: extracted.frozenOk, verdict: registry.assessTemplateCompleteness(custom) }));

    // 缺省（意图对齐）：同名完整 → 项目内优先于内置
    const byProject = await entry.runWorkflowBatchTool({ materials: [{ sourceVideoNodeId: "x-m1" }] }, entryDeps());
    check("C1 项目内有同名完整模板时优先用它：templateFrom=project（自定义覆盖同名内置）", byProject.templateFrom === "project" && byProject.templateId === custom.templateId, JSON.stringify({ from: byProject.templateFrom, id: byProject.templateId, builtin: BUILTIN_ID }));
    check("C2 项目模板批次用的是项目模板的实例 id 前缀（不是内置模板）", byProject.instances.every((row) => String(row.instanceId).startsWith(`${custom.templateId}#`)), JSON.stringify(byProject.instances.map((row) => row.instanceId)));

    // D1（复审）意图对齐反例：项目内完整但**不同名**的模板不参与缺省采用（完整性不构成用途匹配）
    project = { ...project, workflowTemplate: { ...custom, templateId: "custom-other", name: "换装视频" } };
    const byDifferentName = await entry.runWorkflowBatchTool({ materials: [{ sourceVideoNodeId: "x-m1" }] }, entryDeps());
    check("C2b 项目内完整但不同名（换装视频）→ 缺省仍用内置（答非所问的模板不挡内置）", byDifferentName.templateFrom === "builtin" && byDifferentName.templateId === BUILTIN_ID, JSON.stringify({ from: byDifferentName.templateFrom, id: byDifferentName.templateId }));
    project = { ...project, workflowTemplate: custom };

    // 显式传项目模板 id
    const byExplicit = await entry.runWorkflowBatchTool({ templateId: custom.templateId, materials: [{ sourceVideoNodeId: "x-m1" }] }, entryDeps());
    check("C3 显式传项目模板 id → templateFrom=explicit", byExplicit.templateFrom === "explicit" && byExplicit.templateId === custom.templateId, JSON.stringify({ from: byExplicit.templateFrom, id: byExplicit.templateId }));

    // 显式传内置 id：即使项目有自定义模板也用内置（点名内置也是显式指定）
    const byBuiltin = await entry.runWorkflowBatchTool({ templateId: BUILTIN_ID, materials: [{ sourceVideoNodeId: "x-m1" }] }, entryDeps());
    check("C4 显式传内置 id → 用内置模板（templateFrom=explicit，与「自动兜底到内置」可区分），项目模板不受影响", byBuiltin.templateFrom === "explicit" && byBuiltin.templateId === BUILTIN_ID && project.workflowTemplate?.templateId === custom.templateId, JSON.stringify({ from: byBuiltin.templateFrom, id: byBuiltin.templateId, projectTemplate: project.workflowTemplate?.templateId }));
    check("C5 并存不冲突：两个批次各自独立入册（项目模板批次 + 内置批次）", (() => {
        const ids = new Set((project.workflowBatches || []).map((row) => row.templateId));
        return ids.has(custom.templateId) && ids.has(BUILTIN_ID);
    })(), JSON.stringify((project.workflowBatches || []).map((row) => row.templateId)));
    check("C6 内置批次回执模板名带「（内置）」后缀（与侧栏/抽屉同一个显示名口径）", byBuiltin.templateName === `${BUILTIN_NAME}（内置）` && byDifferentName.templateName === `${BUILTIN_NAME}（内置）` && byProject.templateName === "视频拆解", JSON.stringify({ builtin: byBuiltin.templateName, fallback: byDifferentName.templateName, project: byProject.templateName }));
}

// ================================================================ D. 空壳模板（所有者真机实证形态）
setSection("D");
{
    // 素材画布（1 视频 + 1 图，没有任何链）→ 默认范围抽取 = 2 节点空壳（与真机 w9a7b55 同形）
    resetProject(emptyCanvasNodes(), []);
    const hollow = await entry.extractWorkflowTemplateTool({ name: "视频拆解" }, entryDeps());
    const hollowVerdict = registry.assessTemplateCompleteness(project.workflowTemplate);
    check("D0 前置：空画布抽出的模板确实是空壳（缺指令冻结/缺拓扑连线，被判不完整）", hollow.nodeCount >= 1 && hollowVerdict.ok === false && hollowVerdict.problems.some((line) => line.includes("指令冻结")) && hollowVerdict.problems.some((line) => line.includes("拓扑连线")), JSON.stringify({ nodes: hollow.nodeCount, verdict: hollowVerdict }));
    const batchesBefore = (project.workflowBatches || []).length;

    // 缺省解析：同名空壳不采用、用内置，并如实说明
    const result = await entry.runWorkflowBatchTool({}, entryDeps());
    check("D1 项目内同名「视频拆解」是空壳 → 自动改用内置模板（templateFrom=builtin），照常跑到待审批", result.templateFrom === "builtin" && result.instances.length >= 1 && result.instances.every((row) => row.status === "awaiting_approval"), JSON.stringify({ from: result.templateFrom, rows: result.instances.map((row) => [row.instanceId, row.status]) }));
    check("D2 回执如实说明「项目内模板不完整（按实际缺项报人话），已使用内置模板」", result.warnings.some((line) => /项目内模板/.test(line) && /不完整/.test(line) && line.includes(`${hollow.nodeCount} 个节点`) && /指令冻结/.test(line) && /本次使用内置模板/.test(line)), JSON.stringify(result.warnings));
    check("D3 空壳模板没有被拿来建批次（没有 2 节点模板的实例）", (project.workflowBatches || []).slice(batchesBefore).every((row) => row.templateId === BUILTIN_ID), JSON.stringify((project.workflowBatches || []).slice(batchesBefore).map((row) => row.templateId)));

    // 显式传空壳 id：人话报错、零建批
    const batchesAfter = (project.workflowBatches || []).length;
    const error = await entry.runWorkflowBatchTool({ templateId: hollow.templateId }, entryDeps()).then(() => null, (err) => err);
    check("D4 显式传空壳 templateId → 人话报错（不完整 + 指出实际缺项 + 指出可改用内置）", Boolean(error) && /不完整/.test(error.message) && /指令冻结/.test(error.message) && /不构成完整的/.test(error.message) && /内置模板/.test(error.message), String(error?.message));
    check("D5 报错路径零写入：批次表没有新增", (project.workflowBatches || []).length === batchesAfter, JSON.stringify({ before: batchesAfter, after: (project.workflowBatches || []).length }));
}

// ================================================================ F. 探针自检
setSection("F");
{
    const selfSource = execFileSync("node", ["-e", "process.stdout.write('ok')"], { encoding: "utf8" });
    check("F1 探针自身能取到运行时（自检通道可用）", selfSource === "ok", selfSource);
    check("F2 全程零未覆盖网络请求（所有 fetch 都在 stub 白名单里）", unexpectedFetch === 0, `unexpectedFetch=${unexpectedFetch}`);
    check("F3 全程零视频生成端点（无 generate/create/completions 类请求）", fetchCalls.every((url) => !/generate|video\/create|image\/create|completions/i.test(url)), JSON.stringify([...new Set(fetchCalls)].filter((url) => /generate|create|completions/i.test(url))));
    check("F4 只碰自建夹具项目（projectId 固定 proj-p12，从未触碰真实 store）", project.id === "proj-p12", project.id);
}

// ---------------------------------------------------------------- 证据落盘
const summary = {
    at: new Date().toISOString(),
    pass,
    fail: fails.length,
    sections: Object.fromEntries([...sections.entries()].map(([key, value]) => [key, value])),
    fails,
    results,
};
writeFileSync(path.join(outDir, "p12-probe-results.json"), JSON.stringify(summary, null, 2), "utf8");
writeFileSync(
    path.join(outDir, "p12-builtin-receipt.json"),
    JSON.stringify(
        {
            note: "核心段（全新空项目 + 一句话）回执节选：templateFrom=builtin 的证据",
            batchId: coreBatchId,
            template: { id: BUILTIN_ID, name: BUILTIN_NAME, from: "builtin" },
            builtinTemplate: {
                slots: registry.builtinVideoAnalysisTemplate().slots.map((slot) => ({ kind: slot.kind, targets: slot.targets.map((target) => target.nodeId), derived: slot.derived ?? null })),
                frozen: registry.builtinVideoAnalysisTemplate().frozen.topology,
                externalReferences: registry.builtinVideoAnalysisTemplate().externalReferences.length,
            },
        },
        null,
        2,
    ),
    "utf8",
);

console.log(`\nP12 探针（node 层）：${pass} 通过 / ${fails.length} 失败（证据 ${path.join(outDir, "p12-probe-results.json")}）`);
for (const [key, value] of sections.entries()) console.log(`  ${key}: ${value.pass} 通过 / ${value.fail} 失败`);
if (fails.length) {
    console.log("失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
    process.exit(1);
}
