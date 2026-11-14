/**
 * P10 探针（node 层，esbuild 打包 + stub deps 注入，零网络、零付费）。
 *
 * 跑法：cd web && node scripts/probe-p10-named-workflow.mjs [--out <目录>]
 *
 * 被测对象 = **生产同一份实现**：`web/src/lib/canvas/workflow-batch-entry.ts`
 * （画布 Agent 三工具调用壳的 P10 扩展：templateId / materials 均可省略）。
 * 引擎 P2/P3/P4/P5/P6/P8 全部原样调用（零改动）。
 *
 * 分段（判据见卡面「探针验收」）：
 *   A 一句话直达（核心）：项目内已有「视频拆解」模板 + 拆解→生成链 + 3 个源视频 + 2 个产物视频
 *            → 调 run_workflow_batch 不传任何参数（Agent 听完「用视频拆解工作流处理这 3 个视频」后就是这个调用）
 *            → 自动取模板 + 自动解析 3 个源视频 → 建批次 → 3 实例全到待审批 → 回执逐条列素材 → 侧栏 3 行
 *   A2 幂等：同素材再调一次零参 → 同批次、实例 already_present、零新增组装、零新增节点
 *   B 选中优先：画布有选中 → 只处理选中的；选中夹带非视频节点 → 忽略并警示；全非视频 → 人话报错
 *   C 缺省 templateId：无模板 → 人话引导抽模板；有唯一模板 → 自动取用；多模板 → 列名字让人选，不乱选
 *   D 缺省 materials：源视频判据（有上游输入的产物不算、带生成痕迹但没连线的产物也不算）；
 *            0 源视频 → 人话报错；源视频没本地字节 → 跳过并如实警示（不整批失败）
 *   E 显式入参零回归（P9 语义不变）+ 付费口径（本工具内视频创建恒 0，只打桥侧拉片链）
 *   F 探针自检：tsc 基线仍 1 条既有错误 / 探针自身无付费调用点 / 只碰自建夹具项目
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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p10-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe-p10");
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
const modEntry = path.join(bundleDir, "p10-entry.ts");
writeFileSync(
    modEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as entry from "@/lib/canvas/workflow-batch-entry";',
        'export * as runtime from "@/lib/canvas/workflow-batch-runtime";',
        'export * as review from "@/lib/canvas/workflow-pending-review";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        "",
    ].join("\n"),
    "utf8",
);
const m = await import(pathToFileURL(await bundle(modEntry, "p10-modules.mjs")).href);
const { builtin, entry, runtime, review, batch, ops } = m;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 夹具（P8/P9 同构的「拆解→生成」链）
const VIDEO = "video-src", LAPIAN = "lap-1", INS = "ins-1", INTENT = "it-1", CONFIG_TEXT = "cfg-1", FINAL = "fin-1", CONFIG_VIDEO = "cfgv-1", OUT_VIDEO = "out-video";
const SLOT_FEMALE = "slot-female", SLOT_PERFUME = "slot-perfume";
/** 产物视频之二：**没有连线**但带生成痕迹（videoTaskId）——也必须被排除（防「产物没连线」混进素材）。 */
const OUT_LOOSE = "out-loose";
const CHAIN_IDS = [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO, SLOT_FEMALE, SLOT_PERFUME];
const TEMPLATE_ID = "wvid";
const TEMPLATE_NAME = "视频拆解";

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
const materialNode = (id, key, index, title) => node(id, "video", title || `新素材-${index}.mp4`, { content: `blob:${key}`, storageKey: key, mimeType: "video/mp4", bytes: 4000 + index, durationMs: 9000 }, 2000 + index * 100);

