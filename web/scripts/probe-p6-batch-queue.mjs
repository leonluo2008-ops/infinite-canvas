/**
 * P6 探针：批次队列批跑 + 恢复 + 渠道拥塞分类 + 未知提交对账 + Wan 成片映射持久化（S2）。
 *
 * 跑法：cd web && node scripts/probe-p6-batch-queue.mjs --store <real-canvas-store.json> --out <输出目录>
 *
 * 分段：
 *   A. 单测真跑：esbuild 打包 5 个测试文件 → `node --test`（P6 新增 39 例 + P5/P4 回归）
 *   B. 3 实例真实链路批跑（真实「无限画布 3」模板 + P3 materialize + P4 编排，stub 拦截网络/hub）
 *      → 并发曲线峰值 ≤3、全部到待批、per-instance 派生状态正确、排队簿记已落盘
 *   C. 429 注入（退避重排队最终成功）+ 一直 409（退避耗尽给可读上限文案）；与未知提交路径不混淆
 *   D. 未知提交对账：创建回执丢失 → 按 P5 查询口径（生产 buildGenerationStatus）核对既有任务
 *      → 命中=resolved（续查不重建）/ 未命中=explicit_unknown；两种情况**都不重发**（run 次数恒 1）
 *   E. 失败隔离：中间实例组装失败 → 其余照常到待批，聚合带 per-instance 失败原因
 *   F. 恢复：批跑中途暂停 → 落盘 → **新进程真 rehydrate** → 按 planBatchResume 续跑剩余实例
 *      （已完成的跳过、不重拉片、不重建节点；断言 lapian 调用数与画布节点数不变）
 *   G. S2：Wan job→成片 taskId 走**生产创建函数**（axios adapter 替身，零真实网络）→ 随项目持久化
 *      → 导出（真 buildCanvasExport）→ 导入 → 可查；老 localStorage 200 条行为兼容回归
 *   H. 零回归：store 方法面、节点 metadata 不带队列字段、付费创建 0、真实样本项目零改动
 *
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails，不跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p6-out"));
const PAYLOAD = path.join(outDir, "persisted-store.json");
mkdirSync(outDir, { recursive: true });
// 开工先删上一次的 payload：否则任何「读错目录」的子进程都会读到陈旧状态，rehydrate 断言会假绿。
if (!args.includes("--reload")) rmSync(PAYLOAD, { force: true });
const bundleDir = path.resolve("node_modules/.probe-p6");
mkdirSync(bundleDir, { recursive: true });

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");
const STORE_KEY_SUFFIX = "infinite-canvas:canvas_store";

// 浏览器存储替身：**必须在任何 bundle import 之前就位**（i18n 模块加载时就读 localStorage）。
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
    const payload = readFileSync(PAYLOAD, "utf8");
    const mem = new Map([[`infinite-canvas/app_state/${STORE_KEY_SUFFIX}`, payload]]);
    globalThis.localStorage = {
        getItem: (key) => (mem.has(key) ? mem.get(key) : null),
        setItem: (key, value) => mem.set(key, String(value)),
        removeItem: (key) => mem.delete(key),
    };
    globalThis.window = { localStorage: globalThis.localStorage };
    const entry = path.join(bundleDir, "p6-reload-entry.ts");
    writeFileSync(
        entry,
        ['export * as store from "@/stores/canvas/use-canvas-store";', 'export * as wanProjects from "@/lib/canvas/wan-file-task-project";', ""].join("\n"),
        "utf8",
    );
    const out = path.join(bundleDir, "p6-reload.mjs");
    await esbuild.build({ entryPoints: [entry], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "silent", alias: { "@": path.resolve("src") } });
    const mod = await import(pathToFileURL(out).href);
    const store = mod.store.useCanvasStore;
    for (let attempt = 0; attempt < 200 && !store.getState().hydrated; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 25));
    // 取「带批次记录的项目」= 本探针项目；projects[0] 会随 importProject 的插入位置漂移，不能当锚点。
    const project = store.getState().projects.find((item) => (item.workflowBatches || []).length) || store.getState().projects[0];
    mod.wanProjects.bindWanFileTaskProject(project?.id || "");
    const job = String(process.env.P6_RELOAD_JOB || "");
    console.log(
        JSON.stringify({
            hydrated: store.getState().hydrated,
            projectId: project?.id,
            nodeCount: project?.nodes?.length || 0,
            batches: (project?.workflowBatches || []).map((batch) => ({ batchId: batch.batchId, members: batch.members.length, queue: batch.queue ? { phase: batch.queue.phase, concurrency: batch.queue.concurrency, entries: batch.queue.entries.length } : null })),
            wanTasks: (project?.workflowWanFileTasks || []).length,
            wanLookup: job ? mod.wanProjects.lookupProjectWanFileTask(job) : "",
            instances: (project?.workflowInstances || []).length,
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
    ["src/lib/canvas/workflow-batch-queue.test.ts", "p6-queue.test.mjs", "P6 队列/对账/恢复"],
    ["src/lib/canvas/wan-file-task-map.test.ts", "p6-wan-map.test.mjs", "P6/S2 成片映射"],
    ["src/lib/canvas/workflow-batch.test.ts", "p5-batch.test.mjs", "P5 回归"],
    ["src/lib/canvas/workflow-instance-run.test.ts", "p4-instance-run.test.mjs", "P4 回归"],
    ["src/lib/canvas/canvas-persist-queue.test.ts", "p5-persist-queue.test.mjs", "P5 落盘队列回归"],
];
let unitTotal = 0;
const unitSummary = [];
for (const [entry, outName, label] of testFiles) {
    const testBundle = await bundle(entry, outName, {
        banner: { js: "globalThis.localStorage = globalThis.localStorage || { getItem: () => null, setItem: () => {}, removeItem: () => {} };" },
    });
    let output = "";
    let failed = false;
    try {
        output = execFileSync(process.execPath, ["--test", "--test-timeout=60000", testBundle], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
        failed = true;
        output = `${error.stdout || ""}${error.stderr || ""}`;
    }
    writeFileSync(path.join(outDir, `unit-${outName.replace(/\.mjs$/, "")}.tap`), output, "utf8");
    const count = (label2) => {
        const match = output.match(new RegExp(`^(?:# |ℹ )${label2} ([0-9]+)$`, "m"));
        return match ? Number(match[1]) : undefined;
    };
    const tests = count("tests");
    const failCount = count("fail");
    unitTotal += Number(tests || 0);
    unitSummary.push({ label, tests, fail: failCount, failed });
    check(`A 单测（${label}）进程零失败`, !failed, failed ? output.split("\n").filter((line) => line.includes("✖")).slice(0, 5).join(" | ") : "");
    check(`A 单测（${label}）用例数 > 0`, Number(tests || 0) > 0, `tests=${tests}`);
    check(`A 单测（${label}）fail 计数 0`, failCount === 0, `fail=${failCount}`);
}
check("A 单测总数 ≥ 60（P6 新增 39 + P5/P4 回归）", unitTotal >= 60, `total=${unitTotal}`);

// ---------------------------------------------------------------- 模块（同一 bundle：共享单例）
const modulesEntry = path.join(bundleDir, "p6-modules-entry.ts");
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
        'export * as queue from "@/lib/canvas/workflow-batch-queue";',
        'export * as channelFailure from "@/lib/canvas/workflow-channel-failure";',
        'export * as wanMap from "@/lib/canvas/wan-file-task-map";',
        'export * as wanProject from "@/lib/canvas/wan-file-task-project";',
        'export * as store from "@/stores/canvas/use-canvas-store";',
        'export * as videoApi from "@/services/api/video";',
        'export * as exporter from "@/lib/canvas/canvas-export-builder";',
        'export * as agentText from "@/lib/agent/agent-node-text";',
        'export * as configStore from "@/stores/use-config-store";',
        "export { default as axios } from \"axios\";",
        `export * as paid from ${JSON.stringify(path.resolve("..", "canvas-agent/src/canvas/paid-actions.ts").split(path.sep).join("/"))};`,
        "",
    ].join("\n"),
    "utf8",
);
// axios 外置：让 video.ts 与探针拿到**同一个** axios 实例（adapter 替身才生效），
// 否则 esbuild 会把 axios 的 CJS 依赖（form-data → util）打进 ESM 产物而崩。
const modules = await import(pathToFileURL(await bundle(modulesEntry, "p6-modules.mjs", { external: ["axios", "node:test", "node:assert", "node:assert/strict"] })).href);
const { builtin, workflow, instancer, runner, identity, ops, genctx, batch, batchStatus, batchQuery, queue, channelFailure, wanMap, wanProject, videoApi, exporter, agentText, configStore, axios, paid, store: storeNs } = modules;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- 真实 store（真 zustand persist + 真 localforage）
// 必须用**模块 bundle 里那一份** store：`services/api/video.ts → wan-file-task-project → use-canvas-store`
// 也在同一 bundle 里，分成两份会得到两个互不相通的 store 单例（S2 的写入就会落到空 store 上）。
const useCanvasStore = storeNs.useCanvasStore;
const readPersisted = () => {
    for (const [key, value] of memory) if (key.endsWith(STORE_KEY_SUFFIX)) return value;
    return "";
};
check("B store 模块可加载（真 zustand persist）", typeof useCanvasStore.getState === "function");

// ---------------------------------------------------------------- B. 真实样本 → 模板 → 实例
const storeJson = JSON.parse(readFileSync(storePath, "utf8"));
const realProjects = (storeJson.state && storeJson.state.projects) || [];
const realProject = realProjects.find((item) => item.id === projectKey) || realProjects.find((item) => item.title === projectKey);
check("B 找到真实样本项目", Boolean(realProject), `projects=${realProjects.map((item) => item.title).join(",")}`);
if (!realProject) finish();
const realNodesBefore = JSON.stringify(realProject);

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
const isFixedAsset = (node) => node && node.type === "image" && Boolean(node.metadata?.storageKey) && !node.metadata?.assetSlotSourceId && !node.metadata?.assetGraftForSlotId && !node.metadata?.assetGraftOfImageId;
const fixedAssets = [...new Set([...incoming(configText.id), ...incoming(configVideo.id)])].map((id) => byId.get(id)).filter(isFixedAsset);
const skeletonIds = [sourceVideo?.id, lapianId, instruction?.id, intent?.id, configText?.id, finalText?.id, configVideo?.id, ...fixedAssets.map((node) => node.id)].filter(Boolean);
check("B 骨架齐全（video→lapian→config text→占位终稿→config video + 固定参考图）", skeletonIds.length >= 7 && Boolean(sourceVideo && configText && finalText && configVideo), `边界=${skeletonIds.length} 固定图=${fixedAssets.length}`);

const templateSource = workflow.extractWorkflowTemplate({ sourceProject: realProject, selectedNodeIds: skeletonIds, name: `${realProject.title} · P6 骨架`, templateId: "p6skeleton", createdAt: "2026-11-13T00:00:00.000Z" });
check("B 模板自足（无悬空引用）", workflow.validateTemplateProject(templateSource.project.nodes, templateSource.project.connections).length === 0, JSON.stringify(workflow.validateTemplateProject(templateSource.project.nodes, templateSource.project.connections).slice(0, 3)));

const appliedOpsLog = [];
function makeCanvas() {
    let snapshot = { projectId: realProject.id, title: realProject.title, nodes: realNodes, connections: realConnections, selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (batchOps) => {
            appliedOpsLog.push(...batchOps);
            const next = ops.applyCanvasAgentOps(snapshot, batchOps);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
            return next;
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
        snapshot: () => snapshot,
    };
}
const canvas = makeCanvas();

const projectId = useCanvasStore.getState().importProject({ title: `${realProject.title} · P6 探针`, nodes: realNodes, connections: realConnections, createdAt: "2026-11-13T00:00:00.000Z" });
check("B store 载入真实项目（importProject）", useCanvasStore.getState().projects.length === 1 && useCanvasStore.getState().projects[0].id === projectId);
// S2：把项目绑上（页面里由 project.tsx 的 effect 完成；这里显式绑定以走同一条生产路径）
wanProject.bindWanFileTaskProject(projectId);

let identities = [];
let batches = [];
const flushLog = [];
const lapianRunUrls = [];

function makeLapianFetch({ taskId, jobId, markdown, tag }) {
    return async (url) => {
        lapianRunUrls.push({ url: String(url), tag });
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
const generationCallsByTag = new Map();
function makeGenerationChain(targetCanvas, tag, { failFirst = null, failAlways = null, markRunning = null } = {}) {
    let attempts = 0;
    return async (input) => {
        attempts += 1;
        generationCallsByTag.set(tag, (generationCallsByTag.get(tag) || 0) + 1);
        const mode = failAlways || (attempts === 1 ? failFirst : null);
        if (mode === "429") throw Object.assign(new Error("429 Too Many Requests：渠道限流，请稍后再试"), { status: 429 });
        if (mode === "409") throw Object.assign(new Error("409 已经有一个万相生成在跑（CLI 同时只认一条），稍后再试"), { status: 409 });
        if (mode === "hard") throw new Error("本实例没有文本组装 Config（模板冻结件缺失）");
        if (mode === "submit_unknown") {
            // 模拟「请求已发出、渠道侧任务在跑，但回执丢了」：把提交节点（视频 Config）标成生成中
            if (markRunning?.id) await targetCanvas.apply([{ type: "update_node", id: markRunning.id, metadata: { status: "loading" } }]);
            throw new Error("SD GF 未返回任务 ID（提交状态不明——到站内控制台核对，勿重复提交）");
        }
        const context = genctx.buildNodeGenerationContext(input.nodeId, targetCanvas.nodes(), targetCanvas.connections(), input.prompt);
        generationSeq += 1;
        const childNodeId = `${tag}-out-${generationSeq}`;
        const content = `【本次产物 ${tag}-${generationSeq}】\n${context.prompt}`;
        await targetCanvas.apply([
            { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 6000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
            { type: "connect_nodes", fromNodeId: input.nodeId, toNodeId: childNodeId },
        ]);
        return { childNodeId };
    };
}
function makeHubStub(seed) {
    const localId = `auto-p6-${seed}`;
    return {
        localId,
        client: {
            async ingest(input) {
                return { localId, bridgeId: `bridge-p6-${seed}`, result: "OK", bytes: input.blob?.size || 0 };
            },
            async uploadAsset(input) {
                return { ref: `local:p6/${input.filename}`, source: "local" };
            },
            async getTask() {
                return { state: "awaiting_approval", bridgeId: `bridge-p6-${seed}` };
            },
            async saveRevision(input) {
                return { state: "awaiting_approval", approvalSnapshot: `snap-p6-${seed}-${input.text.length}`, promptEditedAt: "2026-11-13T00:00:00.000Z", selfcheck: [{ 序号: 1, 结论: "PASS" }] };
            },
            approve: async () => ({ state: "approved" }),
            rollback: async () => ({ state: "awaiting_assets" }),
            revoke: async () => ({ state: "awaiting_approval" }),
        },
    };
}

const materialOf = (tag) => ({
    sourceVideo: { storageKey: `video:p6-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/p6-${tag}`, mimeType: "video/mp4", bytes: 6000, durationMs: 12000 },
    intentText: `把主体换成客户模特（P6-${tag}）`,
    optionalSegments: "A",
    generationParams: [{ nodeId: configText.id, values: { model: `default::p6-${tag.toLowerCase()}-text-model` } }],
});
const markdown = (tag) => `# 本次拉片正文 · ${tag}\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近`;

/** 建一个实例的作业（prepare = P3 materialize；run = P4 编排）。 */
function makeJob({ index, tag, batchId, chainOptions, batchMeta, nodesFilter, recordPlan }) {
    const materials = materialOf(tag);
    const lapianFetch = makeLapianFetch({ taskId: `task-p6-${tag}`, jobId: `job-p6-${tag}`, markdown: markdown(tag), tag });
    const hub = makeHubStub(tag);
    const prepared = { status: "none" };
    // 「提交节点」= 本实例的视频 Config（真实节点 id 只有 materialize 出计划后才知道）
    const runningNode = { id: "" };
    // 生成链只建一次：重跑要复用同一个闭包（否则每次重跑都从「第一次」重新计数，429/409 永远退不出去）
    const chain = makeGenerationChain(canvas, tag, { ...chainOptions, markRunning: chainOptions?.markRunning ? runningNode : null });
    return {
        instanceId: `${templateSource.template.templateId}#${index}`,
        instanceIndex: index,
        materials,
        hub,
        prepared,
        prepare: async () => {
            const created = await instancer.materializeWorkflowInstance(
                { template: templateSource.template, templateNodes: templateSource.project.nodes, templateConnections: templateSource.project.connections, materials, instanceIndex: index, origin: { x: 2400 * index, y: 0 }, unnamedCaption: "未命名参考图", lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
                { getBlob: async () => new Blob([`video-${tag}`], { type: "video/mp4" }), fetch: lapianFetch, applyOps: canvas.apply, existingNodes: canvas.nodes },
            );
            prepared.status = created.status;
            if (created.status === "incomplete") return { ok: false, reason: created.incomplete?.reason || "materialize incomplete" };
            recordPlan(created.plan);
            runningNode.id = created.plan.nodes.find((node) => node.type === "config" && String(node.metadata?.generationMode || "") === "video")?.id || "";
            return { ok: true, status: created.status };
        },
        run: async () => {
            const plan = recordedPlans.get(index);
            const result = await runner.runWorkflowInstance(
                { plan, materials, lapian: { taskId: `task-p6-${tag}`, jobId: `job-p6-${tag}` }, batchId },
                {
                    nodes: nodesFilter ? () => nodesFilter(canvas.nodes()) : canvas.nodes,
                    connections: canvas.connections,
                    applyOps: canvas.apply,
                    runGeneration: chain,
                    fetchProjection: makeProjectionFetch(),
                    hub: hub.client,
                    getBlob: async (storageKey) => new Blob([`bytes-of-${storageKey}`], { type: String(storageKey).startsWith("image:") ? "image/png" : "video/mp4" }),
                    saveIdentity: (record) => {
                        identities = identity.upsertInstanceIdentity(identities, record);
                    },
                    saveBatchMember: (member) => {
                        batches = batch.upsertBatchMember(batches, batchMeta, member, "2026-11-13T00:00:00.000Z");
                    },
                    wanFileTasks: () => wanProject.projectWanFileTaskTable(),
                    flush: async (stage) => {
                        syncProject();
                        const outcome = await useCanvasStore.getState().flushProjects(`${batchId}:${stage}`);
                        flushLog.push({ tag, stage, ok: outcome.ok });
                        return outcome;
                    },
                    newRunToken: () => `runToken-${tag}`,
                    now: () => "2026-11-13T00:00:00.000Z",
                },
            );
            return result;
        },
    };
}

/** 取真实批次记录（队列的 memberOf/对账需要成员节点；空壳记录会让对账查不到节点）。 */
function batchInputOf(batchIdValue) {
    const found = batches.find((item) => item.batchId === batchIdValue);
    return found || { schemaVersion: 1, batchId: batchIdValue, templateId: templateSource.template.templateId, signature: "sig", createdAt: "2026-11-13T00:00:00.000Z", updatedAt: "2026-11-13T00:00:00.000Z", members: [] };
}

const recordedPlans = new Map();
function syncProject(queueOverride) {
    const nextBatches = queueOverride ? batches.map((item) => (item.batchId === queueOverride.batchId ? { ...item, queue: queueOverride.queue } : item)) : batches;
    if (queueOverride) batches = nextBatches;
    useCanvasStore.getState().updateProject(projectId, { nodes: canvas.nodes(), connections: canvas.connections(), workflowInstances: identities, workflowBatches: batches, workflowWanFileTasks: wanProject.listProjectWanFileTasks() });
}

// ───────────── B. 3 实例并发批跑（并发峰值 ≤3 / 全部到待批）
const batchId = batch.deriveBatchId({ templateId: templateSource.template.templateId, materialDigests: ["A", "B", "C"], createdAt: "2026-11-13T00:00:00.000Z" });
const batchMeta = { templateId: templateSource.template.templateId, batchId, title: "P6 探针批次" };
const curve = [];
const jobsB = [1, 2, 3].map((index) =>
    makeJob({ index, tag: ["A", "B", "C"][index - 1], batchId, chainOptions: {}, batchMeta, recordPlan: (plan) => recordedPlans.set(index, plan) }),
);
const runB = await queue.runBatchQueue({
    batch: batchInputOf(batchId),
    jobs: jobsB,
    persist: async (next) => {
        batches = queue.setBatchQueue(batches.length ? batches : [batch.createWorkflowBatch({ templateId: templateSource.template.templateId, batchId, title: "P6 探针批次" })], batchId, next);
        syncProject();
        await useCanvasStore.getState().flushProjects("p6-queue");
    },
    onSlot: (sample) => curve.push({ active: sample.active, running: sample.running.length }),
});
check("B 并发峰值 ≤ 3（实例级编排并发）", runB.concurrencyPeak <= 3 && curve.every((item) => item.active <= 3), JSON.stringify({ peak: runB.concurrencyPeak, curve }));
check("B 3 实例全部到待批", runB.aggregate.awaitingApproval === 3 && runB.aggregate.failed.length === 0, JSON.stringify({ aggregate: runB.aggregate, perInstance: runB.perInstance }));
check("B 付费视频创建 = 0（终点=待批）", runB.paidVideoCreations === 0, String(runB.paidVideoCreations));
check("B 队列相位=finished + 并发上限落盘为 3", runB.queue.phase === "finished" && runB.queue.concurrency === 3, JSON.stringify({ phase: runB.queue.phase, concurrency: runB.queue.concurrency }));
const batchBoard = batches.find((item) => item.batchId === batchId);
check("B 批次记录 3 成员 + 排队簿记 3 条目", batchBoard?.members.length === 3 && batchBoard?.queue?.entries.length === 3, JSON.stringify({ members: batchBoard?.members.length, entries: batchBoard?.queue?.entries.length }));
const persistedAfterB = readPersisted();
check("B 排队簿记已落盘（磁盘 payload 含 batchId 与队列条目）", persistedAfterB.includes(batchId) && (batchBoard?.queue?.entries || []).every((entry) => persistedAfterB.includes(entry.instanceId)));
check("B 每次实例都只落一次盘（flush 记录 = 3 实例 × 关键节点）", flushLog.filter((item) => item.ok).length >= 6, JSON.stringify(flushLog));

// per-instance 派生状态（P5 派生器 + hub 现查口径）
const allMemberNodeIds = (batchBoard?.members || []).flatMap((member) => batch.batchMemberNodeIds(member)).filter((id, index, list) => list.indexOf(id) === index);
const snapshotOf = (nodes) => ({ projectId, title: realProject.title, nodes, connections: canvas.connections(), selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } });
const prodQuery = async (input) => {
    const result = agentText.buildGenerationStatus({ nodeIds: input.nodeIds, taskId: input.taskId, limit: input.limit, scope: "canvas" }, snapshotOf(canvas.nodes()), []);
    return { total: result.total, tasks: result.tasks };
};
const evidence = batchQuery.splitEvidenceByMember({ members: batchBoard.members, result: await batchQuery.queryBatchMemberTasks({ members: batchBoard.members, query: prodQuery }), extraTaskStatus: new Map([["task-p6-A", "succeeded"], ["task-p6-B", "succeeded"], ["task-p6-C", "succeeded"]]) });
const hubStates = Object.fromEntries(batchBoard.members.map((member) => [member.instanceId, "awaiting_approval"]));
const viewB = batchStatus.deriveBatchStatus({ batch: batchBoard, nodes: canvas.nodes(), hubStates, evidenceOf: (member) => evidence.get(member.instanceId) });
check("B per-instance 派生状态：3 个全在待批（R4）且无未知", viewB.counts.awaiting_approval === 3 && viewB.members.every((member) => member.rule === "R4" && member.status !== "unknown"), JSON.stringify(viewB.members.map((member) => [member.status, member.rule])));
check("B 批次聚合结论 = in_progress（待批不是完成）", viewB.outcome === "in_progress" && viewB.counts.done === 0, JSON.stringify({ outcome: viewB.outcome, counts: viewB.counts }));
check("B 成员四层关联齐（batchId↔实例↔节点↔hub）", batchBoard.members.every((member) => batch.batchMemberGaps(member).length === 0), JSON.stringify(batchBoard.members.flatMap((member) => batch.batchMemberGaps(member))));

