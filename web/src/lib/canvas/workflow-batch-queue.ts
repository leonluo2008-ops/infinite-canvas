/**
 * P6 · 批次编排队列（N 实例排队批跑 / 渠道拥塞分类 / 未知提交对账 / 断线续跑）。
 *
 * 依据：
 * - 本卡 D1（N 实例排队，**并发上限 3**，作用域 = 实例级编排并发）+ D11（未知提交一律对账、禁自动重试、
 *   单实例失败不误判他例）+ 审计 §5.2（同节点重触发会 abort 前一次、但渠道侧任务仍在跑并处于扣费窗口 →
 *   429/409 与未知提交必须分开处理）+ §5.3（两态时间域：30s 工具回执 ≠ 生成时限）。
 * - 设计文档 §6.3（并发 3 的作用域）：Wan 单槽=1、SDGF=2、拆解槽另有一把锁——409 是「槽被占、未提交」，
 *   语义是**排队**不是失败（`wanrepro/ui/server.py:735 / 1135 / 3670` 亲读）。
 * - 红线 25：单实例编排一律调 P4 `runWorkflowInstance`（本文件不复制六步）；状态派一律调 P5
 *   `deriveInstanceStatus`（本文件不写「成功/失败」派生逻辑）；对账只调 P5 `workflow-batch-query` 的查询口径。
 * - C5/§5.3：队列条目只存**排队簿记**（尝试次数、退避时刻、提交守卫、最近一次尝试的结局快照），
 *   不存「实例现在怎么样」——实例当前状态永远由 P5 派生器现查；两者冲突时以派生器为准（见 `planBatchResume`）。
 *
 * 常量（AGENTS.md:92 精神：不静默自填，数值与失败处置写在回卡/文档里）：
 * - `BATCH_CONCURRENCY_DEFAULT/MAX = 3`：实例级在途编排上限；超出的实例排队等槽，不是失败。
 * - `BATCH_BACKOFF_BASE_MS = 4_000`、`BATCH_BACKOFF_FACTOR = 2`、`BATCH_BACKOFF_MAX_MS = 60_000`：
 *   429/409 重排等待 4s → 8s → 16s → 32s（封顶 60s）。
 * - `BATCH_CONGESTION_MAX_RETRIES = 4`：429/409 最多重排 4 次（累计等待 ≤60s）；耗尽即按「渠道持续拥塞」如实失败
 *   并给出可读文案，不无限重试。
 * - `BATCH_INSTANCE_MAX_ATTEMPTS = 6`：同一实例跨刷新累计编排尝试上限（防「续跑→失败→再续跑」死循环）。
 * - `BATCH_SUBMIT_RECONCILE_ROUNDS = 2` / `..._INTERVAL_MS = 15_000`：未知提交对账查两轮（第二轮吃掉
 *   「后端任务列表滞后」窗口）；查不到 → explicit-unknown 停住交人，**禁止自动重发**。
 */

import type { CanvasNodeData } from "@/types/canvas";

