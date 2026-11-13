/**
 * P4 探针：新视频 → 拉片 → 组装 → 自检 → 停在待批（单实例端到端第一链 + hub 身份绑定 + G18 输出身份 + 槽真实生效）。
 *
 * 跑法：cd web && node scripts/probe-p4-instance-run.mjs
 *   --store <real-canvas-store.json> --project <项目标题或 id> --out <输出目录>
 *
 * 分段：
 *   A. 单测：esbuild 打包 `workflow-instance-run.test.ts` → `node --test` 真跑。
 *   B. 真实样本端到端：真实「无限画布 3」项目抽模板 → P3 实例化（stub 网络，回填本次拉片正文）
 *      → P4 编排器：可选段投影刷新 → 五槽校验 → 组装（真实 buildNodeGenerationContext + 生成链替身）
 *      → G18 产物绑定（下游 token/连线换到本次产物、占位产物退役）→ hub 身份（stub 客户端）
 *      → 停在待批。**零真实付费**：只有 mode=text 的组装调用，且全程无 run_generation op。
 *   C. 槽真实生效：改挂图（含传参序打乱）→ 实际请求图序跟槽序走；切可选段 → 本次发送正文真的变。
 *   D. 重跑：同实例再跑一次 → 旧产物退役、下游改吃新产物、无悬空引用。
 *
 * 失败 rc=1（禁降级：任何断言失败如实计入 fails，不跳过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

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
const outDir = path.resolve(argOf("out", "C:/Users/leon3/AppData/Local/hermes/cache/scratch/p4-out"));
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

// ---------------------------------------------------------------- A. 单测
const testBundle = await bundle("src/lib/canvas/workflow-instance-run.test.ts", "p4-instance-run.test.mjs", {
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
const tapFails = tapCount("fail");
check("A 单测进程零失败", !testFailed, testFailed ? testOutput.split("\n").filter((line) => line.includes("✖") || line.includes("not ok")).slice(0, 6).join(" | ") : "");
check("A 单测用例数 ≥ 10", Number(tapTests || 0) >= 10, `tests=${tapTests}`);
check("A 单测 fail 计数为 0", tapFails === 0, `fail=${tapFails}`);
writeFileSync(path.join(outDir, "unit-tests.tap"), testOutput, "utf8");

// ---------------------------------------------------------------- 准备模块（全部真实现，不复制）
// 必须**同一个 bundle**：esbuild 每个 bundle 是一份独立模块图，node-registry 会被复制成多份，
// 分开打包会让 ops 侧认不到 instancer 注册的节点类型（实测全部退化成 Text）。
const modulesEntry = path.join(bundleDir, "p4-modules-entry.ts");
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
        `export * as paid from ${JSON.stringify(path.resolve("..", "canvas-agent/src/canvas/paid-actions.ts").split(path.sep).join("/"))};`,
        "",
    ].join("\n"),
    "utf8",
);
const modules = await import(pathToFileURL(await bundle(modulesEntry, "p4-modules.mjs")).href);
const { builtin, workflow, instancer, runner, ops, genctx, paid } = modules;
const identityModule = modules.identity;
builtin.registerBuiltinNodes();

// ---------------------------------------------------------------- B. 真实样本模板
const store = JSON.parse(readFileSync(storePath, "utf8"));
const projects = (store.state && store.state.projects) || [];
const project = projects.find((item) => item.id === projectKey) || projects.find((item) => item.title === projectKey);
check("B 找到真实样本项目", Boolean(project), `projects=${projects.map((item) => item.title).join(",")}`);
if (!project) {
    finish();
}
const nodes = project.nodes || [];
const connections = project.connections || [];
const byId = new Map(nodes.map((node) => [node.id, node]));
const outgoing = (id) => connections.filter((connection) => connection.fromNodeId === id).map((connection) => connection.toNodeId);
const incoming = (id) => connections.filter((connection) => connection.toNodeId === id).map((connection) => connection.fromNodeId);

const lapians = nodes.filter((node) => node.type === "lapian" && node.metadata?.jobId);
const chainRoot = lapians.find((node) => nodes.some((candidate) => candidate.type === "config" && candidate.metadata?.generationMode === "text" && candidate.metadata?.lapianSourceId === node.id));
check("B 找到已跑通的拉片链根", Boolean(chainRoot), `lapians=${lapians.map((node) => node.id).join(",")}`);
if (!chainRoot) finish();
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
check("B 骨架边界齐全（video→lapian→config text→占位终稿→config video）", skeletonIds.length === 7 + fixedAssets.length && Boolean(sourceVideo && configText && finalText && configVideo), `边界=${skeletonIds.join(",")}`);

const skeleton = workflow.extractWorkflowTemplate({ sourceProject: project, selectedNodeIds: skeletonIds, name: `${project.title} · 骨架模板`, templateId: "p4skeleton", createdAt: "2026-11-13T00:00:00.000Z" });
const realSlots = nodes.filter((node) => node.metadata?.assetSlotSourceId === lapianId && node.metadata?.attachedImageId);
const slotImageIds = realSlots.map((slot) => slot.metadata?.attachedImageId).filter((id) => id && byId.has(id));
const grafts = nodes.filter((node) => node.metadata?.assetGraftForSlotId && realSlots.some((slot) => slot.id === node.metadata.assetGraftForSlotId));
const graftImageIds = grafts.map((node) => node.metadata?.assetGraftOfImageId).filter((id) => id && byId.has(id));
const fullIds = [...new Set([...skeletonIds, ...realSlots.map((slot) => slot.id), ...slotImageIds, ...grafts.map((node) => node.id), ...graftImageIds])];
const full = workflow.extractWorkflowTemplate({ sourceProject: project, selectedNodeIds: fullIds, name: `${project.title} · 全量模板`, templateId: "p4full", createdAt: "2026-11-13T00:00:00.000Z" });
check("B 全量模板含真实挂图的资产位", realSlots.length > 0 && full.template.slots[2].targets.length === realSlots.length, `slots=${realSlots.length} targets=${full.template.slots[2].targets.length}`);
check("B 全量模板自足（无悬空引用）", workflow.validateTemplateProject(full.project.nodes, full.project.connections).length === 0, JSON.stringify(workflow.validateTemplateProject(full.project.nodes, full.project.connections).slice(0, 3)));

// ---------------------------------------------------------------- 夹具：内存画布 / 网络 stub / 生成链替身 / hub
function makeCanvasStore() {
    let snapshot = { projectId: project.id, title: project.title, nodes: [], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
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

function makeLapianFetch({ taskId, jobId, markdown }) {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push({ url, method: String(init?.method || "GET").toUpperCase(), headers: init?.headers || {}, body: init?.body });
        const json = (payload) => ({ ok: true, status: 200, json: async () => payload });
        if (url === "/api/video/upload") return json({ id: taskId });
        if (url === "/api/lapian/run") return json({ job: jobId });
        if (url === `/api/job/${jobId}`) return json({ status: "done" });
        if (url === `/api/lapian/${encodeURIComponent(taskId)}`) return json({ markdown });
        throw new Error(`stub 未覆盖：${url}`);
    };
    return { fetchImpl, calls };
}

/** 投影 stub：**按 optional 参数真实改变正文**（槽⑤是否真生效，就靠这点区分）。 */
function makeProjectionFetch() {
    const calls = [];
    const fetchImpl = async (url) => {
        calls.push(url);
        const match = String(url).match(/\/api\/render\/([^/]+)\/([^?]+)\?optional=(.*)$/);
        if (!match) return { ok: false, status: 404, json: async () => ({ error: `stub 不认这个 URL：${url}` }) };
        const optional = decodeURIComponent(match[3] || "");
        const text = [`# 本次投影正文 · ${match[1]}`, `可选段：${optional || "（全关）"}`, optional ? `可选段内容 ${optional}：客户模特在雨夜街头转身。` : "正片结构：开场/推进/收尾。", "", "# 附注", "这行必须被截断"].join("\n");
        return { ok: true, status: 200, json: async () => ({ text }) };
    };
    return { fetchImpl, calls };
}

