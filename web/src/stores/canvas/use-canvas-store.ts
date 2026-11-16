import { create } from "zustand";
import { persist, type PersistStorage, type StorageValue } from "zustand/middleware";

import { nanoid } from "nanoid";
import i18n from "@/i18n";
import { localForageStorage } from "@/lib/localforage-storage";
import type { CanvasBackgroundMode } from "@/lib/canvas-theme";
import { createPersistQueue, type FlushResult, type PersistQueueStorage } from "@/lib/canvas/canvas-persist-queue";
import { clampDeletedAtIso } from "@/lib/canvas/canvas-timestamp";
import { acceptWorkflowBatches, type WorkflowBatchRecord } from "@/lib/canvas/workflow-batch";
import { acceptWorkflowAssetVetoes, type WorkflowAssetVetoRecord } from "@/lib/canvas/workflow-asset-match";
import { acceptWorkflowInstances } from "@/lib/canvas/workflow-instance-identity";
import { acceptWanFileTasks, type WanFileTaskRecord } from "@/lib/canvas/wan-file-task-map";
import type { WorkflowInstanceIdentityRecord } from "@/lib/canvas/workflow-instance-run";
import type { CanvasWorkflowTemplate } from "@/lib/canvas/workflow-template";
import type { CanvasAssistantSession, CanvasConnection, CanvasNodeData, ViewportTransform } from "@/types/canvas";

export type CanvasProject = {
    id: string;
    title: string;
    createdAt: string;
    updatedAt: string;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    chatSessions: CanvasAssistantSession[];
    activeChatId: string | null;
    backgroundMode: CanvasBackgroundMode;
    showImageInfo: boolean;
    viewport: ViewportTransform;
    /**
     * 工作流模板声明（P2，方案A 批量代办）：五槽参数声明 + 冻结项 + 图外引用报告。
     * 落点为 CanvasProject 显式扩展字段（P0a §3.2）：随项目持久化（IndexedDB）与 v3 导出同路，
     * 不建第二份图 DSL、不进节点 metadata。创建项目时缺省不存在；导入/更新必须透传（逐字段构造会丢未知顶层字段）。
     */
    workflowTemplate?: CanvasWorkflowTemplate;
    /**
     * 实例→hub 身份记录（P4，P0a §5.1）：实例id ↔ hub local_id/bridge_id ↔ 画布节点集（含执行期回填的
     * 产物节点 id）↔ 后端任务号 ↔ 正文修订。随项目持久化/导出同路，只存身份不存状态（§5.3）。
     */
    workflowInstances?: WorkflowInstanceIdentityRecord[];
    /**
     * 批次身份记录（P5，P0a §5.1/§5.3）：batchId ↔ 实例 ↔ 节点骨架 id ↔ hub/后端任务外键。
     * 与 `workflowInstances` 同一种落点（项目显式扩展字段，随项目持久化 + 导出同路），
     * 只存身份不存状态：状态一律由 `workflow-batch-status.ts` 现查派生。
     */
    workflowBatches?: WorkflowBatchRecord[];
    /**
     * Wan 成片映射登记表（S2，P6）：job → 成片 taskId。旧实现只落 `localStorage["wan_video_file_tasks"]`
     * 且仅留 200 条、导出不带；这里是**真源**（随项目持久化 + 随 v3 导出同路），老 localStorage 仍可读。
     */
    workflowWanFileTasks?: WanFileTaskRecord[];
    /**
     * 资产否决记录（P7a）：所有者「一键否决」掉某实例某槽的某张匹配图。
     * 与上面同族落点（项目显式扩展字段：随项目持久化 + 随 v3 导出同路）；它是**人工决策的身份侧事实**
     * （谁在哪一槽拒了哪张图），不是状态位。匹配与实例化都会读它——否决过的旧匹配不得再次进请求。
     */
    workflowAssetVetoes?: WorkflowAssetVetoRecord[];
};

