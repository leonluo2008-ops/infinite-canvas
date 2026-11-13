/**
 * P4 单实例编排器单测（node:test + node:assert；经 `scripts/probe-p4-instance-run.mjs` 打包后真跑）。
 *
 * 覆盖：
 *  A. 组装计划：组装 Config 唯一定位 / 本次正文（既有 buildNodeGenerationContext）/ 占位产物与下游识别
 *  B. G18 产物绑定：下游 token 与连线都换成本次产物 / 占位产物删除
 *  C. 五槽代入校验：全过；意图不一致/可选段只改 metadata/挂图序错 → 逐槽 FAIL（不静默）
 *  D. 编排器：文本链只调一次（零付费视频）/ 产物绑定后半成品不能冒充 / 生成链不回报子节点 → 失败
 *  E. hub 身份：ingest→资产→正文保存（自检原样回传）→身份记录；修订变更 → revoke_required
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildNodeGenerationContext } from "@/components/canvas/canvas-node-generation";
import { registerBuiltinNodes } from "@/components/canvas/nodes/builtin-nodes";
import { applyCanvasAgentOps, type CanvasAgentOp, type CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";
import { extractWorkflowTemplate, TEMPLATE_INTENT_PLACEHOLDER } from "@/lib/canvas/workflow-template";
import { planWorkflowInstance, type PlanWorkflowInstanceInput, type WorkflowInstanceMaterials } from "@/lib/canvas/workflow-instance";
import {
    buildProductBindingOps,
    planInstanceAssembly,
    planRevisionChange,
    runWorkflowInstance,
    verifySlotSubstitution,
    type WorkflowInstanceHubClient,
    type WorkflowInstanceIdentityRecord,
} from "@/lib/canvas/workflow-instance-run";
import { instanceIdentityGaps, upsertInstanceIdentity } from "@/lib/canvas/workflow-instance-identity";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

// 与页面启动同口径：注册**真实**内置节点定义（含 lapian 的 resource=文本数据源——
// 少了它，拉片正文根本不会进组装正文，测试会假过/假红）。
registerBuiltinNodes();

const IMG1 = "img-1";
const IMG2 = "img-2";
const SLOT = "slot-1";
const GRAFT = "graft-1";
const CONFIG_TEXT = "cfg-1";
const FINAL_TEXT = "fin-1";
const CONFIG_VIDEO = "cfgv-1";
const LAPIAN = "lap-1";

function fixtureNodes(): CanvasNodeData[] {
    const node = (id: string, type: string, title: string, metadata: Record<string, unknown>, x = 0): CanvasNodeData => ({ id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata: metadata as CanvasNodeData["metadata"] });
    return [
        node("video-src", "video", "源视频.mp4", { content: "blob:http://localhost:3000/aaa", storageKey: "video:aaa", bytes: 1000, mimeType: "video/mp4", durationMs: 15000, status: "success" }),
        node(LAPIAN, "lapian", "拉片 · pop-3", { content: "# 旧拉片正文", taskId: "pop-3", jobId: "job-9", intent: "旧意图", lapPhase: "done", lapianModel: "c::m", lapianOptional: "A,C", status: "success" }),
        node("ins-1", "text", "转换指令", { content: "指令正文逐字", prompt: "指令正文逐字", promptTemplate: "seedance-flow", fontSize: 14, status: "success" }),
        node("it-1", "text", "改造意图（可留空）", { content: "旧意图正文", prompt: TEMPLATE_INTENT_PLACEHOLDER, lapianIntent: true, status: "success" }),
        node(CONFIG_TEXT, "config", "提示词生成配置", {
            generationMode: "text",
            model: "default::deepseek-v4-flash-0731",
            count: 1,
            lapianSourceId: LAPIAN,
            composerContent: "拉片正文：@[node:lap-1]\n任务说明：@[node:ins-1]\n用户意图：@[node:it-1]\n\n本次参考图：@[node:img-1]=人物、@[node:img-2]=服装B",
            status: "success",
        }),
        node(FINAL_TEXT, "text", "终稿", { fontSize: 14, model: "default::deepseek", textCount: 1 }),
        node(CONFIG_VIDEO, "config", "视频生成配置", {
            generationMode: "video",
            model: "k::sd2.5-720p-ap",
            size: "720x1280",
            seconds: "15",
            videoMode: "reference",
            composerContent: "参考图：@[node:img-1]、@[node:img-2]\n提示词：@[node:fin-1]",
            status: "success",
        }),
        node(IMG1, "image", "人物.png", { content: "blob:http://localhost:3000/bbb", storageKey: "image:bbb", bytes: 2024, mimeType: "image/png", naturalWidth: 941, naturalHeight: 1672, status: "success" }),
        node(IMG2, "image", "服装B.png", { content: "blob:http://localhost:3000/ccc", storageKey: "image:ccc", bytes: 3033, mimeType: "image/png", naturalWidth: 800, naturalHeight: 800, status: "success" }),
        node(SLOT, "image", "资产位·人物", {
            content: "/api/asset-frame/pop-3?name=x",
            assetSlotSourceId: LAPIAN,
            assetSlotOrder: 0,
            assetSlotName: "人物",
            attachedImageId: IMG1,
            attachedImageDesc: "人物",
            assetSlot: { desc: "原片对应物", span: "全片", frameUrl: "/api/asset-frame/pop-3?name=x", frameAt: 14.5, attachedImageId: IMG1, caption: "人物" },
            status: "success",
        }),
        node(GRAFT, "image", "挂图·人物", { content: "blob:http://localhost:3000/bbb", storageKey: "image:bbb", bytes: 2024, mimeType: "image/png", assetGraftForSlotId: SLOT, assetGraftOfImageId: IMG1, status: "success" }),
        node("out-video", "video", "生成结果", { content: "blob:http://localhost:3000/ddd", storageKey: "video:ddd", status: "success" }),
    ];
}

function fixtureConnections(): CanvasConnection[] {
    return [
        { id: "e1", fromNodeId: "video-src", toNodeId: LAPIAN },
        { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
        { id: "e3", fromNodeId: "ins-1", toNodeId: CONFIG_TEXT },
        { id: "e4", fromNodeId: "it-1", toNodeId: CONFIG_TEXT },
        { id: "e5", fromNodeId: IMG1, toNodeId: CONFIG_TEXT },
        { id: "e6", fromNodeId: IMG2, toNodeId: CONFIG_TEXT },
        { id: "e7", fromNodeId: CONFIG_TEXT, toNodeId: FINAL_TEXT },
        { id: "e8", fromNodeId: FINAL_TEXT, toNodeId: CONFIG_VIDEO },
        { id: "e9", fromNodeId: IMG1, toNodeId: CONFIG_VIDEO },
        { id: "e10", fromNodeId: IMG2, toNodeId: CONFIG_VIDEO },
        { id: "e11", fromNodeId: CONFIG_VIDEO, toNodeId: "out-video" },
        { id: "e12", fromNodeId: LAPIAN, toNodeId: SLOT },
        { id: "e13", fromNodeId: SLOT, toNodeId: GRAFT },
    ];
}

/** 与真实 pop-3 链同构的模板（含资产位，槽③可挂图）。 */
const SELECTED = ["video-src", LAPIAN, "ins-1", "it-1", CONFIG_TEXT, FINAL_TEXT, CONFIG_VIDEO, IMG1, IMG2, SLOT, GRAFT];