/** 生成链替身：与 `handleGenerateNode` 同一「建子节点 + Config→子节点」口径（正文=模型输出替身）。 */
let generationSeq = 0;   // 全局递增：重跑同实例也必须拿到新产物 id（生成链本来就是新 nanoid）
function makeGenerationChain(canvas, opts) {
    const calls = [];
    return {
        calls,
        run: async (input) => {
            calls.push({ nodeId: input.nodeId, mode: input.mode, prompt: input.prompt });
            // 真实链路在生成前用同一个 buildNodeGenerationContext 展开 token（这里真跑，证明同源）
            const context = genctx.buildNodeGenerationContext(input.nodeId, canvas.nodes(), canvas.connections(), input.prompt);
            generationSeq += 1;
            const childNodeId = `${opts.prefix}-out-${generationSeq}`;
            const content = `【本次产物 ${opts.token}-${generationSeq}】\n${context.prompt}`;
            await canvas.apply([
                { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 4000 + generationSeq * 100, y: 0 }, width: 320, height: 240, metadata: { content, prompt: content, status: "success" } },
                { type: "connect_nodes", fromNodeId: input.nodeId, toNodeId: childNodeId },
            ]);
            return { childNodeId, context };
        },
    };
}

function makeHubStub() {
    const calls = [];
    let promptText = "";
    const client = {
        async ingest(input) {
            calls.push({ kind: "ingest", filename: input.filename, bytes: input.blob?.size || 0, intent: input.intent });
            return { localId: "auto-9101", bridgeId: "bridge-p4-01", result: "OK" };
        },
        async uploadAsset(input) {
            calls.push({ kind: "asset", filename: input.filename, localId: input.localId });
            return { ref: `local:p4/${input.filename}`, source: "local" };
        },
        async getTask() {
            calls.push({ kind: "getTask" });
            return { state: "awaiting_approval", bridgeId: "bridge-p4-01" };
        },
        async saveRevision(input) {
            promptText = input.text;
            calls.push({ kind: "prompt", chars: input.text.length });
            return { state: "awaiting_approval", approvalSnapshot: "snap-p4-01", promptEditedAt: "2026-11-13T00:00:00.000Z", selfcheck: [{ 序号: 1, 结论: "PASS" }, { 序号: 2, 结论: "WARN", 说明: "探针夹具" }] };
        },
        async approve() {
            calls.push({ kind: "approve" });
            return { state: "approved", snapshot_sha256: "snap-p4-01" };
        },
        async rollback() {
            calls.push({ kind: "rollback" });
            return { state: "awaiting_assets" };
        },
        async revoke() {
            calls.push({ kind: "revoke" });
            return { state: "awaiting_approval" };
        },
    };
    return { client, calls, promptText: () => promptText };
}