/** 导入侧校验：只要形如模板声明就透传（版本号留给后续迁移，不静默丢未知新版本）。 */
function acceptWorkflowTemplate(value: unknown): CanvasWorkflowTemplate | undefined {
    if (!value || typeof value !== "object") return undefined;
    const candidate = value as Partial<CanvasWorkflowTemplate>;
    if (!candidate.templateId || !Array.isArray(candidate.slots) || !candidate.frozen) return undefined;
    return candidate as CanvasWorkflowTemplate;
}


export type CanvasDeletedProject = {
    id: string;
    deletedAt: string;
};

/** 最近一次显式 flush 的结果（只在内存里，不进持久化载荷——它不是画布数据）。 */
export type CanvasFlushStamp = {
    at: string;
    ok: boolean;
    bytes: number;
    reason?: string;
    error?: string;
};

type CanvasStore = {
    hydrated: boolean;
    projects: CanvasProject[];
    deletedProjects: CanvasDeletedProject[];
    /** 最近一次 flush 结果：失败时 UI/编排层看得见（不静默）。 */
    lastFlush: CanvasFlushStamp | null;
    createProject: (title?: string) => string;
    importProject: (project: Partial<CanvasProject>) => string;
    openProject: (id: string) => CanvasProject | null;
    renameProject: (id: string, title: string) => void;
    deleteProjects: (ids: string[]) => void;
    replaceProjects: (projects: CanvasProject[], deletedProjects?: CanvasDeletedProject[]) => void;
    updateProject: (id: string, patch: Partial<Pick<CanvasProject, "nodes" | "connections" | "chatSessions" | "activeChatId" | "backgroundMode" | "showImageInfo" | "viewport" | "workflowTemplate" | "workflowInstances" | "workflowBatches" | "workflowWanFileTasks" | "workflowAssetVetoes">>) => void;
    /**
     * 立即落盘当前 projects（不等 400ms 防抖窗口）。编排器在实例关键节点（落库/待批）调用；
     * 失败结果同时写进 `lastFlush` 并原样返回，绝不静默吞掉。
     */
    flushProjects: (reason?: string) => Promise<FlushResult>;
};

const initialViewport: ViewportTransform = { x: 0, y: 0, k: 1 };
/** localForage 键：**不可改**（任务书 §2.5；6 个既有 ego 探针直接读写该键）。 */
export const CANVAS_STORE_KEY = "infinite-canvas:canvas_store";
/**
 * 落盘的 state 载荷。
 *
 * S3-B2 在 `state` 内**追加**三个可选字段（`dirtyIds` / `owner` / `migratedAt`），外层 `{state,version}`
 * 信封不变：
 *  - 单人/本地模式这三个字段恒不存在 → 序列化字节与基线逐字节一致（V4 指纹口径）；
 *  - 账号服务端模式由 `services/canvas-project-sync.ts` 的 transport 在落盘时补进去（store 本身不感知远端）。
 */
export type PersistedCanvasState = Pick<CanvasStore, "projects" | "deletedProjects"> & {
    /** 待推服务端：项目 id → 本地 revision（ms）。单人模式不出现。 */
    dirtyIds?: Record<string, number>;
    /** 该缓存载荷归属的账号（服务端下发 username）。单人模式不出现。 */
    owner?: string;
    /** 一次性迁移完成时刻（ISO）。单人模式不出现。 */
    migratedAt?: string;
};
/** 落盘载荷就是 zustand persist 的 StorageValue（`{state, version}`）——不能只存 state，否则恢复时无处可取。 */
export type PersistedCanvasValue = { state: PersistedCanvasState; version?: number };

/**
 * 持久化队列（P5 / 收敛裁决 A8-G14）：set 后 400ms 防抖合并写，`flushProjects` 可立即落盘。
 * 去重口径与改造前一致：projects/deletedProjects 引用都没变 → 不入队（zustand 每次 set 都会交同一份）。
 */
