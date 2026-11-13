/**
 * Wan 成片映射 ↔ 项目的绑定层（S2，P6）：把「job → 成片 taskId」写进**当前画布项目**。
 *
 * 分工（避免又造一个存储）：
 * - 纯函数在 `wan-file-task-map.ts`（解析/合并/查找/老载荷回写）；
 * - 落库在 `use-canvas-store` 的 `CanvasProject.workflowWanFileTasks`（随项目持久化 + 导出同路）；
 * - 本文件只做「当前项目是哪个 + 写的时候顺手 flush」——页面挂载时 `bindWanFileTaskProject(projectId)`。
 *
 * 为什么不静默降级：没绑定项目（或 store 未水合）时**返回失败原因**，由调用方打警告。
 * 映射丢了 → 成片查不回来 → 等于白花一次生成钱，这种失败不能咽掉。
 */

import { lookupWanFileTask, upsertWanFileTask, type WanFileTaskRecord } from "@/lib/canvas/wan-file-task-map";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";

let boundProjectId = "";

/** 绑定当前项目（页面挂载时调用；返回解绑函数，切项目/卸载时用）。 */
export function bindWanFileTaskProject(projectId: string): () => void {
    boundProjectId = String(projectId || "");
    return () => {
        if (boundProjectId === String(projectId || "")) boundProjectId = "";
    };
}

export function currentWanFileTaskProjectId(): string {
    return boundProjectId;
}

export type WanFileTaskWriteResult = { ok: boolean; reason?: string };

/**
 * 登记一条映射到当前项目，并立即落盘（flush：成片文件号只回一次，防抖窗口崩了就是永久丢失）。
 */
export function rememberProjectWanFileTask(input: { job: string; fileTaskId: string; nodeId?: string; batchId?: string; instanceId?: string; recordedAt?: string }): WanFileTaskWriteResult {
    const job = String(input.job || "").trim();
    const fileTaskId = String(input.fileTaskId || "").trim();
    if (!job || !fileTaskId) return { ok: false, reason: "job / fileTaskId 为空：无可登记内容" };
    if (!boundProjectId) return { ok: false, reason: "没有绑定当前画布项目（bindWanFileTaskProject 未调用）：映射只能留在 localStorage 200 条里" };

    const state = useCanvasStore.getState();
    if (!state.hydrated) return { ok: false, reason: "画布 store 尚未水合：此时写入会被后续 rehydrate 覆盖" };
    const project = state.projects.find((item) => item.id === boundProjectId);
    if (!project) return { ok: false, reason: `当前项目 ${boundProjectId} 不在 store 里（项目可能已被删除）` };

    const record: WanFileTaskRecord = {
        job,
        fileTaskId,
        provider: "wan",
        recordedAt: input.recordedAt || new Date().toISOString(),
        ...(input.nodeId ? { nodeId: input.nodeId } : {}),
        ...(input.batchId ? { batchId: input.batchId } : {}),
        ...(input.instanceId ? { instanceId: input.instanceId } : {}),
    };
    state.updateProject(boundProjectId, { workflowWanFileTasks: upsertWanFileTask(project.workflowWanFileTasks, record) });
    // 立即落盘（不 await：调用点在同步的 create 流程里；flush 结果进 store.lastFlush，失败可见）
    void useCanvasStore.getState().flushProjects("wan-file-task");
    return { ok: true };
}

/** 查当前项目的成片 taskId（查不到返回空串）。 */
export function lookupProjectWanFileTask(job: string): string {
    if (!boundProjectId) return "";
    const project = useCanvasStore.getState().projects.find((item) => item.id === boundProjectId);
    return lookupWanFileTask(project?.workflowWanFileTasks, job);
}

/** 当前项目的整张登记表（探针/报告用；缺省空数组）。 */
export function listProjectWanFileTasks(): WanFileTaskRecord[] {
    if (!boundProjectId) return [];
    const project = useCanvasStore.getState().projects.find((item) => item.id === boundProjectId);
    return project?.workflowWanFileTasks || [];
}

/** job → 成片 taskId 的纯映射视图（给 P4 编排器入册成批次成员外键用）。 */
export function projectWanFileTaskTable(): Map<string, string> {
    return new Map(listProjectWanFileTasks().map((item) => [item.job, item.fileTaskId]));
}
