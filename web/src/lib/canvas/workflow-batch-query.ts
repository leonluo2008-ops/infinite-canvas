/**
 * 批次成员的远端任务查询覆盖（P5；B5 判据落地：「tasks 查询默认 20 / 上限 100 / 无分页」）。
 *
 * 陷阱（B5 实测）：既有 `generation_get_status` 是**一次调用一个截断列表**——
 * `limit` 缺省 20、被夹在 1..100（`agent-node-text.ts` `Math.max(1, Math.min(100, …))`），
 * 结果 `tasks.slice(0, limit)`，没有 offset/cursor。直接拿它查一个批次（N 个实例 × 每实例 8+ 节点）
 * 必然被静默截断：超过 20 的前半批永远查不到，而 UI 会以为「那些实例就是没状态」。
 *
 * 本文件的做法（只用既有查询，不新写轮询器 —— 红线 25）：
 * ① 按**成员分组**下发：成员节点 id 分块（每块 ≤ 上限），块内 limit=块大小；
 * ② 截断可检测：`total > tasks.length` 说明被切了 → 递归二分重查（分页拉全的等价物）；
 * ③ 本地去重：`source:id` 去重合并；
 * ④ 覆盖不足**显式**：查不全的节点 id 进 `uncoveredNodeIds` + `complete=false` + warning，
 *    派生层据此报「部分未知」，绝不把查不到当成功。
 */

import { batchMemberNodeIds, type WorkflowBatchMember } from "@/lib/canvas/workflow-batch";
import type { BackendTaskStatus, MemberTaskEvidence } from "@/lib/canvas/workflow-batch-status";

/** `generation_get_status` 的缺省条数（B5：不传 limit 就只回 20 条）。 */
export const STATUS_QUERY_DEFAULT_LIMIT = 20;
/** `generation_get_status` 的硬上限（B5：>100 被静默夹紧，且无分页）。 */
export const STATUS_QUERY_MAX_LIMIT = 100;

export type StatusQueryTask = {
    id: string;
    status: string;
    source?: string;
    kind?: string;
    title?: string;
};

export type StatusQueryResponse = {
    /** 匹配总数（**截断前**；既有实现返回的就是这个口径，本文件用它检测截断）。 */
    total: number;
    tasks: StatusQueryTask[];
};

export type StatusQuery = (input: { nodeIds?: string[]; taskId?: string; limit?: number }) => Promise<StatusQueryResponse>;

export type BatchTaskQueryResult = {
    byNodeId: Map<string, BackendTaskStatus>;
    byTaskId: Map<string, BackendTaskStatus>;
    tasks: Map<string, StatusQueryTask>;
    coverage: {
        requestedNodeIds: string[];
        coveredNodeIds: string[];
        /** 查了但确实没有任务状态（idle/无生成/Lapian 这类本就没有通用 status）——不等于「没覆盖」。 */
        noTaskNodeIds: string[];
        /** 因截断查不全的节点（覆盖不足，派生层必须报部分未知）。 */
        uncoveredNodeIds: string[];
        requestedTaskIds: string[];
        missingTaskIds: string[];
        complete: boolean;
        queries: number;
        truncatedQueries: number;
        splitRounds: number;
    };
    warnings: string[];
};

/** 分块计划（纯函数）：块大小 ≤ 上限，去重保序。探针据此断言「>100 规模也不会一次塞进去」。 */
export function planStatusQueryChunks(nodeIds: string[], maxLimit: number = STATUS_QUERY_MAX_LIMIT): string[][] {
    const limit = Math.max(1, Math.floor(Number(maxLimit) || STATUS_QUERY_MAX_LIMIT));
    const unique = nodeIds.filter(Boolean).filter((id, index, list) => list.indexOf(id) === index);
    const chunks: string[][] = [];
    for (let cursor = 0; cursor < unique.length; cursor += limit) chunks.push(unique.slice(cursor, cursor + limit));
    return chunks;
}

const TASK_KEY = (task: StatusQueryTask) => `${task.source || "canvas"}:${task.id}`;

function normalizeTaskStatus(value: unknown): BackendTaskStatus {
    const text = String(value ?? "").toLowerCase();
    if (text === "queued") return "queued";
    if (text === "running" || text === "loading" || text === "processing") return "running";
    if (text === "succeeded" || text === "success" || text === "done") return "succeeded";
    if (text === "failed" || text === "error" || text === "fail") return "failed";
    return "unknown";
}

/**
 * 批次成员任务查询（真下发、真去重、真检测截断）。
 * 每个节点块一次查询；被截断就二分重查（≤1 个仍被截断 → 该 id 记未覆盖）。
 */