// ───────────── C. 渠道拥塞分类（429 退避成功 / 409 上限文案）
const jobsRate = [makeJob({ index: 4, tag: "D", batchId: "batch-p6-rate", chainOptions: { failFirst: "429" }, batchMeta: { templateId: templateSource.template.templateId, batchId: "batch-p6-rate" }, recordPlan: (plan) => recordedPlans.set(4, plan) })];
const jobsSlot = [makeJob({ index: 5, tag: "E", batchId: "batch-p6-slot", chainOptions: { failAlways: "409" }, batchMeta: { templateId: templateSource.template.templateId, batchId: "batch-p6-slot" }, recordPlan: (plan) => recordedPlans.set(5, plan) })];
for (const job of [...jobsRate, ...jobsSlot]) batches = batch.upsertBatchMember(batches, { templateId: templateSource.template.templateId, batchId: `batch-p6-${job.tag === "D" ? "rate" : "slot"}` }, { instanceId: job.instanceId, templateId: templateSource.template.templateId, instanceIndex: job.instanceIndex, materials: batch.summarizeMaterials(job.materials), nodes: { all: [job.instanceId], sourceVideo: job.instanceId, lapian: job.instanceId, retiredPlaceholders: [], downstream: [] }, hub: null, backend: { lapianTaskId: `task-p6-${job.tag}`, lapianJobId: `job-p6-${job.tag}` }, createdAt: "2026-11-13T00:00:00.000Z" });
let virtualNow = Date.parse("2026-11-13T00:00:00.000Z");
const makeVirtualSleep = (sink) => async (ms) => {
    sink.push(ms);
    virtualNow += ms;
};
const rateWaits = [];
const runC = await queue.runBatchQueue({ batch: batchInputOf("batch-p6-rate"), jobs: jobsRate, sleep: makeVirtualSleep(rateWaits), now: () => virtualNow, persist: async () => {} });
const slotWaits = [];
const runC2 = await queue.runBatchQueue({ batch: batchInputOf("batch-p6-slot"), jobs: jobsSlot, sleep: makeVirtualSleep(slotWaits), now: () => virtualNow, persist: async () => {} });
const rateOutcome = runC.perInstance.find((item) => item.instanceIndex === 4);
const slotOutcome = runC2.perInstance.find((item) => item.instanceIndex === 5);
check("C 429 实例退避重排后最终到待批", rateOutcome?.outcome === "awaiting_approval" && runC.queue.entries.find((entry) => entry.instanceIndex === 4)?.congestionRetries === 1, JSON.stringify({ rateOutcome, rateWaits }));
check("C 429 退避等待 = 4000ms（指数退避起点）且只退一次", JSON.stringify(rateWaits) === JSON.stringify([4000]), JSON.stringify(rateWaits));
check("C 一直 409 的实例：退避 4 次后如实失败并给可读上限文案", slotOutcome?.outcome === "failed" && /退避重排 4 次仍失败/.test(slotOutcome.reason || ""), JSON.stringify({ slotOutcome, slotWaits }));
check("C 409 退避序列 4s→8s→16s→32s", JSON.stringify(slotWaits) === JSON.stringify([4000, 8000, 16000, 32000]), JSON.stringify(slotWaits));
check("C 拥塞路径没有被误判成未知提交", runC.aggregate.heldSubmitUnknown.length === 0 && runC2.aggregate.heldSubmitUnknown.length === 0, JSON.stringify([runC.aggregate.heldSubmitUnknown, runC2.aggregate.heldSubmitUnknown]));