const persistQueue = createPersistQueue<PersistedCanvasValue>({
    name: CANVAS_STORE_KEY,
    storage: localForageStorage,
    delayMs: 400,
    isEqual: (previous, next) => previous?.state?.projects === next?.state?.projects && previous?.state?.deletedProjects === next?.state?.deletedProjects,
    onError: (error) => {
        // 写盘失败可见：lastFlush 不在 partialize 里，所以这次 set 不会把错误本身再写回盘。
        useCanvasStore.setState({ lastFlush: { at: new Date().toISOString(), ok: false, bytes: 0, error: error.message } });
    },
});

/**
 * S3-B2 的 transport indirection 入口（只给 `services/canvas-project-sync.ts` 用）。
 *
 * 为什么不让 store 直接认识远端：单人模式零改动（§2.5）——store 只提供「换目标 / 换基线 / 立即落盘」
 * 三个原子的队列操作，远端语义（清单/合并/dirty/推送）全部留在服务层，local 分支逐字节不变。
 */
export const canvasPersistQueue = {
    setTransport: (storage: PersistQueueStorage) => persistQueue.setTransport(storage),
    /** 启动门：切完 transport 之后用合并结果 prime 基线（此后 store 的 set 才走远端写穿）。 */
    rehydrate: (value: PersistedCanvasValue) => persistQueue.rehydrate(value),
    flush: (reason?: string) => persistQueue.flush(reason),
    pending: () => persistQueue.pending(),
    lastError: () => persistQueue.lastError(),
};

const canvasStorage: PersistStorage<CanvasStore> = {
    getItem: async (name) => {
        const value = await localForageStorage.getItem(name);
        if (!value) return null;
        const parsed = JSON.parse(value) as StorageValue<CanvasStore>;
        // 恢复基线：这份刚从盘上读上来，已知在盘上——不排队、不重写。
        persistQueue.prime({ state: parsed.state as PersistedCanvasState, version: parsed.version });
        return parsed;
    },
    setItem: (name, value) => {
        void name;
        persistQueue.setItem({ state: value.state as PersistedCanvasState, version: value.version });
    },
    removeItem: (name) => localForageStorage.removeItem(name),
};