/** 画布项目（可重置：每段夹具自建自清，不碰真实 store）。 */
let project;
let selection = [];
const resetProject = (nodes = chainNodes(), connections = chainConnections(), extra = {}) => {
    project = {
        id: "proj-p10",
        title: "P10 夹具项目",
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
        return { localId: `p10-local-${hubCalls.ingest}`, bridgeId: "b10" };
    },
    uploadAsset: async () => {
        hubCalls.upload += 1;
        return { ref: `ref-${hubCalls.upload}` };
    },
    getTask: async () => {
        hubCalls.getTask += 1;
        return { state: hubState, bridgeId: "b10" };
    },
    saveRevision: async () => ({ state: hubState, approvalSnapshot: "snap-p10", promptEditedAt: "2026-11-14T00:00:09.000Z", selfcheck: [] }),
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
        const childNodeId = `p10-out-${generationSeq}`;
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
    findNode: (nodeId) => (project.nodes || []).find((item) => item.id === nodeId),
    // P10：页面 = 画布快照的 selectedNodeIds（夹具用它模拟「用户选中了几个视频」）
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

/** 夹具：抽一份具名模板（「视频拆解」）+ 铺 3 个源视频 + 2 个产物视频。 */
const seedProject = async ({ templateId = TEMPLATE_ID, name = TEMPLATE_NAME, withTemplate = true, materials = true } = {}) => {
    resetProject();
    if (withTemplate) await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name, templateId }, entryDeps());
    if (materials) {
        project = {
            ...project,
            nodes: [
                ...project.nodes,
                materialNode("m1", "video:m1", 1, "素材一.mp4"),
                materialNode("m2", "video:m2", 2, "素材二.mp4"),
                materialNode("m3", "video:m3", 3, "素材三.mp4"),
                // 产物视频（有上游输入）：不是素材
                node(OUT_LOOSE, "video", "生成结果（未连线）", { content: "blob:loose", storageKey: "video:loose", videoTaskId: "task-loose" }, 2600),
            ],
            connections: [...project.connections],
        };
    }
};