// ───────────── D. 未知提交对账（命中=resolved / 查无=explicit_unknown，均不重发）
const unknownBatchMeta = { templateId: templateSource.template.templateId, batchId: "batch-p6-unknown" };
const jobUnknown = makeJob({ index: 6, tag: "F", batchId: "batch-p6-unknown", chainOptions: { failAlways: "submit_unknown", markRunning: true }, batchMeta: unknownBatchMeta, recordPlan: (plan) => recordedPlans.set(6, plan) });
// 生成段失败时 P4 不会入册成员（入册发生在产物绑定之后）→ 这里按真实计划登记一份成员，
// 让「对账按成员节点查既有任务」走的是真节点 id（生产里对账同样以 P5 批次成员为输入）。
const preparedUnknown = await jobUnknown.prepare();
check("D 未知提交实例先落库（P3 materialize）", preparedUnknown.ok === true, JSON.stringify(preparedUnknown));
batches = batch.upsertBatchMember(batches, unknownBatchMeta, batch.buildBatchMember({ plan: recordedPlans.get(6), materials: jobUnknown.materials, nodes: canvas.nodes(), connections: canvas.connections(), lapian: { taskId: "task-p6-F", jobId: "job-p6-F" }, createdAt: "2026-11-13T00:00:00.000Z" }));
const unknownMember = batch.findBatchMember(batches.find((item) => item.batchId === "batch-p6-unknown"), jobUnknown.instanceId);
let reconcileCalls = 0;
const runD = await queue.runBatchQueue({
    batch: batchInputOf("batch-p6-unknown"),
    jobs: [jobUnknown],
    persist: async () => {},
    reconcile: async ({ guard }) => {
        reconcileCalls += 1;
        return queue.reconcileBatchSubmit({ guard, member: unknownMember, query: prodQuery, rounds: 1, sleep: async () => {}, lookupWanFileTask: (job) => wanMap.lookupWanFileTask(wanProject.listProjectWanFileTasks(), job) });
    },
});
const unknownEntry = runD.queue.entries.find((entry) => entry.instanceIndex === 6);
check("D 回执丢失：实例只跑一次（禁自动重发）", (generationCallsByTag.get("F") || 0) === 1, `generationCalls=${generationCallsByTag.get("F")}`);
check("D 对账命中既有后端任务 → resolved（按 taskId 续查不重建）", unknownEntry?.submitGuard?.reconcile?.verdict === "resolved" && runD.aggregate.heldSubmitUnknown[0]?.verdict === "resolved", JSON.stringify({ guard: unknownEntry?.submitGuard, held: runD.aggregate.heldSubmitUnknown }));
check("D 无回执时不造 receipt（禁止假装有回执）", unknownEntry?.submitGuard?.receipt === undefined, JSON.stringify(unknownEntry?.submitGuard));
check("D 对账确实走了生产查询口径（至少一次）", reconcileCalls === 1, String(reconcileCalls));