const imageAttachOf = (slot, imageId) => ({ slotNodeId: slot.id, imageNodeId: imageId || slot.metadata?.attachedImageId });
const materials = (tag, extra = {}) => ({
    sourceVideo: { storageKey: `video:new-${tag}`, title: `新素材-${tag}.mp4`, content: `blob:http://127.0.0.1:3000/new-${tag}`, mimeType: "video/mp4", bytes: 5000 + tag.length, durationMs: 12000 },
    intentText: `把主体换成客户模特（${tag}）`,
    optionalSegments: "A,C",
    assetAttach: realSlots.map((slot) => imageAttachOf(slot)),
    generationParams: [{ nodeId: configText.id, values: { model: `default::${tag.toLowerCase()}-text-model` } }],
    ...extra,
});

const baseInput = (templateSource, index, mats) => ({ template: templateSource.template, templateNodes: templateSource.project.nodes, templateConnections: templateSource.project.connections, materials: mats, instanceIndex: index, origin: { x: 2000 * index, y: 0 }, unnamedCaption: "未命名参考图" });

const markdown = (tag) => `# 本次拉片正文 · ${tag} · 唯一标记 ${tag.toLowerCase()}7f3a\n## 一 剧情\n客户模特转身\n## 二 镜头\n推近`;
async function materialize(templateSource, index, mats, canvas, tag) {
    const stub = makeLapianFetch({ taskId: `task-p4-${tag}`, jobId: `job-p4-${tag}`, markdown: markdown(tag) });
    const created = await instancer.materializeWorkflowInstance(
        { ...baseInput(templateSource, index, mats), lapianConfig: { lapianDefaultModel: "", channels: [] }, contentWait: { sleep: async () => {} } },
        { getBlob: async () => new Blob(["new-video-bytes"], { type: "video/mp4" }), fetch: stub.fetchImpl, applyOps: canvas.apply, existingNodes: canvas.nodes },
    );
    console.log(`[debug] materialize ${tag}: status=${created.status} canvasNodes=${canvas.nodes().length} ops=${(created.appliedOps || []).length}`);
    return { created, stub };
}

// ---------------------------------------------------------------- C. 实例 1：全链跑到待批
const canvas = makeCanvasStore();
const matsA = materials("A");
const { created: createdA } = await materialize(full, 1, matsA, canvas, "A");
check("C 实例 1 落库（created）", createdA.status === "created", JSON.stringify(createdA.incomplete || {}));
const planA = createdA.plan;