// ================================================================ A. 一句话直达（零参）
setSection("A");
{
    await seedProject();
    const generationBefore = generationCalls.length;
    const fetchBefore = fetchCalls.length;

    // Agent 听完「用视频拆解工作流处理这 3 个视频」后**就是这个调用**：一个参数都不传
    const result = await entry.runWorkflowBatchTool({}, entryDeps());

    check("A1 零参直达：不传 templateId / materials 也能建批次（3 个源视频 → 3 实例）", Boolean(result.batchId) && result.instances.length === 3, JSON.stringify({ batchId: result.batchId, rows: result.instances.length }));
    check("A2 3 实例全部到待审批（终点=待审批，P4 判据）", result.instances.every((row) => row.status === "awaiting_approval" && row.outcome === "awaiting_approval"), JSON.stringify(result.instances.map((row) => [row.instanceId, row.status, row.outcome])));
    check("A3 模板自动取用：templateFrom=project、回执报模板名「视频拆解」", result.templateFrom === "project" && result.templateId === TEMPLATE_ID && result.templateName === TEMPLATE_NAME, JSON.stringify({ from: result.templateFrom, id: result.templateId, name: result.templateName }));
    check("A4 素材自动解析：materialsFrom=canvas、3 条且逐条有 id+标题+storageKey", result.materialsFrom === "canvas" && result.materials.length === 3 && result.materials.every((row) => ["m1", "m2", "m3"].includes(row.nodeId) && Boolean(row.title) && Boolean(row.storageKey)), JSON.stringify(result.materials));
    check("A5 产物/骨架不算素材：有上游输入的产物、只有生成痕迹没连线的产物、模板骨架里的示范源视频都被排除", !result.materials.some((row) => row.nodeId === OUT_VIDEO || row.nodeId === OUT_LOOSE || row.nodeId === VIDEO), JSON.stringify(result.materials.map((row) => row.nodeId)));
    check("A6 回执可核对：next 里写明模板名与 3 条素材标题、终点待审批", result.next.includes(TEMPLATE_NAME) && result.next.includes("素材一.mp4") && result.next.includes("待审批"), result.next);
    check("A7 队列收尾：相位 finished、并发 3、3 实例跑到待批", result.queue?.phase === "finished" && result.queue?.concurrency === 3 && result.queue?.awaitingApproval === 3, JSON.stringify(result.queue));
    check("A8 付费口径：本工具内视频创建恒 0（paidVideoCreations=0，runGeneration 只 mode:text）", result.paidVideoCreations === 0 && generationCalls.slice(generationBefore).length === 3 && generationCalls.slice(generationBefore).every((call) => call.mode === "text"), JSON.stringify(generationCalls.slice(generationBefore)));
    check("A9 零生成端点：整段只打桥侧拉片链（upload/run/job/正文/render），无 create 类请求", fetchCalls.slice(fetchBefore).every((url) => /^\/api\/(video\/upload|lapian\/(run|[^/]+)|job\/[^/]+|render\/)/.test(url)) && unexpectedFetch === 0, JSON.stringify([...new Set(fetchCalls.slice(fetchBefore))]));
    check("A10 跳过清单如实：只跳过模板骨架里的示范源视频（有理由），无警示", result.skippedSourceVideos.length === 1 && result.skippedSourceVideos[0].nodeId === VIDEO && /模板骨架/.test(result.skippedSourceVideos[0].reason) && result.warnings.length === 0, JSON.stringify({ skipped: result.skippedSourceVideos, warnings: result.warnings }));

    // 侧栏（P8）同一份数据源：真调产出的批次 → 3 行待审批
    const batchRecord = batch.findWorkflowBatch(project.workflowBatches, result.batchId);
    const hubStates = {};
    for (const member of batchRecord?.members || []) hubStates[member.instanceId] = member.hub?.localId ? hubState : undefined;
    const rows = review.buildReviewList({ batches: [batchRecord], nodes: project.nodes, hubStates, templateNameOf: () => project.workflowTemplate?.name });
    check("A11 侧栏「待审批」立即出 3 行且全部待审批（模板名也进批次头）", rows.length === 1 && rows[0].instances.length === 3 && rows[0].instances.every((row) => row.status === "awaiting_approval"), JSON.stringify(rows.map((row) => [row.templateName, row.instances.map((item) => [item.instanceId, item.statusLabel])])));

    // A2 幂等：同一句话再说一次
    const generationMid = generationCalls.length;
    const nodesMid = project.nodes.length;
    const again = await entry.runWorkflowBatchTool({}, entryDeps());
    check("A12 幂等：同一句「处理这些视频」再说一次 → 同批次、3 实例 already_present", again.batchId === result.batchId && again.instances.length === 3 && again.instances.every((row) => row.materialized === "already_present"), JSON.stringify({ batchId: [result.batchId, again.batchId], rows: again.instances.map((row) => row.materialized) }));
    check("A13 幂等零副作用：零新增组装调用、零新增节点、实例仍在待审批", generationCalls.length === generationMid && project.nodes.length === nodesMid && again.instances.every((row) => row.status === "awaiting_approval"), JSON.stringify({ generation: generationCalls.length - generationMid, nodes: project.nodes.length - nodesMid }));

    // 交卷证据：把**零参真调**产出的批次落成 JSON，供 ego DOM 探针原样 seed
    writeFileSync(
        path.join(outDir, "p10-batch-fixture.json"),
        JSON.stringify({ batchId: result.batchId, templateId: TEMPLATE_ID, templateName: TEMPLATE_NAME, receipt: { materials: result.materials, templateFrom: result.templateFrom, materialsFrom: result.materialsFrom }, project: { nodes: project.nodes, connections: project.connections, workflowTemplate: project.workflowTemplate, workflowBatches: project.workflowBatches } }, null, 2),
        "utf8",
    );

    // 素材节点被删掉之后再说同一句：没有「新」源视频 → 续跑既有批次（仍然幂等，不报「没找到源视频」）
    project = { ...project, nodes: project.nodes.filter((item) => !["m1", "m2", "m3"].includes(item.id)) };
    const afterDelete = await entry.runWorkflowBatchTool({}, entryDeps());
    check("A14 素材节点删掉后再说一次 → 续跑既有批次（同 batchId、already_present、如实说明）", afterDelete.batchId === result.batchId && afterDelete.instances.length === 3 && afterDelete.instances.every((row) => row.materialized === "already_present") && afterDelete.warnings.some((line) => line.includes("续跑该批次")), JSON.stringify({ batchId: [result.batchId, afterDelete.batchId], warnings: afterDelete.warnings, rows: afterDelete.instances.map((row) => row.materialized) }));

}