// 查不到 → explicit_unknown（节点无任务状态，生产查询口径查无）
const jobUnknown2 = makeJob({ index: 7, tag: "G", batchId: "batch-p6-unknown2", chainOptions: { failAlways: "submit_unknown", markRunning: false }, batchMeta: { templateId: templateSource.template.templateId, batchId: "batch-p6-unknown2" }, recordPlan: (plan) => recordedPlans.set(7, plan) });
const runD2 = await queue.runBatchQueue({
    batch: batchInputOf("batch-p6-unknown2"),
    jobs: [jobUnknown2],
    persist: async () => {},
    reconcile: async ({ guard }) => queue.reconcileBatchSubmit({ guard, query: prodQuery, rounds: 2, intervalMs: 5000, sleep: async () => {}, lookupWanFileTask: () => "" }),
});
const unknownEntry2 = runD2.queue.entries.find((entry) => entry.instanceIndex === 7);
check("D 对账查无 → explicit_unknown 停住交人", unknownEntry2?.submitGuard?.reconcile?.verdict === "explicit_unknown" && runD2.perInstance[0].outcome === "held_submit_unknown", JSON.stringify({ guard: unknownEntry2?.submitGuard, perInstance: runD2.perInstance }));
check("D 查无时同样只跑一次（零重复付费）", (generationCallsByTag.get("G") || 0) === 1, `generationCalls=${generationCallsByTag.get("G")}`);