export async function queryBatchMemberTasks(input: {
    members: WorkflowBatchMember[];
    query: StatusQuery;
    maxLimit?: number;
    /** 是否额外按 videoTaskId 单查后端任务（缺省 true；工作台任务只有 taskId 口径）。 */
    includeTaskIds?: boolean;
}): Promise<BatchTaskQueryResult> {
    const maxLimit = Math.max(1, Math.floor(Number(input.maxLimit ?? STATUS_QUERY_MAX_LIMIT)));
    const requestedNodeIds = input.members.flatMap((member) => batchMemberNodeIds(member)).filter((id, index, list) => list.indexOf(id) === index);
    const requestedTaskIds = (input.includeTaskIds === false ? [] : input.members.map((member) => member.backend.videoTaskId || ""))
        .filter(Boolean)
        .filter((id, index, list) => list.indexOf(id) === index);

    const tasks = new Map<string, StatusQueryTask>();
    const uncovered: string[] = [];
    let queries = 0;
    let truncatedQueries = 0;
    let splitRounds = 0;

    const queue: string[][] = planStatusQueryChunks(requestedNodeIds, maxLimit);
    while (queue.length) {
        const chunk = queue.shift()!;
        queries += 1;
        const response = await input.query({ nodeIds: chunk, limit: Math.min(chunk.length, maxLimit) });
        const rows = Array.isArray(response?.tasks) ? response.tasks : [];
        const truncated = Number(response?.total || 0) > rows.length;
        if (truncated && chunk.length > 1) {
            // 分页拉全的等价物：二分重查（既有接口无 offset，只能缩小查询面）。
            truncatedQueries += 1;
            splitRounds += 1;
            const middle = Math.ceil(chunk.length / 2);
            queue.unshift(chunk.slice(middle), chunk.slice(0, middle));
            continue;
        }
        if (truncated) {
            truncatedQueries += 1;
            uncovered.push(chunk[0]);
            continue;
        }
        for (const row of rows) {
            if (!row || !row.id) continue;
            tasks.set(TASK_KEY(row), { ...row, status: String(row.status || "unknown") });
        }
    }

    // 后端任务 id 单查（视频任务 / 工作台任务）：拿不到就如实记 missing，不补默认值。
    const taskStatusById = new Map<string, BackendTaskStatus>();
    for (const taskId of requestedTaskIds) {
        queries += 1;
        const response = await input.query({ taskId, limit: 1 });
        const row = (response?.tasks || []).find((task) => task.id === taskId);
        if (row) {
            taskStatusById.set(taskId, normalizeTaskStatus(row.status));
            tasks.set(TASK_KEY(row), { ...row, status: String(row.status || "unknown") });
        }
    }

    const byNodeId = new Map<string, BackendTaskStatus>();
    for (const [key, task] of tasks) {
        if (!key.startsWith("canvas:")) continue;
        byNodeId.set(task.id, normalizeTaskStatus(task.status));
    }

    const coveredNodeIds = requestedNodeIds.filter((id) => byNodeId.has(id));
    const noTaskNodeIds = requestedNodeIds.filter((id) => !byNodeId.has(id) && !uncovered.includes(id));
    const missingTaskIds = requestedTaskIds.filter((id) => !taskStatusById.has(id));
    const warnings: string[] = [];
    if (uncovered.length) warnings.push(`部分未知：${uncovered.length} 个节点因查询截断未被覆盖（${uncovered.slice(0, 5).join(",")}）——不按成功处理`);
    if (missingTaskIds.length) warnings.push(`部分未知：${missingTaskIds.length} 个后端任务查不到状态（${missingTaskIds.slice(0, 5).join(",")}）`);
    if (noTaskNodeIds.length) warnings.push(`${noTaskNodeIds.length} 个节点没有任务状态（idle/无生成；Lapian 本就无通用 status）`);

    return {
        byNodeId,
        byTaskId: taskStatusById,
        tasks,
        coverage: {
            requestedNodeIds,
            coveredNodeIds,
            noTaskNodeIds,
            uncoveredNodeIds: uncovered,
            requestedTaskIds,
            missingTaskIds,
            complete: uncovered.length === 0,
            queries,
            truncatedQueries,
            splitRounds,
        },
        warnings,
    };
}

/** 把批次查询结果切成「每个成员一份证据」（派生器按成员消费）。 */
export function splitEvidenceByMember(input: {
    members: WorkflowBatchMember[];
    result: BatchTaskQueryResult;
    /** 额外任务状态（拉片 job 这类不在 generation 查询口径里的后端任务；由调用方现查后注入）。 */
    extraTaskStatus?: Map<string, BackendTaskStatus>;
}): Map<string, MemberTaskEvidence> {
    const map = new Map<string, MemberTaskEvidence>();
    for (const member of input.members) {
        const nodeIds = batchMemberNodeIds(member);
        const byNodeId = new Map<string, BackendTaskStatus>();
        for (const id of nodeIds) {
            const status = input.result.byNodeId.get(id);
            if (status) byNodeId.set(id, status);
        }
        const byTaskId = new Map<string, BackendTaskStatus>();
        for (const taskId of [member.backend.videoTaskId, member.backend.lapianTaskId].filter(Boolean) as string[]) {
            const status = input.result.byTaskId.get(taskId) || input.extraTaskStatus?.get(taskId);
            if (status) byTaskId.set(taskId, status);
        }
        const missingIds = nodeIds.filter((id) => input.result.coverage.uncoveredNodeIds.includes(id));
        map.set(member.instanceId, { byNodeId, byTaskId, coverage: { complete: missingIds.length === 0, missingIds } });
    }
    return map;
}