writeFileSync(path.join(outDir, "materialize-A.json"), JSON.stringify({ status: createdA.status, incomplete: createdA.incomplete, lapian: createdA.lapian }, null, 2), "utf8");
writeFileSync(path.join(outDir, "canvas-after-materialize.json"), JSON.stringify({ lapianNodeId: planA.lapianNodeId, nodes: canvas.nodes().map((node) => ({ id: node.id, type: node.type, mode: node.metadata?.generationMode, lapianSourceId: node.metadata?.lapianSourceId })) }, null, 2), "utf8");
const projectionA = makeProjectionFetch();
const generationA = makeGenerationChain(canvas, { prefix: "p4a", token: "P4TOKENA" });
const hubA = makeHubStub();
const identities = [];
const runA = await runner.runWorkflowInstance(
    { plan: planA, materials: matsA, lapian: { taskId: "task-p4-A", jobId: "job-p4-A" } },
    {
        nodes: canvas.nodes,
        connections: canvas.connections,
        applyOps: canvas.apply,
        runGeneration: generationA.run,
        fetchProjection: projectionA.fetchImpl,
        hub: hubA.client,
        getBlob: async (storageKey) => new Blob([`bytes-of-${storageKey}`], { type: String(storageKey).startsWith("image:") ? "image/png" : "video/mp4" }),
        saveIdentity: (record) => void identities.push(record),
        newRunToken: () => "runTokenA",
        now: () => "2026-11-13T00:00:00.000Z",
    },
);
writeFileSync(path.join(outDir, "run-A.json"), JSON.stringify({ result: runA, ops: runA.appliedOps }, null, 2), "utf8");
// 跨语言交接：画布编排器算出的**本次正文**原样落盘，供 hub 侧探针（probe_p4_hub_identity.py --assembled）消费，
// 两端比对的必须是同一份字节（不是各自再算一遍）。
if (runA.assembly?.prompt) writeFileSync(path.join(outDir, "assembly-A.txt"), runA.assembly.prompt, "utf8");
check("C 实例 1 全链成功（无 failure）", !runA.failure, JSON.stringify(runA.failure));
check("C 实例状态=待审批（停在付费闸前）", runA.status === "awaiting_approval", runA.status);
check("C 五槽代入校验 5/5 PASS", runA.slotChecks.length === 5 && runA.slotChecks.every((item) => item.ok), JSON.stringify(runA.slotChecks.filter((item) => !item.ok)));
check("C 组装只调文本链一次（非付费）", runA.generationCalls.length === 1 && runA.generationCalls[0].mode === "text", JSON.stringify(runA.generationCalls));
check("C 付费视频创建调用 = 0（P0b 闸前置）", runA.paidVideoCreations === 0, String(runA.paidVideoCreations));
check("C 提交的 ops 里零 run_generation", runA.appliedOps.every((op) => op.type !== "run_generation"));
check("C P0b 闸在位复验：这些 ops 过付费提取 = 0 条付费动作", paid.collectPaidActions("canvas_apply_ops", { ops: runA.appliedOps }, { projectId: project.id, title: project.title, nodes: canvas.nodes(), connections: canvas.connections(), selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } }).length === 0);

check("C 可选段投影按本次字母请求（?optional=A,C）", projectionA.calls.length === 1 && projectionA.calls[0].includes("optional=A%2CC"), projectionA.calls.join(","));
check("C 投影正文已写进拉片节点（changed=true）", runA.assembly?.projection?.changed === true && String(canvas.nodes().find((node) => node.id === planA.lapianNodeId)?.metadata?.content || "").includes("本次投影正文"));
const assemblyA = runA.assembly;
check("C 本次发送正文 = 既有 buildNodeGenerationContext 同源算出", assemblyA && genctx.buildNodeGenerationContext(assemblyA.configNodeId, canvas.nodes(), canvas.connections(), assemblyA.composerContent).prompt.trim() === assemblyA.prompt);
check("C 本次正文含拉片投影 + 意图 + 挂图标签", Boolean(assemblyA) && assemblyA.prompt.includes("本次投影正文") && assemblyA.prompt.includes("客户模特（A）") && assemblyA.prompt.includes("参考图"));