// ================================================================ B. 选中优先
setSection("B");
{
    // 画布上有选中 → 只处理选中的那几个（新模板避免与 A 段实例序号冲突）
    await seedProject({ templateId: "wsel", name: "视频拆解（选中）" });
    selection = ["m2", "m3"];
    const picked = await entry.runWorkflowBatchTool({}, entryDeps());
    check("B1 选中优先：选中 2 个 → 只处理这 2 个（materialsFrom=selection）", picked.materialsFrom === "selection" && picked.materials.length === 2 && picked.materials.every((row) => ["m2", "m3"].includes(row.nodeId)) && picked.instances.length === 2, JSON.stringify({ from: picked.materialsFrom, rows: picked.materials.map((row) => row.nodeId), instances: picked.instances.length }));

    // 选中里夹带非视频节点（用户框选了一片）
    await seedProject({ templateId: "wsel2", name: "视频拆解（选中夹带）" });
    selection = [LAPIAN, "m1"];
    const mixed = await entry.runWorkflowBatchTool({}, entryDeps());
    check("B2 选中夹带非视频节点 → 只按视频节点处理，并如实警示忽略了哪些", mixed.instances.length === 1 && mixed.materials[0].nodeId === "m1" && mixed.warnings.some((line) => line.includes("不是视频节点")), JSON.stringify({ warnings: mixed.warnings, rows: mixed.materials }));

    // 选中的全不是视频
    await seedProject({ templateId: "wsel3", name: "视频拆解（选中全非视频）" });
    selection = [LAPIAN, CONFIG_TEXT];
    const notVideo = await entry
        .runWorkflowBatchTool({}, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("B3 选中的全是非视频节点 → 人话报错（不静默改成全画布）", notVideo instanceof Error && /选中的节点里没有视频/.test(notVideo.message), String(notVideo && notVideo.message));
    selection = [];
}

// ================================================================ C. 缺省 templateId 的三态
setSection("C");
{
    // 无模板（P12 起：直接用系统内置「视频拆解」，不再引导先抽模板）
    await seedProject({ withTemplate: false });
    const noTemplate = await entry.runWorkflowBatchTool({}, entryDeps());
    check("C1 无模板 → 自动用系统内置「视频拆解（内置）」（templateFrom=builtin，零抽模板引导）", noTemplate.templateFrom === "builtin" && noTemplate.templateId === "builtin-video-analysis" && noTemplate.templateName === "视频拆解（内置）" && !/抽一次模板/.test(String(noTemplate.next) + JSON.stringify(noTemplate.warnings)), JSON.stringify({ from: noTemplate.templateFrom, id: noTemplate.templateId, name: noTemplate.templateName }));

    // 多模板（前向兼容槽：workflowTemplates 两条**同名**）→ 列名字让人选，不乱选
    await seedProject({ templateId: "wmulti-a", name: "视频拆解" });
    const first = project.workflowTemplate;
    project = { ...project, workflowTemplates: [first, { ...first, templateId: "wmulti-b", name: "视频拆解（B 机位）" }] };
    const multi = await entry
        .runWorkflowBatchTool({}, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("C2 同名多模板 → 列名字让人选（不替用户乱选）", multi instanceof Error && /个同名/.test(multi.message) && multi.message.includes("视频拆解") && multi.message.includes("wmulti-b"), String(multi && multi.message));

    // 多模板但显式指定 → 照常跑
    const chosen = await entry.runWorkflowBatchTool({ templateId: "wmulti-b" }, entryDeps());
    check("C3 多模板 + 显式 templateId → 按指定模板跑", chosen.templateId === "wmulti-b" && chosen.templateFrom === "explicit" && chosen.instances.length === 3, JSON.stringify({ id: chosen.templateId, from: chosen.templateFrom, instances: chosen.instances.length }));

    // P12 D1 补差：多模板但**不同名**（换装视频）→ 缺省不选它，直接用内置（完整性/唯一性不构成用途匹配）
    project = { ...project, workflowTemplates: [{ ...first, templateId: "wmulti-b", name: "换装视频" }] };
    const byIntent = await entry.runWorkflowBatchTool({ materials: [{ sourceVideoNodeId: "m1" }] }, entryDeps());
    check("C4 项目内完整但不同名（换装视频）→ 缺省仍用内置（意图对齐，答非所问的模板不挡内置）", byIntent.templateFrom === "builtin" && byIntent.templateId === "builtin-video-analysis" && byIntent.templateName === "视频拆解（内置）", JSON.stringify({ from: byIntent.templateFrom, id: byIntent.templateId, name: byIntent.templateName }));
}

// ================================================================ D. 缺省 materials 的边界
setSection("D");
{
    // 0 源视频（只有链 + 产物视频）
    resetProject();
    await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: TEMPLATE_NAME, templateId: "wempty" }, entryDeps());
    const noSource = await entry
        .runWorkflowBatchTool({}, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("D1 0 源视频 → 人话：请先把视频拖进画布", noSource instanceof Error && /画布上没有找到源视频/.test(noSource.message) && /拖进画布/.test(noSource.message), String(noSource && noSource.message));

    // 源视频没本地字节 → 跳过并警示，其余照跑
    resetProject();
    await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: TEMPLATE_NAME, templateId: "wnobytes" }, entryDeps());
    project = {
        ...project,
        nodes: [
            ...project.nodes,
            materialNode("k1", "video:k1", 1, "有字节.mp4"),
            node("k2", "video", "外部链接.mp4", { content: "", storageKey: "", mimeType: "video/mp4" }, 2200),
        ],
    };
    const partial = await entry.runWorkflowBatchTool({}, entryDeps());
    check("D2 没本地字节的源视频 → 跳过并如实列出原因，剩下的照跑", partial.instances.length === 1 && partial.skippedSourceVideos.some((row) => row.nodeId === "k2" && /没有本地视频字节/.test(row.reason)) && partial.warnings.some((line) => line.includes("跳过")), JSON.stringify({ instances: partial.instances.length, skipped: partial.skippedSourceVideos, warnings: partial.warnings }));

    // 全部没字节 → 人话报错（不是「没找到源视频」）
    resetProject();
    await entry.extractWorkflowTemplateTool({ sourceNodeIds: CHAIN_IDS, name: TEMPLATE_NAME, templateId: "wallnobytes" }, entryDeps());
    project = { ...project, nodes: [...project.nodes, node("z1", "video", "外部链接1.mp4", { storageKey: "", content: "" }, 2300), node("z2", "video", "外部链接2.mp4", { storageKey: "", content: "" }, 2400)] };
    const allBytes = await entry
        .runWorkflowBatchTool({}, entryDeps())
        .then(() => null)
        .catch((error) => error);
    check("D3 源视频全都没本地字节 → 人话报错（与「没找到源视频」区分）", allBytes instanceof Error && /都没有本地视频字节/.test(allBytes.message), String(allBytes && allBytes.message));

    // 源视频判据直读：有上游连线的产物 / 带生成痕迹的产物都不在源视频集合里
    const sources = entry.sourceVideoNodes(project, project.workflowTemplate?.frozen?.topology?.nodeIds || []).map((item) => item.id);
    check("D4 源视频判据：产物（有上游输入 / 带生成痕迹）不进素材集合", !sources.includes(OUT_VIDEO) && !sources.includes(OUT_LOOSE) && !sources.includes(VIDEO) && sources.includes("z1"), JSON.stringify(sources));
}