// 守卫式提交：守卫先落盘再发（真 flush），回执丢失留盘
const guardQueue = queue.createBatchQueue({ members: [unknownMember], updatedAt: "2026-11-13T00:00:00.000Z" });
const guardEntry = queue.batchQueueEntry(guardQueue, unknownMember);
const guardOrder = [];
let submitCalls = 0;
const guarded = await queue.guardedBatchSubmit({
    queue: guardQueue,
    entry: guardEntry,
    spec: { nodeId: unknownMember.nodes.lapian, provider: "wan", requestDigest: "req-digest" },
    persist: async (next) => {
        const hasGuard = Boolean(next.entries.find((entry) => entry.instanceId === unknownMember.instanceId)?.submitGuard);
        guardOrder.push(`persist:${hasGuard}`);
        syncProject({ batchId: "batch-p6-unknown", queue: next });
        await useCanvasStore.getState().flushProjects("p6-submit-guard");
    },
    submit: async () => {
        submitCalls += 1;
        throw new Error("timeout of 300000ms exceeded（回执丢失）");
    },
    at: "2026-11-13T00:00:00.000Z",
});
check("D 守卫式提交：先落盘守卫再发请求", guardOrder[0] === "persist:true" && submitCalls === 1, JSON.stringify({ guardOrder, submitCalls }));
check("D 守卫式提交：回执丢失 → 分类 submit_unknown 且守卫留盘无 receipt", guarded.ok === false && guarded.failure?.kind === "submit_unknown" && guarded.entry.submitGuard?.receipt === undefined, JSON.stringify({ failure: guarded.failure, guard: guarded.entry.submitGuard }));
check("D 守卫已随项目落盘（刷新后仍能知道「可能发过一次」）", readPersisted().includes("req-digest"), readPersisted().slice(0, 120));