// G18：产物身份
const productNodeId = assemblyA?.productNodeId || "";
const productNode = canvas.nodes().find((node) => node.id === productNodeId);
check("C 本次产物节点 id 由生成链回报（不是猜的）", Boolean(productNodeId) && Boolean(productNode), `product=${productNodeId}`);
check("C 本次产物带唯一标识串（下游消费可判定）", Boolean(productNode) && String(productNode.metadata?.content || "").includes("P4TOKENA-"));
const placeholderId = planA.nodeIdMap[finalText.id];
check("C 模板占位产物已退役（节点不在画布上）", !canvas.nodes().some((node) => node.id === placeholderId), placeholderId);
const videoConfigNodeId = planA.nodeIdMap[configVideo.id];
const videoConfigNode = canvas.nodes().find((node) => node.id === videoConfigNodeId);
check("C 下游 token 指向本次产物（G18 判据）", String(videoConfigNode?.metadata?.composerContent || "").includes(`@[node:${productNodeId}]`) && !String(videoConfigNode?.metadata?.composerContent || "").includes(`@[node:${placeholderId}]`), String(videoConfigNode?.metadata?.composerContent || "").slice(-80));
check("C 下游连线指向本次产物（占位产物那条边已随节点删除）", canvas.connections().some((connection) => connection.fromNodeId === productNodeId && connection.toNodeId === videoConfigNodeId) && !canvas.connections().some((connection) => connection.fromNodeId === placeholderId));
const downstreamPrompt = genctx.buildNodeGenerationContext(videoConfigNodeId, canvas.nodes(), canvas.connections(), String(videoConfigNode?.metadata?.composerContent || "")).prompt;
check("C 下游实际消费本次产物正文（含唯一标识串）", downstreamPrompt.includes("P4TOKENA-"), downstreamPrompt.slice(-120));
check("C 全实例零悬空引用（token 与连线端点都落在画布上）", (() => {
    const ids = new Set(canvas.nodes().map((node) => node.id));
    const danglingTokens = canvas.nodes().flatMap((node) => [...String(node.metadata?.composerContent || "").matchAll(/@\[node:([^\]]+)\]/g)].map((match) => match[1])).filter((id) => !ids.has(id));
    const danglingEdges = canvas.connections().filter((connection) => !ids.has(connection.fromNodeId) || !ids.has(connection.toNodeId));
    return danglingTokens.length === 0 && danglingEdges.length === 0;
})());

// hub 身份绑定
const identityA = runA.identity;
check("C hub 身份：ingest → 资产按槽序 → 正文保存（同一 local_id）", hubA.calls[0]?.kind === "ingest" && hubA.calls.filter((call) => call.kind === "asset").length === realSlots.length && hubA.calls.at(-1)?.kind === "prompt" && hubA.calls.every((call) => !call.localId || call.localId === "auto-9101"), JSON.stringify(hubA.calls.map((call) => call.kind)));
check("C hub 身份：local_id/bridge_id 与 ingest 回执一致", identityA?.hub?.localId === "auto-9101" && identityA?.hub?.bridgeId === "bridge-p4-01");
check("C hub 身份：修订 digest = 本次正文 digest", identityA?.hub?.revision.digest === workflow.stableDigest(hubA.promptText()), `${identityA?.hub?.revision.digest} vs ${workflow.stableDigest(hubA.promptText())}`);
check("C hub 身份：保存的正文就是本次产物正文（逐字节）", hubA.promptText() === String(productNode?.metadata?.content || "").trim());
check("C hub 身份：资产引用按槽序记录", identityA?.hub?.assets.length === realSlots.length && identityA.hub.assets.every((asset, index) => asset.imageNodeId === planA.assetAttaches[index].imageNodeId));
check("C hub 身份：审批快照已回填（快照是批准绑定物）", Boolean(identityA?.hub?.revision.approvalSnapshot));
check("C hub 自检结果原样回传（G16：不另写评分器）", runA.hub?.selfcheck.length === 2 && runA.hub?.selfcheckFailures === 1 && runA.warnings.some((line) => line.includes("自检")));
check("C 身份记录缺口为空（四件套齐）", identityModule.instanceIdentityGaps(identityA).length === 0, JSON.stringify(identityModule.instanceIdentityGaps(identityA)));
check("C 身份记录已落库（随项目持久化 seam 被调用）", identities.length === 1 && identities[0].instanceId === planA.instanceId);