function workflowTemplate() {
    return extractWorkflowTemplate({
        sourceProject: { id: "proj-p4", title: "P4 夹具项目", nodes: fixtureNodes(), connections: fixtureConnections(), backgroundMode: "lines" as const, viewport: { x: 0, y: 0, k: 1 } },
        selectedNodeIds: SELECTED,
        name: "p4fixture",
        templateId: "p4demo",
    });
}

const templateSource = workflowTemplate();

function materials(tag: string): WorkflowInstanceMaterials {
    return {
        sourceVideo: { storageKey: `video:${tag}`, title: `${tag}.mp4`, content: `blob:http://localhost:3000/${tag}`, mimeType: "video/mp4", bytes: 100 + tag.length, durationMs: 9000 },
        intentText: `${tag} 的改造意图`,
        optionalSegments: "A,C",
        assetAttach: [{ slotNodeId: SLOT, imageNodeId: IMG2, caption: "服装B" }],
        generationParams: [{ nodeId: CONFIG_TEXT, values: { model: `default::${tag}-model` } }],
    };
}

function planInput(index: number, mats: WorkflowInstanceMaterials): PlanWorkflowInstanceInput {
    return { template: templateSource.template, templateNodes: templateSource.project.nodes, templateConnections: templateSource.project.connections, materials: mats, instanceIndex: index, unnamedCaption: "未命名参考图" };
}

function makeStore() {
    let snapshot: CanvasAgentSnapshot = { projectId: "p", title: "t", nodes: [], connections: [], selectedNodeIds: [], viewport: { x: 0, y: 0, k: 1 } };
    return {
        apply: async (ops: CanvasAgentOp[]) => {
            const next = applyCanvasAgentOps(snapshot, ops);
            snapshot = { ...snapshot, nodes: next.nodes, connections: next.connections, selectedNodeIds: next.selectedNodeIds, viewport: next.viewport };
        },
        nodes: () => snapshot.nodes,
        connections: () => snapshot.connections,
        seed: (nodes: CanvasNodeData[], connections: CanvasConnection[]) => {
            snapshot = { ...snapshot, nodes, connections };
        },
    };
}

