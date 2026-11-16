/**
 * 画布域合并语义（S3-B2 从 `services/app-sync.ts` 原样搬出，行为逐字节不变）。
 *
 * 为什么单独成模块：S3-B2 的启动门要与服务端清单合并，而 `app-sync.ts` 又要读「当前是否账号服务端模式」
 * 来决定是否禁用 WebDAV 画布域 —— 若两边互相 import 就是循环依赖。把**纯合并函数**放到无依赖的
 * lib 层，两边都指向**同一份实现**（任务书 §2.1：「复用 app-sync.ts mergeCanvasData 既有语义，不新造合并逻辑」）。
 *
 * 语义（不得改动）：
 *  - 同 id 逐项目按 `updatedAt` 取新，时间相等时**本地优先**；
 *  - 墓碑按 id 取最大 `deletedAt`；`project.updatedAt > deletedAt` 才复活（V3「旧设备 PUT 不复活」的客户端半边）。
 */
import type { CanvasDeletedProject, CanvasProject } from "@/stores/canvas/use-canvas-store";

export type CanvasDomainData = { projects: CanvasProject[]; deleted: CanvasDeletedProject[] };

export function mergeCanvasData(local: CanvasDomainData, remote: CanvasDomainData): CanvasDomainData {
    const localDeleted = local.deleted || [];
    const remoteDeleted = remote.deleted || [];
    const deletedAtById = new Map<string, string>();
    for (const item of [...remoteDeleted, ...localDeleted]) {
        if (!item.id || !item.deletedAt) continue;
        const current = deletedAtById.get(item.id);
        if (!current || item.deletedAt >= current) deletedAtById.set(item.id, item.deletedAt);
    }

    const projects = mergeCanvasById(local.projects || [], remote.projects || [], "updatedAt").filter((project) => {
        const deletedAt = deletedAtById.get(project.id);
        if (!deletedAt) return true;
        if (getCanvasTime(project as Record<string, unknown>, "updatedAt") > Date.parse(deletedAt)) {
            deletedAtById.delete(project.id);
            return true;
        }
        return false;
    });

    return {
        projects,
        deleted: [...deletedAtById.entries()].map(([id, deletedAt]) => ({ id, deletedAt })),
    };
}

export function mergeCanvasById<T extends { id?: string }>(local: T[], remote: T[], timeKey: string) {
    const items = new Map<string, T>();
    remote.forEach((item) => {
        const id = item.id || "";
        if (id) items.set(id, item);
    });
    local.forEach((item) => {
        const id = item.id || "";
        if (!id) return;
        const current = items.get(id);
        if (!current || getCanvasTime(item as Record<string, unknown>, timeKey) >= getCanvasTime(current as Record<string, unknown>, timeKey)) items.set(id, item);
    });
    return Array.from(items.values()).sort((a, b) => getCanvasTime(b as Record<string, unknown>, timeKey) - getCanvasTime(a as Record<string, unknown>, timeKey));
}

export function getCanvasTime(item: Record<string, unknown>, key: string) {
    const value = item[key];
    if (typeof value === "number") return value;
    if (typeof value === "string") return Date.parse(value) || 0;
    return 0;
}