// 审批语义对接（既有函数；真实端点在隔离实例里由 probe_p4_hub_identity.py 验）
const approved = await hubA.client.approve("auto-9101");
const rolledBack = await hubA.client.rollback("auto-9101");
const revoked = await hubA.client.revoke("auto-9101");
check("C 既有 approve/rollback/revoke 可消费（stub 契约）", approved.state === "approved" && rolledBack.state === "awaiting_assets" && revoked.state === "awaiting_approval");
const revisionHook = runner.planRevisionChange({ record: identityA, nextTextDigest: "ffffffff", hubState: "approved" });
check("C 改正文挂钩：已批准 → 必须先 revoke（revoke_required）", revisionHook.action === "revoke_required", revisionHook.reason);

// ---------------------------------------------------------------- D. 槽真实生效：挂图改序 + 可选段
const slotImages = realSlots.map((slot) => slot.metadata?.attachedImageId).filter(Boolean);
// 传参序打乱（倒序）：请求图序必须仍跟**槽序**走（token 出现序），不跟传参序走
const shuffledAttach = [...realSlots].reverse().map((slot) => imageAttachOf(slot));
const canvasOrder = makeCanvasStore();
const matsOrder = materials("ORD", { assetAttach: shuffledAttach, optionalSegments: "A" });
const { created: createdOrder } = await materialize(full, 1, matsOrder, canvasOrder, "ORD");
check("D 打乱挂图传参序的实例落库", createdOrder.status === "created", JSON.stringify(createdOrder.incomplete || {}));
const runOrder = await runner.runWorkflowInstance(
    { plan: createdOrder.plan, materials: matsOrder, lapian: { taskId: "task-p4-ORD", jobId: "job-p4-ORD" } },
    { nodes: canvasOrder.nodes, connections: canvasOrder.connections, applyOps: canvasOrder.apply, runGeneration: makeGenerationChain(canvasOrder, { prefix: "p4ord", token: "P4TOKENORD" }).run, fetchProjection: makeProjectionFetch().fetchImpl, newRunToken: () => "runTokenORD" },
);
check("D 打乱传参序不影响五槽校验（槽序是唯一真源）", !runOrder.failure, JSON.stringify(runOrder.failure));
const orderCheck = runOrder.slotChecks.find((item) => item.slotKind === "assetAttach");
check("D 实际请求图序 = 槽序（与传参序无关）", Boolean(orderCheck?.ok) && JSON.stringify(orderCheck.evidence.actualAttachedOrder) === JSON.stringify(orderCheck.evidence.referenceImageIds.filter((id) => orderCheck.evidence.actualAttachedOrder.includes(id))), JSON.stringify(orderCheck?.evidence || {}).slice(0, 300));
check("D 请求图序首项 = 槽 0 的挂图", orderCheck.evidence.actualAttachedOrder[0] === createdOrder.plan.nodeIdMap[slotImages[0]], `${orderCheck.evidence.actualAttachedOrder[0]} vs ${createdOrder.plan.nodeIdMap[slotImages[0]]}`);

// 换某槽挂图（槽 0 换成槽 1 的图）→ 请求图序跟着变
const swappedAttach = realSlots.map((slot, index) => imageAttachOf(slot, index === 0 ? slotImages[1] : index === 1 ? slotImages[0] : undefined));
const canvasSwap = makeCanvasStore();
const matsSwap = materials("SWAP", { assetAttach: swappedAttach });
const { created: createdSwap } = await materialize(full, 1, matsSwap, canvasSwap, "SWAP");
// 换图只改槽③：materialize 已按新挂图落库（P3 的 upsertLapianAssetManifest 同源重算）
const runSwap = await runner.runWorkflowInstance(
    { plan: createdSwap.plan, materials: matsSwap, lapian: { taskId: "task-p4-SWAP", jobId: "job-p4-SWAP" } },
    { nodes: canvasSwap.nodes, connections: canvasSwap.connections, applyOps: canvasSwap.apply, runGeneration: makeGenerationChain(canvasSwap, { prefix: "p4swap", token: "P4TOKENSWAP" }).run, fetchProjection: makeProjectionFetch().fetchImpl, newRunToken: () => "runTokenSWAP" },
);
const swapCheck = runSwap.slotChecks.find((item) => item.slotKind === "assetAttach");
check("D 换槽 0/1 挂图 → 请求图序前两位互换", Boolean(swapCheck?.ok) && swapCheck.evidence.actualAttachedOrder[0] === createdSwap.plan.nodeIdMap[slotImages[1]] && swapCheck.evidence.actualAttachedOrder[1] === createdSwap.plan.nodeIdMap[slotImages[0]], JSON.stringify(swapCheck?.evidence?.actualAttachedOrder || []).slice(0, 200));