/** 实例落库（等价 P3 materialize 的 applyOps 之后状态：节点齐、拉片正文=本次）。 */
function materializeTo(store: ReturnType<typeof makeStore>, plan: ReturnType<typeof planWorkflowInstance>, text = "本次拉片正文：镜头 1 推近；镜头 2 拉远。") {
    const lapianNode = plan.nodes.find((node) => node.id === plan.lapianNodeId)!;
    const nodes = plan.nodes.map((node) => (node.id === plan.lapianNodeId ? { ...node, metadata: { ...node.metadata, taskId: "task-p4", jobId: "job-p4", content: text } } : node));
    store.seed(nodes, plan.connections);
    assert.equal(lapianNode.id, plan.lapianNodeId);
}

/** 生成链替身：与 handleGenerateNode 同一「建子节点 + 连边」口径（真实正文由 deps 注入）。 */
function makeGeneration(store: ReturnType<typeof makeStore>, opts: { token: string; reportChild?: boolean; productText?: string }) {
    const calls: { nodeId: string; mode: string; prompt: string }[] = [];
    return {
        calls,
        run: async (input: { nodeId: string; mode: "text" | "image" | "video" | "audio"; prompt: string }) => {
            calls.push({ nodeId: input.nodeId, mode: input.mode, prompt: input.prompt });
            const context = buildNodeGenerationContext(input.nodeId, store.nodes(), store.connections(), input.prompt);
            const childNodeId = `product-${opts.token}`;
            const content = opts.productText ?? `${context.prompt}\n\n——本次产物 ${opts.token}`;
            await store.apply([
                { type: "add_node", id: childNodeId, nodeType: "text", title: "终稿（本次）", position: { x: 2000, y: 0 }, metadata: { content, prompt: content, status: "success" } },
                { type: "connect_nodes", fromNodeId: input.nodeId, toNodeId: childNodeId },
            ]);
            return opts.reportChild === false ? {} : { childNodeId };
        },
    };
}

function hubStub(options: { state?: string; selfcheck?: unknown[] } = {}) {
    const calls: string[] = [];
    const client: WorkflowInstanceHubClient = {
        async ingest() {
            calls.push("ingest");
            return { localId: "auto-9001", bridgeId: "bridge-42", result: "OK" };
        },
        async uploadAsset(input) {
            calls.push(`asset:${input.filename}`);
            return { ref: `local:${input.filename}`, source: "local" };
        },
        async getTask() {
            return { state: options.state || "awaiting_approval" };
        },
        async saveRevision(input) {
            calls.push(`prompt:${input.text.length}`);
            return { state: "awaiting_approval", approvalSnapshot: "sha-p4", selfcheck: options.selfcheck ?? [{ 序号: 1, 结论: "PASS" }, { 序号: 2, 结论: "FAIL" }] };
        },
        approve: async () => ({ state: "approved" }),
        rollback: async () => ({ state: "awaiting_assets" }),
        revoke: async () => ({ state: "awaiting_approval" }),
    };
    return { client, calls };
}

describe("A 组装计划（纯函数）", () => {
    it("定位组装 Config、算本次正文、认出模板占位产物与下游", () => {
        const store = makeStore();
        const plan = planWorkflowInstance(planInput(1, materials("A")));
        materializeTo(store, plan);
        const assembly = planInstanceAssembly({ plan, nodes: store.nodes(), connections: store.connections() });
        assert.equal(assembly.assemblyConfigId, plan.nodeIdMap[CONFIG_TEXT]);
        assert.ok(assembly.assemblyPrompt.includes("本次拉片正文"));
        assert.ok(assembly.assemblyPrompt.includes("A 的改造意图"));
        assert.ok(assembly.assemblyPrompt.includes("服装B.png") || assembly.assemblyPrompt.includes("参考图"), "参考图标签应在正文里");
        assert.deepEqual(assembly.staleOutputNodeIds, [plan.nodeIdMap[FINAL_TEXT]]);
        assert.deepEqual(assembly.downstream.map((item) => item.nodeId), [plan.nodeIdMap[CONFIG_VIDEO]]);
        assert.deepEqual(assembly.downstream[0].via.sort(), ["connection", "token"]);
    });

    it("组装 Config 缺失 → 明确抛错（不猜）", () => {
        const store = makeStore();
        const plan = planWorkflowInstance(planInput(1, materials("A")));
        materializeTo(store, plan);
        const nodes = store.nodes().map((node) => (node.id === plan.nodeIdMap[CONFIG_TEXT] ? { ...node, metadata: { ...node.metadata, generationMode: "image" as const } } : node));
        assert.throws(() => planInstanceAssembly({ plan, nodes, connections: store.connections() }), /没有文本组装 Config/);
    });
});

