/**
 * P5 批次身份 + 状态派生 + 查询覆盖单测（node:test；经 `scripts/probe-p5-batch-status.mjs` 打包后真跑）。
 *
 * 覆盖：
 *  A. 批次身份：四层关联（batchId↔实例↔节点↔hub）、幂等入册、导入透传、缺口清单、C5（不写节点 metadata / 不存状态字段）
 *  B. 状态派生矩阵：Config success+视频 running≠完成 / 任务 failed→失败 / 任务缺失→未知 / 待批 / 已批 / 完成 / 节点缺失→未知
 *  C. 查询覆盖：分块计划、绕过默认 20 / 上限 100 的截断（二分重查）、查不全→显式未覆盖、本地去重、按成员切证据
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { registerBuiltinNodes } from "@/components/canvas/nodes/builtin-nodes";
import type { CanvasConnection, CanvasNodeData, CanvasNodeStatus } from "@/types/canvas";
import { planWorkflowInstance, type WorkflowInstanceMaterials } from "@/lib/canvas/workflow-instance";
import type { WorkflowInstanceIdentityRecord } from "@/lib/canvas/workflow-instance-run";
import { extractWorkflowTemplate } from "@/lib/canvas/workflow-template";
import {
    acceptWorkflowBatches,
    batchMemberGaps,
    batchMemberNodeIds,
    buildBatchMember,
    createWorkflowBatch,
    deriveBatchId,
    findBatchMember,
    summarizeMaterials,
    upsertBatchMember,
    type WorkflowBatchMember,
    type WorkflowBatchRecord,
} from "@/lib/canvas/workflow-batch";
import { deriveBatchStatus, deriveInstanceStatus, INSTANCE_STATUSES, type BackendTaskStatus } from "@/lib/canvas/workflow-batch-status";
import { planStatusQueryChunks, queryBatchMemberTasks, splitEvidenceByMember, type StatusQuery } from "@/lib/canvas/workflow-batch-query";

registerBuiltinNodes();

// ─────────────────────────────────────────────── 夹具：与真实拉片链同构的最小模板

const VIDEO = "video-src";
const LAPIAN = "lap-1";
const INS = "ins-1";
const INTENT = "it-1";
const CONFIG_TEXT = "cfg-1";
const FINAL = "fin-1";
const CONFIG_VIDEO = "cfgv-1";
const OUT_VIDEO = "out-video";

function node(id: string, type: string, title: string, metadata: Record<string, unknown>, x = 0): CanvasNodeData {
    return { id, type, title, position: { x, y: 0 }, width: 300, height: 200, metadata: metadata as CanvasNodeData["metadata"] };
}

function fixtureNodes(): CanvasNodeData[] {
    return [
        node(VIDEO, "video", "源视频.mp4", { content: "blob:src", storageKey: "video:src", mimeType: "video/mp4", status: "success" }),
        node(LAPIAN, "lapian", "拉片", { content: "# 拉片正文", taskId: "lap-task", jobId: "lap-job", lapPhase: "done" }),
        node(INS, "text", "转换指令", { content: "指令正文", prompt: "指令正文", promptTemplate: "seedance-flow", status: "success" }, 100),
        node(INTENT, "text", "改造意图", { content: "旧意图", prompt: "旧意图", lapianIntent: true, status: "success" }, 200),
        node(CONFIG_TEXT, "config", "提示词生成配置", { generationMode: "text", model: "default::m", lapianSourceId: LAPIAN, composerContent: "拉片：@[node:lap-1]\n指令：@[node:ins-1]\n意图：@[node:it-1]", status: "success" }, 300),
        node(FINAL, "text", "终稿（占位）", { fontSize: 14, model: "default::m", textCount: 1 }, 400),
        node(CONFIG_VIDEO, "config", "视频生成配置", { generationMode: "video", model: "k::sd2.5", size: "720x1280", composerContent: "提示词：@[node:fin-1]", status: "success" }, 500),
        node(OUT_VIDEO, "video", "生成结果", { content: "blob:out", storageKey: "video:out", status: "idle" }, 600),
    ];
}

function fixtureConnections(): CanvasConnection[] {
    return [
        { id: "e1", fromNodeId: VIDEO, toNodeId: LAPIAN },
        { id: "e2", fromNodeId: LAPIAN, toNodeId: CONFIG_TEXT },
        { id: "e3", fromNodeId: INS, toNodeId: CONFIG_TEXT },
        { id: "e4", fromNodeId: INTENT, toNodeId: CONFIG_TEXT },
        { id: "e5", fromNodeId: CONFIG_TEXT, toNodeId: FINAL },
        { id: "e6", fromNodeId: FINAL, toNodeId: CONFIG_VIDEO },
        { id: "e7", fromNodeId: CONFIG_VIDEO, toNodeId: OUT_VIDEO },
    ];
}

const templateSource = extractWorkflowTemplate({
    sourceProject: { id: "proj-p5", title: "P5 夹具项目", nodes: fixtureNodes(), connections: fixtureConnections(), backgroundMode: "lines" as const, viewport: { x: 0, y: 0, k: 1 } },
    selectedNodeIds: [VIDEO, LAPIAN, INS, INTENT, CONFIG_TEXT, FINAL, CONFIG_VIDEO, OUT_VIDEO],
    name: "p5fixture",
    templateId: "p5demo",
});

function materials(tag: string): WorkflowInstanceMaterials {
    return {
        sourceVideo: { storageKey: `video:${tag}`, title: `${tag}.mp4`, content: `blob:${tag}`, mimeType: "video/mp4", bytes: 1000, durationMs: 9000 },
        intentText: `${tag} 的改造意图`,
        optionalSegments: "A",
    };
}

function planOf(index: number, mats = materials("A")) {
    return planWorkflowInstance({
        template: templateSource.template,
        templateNodes: templateSource.project.nodes,
        templateConnections: templateSource.project.connections,
        materials: mats,
        instanceIndex: index,
        unnamedCaption: "未命名参考图",
    });
}

/** 实例落库后的节点（等价 P3 applyOps 之后：拉片带 taskId、正文=本次）。 */
function materializedNodes(plan: ReturnType<typeof planOf>): CanvasNodeData[] {
    return plan.nodes.map((item) => (item.id === plan.lapianNodeId ? { ...item, metadata: { ...item.metadata, taskId: "lap-task-1", jobId: "lap-job-1", content: "本次拉片正文" } } : item));
}