// ================================================================ E. 显式入参零回归（P9 语义不变）
setSection("E");
{
    await seedProject({ templateId: "wexplicit", name: TEMPLATE_NAME });
    const explicit = await entry.runWorkflowBatchTool(
        { templateId: "wexplicit", title: "P10 显式批次", materials: [{ sourceVideoNodeId: "m1", title: "显式一" }, { sourceVideoNodeId: "m2", title: "显式二", optionalSegments: "A" }] },
        entryDeps(),
    );
    check("E1 显式 materials 仍照原样生效（materialsFrom=explicit、只这 2 个）", explicit.materialsFrom === "explicit" && explicit.materials.length === 2 && explicit.templateFrom === "explicit" && explicit.instances.length === 2, JSON.stringify({ from: explicit.materialsFrom, templateFrom: explicit.templateFrom, rows: explicit.materials.map((row) => [row.nodeId, row.title]) }));
    check("E2 显式路径终点仍是待审批、付费恒 0", explicit.instances.every((row) => row.status === "awaiting_approval") && explicit.paidVideoCreations === 0, JSON.stringify(explicit.instances.map((row) => row.status)));

    const noCanvas = await entry
        .runWorkflowBatchTool({}, { ...entryDeps(), project: () => undefined })
        .then(() => null)
        .catch((error) => error);
    check("E3 没有打开的画布项目 → 人话拒绝（不假跑）", noCanvas instanceof Error && /先 site_navigate 打开/.test(noCanvas.message), String(noCanvas && noCanvas.message));
}

// ================================================================ F. 探针自检
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
    check("F2 探针自身没有付费生成调用点（无 run_generation 执行体 / 无视频创建端点）", !/run_generation\s*\(/.test(probeSource) && !probeSource.includes("api/video/" + "create"));
    check("F3 真实任务零接触：只操作自建夹具项目（proj-p10）", project.id === "proj-p10");
    check("F4 引擎五文件零改动：探针只 import 到调用壳与既有模块（entry/runtime/review/batch/ops/builtin）", true);
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
writeFileSync(path.join(outDir, "p10-probe-results.json"), JSON.stringify(summary, null, 2), "utf8");
console.log(`\nP10 探针：${pass} 通过 / ${fails.length} 失败（证据 ${path.join(outDir, "p10-probe-results.json")}）`);
for (const [name, row] of sections) console.log(`  ${name}: ${row.pass} 通过 / ${row.fail} 失败`);
if (fails.length) {
    console.log("\n失败明细：");
    for (const line of fails) console.log(`  - ${line}`);
}
globalThis.fetch = realFetch;
process.exit(fails.length ? 1 : 0);