// ───────────── E. 失败隔离（中间实例组装失败，其余照常）
const batchEMeta = { templateId: templateSource.template.templateId, batchId: "batch-p6-isolate" };
const jobsE = [8, 9, 10].map((index) => {
    const tag = ["H", "I", "J"][index - 8];
    const job = makeJob({
        index,
        tag,
        batchId: "batch-p6-isolate",
        chainOptions: index === 9 ? { failAlways: "hard" } : {},
        batchMeta: batchEMeta,
        recordPlan: (plan) => recordedPlans.set(index, plan),
    });
    batches = batch.upsertBatchMember(batches, batchEMeta, { instanceId: job.instanceId, templateId: templateSource.template.templateId, instanceIndex: index, materials: batch.summarizeMaterials(job.materials), nodes: { all: [job.instanceId], sourceVideo: job.instanceId, lapian: job.instanceId, retiredPlaceholders: [], downstream: [] }, hub: null, backend: { lapianTaskId: `task-p6-${tag}`, lapianJobId: `job-p6-${tag}` }, createdAt: "2026-11-13T00:00:00.000Z" });
    return job;
});
const runE = await queue.runBatchQueue({
    batch: batchInputOf("batch-p6-isolate"),
    jobs: jobsE,
    persist: async () => {},
});
check("E 中间实例失败、其余两实例照常到待批", runE.aggregate.awaitingApproval === 2 && runE.aggregate.failed.length === 1, JSON.stringify(runE.perInstance));
check("E 聚合带 per-instance 失败原因（可定位到实例与阶段）", runE.aggregate.failed[0]?.instanceId.includes("#9") && /组装|Config/.test(runE.aggregate.failed[0]?.reason || ""), JSON.stringify(runE.aggregate.failed));

// ───────────── F. 恢复（中途暂停 → 新进程 rehydrate → 续跑剩余）
// 用 5 个实例（> 并发上限 3）：这样「跑完一部分就刷新」能真实留下未起跑的实例。
const batchFMeta = { templateId: templateSource.template.templateId, batchId: "batch-p6-resume" };
const resumeTags = { 11: "K", 12: "L", 13: "M", 14: "N", 15: "O" };
const jobsF = Object.keys(resumeTags).map(Number).map((index) => {
    const tag = resumeTags[index];
    const job = makeJob({ index, tag, batchId: "batch-p6-resume", chainOptions: {}, batchMeta: batchFMeta, recordPlan: (plan) => recordedPlans.set(index, plan) });
    batches = batch.upsertBatchMember(batches, batchFMeta, { instanceId: job.instanceId, templateId: templateSource.template.templateId, instanceIndex: index, materials: batch.summarizeMaterials(job.materials), nodes: { all: [job.instanceId], sourceVideo: job.instanceId, lapian: job.instanceId, retiredPlaceholders: [], downstream: [] }, hub: null, backend: { lapianTaskId: `task-p6-${tag}`, lapianJobId: `job-p6-${tag}` }, createdAt: "2026-11-13T00:00:00.000Z" });
    return job;
});
let pausedOnce = false;
let settledCount = 0;
let prevActive = 0;
const runF1 = await queue.runBatchQueue({
    batch: batchInputOf("batch-p6-resume"),
    jobs: jobsF,
    shouldPause: () => pausedOnce,
    // 第一个实例收口后立刻「刷新页面」——等价于用户中途关页
    onSlot: (sample) => {
        if (sample.active < prevActive) settledCount += 1;
        prevActive = sample.active;
        if (settledCount >= 1 && sample.active <= 2) pausedOnce = true;
    },
    persist: async (next) => {
        batches = queue.setBatchQueue(batches, "batch-p6-resume", next);
        syncProject();
        await useCanvasStore.getState().flushProjects("p6-resume");
    },
});
const pausedCount = runF1.perInstance.filter((item) => item.outcome === "awaiting_approval").length;
check("F 中途暂停：只完成部分实例、队列相位 paused", runF1.queue.phase === "paused" && pausedCount >= 1 && pausedCount < 5, JSON.stringify({ phase: runF1.queue.phase, perInstance: runF1.perInstance }));
check("F 排队簿记随项目落盘（刷新前磁盘上就有尝试次数）", readPersisted().includes("batch-p6-resume") && (batches.find((item) => item.batchId === "batch-p6-resume")?.queue?.entries || []).some((entry) => entry.attempts > 0));

const lapianRuns = () => lapianRunUrls.filter((item) => item.url === "/api/lapian/run");
const lapianRunsOf = (tag) => lapianRuns().filter((item) => item.tag === tag).length;
const materializedTagsBefore = Object.keys(resumeTags).map(Number).filter((index) => recordedPlans.has(index)).map((index) => resumeTags[index]);
const lapianBeforeByTag = Object.fromEntries(materializedTagsBefore.map((tag) => [tag, lapianRunsOf(tag)]));
const lapianCallsBeforeResume = lapianRuns().length;
const nodeCountBeforeResume = canvas.nodes().length;
syncProject();
await useCanvasStore.getState().flushProjects("p6-resume-final");
writeFileSync(PAYLOAD, readPersisted(), "utf8");
// 子进程必须继承 --out/--store：否则它会读默认 outDir 里上一次的陈旧 payload（rehydrate 断言就会「假绿」）。
const reloadChild = (job) =>
    JSON.parse(
        execFileSync(process.execPath, [path.resolve("scripts/probe-p6-batch-queue.mjs"), "--reload", "--out", outDir, "--store", storePath], {
            encoding: "utf8",
            stdio: ["ignore", "pipe", "pipe"],
            env: { ...process.env, P6_RELOAD_JOB: job },
        })
            .trim()
            .split("\n")
            .at(-1),
    );