describe("B G18 产物绑定", () => {
    it("下游 token 与连线都换成本次产物，占位产物删除", async () => {
        const store = makeStore();
        const plan = planWorkflowInstance(planInput(1, materials("A")));
        materializeTo(store, plan);
        const assembly = planInstanceAssembly({ plan, nodes: store.nodes(), connections: store.connections() });
        const productNodeId = "product-x";
        const ops = buildProductBindingOps({ plan, assembly, productNodeId, nodes: store.nodes(), connections: store.connections() });
        const kinds = ops.map((op) => op.type).sort();
        assert.deepEqual(kinds, ["connect_nodes", "delete_node", "update_node"]);
        const update = ops.find((op) => op.type === "update_node") as Extract<CanvasAgentOp, { type: "update_node" }>;
        assert.equal(update.id, plan.nodeIdMap[CONFIG_VIDEO]);
        assert.ok(String(update.metadata?.composerContent).includes(`@[node:${productNodeId}]`));
        assert.ok(!String(update.metadata?.composerContent).includes(`@[node:${plan.nodeIdMap[FINAL_TEXT]}]`));
        await store.apply(ops);
        assert.equal(store.nodes().some((node) => node.id === plan.nodeIdMap[FINAL_TEXT]), false, "占位产物必须删除");
    });
});

describe("C 五槽代入校验", () => {
    it("五槽全过：槽值真落进节点/正文", () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        const assembly = planInstanceAssembly({ plan, nodes: store.nodes(), connections: store.connections() });
        const lapianText = String(store.nodes().find((node) => node.id === plan.lapianNodeId)?.metadata?.content || "");
        const checks = verifySlotSubstitution({ plan, materials: mats, nodes: store.nodes(), connections: store.connections(), assembly, lapianText });
        assert.deepEqual(checks.filter((check) => !check.ok).map((check) => `${check.slotKind}:${check.detail}`), []);
        assert.equal(checks.length, 5);
    });

    it("意图不一致 / 挂图未生效 / 可选段只改 metadata → 逐槽 FAIL（不静默）", () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        const assembly = planInstanceAssembly({ plan, nodes: store.nodes(), connections: store.connections() });
        const nodesBroken = store.nodes().map((node) => {
            if (node.id === plan.nodeIdMap["it-1"]) return { ...node, metadata: { ...node.metadata, content: "被别人改过的意图" } };
            if (node.id === plan.nodeIdMap[SLOT]) return { ...node, metadata: { ...node.metadata, attachedImageId: IMG1, assetSlot: { ...node.metadata!.assetSlot!, attachedImageId: IMG1 } } };
            return node;
        });
        const checks = verifySlotSubstitution({ plan, materials: mats, nodes: nodesBroken, connections: store.connections(), assembly, lapianText: "这段正文不含本次拉片内容" });
        const failed = checks.filter((check) => !check.ok).map((check) => check.slotKind);
        assert.ok(failed.includes("intentText"), `期望意图槽 FAIL，实际 ${JSON.stringify(failed)}`);
        assert.ok(failed.includes("assetAttach"), `期望挂图槽 FAIL，实际 ${JSON.stringify(failed)}`);
        assert.ok(failed.includes("optionalSegments"), `期望可选段槽 FAIL，实际 ${JSON.stringify(failed)}`);
    });
});