export const useCanvasStore = create<CanvasStore>()(
    persist(
        (set, get) => ({
            hydrated: false,
            projects: [],
            deletedProjects: [],
            lastFlush: null,
            createProject: (title = i18n.t("canvas.project.untitled")) => {
                const now = new Date().toISOString();
                const id = nanoid();
                const project: CanvasProject = {
                    id,
                    title,
                    createdAt: now,
                    updatedAt: now,
                    nodes: [],
                    connections: [],
                    chatSessions: [],
                    activeChatId: null,
                    backgroundMode: "lines",
                    showImageInfo: false,
                    viewport: initialViewport,
                };
                set((state) => ({ projects: [project, ...state.projects] }));
                return id;
            },
            importProject: (source) => {
                const now = new Date().toISOString();
                const project: CanvasProject = {
                    id: nanoid(),
                    title: source.title || i18n.t("canvas.project.imported"),
                    createdAt: source.createdAt || now,
                    updatedAt: now,
                    nodes: source.nodes || [],
                    connections: source.connections || [],
                    chatSessions: source.chatSessions || [],
                    activeChatId: source.activeChatId || null,
                    backgroundMode: source.backgroundMode || "lines",
                    showImageInfo: source.showImageInfo || false,
                    viewport: source.viewport || initialViewport,
                    // P2：模板声明随项目走（导入逐字段构造，显式透传，否则未知顶层字段会被丢——Codex §3.3）。
                    workflowTemplate: acceptWorkflowTemplate(source.workflowTemplate),
                    // P4：实例身份记录随项目走（导入显式透传，理由同上）。
                    workflowInstances: acceptWorkflowInstances(source.workflowInstances),
                    // P5：批次身份记录随项目走（导入显式透传，理由同上）。
                    workflowBatches: acceptWorkflowBatches(source.workflowBatches),
                    // P6/S2：Wan 成片映射随项目走（导入显式透传，理由同上）。
                    workflowWanFileTasks: acceptWanFileTasks(source.workflowWanFileTasks),
                    // P7a：资产否决记录随项目走（导入显式透传，理由同上：否决不复用必须跨导入/刷新成立）。
                    workflowAssetVetoes: acceptWorkflowAssetVetoes(source.workflowAssetVetoes),
                };
                set((state) => ({ projects: [project, ...state.projects] }));
                return project.id;
            },
            openProject: (id) => {
                return get().projects.find((item) => item.id === id) || null;
            },
            renameProject: (id, title) =>
                set((state) => ({
                    projects: state.projects.map((project) => (project.id === id ? { ...project, title: title.trim() || project.title, updatedAt: new Date().toISOString() } : project)),
                })),
            deleteProjects: (ids) =>
                set((state) => {
                    const now = Date.now();
                    const removing = new Set(ids);
                    const byId = new Map(state.projects.map((project) => [project.id, project]));
                    const projects = state.projects.filter((project) => !removing.has(project.id));
                    // 任务书 §2.1「删除时序 clamp」（v1.2）：墓碑时间必须 ≥ 该项目最后一次改写，
                    // 否则另一台设备在「删除」与「最后一次 PUT」之间的窗口里会把项目复活（V3 的反向漏口）。
                    // 正常时钟下 max(now, updatedAt) == now —— 单人模式取值逐字节不变。
                    const deletedProjects = [
                        ...state.deletedProjects.filter((item) => !removing.has(item.id)),
                        ...ids.map((id) => ({ id, deletedAt: clampDeletedAtIso(byId.get(id)?.updatedAt, now) })),
                    ];
                    return { projects, deletedProjects };
                }),
            replaceProjects: (projects, deletedProjects = []) => set({ projects, deletedProjects }),
            updateProject: (id, patch) =>
                set((state) => ({
                    projects: state.projects.map((project) => (project.id === id ? { ...project, ...patch, updatedAt: new Date().toISOString() } : project)),
                })),
            flushProjects: async (reason) => {
                const result = await persistQueue.flush(reason);
                set({
                    lastFlush: { at: new Date().toISOString(), ok: result.ok, bytes: result.bytes, reason, ...(result.error ? { error: result.error } : {}) },
                });
                return result;
            },
        }),
        {
            name: CANVAS_STORE_KEY,
            storage: canvasStorage,
            partialize: (state) =>
                ({
                    projects: state.projects,
                    deletedProjects: state.deletedProjects,
                }) as StorageValue<CanvasStore>["state"],
            /**
             * S3-B2.5（polish③）：恢复时**只认 store 认识的键**。
             *
             * 账号模式的落盘载荷在 `state` 里追加了 `owner` / `dirtyIds` / `migratedAt` 三个同步元数据。
             * zustand 默认 merge 是 `{...current, ...persisted}` —— 会把这三个键原样灌进 store 状态，
             * 于是 `useCanvasStore.getState().owner` 之类的幽灵字段出现，和 `canvas-project-sync` 的
             * `runtime.owner` 形成两个真源（读的人会误以为 store 知道账号）。
             * 单人模式载荷本来就只有这两个键 → 逐字节/逐键不变（§2.5 零改动）。
             */
            merge: (persisted, current) => {
                const stored = (persisted ?? null) as Partial<PersistedCanvasState> | null;
                return {
                    ...current,
                    projects: Array.isArray(stored?.projects) ? (stored!.projects as CanvasProject[]) : current.projects,
                    deletedProjects: Array.isArray(stored?.deletedProjects) ? (stored!.deletedProjects as CanvasDeletedProject[]) : current.deletedProjects,
                } as CanvasStore;
            },
            onRehydrateStorage: () => () => {
                useCanvasStore.setState({ hydrated: true });
            },
        },
    ),
);