function identityOf(plan: ReturnType<typeof planOf>, productNodeId: string): WorkflowInstanceIdentityRecord {
    return {
        schemaVersion: 1,
        instanceId: plan.instanceId,
        templateId: plan.templateId,
        instanceIndex: plan.instanceIndex,
        createdAt: "2026-11-13T00:00:00.000Z",
        canvas: {
            nodeIds: plan.nodes.map((item) => item.id),
            outputNodeId: productNodeId,
            retiredPlaceholderNodeIds: [plan.nodeIdMap[FINAL]],
            lapianTaskId: "lap-task-1",
            lapianJobId: "lap-job-1",
            videoStorageKey: "video:A",
            runToken: "run-1",
            assemblyPromptDigest: "abcd1234",
            assemblyPromptChars: 100,
        },
        hub: { localId: "auto-9001", bridgeId: "bridge-1", boundAt: "2026-11-13T00:00:00.000Z", revision: { digest: "digest01", chars: 100, approvalSnapshot: "snap-1" }, assets: [] },
    };
}

/** 造一个「已绑定」成员（画布 + hub 四层齐）；视频结果节点可注入任务号与生成态。 */
function memberOf(index = 1, opts: { videoTaskId?: string; videoStatus?: "idle" | "loading" | "success" | "error" } = {}): { member: WorkflowBatchMember; plan: ReturnType<typeof planOf>; nodes: CanvasNodeData[] } {
    const mats = materials("A");
    const plan = planOf(index, mats);
    const productId = `product-${index}`;
    const videoNodeId = plan.nodeIdMap[OUT_VIDEO];
    const nodes = materializedNodes(plan).map((item) => {
        if (item.id === plan.nodeIdMap[FINAL]) return { ...item, id: productId, title: "终稿（本次）", metadata: { ...item.metadata, content: "本次产物正文", status: "success" as const } };
        if (item.id === videoNodeId) {
            return {
                ...item,
                metadata: {
                    ...item.metadata,
                    status: (opts.videoStatus ?? "idle") as CanvasNodeStatus,
                    ...(opts.videoTaskId ? { videoTaskId: opts.videoTaskId, videoTaskProvider: "sdgf" as const } : {}),
                },
            };
        }
        return item;
    });
    const connections = [...plan.connections.filter((connection) => connection.fromNodeId !== plan.nodeIdMap[FINAL] && !(connection.fromNodeId === videoNodeId)), { id: "e-product", fromNodeId: productId, toNodeId: plan.nodeIdMap[CONFIG_VIDEO] }, { id: "e-video", fromNodeId: plan.nodeIdMap[CONFIG_VIDEO], toNodeId: videoNodeId }];
    const member = buildBatchMember({ plan, materials: mats, identity: identityOf(plan, productId), nodes, connections, lapian: { taskId: "lap-task-1", jobId: "lap-job-1" }, createdAt: "2026-11-13T00:00:00.000Z" });
    return { member, plan, nodes };
}

