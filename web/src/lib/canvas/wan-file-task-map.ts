/**
 * Wan 成片映射（S2；P6 收口）：job → 成片 taskId 的**随项目持久化**登记表。
 *
 * 为什么必须做（P0a §5.3 + 收敛裁决 A2/A5/B6+S2 亲读）：
 * 旧实现只把映射塞进 `localStorage["wan_video_file_tasks"]`，且 `slice(-200)`——
 * ① 超 200 条即静默丢老映射；② 导出/导入完全不带（换机/换浏览器后老成片查不回来）；
 * ③ 项目与映射分叉（项目在 IndexedDB，映射在另一个键里）。
 * 结果就是「job 在、成片找不到」——批量场景里等于整条实例的终态产物丢失。
 *
 * 本文件是纯函数层（不读 store、不摸 localStorage 全局），只做三件事：
 * - 老载荷解析（兼容：`{job: fileTaskId}` 这种 localStorage 形状仍然可读）；
 * - 合并/去重/查找（项目记录优先于老 localStorage 记录）；
 * - 老载荷回写（**保持旧行为不变**：仍是 `{job: taskId}`、仍按 200 条截断——兼容回归不能破）。
 *
 * 落点：`CanvasProject.workflowWanFileTasks: WanFileTaskRecord[]`（与 `workflowTemplate` /
 * `workflowInstances` / `workflowBatches` 同族显式扩展字段：随项目持久化 + 随 v3 导出同路）。
 */

import { stableDigest } from "@/lib/canvas/workflow-template";

export const WORKFLOW_WAN_FILE_TASKS_SCHEMA_VERSION = 1;
/** 旧实现的 localStorage 键（保持原值：老数据必须还能读）。 */
export const WAN_FILE_TASK_LEGACY_KEY = "wan_video_file_tasks";
/** 旧实现的条数上限（老载荷回写时保持 200——兼容回归不能破）。 */
export const WAN_FILE_TASK_LEGACY_LIMIT = 200;

export type WanFileTaskRecord = {
    /** Wan 侧 job id（画布节点 `videoTaskId` 存的就是它）。 */
    job: string;
    /** 成片文件 taskId（`GET /api/wan/video/file/{taskId}` 用的那个；只随 run 返回一次）。 */
    fileTaskId: string;
    provider: "wan";
    recordedAt: string;
    /** 可选归属（有则记：便于从成片反查节点/批次）。 */
    nodeId?: string;
    batchId?: string;
    instanceId?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** 单条记录规整（导入/老数据/坏载荷都走这里：坏条目直接丢，不猜）。 */
export function normalizeWanFileTaskRecord(value: unknown, fallbackAt = ""): WanFileTaskRecord | null {
    if (!isRecord(value)) return null;
    const job = String(value.job ?? "").trim();
    const fileTaskId = String(value.fileTaskId ?? value.taskId ?? "").trim();
    if (!job || !fileTaskId) return null;
    return {
        job,
        fileTaskId,
        provider: "wan",
        recordedAt: String(value.recordedAt || fallbackAt || ""),
        ...(value.nodeId ? { nodeId: String(value.nodeId) } : {}),
        ...(value.batchId ? { batchId: String(value.batchId) } : {}),
        ...(value.instanceId ? { instanceId: String(value.instanceId) } : {}),
    };
}

/**
 * 解析老的 localStorage 载荷（`{"job-1":"task-1",...}`）。
 * 老数据仍可读——这是「不破坏既有行为」的一半；另一半是 `toLegacyPayload` 继续按 200 条回写。
 */
export function parseLegacyWanFileTasks(raw: string | null | undefined, recordedAt = ""): WanFileTaskRecord[] {
    if (!raw) return [];
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return []; // 坏载荷当不存在（与旧实现同口径）
    }
    if (!isRecord(parsed)) return [];
    const out: WanFileTaskRecord[] = [];
    for (const [job, taskId] of Object.entries(parsed)) {
        const record = normalizeWanFileTaskRecord({ job, fileTaskId: taskId, recordedAt }, recordedAt);
        if (record) out.push(record);
    }
    return out;
}

/** 合并（后者覆盖同 job 的前者；保持「先出现的位置」顺序，最新值赢）。 */
export function mergeWanFileTasks(...lists: (WanFileTaskRecord[] | undefined)[]): WanFileTaskRecord[] {
    const merged = new Map<string, WanFileTaskRecord>();
    for (const list of lists) {
        for (const record of list || []) {
            if (!record?.job || !record.fileTaskId) continue;
            merged.set(record.job, record);
        }
    }
    return [...merged.values()];
}

/** 登记一条映射（同 job 覆盖；不做条数截断——项目记录不丢，这是 S2 的核心诉求）。 */
export function upsertWanFileTask(records: WanFileTaskRecord[] | undefined, record: WanFileTaskRecord): WanFileTaskRecord[] {
    const list = (records || []).filter((item) => item.job !== record.job);
    return [...list, record];
}

/** 查成片 taskId（查不到返回空串：调用方据此报「成片文件号缺失」，不冒充成功）。 */
export function lookupWanFileTask(records: WanFileTaskRecord[] | undefined, job: string): string {
    if (!job) return "";
    return (records || []).find((item) => item.job === job)?.fileTaskId || "";
}

/** 导入侧校验（形如登记表就透传；未知新版本不静默丢）。 */
export function acceptWanFileTasks(value: unknown): WanFileTaskRecord[] | undefined {
    if (!Array.isArray(value)) return undefined;
    const records = value.map((item) => normalizeWanFileTaskRecord(item)).filter((item): item is WanFileTaskRecord => Boolean(item));
    return records.length ? records : undefined;
}

/**
 * 老 localStorage 载荷（兼容回写）：`{job: fileTaskId}`，按 **200 条**截断（保持旧行为）。
 * 顺序 = 传入顺序的最后 200 条（与旧实现 `[...map.entries()].slice(-200)` 同口径）。
 */
export function toLegacyPayload(records: (WanFileTaskRecord | [string, string])[], limit = WAN_FILE_TASK_LEGACY_LIMIT): Record<string, string> {
    const pairs: [string, string][] = records
        .map((item): [string, string] => (Array.isArray(item) ? [String(item[0] || ""), String(item[1] || "")] : [String(item.job || ""), String(item.fileTaskId || "")]))
        .filter(([job, taskId]) => Boolean(job) && Boolean(taskId));
    const capped = pairs.slice(-Math.max(1, Math.floor(limit)));
    return Object.fromEntries(capped);
}

/** 登记表摘要（探针/报告用：同一组映射可复算。不含任何运行态）。 */
export function wanFileTasksDigest(records: WanFileTaskRecord[] | undefined): string {
    const pairs = (records || [])
        .map((item) => `${item.job}=${item.fileTaskId}`)
        .sort();
    return stableDigest(pairs.join("|"));
}
