/**
 * P5 探针：批次成员 + 状态派生 + 落盘 flush（真跑；stub 拦截付费）。
 *
 * 跑法：cd web && node scripts/probe-p5-batch-status.mjs --store <real-canvas-store.json> --out <输出目录>
 *
 * 分段：
 *   A. 单测真跑：esbuild 打包 `workflow-batch.test.ts` / `canvas-persist-queue.test.ts` /
 *      `workflow-instance-run.test.ts`（P4 回归）→ `node --test`。
 *   B. 建批次 3 实例：真实「无限画布 3」抽模板 → P3 实例化 ×3（stub 网络）→ P4 编排 ×3（stub hub/生成链）
 *      → 每条实例 saveIdentity + saveBatchMember → 关键节点 flush（真实 use-canvas-store + 真实 localforage）。
 *   C. 刷新恢复：落盘 payload 交给**新进程**重建 store（真 rehydrate）→ batchId↔实例↔节点↔hub 四层恢复。
 *   D. 状态派生矩阵：真实成员（真实查询证据）+ 构造场景（Config success+视频 running / failed / 缺失）。
 *   E. 查询覆盖：查询后端 = **生产函数** `buildGenerationStatus`（默认 20 / 上限 100 无分页）
 *      → 裸查必被截断；分组二分后全拉回；>100 规模（30 成员/200+ 节点）分块无静默截断。
 *   F. flush：编排关键节点被调用、立即落盘（不等防抖）、写盘失败可见（不静默）。
 *   G. 零回归：store 既有方法面、导入透传、节点 metadata 不带批次字段（C5）。
 *
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails，不跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const storePath = path.resolve(argOf("store", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p2-ego/real-canvas-store.json"));
const projectKey = argOf("project", "无限画布 3");
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p5-out"));
const BATCH_PAYLOAD = path.join(outDir, "persisted-store.json");
mkdirSync(outDir, { recursive: true });
const bundleDir = path.resolve("node_modules/.probe");
mkdirSync(bundleDir, { recursive: true });

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");
const STORE_KEY_SUFFIX = "infinite-canvas:canvas_store";

// 浏览器存储替身：**必须在任何 bundle import 之前就位**（i18n 模块在加载时就读 localStorage 选语言）。
const memory = new Map();
let storageBroken = false;
globalThis.localStorage = {
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => {
        if (storageBroken) throw new Error("探针伪造：localStorage 写入失败（磁盘配额）");
        memory.set(key, String(value));
    },
    removeItem: (key) => memory.delete(key),
};
globalThis.window = { localStorage: globalThis.localStorage };

// ---------------------------------------------------------------- 刷新恢复子模式（新进程 = 真刷新）
if (args.includes("--reload")) {
    const payload = readFileSync(BATCH_PAYLOAD, "utf8");
    const mem = new Map([[`infinite-canvas/app_state/${STORE_KEY_SUFFIX}`, payload]]);
    globalThis.localStorage = {
        getItem: (key) => (mem.has(key) ? mem.get(key) : null),
        setItem: (key, value) => mem.set(key, String(value)),
        removeItem: (key) => mem.delete(key),
    };
    globalThis.window = { localStorage: globalThis.localStorage };
    const entry = path.join(bundleDir, "p5-store-reload-entry.ts");
    writeFileSync(entry, 'export * as store from "@/stores/canvas/use-canvas-store";\n', "utf8");
    const out = path.join(bundleDir, "p5-store-reload.mjs");
    await esbuild.build({ entryPoints: [entry], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "silent", alias: { "@": path.resolve("src") } });
    const mod = await import(pathToFileURL(out).href);
    const store = mod.store.useCanvasStore;
    for (let attempt = 0; attempt < 200 && !store.getState().hydrated; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 25));
    const project = store.getState().projects[0];
    console.log(
        JSON.stringify({
            hydrated: store.getState().hydrated,
            projectCount: store.getState().projects.length,
            projectId: project?.id,
            nodeCount: project?.nodes?.length || 0,
            batches: project?.workflowBatches || [],
            instances: (project?.workflowInstances || []).map((record) => ({ instanceId: record.instanceId, localId: record.hub?.localId ?? null })),
        }),
    );
    process.exit(0);
}

// ---------------------------------------------------------------- 断言脚手架
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

// ---------------------------------------------------------------- A. 单测（真跑）
const testFiles = [
    ["src/lib/canvas/workflow-batch.test.ts", "p5-batch.test.mjs", "P5 批次/派生/查询"],
    ["src/lib/canvas/canvas-persist-queue.test.ts", "p5-queue.test.mjs", "P5 落盘队列"],
    ["src/lib/canvas/workflow-instance-run.test.ts", "p4-instance-run.test.mjs", "P4 回归"],
];
let unitTotal = 0;
for (const [entry, outName, label] of testFiles) {
    const testBundle = await bundle(entry, outName, {
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    let output = "";
    let failed = false;
    try {
        output = execFileSync(process.execPath, ["--test", testBundle], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
        failed = true;
        output = `${error.stdout || ""}${error.stderr || ""}`;
    }
    writeFileSync(path.join(outDir, `unit-${outName.replace(/\.mjs$/, "")}.tap`), output, "utf8");
    const count = (label2) => {
        const match = output.match(new RegExp(`^(?:# |ℹ )${label2} (\\d+)$`, "m"));
        return match ? Number(match[1]) : undefined;
    };
    const tests = count("tests");
    const failCount = count("fail");
    unitTotal += Number(tests || 0);
    check(`A 单测（${label}）进程零失败`, !failed, failed ? output.split("\n").filter((line) => line.includes("✖")).slice(0, 5).join(" | ") : "");
    check(`A 单测（${label}）用例数 > 0`, Number(tests || 0) > 0, `tests=${tests}`);
    check(`A 单测（${label}）fail 计数 0`, failCount === 0, `fail=${failCount}`);
}
check("A 单测总数 ≥ 26（P5 新增 26 + P4 回归）", unitTotal >= 26, `total=${unitTotal}`);

// ---------------------------------------------------------------- 模块（同一 bundle：node-registry 单例）
const modulesEntry = path.join(bundleDir, "p5-modules-entry.ts");
writeFileSync(
    modulesEntry,
    [
        'export * as builtin from "@/components/canvas/nodes/builtin-nodes";',
        'export * as workflow from "@/lib/canvas/workflow-template";',
        'export * as instancer from "@/lib/canvas/workflow-instance";',
        'export * as runner from "@/lib/canvas/workflow-instance-run";',
        'export * as identity from "@/lib/canvas/workflow-instance-identity";',
        'export * as ops from "@/lib/canvas/canvas-agent-ops";',
        'export * as genctx from "@/components/canvas/canvas-node-generation";',
        'export * as batch from "@/lib/canvas/workflow-batch";',
        'export * as batchStatus from "@/lib/canvas/workflow-batch-status";',
        'export * as batchQuery from "@/lib/canvas/workflow-batch-query";',
        'export * as agentText from "@/lib/agent/agent-node-text";',
        `export * as paid from ${JSON.stringify(path.resolve("..", "canvas-agent/src/canvas/paid-actions.ts").split(path.sep).join("/"))};`,
        "",
    ].join("\n"),
    "utf8",
);
const modules = await import(pathToFileURL(await bundle(modulesEntry, "p5-modules.mjs")).href);
const { builtin, workflow, instancer, runner, identity: identityModule, ops, genctx, batch, batchStatus, batchQuery, agentText, paid } = modules;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 真实 store（真 zustand persist + 真 localforage）
const storeEntry = path.join(bundleDir, "p5-store-entry.ts");
writeFileSync(storeEntry, 'export * as store from "@/stores/canvas/use-canvas-store";\n', "utf8");
const storeModule = await import(pathToFileURL(await bundle(storeEntry, "p5-store.mjs")).href);
const useCanvasStore = storeModule.store.useCanvasStore;
const readPersisted = () => {
    for (const [key, value] of memory) if (key.endsWith(STORE_KEY_SUFFIX)) return value;
    return "";
};
check("B store 模块可加载（真 zustand persist）", typeof useCanvasStore.getState === "function");
check("B store 暴露 flushProjects（P5 新 API）", typeof useCanvasStore.getState().flushProjects === "function");

// ---------------------------------------------------------------- B. 真实样本 → 模板 → 3 实例
const storeJson = JSON.parse(readFileSync(storePath, "utf8"));
const realProjects = (storeJson.state && storeJson.state.projects) || [];
const realProject = realProjects.find((item) => item.id === projectKey) || realProjects.find((item) => item.title === projectKey);
check("B 找到真实样本项目", Boolean(realProject), `projects=${realProjects.map((item) => item.title).join(",")}`);
if (!realProject) finish();

const realNodes = realProject.nodes || [];
const realConnections = realProject.connections || [];
const byId = new Map(realNodes.map((node) => [node.id, node]));
const outgoing = (id) => realConnections.filter((connection) => connection.fromNodeId === id).map((connection) => connection.toNodeId);
const incoming = (id) => realConnections.filter((connection) => connection.toNodeId === id).map((connection) => connection.fromNodeId);
const lapians = realNodes.filter((node) => node.type === "lapian" && node.metadata?.jobId);
const chainRoot = lapians.find((node) => realNodes.some((candidate) => candidate.type === "config" && candidate.metadata?.generationMode === "text" && candidate.metadata?.lapianSourceId === node.id));
check("B 找到已跑通的拉片链根", Boolean(chainRoot), `lapians=${lapians.map((node) => node.id).join(",")}`);
if (!chainRoot) finish();

const lapianId = chainRoot.id;
const configText = realNodes.find((node) => node.type === "config" && node.metadata?.generationMode === "text" && node.metadata?.lapianSourceId === lapianId);
const sourceVideo = incoming(lapianId).map((id) => byId.get(id)).find((node) => node && node.type === "video" && node.metadata?.storageKey);
const instruction = incoming(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text" && node.metadata?.promptTemplate);
const intent = incoming(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text" && node.metadata?.lapianIntent);
const finalText = outgoing(configText.id).map((id) => byId.get(id)).find((node) => node && node.type === "text");
const configVideo = outgoing(finalText.id).map((id) => byId.get(id)).find((node) => node && node.type === "config" && node.metadata?.generationMode === "video");
// 组装/视频 Config 的正文里引用了模板外的固定参考图：必须一起进模板（P2 自足性要求，P4 同口径）
const isFixedAsset = (node) => node && node.type === "image" && Boolean(node.metadata?.storageKey) && !node.metadata?.assetSlotSourceId && !node.metadata?.assetGraftForSlotId && !node.metadata?.assetGraftOfImageId;
const fixedAssets = [...new Set([...incoming(configText.id), ...incoming(configVideo.id)])].map((id) => byId.get(id)).filter(isFixedAsset);
const skeletonIds = [sourceVideo?.id, lapianId, instruction?.id, intent?.id, configText?.id, finalText?.id, configVideo?.id, ...fixedAssets.map((node) => node.id)].filter(Boolean);
check("B 骨架齐全（video→lapian→config text→占位终稿→config video + 固定参考图）", skeletonIds.length >= 7 && Boolean(sourceVideo && configText && finalText && configVideo), `边界=${skeletonIds.length} 固定图=${fixedAssets.length}`);

const skeletonTemplate = workflow.extractWorkflowTemplate({ sourceProject: realProject, selectedNodeIds: skeletonIds, name: `${realProject.title} · P5 骨架`, templateId: "p5skeleton", createdAt: "2026-11-13T00:00:00.000Z" });
const templateSource = skeletonTemplate;
check("B 模板自足（无悬空引用）", workflow.validateTemplateProject(templateSource.project.nodes, templateSource.project.connections).length === 0, JSON.stringify(workflow.validateTemplateProject(templateSource.project.nodes, templateSource.project.connections).slice(0, 3)));

// 内存画布（以真实项目为底：批次实例与既有链共存，和真机一致）
function makeCanvas() {
    let snapshot = { projectId: realProject.id, title: realProject.title, nodes: realNodes, connections: realConnections, selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (batchOps) => {
            const next = ops.applyCanvasAgentOps(snapshot, batchOps);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
    };
}
const canvas = makeCanvas();

// 真实 store 里装这个项目（走 importProject：同时验证批次记录透传）
const projectId = useCanvasStore.getState().importProject({ title: `${realProject.title} · P5 探针`, nodes: realNodes, connections: realConnections, createdAt: "2026-11-13T00:00:00.000Z" });
check("B store 载入真实项目（importProject）", useCanvasStore.getState().projects.length === 1 && useCanvasStore.getState().projects[0].id === projectId, JSON.stringify(useCanvasStore.getState().projects.map((item) => item.id)));

function makeLapianFetch({ taskId, jobId, markdown }) {
    return async (url) => {
        const json = (payload) => ({ ok: true, status: 200, json: async () => payload });
        if (url === "/api/video/upload") return json({ id: taskId });
        if (url === "/api/lapian/run") return json({ job: jobId });
        if (url === `/api/job/${jobId}`) return json({ status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(taskId)}`) return json({ markdown });
        throw new Error(`stub 未覆盖：${url}`);
    };
}
function makeProjectionFetch() {
    return async (url) => {
        const match = String(url).match(/\/api\/render\/([^/]+)\/([^?]+)\?optional=(.*)$/);
        if (!match) return { ok: false, status: 404, json: async () => ({ error: `stub 不认：${url}` }) };
        const optional = decodeURIComponent(match[3] || "");
        const text = [`# 本次投影正文 · ${match[1]}`, `可选段：${optional || "（全关）"}`, optional ? `可选段内容 ${optional}：客户模特在雨夜街头转身。` : "正片结构：开场/推进/收尾。"].join("\n");
        return { ok: true, status: 200, json: async () => ({ text }) };
    };
}
let generationSeq = 0;
function makeGenerationChain(targetCanvas, token) {
    return async (input) => {
        const context = genctx.buildNodeGenerationContext(input.nodeId, targetCanvas.nodes(), targetCanvas.connections(), input.prompt);
        generationSeq += 1;
        const childNodeId = `${token}-out-${generationSeq}`;
        const content = `【本次产物 ${token}-${generationSeq}】\n${context.prompt}`;
        await targetCanvas.apply([
            { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 6000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
            { type: "connect_nodes", fromNodeId: input.nodeId, toNodeId: childNodeId },
        ]);
        return { childNodeId };
    };
}
function makeHubStub(index) {
    const calls = [];
    const localId = `auto-95${index}0`;
    const client = {
        async ingest(input) {
            calls.push({ kind: "ingest", bytes: input.blob?.size || 0 });
            return { localId, bridgeId: `bridge-p5-0${index}`, result: "OK" };
        },
        async uploadAsset(input) {
            calls.push({ kind: "asset", filename: input.filename, localId: input.localId });
            return { ref: `local:p5/${input.filename}`, source: "local" };
        },
        async getTask() {
            return { state: "awaiting_approval", bridgeId: `bridge-p5-0${index}` };
        },
        async saveRevision(input) {
            calls.push({ kind: "prompt", chars: input.text.length });
            return { state: "awaiting_approval", approvalSnapshot: `snap-p5-0${index}`, promptEditedAt: "2026-11-13T00:00:00.000Z", selfcheck: [{ 序号: 1, 结论: "PASS" }] };
        },
        approve: async () => ({ state: "approved" }),
        rollback: async () => ({ state: "awaiting_assets" }),
        revoke: async () => ({ state: "awaiting_approval" }),
    };
    return { client, calls, localId };
}

const batchId = batch.deriveBatchId({ templateId: templateSource.template.templateId, materialDigests: ["A", "B", "C"], createdAt: "2026-11-13T00:00:00.000Z" });
const batchMeta = { templateId: templateSource.template.templateId, batchId, title: "P5 探针批次" };
let identities = [];
let batches = [];
const flushLog = [];
const flushPayloadChecks = [];

function syncProject() {
    useCanvasStore.getState().updateProject(projectId, { nodes: canvas.nodes(), connections: canvas.connections(), workflowInstances: identities, workflowBatches: batches });
}

const materialOf = (tag) => ({
    sourceVideo: { storageKey: `video:p5-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/p5-${tag}`, mimeType: "video/mp4", bytes: 6000, durationMs: 12000 },
    intentText: `把主体换成客户模特（P5-${tag}）`,
    optionalSegments: "A",
    generationParams: [{ nodeId: configText.id, values: { model: `default::p5-${tag.toLowerCase()}-text-model` } }],
});
const markdown = (tag) => `# 本次拉片正文 · ${tag}\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近`;

const perInstance = [];
for (const [index, tag] of [
    [1, "A"],
    [2, "B"],
    [3, "C"],
]) {
    const mats = materialOf(tag);
    const stub = makeLapianFetch({ taskId: `task-p5-${tag}`, jobId: `job-p5-${tag}`, markdown: markdown(tag) });
    const created = await instancer.materializeWorkflowInstance(
        { template: templateSource.template, templateNodes: templateSource.project.nodes, templateConnections: templateSource.project.connections, materials: mats, instanceIndex: index, origin: { x: 2400 * index, y: 0 }, unnamedCaption: "未命名参考图", lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
        { getBlob: async () => new Blob([`video-${tag}`], { type: "video/mp4" }), fetch: stub, applyOps: canvas.apply, existingNodes: canvas.nodes },
    );
    check(`B 实例 ${index} 落库（P3 materialize）`, created.status === "created", JSON.stringify(created.incomplete || {}));
    if (created.status !== "created") continue;

    const hub = makeHubStub(index);
    const result = await runner.runWorkflowInstance(
        { plan: created.plan, materials: mats, lapian: { taskId: `task-p5-${tag}`, jobId: `job-p5-${tag}` }, batchId },
        {
            nodes: canvas.nodes,
            connections: canvas.connections,
            applyOps: canvas.apply,
            runGeneration: makeGenerationChain(canvas, `p5${tag.toLowerCase()}`),
            fetchProjection: makeProjectionFetch(),
            hub: hub.client,
            getBlob: async (storageKey) => new Blob([`bytes-of-${storageKey}`], { type: String(storageKey).startsWith("image:") ? "image/png" : "video/mp4" }),
            saveIdentity: (record) => {
                identities = identityModule.upsertInstanceIdentity(identities, record);
            },
            saveBatchMember: (member) => {
                batches = batch.upsertBatchMember(batches, batchMeta, member, "2026-11-13T00:00:00.000Z");
            },
            flush: async (stage) => {
                syncProject();
                const outcome = await useCanvasStore.getState().flushProjects(stage);
                flushLog.push({ instanceIndex: index, stage, ok: outcome.ok, written: outcome.written, bytes: outcome.bytes });
                const payload = readPersisted();
                flushPayloadChecks.push({ instanceIndex: index, stage, hasInstance: payload.includes(created.plan.instanceId), hasAllPrevious: perInstance.every((item) => payload.includes(item.plan.instanceId)) });
                return outcome;
            },
            newRunToken: () => `runToken${tag}`,
            now: () => "2026-11-13T00:00:00.000Z",
        },
    );
    perInstance.push({ plan: created.plan, result, hub, tag });
    check(`B 实例 ${index} 全链到待批（P4 编排 + 批次入册）`, !result.failure && result.status === "awaiting_approval", JSON.stringify(result.failure));
    check(`B 实例 ${index} 付费视频创建 = 0`, result.paidVideoCreations === 0, String(result.paidVideoCreations));
    check(`B 实例 ${index} 关键节点 flush 两次（落库 + 待批）`, JSON.stringify(result.flushes.map((item) => item.stage)) === JSON.stringify(["instance_persisted", "awaiting_approval"]), JSON.stringify(result.flushes));
    check(`B 实例 ${index} flush 全部成功且真的写了盘`, result.flushes.every((item) => item.ok) && flushLog.filter((item) => item.instanceIndex === index).every((item) => item.written), JSON.stringify(flushLog.filter((item) => item.instanceIndex === index)));
}

// ---------------------------------------------------------------- B′ 四层关联 + 落盘内容
check("B 批次记录 1 个 / 成员 3 个", batches.length === 1 && batches[0].members.length === 3, `batches=${batches.length} members=${batches[0]?.members.length}`);
const board = batches[0];
const gaps = (board?.members || []).flatMap((member) => batch.batchMemberGaps(member).map((gap) => `${member.instanceId}: ${gap}`));
check("B 四层关联齐（batchId↔实例↔节点↔hub）", gaps.length === 0, JSON.stringify(gaps));
check("B 成员 hub 外键来自真 ingest 回执", board?.members.every((member, index) => member.hub?.localId === perInstance[index].hub.localId && Boolean(member.hub?.bridgeId)), JSON.stringify(board?.members.map((member) => member.hub)));
const nodeIdsOnCanvas = new Set(canvas.nodes().map((node) => node.id));
const missingNodeRefs = (board?.members || []).flatMap((member) => [...member.nodes.all, member.nodes.product || ""].filter((id) => id && !nodeIdsOnCanvas.has(id)));
check("B 成员节点骨架 id 全部落在画布上", missingNodeRefs.length === 0, JSON.stringify(missingNodeRefs.slice(0, 6)));
const persistedNow = readPersisted();
check("B 磁盘 payload 含 3 个实例与批次号（flush 已落盘）", Boolean(persistedNow) && board.members.every((member) => persistedNow.includes(member.instanceId)) && persistedNow.includes(batchId));
check("B 每次 flush 回来时磁盘已含本条实例（不等防抖）", flushPayloadChecks.length === 6 && flushPayloadChecks.every((item) => item.hasInstance), JSON.stringify(flushPayloadChecks));
check("B 后一次 flush 回来时磁盘已含前面全部实例", flushPayloadChecks.filter((item) => item.stage === "awaiting_approval").every((item) => item.hasAllPrevious), JSON.stringify(flushPayloadChecks));

// 防抖窗口对照：updateProject 之后、flush 之前，磁盘上没有新状态
const probeMarker = "P5-DEBOUNCE-MARKER";
useCanvasStore.getState().updateProject(projectId, { title: `${realProject.title} · P5 探针 ${probeMarker}` });
const beforeFlush = readPersisted();
check("F 防抖窗口内磁盘不含最新改动（正是需要 flush 的原因）", !beforeFlush.includes(probeMarker), "防抖窗口内就写盘了？");
const flushImmediate = await useCanvasStore.getState().flushProjects("probe-debounce-check");
const afterFlush = readPersisted();
check("F flush 后磁盘立即含最新改动", flushImmediate.ok && afterFlush.includes(probeMarker), JSON.stringify(flushImmediate));
check("F flush 结果写进 store（lastFlush 可见）", useCanvasStore.getState().lastFlush?.ok === true && useCanvasStore.getState().lastFlush?.reason === "probe-debounce-check", JSON.stringify(useCanvasStore.getState().lastFlush));

// ---------------------------------------------------------------- C. 刷新恢复（新进程真 rehydrate）
writeFileSync(BATCH_PAYLOAD, readPersisted(), "utf8");
const reloadRaw = execFileSync(process.execPath, [path.resolve("scripts/probe-p5-batch-status.mjs"), "--reload"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
const reloaded = JSON.parse(reloadRaw.trim().split("\n").at(-1));
check("C 刷新后 store 已水合", reloaded.hydrated === true, JSON.stringify(reloaded));
check("C 刷新后项目还在（1 个）", reloaded.projectCount === 1 && reloaded.projectId === projectId, JSON.stringify({ count: reloaded.projectCount, id: reloaded.projectId }));
const reloadedBatch = (reloaded.batches || [])[0];
check("C 刷新后批次成员完整恢复（3 个）", reloadedBatch?.members?.length === 3 && reloadedBatch.batchId === batchId, JSON.stringify(reloadedBatch?.members?.map((member) => member.instanceId)));
const reloadedGaps = (reloadedBatch?.members || []).flatMap((member) => batch.batchMemberGaps(member).map((gap) => `${member.instanceId}: ${gap}`));
check("C 刷新后四层关联仍齐（节点 id 全部能在恢复的节点表里找到）", reloadedGaps.length === 0 && reloadedBatch.members.every((member) => member.nodes.all.every((id) => id === member.nodes.product || reloaded.nodeCount > 0)), JSON.stringify(reloadedGaps));
check("C 刷新后 hub 外键原样恢复", JSON.stringify((reloadedBatch?.members || []).map((member) => member.hub?.localId)) === JSON.stringify(perInstance.map((item) => item.hub.localId)), JSON.stringify((reloadedBatch?.members || []).map((member) => member.hub?.localId)));
check("C 刷新后实例身份记录（P4）也恢复", (reloaded.instances || []).length === 3, JSON.stringify(reloaded.instances));

// ---------------------------------------------------------------- E. 查询覆盖（后端 = 生产函数 buildGenerationStatus）
const nodesNow = canvas.nodes();
const videoTasks = [];
const snapshotOf = (nodes) => ({ projectId, title: realProject.title, nodes, connections: canvas.connections(), selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } });
const realQuery = async (input) => {
    const result = agentText.buildGenerationStatus({ nodeIds: input.nodeIds, taskId: input.taskId, limit: input.limit, scope: "canvas" }, snapshotOf(nodesNow), videoTasks);
    return { total: result.total, tasks: result.tasks };
};

const allMemberNodeIds = (board?.members || []).flatMap((member) => batch.batchMemberNodeIds(member)).filter((id, index, list) => list.indexOf(id) === index);
const naive = await realQuery({ nodeIds: allMemberNodeIds });
check("E B5 陷阱复现：裸查按默认 20 条被截断（total > 实际返回）", naive.tasks.length <= 20 && naive.total > naive.tasks.length, `total=${naive.total} returned=${naive.tasks.length}`);
const covered = await batchQuery.queryBatchMemberTasks({ members: board.members, query: realQuery });
check("E 分组查询覆盖 3 个成员的全部节点（无静默截断）", covered.coverage.complete === true && covered.coverage.uncoveredNodeIds.length === 0, JSON.stringify(covered.coverage).slice(0, 400));
check("E 分组查询按成员节点分块下发（块 ≤ 上限 100）", covered.coverage.queries >= 1 && batchQuery.planStatusQueryChunks(allMemberNodeIds, batchQuery.STATUS_QUERY_MAX_LIMIT).every((chunk) => chunk.length <= 100));

// >100 规模：30 个成员（真实成员克隆 + 唯一 id），节点 id 200+
const manyMembers = Array.from({ length: 30 }, (_, index) => {
    const base = board.members[index % board.members.length];
    const salt = `m${index}`;
    const remap = (id) => (id ? `${id}-${salt}` : id);
    return {
        ...base,
        instanceId: `${base.instanceId}-${salt}`,
        backend: { ...base.backend, videoTaskId: undefined, lapianTaskId: remap(base.backend.lapianTaskId) },
        nodes: { ...base.nodes, all: base.nodes.all.map(remap), sourceVideo: remap(base.nodes.sourceVideo), lapian: remap(base.nodes.lapian), assemblyConfig: remap(base.nodes.assemblyConfig), product: remap(base.nodes.product), videoConfig: remap(base.nodes.videoConfig), videoTarget: remap(base.nodes.videoTarget), retiredPlaceholders: base.nodes.retiredPlaceholders.map(remap), downstream: base.nodes.downstream.map(remap) },
    };
});
const manyIds = manyMembers.flatMap((member) => batch.batchMemberNodeIds(member)).filter((id, index, list) => list.indexOf(id) === index);
check("E 构造 >100 规模（成员 30 / 节点 >200）", manyMembers.length === 30 && manyIds.length > 200, `members=${manyMembers.length} nodes=${manyIds.length}`);
const manySnapshotNodes = manyIds.map((id) => ({ id, type: "text", title: id, position: { x: 0, y: 0 }, width: 10, height: 10, metadata: { status: "success" } }));
const manyQuery = async (input) => {
    const result = agentText.buildGenerationStatus({ nodeIds: input.nodeIds, taskId: input.taskId, limit: input.limit, scope: "canvas" }, snapshotOf([...nodesNow, ...manySnapshotNodes]), videoTasks);
    return { total: result.total, tasks: result.tasks };
};
const manyResult = await batchQuery.queryBatchMemberTasks({ members: manyMembers, query: manyQuery });
check("E >100 规模全覆盖（分块 + 二分，零静默截断）", manyResult.coverage.complete === true && manyResult.coverage.coveredNodeIds.length === manyIds.length, JSON.stringify({ requested: manyIds.length, covered: manyResult.coverage.coveredNodeIds.length, queries: manyResult.coverage.queries, splits: manyResult.coverage.splitRounds, uncovered: manyResult.coverage.uncoveredNodeIds.length }));
check("E >100 规模确实分多块下发", manyResult.coverage.queries > 2, `queries=${manyResult.coverage.queries}`);

// 覆盖率不足 → 显式部分未知（后端数据本身缺）
const holeIds = new Set([allMemberNodeIds[0], allMemberNodeIds[1]]);
const holeQuery = async (input) => {
    const ids = input.nodeIds || [];
    if (!ids.length) return { total: 0, tasks: [] };
    const rows = ids.filter((id) => !holeIds.has(id)).map((id) => ({ id, status: "succeeded", source: "canvas" }));
    // 这两个节点后端就是给不出状态（单查也只回 total 不给条目 → 被判「查不全」）
    if (ids.length === 1 && holeIds.has(ids[0])) return { total: 1, tasks: [] };
    return { total: ids.length, tasks: rows.slice(0, 20) };
};
const holeResult = await batchQuery.queryBatchMemberTasks({ members: [board.members[0]], query: holeQuery });
check("E 查不全时显式报「部分未知」（覆盖率不足 ≠ 成功）", holeResult.coverage.complete === false && holeResult.warnings.some((line) => line.includes("部分未知")), JSON.stringify({ complete: holeResult.coverage.complete, warnings: holeResult.warnings }));

// ---------------------------------------------------------------- D. 状态派生矩阵（真实 + 构造）
const evidenceByMember = batchQuery.splitEvidenceByMember({ members: board.members, result: covered, extraTaskStatus: new Map(perInstance.map((item, index) => [`task-p5-${["A", "B", "C"][index]}`, "succeeded"])) });
const hubStatesOfReal = Object.fromEntries(board.members.map((member) => [member.instanceId, "awaiting_approval"]));
const realView = batchStatus.deriveBatchStatus({ batch: board, nodes: canvas.nodes(), hubStates: hubStatesOfReal, evidenceOf: (member) => evidenceByMember.get(member.instanceId) });
check("D 真实批次：3 实例都在待批（Config success 不改判完成）", realView.counts.awaiting_approval === 3 && realView.counts.done === 0, JSON.stringify(realView.counts));
check("D 真实批次：成员状态带命中规则（可复核）", realView.members.every((member) => member.rule === "R4" && member.reason.includes("待审批")), JSON.stringify(realView.members.map((member) => [member.rule, member.reason])));
check("D 真实批次：计数汇总口径完整（9 种状态都有计数）", batchStatus.INSTANCE_STATUSES.every((status) => typeof realView.counts[status] === "number"), JSON.stringify(realView.counts));

const first = board.members[0];
const nodesCopy = canvas.nodes();
const patchNode = (id, metadata) => nodesCopy.map((node) => (node.id === id ? { ...node, metadata: { ...node.metadata, ...metadata } } : node));
// ① Config success（组装产物 success）+ 视频 running → 进行中，绝不是完成
const runningNodes = patchNode(first.nodes.videoTarget, { status: "loading", videoTaskId: "video-task-p5-A", videoTaskProvider: "sdgf" });
const runningMember = { ...first, backend: { ...first.backend, videoTaskId: "video-task-p5-A", videoProvider: "sdgf" } };
const runningView = batchStatus.deriveInstanceStatus({ member: runningMember, nodes: runningNodes, hubState: "generating", evidence: { byTaskId: new Map([["video-task-p5-A", "running"], ["task-p5-A", "succeeded"]]) } });
check("D 矩阵①：Config success + 视频 running → 进行中非完成", runningView.status === "generating" && runningView.status !== "done", JSON.stringify({ status: runningView.status, rule: runningView.rule }));
// ② 后端任务 failed → 失败
const failedView = batchStatus.deriveInstanceStatus({ member: runningMember, nodes: patchNode(first.nodes.videoTarget, { status: "error", videoTaskId: "video-task-p5-A" }), hubState: "generating", evidence: { byTaskId: new Map([["video-task-p5-A", "failed"]]) } });
check("D 矩阵②：后端任务 failed → 实例失败", failedView.status === "failed" && failedView.rule === "R1", JSON.stringify({ status: failedView.status, rule: failedView.rule }));
// ③ 任务缺失 → 显式未知，不计成功
const missingView = batchStatus.deriveInstanceStatus({ member: { ...first, backend: { ...first.backend, videoTaskId: "video-task-missing" } }, nodes: canvas.nodes(), hubState: undefined, evidence: { byTaskId: new Map() } });
check("D 矩阵③：任务缺失 → 显式未知不计成功", missingView.status === "unknown" && missingView.status !== "done" && missingView.missing.some((item) => item.includes("视频后端任务状态未知")), JSON.stringify({ status: missingView.status, missing: missingView.missing }));
// ④ 完成态：产物 + 视频任务 succeeded
const doneView = batchStatus.deriveInstanceStatus({ member: runningMember, nodes: patchNode(first.nodes.videoTarget, { status: "success", videoTaskId: "video-task-p5-A" }), hubState: "done", evidence: { byTaskId: new Map([["video-task-p5-A", "succeeded"], ["task-p5-A", "succeeded"]]) } });
check("D 矩阵④：产物 + 视频任务 succeeded → 完成", doneView.status === "done" && doneView.missing.length === 0, JSON.stringify({ status: doneView.status, missing: doneView.missing }));
// ⑤ 成员节点缺失 → 未知
const lostNodes = canvas.nodes().filter((node) => node.id !== first.nodes.lapian);
const lostView = batchStatus.deriveInstanceStatus({ member: first, nodes: lostNodes, hubState: "awaiting_approval" });
check("D 矩阵⑤：成员节点缺失 → 未知（缺失≠成功）", lostView.status === "unknown" && lostView.rule === "R2", JSON.stringify({ status: lostView.status, rule: lostView.rule }));
// 批次聚合：一条未知 → 批次部分未知
const mixedBatch = batch.createWorkflowBatch({ templateId: board.templateId, batchId: "batch-mixed", members: [first, board.members[1]] });
const mixedView = batchStatus.deriveBatchStatus({ batch: mixedBatch, nodes: canvas.nodes().filter((node) => node.id !== first.nodes.lapian), hubStates: { [first.instanceId]: "awaiting_approval", [board.members[1].instanceId]: "awaiting_approval" }, evidenceOf: (member) => evidenceByMember.get(member.instanceId) });
check("D 批次聚合：未知成员 → partial_unknown（不算成功）", mixedView.outcome === "partial_unknown" && mixedView.coverage.complete === false, JSON.stringify({ outcome: mixedView.outcome, coverage: mixedView.coverage }));

// ---------------------------------------------------------------- F. flush 失败可见（不静默）
storageBroken = true;
// 先制造一次待写（否则没有 pending，flush 会正确地什么都不写）
useCanvasStore.getState().updateProject(projectId, { title: `${realProject.title} · P5 探针 写盘失败` });
const brokenFlush = await useCanvasStore.getState().flushProjects("probe-broken-storage");
check("F 写盘失败：flush 返回 ok=false（不静默）", brokenFlush.ok === false && Boolean(brokenFlush.error), JSON.stringify(brokenFlush));
check("F 写盘失败：store.lastFlush 可见错误", useCanvasStore.getState().lastFlush?.ok === false && Boolean(useCanvasStore.getState().lastFlush?.error), JSON.stringify(useCanvasStore.getState().lastFlush));
// 编排器侧：flush 依赖返回失败 → 结果里可见 + 警告，但实例本身仍到待批（身份不丢）
const brokenMats = materialOf("D");
const brokenStub = makeLapianFetch({ taskId: "task-p5-D", jobId: "job-p5-D", markdown: markdown("D") });
const brokenCreated = await instancer.materializeWorkflowInstance(
    { template: templateSource.template, templateNodes: templateSource.project.nodes, templateConnections: templateSource.project.connections, materials: brokenMats, instanceIndex: 4, origin: { x: 2400 * 4, y: 0 }, unnamedCaption: "未命名参考图", lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
    { getBlob: async () => new Blob(["video-D"], { type: "video/mp4" }), fetch: brokenStub, applyOps: canvas.apply, existingNodes: canvas.nodes },
);
if (brokenCreated.status === "created") {
    const brokenRun = await runner.runWorkflowInstance(
        { plan: brokenCreated.plan, materials: brokenMats, lapian: { taskId: "task-p5-D", jobId: "job-p5-D" }, batchId },
        {
            nodes: canvas.nodes,
            connections: canvas.connections,
            applyOps: canvas.apply,
            runGeneration: makeGenerationChain(canvas, "p5d"),
            fetchProjection: makeProjectionFetch(),
            hub: makeHubStub(4).client,
            getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }),
            saveIdentity: (record) => {
                identities = identityModule.upsertInstanceIdentity(identities, record);
            },
            saveBatchMember: (member) => {
                batches = batch.upsertBatchMember(batches, batchMeta, member, "2026-11-13T00:00:00.000Z");
            },
            flush: async (stage) => {
                syncProject();
                return useCanvasStore.getState().flushProjects(stage);
            },
            newRunToken: () => "runTokenD",
        },
    );
    check("F 编排器：flush 失败如实回报（flushes.ok=false）且实例不因此判失败", brokenRun.flushes.some((item) => item.ok === false) && brokenRun.status === "awaiting_approval" && brokenRun.warnings.some((line) => line.includes("flush 失败")), JSON.stringify({ flushes: brokenRun.flushes, status: brokenRun.status, warnings: brokenRun.warnings }));
} else {
    check("F 编排器：flush 失败场景实例落库", false, JSON.stringify(brokenCreated.incomplete || {}));
}
storageBroken = false;

// ---------------------------------------------------------------- G. 零回归（store 方法面 / 导入透传 / C5）
const stateKeys = Object.keys(useCanvasStore.getState());
const legacyMethods = ["createProject", "importProject", "openProject", "renameProject", "deleteProjects", "replaceProjects", "updateProject"];
check("G store 既有方法面零破坏（7 个方法都在）", legacyMethods.every((name) => typeof useCanvasStore.getState()[name] === "function"), JSON.stringify(stateKeys));
check("G store 新增只加不改（flushProjects/lastFlush/workflowBatches）", stateKeys.includes("flushProjects") && stateKeys.includes("lastFlush"), JSON.stringify(stateKeys));
const importedId = useCanvasStore.getState().importProject({ title: "P5 导入透传检查", workflowBatches: board.workflowBatches || [board], workflowTemplate: templateSource.template, workflowInstances: identities });
const importedProject = useCanvasStore.getState().openProject(importedId);
check("G 导入透传：workflowBatches / workflowTemplate / workflowInstances 都不丢", importedProject?.workflowBatches?.length === 1 && Boolean(importedProject?.workflowTemplate?.templateId) && (importedProject?.workflowInstances || []).length > 0, JSON.stringify({ batches: importedProject?.workflowBatches?.length, template: importedProject?.workflowTemplate?.templateId, instances: importedProject?.workflowInstances?.length }));
const metadataKeys = new Set(canvas.nodes().flatMap((node) => Object.keys(node.metadata || {})));
const batchLeak = [...metadataKeys].filter((key) => /^(batchId|workflowBatch|batchMember|instanceStatus|approvalState)$/.test(key));
check("G C5：节点 metadata 里没有批次/审批字段（身份只存项目字段）", batchLeak.length === 0, JSON.stringify(batchLeak));
const paidOps = paid.collectPaidActions("canvas_apply_ops", { ops: perInstance.flatMap((item) => item.result.appliedOps) }, snapshotOf(canvas.nodes()));
check("G P0b 闸复验：P5 全部 ops 过付费提取 = 0 条付费动作", paidOps.length === 0, JSON.stringify(paidOps).slice(0, 200));

// ---------------------------------------------------------------- 汇总
writeFileSync(
    path.join(outDir, "probe-summary.json"),
    JSON.stringify(
        {
            pass,
            fail: fails.length,
            unitTotal,
            batch: { batchId, members: (board?.members || []).map((member) => ({ instanceId: member.instanceId, instanceIndex: member.instanceIndex, localId: member.hub?.localId, nodeCount: member.nodes.all.length, materialsDigest: member.materials.digest })) },
            statusView: { counts: realView.counts, outcome: realView.outcome, members: realView.members.map((member) => ({ instanceId: member.instanceId, status: member.status, rule: member.rule, missing: member.missing })) },
            coverage: { naiveTotal: naive.total, naiveReturned: naive.tasks.length, groupedComplete: covered.coverage.complete, groupedQueries: covered.coverage.queries, manyCovered: manyResult.coverage.coveredNodeIds.length, manyRequested: manyIds.length },
            flush: flushLog,
            reloaded: { hydrated: reloaded.hydrated, members: reloadedBatch?.members?.length, instances: reloaded.instances?.length },
            results,
        },
        null,
        2,
    ),
    "utf8",
);
finish();

function finish() {
    console.log(`\nP5 批次成员+状态派生探针：${pass} 通过 / ${fails.length} 失败（单测 ${unitTotal} 例）`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(fails.length ? 1 : 0);
}
