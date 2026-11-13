/**
 * P3 探针：模板 + 素材 → 画布新链路实例（实例化器 / add_node 幂等 / 受控拉片入口 / 失败原子性）。零真实付费。
 *
 * 分两段：
 *   A. 单测：esbuild 打包 `src/lib/canvas/workflow-instance.test.ts` → `node --test` 真跑。
 *   B. 真实样本端到端：从真实「无限画布 3」项目（IndexedDB canvas_store 落盘 JSON）抽两条模板——
 *      `pop3skeleton`（P0a §1.1 骨架边界，与 P2 相同）与 `pop3full`（骨架 + 真实资产位/挂图，用于槽③挂图）；
 *      然后真跑实例化：两次实例化零冲突、七类引用全重绑、模板零改动、add_node 重放幂等、
 *      受控拉片入口（stub 网络）回填**本次**正文、失败注入无半条链。
 *
 * 跑法：cd web && node scripts/probe-p3-instance.mjs
 *   --store <real-canvas-store.json> --project <项目标题或 id> --out <输出目录>
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails，不跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

// i18n 在纯 node 下读 localStorage：探针与页面启动同口径地注册内置节点（尺寸/默认 metadata 来自 constant/canvas）。
globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const storePath = path.resolve(argOf("store", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/real-canvas-store.json"));
const projectKey = argOf("project", "无限画布 3");
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p3-out"));
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe");
mkdirSync(bundleDir, { recursive: true });

let pass = 0;
const fails = [];
const results = [];
function check(name, condition, detail = "") {
    const ok = Boolean(condition);
    results.push({ name, ok, detail: ok ? "" : detail });
    if (ok) pass += 1;
    else fails.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

async function bundle(entry, outName, extra = {}) {
    const outFile = path.join(bundleDir, outName);
    await esbuild.build({
        entryPoints: [path.resolve(entry)],
        bundle: true,
        format: "esm",
        platform: "node",
        outfile: outFile,
        logLevel: "silent",
        alias: { "@": path.resolve("src") },
        external: ["node:test", "node:assert", "node:assert/strict"],
        ...extra,
    });
    return outFile;
}

// ---------------------------------------------------------------- A. 单测（node --test）
const testBundle = await bundle("src/lib/canvas/workflow-instance.test.ts", "p3-instance.test.mjs", {
    banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
});
let testOutput = "";
let testFailed = false;
try {
    testOutput = execFileSync(process.execPath, ["--test", testBundle], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
} catch (error) {
    testFailed = true;
    testOutput = `${error.stdout || ""}${error.stderr || ""}`;
}
const tapCount = (label) => {
    const match = testOutput.match(new RegExp(`^(?:# |ℹ )${label} (\\d+)$`, "m"));
    return match ? Number(match[1]) : undefined;
};
const tapTests = tapCount("tests");
const tapPass = tapCount("pass");
const tapFails = tapCount("fail");
check("A 单测进程零失败", !testFailed, testFailed ? testOutput.split("\n").filter((line) => line.includes("✖") || line.includes("not ok")).slice(0, 6).join(" | ") : "");
check("A 单测用例数 ≥ 30", Number(tapTests || 0) >= 30, `tests=${tapTests} pass=${tapPass} fail=${tapFails}`);
check("A 单测 fail 计数为 0", tapFails === 0, `fail=${tapFails}`);
writeFileSync(path.join(outDir, "unit-tests.tap"), testOutput, "utf8");

// ---------------------------------------------------------------- 准备真实跑所需的模块
const registry = await import(pathToFileURL(await bundle("src/lib/canvas/node-registry.ts", "p3-node-registry.mjs")).href);
const specs = await import(pathToFileURL(await bundle("src/constant/canvas.ts", "p3-node-specs.mjs")).href);
registry.registerNodeDefinitions(
    Object.entries(specs.NODE_SPECS).map(([type, spec]) => ({ type, title: spec.title, defaultSize: { width: spec.width, height: spec.height }, defaultMetadata: spec.metadata })),
    "builtin",
);

const workflow = await import(pathToFileURL(await bundle("src/lib/canvas/workflow-template.ts", "p3-workflow-template.mjs")).href);
const ids = await import(pathToFileURL(await bundle("src/lib/canvas/workflow-template-ids.ts", "p3-workflow-template-ids.mjs")).href);
const instancer = await import(pathToFileURL(await bundle("src/lib/canvas/workflow-instance.ts", "p3-workflow-instance.mjs")).href);
const ops = await import(pathToFileURL(await bundle("src/lib/canvas/canvas-agent-ops.ts", "p3-canvas-agent-ops.mjs")).href);
const lapianExec = await import(pathToFileURL(await bundle("src/lib/canvas/lapian-execute.ts", "p3-lapian-execute.mjs")).href);

// ---------------------------------------------------------------- B. 真实样本
const store = JSON.parse(readFileSync(storePath, "utf8"));
const projects = (store.state && store.state.projects) || [];
const project = projects.find((item) => item.id === projectKey) || projects.find((item) => item.title === projectKey);
check("B 找到真实样本项目", Boolean(project), `projects=${projects.map((item) => item.title).join(",")}`);
if (!project) {
    console.log(`\nP3 实例化探针：${pass} 通过 / ${fails.length} 失败`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(1);
}
const nodes = project.nodes || [];
const connections = project.connections || [];
const byId = new Map(nodes.map((node) => [node.id, node]));
const outgoing = (id) => connections.filter((connection) => connection.fromNodeId === id).map((connection) => connection.toNodeId);
const incoming = (id) => connections.filter((connection) => connection.toNodeId === id).map((connection) => connection.fromNodeId);

const lapians = nodes.filter((node) => node.type === "lapian" && node.metadata?.jobId);
const chainRoot = lapians.find((node) => nodes.some((candidate) => candidate.type === "config" && candidate.metadata?.generationMode === "text" && candidate.metadata?.lapianSourceId === node.id));
check("B 找到已跑通的拉片链根", Boolean(chainRoot), `lapians=${lapians.map((node) => node.id).join(",")}`);
if (!chainRoot) {
    console.log(`\nP3 实例化探针：${pass} 通过 / ${fails.length} 失败`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(1);
}
const lapianId = chainRoot.id;
const configText = nodes.find((node) => node.type === "config" && node.metadata?.generationMode === "text" && node.metadata?.lapianSourceId === lapianId);
const sourceVideo = incoming(lapianId).map((id) => byId.get(id)).find((node) => node && node.type === "video" && node.metadata?.storageKey);
const instruction = incoming(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text" && node.metadata?.promptTemplate);
const intent = incoming(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text" && node.metadata?.lapianIntent);
const finalText = outgoing(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text");
const configVideo = outgoing(finalText.id).map((id) => byId.get(id)).find((node) => node && node.type === "config" && node.metadata?.generationMode === "video");
const isFixedAsset = (node) => node && node.type === "image" && Boolean(node.metadata?.storageKey) && !node.metadata?.assetSlotSourceId && !node.metadata?.assetGraftForSlotId && !node.metadata?.assetGraftOfImageId;
const fixedAssets = [...new Set([...incoming(configText.id), ...incoming(configVideo.id)])].map((id) => byId.get(id)).filter(isFixedAsset);
const skeletonIds = [sourceVideo?.id, lapianId, instruction?.id, intent?.id, configText?.id, finalText?.id, configVideo?.id, ...fixedAssets.map((node) => node.id)].filter(Boolean);
check("B 骨架边界齐全", skeletonIds.length === 7 + fixedAssets.length, `边界=${skeletonIds.join(",")}`);

const skeleton = workflow.extractWorkflowTemplate({ sourceProject: project, selectedNodeIds: skeletonIds, name: `${project.title} · 骨架模板`, templateId: "pop3skeleton", createdAt: "2026-11-13T00:00:00.000Z" });

// 全量边界：骨架 + 真实资产位 + 其挂图 + 挂图对照（用于槽③扁平/镜像/嫁接/清单真跑）
const realSlots = nodes.filter((node) => node.metadata?.assetSlotSourceId === lapianId);
const slotImageIds = realSlots.map((slot) => slot.metadata?.attachedImageId).filter((id) => id && byId.has(id));
const grafts = nodes.filter((node) => node.metadata?.assetGraftForSlotId && realSlots.some((slot) => slot.id === node.metadata.assetGraftForSlotId));
const graftImageIds = grafts.map((node) => node.metadata?.assetGraftOfImageId).filter((id) => id && byId.has(id));
const fullIds = [...new Set([...skeletonIds, ...realSlots.map((slot) => slot.id), ...slotImageIds, ...grafts.map((node) => node.id), ...graftImageIds])];
const full = workflow.extractWorkflowTemplate({ sourceProject: project, selectedNodeIds: fullIds, name: `${project.title} · 全量模板`, templateId: "pop3full", createdAt: "2026-11-13T00:00:00.000Z" });
check("B 全量边界含真实资产位", realSlots.length > 0 && full.template.slots[2].targets.length === realSlots.length, `slots=${realSlots.length} targets=${full.template.slots[2].targets.length}`);
check("B 全量模板自足（无悬空引用）", workflow.validateTemplateProject(full.project.nodes, full.project.connections).length === 0, JSON.stringify(workflow.validateTemplateProject(full.project.nodes, full.project.connections).slice(0, 4)));
writeFileSync(path.join(outDir, "template-skeleton.json"), JSON.stringify(skeleton.project, null, 2), "utf8");
writeFileSync(path.join(outDir, "template-full.json"), JSON.stringify(full.project, null, 2), "utf8");

// ---------------------------------------------------------------- 夹具：素材组 / 网络 stub / 内存画布
const realLapianBody = String(byId.get(lapianId)?.metadata?.content || "");
check("B 原链拉片正文存在（用于「旧正文零残留」断言）", realLapianBody.length > 50, `len=${realLapianBody.length}`);

function materials(tag, extra = {}) {
    return {
        sourceVideo: { storageKey: `video:new-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/new-${tag}`, mimeType: "video/mp4", bytes: 5000 + tag.length, durationMs: 12000 },
        intentText: `把主体换成客户模特（${tag}）`,
        optionalSegments: tag === "A" ? "A,C" : "",
        generationParams: [{ nodeId: configText.id, values: { model: `default::${tag.toLowerCase()}-text-model` } }],
        ...extra,
    };
}

function makeFetchStub({ taskId, jobId, markdown, failUpload = false, failJob = false }) {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push({ url, method: String(init?.method || "GET").toUpperCase(), headers: init?.headers || {}, body: init?.body });
        const json = (payload) => ({ ok: true, status: 200, json: async () => payload });
        if (url === "/api/video/upload") return json(failUpload ? { error: "上传失败（探针注入）" } : { id: taskId });
        if (url === "/api/lapian/run") return json({ job: jobId });
        if (url === `/api/job/${jobId}`) return json(failJob ? { status: "error", error: "拆解失败（探针注入）" } : { status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(taskId)}`) return json({ markdown });
        throw new Error(`stub 未覆盖：${url}`);
    };
    return { fetchImpl, calls };
}

function makeCanvasStore(initial = { nodes: [], connections: [] }) {
    let snapshot = { projectId: project.id, title: project.title, nodes: initial.nodes, connections: initial.connections, selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (batch) => {
            const next = ops.applyCanvasAgentOps(snapshot, batch);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
    };
}

const baseInput = (templateSource, index, mats) => ({
    template: templateSource.template,
    templateNodes: templateSource.project.nodes,
    templateConnections: templateSource.project.connections,
    materials: mats,
    instanceIndex: index,
    origin: { x: 2000 * index, y: 0 },
    unnamedCaption: "未命名参考图",
});

// ---------------------------------------------------------------- C. 两次实例化零冲突
const skeletonFrozenBefore = JSON.stringify({ nodes: skeleton.project.nodes, connections: skeleton.project.connections, template: skeleton.template });
const planA = instancer.planWorkflowInstance(baseInput(skeleton, 1, materials("A")));
const planB = instancer.planWorkflowInstance(baseInput(skeleton, 2, materials("B")));
check("C 两次实例化 id 交集为空（节点）", ids.instanceIdSetsDisjoint(planA.nodes.map((node) => node.id), planB.nodes.map((node) => node.id)));
check("C 两次实例化 id 交集为空（连线）", ids.instanceIdSetsDisjoint(planA.connections.map((item) => item.id), planB.connections.map((item) => item.id)));
check("C 实例 id 与模板 id 零交集", !planA.nodes.some((node) => skeleton.project.nodes.some((templateNode) => templateNode.id === node.id)));
check("C 拓扑逐边同构（F1）", planA.topologyDigest === skeleton.template.frozen.topology.digest && planA.connections.length === skeleton.project.connections.length);
check("C 两次实例化的拓扑 digest 各自等于冻结值（B 亦然）", planB.topologyDigest === skeleton.template.frozen.topology.digest);
check("C 模板零改动（两次实例化前后 JSON 全等）", JSON.stringify({ nodes: skeleton.project.nodes, connections: skeleton.project.connections, template: skeleton.template }) === skeletonFrozenBefore);

const idsA = new Set(planA.nodes.map((node) => node.id));
const tokensOf = (plan) => plan.nodes.flatMap((node) => workflow.composerTokenIds(String(node.metadata?.composerContent || "")));
const leaked = [];
for (const node of planB.nodes) {
    for (const token of workflow.composerTokenIds(String(node.metadata?.composerContent || ""))) if (idsA.has(token)) leaked.push(`token:${token}`);
    for (const value of [node.metadata?.lapianSourceId, node.metadata?.assetSlotSourceId, node.metadata?.attachedImageId, node.metadata?.assetSlot?.attachedImageId, node.metadata?.assetGraftOfImageId, node.metadata?.assetGraftForSlotId, node.metadata?.groupId]) {
        if (value && idsA.has(String(value))) leaked.push(`meta:${value}`);
    }
}
check("C 实例 2 不引用实例 1 任何节点", leaked.length === 0, leaked.slice(0, 6).join(","));

// 七类引用（骨架侧：token / 来源 / 连线端点）
const tokenList = tokensOf(planA);
check("C 七类①：组装串 token 全部指向本实例节点", tokenList.length > 0 && tokenList.every((token) => idsA.has(token)), `tokens=${tokenList.length}`);
const cfgA = planA.nodes.find((node) => node.id === planA.nodeIdMap[configText.id]);
check("C 七类⑤：Config→拉片 lapianSourceId 重绑", cfgA.metadata?.lapianSourceId === planA.nodeIdMap[lapianId], `${cfgA.metadata?.lapianSourceId}`);
check("C 七类⑦：连线端点全部落在本实例节点", planA.connections.every((edge) => idsA.has(edge.fromNodeId) && idsA.has(edge.toNodeId)));

// ---------------------------------------------------------------- D. 槽值 + 幂等落库
const sourceNodeA = planA.nodes.find((node) => node.id === planA.nodeIdMap[sourceVideo.id]);
check("D 槽①：源视频写新材料 storageKey/content（旧字节无残留）", sourceNodeA.metadata?.storageKey === "video:new-A" && sourceNodeA.metadata?.content === "blob:http://127.0.0.1:3000/new-A" && sourceNodeA.metadata?.taskId === undefined);
const lapianNodeA = planA.nodes.find((node) => node.id === planA.nodeIdMap[lapianId]);
check("D 拉片节点模板运行态已清空（等受控入口回填）", lapianNodeA.metadata?.taskId === undefined && lapianNodeA.metadata?.jobId === undefined && lapianNodeA.metadata?.content === undefined);
const intentA = planA.nodes.find((node) => node.id === planA.nodeIdMap[intent.id]);
check("D 槽②：意图正文写入", intentA.metadata?.content === "把主体换成客户模特（A）");
const cfgvA = planA.nodes.find((node) => node.id === planA.nodeIdMap[configVideo.id]);
check("D 槽④：生成参数只写值（config text 模型覆盖，video 参数保持模板值）", cfgA.metadata?.model === "default::a-text-model" && cfgvA.metadata?.generationMode === "video" && cfgvA.metadata?.model === byId.get(configVideo.id).metadata.model);
check("D 槽⑤：可选段写入", lapianNodeA.metadata?.lapianOptional === "A,C");

const canvas = makeCanvasStore();
const firstApply = await canvas.apply(planA.ops);
check("D 首放：节点/连线数 = 计划数", canvas.nodes().length === planA.nodes.length && canvas.connections().length === planA.connections.length, `${canvas.nodes().length}/${planA.nodes.length}`);
const secondApply = await canvas.apply(planA.ops);
check("D 重放：节点数不翻倍、全部幂等跳过", canvas.nodes().length === planA.nodes.length && secondApply.diagnostics.addedNodeIds.length === 0 && secondApply.diagnostics.idempotentNodeIds.length === planA.nodes.length, `added=${secondApply.diagnostics.addedNodeIds.length} skipped=${secondApply.diagnostics.idempotentNodeIds.length}`);
const conflictOps = planA.ops.map((op) => (op.type === "add_node" && op.id === planA.lapianNodeId ? { ...op, metadata: { ...(op.metadata || {}), taskId: "冲突任务" } } : op));
let conflictRejected = false;
try {
    await canvas.apply(conflictOps);
} catch (error) {
    conflictRejected = error instanceof ops.CanvasAgentOpConflictError;
}
check("D 同 id 形态不一致 → 冲突报错（不静默 append）", conflictRejected && canvas.nodes().length === planA.nodes.length);

// ---------------------------------------------------------------- E. 换源视频实例：受控拉片入口 + 本次正文
const markdownA = "# 本次拉片正文 · A · 唯一标记 7f3a\n## 一 剧情\n客户模特转身";
const stubA = makeFetchStub({ taskId: "task-new-A", jobId: "job-new-A", markdown: markdownA });
const canvasReal = makeCanvasStore();
const createdA = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 1, materials("A")), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["new-video-bytes"], { type: "video/mp4" }), fetch: stubA.fetchImpl, applyOps: canvasReal.apply, existingNodes: canvasReal.nodes },
);
check("E 实例化成功（created）", createdA.status === "created", JSON.stringify(createdA.incomplete || {}));
check("E 触发上传：X-Filename/octet-stream 合同", stubA.calls.some((call) => call.url === "/api/video/upload" && call.method === "POST" && call.headers["Content-Type"] === "application/octet-stream" && call.headers["X-Filename"] === encodeURIComponent("新素材-A.mp4")));
const runCall = stubA.calls.find((call) => call.url === "/api/lapian/run");
const runBody = runCall ? JSON.parse(String(runCall.body)) : {};
check("E 触发拉片：请求体 {id,intent}（未配模型不发半截请求）", runBody.id === "task-new-A" && runBody.intent === "把主体换成客户模特（A）" && !("model" in runBody), JSON.stringify(runBody));
const lapianNodeReal = canvasReal.nodes().find((node) => node.id === createdA.plan.lapianNodeId);
check("E 回填**本次**正文", lapianNodeReal.metadata?.content === markdownA, `content=${String(lapianNodeReal.metadata?.content || "").slice(0, 40)}`);
check("E 回填 taskId/jobId = 本次任务", lapianNodeReal.metadata?.taskId === "task-new-A" && lapianNodeReal.metadata?.jobId === "job-new-A");
check("E 旧正文零残留（原链正文不进实例）", realLapianBody.length > 0 && !JSON.stringify(canvasReal.nodes()).includes(realLapianBody.slice(0, 40)));
check("E 零付费创建：提交的 ops 无 run_generation", (createdA.appliedOps || []).every((op) => op.type !== "run_generation"));

// 实例 B：另一组素材 → 另一条任务号与正文，两条实例互不引用
const markdownB = "# 本次拉片正文 · B · 唯一标记 91c2\n## 一 剧情\n客户模特正面";
const stubB = makeFetchStub({ taskId: "task-new-B", jobId: "job-new-B", markdown: markdownB });
const createdB = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 2, materials("B")), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["new-video-bytes-b"], { type: "video/mp4" }), fetch: stubB.fetchImpl, applyOps: canvasReal.apply, existingNodes: canvasReal.nodes },
);
check("E 第二组实例化成功", createdB.status === "created", JSON.stringify(createdB.incomplete || {}));
const idsB = new Set(createdB.plan.nodes.map((node) => node.id));
check("E 两组实例 id 零交集（含连线）", [...createdA.plan.nodes.map((node) => node.id), ...createdA.plan.connections.map((item) => item.id)].every((id) => !idsB.has(id)));
const lapianNodeB = canvasReal.nodes().find((node) => node.id === createdB.plan.lapianNodeId);
check("E 第二组回填自己那份正文", lapianNodeB.metadata?.content === markdownB && lapianNodeB.metadata?.content !== markdownA);
check("E 画布累计节点数 = 两实例之和（无覆盖/无翻倍）", canvasReal.nodes().length === planA.nodes.length + planB.nodes.length, `${canvasReal.nodes().length} vs ${planA.nodes.length + planB.nodes.length}`);

// 幂等重放（同实例请求再来一次）：already_present，零网络调用（不重复花钱）
const stubReplay = makeFetchStub({ taskId: "task-should-not-be-used", jobId: "job-should-not-be-used", markdown: "# 不该出现" });
const replay = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 1, materials("A")), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["x"]), fetch: stubReplay.fetchImpl, applyOps: canvasReal.apply, existingNodes: canvasReal.nodes },
);
check("E 重放 → already_present 且零网络调用（不重复拉片）", replay.status === "already_present" && stubReplay.calls.length === 0, `status=${replay.status} calls=${stubReplay.calls.length}`);
check("E 重放后画布节点数不变", canvasReal.nodes().length === planA.nodes.length + planB.nodes.length);

// ---------------------------------------------------------------- F. 失败注入：无半条链
const failingCanvas = makeCanvasStore();
const missingMaterial = await instancer
    .materializeWorkflowInstance(
        { ...baseInput(skeleton, 3, { sourceVideo: { storageKey: "" } }), lapianConfig: {}, contentWait: { sleep: async () => {} } },
        { getBlob: async () => new Blob(["x"]), fetch: stubA.fetchImpl, applyOps: failingCanvas.apply, existingNodes: failingCanvas.nodes },
    )
    .then(() => null)
    .catch((error) => error);
check("F 素材缺失 → 计划期拒绝（material_missing）", missingMaterial && missingMaterial.code === "material_missing", String(missingMaterial && missingMaterial.code));
check("F 素材缺失 → 画布零写入", failingCanvas.nodes().length === 0);

const stubUploadFail = makeFetchStub({ taskId: "t", jobId: "j", markdown: "x", failUpload: true });
const uploadFail = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 3, materials("C")), lapianConfig: {}, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["x"]), fetch: stubUploadFail.fetchImpl, applyOps: failingCanvas.apply, existingNodes: failingCanvas.nodes },
);
check("F 上传失败 → incomplete(lapian_chain) 且画布零写入", uploadFail.status === "incomplete" && uploadFail.incomplete.stage === "lapian_chain" && failingCanvas.nodes().length === 0, `${uploadFail.status}/${uploadFail.incomplete?.stage}`);
check("F 上传失败后未发起拉片（不产生拆解费用）", !stubUploadFail.calls.some((call) => call.url === "/api/lapian/run"));

const stubJobFail = makeFetchStub({ taskId: "task-fail", jobId: "job-fail", markdown: "x", failJob: true });
const jobFailCanvas = makeCanvasStore();
const jobFail = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 4, materials("D")), lapianConfig: {}, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["x"]), fetch: stubJobFail.fetchImpl, applyOps: jobFailCanvas.apply, existingNodes: jobFailCanvas.nodes },
);
check("F 正文没回来 → incomplete(lapian_content)，显式标记不冒充完整", jobFail.status === "incomplete" && jobFail.incomplete.stage === "lapian_content" && jobFail.incomplete.rolledBack === false);
const failLapianNode = jobFailCanvas.nodes().find((node) => node.id === jobFail.plan.lapianNodeId);
check("F incomplete 实例的拉片节点带 taskId 但正文为空（可重查续）", failLapianNode.metadata?.taskId === "task-fail" && failLapianNode.metadata?.content === undefined);

const rollbackCanvas = makeCanvasStore();
let applyCalls = 0;
const applyFail = await instancer.materializeWorkflowInstance(
    { ...baseInput(skeleton, 5, materials("E")), lapianConfig: {}, contentWait: { sleep: async () => {} } },
    {
        getBlob: async () => new Blob(["x"]),
        fetch: makeFetchStub({ taskId: "task-rb", jobId: "job-rb", markdown: "# rb" }).fetchImpl,
        applyOps: async (batch) => {
            applyCalls += 1;
            if (applyCalls === 1) throw new Error("落库失败（探针注入）");
            await rollbackCanvas.apply(batch);
        },
        existingNodes: rollbackCanvas.nodes,
    },
);
check("F 落库失败 → incomplete(apply_ops) + 已回滚", applyFail.status === "incomplete" && applyFail.incomplete.stage === "apply_ops" && applyFail.incomplete.rolledBack === true, `${applyFail.status}/${applyFail.incomplete?.stage}`);
check("F 回滚后画布零残留（节点/连线）", rollbackCanvas.nodes().length === 0 && rollbackCanvas.connections().length === 0);

// ---------------------------------------------------------------- G. 全量模板：槽③挂图真跑（扁平+镜像+清单+嫁接）
const fullImage = fixedAssets[0];
const attachSlot = full.template.slots[2].targets[0];
const planAttach = instancer.planWorkflowInstance(baseInput(full, 1, materials("F", { assetAttach: [{ slotNodeId: attachSlot.nodeId, imageNodeId: fullImage.id, caption: "客户模特" }] })));
const slotNew = planAttach.nodes.find((node) => node.id === planAttach.nodeIdMap[attachSlot.nodeId]);
const imageNew = planAttach.nodeIdMap[fullImage.id];
check("G 槽③：扁平真源与 assetSlot 镜像同批双写", slotNew.metadata?.attachedImageId === imageNew && slotNew.metadata?.assetSlot?.attachedImageId === imageNew, `${slotNew.metadata?.attachedImageId}/${slotNew.metadata?.assetSlot?.attachedImageId}`);
check("G 槽③：短说明写入（attachedImageDesc + 镜像 caption）", slotNew.metadata?.attachedImageDesc === "客户模特" && slotNew.metadata?.assetSlot?.caption === "客户模特");
const cfgFull = planAttach.nodes.find((node) => node.id === planAttach.nodeIdMap[configText.id]);
check("G 槽③：清单行按本实例挂图重算（token 指向新图 id + 短说明）", String(cfgFull.metadata?.composerContent || "").includes(`本次参考图：@[node:${imageNew}]=客户模特`), String(cfgFull.metadata?.composerContent || "").slice(-120));
const graftNew = planAttach.nodes.find((node) => node.metadata?.assetGraftForSlotId === slotNew.id);
check("G 槽③：挂图对照节点 + 双向引用", Boolean(graftNew) && graftNew.metadata?.assetGraftOfImageId === imageNew);
check("G 槽③：伴生边（图→Config）补齐", planAttach.connections.some((edge) => edge.fromNodeId === imageNew && edge.toNodeId === cfgFull.id));
const templateGraft = full.project.nodes.find((node) => node.metadata?.assetGraftForSlotId && node.id === grafts[0]?.id);
const planGraft = templateGraft ? planAttach.nodes.find((node) => node.id === planAttach.nodeIdMap[templateGraft.id]) : undefined;
check("G 七类⑥：模板自带对照图的嫁接引用重绑到本实例 id", !templateGraft || (planGraft.metadata?.assetGraftForSlotId === slotNew.id && planGraft.metadata?.assetGraftOfImageId === imageNew), `${planGraft?.metadata?.assetGraftForSlotId} / ${planGraft?.metadata?.assetGraftOfImageId}`);
check("G 槽③：派生槽模板（骨架模板）挂图被拒（P4 范围）", (() => {
    try {
        instancer.planWorkflowInstance(baseInput(skeleton, 1, materials("G", { assetAttach: [{ slotNodeId: realSlots[0].id, imageNodeId: fullImage.id }] })));
        return false;
    } catch (error) {
        return error.code === "asset_slot_derivation_required";
    }
})());

// ---------------------------------------------------------------- 汇总落盘
const summary = {
    checkedAt: new Date().toISOString(),
    store: storePath,
    project: { id: project.id, title: project.title, nodes: nodes.length },
    template: {
        skeleton: { templateId: skeleton.template.templateId, nodes: skeleton.project.nodes.length, connections: skeleton.project.connections.length, externalReferences: skeleton.template.externalReferences.length },
        full: { templateId: full.template.templateId, nodes: full.project.nodes.length, connections: full.project.connections.length, slots: full.template.slots[2].targets.length },
    },
    instanceA: { nodes: planA.nodes.length, connections: planA.connections.length, templateId: planA.templateId, instanceIndex: planA.instanceIndex, lapian: createdA.lapian && { taskId: createdA.lapian.taskId, jobId: createdA.lapian.jobId, contentLength: createdA.lapian.content.length } },
    instanceB: { nodes: planB.nodes.length, connections: planB.connections.length, lapian: createdB.lapian && { taskId: createdB.lapian.taskId, contentLength: createdB.lapian.content.length } },
    networkCalls: { A: stubA.calls.map((call) => `${call.method} ${call.url}`), B: stubB.calls.map((call) => `${call.method} ${call.url}`), replay: stubReplay.calls.length },
    pass,
    fails,
    results,
};
writeFileSync(path.join(outDir, "probe-summary.json"), JSON.stringify(summary, null, 2), "utf8");
writeFileSync(path.join(outDir, "plan-instance-a.json"), JSON.stringify({ nodes: planA.nodes, connections: planA.connections, slotWrites: planA.slotWrites }, null, 2), "utf8");
writeFileSync(path.join(outDir, "plan-instance-full-attach.json"), JSON.stringify({ nodes: planAttach.nodes, connections: planAttach.connections, assetAttaches: planAttach.assetAttaches }, null, 2), "utf8");

console.log(`\nP3 实例化探针：${pass} 通过 / ${fails.length} 失败`);
fails.forEach((item) => console.log(`  FAIL ${item}`));
console.log(`输出目录：${outDir}`);
process.exit(fails.length ? 1 : 0);