// ─────────────────────────────────────────────── A 批次身份

describe("A 批次身份（四层关联）", () => {
    it("成员记录四层关联齐：batchId↔实例↔节点骨架↔hub/后端任务", () => {
        const { member, plan } = memberOf(1);
        assert.equal(member.instanceId, plan.instanceId);
        assert.equal(member.instanceIndex, 1);
        assert.equal(member.nodes.sourceVideo, plan.sourceVideoNodeId);
        assert.equal(member.nodes.lapian, plan.lapianNodeId);
        assert.equal(member.nodes.assemblyConfig, plan.nodeIdMap[CONFIG_TEXT]);
        assert.equal(member.nodes.product, "product-1");
        assert.equal(member.nodes.videoConfig, plan.nodeIdMap[CONFIG_VIDEO]);
        assert.equal(member.nodes.videoTarget, plan.nodeIdMap[OUT_VIDEO]);
        assert.equal(member.hub?.localId, "auto-9001");
        assert.equal(member.hub?.bridgeId, "bridge-1");
        assert.equal(member.backend.lapianTaskId, "lap-task-1");
        assert.deepEqual(batchMemberGaps(member), [], JSON.stringify(batchMemberGaps(member)));
    });

    it("素材组摘要可复算：同素材同摘要，改意图就变", () => {
        const base = summarizeMaterials(materials("A"));
        assert.equal(summarizeMaterials(materials("A")).digest, base.digest);
        const changed = summarizeMaterials({ ...materials("A"), intentText: "另一个意图" });
        assert.notEqual(changed.digest, base.digest);
    });

    it("同实例重复入册只留最新一条（幂等），批次签名随成员变化", () => {
        const { member } = memberOf(1);
        const batch = { templateId: member.templateId, batchId: deriveBatchId({ templateId: member.templateId, materialDigests: [member.materials.digest], createdAt: "2026-11-13T00:00:00.000Z" }), title: "批次A" };
        let records: WorkflowBatchRecord[] = [];
        records = upsertBatchMember(records, batch, member, "2026-11-13T00:00:00.000Z");
        assert.equal(records.length, 1);
        assert.equal(records[0].members.length, 1);
        const signatureBefore = records[0].signature;
        // 同实例再入册（hub 换成另一个 localId）→ 成员仍 1 条且被更新
        records = upsertBatchMember(records, batch, { ...member, hub: { ...member.hub!, localId: "auto-9002" } }, "2026-11-13T00:01:00.000Z");
        assert.equal(records[0].members.length, 1);
        assert.equal(findBatchMember(records[0], member.instanceId)?.hub?.localId, "auto-9002");
        assert.equal(records[0].signature, signatureBefore, "身份字段（素材/模板）没变时签名不变");
        assert.equal(records[0].updatedAt, "2026-11-13T00:01:00.000Z");
    });

    it("导入侧校验：留下批次记录、丢掉垃圾、未知新版本字段原样透传", () => {
        const { member } = memberOf(1);
        const record = createWorkflowBatch({ templateId: member.templateId, batchId: "batch-keep", members: [member] });
        const accepted = acceptWorkflowBatches([record, { batchId: "", members: [] }, null, { batchId: "x", templateId: "t", members: [] }]);
        assert.equal(accepted?.length, 2);
        assert.equal(accepted?.[0].batchId, "batch-keep");
        assert.equal((accepted?.[0] as unknown as { schemaVersion: number }).schemaVersion, 1);
    });

    it("C5 相容：不写节点 metadata、成员记录里没有任何状态字段", () => {
        const { member, nodes, plan } = memberOf(1);
        const before = JSON.stringify(nodes);
        // 反复入册（builder 是纯函数）：节点快照逐字节不变
        buildBatchMember({ plan, materials: materials("A"), identity: identityOf(plan, "product-1"), nodes, connections: [], lapian: { taskId: "lap-task-1", jobId: "lap-job-1" } });
        assert.equal(JSON.stringify(nodes), before, "批次入册不得改动节点 metadata");
        const json = JSON.stringify(member);
        for (const forbidden of ["\"status\"", "\"state\"", "\"approved\"", "\"awaiting_approval\"", "\"progress\""]) {
            assert.equal(json.includes(forbidden), false, `成员记录里不该有状态字段：${forbidden}`);
        }
    });

    it("缺口清单按层报：缺 hub 绑定、缺拉片任务各自点名", () => {
        const { member } = memberOf(1);
        const bare = buildBatchMember({ plan: planOf(2), materials: materials("B"), nodes: materializedNodes(planOf(2)) });
        assert.equal(bare.hub, null);
        const gaps = batchMemberGaps(bare);
        assert.ok(gaps.some((gap) => gap.includes("hub local_id")), JSON.stringify(gaps));
        assert.ok(gaps.some((gap) => gap.includes("拉片 taskId")), JSON.stringify(gaps));
        assert.equal(batchMemberGaps({ ...member, hub: { ...member.hub!, bridgeId: "" } }).some((gap) => gap.includes("bridge_id")), true);
    });

    it("可查询节点集去重保序（成员查询输入）", () => {
        const { member } = memberOf(1);
        const ids = batchMemberNodeIds(member);
        assert.equal(new Set(ids).size, ids.length, "节点 id 必须去重");
        assert.ok(ids.includes(member.nodes.product!));
        assert.ok(ids.includes(member.nodes.videoTarget!));
    });
});