const reloaded = reloadChild("none");
check("F 新进程真 rehydrate：store 已水合、项目还在", reloaded.hydrated === true && reloaded.projectId === projectId, JSON.stringify(reloaded));
const reloadedResume = (reloaded.batches || []).find((item) => item.batchId === "batch-p6-resume");
check("F 刷新后队列簿记恢复（相位/并发/条目数）", reloadedResume?.queue?.concurrency === 3 && reloadedResume?.queue?.entries === jobsF.length, JSON.stringify({ reloadedResume, expectedEntries: jobsF.length }));

// 续跑计划：派生状态（已完成的实例 = 待批）
const doneInstanceIds = runF1.perInstance.filter((item) => item.outcome === "awaiting_approval").map((item) => item.instanceId);
const resumeBatch = batches.find((item) => item.batchId === "batch-p6-resume");
const statuses = Object.fromEntries(resumeBatch.members.map((member) => [member.instanceId, doneInstanceIds.includes(member.instanceId) ? "awaiting_approval" : "pending"]));
const resumePlan = queue.planBatchResume({ batch: resumeBatch, queue: resumeBatch.queue, statuses, nodes: canvas.nodes() });
check("F 续跑计划：已完成实例跳过、剩余实例待跑", resumePlan.skippedDone.length === doneInstanceIds.length && resumePlan.run.length === jobsF.length - doneInstanceIds.length, JSON.stringify({ plan: resumePlan, doneInstanceIds }));
check("F 续跑计划：已落库实例不需要再 materialize（不重建节点/不重拉片）", resumePlan.prepareNeeded.every((id) => !doneInstanceIds.includes(id)), JSON.stringify(resumePlan.prepareNeeded));
const resumeJobs = jobsF.filter((job) => resumePlan.run.includes(job.instanceId));
const runF2 = await queue.runBatchQueue({
    batch: resumeBatch,
    jobs: resumeJobs,
    queue: { ...resumeBatch.queue, phase: "running" },
    resume: resumePlan,
    persist: async (next) => {
        batches = queue.setBatchQueue(batches, "batch-p6-resume", next);
        syncProject();
        await useCanvasStore.getState().flushProjects("p6-resume-continue");
    },
});
const lapianCallsAfterResume = lapianRuns().length;
check("F 续跑后剩余实例全部到待批（整批 5 个）", runF2.aggregate.awaitingApproval + doneInstanceIds.length === jobsF.length && runF2.aggregate.failed.length === 0, JSON.stringify({ aggregate: runF2.aggregate, done: doneInstanceIds }));
check("F 续跑不重拉片（已落库实例的 lapian /run 调用数不变）", materializedTagsBefore.every((tag) => lapianRunsOf(tag) === lapianBeforeByTag[tag]), JSON.stringify({ before: lapianBeforeByTag, after: Object.fromEntries(materializedTagsBefore.map((tag) => [tag, lapianRunsOf(tag)])), total: [lapianCallsBeforeResume, lapianCallsAfterResume] }));
check("F 续跑不重建已完成实例的节点（画布节点数不减少）", canvas.nodes().length >= nodeCountBeforeResume, `${nodeCountBeforeResume} → ${canvas.nodes().length}`);
check("F 续跑时已完成实例零重跑（其生成调用次数不变）", doneInstanceIds.every((id) => {
    const index = Number(String(id).split("#")[1]);
    return (generationCallsByTag.get(resumeTags[index]) || 0) === 1;
}), JSON.stringify({ done: doneInstanceIds, calls: Object.fromEntries(generationCallsByTag) }));

// ───────────── G. S2：Wan 成片映射随项目持久化 + 导出/导入 + 老行为兼容
const axiosCalls = [];
axios.defaults.adapter = async (config) => {
    axiosCalls.push({ url: config.url, method: config.method });
    return { data: { job: "wan-job-s2", taskId: "wan-file-s2" }, status: 200, statusText: "OK", headers: {}, config };
};
const wanConfig = {
    ...configStore.defaultConfig,
    baseUrl: "http://127.0.0.1:8779",
    apiFormat: "wan",
    channels: [{ id: "default", name: "wan", baseUrl: "http://127.0.0.1:8779", apiKey: "", apiFormat: "wan", models: [{ name: "wan-2.6", capability: "video" }] }],
    model: "wan-2.6",
    videoModel: "wan-2.6",
};
const created = await videoApi.createVideoGenerationTask(wanConfig, "P6 探针：成片映射持久化", []);
check("G 生产创建函数走通（axios adapter 替身，零真实网络）", created.id === "wan-job-s2" && axiosCalls.length === 1 && String(axiosCalls[0].url).includes("/api/wan/video/run"), JSON.stringify({ created, axiosCalls }));
const projectAfterCreate = useCanvasStore.getState().projects.find((item) => item.id === projectId);
const wanRecord = (projectAfterCreate?.workflowWanFileTasks || []).find((item) => item.job === "wan-job-s2");
check("G job→成片 taskId 已写进项目登记表", wanRecord?.fileTaskId === "wan-file-s2", JSON.stringify(projectAfterCreate?.workflowWanFileTasks));
check("G 生产查找口径可查回（video.ts recall 走的就是它）", wanProject.lookupProjectWanFileTask("wan-job-s2") === "wan-file-s2");
check("G 老 localStorage 键仍被写入（兼容不破坏）", (() => {
    const raw = memory.get("wan_video_file_tasks");
    if (!raw) return false;
    const parsed = JSON.parse(raw);
    return parsed["wan-job-s2"] === "wan-file-s2";
})(), memory.get("wan_video_file_tasks"));
await useCanvasStore.getState().flushProjects("p6-wan-persist");
check("G 成片映射已随项目落盘", readPersisted().includes("wan-file-s2"));