// 可选段：切段 → 本次发送正文真的变（不只是 metadata）
const canvasB = makeCanvasStore();
const matsB = materials("B", { optionalSegments: "B" });
const { created: createdB } = await materialize(full, 2, matsB, canvasB, "B");
const projectionB = makeProjectionFetch();
const runB = await runner.runWorkflowInstance(
    { plan: createdB.plan, materials: matsB, lapian: { taskId: "task-p4-B", jobId: "job-p4-B" } },
    { nodes: canvasB.nodes, connections: canvasB.connections, applyOps: canvasB.apply, runGeneration: makeGenerationChain(canvasB, { prefix: "p4b", token: "P4TOKENB" }).run, fetchProjection: projectionB.fetchImpl, newRunToken: () => "runTokenB" },
);
check("D 可选段 B：投影 URL 只带 B", projectionB.calls[0]?.includes("optional=B") && !projectionB.calls[0]?.includes("%2CC"), projectionB.calls.join(","));
check("D 可选段 B：本次正文与实例 1（A,C）不同（真实生效，不是 metadata 变）", Boolean(runA.assembly && runB.assembly) && runB.assembly.prompt !== runA.assembly.prompt && runB.assembly.prompt.includes("可选段内容 B") && !runB.assembly.prompt.includes("可选段内容 A,C"));
check("D 可选段 B：拉片节点正文也被换成 B 的投影", String(canvasB.nodes().find((node) => node.id === createdB.plan.lapianNodeId)?.metadata?.content || "").includes("可选段：B"));
check("D 两条实例各自正文互不串（token 各指自己产物）", String(productNode?.metadata?.content || "").includes("P4TOKENA-") && String(canvasB.nodes().find((node) => node.id === runB.assembly?.productNodeId)?.metadata?.content || "").includes("P4TOKENB-") && canvasB.nodes().every((node) => !String(node.metadata?.content || "").includes("P4TOKENA-")));

// ---------------------------------------------------------------- E. 同实例重跑：旧产物退役
const rerun = await runner.runWorkflowInstance(
    { plan: planA, materials: matsA, lapian: { taskId: "task-p4-A", jobId: "job-p4-A" } },
    { nodes: canvas.nodes, connections: canvas.connections, applyOps: canvas.apply, runGeneration: makeGenerationChain(canvas, { prefix: "p4a", token: "P4TOKENA2" }).run, fetchProjection: makeProjectionFetch().fetchImpl, newRunToken: () => "runTokenA2" },
);
check("E 重跑成功", !rerun.failure, JSON.stringify(rerun.failure));
const product2 = rerun.assembly?.productNodeId || "";
check("E 重跑换了新产物", Boolean(product2) && product2 !== productNodeId, `${product2} vs ${productNodeId}`);
check("E 重跑后旧产物已退役", !canvas.nodes().some((node) => node.id === productNodeId));
const videoConfig2 = canvas.nodes().find((node) => node.id === videoConfigNodeId);
check("E 重跑后下游指向新产物", String(videoConfig2?.metadata?.composerContent || "").includes(`@[node:${product2}]`) && String(canvas.nodes().find((node) => node.id === product2)?.metadata?.content || "").includes("P4TOKENA2"));
check("E 重跑后零悬空引用", (() => {
    const ids = new Set(canvas.nodes().map((node) => node.id));
    return canvas.nodes().flatMap((node) => [...String(node.metadata?.composerContent || "").matchAll(/@\[node:([^\]]+)\]/g)].map((match) => match[1])).every((id) => ids.has(id)) && canvas.connections().every((connection) => ids.has(connection.fromNodeId) && ids.has(connection.toNodeId));
})());

// ---------------------------------------------------------------- 汇总
writeFileSync(path.join(outDir, "probe-summary.json"), JSON.stringify({ pass, fail: fails.length, results }, null, 2), "utf8");
finish();

function finish() {
    console.log(`\nP4 单实例端到端探针：${pass} 通过 / ${fails.length} 失败`);
    fails.forEach((item) => console.log(`  FAIL ${item}`));
    process.exit(fails.length ? 1 : 0);
}