// ─────────────────────────────────────────────── B 状态派生矩阵

function taskMap(entries: Record<string, BackendTaskStatus>): Map<string, BackendTaskStatus> {
    return new Map(Object.entries(entries));
}

describe("B 状态派生矩阵", () => {
    it("Config success + 视频 running → 进行中，绝不是完成", () => {
        const { member, nodes } = memberOf(1, { videoTaskId: "video-task-1", videoStatus: "loading" });
        const view = deriveInstanceStatus({ member, nodes, hubState: "generating", evidence: { byTaskId: taskMap({ "video-task-1": "running", "lap-task-1": "succeeded" }) } });
        assert.notEqual(view.status, "done");
        assert.equal(view.status, "generating");
        assert.equal(view.rule, "R6");
        assert.ok(view.reason.includes("出片") || view.reason.includes("视频"));
    });

    it("视频节点自身 loading（未给任务号）→ running，不是完成", () => {
        const { member, nodes } = memberOf(1, { videoStatus: "loading" });
        const view = deriveInstanceStatus({ member, nodes, hubState: undefined });
        assert.equal(view.status, "running");
        assert.equal(view.rule, "R7");
    });

    it("后端任务 failed → 实例失败（失败优先于任何 success 痕迹）", () => {
        const { member, nodes } = memberOf(1, { videoTaskId: "video-task-1", videoStatus: "error" });
        const failure = deriveInstanceStatus({ member, nodes, hubState: "generating", evidence: { byTaskId: taskMap({ "video-task-1": "failed", "lap-task-1": "succeeded" }) } });
        assert.equal(failure.status, "failed");
        assert.equal(failure.rule, "R1");
        assert.ok(failure.reason.includes("失败") || failure.reason.includes("failed"), failure.reason);
        // 节点自己就是 error 也算失败（后端任务号缺失时仍有结论）
        const byNode = memberOf(2, { videoStatus: "error" });
        assert.equal(deriveInstanceStatus({ member: byNode.member, nodes: byNode.nodes, hubState: undefined }).status, "failed");
    });

    it("视频后端任务缺失 → 显式未知，不判成功", () => {
        const { member, nodes } = memberOf(1, { videoTaskId: "video-task-missing" });
        const view = deriveInstanceStatus({ member, nodes, hubState: undefined, evidence: { byTaskId: taskMap({ "lap-task-1": "succeeded" }) } });
        assert.equal(view.status, "unknown");
        assert.equal(view.rule, "R3");
        assert.ok(view.missing.some((item) => item.includes("视频后端任务状态未知")), JSON.stringify(view.missing));
    });

    it("组装 Config success + 待批 → awaiting_approval（不改判完成）", () => {
        const { member, nodes } = memberOf(1);
        const view = deriveInstanceStatus({ member, nodes, hubState: "awaiting_approval", evidence: { byTaskId: taskMap({ "lap-task-1": "succeeded" }) } });
        assert.equal(view.status, "awaiting_approval");
        assert.equal(view.rule, "R4");
        assert.equal(view.evidence.configSucceeded, true);
    });

    it("已批准未生成 → approved；已批准且视频任务在跑 → generating", () => {
        const approvedCase = memberOf(1);
        assert.equal(deriveInstanceStatus({ member: approvedCase.member, nodes: approvedCase.nodes, hubState: "approved", evidence: { byTaskId: taskMap({ "lap-task-1": "succeeded" }) } }).status, "approved");
        const running = memberOf(2, { videoTaskId: "video-task-2", videoStatus: "loading" });
        const view = deriveInstanceStatus({ member: running.member, nodes: running.nodes, hubState: "approved", evidence: { byTaskId: taskMap({ "video-task-2": "queued", "lap-task-1": "succeeded" }) } });
        assert.equal(view.status, "generating");
        assert.equal(view.rule, "R6");
    });

    it("产物 + 视频任务 succeeded → done（唯一完成判据）", () => {
        const { member, nodes } = memberOf(1, { videoTaskId: "video-task-1", videoStatus: "success" });
        const done = deriveInstanceStatus({ member, nodes, hubState: "done", evidence: { byTaskId: taskMap({ "video-task-1": "succeeded", "lap-task-1": "succeeded" }) } });
        assert.equal(done.status, "done");
        assert.equal(done.rule, "R3");
        assert.deepEqual(done.missing, []);
    });

    it("成员必需节点缺失 → unknown（缺失≠成功）", () => {
        const { member, nodes } = memberOf(1);
        const partial = nodes.filter((item) => item.id !== member.nodes.lapian);
        const view = deriveInstanceStatus({ member, nodes: partial, hubState: "awaiting_approval" });
        assert.equal(view.status, "unknown");
        assert.equal(view.rule, "R2");
        assert.ok(view.missing[0].includes("节点快照缺"));
    });

    it("占位产物退役不算缺失（退役是正常终态）", () => {
        const { member, nodes } = memberOf(1);
        const view = deriveInstanceStatus({ member, nodes, hubState: "awaiting_approval", evidence: { byTaskId: taskMap({ "lap-task-1": "succeeded" }) } });
        assert.equal(view.rule, "R4");
        assert.equal((view.evidence.missingNodeIds as string[]).length, 0);
        assert.deepEqual(view.missing, [], "拉片任务状态也已给出 → 证据齐");
    });

    it("批次聚合：计数 + 覆盖率不足显式部分未知；有失败即 failed", () => {
        const a = memberOf(1);
        const b = memberOf(2);
        const batch = createWorkflowBatch({ templateId: a.member.templateId, batchId: "batch-agg", members: [a.member, b.member] });
        const evidenceOf = () => ({ byTaskId: taskMap({ "lap-task-1": "succeeded" }) });
        const pendingView = deriveBatchStatus({ batch, nodes: [...a.nodes, ...b.nodes], hubStates: { [a.member.instanceId]: "awaiting_approval", [b.member.instanceId]: "awaiting_approval" }, evidenceOf });
        assert.equal(pendingView.total, 2);
        assert.equal(pendingView.counts.awaiting_approval, 2);
        assert.equal(pendingView.outcome, "in_progress", JSON.stringify(pendingView.coverage));
        // 缺一个成员的节点 → 该成员 unknown，批次部分未知（不算成功）
        const partial = deriveBatchStatus({ batch, nodes: a.nodes, hubStates: { [a.member.instanceId]: "awaiting_approval", [b.member.instanceId]: "awaiting_approval" }, evidenceOf });
        assert.equal(partial.members.find((item) => item.instanceId === b.member.instanceId)?.status, "unknown");
        assert.equal(partial.outcome, "partial_unknown");
        assert.equal(partial.coverage.complete, false);
        assert.ok(partial.coverage.partialUnknownMembers.includes(b.member.instanceId));
        // 有失败 → failed 优先
        const failed = deriveBatchStatus({ batch, nodes: [...a.nodes, ...b.nodes], hubStates: { [a.member.instanceId]: "failed", [b.member.instanceId]: "awaiting_approval" }, evidenceOf });
        assert.equal(failed.outcome, "failed");
        assert.equal(INSTANCE_STATUSES.includes("failed"), true);
    });
});