// 导出（真 buildCanvasExport）→ 导入 → 可查
const reader = async () => new Blob(["x"], { type: "video/mp4" });
const exportBuild = await exporter.buildCanvasExport([useCanvasStore.getState().projects.find((item) => item.id === projectId)], reader);
const exportedProject = exportBuild.file.projects[0].project;
check("G 导出携带成片映射（v3 信封里的 project 字段）", (exportedProject.workflowWanFileTasks || []).some((item) => item.job === "wan-job-s2" && item.fileTaskId === "wan-file-s2"), JSON.stringify((exportedProject.workflowWanFileTasks || []).length));
const importedId = useCanvasStore.getState().importProject(JSON.parse(JSON.stringify(exportedProject)));
wanProject.bindWanFileTaskProject(importedId);
const importedProject = useCanvasStore.getState().openProject(importedId);
check("G 导入后成片映射仍在（显式透传）", (importedProject?.workflowWanFileTasks || []).some((item) => item.job === "wan-job-s2"), JSON.stringify(importedProject?.workflowWanFileTasks));
check("G 导入后按 job 可查成片 taskId", wanProject.lookupProjectWanFileTask("wan-job-s2") === "wan-file-s2");
check("G 导入同时带回批次/队列/实例记录（同族透传零丢失）", Boolean(importedProject?.workflowBatches?.length) && Boolean(importedId), JSON.stringify({ batches: importedProject?.workflowBatches?.length }));
wanProject.bindWanFileTaskProject(projectId);

// 老数据 200 条兼容回归
const legacyMany = Object.fromEntries(Array.from({ length: 250 }, (_, index) => [`legacy-job-${index}`, `legacy-task-${index}`]));
memory.set("wan_video_file_tasks", JSON.stringify(legacyMany));
const parsedLegacy = wanMap.parseLegacyWanFileTasks(memory.get("wan_video_file_tasks"), "2026-11-13T00:00:00.000Z");
check("G 老 localStorage 250 条仍可读（老数据不丢可解析）", parsedLegacy.length === 250 && wanMap.lookupWanFileTask(parsedLegacy, "legacy-job-0") === "legacy-task-0", String(parsedLegacy.length));
check("G 老载荷回写仍按 200 条截断（旧行为一致）", Object.keys(wanMap.toLegacyPayload(parsedLegacy)).length === 200 && Object.keys(wanMap.toLegacyPayload(parsedLegacy))[0] === "legacy-job-50");
// 真刷新一次：新进程 rehydrate 后，成片映射仍可查（这是 S2 的最终判据：映射活在项目里，不是内存 Map）
await useCanvasStore.getState().flushProjects("p6-s2-reload");
writeFileSync(PAYLOAD, readPersisted(), "utf8");
const reloadedS2 = reloadChild("wan-job-s2");
check("G 新进程真刷新后：成片映射随项目恢复且可查", reloadedS2.hydrated === true && reloadedS2.wanTasks >= 1 && reloadedS2.wanLookup === "wan-file-s2", JSON.stringify(reloadedS2));

const mergedView = wanMap.mergeWanFileTasks(parsedLegacy, projectAfterCreate?.workflowWanFileTasks);
check("G 项目登记表与老 localStorage 可合并成一张视图（>200 条不丢）", mergedView.length === 251 && wanMap.lookupWanFileTask(mergedView, "legacy-job-0") === "legacy-task-0" && wanMap.lookupWanFileTask(mergedView, "wan-job-s2") === "wan-file-s2", String(mergedView.length));

// ───────────── H. 零回归
const stateKeys = Object.keys(useCanvasStore.getState());
check("H store 既有方法面零破坏（7 个方法都在）", ["createProject", "importProject", "openProject", "renameProject", "deleteProjects", "replaceProjects", "updateProject"].every((name) => typeof useCanvasStore.getState()[name] === "function"), JSON.stringify(stateKeys));
check("H store 新增只加不改（flushProjects / lastFlush / workflowWanFileTasks 落点）", stateKeys.includes("flushProjects") && stateKeys.includes("lastFlush"));
const metadataKeys = new Set(canvas.nodes().flatMap((node) => Object.keys(node.metadata || {})));
const leaks = [...metadataKeys].filter((key) => /^(batchId|workflowBatch|batchMember|instanceStatus|queuePhase|submitGuard|approvalState)$/.test(key));
check("H C5：节点 metadata 里没有被塞入队列/批次字段", leaks.length === 0, JSON.stringify(leaks));
const paidOps = paid.collectPaidActions("canvas_apply_ops", { ops: appliedOpsLog }, snapshotOf(canvas.nodes()));
check("H P0b 闸复验：本卡全部 ops（含队列批跑/对账/恢复）过付费提取 = 0 条付费动作", paidOps.length === 0, JSON.stringify(paidOps).slice(0, 200));
check("H 本轮确实应用过 ops（付费闸检查不是空集）", appliedOpsLog.length > 0, `ops=${appliedOpsLog.length}`);
check("H 真实样本项目零改动（只读：内存副本上操作）", JSON.stringify(realProject) === realNodesBefore);

// ---------------------------------------------------------------- 汇总
const summary = {
    pass,
    fail: fails.length,
    unitTotal,
    unit: unitSummary,
    concurrency: { peak: runB.concurrencyPeak, curve },
    batchB: { batchId, members: batchBoard.members.length, perInstance: runB.perInstance, aggregate: runB.aggregate },
    derived: { counts: viewB.counts, outcome: viewB.outcome, members: viewB.members.map((member) => ({ instanceId: member.instanceId, status: member.status, rule: member.rule })) },
    congestion: { rateWaits, slotWaits, rate: rateOutcome, slot: slotOutcome, requeues: runC.aggregate.congestionRequeues + runC2.aggregate.congestionRequeues },
    unknownSubmit: { resolved: runD.aggregate.heldSubmitUnknown, explicitUnknown: runD2.aggregate.heldSubmitUnknown, guardOnDisk: readPersisted().includes("req-digest") },
    isolation: runE.aggregate.failed,
    resume: { phaseBefore: runF1.queue.phase, done: doneInstanceIds, plan: { run: resumePlan.run, skippedDone: resumePlan.skippedDone.map((item) => item.instanceId), prepareNeeded: resumePlan.prepareNeeded }, lapianCalls: { before: lapianCallsBeforeResume, after: lapianCallsAfterResume }, reloaded: { batches: reloaded.batches?.length, wanTasks: reloaded.wanTasks } },
    s2: { record: wanRecord, legacyCount: Object.keys(wanMap.toLegacyPayload(parsedLegacy)).length, merged: mergedView.length, exported: (exportedProject.workflowWanFileTasks || []).length },
    results,
};
writeFileSync(path.join(outDir, "probe-summary.json"), JSON.stringify(summary, null, 2), "utf8");
finish();

function finish() {
    // 探针自建自清：临时打包目录（node_modules/.probe-p6）不留残余。
    rmSync(bundleDir, { recursive: true, force: true });
    console.log(`\nP6 队列批跑+恢复探针：${pass} 通过 / ${fails.length} 失败（单测 ${unitTotal} 例）`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(fails.length ? 1 : 0);
}