import { batchMemberRequiredNodeIds, findBatchMember, type WorkflowBatchMember, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import type { InstanceStatus } from "@/lib/canvas/workflow-batch-status";
import type { StatusQuery } from "@/lib/canvas/workflow-batch-query";
import type { InstanceRunResult } from "@/lib/canvas/workflow-instance-run";
import { classifyChannelFailure, isCongestion, type ChannelFailureClass, type ChannelFailureKind } from "@/lib/canvas/workflow-channel-failure";

export const BATCH_QUEUE_SCHEMA_VERSION = 1;
export const BATCH_CONCURRENCY_DEFAULT = 3;
export const BATCH_CONCURRENCY_MAX = 3;
export const BATCH_BACKOFF_BASE_MS = 4_000;
export const BATCH_BACKOFF_FACTOR = 2;
export const BATCH_BACKOFF_MAX_MS = 60_000;
export const BATCH_CONGESTION_MAX_RETRIES = 4;
export const BATCH_INSTANCE_MAX_ATTEMPTS = 6;
export const BATCH_SUBMIT_RECONCILE_ROUNDS = 2;
export const BATCH_SUBMIT_RECONCILE_INTERVAL_MS = 15_000;

/** 派生状态里「已经走到终点，不该再重跑」的那些（P5 派生器给的口径）。 */
export const BATCH_TERMINAL_STATUSES: InstanceStatus[] = ["awaiting_approval", "approved", "generating", "done"];

// ─────────────────────────────────────────────── 提交守卫（未知提交对账的落点）

/**
 * 提交守卫（付费/渠道创建请求的**先写后发**凭据）。
 *
 * 顺序不可颠倒：先把「我要提交什么」写进项目并落盘，再发请求。这样刷新/断线/回执丢失后，
 * 队列能从盘上知道「这个节点可能已经发过一次」，从而**只对账、不重发**（D11 / B6）。
 */
export type BatchSubmitGuard = {
    instanceId: string;
    /** 发起创建的画布节点（渠道任务的归属节点）。 */
    nodeId: string;
    provider: string;
    /** 请求指纹（正文/参数摘要）：对账时用来说明「查到的那条是不是我们发的」。 */
    requestDigest: string;
    requestedAt: string;
    /** 任何已知的任务号线索（例如节点上残留的 videoTaskId / 已回填的 Wan job）：只用于查，不用于假设成功。 */
    candidateTaskIds?: string[];
    /** 回执：拿到就立刻补写并落盘。缺省 = 回执未确认。 */
    receipt?: { taskId: string; jobId?: string; receivedAt: string };
    /** 对账结论（只在回执缺失时写）。 */
    reconcile?: {
        at: string;
        verdict: "resolved" | "explicit_unknown";
        rounds: number;
        /** resolved = 命中的既有后端任务号（续查不重建）。 */
        taskId?: string;
        candidates: { taskId: string; status: string; nodeId?: string }[];
        reason: string;
    };
};

// ─────────────────────────────────────────────── 队列记录

export type BatchQueuePhase = "idle" | "running" | "paused" | "finished";

export type BatchQueueEntry = {
    instanceId: string;
    instanceIndex: number;
    /** 累计编排尝试次数（跨刷新持久；上限 `BATCH_INSTANCE_MAX_ATTEMPTS`）。 */
    attempts: number;
    /**
     * 429/409 **命中次数**（含最后那次终判）= 已重排次数 + 1。
     * 与 attempts 分开计：拥塞重排不该吃掉失败预算（上限见 `BATCH_CONGESTION_MAX_RETRIES`）。
     */
    congestionRetries: number;
    /** 下次可跑时刻（退避窗口）。 */
    nextEligibleAt?: string;
    lastCongestion?: { kind: "rate_limited" | "slot_busy"; status?: number; at: string; retry: number; delayMs: number; evidence: string };
    /** 提交守卫（发出付费/渠道创建前写入；回执/对账后更新）。 */
    submitGuard?: BatchSubmitGuard;
    /**
     * 最近一次编排尝试的结局快照（**尝试日志**，不是实例状态）：仅用于续跑决策与失败聚合；
     * 实例当前状态永远由 P5 `deriveInstanceStatus` 现查。
     */
    lastRun?: { at: string; outcome: "awaiting_approval" | "canvas_ready" | "failed" | "held_submit_unknown"; stage?: string; reason?: string };
};

export type BatchQueueSummary = {
    attempted: number;
    awaitingApproval: number;
    canvasReady: number;
    failed: { instanceId: string; stage?: string; reason: string }[];
    heldSubmitUnknown: { instanceId: string; verdict: "resolved" | "explicit_unknown"; taskId?: string; reason: string }[];
    skippedDone: string[];
    congestionRequeues: number;
    concurrencyPeak: number;
};

export type WorkflowBatchQueueRecord = {
    schemaVersion: typeof BATCH_QUEUE_SCHEMA_VERSION;
    /** 本批次实例级编排并发上限（创建时冻结；> `BATCH_CONCURRENCY_MAX` 会被夹到上限）。 */
    concurrency: number;
    phase: BatchQueuePhase;
    startedAt?: string;
    updatedAt: string;
    entries: BatchQueueEntry[];
    summary?: BatchQueueSummary;
};

function clampConcurrency(value: number | undefined): number {
    const raw = Math.floor(Number(value ?? BATCH_CONCURRENCY_DEFAULT));
    if (!Number.isFinite(raw) || raw < 1) return BATCH_CONCURRENCY_DEFAULT;
    return Math.min(raw, BATCH_CONCURRENCY_MAX);
}

/** 队列记录（纯函数）：D1 顺序 = 成员 `instanceIndex` 升序。 */
export function createBatchQueue(input: { members: WorkflowBatchMember[]; concurrency?: number; startedAt?: string; updatedAt?: string }): WorkflowBatchQueueRecord {
    const stamp = input.updatedAt || input.startedAt || new Date(0).toISOString();
    const entries = [...input.members]
        .sort((left, right) => left.instanceIndex - right.instanceIndex)
        .map<BatchQueueEntry>((member) => ({ instanceId: member.instanceId, instanceIndex: member.instanceIndex, attempts: 0, congestionRetries: 0 }));
    return { schemaVersion: BATCH_QUEUE_SCHEMA_VERSION, concurrency: clampConcurrency(input.concurrency), phase: "idle", startedAt: input.startedAt, updatedAt: stamp, entries };
}

/** 429/409 第 n 次重排的等待毫秒（1-based；4s→8s→16s→32s，封顶 60s）。 */
export function batchBackoffDelayMs(retry: number): number {
    const step = Math.max(1, Math.floor(Number(retry) || 1));
    const raw = BATCH_BACKOFF_BASE_MS * Math.pow(BATCH_BACKOFF_FACTOR, step - 1);
    return Math.min(BATCH_BACKOFF_MAX_MS, Math.round(raw));
}

/** 队列里某个实例的条目（缺省补一条：批次记录里有成员但队列还没入册）。 */
export function batchQueueEntry(queue: WorkflowBatchQueueRecord | undefined, member: { instanceId: string; instanceIndex: number }): BatchQueueEntry {
    const found = (queue?.entries || []).find((entry) => entry.instanceId === member.instanceId);
    return found || { instanceId: member.instanceId, instanceIndex: member.instanceIndex, attempts: 0, congestionRetries: 0 };
}

/** 写回某实例条目（不可变；返回新队列）。 */
export function withBatchQueueEntry(queue: WorkflowBatchQueueRecord, entry: BatchQueueEntry, updatedAt?: string): WorkflowBatchQueueRecord {
    const entries = queue.entries.some((item) => item.instanceId === entry.instanceId) ? queue.entries.map((item) => (item.instanceId === entry.instanceId ? entry : item)) : [...queue.entries, entry];
    return { ...queue, entries, updatedAt: updatedAt || queue.updatedAt };
}

/** 挂到批次记录上（身份结构延伸：随 `CanvasProject.workflowBatches` 持久化 + 导出同路）。 */
export function setBatchQueue(records: WorkflowBatchRecord[] | undefined, batchId: string, queue: WorkflowBatchQueueRecord | undefined): WorkflowBatchRecord[] {
    const list = Array.isArray(records) ? records : [];
    const index = list.findIndex((item) => item.batchId === batchId);
    if (index < 0) return list;
    const current = list[index];
    const next: WorkflowBatchRecord = { ...current, updatedAt: queue?.updatedAt || current.updatedAt };
    if (queue) next.queue = queue;
    else delete next.queue;
    return [...list.slice(0, index), next, ...list.slice(index + 1)];
}

// ─────────────────────────────────────────────── 提交守卫（先写后发）

export function beginBatchSubmit(input: { queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry; spec: { nodeId: string; provider: string; requestDigest: string; candidateTaskIds?: string[] }; at: string }): { queue: WorkflowBatchQueueRecord; guard: BatchSubmitGuard; entry: BatchQueueEntry } {
    const guard: BatchSubmitGuard = {
        instanceId: input.entry.instanceId,
        nodeId: input.spec.nodeId,
        provider: input.spec.provider,
        requestDigest: input.spec.requestDigest,
        requestedAt: input.at,
        ...(input.spec.candidateTaskIds?.length ? { candidateTaskIds: [...new Set(input.spec.candidateTaskIds.filter(Boolean))] } : {}),
    };
    const entry: BatchQueueEntry = { ...input.entry, submitGuard: guard };
    return { queue: withBatchQueueEntry(input.queue, entry, input.at), guard, entry };
}

export function confirmBatchSubmit(input: { queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry; receipt: { taskId: string; jobId?: string }; at: string }): { queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry } {
    const current = input.entry.submitGuard;
    if (!current) return { queue: input.queue, entry: input.entry };
    const entry: BatchQueueEntry = { ...input.entry, submitGuard: { ...current, receipt: { taskId: input.receipt.taskId, ...(input.receipt.jobId ? { jobId: input.receipt.jobId } : {}), receivedAt: input.at } } };
    return { queue: withBatchQueueEntry(input.queue, entry, input.at), entry };
}

/** 守卫清空（请求被明确拒绝、确认未提交时用：429/409/4xx 都有明确回执，不算未知）。 */
export function clearBatchSubmit(input: { queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry; at: string }): { queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry } {
    if (!input.entry.submitGuard) return { queue: input.queue, entry: input.entry };
    const entry: BatchQueueEntry = { ...input.entry };
    delete entry.submitGuard;
    return { queue: withBatchQueueEntry(input.queue, entry, input.at), entry };
}

export type GuardedSubmitOutcome =
    | { ok: true; taskId: string; jobId?: string; queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry; failure: null }
    | { ok: false; queue: WorkflowBatchQueueRecord; entry: BatchQueueEntry; failure: ChannelFailureClass | null; error: string; submitCalled: boolean };

/**
 * 守卫式提交（付费/渠道创建的唯一安全入口）：
 * ① 守卫先落盘（`persist` 失败 → **拒绝发请求**，宁可不跑也不留「发过但没记」的钱坑）；
 * ② 发请求；
 * ③ 有回执 → 补写 receipt + 落盘；
 * ④ 无回执且分类为未知提交 → 守卫留在盘上（无 receipt）→ 交对账（绝不自动重发）；
 * ⑤ 明确被拒（429/409/4xx）→ 清守卫（确认未提交）→ 由调用方决定退避重排。
 */
export async function guardedBatchSubmit<T extends { taskId: string; jobId?: string }>(input: {
    queue: WorkflowBatchQueueRecord;
    entry: BatchQueueEntry;
    spec: { nodeId: string; provider: string; requestDigest: string; candidateTaskIds?: string[] };
    persist: (queue: WorkflowBatchQueueRecord) => void | Promise<void>;
    submit: () => Promise<T>;
    at: string;
}): Promise<GuardedSubmitOutcome> {
    const begun = beginBatchSubmit({ queue: input.queue, entry: input.entry, spec: input.spec, at: input.at });
    try {
        await input.persist(begun.queue);
    } catch (error) {
        return { ok: false, queue: input.queue, entry: input.entry, failure: null, error: `提交守卫未落盘，拒绝发请求：${(error as Error).message}`, submitCalled: false };
    }

    let receipt: T;
    try {
        receipt = await input.submit();
    } catch (error) {
        const raw = error as Error & Record<string, unknown>;
        const failure = classifyChannelFailure(raw);
        if (failure && failure.kind === "submit_unknown") {
            // 回执丢失：守卫留着（无 receipt）——这是对账的入口，绝不在这里重发
            return { ok: false, queue: begun.queue, entry: begun.entry, failure, error: raw.message || String(error), submitCalled: true };
        }
        const cleared = clearBatchSubmit({ queue: begun.queue, entry: begun.entry, at: input.at });
        try {
            await input.persist(cleared.queue);
        } catch {
            /* 清守卫失败：保守起见守卫继续留在盘上（会走对账），不影响正确性 */
        }
        return { ok: false, queue: cleared.queue, entry: cleared.entry, failure, error: raw.message || String(error), submitCalled: true };
    }

    const confirmed = confirmBatchSubmit({ queue: begun.queue, entry: begun.entry, receipt: { taskId: receipt.taskId, jobId: receipt.jobId }, at: input.at });
    await input.persist(confirmed.queue);
    return { ok: true, taskId: receipt.taskId, jobId: receipt.jobId, queue: confirmed.queue, entry: confirmed.entry, failure: null };
}

// ─────────────────────────────────────────────── 未知提交对账（B6 关闭判据）

export type ReconcileOutcome = {
    verdict: "resolved" | "explicit_unknown";
    rounds: number;
    candidates: { taskId: string; status: string; nodeId?: string }[];
    taskId?: string;
    reason: string;
};

/**
 * 对账（真查，不猜）：按 P5 分组查询口径去核对**既有后端任务**。
 * - 命中该节点上的任务 → `resolved`（taskId 已存在 → 续查，不重建）；
 * - 两轮都查不到 → `explicit_unknown`（交人裁决；自动层不重发）。
 * `lookupWanFileTask` 是 S2 的交叉核对钩子：Wan 的 job→成片 taskId 若已回填，说明提交确实发生过。
 */
export async function reconcileBatchSubmit(input: {
    guard: BatchSubmitGuard;
    member?: WorkflowBatchMember | null;
    query: StatusQuery;
    rounds?: number;
    intervalMs?: number;
    sleep?: (ms: number) => Promise<void>;
    lookupWanFileTask?: (job: string) => string;
    now?: () => string;
}): Promise<ReconcileOutcome> {
    const rounds = Math.max(1, Math.floor(Number(input.rounds ?? BATCH_SUBMIT_RECONCILE_ROUNDS)));
    const intervalMs = Math.max(0, Number(input.intervalMs ?? BATCH_SUBMIT_RECONCILE_INTERVAL_MS));
    const sleep = input.sleep || ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    const now = input.now || (() => new Date().toISOString());
    const nodeIds = [input.guard.nodeId, ...(input.member ? batchMemberRequiredNodeIds(input.member) : [])].filter((id, index, list) => Boolean(id) && list.indexOf(id) === index);
    const candidates: { taskId: string; status: string; nodeId?: string }[] = [];

    for (let round = 1; round <= rounds; round += 1) {
        // ① 按节点查既有任务（既有 generation_get_status 口径，不新写轮询器）
        if (nodeIds.length) {
            const response = await input.query({ nodeIds, limit: 100 });
            for (const task of response?.tasks || []) {
                if (!task?.id) continue;
                if (nodeIds.includes(task.id)) candidates.push({ taskId: task.id, status: String(task.status || "unknown"), nodeId: task.id });
            }
        }
        // ② 按已知任务号单查（线索：节点残留 videoTaskId / 调用方给的候选）
        for (const taskId of input.guard.candidateTaskIds || []) {
            const response = await input.query({ taskId, limit: 1 });
            const row = (response?.tasks || []).find((task) => task.id === taskId);
            if (row) candidates.push({ taskId, status: String(row.status || "unknown") });
        }
        // ③ Wan 成片映射交叉核对（S2）：job 已在映射里 = 提交确实发生过
        for (const taskId of input.guard.candidateTaskIds || []) {
            const fileTaskId = input.lookupWanFileTask?.(taskId) || "";
            if (fileTaskId) candidates.push({ taskId: fileTaskId, status: "mapped", nodeId: input.guard.nodeId });
        }

        const unique = dedupeCandidates(candidates);
        if (unique.length) {
            return { verdict: "resolved", rounds: round, candidates: unique, taskId: unique[0].taskId, reason: `对账第 ${round} 轮命中 ${unique.length} 条既有后端任务：按 taskId 续查，不重建` };
        }
        if (round < rounds) await sleep(intervalMs);
    }

    return {
        verdict: "explicit_unknown",
        rounds,
        candidates: [],
        reason: `对账 ${rounds} 轮均未找到该提交对应的后端任务（节点 ${input.guard.nodeId}）：标 explicit-unknown 交人裁决，自动层不重发`,
    };
}

function dedupeCandidates(list: { taskId: string; status: string; nodeId?: string }[]) {
    const seen = new Set<string>();
    const out: { taskId: string; status: string; nodeId?: string }[] = [];
    for (const item of list) {
        if (!item?.taskId || seen.has(item.taskId)) continue;
        seen.add(item.taskId);
        out.push(item);
    }
    return out;
}

// ─────────────────────────────────────────────── 恢复计划

export type BatchResumePlan = {
    /** 需要跑（含重跑）的实例（D1 顺序）。 */
    run: string[];
    /** 已到终点（待批/已批/生成中/完成）：不重跑。 */
    skippedDone: { instanceId: string; status: InstanceStatus }[];
    /** 无回执的提交守卫：先对账，不重跑、不重发。 */
    danglingSubmit: { instanceId: string; guard: BatchSubmitGuard }[];
    /** 对账已判 explicit-unknown：停住等人。 */
    heldSubmitUnknown: { instanceId: string; guard: BatchSubmitGuard }[];
    /** 尝试预算耗尽：停住等人。 */
    retryExhausted: { instanceId: string; attempts: number }[];
    /** 批次记录里没有成员（身份缺失）：不凭空造实例。 */
    missingMember: string[];
    /** 实例节点骨架是否已在画布上（true = 不需重新 materialize，避免重建节点/重拉片）。 */
    prepareNeeded: string[];
};

/**
 * 续跑计划（纯函数）：**派生状态优先**——批次记录/队列条目只是簿记，判定「这实例跑完没有」一律看 P5 派生。
 * 断线/刷新后按此计划续跑：跳过已完成的、先对账未知提交、其余按队列顺序重跑。
 */
export function planBatchResume(input: { batch: WorkflowBatchRecord; queue?: WorkflowBatchQueueRecord; statuses?: Record<string, InstanceStatus>; nodes?: CanvasNodeData[]; log?: (message: string) => void }): BatchResumePlan {
    const nodeIds = new Set((input.nodes || []).map((node) => node.id));
    const plan: BatchResumePlan = { run: [], skippedDone: [], danglingSubmit: [], heldSubmitUnknown: [], retryExhausted: [], missingMember: [], prepareNeeded: [] };
    const members = [...input.batch.members].sort((left, right) => left.instanceIndex - right.instanceIndex);

    for (const member of members) {
        const entry = batchQueueEntry(input.queue, member);
        const status = input.statuses?.[member.instanceId];
        const nodesPresent = batchMemberRequiredNodeIds(member).every((id) => nodeIds.has(id));
        const materialized = Boolean(member.backend.lapianTaskId) && nodesPresent;

        if (status && BATCH_TERMINAL_STATUSES.includes(status)) {
            plan.skippedDone.push({ instanceId: member.instanceId, status });
            continue;
        }
        const guard = entry.submitGuard;
        if (guard && !guard.receipt) {
            if (guard.reconcile?.verdict === "explicit_unknown") {
                plan.heldSubmitUnknown.push({ instanceId: member.instanceId, guard });
                continue;
            }
            if (!guard.reconcile) {
                plan.danglingSubmit.push({ instanceId: member.instanceId, guard });
                continue;
            }
        }
        if (entry.attempts >= BATCH_INSTANCE_MAX_ATTEMPTS) {
            plan.retryExhausted.push({ instanceId: member.instanceId, attempts: entry.attempts });
            continue;
        }
        plan.run.push(member.instanceId);
        if (!materialized) plan.prepareNeeded.push(member.instanceId);
    }
    if (!members.length) input.log?.("批次记录没有任何成员：续跑计划为空（不凭空造实例）");
    return plan;
}

// ─────────────────────────────────────────────── 编排执行

export type BatchQueueJob = {
    instanceId: string;
    instanceIndex: number;
    /** 幂等准备（P3 `materializeWorkflowInstance`）：已落库的实例应在实现里走 `already_present` 分支。 */
    prepare?: () => Promise<{ ok: boolean; status?: string; reason?: string }>;
    /** 单实例编排（P4 `runWorkflowInstance`）——本文件不复制六步。 */
    run: () => Promise<InstanceRunResult>;
};

export type BatchQueueInstanceOutcome = {
    instanceId: string;
    instanceIndex: number;
    outcome: "awaiting_approval" | "canvas_ready" | "failed" | "held_submit_unknown" | "skipped_done" | "not_started";
    stage?: string;
    reason?: string;
    attempts: number;
    congestionRetries: number;
    resumed: boolean;
};

export type BatchQueueRunResult = {
    queue: WorkflowBatchQueueRecord;
    perInstance: BatchQueueInstanceOutcome[];
    concurrencyPeak: number;
    /** 并发曲线：每次槽位变化记一条（探针据此断言峰值 ≤ 上限）。 */
    curve: { at: number; active: number; running: string[] }[];
    aggregate: BatchQueueSummary;
    /** 付费视频创建调用次数（队列自身恒为 0：本层不发起任何生成/创建）。 */
    paidVideoCreations: number;
    warnings: string[];
};

function classifyRunFailure(result: InstanceRunResult | { failure?: { stage?: string; reason?: string; channel?: ChannelFailureClass } } | undefined): ChannelFailureClass | null {
    const failure = (result as { failure?: { reason?: string; channel?: ChannelFailureClass } } | undefined)?.failure;
    if (!failure) return null;
    if (failure.channel) return failure.channel;
    return classifyChannelFailure({ message: failure.reason });
}

function isTerminalOk(status: InstanceRunResult["status"]): status is "awaiting_approval" | "canvas_ready" {
    return status === "awaiting_approval" || status === "canvas_ready";
}

/**
 * 执行批次队列（真跑；调用方注入实例作业与落盘）。
 *
 * 语义要点：
 * - 并发只在**实例之间**（作用域 = 实例级编排并发），同实例任何时刻只有一个在跑（防同节点重触发 + 扣费窗口）；
 * - 429/409 → 指数退避重排队（上限 4 次），**不算失败**；
 * - 未知提交 → 对账；对账有结论就停住该实例（resolved=续查既有任务 / explicit_unknown=交人），
 *   **本轮不再发第二次创建请求**；
 * - 单实例失败不影响其它实例；收口时聚合 per-instance 失败原因。
 */
export async function runBatchQueue(input: {
    batch: WorkflowBatchRecord;
    jobs: BatchQueueJob[];
    queue?: WorkflowBatchQueueRecord;
    /** 恢复计划：已跳过/已停住的实例进聚合，jobs 只含 `plan.run` 里的实例。 */
    resume?: BatchResumePlan;
    nodes?: CanvasNodeData[];
    concurrency?: number;
    persist?: (queue: WorkflowBatchQueueRecord) => void | Promise<void>;
    sleep?: (ms: number) => Promise<void>;
    now?: () => number;
    onSlot?: (sample: { at: number; active: number; running: string[] }) => void;
    shouldPause?: () => boolean;
    /** 未知提交对账依赖（生产 = P5 查询口径 + S2 映射；缺省 = 无对账能力，直接停住交人）。 */
    reconcile?: (input: { guard: BatchSubmitGuard; member?: WorkflowBatchMember | null }) => Promise<ReconcileOutcome>;
    log?: (message: string) => void;
}): Promise<BatchQueueRunResult> {
    const now = input.now || (() => Date.now());
    const clock = () => new Date(now()).toISOString();
    const sleep = input.sleep || ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    const concurrency = clampConcurrency(input.concurrency ?? input.queue?.concurrency);
    const warnings: string[] = [];

    let queue: WorkflowBatchQueueRecord = input.queue
        ? { ...input.queue, entries: input.queue.entries.map((entry) => ({ ...entry })), concurrency }
        : createBatchQueue({ members: input.batch.members, concurrency, startedAt: clock(), updatedAt: clock() });
    queue = { ...queue, concurrency, phase: "running", startedAt: queue.startedAt || clock(), updatedAt: clock() };
    const entries = new Map(queue.entries.map((entry) => [entry.instanceId, { ...entry }]));
    const memberOf = (instanceId: string) => findBatchMember(input.batch, instanceId) || null;

    const perInstance: BatchQueueInstanceOutcome[] = [];
    const curve: BatchQueueRunResult["curve"] = [];
    const aggregate: BatchQueueSummary = { attempted: 0, awaitingApproval: 0, canvasReady: 0, failed: [], heldSubmitUnknown: [], skippedDone: [], congestionRequeues: 0, concurrencyPeak: 0 };
    const attemptedInstances = new Set<string>();

    let paidVideoCreations = 0;
    let peak = 0;

    const syncQueue = async () => {
        queue = { ...queue, updatedAt: clock(), entries: [...entries.values()].sort((left, right) => left.instanceIndex - right.instanceIndex) };
        if (input.persist) await input.persist({ ...queue, entries: queue.entries.map((entry) => ({ ...entry })) });
    };

    // 恢复计划里的「已完成 / 已停住」先进聚合（这些实例不进 jobs，也不该被重新拉片）
    for (const item of input.resume?.skippedDone || []) {
        aggregate.skippedDone.push(item.instanceId);
        const entry = entries.get(item.instanceId);
        perInstance.push({ instanceId: item.instanceId, instanceIndex: entry?.instanceIndex ?? 0, outcome: "skipped_done", reason: `派生状态 ${item.status}：已到终点，不重跑`, attempts: entry?.attempts ?? 0, congestionRetries: entry?.congestionRetries ?? 0, resumed: true });
    }
    for (const item of input.resume?.heldSubmitUnknown || []) {
        aggregate.heldSubmitUnknown.push({ instanceId: item.instanceId, verdict: "explicit_unknown", reason: item.guard.reconcile?.reason || "对账未命中：停住交人" });
        perInstance.push({ instanceId: item.instanceId, instanceIndex: entries.get(item.instanceId)?.instanceIndex ?? 0, outcome: "held_submit_unknown", reason: item.guard.reconcile?.reason, attempts: entries.get(item.instanceId)?.attempts ?? 0, congestionRetries: entries.get(item.instanceId)?.congestionRetries ?? 0, resumed: true });
    }
    for (const item of input.resume?.retryExhausted || []) {
        aggregate.failed.push({ instanceId: item.instanceId, stage: "attempts_exhausted", reason: `累计编排尝试 ${item.attempts} 次仍失败（上限 ${BATCH_INSTANCE_MAX_ATTEMPTS}）：停住交人` });
        perInstance.push({ instanceId: item.instanceId, instanceIndex: entries.get(item.instanceId)?.instanceIndex ?? 0, outcome: "failed", stage: "attempts_exhausted", reason: `累计编排尝试 ${item.attempts} 次仍失败`, attempts: item.attempts, congestionRetries: entries.get(item.instanceId)?.congestionRetries ?? 0, resumed: true });
    }

    const sample = () => {
        const entryList = [...active.keys()];
        curve.push({ at: now(), active: entryList.length, running: entryList });
        input.onSlot?.({ at: now(), active: entryList.length, running: entryList });
    };

    const readyQueue: BatchQueueJob[] = [...input.jobs].sort((left, right) => left.instanceIndex - right.instanceIndex);
    const delayed: BatchQueueJob[] = [];
    const active = new Map<string, Promise<void>>();
    let paused = false;
    let stallWaits = 0;

    const launch = (job: BatchQueueJob) => {
        const entry = entries.get(job.instanceId) || { instanceId: job.instanceId, instanceIndex: job.instanceIndex, attempts: 0, congestionRetries: 0 };
        entries.set(job.instanceId, entry);
        const task = (async () => {
            // 同实例在跑时不可能有第二个 job（job 出队才 launch）；守卫无回执 → 只对账，不重跑
            const guard = entry.submitGuard;
            if (guard && !guard.receipt && !guard.reconcile) {
                await handleDanglingSubmit(job, guard);
                return;
            }
            entry.attempts += 1;
            if (!attemptedInstances.has(job.instanceId)) {
                attemptedInstances.add(job.instanceId);
                aggregate.attempted += 1;
            }
            await syncQueue();

            // ① 幂等准备（P3）：恢复场景实例节点已在画布上 → 不重复建节点/重拉片
            if (job.prepare) {
                const prepared = await job.prepare();
                if (!prepared.ok) {
                    entry.lastRun = { at: clock(), outcome: "failed", stage: "prepare", reason: prepared.reason };
                    aggregate.failed.push({ instanceId: job.instanceId, stage: "prepare", reason: prepared.reason || "准备阶段失败" });
                    perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "failed", stage: "prepare", reason: prepared.reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
                    await syncQueue();
                    return;
                }
            }

            // ② 单实例编排（P4）
            let result: InstanceRunResult;
            try {
                result = await job.run();
            } catch (error) {
                const failure = classifyChannelFailure(error);
                await handleFailure(job, entry, { stage: "run_threw", reason: (error as Error).message }, failure);
                return;
            }
            paidVideoCreations += Number(result.paidVideoCreations || 0);

            if (isTerminalOk(result.status)) {
                const outcome = result.status;
                entry.lastRun = { at: clock(), outcome };
                if (outcome === "awaiting_approval") aggregate.awaitingApproval += 1;
                else aggregate.canvasReady += 1;
                perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
                await syncQueue();
                return;
            }
            const failure = classifyRunFailure(result);
            await handleFailure(job, entry, { stage: result.failure?.stage, reason: result.failure?.reason || "实例编排失败（未给出原因）" }, failure);
        })().finally(() => {
            active.delete(job.instanceId);
            sample();
        });
        active.set(job.instanceId, task);
        peak = Math.max(peak, active.size);
        aggregate.concurrencyPeak = peak;
        sample();
    };

    const handleFailure = async (job: BatchQueueJob, entry: BatchQueueEntry, failure: { stage?: string; reason: string }, className: ChannelFailureClass | null) => {
        // 未知提交：只对账，绝不重发（D11）
        if (className && className.kind === "submit_unknown") {
            const member = memberOf(job.instanceId);
            const guard: BatchSubmitGuard =
                entry.submitGuard || { instanceId: job.instanceId, nodeId: member?.nodes.videoConfig || member?.nodes.lapian || job.instanceId, provider: "unknown", requestDigest: "", requestedAt: clock() };
            const outcome = input.reconcile ? await input.reconcile({ guard, member: memberOf(job.instanceId) }) : { verdict: "explicit_unknown" as const, rounds: 0, candidates: [], reason: "没有对账依赖：按 explicit-unknown 停住交人（不重发）" };
            const reconciled: BatchSubmitGuard = { ...guard, reconcile: { at: clock(), verdict: outcome.verdict, rounds: outcome.rounds, candidates: outcome.candidates, ...(outcome.taskId ? { taskId: outcome.taskId } : {}), reason: outcome.reason } };
            entry.submitGuard = reconciled;
            entry.lastRun = { at: clock(), outcome: "held_submit_unknown", stage: failure.stage, reason: outcome.reason };
            aggregate.heldSubmitUnknown.push({ instanceId: job.instanceId, verdict: outcome.verdict, ...(outcome.taskId ? { taskId: outcome.taskId } : {}), reason: outcome.reason });
            perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "held_submit_unknown", stage: failure.stage, reason: outcome.reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
            warnings.push(`${job.instanceId}：创建回执丢失 → 对账 ${outcome.verdict === "resolved" ? "命中既有任务（按 taskId 续查，不重建）" : "未命中（explicit-unknown，停住交人）"}；自动层未重发`);
            await syncQueue();
            return;
        }

        // 渠道拥塞（429/409）：退避重排队，不算失败
        if (className && isCongestion(className.kind)) {
            entry.congestionRetries += 1;
            if (entry.congestionRetries > BATCH_CONGESTION_MAX_RETRIES) {
                const reason = `渠道持续拥塞（${className.kind === "rate_limited" ? "429 限流" : "409 单槽忙"}）已退避重排 ${BATCH_CONGESTION_MAX_RETRIES} 次仍失败：停住交人（调低并发或稍后重跑）`;
                entry.lastRun = { at: clock(), outcome: "failed", stage: failure.stage, reason };
                aggregate.failed.push({ instanceId: job.instanceId, stage: failure.stage || "congestion", reason });
                perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "failed", stage: failure.stage, reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
                await syncQueue();
                return;
            }
            const delayMs = batchBackoffDelayMs(entry.congestionRetries);
            entry.nextEligibleAt = new Date(now() + delayMs).toISOString();
            entry.lastCongestion = { kind: className.kind, ...(className.status ? { status: className.status } : {}), at: clock(), retry: entry.congestionRetries, delayMs, evidence: className.evidence };
            aggregate.congestionRequeues += 1;
            warnings.push(`${job.instanceId}：渠道拥塞（${className.kind}，${className.evidence}）→ 退避 ${delayMs}ms 后重排（第 ${entry.congestionRetries}/${BATCH_CONGESTION_MAX_RETRIES} 次）`);
            await syncQueue();
            readyQueue.push(job);
            return;
        }

        // 其它失败：本实例失败，其他实例继续（失败隔离）
        const reason = failure.reason;
        entry.lastRun = { at: clock(), outcome: "failed", stage: failure.stage, reason };
        aggregate.failed.push({ instanceId: job.instanceId, stage: failure.stage, reason });
        perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "failed", stage: failure.stage, reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
        await syncQueue();
    };

    const handleDanglingSubmit = async (job: BatchQueueJob, guard: BatchSubmitGuard) => {
        const entry = entries.get(job.instanceId)!;
        const outcome = input.reconcile ? await input.reconcile({ guard, member: memberOf(job.instanceId) }) : { verdict: "explicit_unknown" as const, rounds: 0, candidates: [], reason: "没有对账依赖：按 explicit-unknown 停住交人（不重发）" };
        entry.submitGuard = { ...guard, reconcile: { at: clock(), verdict: outcome.verdict, rounds: outcome.rounds, candidates: outcome.candidates, ...(outcome.taskId ? { taskId: outcome.taskId } : {}), reason: outcome.reason } };
        entry.lastRun = { at: clock(), outcome: "held_submit_unknown", reason: outcome.reason };
        aggregate.heldSubmitUnknown.push({ instanceId: job.instanceId, verdict: outcome.verdict, ...(outcome.taskId ? { taskId: outcome.taskId } : {}), reason: outcome.reason });
        perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "held_submit_unknown", reason: outcome.reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: true });
        warnings.push(`${job.instanceId}：盘上存在无回执的提交守卫 → 只对账（${outcome.verdict}），未重跑/未重发`);
        await syncQueue();
    };

    try {
        while (readyQueue.length || delayed.length || active.size) {
            if (input.shouldPause?.()) {
                paused = true;
                // 安全边界：不再起新实例，但**在途实例必须跑完再返回**——否则返回后在途实例成孤儿，
                // 其结果会在调用方已收口之后写回（聚合/条目被事后改写，UI 显示「已暂停」却在偷偷跑）。
                if (active.size) await Promise.all([...active.values()]);
                break;
            }
            while (active.size < concurrency && readyQueue.length) {
                const job = readyQueue.shift()!;
                const entry = entries.get(job.instanceId) || { instanceId: job.instanceId, instanceIndex: job.instanceIndex, attempts: 0, congestionRetries: 0 };
                entries.set(job.instanceId, entry);
                if (entry.nextEligibleAt && Date.parse(entry.nextEligibleAt) > now()) {
                    delayed.push(job);
                    continue;
                }
                launch(job);
            }
            if (!active.size) {
                if (!readyQueue.length && !delayed.length) break;
                if (readyQueue.length) continue;
                const waits = delayed.map((job) => Date.parse(entries.get(job.instanceId)?.nextEligibleAt || "") || 0).filter((value) => value > 0);
                const earliest = waits.length ? Math.min(...waits) : now();
                const before = now();
                await sleep(Math.max(0, earliest - before));
                // 防呆：sleep 依赖没让时间前进（探针/替身写错）时不能无限等——如实失败交人，绝不空转
                stallWaits = now() > before ? 0 : stallWaits + 1;
                if (stallWaits > 2) {
                    for (const job of delayed) {
                        const entry = entries.get(job.instanceId)!;
                        const reason = "退避等待未推进（sleep 依赖未让时钟前进）：停止等待，交人重跑";
                        entry.lastRun = { at: clock(), outcome: "failed", stage: "backoff_stalled", reason };
                        aggregate.failed.push({ instanceId: job.instanceId, stage: "backoff_stalled", reason });
                        perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "failed", stage: "backoff_stalled", reason, attempts: entry.attempts, congestionRetries: entry.congestionRetries, resumed: false });
                    }
                    delayed.length = 0;
                    break;
                }
                readyQueue.push(...delayed.splice(0));
                continue;
            }
            await Promise.race([...active.values()]);
        }
    } finally {
        queue = {
            ...queue,
            phase: paused ? "paused" : "finished",
            updatedAt: clock(),
            entries: [...entries.values()].sort((left, right) => left.instanceIndex - right.instanceIndex),
            summary: { ...aggregate, concurrencyPeak: peak },
        };
        await syncQueue();
    }

    // 未起跑的实例如实记录（暂停/中断时）
    const started = new Set(perInstance.map((item) => item.instanceId));
    for (const job of [...readyQueue, ...delayed]) {
        if (started.has(job.instanceId)) continue;
        perInstance.push({ instanceId: job.instanceId, instanceIndex: job.instanceIndex, outcome: "not_started", attempts: entries.get(job.instanceId)?.attempts ?? 0, congestionRetries: entries.get(job.instanceId)?.congestionRetries ?? 0, resumed: false });
    }
    perInstance.sort((left, right) => left.instanceIndex - right.instanceIndex);

    return {
        queue: { ...queue, summary: { ...aggregate, concurrencyPeak: peak } },
        perInstance,
        concurrencyPeak: peak,
        curve,
        aggregate,
        paidVideoCreations,
        warnings,
    };
}

/** 渠道失败类别别名（供调用方/探针断言用，避免重复 import 两个模块）。 */
export type { ChannelFailureKind };