// ─────────────────────────────────────────────── C 查询覆盖（默认 20 / 上限 100 / 无分页）

describe("C 分组查询覆盖", () => {
    it("分块计划：250 个节点 → 100/100/50，去重保序", () => {
        const ids = Array.from({ length: 250 }, (_, index) => `n-${index}`);
        const chunks = planStatusQueryChunks(ids, 100);
        assert.deepEqual(chunks.map((chunk) => chunk.length), [100, 100, 50]);
        assert.deepEqual(planStatusQueryChunks([...ids, "n-0"], 100)[0][0], "n-0");
        assert.equal(planStatusQueryChunks([...ids, "n-0"], 100).flat().length, 250);
    });

    it("后端只认默认 20 条（忽略 limit）→ 二分重查拉全，零静默截断", async () => {
        const members = Array.from({ length: 6 }, (_, index) => memberOf(index + 1).member);
        const requested = members.flatMap((member) => batchMemberNodeIds(member));
        const statuses = new Map(requested.map((id) => [id, "running"]));
        const calls: { nodeIds: string[]; limit?: number }[] = [];
        const query: StatusQuery = async (input) => {
            calls.push({ nodeIds: input.nodeIds || [], limit: input.limit });
            if (input.taskId) return { total: 0, tasks: [] };
            const ids = input.nodeIds || [];
            const rows = ids.filter((id) => statuses.has(id)).map((id) => ({ id, status: statuses.get(id)!, source: "canvas" as const }));
            // 模拟「不认 limit、只回 20 条」的后端：total 报全量，tasks 只给 20 条
            return { total: rows.length, tasks: rows.slice(0, 20) };
        };
        const result = await queryBatchMemberTasks({ members, query });
        assert.equal(result.coverage.complete, true, JSON.stringify(result.coverage));
        assert.equal(result.coverage.coveredNodeIds.length, requested.filter((id, index, list) => list.indexOf(id) === index).length);
        assert.equal(result.coverage.uncoveredNodeIds.length, 0);
        assert.ok(result.coverage.splitRounds > 0, "必须发生二分重查（否则就是吃了一次 20 条截断）");
        assert.equal(result.coverage.queries, calls.length, "查询次数要如实累计");
        assert.equal(result.warnings.some((line) => line.includes("部分未知")), false);
    });

    it("单节点也查不全 → 该节点记未覆盖，覆盖率显式不足（绝不按成功）", async () => {
        const member = memberOf(1).member;
        const target = member.nodes.lapian;
        const query: StatusQuery = async (input) => {
            if (input.taskId) return { total: 0, tasks: [] };
            const ids = input.nodeIds || [];
            // 后端只回 20 条（默认上限），且这个节点永远给不出状态（模拟后端数据缺失）
            const rows = ids.filter((id) => id !== target).map((id) => ({ id, status: "succeeded", source: "canvas" as const }));
            return { total: ids.length, tasks: rows.slice(0, 20) };
        };
        const result = await queryBatchMemberTasks({ members: [member], query });
        assert.equal(result.coverage.complete, false);
        assert.ok(result.coverage.uncoveredNodeIds.includes(target), JSON.stringify(result.coverage.uncoveredNodeIds));
        assert.ok(result.warnings.some((line) => line.includes("部分未知")));
        const evidence = splitEvidenceByMember({ members: [member], result }).get(member.instanceId)!;
        assert.equal(evidence.coverage?.complete, false);
        const view = deriveInstanceStatus({ member, nodes: memberOf(1).nodes, hubState: "awaiting_approval", evidence });
        assert.ok(view.missing.some((item) => item.includes("任务查询未全覆盖")), JSON.stringify(view.missing));
    });

    it("本地去重：同一节点被多个成员引用只查一次；按成员切证据", async () => {
        const a = memberOf(1).member;
        // 人为让两个成员共享同一节点 id
        const b: WorkflowBatchMember = { ...memberOf(2).member, nodes: { ...memberOf(2).member.nodes, lapian: a.nodes.lapian } };
        const seen: string[] = [];
        const query: StatusQuery = async (input) => {
            if (input.taskId) return { total: 0, tasks: [] };
            for (const id of input.nodeIds || []) if (seen.includes(id)) throw new Error(`重复查询同一节点：${id}`);
            seen.push(...(input.nodeIds || []));
            const rows = (input.nodeIds || []).map((id) => ({ id, status: "succeeded", source: "canvas" as const }));
            return { total: rows.length, tasks: rows };
        };
        const result = await queryBatchMemberTasks({ members: [a, b], query });
        assert.equal(result.coverage?.complete, true);
        const evidence = splitEvidenceByMember({ members: [a, b], result, extraTaskStatus: new Map([["lap-task-1", "succeeded" as BackendTaskStatus]]) });
        assert.equal(evidence.size, 2);
        assert.equal(evidence.get(a.instanceId)?.byTaskId?.get("lap-task-1"), "succeeded");
    });
});