describe("D 编排器", () => {
    it("单实例全链：文本链只调一次、产物绑定下游、零付费视频、身份记录齐", async () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        const generation = makeGeneration(store, { token: "tokA" });
        const hub = hubStub();
        const saved: WorkflowInstanceIdentityRecord[] = [];
        const result = await runWorkflowInstance(
            { plan, materials: mats, lapian: { taskId: "task-p4", jobId: "job-p4" } },
            {
                nodes: store.nodes,
                connections: store.connections,
                applyOps: store.apply,
                runGeneration: generation.run,
                hub: hub.client,
                getBlob: async () => new Blob(["bytes"], { type: "video/mp4" }),
                saveIdentity: (record) => void saved.push(record),
                newRunToken: () => "tokA",
                now: () => "2026-11-13T00:00:00.000Z",
            },
        );
        assert.equal(result.failure, undefined, JSON.stringify(result.failure));
        assert.equal(result.status, "awaiting_approval");
        assert.deepEqual(generation.calls.map((call) => call.mode), ["text"]);
        assert.equal(result.paidVideoCreations, 0);
        assert.equal(result.assembly?.productNodeId, "product-tokA");
        assert.equal(result.assembly?.downstreamNodeIds.length, 1);
        assert.ok(String(result.assembly?.downstreamPrompt).includes("本次产物 tokA"), "下游必须消费本次产物");
        assert.equal(hub.calls[0], "ingest");
        assert.ok(hub.calls[1].startsWith("asset:"), `资产应按槽序上传，实际 ${hub.calls.join(",")}`);
        assert.ok(hub.calls[2].startsWith("prompt:"), `正文保存必须走 hub 保存语义，实际 ${hub.calls.join(",")}`);
        assert.equal(result.identity?.hub?.localId, "auto-9001");
        assert.equal(result.identity?.hub?.revision.digest.length, 8);
        assert.equal(result.identity?.canvas.outputNodeId, "product-tokA");
        assert.deepEqual(instanceIdentityGaps(result.identity), []);
        assert.equal(saved.length, 1);
        assert.deepEqual(upsertInstanceIdentity(saved, saved[0]), saved);
        assert.equal(result.hub?.selfcheckFailures, 1);
    });

    it("生成链不回报子节点 id → failed(product_binding)，绝不按最新节点猜", async () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        const generation = makeGeneration(store, { token: "tokB", reportChild: false });
        const result = await runWorkflowInstance(
            { plan, materials: mats, lapian: { taskId: "task-p4", jobId: "job-p4" } },
            { nodes: store.nodes, connections: store.connections, applyOps: store.apply, runGeneration: generation.run, newRunToken: () => "tokB" },
        );
        assert.equal(result.status, "failed");
        assert.equal(result.failure?.stage, "product_binding");
        assert.match(String(result.failure?.reason), /G18/);
    });

    it("五槽校验不过 → 一次生成都不发（零花费）", async () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        // 把意图节点改坏：槽② 不再等于本次意图
        store.seed(store.nodes().map((node) => (node.id === plan.nodeIdMap["it-1"] ? { ...node, metadata: { ...node.metadata, content: "被改过" } } : node)), store.connections());
        const generation = makeGeneration(store, { token: "tokC" });
        const result = await runWorkflowInstance(
            { plan, materials: mats, lapian: { taskId: "task-p4", jobId: "job-p4" } },
            { nodes: store.nodes, connections: store.connections, applyOps: store.apply, runGeneration: generation.run },
        );
        assert.equal(result.status, "failed");
        assert.equal(result.failure?.stage, "slot_verify");
        assert.equal(generation.calls.length, 0);
    });

    it("hub 未到待审批且没有等待器 → failed(hub_bind)，不硬写正文", async () => {
        const store = makeStore();
        const mats = materials("A");
        const plan = planWorkflowInstance(planInput(1, mats));
        materializeTo(store, plan);
        const generation = makeGeneration(store, { token: "tokD" });
        const hub = hubStub({ state: "ingested" });
        const result = await runWorkflowInstance(
            { plan, materials: mats, lapian: { taskId: "task-p4", jobId: "job-p4" } },
            { nodes: store.nodes, connections: store.connections, applyOps: store.apply, runGeneration: generation.run, hub: hub.client, getBlob: async () => new Blob(["x"], { type: "video/mp4" }), newRunToken: () => "tokD" },
        );
        assert.equal(result.status, "failed");
        assert.equal(result.failure?.stage, "hub_bind");
        assert.match(String(result.failure?.reason), /未到待审批/);
        assert.equal(hub.calls.includes("prompt:0"), false);
    });
});

describe("E 修订变更挂钩", () => {
    const record = { hub: { revision: { digest: "abcdef12" } } } as unknown as WorkflowInstanceIdentityRecord;
    it("同修订 → none；待审批改文 → rewrite；已批准改文 → revoke_required", () => {
        assert.equal(planRevisionChange({ record, nextTextDigest: "abcdef12", hubState: "awaiting_approval" }).action, "none");
        assert.equal(planRevisionChange({ record, nextTextDigest: "ffffffff", hubState: "awaiting_approval" }).action, "rewrite");
        assert.equal(planRevisionChange({ record, nextTextDigest: "ffffffff", hubState: "approved" }).action, "revoke_required");
        assert.equal(planRevisionChange({ record: null, nextTextDigest: "ffffffff", hubState: "approved" }).action, "none");
    });
});
