/**
 * P6 批次队列单测（node:test；经 `scripts/probe-p6-batch-queue.mjs` 打包后真跑）。
 *
 * 覆盖：
 *  A. 常量口径（并发 3 / 退避序列 / 重排与尝试上限）：数值写死，改口径必须显式改这里
 *  B. 队列记录：D1 顺序、条目只记排队簿记（C5：不写状态字段）、挂到批次记录上
 *  C. 渠道分类：429/409 ≠ 未知提交；4xx 明确拒绝不重排；5xx/408/504 一律按未知（禁重发）
 *  D. 并发上限：5 实例峰值 = 3、曲线不越限、全部到待批
 *  E. 拥塞重排：429 退避后成、退避耗尽给可读文案、与未知提交不混淆
 *  F. 失败隔离：中间实例失败不影响其余
 *  G. 未知提交对账：命中既有任务 → resolved 停住（零重发）；查不到 → explicit_unknown 停住交人
 *  H. 守卫式提交：先落盘再发；守卫落盘失败 → 拒绝发；回执丢失 → 守卫留盘无 receipt
 *  I. 恢复计划：已完成跳过、无回执守卫先对账、explicit-unknown 停住、节点在盘 → 不重复准备
 *  J. 对账函数：按节点查询命中 / 两轮未命中 / 轮次间隔
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { CanvasNodeData } from "@/types/canvas";
import { batchMemberRequiredNodeIds, createWorkflowBatch, findBatchMember, upsertBatchMember, type WorkflowBatchMember } from "@/lib/canvas/workflow-batch";
import {
    BATCH_BACKOFF_BASE_MS,
    BATCH_BACKOFF_MAX_MS,
    BATCH_CONCURRENCY_DEFAULT,
    BATCH_CONCURRENCY_MAX,
    BATCH_CONGESTION_MAX_RETRIES,
    BATCH_INSTANCE_MAX_ATTEMPTS,
    BATCH_SUBMIT_RECONCILE_ROUNDS,
    batchBackoffDelayMs,
    batchQueueEntry,
    beginBatchSubmit,
    confirmBatchSubmit,
    createBatchQueue,
    guardedBatchSubmit,
    planBatchResume,
    reconcileBatchSubmit,
    runBatchQueue,
    setBatchQueue,
    type BatchQueueJob,
    type BatchSubmitGuard,
    type WorkflowBatchQueueRecord,
} from "@/lib/canvas/workflow-batch-queue";
import { classifyChannelFailure, isCongestion } from "@/lib/canvas/workflow-channel-failure";
import type { InstanceRunResult } from "@/lib/canvas/workflow-instance-run";

const AT = "2026-11-13T00:00:00.000Z";

function makeMember(index: number, overrides: Partial<WorkflowBatchMember> = {}): WorkflowBatchMember {
    const id = (suffix: string) => `tpl#${index}:${suffix}`;
    return {
        instanceId: `tpl#${index}`,
        templateId: "tpl",
        instanceIndex: index,
        materials: { sourceVideoStorageKey: `video:${index}`, intentChars: 3, assetCount: 0, digest: `digest-${index}` },
        nodes: {
            all: [id("video"), id("lapian"), id("assembly"), id("video-config")],
            sourceVideo: id("video"),
            lapian: id("lapian"),
            assemblyConfig: id("assembly"),
            videoConfig: id("video-config"),
            retiredPlaceholders: [],
            downstream: [],
        },
        hub: null,
        backend: { lapianTaskId: `lapian-task-${index}`, lapianJobId: `lapian-job-${index}` },
        createdAt: AT,
        ...overrides,
    };
}

function makeNodes(member: WorkflowBatchMember): CanvasNodeData[] {
    return batchMemberRequiredNodeIds(member).map((id) => ({ id, type: "text", title: id, position: { x: 0, y: 0 }, width: 10, height: 10, metadata: {} }) as CanvasNodeData);
}

function okResult(instanceId: string, status: "awaiting_approval" | "canvas_ready" = "awaiting_approval"): InstanceRunResult {
    const [, index] = instanceId.split("#");
    return {
        status,
        instanceId,
        templateId: "tpl",
        instanceIndex: Number(index),
        slotChecks: [],
        flushes: [],
        paidVideoCreations: 0,
        generationCalls: [],
        appliedOps: [],
        warnings: [],
    } as InstanceRunResult;
}

function failedResult(instanceId: string, reason: string, stage: string = "generation"): InstanceRunResult {
    const result = okResult(instanceId);
    result.status = "failed";
    result.failure = { stage: stage as never, reason };
    return result;
}

const noSleep = async () => {};

describe("A. 常量口径", () => {
    it("并发默认 3、硬上限 3", () => {
        assert.equal(BATCH_CONCURRENCY_DEFAULT, 3);
        assert.equal(BATCH_CONCURRENCY_MAX, 3);
    });

    it("退避序列 4s→8s→16s→32s，封顶 60s", () => {
        assert.deepEqual([1, 2, 3, 4, 5, 6].map((n) => batchBackoffDelayMs(n)), [4_000, 8_000, 16_000, 32_000, 60_000, 60_000]);
        assert.equal(batchBackoffDelayMs(1), BATCH_BACKOFF_BASE_MS);
        assert.ok(batchBackoffDelayMs(9) <= BATCH_BACKOFF_MAX_MS);
    });

    it("重排上限 4 次 / 实例尝试上限 6 次 / 对账 2 轮", () => {
        assert.equal(BATCH_CONGESTION_MAX_RETRIES, 4);
        assert.equal(BATCH_INSTANCE_MAX_ATTEMPTS, 6);
        assert.equal(BATCH_SUBMIT_RECONCILE_ROUNDS, 2);
    });
});

describe("B. 队列记录（只记排队簿记）", () => {
    it("createBatchQueue 按 instanceIndex 升序，条目无状态字段", () => {
        const queue = createBatchQueue({ members: [makeMember(3), makeMember(1), makeMember(2)], updatedAt: AT });
        assert.deepEqual(queue.entries.map((entry) => entry.instanceIndex), [1, 2, 3]);
        assert.equal(queue.concurrency, 3);
        assert.equal(queue.phase, "idle");
        const allowed = new Set(["instanceId", "instanceIndex", "attempts", "congestionRetries", "nextEligibleAt", "lastCongestion", "submitGuard", "lastRun"]);
        for (const entry of queue.entries) for (const key of Object.keys(entry)) assert.ok(allowed.has(key), `队列条目出现未登记字段：${key}`);
        // C5：队列条目里没有「现在怎么样」的字段
        for (const entry of queue.entries) assert.equal(Object.prototype.hasOwnProperty.call(entry, "status"), false);
    });

    it("并发上限被夹到 3（传 8 也不会超）", () => {
        const queue = createBatchQueue({ members: [makeMember(1)], concurrency: 8, updatedAt: AT });
        assert.equal(queue.concurrency, 3);
    });

    it("setBatchQueue 挂到批次记录上，upsertBatchMember 不丢队列", () => {
        const member = makeMember(1);
        const records = upsertBatchMember([], { templateId: "tpl", batchId: "batch-a" }, member, AT);
        const queue = createBatchQueue({ members: [member], updatedAt: AT });
        const withQueue = setBatchQueue(records, "batch-a", queue);
        assert.equal(withQueue[0].queue?.entries.length, 1);
        const second = makeMember(2);
        const updated = upsertBatchMember(withQueue, { templateId: "tpl", batchId: "batch-a" }, second, AT);
        assert.equal(updated[0].members.length, 2);
        assert.equal(updated[0].queue?.entries.length, 1, "成员入册不该把队列簿记冲掉");
        const cleared = setBatchQueue(updated, "batch-a", undefined);
        assert.equal(cleared[0].queue, undefined);
    });
});

describe("C. 渠道分类（429/409 ≠ 未知提交）", () => {
    it("429 → rate_limited；409 → slot_busy；都可自动重排", () => {
        const limited = classifyChannelFailure({ status: 429, message: "too many requests" });
        const busy = classifyChannelFailure({ status: 409, message: "已经有一个万相生成在跑（CLI 同时只认一条），稍后再试" });
        assert.equal(limited?.kind, "rate_limited");
        assert.equal(busy?.kind, "slot_busy");
        assert.ok(isCongestion(limited?.kind));
        assert.ok(isCongestion(busy?.kind));
        assert.equal(limited?.evidence.includes("429"), true);
    });

    it("408/504/5xx → submit_unknown（不可重排）；400 → null（明确拒绝）", () => {
        for (const status of [408, 504, 500, 503]) assert.equal(classifyChannelFailure({ status, message: "x" })?.kind, "submit_unknown", `status=${status}`);
        assert.equal(classifyChannelFailure({ status: 400, message: "images 必须是列表" }), null);
        assert.equal(isCongestion("submit_unknown"), false);
    });

    it("文案兜底：状态不明/勿重复提交 → submit_unknown；单槽文案 → slot_busy", () => {
        assert.equal(classifyChannelFailure(new Error("SD GF 未返回任务 ID（提交状态不明——到站内控制台核对，勿重复提交）"))?.kind, "submit_unknown");
        assert.equal(classifyChannelFailure(new Error("ECONNRESET: socket hang up"))?.kind, "submit_unknown");
        assert.equal(classifyChannelFailure(new Error("已经有一个万相生成在跑（单槽），稍后再试"))?.kind, "slot_busy");
        assert.equal(classifyChannelFailure(new Error("请求过于频繁，请稍后"))?.kind, "rate_limited", "「请求过于频繁」= 限流");
        assert.equal(classifyChannelFailure(new Error("quota exceeded"))?.kind, "rate_limited");
    });

    it("调用方显式分类优先于状态码", () => {
        const explicit = classifyChannelFailure({ status: 400, channel: { kind: "submit_unknown" } });
        assert.equal(explicit?.kind, "submit_unknown");
        assert.match(explicit!.evidence, /结构化/);
    });
});

describe("D. 并发上限（实例级）", () => {
    it("5 实例并发批跑：峰值 = 3、曲线不越限、全部到待批", async () => {
        const members = [1, 2, 3, 4, 5].map((index) => makeMember(index));
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-d", members });
        const jobs: BatchQueueJob[] = members.map((member) => ({
            instanceId: member.instanceId,
            instanceIndex: member.instanceIndex,
            run: async () => {
                await new Promise((resolve) => setTimeout(resolve, 15));
                return okResult(member.instanceId);
            },
        }));
        const result = await runBatchQueue({ batch, jobs, now: () => Date.now() });
        assert.equal(result.concurrencyPeak, 3);
        assert.ok(result.curve.every((sample) => sample.active <= 3), JSON.stringify(result.curve));
        assert.ok(result.curve.some((sample) => sample.active === 3), "应能看到 3 条同时在跑");
        assert.equal(result.aggregate.awaitingApproval, 5);
        assert.equal(result.paidVideoCreations, 0);
        assert.ok(result.perInstance.every((item) => item.outcome === "awaiting_approval"));
        assert.equal(result.queue.phase, "finished");
    });

    it("暂停在安全边界：不再起新实例，但在途实例跑完才返回（不留孤儿）", async () => {
        const members = [1, 2, 3, 4].map((index) => makeMember(index));
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-d-pause", members });
        let inFlight = 0;
        let settledWhileReturning = 0;
        let pauseRequested = false;
        const result = await runBatchQueue({
            batch,
            jobs: members.map((member) => ({
                instanceId: member.instanceId,
                instanceIndex: member.instanceIndex,
                run: async () => {
                    inFlight += 1;
                    await new Promise((resolve) => setTimeout(resolve, member.instanceIndex === 1 ? 5 : 25));
                    inFlight -= 1;
                    if (pauseRequested && inFlight > 0) settledWhileReturning += 1;
                    if (member.instanceIndex === 1) pauseRequested = true;
                    return okResult(member.instanceId);
                },
            })),
            shouldPause: () => pauseRequested,
        });
        assert.equal(result.queue.phase, "paused");
        assert.equal(inFlight, 0, "返回时不能还有在途实例（否则成了孤儿）");
        assert.equal(settledWhileReturning, 1, "在途实例应在返回前收口（第 2 个实例先于返回完成）");
        assert.ok(result.perInstance.filter((item) => item.outcome === "not_started").length >= 1, "暂停后应有未起跑实例等待续跑");
        assert.equal(result.perInstance.filter((item) => item.outcome === "awaiting_approval").length, 3, "并发 3 起步：暂停时在途的 3 个都应已收口");
        assert.equal(result.perInstance.filter((item) => item.outcome === "not_started").length, 1, "第 4 个实例应留在队列里等续跑");
    });

    it("同实例任何时刻只有一个在跑（不会出现 2 条并发同实例）", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-d2", members: [member] });
        let inFlight = 0;
        let maxInFlight = 0;
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        inFlight += 1;
                        maxInFlight = Math.max(maxInFlight, inFlight);
                        await new Promise((resolve) => setTimeout(resolve, 10));
                        inFlight -= 1;
                        return okResult(member.instanceId);
                    },
                },
            ],
        });
        assert.equal(maxInFlight, 1);
        assert.equal(result.perInstance.length, 1);
    });
});

describe("E. 渠道拥塞（429/409 退避重排）", () => {
    it("429 一次 → 退避 4s 重排 → 最终到待批，不算失败", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-e", members: [member] });
        const sleeps: number[] = [];
        let attempt = 0;
        let clock = Date.parse(AT);
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        attempt += 1;
                        if (attempt === 1) return failedResult(member.instanceId, "429 Too Many Requests：限流，请稍后再试");
                        return okResult(member.instanceId);
                    },
                },
            ],
            // 虚拟时钟：sleep 推进时间（真实实现是 setTimeout；探针用这个避免真等 60s）
            sleep: async (ms) => {
                sleeps.push(ms);
                clock += ms;
            },
            now: () => clock,
        });
        assert.deepEqual(sleeps, [4_000], `退避应等 4s，实际 ${JSON.stringify(sleeps)}`);
        assert.equal(attempt, 2, "重排后应再跑一次");
        assert.equal(result.aggregate.awaitingApproval, 1);
        assert.equal(result.aggregate.failed.length, 0);
        assert.equal(result.aggregate.congestionRequeues, 1);
        const entry = result.queue.entries.find((item) => item.instanceId === member.instanceId)!;
        assert.equal(entry.congestionRetries, 1);
        assert.equal(entry.lastCongestion?.kind, "rate_limited");
        assert.equal(entry.lastCongestion?.delayMs, 4_000);
    });

    it("一直 409 → 退避 4 次后给可读上限文案（不无限重试、不误判未知）", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-e2", members: [member] });
        const sleeps: number[] = [];
        let attempt = 0;
        let clock = Date.parse(AT);
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        attempt += 1;
                        return failedResult(member.instanceId, "409 已经有一个万相生成在跑（单槽），稍后再试");
                    },
                },
            ],
            sleep: async (ms) => {
                sleeps.push(ms);
                clock += ms;
            },
            now: () => clock,
        });
        assert.deepEqual(sleeps, [4_000, 8_000, 16_000, 32_000]);
        assert.equal(attempt, BATCH_CONGESTION_MAX_RETRIES + 1);
        assert.equal(result.aggregate.failed.length, 1);
        assert.match(result.aggregate.failed[0].reason, /退避重排 4 次仍失败/);
        assert.equal(result.aggregate.heldSubmitUnknown.length, 0, "拥塞不该被当成未知提交");
    });
});

describe("F. 失败隔离", () => {
    it("中间实例失败 → 其余照常完成，聚合带 per-instance 失败原因", async () => {
        const members = [1, 2, 3].map((index) => makeMember(index));
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-f", members });
        const result = await runBatchQueue({
            batch,
            jobs: members.map((member) => ({
                instanceId: member.instanceId,
                instanceIndex: member.instanceIndex,
                run: async () => (member.instanceIndex === 2 ? failedResult(member.instanceId, "本实例没有文本组装 Config（模板冻结件缺失）", "assembly_plan") : okResult(member.instanceId)),
            })),
        });
        assert.equal(result.aggregate.awaitingApproval, 2);
        assert.equal(result.aggregate.failed.length, 1);
        assert.equal(result.aggregate.failed[0].instanceId, members[1].instanceId);
        assert.match(result.aggregate.failed[0].reason, /没有文本组装 Config/);
        assert.deepEqual(
            result.perInstance.map((item) => item.outcome).sort(),
            ["awaiting_approval", "awaiting_approval", "failed"],
        );
    });
});

describe("G. 未知提交对账（禁自动重发）", () => {
    it("回执丢失 → 对账命中既有任务 → resolved 停住，实例只跑一次", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-g", members: [member] });
        let runs = 0;
        let reconcileCalls = 0;
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        runs += 1;
                        return failedResult(member.instanceId, "SD GF 未返回任务 ID（提交状态不明——到站内控制台核对，勿重复提交）");
                    },
                },
            ],
            reconcile: async ({ guard }) => {
                reconcileCalls += 1;
                // 未知提交没有守卫时，队列用成员的视频节点（真实归属节点）建守卫，而不是拿实例 id 充当节点
                assert.equal(guard.nodeId, member.nodes.videoConfig);
                return { verdict: "resolved", rounds: 1, candidates: [{ taskId: "task-existing", status: "running", nodeId: guard.nodeId }], taskId: "task-existing", reason: "对账第 1 轮命中 1 条既有后端任务：按 taskId 续查，不重建" };
            },
        });
        assert.equal(runs, 1, "未知提交绝不允许自动重跑/重发");
        assert.equal(reconcileCalls, 1);
        assert.equal(result.aggregate.heldSubmitUnknown.length, 1);
        assert.equal(result.aggregate.heldSubmitUnknown[0].verdict, "resolved");
        assert.equal(result.aggregate.heldSubmitUnknown[0].taskId, "task-existing");
        assert.equal(result.aggregate.failed.length, 0);
        const entry = result.queue.entries.find((item) => item.instanceId === member.instanceId)!;
        assert.equal(entry.submitGuard?.reconcile?.verdict, "resolved");
        assert.equal(entry.submitGuard?.receipt, undefined, "没有回执就绝不能自己造一个");
        assert.ok(result.warnings.some((line) => line.includes("未重发")));
    });

    it("回执丢失 → 对账查不到 → explicit_unknown 停住交人", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-g2", members: [member] });
        let runs = 0;
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        runs += 1;
                        return failedResult(member.instanceId, "创建请求超时（回执丢失）");
                    },
                },
            ],
            reconcile: async () => ({ verdict: "explicit_unknown", rounds: 2, candidates: [], reason: "对账 2 轮均未找到该提交对应的后端任务" }),
        });
        assert.equal(runs, 1);
        assert.equal(result.aggregate.heldSubmitUnknown[0].verdict, "explicit_unknown");
        assert.equal(result.queue.entries[0].submitGuard?.reconcile?.verdict, "explicit_unknown");
        assert.equal(result.perInstance[0].outcome, "held_submit_unknown");
    });

    it("没有对账依赖时不猜：按 explicit-unknown 停住（仍不重发）", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-g3", members: [member] });
        let runs = 0;
        const result = await runBatchQueue({
            batch,
            jobs: [
                {
                    instanceId: member.instanceId,
                    instanceIndex: 1,
                    run: async () => {
                        runs += 1;
                        return failedResult(member.instanceId, "ECONNRESET：回执丢失");
                    },
                },
            ],
        });
        assert.equal(runs, 1);
        assert.equal(result.aggregate.heldSubmitUnknown[0].verdict, "explicit_unknown");
    });
});

describe("H. 守卫式提交（先写后发）", () => {
    it("守卫先落盘再发请求；回执丢失 → 守卫留盘且无 receipt", async () => {
        const member = makeMember(1);
        let queue: WorkflowBatchQueueRecord = createBatchQueue({ members: [member], updatedAt: AT });
        const entry = batchQueueEntry(queue, member);
        const order: string[] = [];
        const outcome = await guardedBatchSubmit({
            queue,
            entry,
            spec: { nodeId: "video-config#1", provider: "wan", requestDigest: "digest-req", candidateTaskIds: ["job-hint"] },
            persist: async (next) => {
                order.push(`persist:${Boolean(next.entries[0]?.submitGuard)}`);
            },
            submit: async () => {
                order.push("submit");
                throw Object.assign(new Error("timeout of 300000ms exceeded"), { code: "ECONNABORTED" });
            },
            at: AT,
        });
        assert.deepEqual(order, ["persist:true", "submit"], "必须先落守卫再发请求");
        assert.equal(outcome.ok, false);
        assert.equal(outcome.submitCalled, true);
        assert.equal(outcome.failure?.kind, "submit_unknown");
        const guard = outcome.entry.submitGuard;
        assert.ok(guard);
        assert.equal(guard.receipt, undefined);
        assert.deepEqual(guard.candidateTaskIds, ["job-hint"]);
    });

    it("守卫落盘失败 → 拒绝发请求（宁可不跑，不留钱坑）", async () => {
        const member = makeMember(1);
        const queue = createBatchQueue({ members: [member], updatedAt: AT });
        const entry = batchQueueEntry(queue, member);
        let submitCalled = false;
        const outcome = await guardedBatchSubmit({
            queue,
            entry,
            spec: { nodeId: "video-config#1", provider: "wan", requestDigest: "d" },
            persist: async () => {
                throw new Error("写盘失败（配额）");
            },
            submit: async () => {
                submitCalled = true;
                return { taskId: "should-not-happen" };
            },
            at: AT,
        });
        assert.equal(submitCalled, false);
        assert.equal(outcome.ok, false);
        assert.match(outcome.error, /守卫未落盘|写盘失败/);
    });

    it("成功路径：回执补写 + 落盘；429 明确拒绝 → 清守卫（确认未提交）", async () => {
        const member = makeMember(1);
        const queue = createBatchQueue({ members: [member], updatedAt: AT });
        const entry = batchQueueEntry(queue, member);
        const persisted: WorkflowBatchQueueRecord[] = [];
        const ok = await guardedBatchSubmit({
            queue,
            entry,
            spec: { nodeId: "video-config#1", provider: "wan", requestDigest: "d" },
            persist: async (next) => {
                persisted.push(next);
            },
            submit: async () => ({ taskId: "task-1", jobId: "job-1" }),
            at: AT,
        });
        assert.equal(ok.ok, true);
        assert.equal(ok.entry.submitGuard?.receipt?.taskId, "task-1");
        assert.equal(ok.entry.submitGuard?.receipt?.jobId, "job-1");
        assert.equal(persisted.length, 2, "守卫一次 + 回执一次");

        const limited = await guardedBatchSubmit({
            queue: ok.queue,
            entry: ok.entry,
            spec: { nodeId: "video-config#1", provider: "wan", requestDigest: "d2" },
            persist: async () => {},
            submit: async () => {
                throw Object.assign(new Error("429 Too Many Requests"), { status: 429 });
            },
            at: AT,
        });
        assert.equal(limited.ok, false);
        assert.equal(limited.failure?.kind, "rate_limited");
        assert.equal(limited.entry.submitGuard, undefined, "明确被拒（未提交）应清掉守卫，交给退避重排");
    });
});

describe("I. 恢复计划（续跑）", () => {
    it("已完成跳过 / 无回执守卫先对账 / explicit-unknown 停住 / 节点在盘不重复准备", () => {
        const done = makeMember(1);
        const danglingMember = makeMember(2);
        const unknownMember = makeMember(3);
        const pending = makeMember(4);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-i", members: [done, danglingMember, unknownMember, pending] });
        let queue = createBatchQueue({ members: batch.members, updatedAt: AT });
        const dangling: BatchSubmitGuard = { instanceId: danglingMember.instanceId, nodeId: "video-config#2", provider: "wan", requestDigest: "d", requestedAt: AT };
        const unknown: BatchSubmitGuard = { ...dangling, instanceId: unknownMember.instanceId, reconcile: { at: AT, verdict: "explicit_unknown", rounds: 2, candidates: [], reason: "查不到" } };
        queue = {
            ...queue,
            entries: queue.entries.map((entry) =>
                entry.instanceId === danglingMember.instanceId ? { ...entry, submitGuard: dangling } : entry.instanceId === unknownMember.instanceId ? { ...entry, submitGuard: unknown } : entry,
            ),
        };
        const plan = planBatchResume({ batch, queue, statuses: { [done.instanceId]: "awaiting_approval" }, nodes: [...makeNodes(done), ...makeNodes(danglingMember), ...makeNodes(unknownMember)] });
        assert.deepEqual(plan.skippedDone.map((item) => item.instanceId), [done.instanceId]);
        assert.deepEqual(plan.danglingSubmit.map((item) => item.instanceId), [danglingMember.instanceId]);
        assert.deepEqual(plan.heldSubmitUnknown.map((item) => item.instanceId), [unknownMember.instanceId]);
        assert.deepEqual(plan.run, [pending.instanceId]);
        assert.deepEqual(plan.prepareNeeded, [pending.instanceId], "已落库实例不该再 materialize（不重建节点/不重拉片）");
    });

    it("派生状态优先于簿记：派生说已待批，队列簿记就算写着失败也不重跑", () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-i2", members: [member] });
        const queue = createBatchQueue({ members: [member], updatedAt: AT });
        const withFail = { ...queue, entries: [{ ...queue.entries[0], lastRun: { at: AT, outcome: "failed" as const, reason: "上次失败" } }] };
        const plan = planBatchResume({ batch, queue: withFail, statuses: { [member.instanceId]: "awaiting_approval" } });
        assert.equal(plan.run.length, 0);
        assert.equal(plan.skippedDone.length, 1);
    });

    it("尝试预算耗尽 → 停住交人（不无限续跑）", () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-i3", members: [member] });
        const queue = createBatchQueue({ members: [member], updatedAt: AT });
        const exhausted = { ...queue, entries: [{ ...queue.entries[0], attempts: BATCH_INSTANCE_MAX_ATTEMPTS }] };
        const plan = planBatchResume({ batch, queue: exhausted });
        assert.equal(plan.retryExhausted.length, 1);
        assert.equal(plan.run.length, 0);
    });
});

describe("J. 对账函数（真查）", () => {
    it("按节点查询命中 → resolved（候选带 taskId/status）", async () => {
        const member = makeMember(1);
        const guard: BatchSubmitGuard = { instanceId: member.instanceId, nodeId: member.nodes.videoConfig!, provider: "wan", requestDigest: "d", requestedAt: AT };
        const outcome = await reconcileBatchSubmit({
            guard,
            member,
            query: async (input) => {
                const ids = input.nodeIds || [];
                return { total: ids.length, tasks: ids.map((id) => ({ id, status: id === member.nodes.videoConfig ? "running" : "succeeded", source: "canvas" })) };
            },
            rounds: 2,
            sleep: noSleep,
        });
        assert.equal(outcome.verdict, "resolved");
        assert.equal(outcome.rounds, 1);
        assert.ok(outcome.candidates.some((item) => item.taskId === member.nodes.videoConfig && item.status === "running"));
    });

    it("两轮都查不到 → explicit_unknown，且确实等了两轮间隔", async () => {
        const member = makeMember(1);
        const guard: BatchSubmitGuard = { instanceId: member.instanceId, nodeId: "video-config#missing", provider: "wan", requestDigest: "d", requestedAt: AT };
        const sleeps: number[] = [];
        let queries = 0;
        const outcome = await reconcileBatchSubmit({
            guard,
            member,
            query: async () => {
                queries += 1;
                return { total: 0, tasks: [] };
            },
            rounds: 2,
            intervalMs: 15_000,
            sleep: async (ms) => {
                sleeps.push(ms);
            },
        });
        assert.equal(outcome.verdict, "explicit_unknown");
        assert.equal(outcome.rounds, 2);
        assert.deepEqual(sleeps, [15_000]);
        assert.ok(queries >= 2);
        assert.match(outcome.reason, /不重发|交人/);
    });

    it("Wan 成片映射已回填 → 交叉核对命中（S2）", async () => {
        const guard: BatchSubmitGuard = { instanceId: "tpl#1", nodeId: "video-config#1", provider: "wan", requestDigest: "d", requestedAt: AT, candidateTaskIds: ["job-9"] };
        const outcome = await reconcileBatchSubmit({
            guard,
            query: async () => ({ total: 0, tasks: [] }),
            rounds: 1,
            sleep: noSleep,
            lookupWanFileTask: (job) => (job === "job-9" ? "file-task-9" : ""),
        });
        assert.equal(outcome.verdict, "resolved");
        assert.equal(outcome.taskId, "file-task-9");
    });
});

describe("K. 与批次记录/派生层的接缝", () => {
    it("runBatchQueue 只写排队簿记，不往成员里塞状态", async () => {
        const member = makeMember(1);
        const batch = createWorkflowBatch({ templateId: "tpl", batchId: "batch-k", members: [member] });
        const result = await runBatchQueue({ batch, jobs: [{ instanceId: member.instanceId, instanceIndex: 1, run: async () => okResult(member.instanceId) }] });
        const after = findBatchMember({ ...batch, queue: result.queue }, member.instanceId)!;
        assert.deepEqual(Object.keys(after).sort(), Object.keys(member).sort(), "成员结构不该被队列改动");
        assert.equal("status" in after, false);
        void beginBatchSubmit; // 与 confirm 同族 API 的存在性由 H 段覆盖
    });
});
