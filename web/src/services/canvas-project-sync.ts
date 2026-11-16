/**
 * S3-B2：画布项目按用户服务端同步（任务书 `S3-画布数据按用户隔离.md` v1.2 冻结版）。
 *
 * 合同逐条对应
 * ------------
 * §2.1 逐项目 PUT / DELETE 墓碑 / CAS
 *  - `baseUpdatedAt` 用**服务端返回的 version**（ms 域）；409 后**重拉 GET 单项目**再走
 *    `mergeCanvasData`（409 体不含 content，不能就地合并）。
 *  - 时间戳一律经 `canvas-timestamp` 显式转换：store 是 ISO 字符串，服务端是 int ms，**不猜单位**。
 *  - 删除 clamp：`deletedAt = max(now, project.updatedAt)`（store 的 `deleteProjects` 落盘时就做，本模块推送原值）。
 *
 * §2.2 写路径：本地缓存无条件优先 + dirty 补推
 *  - 写穿双写：`storage.setItem` → ① localForage **立写**（键与 `{state,version}` 信封不变）
 *    → ② 按 `project.id` 分桶防抖 PUT（400ms，与 persistQueue 同一口径）。
 *  - `dirtyIds` 落进缓存载荷（`state.dirtyIds`，单人模式不出现）；PUT 失败保留 dirty，
 *    `online` 事件 + 下次启动补推（**不删本地数据、不静默吞错**）。
 *  - 卸载兜底：`visibilitychange:hidden` / `pagehide` 只同步写 localForage + 标 dirty，
 *    **绝不用 keepalive PUT**（keepalive 64KB 上限对 MB 级项目不可用，发了就是静默丢改动）。
 *
 * §2.3 时序：transport indirection + 启动门
 *  - 启动门内：`GET 清单` → `setTransport(remote)` → 用合并结果 prime 基线 → 才开 debounce PUT。
 *  - 启动门返回前 `pushEnabled=false`：**首帧前零远端 PUT**（V5，探针抓请求序断言）。
 *  - 启动门超时/读不到清单 → **不退 local**（那是 BL-2 的丢失路径），进离线态：本地可写 + dirty 记账
 *    + 顶栏可见「离线，改动将在联网后同步」。
 *
 * §2.4 迁移与跨账号
 *  - 缓存载荷带 `owner`（服务端下发的 username）与 `migratedAt`；
 *  - 迁移**仅当本地无 owner**；owner 不一致**绝不自动迁**——隔离到 `…:foreign:<owner>` 并弹窗确认；
 *  - logout 把本账号载荷隔离留档后清空主键（V9：B 登录看到 0 个项目）。
 *
 * §2.5 单人零改动
 *  - 单人/登出/探针拿不到登录态：transport 保持 `localForageStorage`，一个字节都不写额外字段。
 *
 * 审计遗留（B1 复审给 B2 的三条）
 *  - ① `_refuse` 的 401 体可能被 RST 吞掉 → 前端拿到的是**网络错误**，这时按「可能登录过期」处理：
 *       进 dirty 重试，不当作致命错误报死。
 *  - ② 探针补 `ts == deletedAt` 边界（S3-T7）。
 *  - ③ `unit` 只在 GET 清单下发 → 前端**以清单的 `unit` / `serverTime` 为准**（本模块用它做自检与时钟偏差记录）。
 */
import localforage from "localforage";
import { create } from "zustand";

import i18n from "@/i18n";
import { mergeCanvasData } from "@/lib/canvas/canvas-merge";
import type { PersistQueueStorage } from "@/lib/canvas/canvas-persist-queue";
import { getCanvasSyncMode, isRemoteCanvasMode, setCanvasSyncMode, type CanvasSyncMode } from "@/lib/canvas/canvas-sync-mode";
import { inMillisDomain, toIso, toMillis } from "@/lib/canvas/canvas-timestamp";
import { localForageStorage } from "@/lib/localforage-storage";
import { canvasPersistQueue, CANVAS_STORE_KEY, useCanvasStore, type CanvasDeletedProject, type CanvasProject, type PersistedCanvasState, type PersistedCanvasValue } from "@/stores/canvas/use-canvas-store";
import { probeSession, type SessionProbe } from "./channel-sync";

export const PROJECTS_ENDPOINT = "/api/user/projects";
/** 启动门共享预算（与渠道启动门同一个 8s，`main.tsx` 里并行 Promise.all）。 */
export const BOOTSTRAP_TIMEOUT_MS = 8000;
/** 逐项目防抖窗口：与 persistQueue 的 400ms 同一口径（任务书 §2.5「400ms 不动」）。 */
export const PUSH_DEBOUNCE_MS = 400;
/** 启动门里按需拉取的并发上限（清单是 KB 级，单项目 content 可能是 MB 级）。 */
export const CONTENT_CONCURRENCY = 4;
/** 跨账号隔离键前缀（载荷归属不是当前账号时存到这里，绝不自动并入）。 */
export const FOREIGN_CACHE_PREFIX = "infinite-canvas:canvas_store:foreign:";

/** 服务端清单条目（`project_store._meta()` 同形，**不含 content**）。 */
export type ServerProjectMeta = {
    id: string;
    name: string;
    updatedAt: number;
    deleted: boolean;
    deletedAt: number;
    bytes: number;
    version: number;
};

export type ServerProjectList = {
    projects: ServerProjectMeta[];
    owner?: string;
    serverTime?: number;
    unit?: string;
};

export type CanvasImportPrompt = { count: number; fromOwner: string; toOwner: string };

export type CanvasProjectSyncSnapshot = {
    phase: "idle" | "loading" | "ready";
    mode: CanvasSyncMode;
    owner: string;
    /** 待推项目数（顶栏/配置页可见，不静默）。 */
    dirtyCount: number;
    /**
     * 时间戳解析不出来（`toMillis` 返 null）的项目/墓碑数。
     *
     * 这些行**永远不会进 dirty**（`recomputeDirty` 不能给它们编一个时间），旧实现里它们就静默停在
     * 「本地有、云端没有」——用户在顶栏只看到 dirtyCount=0。这个计数是它们的可见入口（③）。
     */
    invalidCount: number;
    /**
     * 解析不出来的隔离归档（`…:foreign:<owner>`）份数（S3-B2.6 ③）。
     *
     * 策略是**保守**的：坏归档**保留不删**（删了就真的没了，用户以后也许能用别的版本/工具救回来），
     * 但绝不允许它静默滞留——旧实现里 `takeQuarantine` 吞掉解析异常、`listPendingForeign` 直接
     * `continue`，结果是「盘上有一份数据，用户永远不知道」。这个计数就是它的可见入口（顶栏据此提示）。
     */
    brokenArchiveCount: number;
    error: string;
    /** 跨账号确认弹窗（用户拍板保留）：非空即必须显式确认，绝不自动迁移。 */
    importPrompt: CanvasImportPrompt | null;
};

export const useCanvasProjectSyncStore = create<CanvasProjectSyncSnapshot>(() => ({
    phase: "idle",
    mode: "local",
    owner: "",
    dirtyCount: 0,
    invalidCount: 0,
    brokenArchiveCount: 0,
    error: "",
    importPrompt: null,
}));

function setSync(patch: Partial<CanvasProjectSyncSnapshot>) {
    useCanvasProjectSyncStore.setState(patch);
}

export function canvasProjectSyncSnapshot(): CanvasProjectSyncSnapshot {
    return useCanvasProjectSyncStore.getState();
}

// ----------------------------------------------------------------- 落盘目标（remote）

/**
 * remote 模式的 localForage 实例。
 *
 * 为什么不用 `localForageStorage`：它在 IndexedDB 不可用时会回落到 `window.localStorage`（5MB 配额）。
 * 账号模式下缓存里可能是 MB 级项目，回落到 5MB 配额等于写一半失败（且失败点随机）——§2.5 明确要求
 * remote 跳过 localStorage 兜底：写不进去就如实报错，由 `lastFlush` 通道可见。
 */
const canvasLocal = localforage.createInstance({ name: "infinite-canvas", storeName: "app_state" });

type CanvasSyncRuntime = {
    /** 启动门是否已完成（未完成时不允许任何远端写/推）。 */
    gateDone: boolean;
    /** 是否允许发远端 PUT/DELETE（V5：启动门内恒 false）。 */
    pushEnabled: boolean;
    owner: string;
    migratedAt: string;
    /** id → 服务端 version(ms)（CAS 的 baseUpdatedAt 就取它）。 */
    serverVersion: Map<string, number>;
    /** id → 服务端 deleted_at(ms)（活行为 0）。 */
    serverTomb: Map<string, number>;
    /** id → 本地待推 revision(ms)。 */
    dirty: Map<string, number>;
    /** 逐项目防抖计时器（按 project.id 分桶）。 */
    timers: Map<string, ReturnType<typeof setTimeout>>;
    /** 409 后已重试过的 id（避免无限对撞）。 */
    conflictRetried: Set<string>;
    /** 跨账号隔离键（弹窗未决时有效）。 */
    quarantineKey: string;
    /** 服务端时钟与本机时钟的偏差（ms，仅观测/留痕，不参与判序）。 */
    serverSkew: number;
    deadline: number;
    listenersBound: boolean;
    fetchImpl: typeof fetch;
};

const runtime: CanvasSyncRuntime = {
    gateDone: false,
    pushEnabled: false,
    owner: "",
    migratedAt: "",
    serverVersion: new Map(),
    serverTomb: new Map(),
    dirty: new Map(),
    timers: new Map(),
    conflictRetried: new Set(),
    quarantineKey: "",
    serverSkew: 0,
    deadline: 0,
    listenersBound: false,
    fetchImpl: typeof fetch === "undefined" ? (undefined as unknown as typeof fetch) : fetch,
};

function clearTimers() {
    for (const timer of runtime.timers.values()) clearTimeout(timer);
    runtime.timers.clear();
}

function publish() {
    const store = useCanvasStore.getState();
    setSync({
        mode: getCanvasSyncMode(),
        owner: runtime.owner,
        dirtyCount: runtime.dirty.size,
        invalidCount: invalidTimestampIds({ projects: store.projects, deletedProjects: store.deletedProjects }).length,
    });
}

function remaining() {
    return Math.max(0, runtime.deadline - Date.now());
}

class GateTimeoutError extends Error {}

function withDeadline<T>(promise: Promise<T>, label: string): Promise<T> {
    const ms = remaining();
    if (ms <= 0) return Promise.reject(new GateTimeoutError(`${label} 超时（启动门预算已耗尽）`));
    return new Promise<T>((resolve, reject) => {
        const timer = setTimeout(() => reject(new GateTimeoutError(`${label} 超时（${ms}ms）`)), ms);
        promise.then(
            (value) => {
                clearTimeout(timer);
                resolve(value);
            },
            (error) => {
                clearTimeout(timer);
                reject(error);
            },
        );
    });
}

function errorText(error: unknown): string {
    return error instanceof Error ? error.message : String(error ?? "");
}

// ----------------------------------------------------------------- 缓存载荷读写

export async function readCanvasCache(): Promise<PersistedCanvasValue | null> {
    const raw = await canvasLocal.getItem<string>(CANVAS_STORE_KEY);
    if (typeof raw !== "string" || !raw) return null;
    try {
        const parsed = JSON.parse(raw) as PersistedCanvasValue;
        if (!parsed || typeof parsed !== "object" || !parsed.state) return null;
        return parsed;
    } catch {
        return null;
    }
}

async function readCanvasCacheRaw(): Promise<string | null> {
    const raw = await canvasLocal.getItem<string>(CANVAS_STORE_KEY);
    return typeof raw === "string" && raw ? raw : null;
}

function emptyState(): PersistedCanvasState {
    return { projects: [], deletedProjects: [] };
}

function localDeleted(state: PersistedCanvasState): CanvasDeletedProject[] {
    return Array.isArray(state.deletedProjects) ? state.deletedProjects : [];
}

function localProjects(state: PersistedCanvasState): CanvasProject[] {
    return Array.isArray(state.projects) ? state.projects : [];
}

// ----------------------------------------------------------------- 服务端接口

export async function fetchProjectList(client: typeof fetch): Promise<ServerProjectList> {
    const response = await client(PROJECTS_ENDPOINT, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!response.ok) throw new Error(i18n.t("canvasSync.loadFailed", { status: response.status }));
    const payload = (await response.json().catch(() => null)) as ServerProjectList | null;
    const projects = Array.isArray(payload?.projects) ? payload!.projects : [];
    return { ...(payload || {}), projects: projects.filter((item): item is ServerProjectMeta => Boolean(item) && typeof (item as ServerProjectMeta).id === "string") };
}

type SingleProject = { meta: ServerProjectMeta; project: CanvasProject | null };

export async function fetchServerProject(client: typeof fetch, id: string): Promise<SingleProject | null> {
    const response = await client(`${PROJECTS_ENDPOINT}/${encodeURIComponent(id)}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(i18n.t("canvasSync.loadFailed", { status: response.status }));
    const payload = (await response.json().catch(() => null)) as { project?: ServerProjectMeta & { content?: unknown } } | null;
    if (!payload?.project) return null;
    const meta = payload.project;
    return { meta, project: toCanvasProject(meta, (payload.project as { content?: unknown }).content) };
}

/** 服务端 content（原样存回来的 CanvasProject）→ store 模型；非法形状返 null（不硬塞脏数据）。 */
export function toCanvasProject(meta: ServerProjectMeta, content: unknown): CanvasProject | null {
    if (!content || typeof content !== "object") return null;
    const candidate = content as Partial<CanvasProject>;
    if (!Array.isArray(candidate.nodes) || !Array.isArray(candidate.connections)) return null;
    return {
        ...(candidate as CanvasProject),
        id: meta.id,
        title: candidate.title || meta.name || "",
        updatedAt: toIso(meta.updatedAt),
    };
}

/** PUT 请求体（纯函数，探针直接断言时间戳域与 CAS 字段）。 */
export function buildProjectPutBody(project: CanvasProject, baseUpdatedAt?: number): { content: CanvasProject; name: string; updatedAt: number; baseUpdatedAt?: number } | null {
    const updatedAt = toMillis(project.updatedAt);
    if (!inMillisDomain(updatedAt)) return null;
    return {
        content: project,
        name: project.title || "",
        updatedAt,
        ...(baseUpdatedAt !== undefined && inMillisDomain(baseUpdatedAt) ? { baseUpdatedAt } : {}),
    };
}

/** DELETE 请求体（墓碑时间来自 store 已 clamp 的 `deletedAt`）。 */
export function buildProjectDeleteBody(deletedAt: string): { deletedAt: number } | null {
    const stamp = toMillis(deletedAt);
    if (!inMillisDomain(stamp)) return null;
    return { deletedAt: stamp };
}

// ----------------------------------------------------------------- dirty 记账

/**
 * 由一份落盘载荷推导 dirty 集合（**唯一口径**）：本地 revision 大于「服务端已知 version」即待推。
 *  - 迁移：服务端没有这行（`serverVersion` 无此项）→ 本地项目必然 dirty；
 *  - 删除：墓碑 `deletedAt` 大于服务端 `deleted_at` → 待推 DELETE；
 *  - 已推成功：服务端 version 已等于本地 revision → 自动出列（不重复推）。
 */
export function recomputeDirty(state: PersistedCanvasState, versions: Map<string, number>, tombs: Map<string, number>): Map<string, number> {
    const next = new Map<string, number>();
    for (const project of localProjects(state)) {
        if (!project?.id) continue;
        const ms = toMillis(project.updatedAt);
        if (ms === null) continue;
        if (ms > (versions.get(project.id) ?? 0)) next.set(project.id, ms);
    }
    for (const tomb of localDeleted(state)) {
        if (!tomb?.id) continue;
        const ms = toMillis(tomb.deletedAt);
        if (ms === null) continue;
        if (ms > (tombs.get(tomb.id) ?? 0)) next.set(tomb.id, Math.max(ms, next.get(tomb.id) ?? 0));
    }
    return next;
}

/**
 * ③：`recomputeDirty` 会跳过时间戳解析不出来的行（不能给它编一个时间），旧实现里这些行就此静默
 * 「本地有、云端没有」。这里把它们的 id 显式列出来，由 `publish()` 记进 snapshot 的 `invalidCount`
 * 交给顶栏可见（不静默、也不擅自改写用户数据的时间戳）。
 */
export function invalidTimestampIds(state: PersistedCanvasState | null | undefined): string[] {
    if (!state) return [];
    const bad: string[] = [];
    for (const project of localProjects(state)) {
        if (project?.id && toMillis(project.updatedAt) === null) bad.push(project.id);
    }
    for (const tomb of localDeleted(state)) {
        if (tomb?.id && toMillis(tomb.deletedAt) === null) bad.push(tomb.id);
    }
    return bad;
}

function syncDirty(state: PersistedCanvasState) {
    const next = recomputeDirty(state, runtime.serverVersion, runtime.serverTomb);
    runtime.dirty.clear();
    next.forEach((value, key) => runtime.dirty.set(key, value));
}

function dirtyRecord(): Record<string, number> {
    const record: Record<string, number> = {};
    runtime.dirty.forEach((value, key) => {
        record[key] = value;
    });
    return record;
}

/**
 * 直接写一份「合并结果」缓存载荷（含 owner/dirtyIds），**不经过队列、不触发 PUT**。
 *
 * 启动门必须走它：prime 基线会清掉队列里的待写值，若合并结果与队列基线相同（isEqual 去重）就没有任何写入，
 * 缓存会停留在「隔离前的旧载荷」或干脆不存在——下次启动就没有本地缓存可读了。
 */
async function writeCanvasCacheNow(version?: number): Promise<PersistedCanvasValue> {
    const store = useCanvasStore.getState();
    const state: PersistedCanvasState = { projects: store.projects, deletedProjects: store.deletedProjects };
    if (runtime.dirty.size) state.dirtyIds = dirtyRecord();
    if (runtime.owner) state.owner = runtime.owner;
    if (runtime.migratedAt) state.migratedAt = runtime.migratedAt;
    const payload: PersistedCanvasValue = { state, version: version ?? 0 };
    await canvasLocal.setItem(CANVAS_STORE_KEY, JSON.stringify(payload));
    return payload;
}

/**
 * 把 dirty/owner 补进缓存载荷并写盘（**不触发任何 PUT**）。
 *
 * 只在「盘上的 projects/deletedProjects 与内存当前状态一致」时改写：否则说明有更新的写入在路上，
 * 那次写入会带新的 dirty 集合，这里再写就会用旧快照盖掉新数据。
 */
async function persistDirtyMarker(): Promise<void> {
    publish();
    if (!runtime.gateDone) return;
    const raw = await readCanvasCacheRaw();
    if (!raw) return;
    let parsed: PersistedCanvasValue;
    try {
        parsed = JSON.parse(raw) as PersistedCanvasValue;
    } catch {
        return;
    }
    const store = useCanvasStore.getState();
    if (!sameCanvasContent(parsed.state, { projects: store.projects, deletedProjects: store.deletedProjects })) return;
    const state: PersistedCanvasState = { ...parsed.state, projects: store.projects, deletedProjects: store.deletedProjects };
    if (runtime.dirty.size) state.dirtyIds = dirtyRecord();
    else delete state.dirtyIds;
    if (runtime.owner) state.owner = runtime.owner;
    if (runtime.migratedAt) state.migratedAt = runtime.migratedAt;
    await canvasLocal.setItem(CANVAS_STORE_KEY, JSON.stringify({ ...parsed, state }));
}

/** projects/deletedProjects 的「同一份」判定（id + 时间戳指纹，不比对整份大对象）。 */
export function sameCanvasContent(a: PersistedCanvasState | null | undefined, b: PersistedCanvasState | null | undefined): boolean {
    if (!a || !b) return false;
    const fingerprint = (state: PersistedCanvasState) =>
        JSON.stringify({
            p: localProjects(state).map((project) => `${project?.id}@${project?.updatedAt}`),
            d: localDeleted(state).map((tomb) => `${tomb?.id}@${tomb?.deletedAt}`),
        });
    return fingerprint(a) === fingerprint(b);
}

// ----------------------------------------------------------------- remote transport（写穿双写）

function enrichPayload(raw: string): { json: string; state: PersistedCanvasState | null } {
    let parsed: PersistedCanvasValue;
    try {
        parsed = JSON.parse(raw) as PersistedCanvasValue;
    } catch {
        return { json: raw, state: null };
    }
    if (!parsed?.state) return { json: raw, state: null };
    syncDirty(parsed.state);
    const state: PersistedCanvasState = { ...parsed.state };
    if (runtime.dirty.size) state.dirtyIds = dirtyRecord();
    else delete state.dirtyIds;
    if (runtime.owner) state.owner = runtime.owner;
    if (runtime.migratedAt) state.migratedAt = runtime.migratedAt;
    return { json: JSON.stringify({ ...parsed, state }), state };
}

/**
 * remote 落盘目标：① localForage 立写 → ② 按项目分桶防抖 PUT。
 *
 * 顺序不能反：先落盘才有「崩溃保护」，远端推送是可以补的（dirty + 下次启动）。
 * `flushProjects()` 的 `{ok,written,bytes}` 契约因此不变（它等的是这一步落盘，不是 PUT）。
 */
export const remoteCanvasStorage: PersistQueueStorage = {
    setItem: async (name: string, value: string) => {
        const { json, state } = enrichPayload(value);
        // ① 立写（不带 localStorage 兜底：见 canvasLocal 的注释）
        await canvasLocal.setItem(name, json);
        if (!state) return;
        // ② 分桶防抖推送
        if (!runtime.pushEnabled) return;
        runtime.dirty.forEach((_ms, id) => schedulePush(id));
    },
};

/** 无落盘目标（logout 用）：任何写都丢弃，保证上一个账号的数据不会再落到共享键上。 */
const sinkCanvasStorage: PersistQueueStorage = { setItem: async () => undefined };

// ----------------------------------------------------------------- 推送

function schedulePush(id: string) {
    if (!runtime.pushEnabled) return;
    const existing = runtime.timers.get(id);
    if (existing) clearTimeout(existing);
    runtime.timers.set(
        id,
        setTimeout(() => {
            runtime.timers.delete(id);
            void pushOne(id);
        }, PUSH_DEBOUNCE_MS),
    );
}

/** 启动时补推：把当前 dirty 集合全部排进逐项目防抖队列（V8）。 */
export function scheduleAllDirty() {
    runtime.dirty.forEach((_ms, id) => schedulePush(id));
}

export async function pushOne(id: string): Promise<void> {
    const state = useCanvasStore.getState();
    const project = state.projects.find((item) => item.id === id);
    const tomb = state.deletedProjects.find((item) => item.id === id);
    if (project) {
        await pushProject(project);
        return;
    }
    if (tomb) {
        await pushDelete(tomb);
        return;
    }
    // 本地已经没有这个项目也没有墓碑（例如被别处清空）→ 无内容可推，出列
    runtime.dirty.delete(id);
    await persistDirtyMarker();
}

async function pushProject(project: CanvasProject): Promise<void> {
    const body = buildProjectPutBody(project, runtime.serverVersion.get(project.id));
    if (!body) {
        setSync({ error: i18n.t("canvasSync.pushFailed", { name: project.title, status: "invalid-timestamp" }) });
        return;
    }
    let response: Response;
    try {
        response = await runtime.fetchImpl(`${PROJECTS_ENDPOINT}/${encodeURIComponent(project.id)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            credentials: "same-origin",
            body: JSON.stringify(body),
        });
    } catch (error) {
        // 顺手①：`_refuse` 的 401 体可能被 RST 吞掉 —— 前端看到的是网络错误。
        // 这时**不能**当成「保存失败」报死：按「可能登录过期」处理，保留 dirty 等补推。
        handlePushFailure(project.id, 0, errorText(error));
        return;
    }
    if (response.status === 409) {
        await resolveConflict(project.id);
        return;
    }
    if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as { error?: string } | null;
        handlePushFailure(project.id, response.status, payload?.error || i18n.t("canvasSync.pushFailed", { name: project.title, status: response.status }));
        return;
    }
    const payload = (await response.json().catch(() => null)) as { project?: ServerProjectMeta } | null;
    const meta = payload?.project;
    if (meta && typeof meta.version === "number") {
        runtime.serverVersion.set(project.id, meta.version);
        runtime.serverTomb.set(project.id, meta.deleted ? meta.deletedAt : 0);
    }
    runtime.dirty.delete(project.id);
    runtime.conflictRetried.delete(project.id);
    setSync({ error: "" });
    await persistDirtyMarker();
}

async function pushDelete(tomb: CanvasDeletedProject): Promise<void> {
    const body = buildProjectDeleteBody(tomb.deletedAt);
    if (!body) {
        setSync({ error: i18n.t("canvasSync.pushFailed", { name: tomb.id, status: "invalid-timestamp" }) });
        return;
    }
    let response: Response;
    try {
        response = await runtime.fetchImpl(`${PROJECTS_ENDPOINT}/${encodeURIComponent(tomb.id)}`, {
            method: "DELETE",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            credentials: "same-origin",
            body: JSON.stringify(body),
        });
    } catch (error) {
        handlePushFailure(tomb.id, 0, errorText(error));
        return;
    }
    // ⑥：DELETE 与 PUT 对齐 —— 409 不是「永久失败」，要按服务端现状判谁更新（见 resolveDeleteConflict）
    if (response.status === 409) {
        await resolveDeleteConflict(tomb);
        return;
    }
    if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as { error?: string } | null;
        handlePushFailure(tomb.id, response.status, payload?.error || i18n.t("canvasSync.pushFailed", { name: tomb.id, status: response.status }));
        return;
    }
    const payload = (await response.json().catch(() => null)) as { project?: ServerProjectMeta } | null;
    const meta = payload?.project;
    runtime.serverVersion.set(tomb.id, meta?.version ?? Date.now());
    runtime.serverTomb.set(tomb.id, meta?.deletedAt ?? toMillis(tomb.deletedAt) ?? 0);
    runtime.dirty.delete(tomb.id);
    setSync({ error: "" });
    await persistDirtyMarker();
}

/**
 * 推送失败：**一律保留 dirty**（不删本地数据、不当作已同步）。
 *
 * 401 / 网络错误（status=0 或 fetch 抛）→ 视为「可能登录过期」（顺手①）：留 dirty 等 `online` / 下次启动补推，
 * 文案说明「改动已留在本地」，不阻断用户继续画。
 * 413 → 走 `lastFlush` 通道（任务书 §2.1：超体积错误在前端可见，不静默）。
 */
function handlePushFailure(id: string, status: number, message: string) {
    runtime.dirty.set(id, runtime.dirty.get(id) ?? Date.now());
    publish();
    if (status === 413) {
        useCanvasStore.setState({ lastFlush: { at: new Date().toISOString(), ok: false, bytes: 0, error: message } });
        return;
    }
    if (status === 401 || status === 0) {
        setSync({ error: i18n.t("canvasSync.sessionMaybeExpired") });
        return;
    }
    setSync({ error: message });
}

/**
 * 409 → **重拉 GET 单项目**再按 `mergeCanvasData` 合并（409 体只有元数据、没有 content）。
 *
 * 合并结果三种走向：
 *  - 服务端胜（更新的改写）：落 store（本地被覆盖是 LWW 的应有语义），清 dirty；
 *  - 本地胜（本项目自己的改动更新，例如时钟/离线补推）：把 baseUpdatedAt 设成服务端 version **重试一次**；
 *  - 服务端 404（这行不在服务端了，例如被别处物理清掉/换账号）：去掉 base 直接重写。
 */
async function resolveConflict(id: string): Promise<void> {
    const local = useCanvasStore.getState().projects.find((item) => item.id === id);
    if (!local) {
        runtime.dirty.delete(id);
        await persistDirtyMarker();
        return;
    }
    let fetched: SingleProject | null = null;
    try {
        fetched = await fetchServerProject(runtime.fetchImpl, id);
    } catch (error) {
        handlePushFailure(id, 0, errorText(error));
        return;
    }
    if (!fetched) {
        runtime.serverVersion.delete(id);
        runtime.serverTomb.delete(id);
        runtime.conflictRetried.add(id);
        await pushProject(local);
        return;
    }
    runtime.serverVersion.set(id, fetched.meta.version);
    runtime.serverTomb.set(id, fetched.meta.deleted ? fetched.meta.deletedAt : 0);
    const remoteProject = fetched.project;
    const remoteTomb: CanvasDeletedProject[] = fetched.meta.deleted ? [{ id, deletedAt: toIso(fetched.meta.deletedAt) }] : [];
    const merged = mergeCanvasData(
        { projects: [local], deleted: useCanvasStore.getState().deletedProjects.filter((item) => item.id === id) },
        { projects: remoteProject ? [remoteProject] : [], deleted: remoteTomb },
    );
    const winner = merged.projects.find((item) => item.id === id) || null;
    if (!winner) {
        // 服务端墓碑胜出：本地项目作废（V3），把墓碑写进 store（服务端已成立，不再推 DELETE）
        useCanvasStore.getState().replaceProjects(
            useCanvasStore.getState().projects.filter((item) => item.id !== id),
            [...useCanvasStore.getState().deletedProjects.filter((item) => item.id !== id), { id, deletedAt: toIso(fetched.meta.deletedAt) }],
        );
        runtime.dirty.delete(id);
        await persistDirtyMarker();
        return;
    }
    // mergeCanvasData 在同 id 时间相等时本地优先 → winner === local 即「本地胜」
    const localWins = winner === local;
    if (!localWins && remoteProject) {
        // 服务端胜：落服务端版本（LWW 的应有语义），清 dirty
        useCanvasStore.getState().replaceProjects(
            useCanvasStore.getState().projects.map((item) => (item.id === id ? remoteProject : item)),
            useCanvasStore.getState().deletedProjects.filter((item) => item.id !== id),
        );
        runtime.dirty.delete(id);
        await persistDirtyMarker();
        return;
    }
    // 本地胜：带服务端 version 重试一次（再冲突就把 dirty 留到下次启动，别打成死循环）
    if (runtime.conflictRetried.has(id)) {
        publish();
        return;
    }
    runtime.conflictRetried.add(id);
    await pushProject(winner);
}

/**
 * DELETE 的 409（⑥「与 PUT 对齐」）：**重拉单项目**看服务端现状，不把 409 当永久失败。
 *
 * 判据（与 `resolveConflict` 同一套 LWW 语义，绝不静默丢删除意愿）：
 *  - 服务端墓碑 ≥ 我们的 `deletedAt`：服务端已成立 → 本地墓碑抬到服务端值，`syncDirty` 重算后**出列**；
 *  - 服务端活行且 `updatedAt > deletedAt`：别处已复活 → 服务端胜（本地墓碑撤掉、项目回来）；
 *  - 我们的墓碑更新（含服务端 404 / 行已不在）：重试 DELETE 一次；再冲突就把 dirty 留到下次启动，不打循环。
 *
 * 为什么用 `syncDirty`（`recomputeDirty`）而不是直接 `dirty.delete`：**未来时间墓碑**（本机时钟快）
 * 只能靠「本地墓碑 ms vs 服务端墓碑 ms」的重算来出列；直接删键会在下一次启动被重算成 dirty →
 * 每次启动重发一次必然 409 的 DELETE。
 */
async function resolveDeleteConflict(tomb: CanvasDeletedProject): Promise<void> {
    const localTombMs = toMillis(tomb.deletedAt) ?? 0;
    let fetched: SingleProject | null = null;
    try {
        fetched = await fetchServerProject(runtime.fetchImpl, tomb.id);
    } catch (error) {
        handlePushFailure(tomb.id, 0, errorText(error));
        return;
    }
    const store = useCanvasStore.getState();
    if (!fetched) {
        // 服务端没有这一行了：DELETE 幂等（无行也回 200，不建幽灵行）→ 本地墓碑即唯一事实，按已同步出列
        runtime.serverVersion.set(tomb.id, Math.max(runtime.serverVersion.get(tomb.id) ?? 0, localTombMs));
        runtime.serverTomb.set(tomb.id, localTombMs);
        syncDirty({ projects: store.projects, deletedProjects: store.deletedProjects });
        publish();
        await persistDirtyMarker();
        return;
    }
    runtime.serverVersion.set(tomb.id, fetched.meta.version);
    runtime.serverTomb.set(tomb.id, fetched.meta.deleted ? fetched.meta.deletedAt : 0);
    const serverTombMs = fetched.meta.deleted ? fetched.meta.deletedAt : 0;
    const serverLiveMs = fetched.meta.deleted ? 0 : fetched.meta.updatedAt;
    if (serverTombMs >= localTombMs || serverLiveMs > localTombMs) {
        // 服务端更新：墓碑抬到服务端时间（墓碑胜）／项目复活（活行胜）——两种都是 LWW 的应有语义
        const alive = !fetched.meta.deleted;
        const nextDeleted = alive
            ? store.deletedProjects.filter((item) => item.id !== tomb.id)
            : [...store.deletedProjects.filter((item) => item.id !== tomb.id), { id: tomb.id, deletedAt: toIso(serverTombMs) }];
        const nextProjects = alive && fetched.project ? [...store.projects.filter((item) => item.id !== tomb.id), fetched.project] : store.projects.filter((item) => item.id !== tomb.id);
        store.replaceProjects(nextProjects, nextDeleted);
        syncDirty({ projects: nextProjects, deletedProjects: nextDeleted });
        setSync({ error: "" });
        publish();
        await persistDirtyMarker();
        return;
    }
    if (runtime.conflictRetried.has(tomb.id)) {
        // 已经重试过一次还是 409：留 dirty 等下次启动补推（不打死循环）
        publish();
        return;
    }
    runtime.conflictRetried.add(tomb.id);
    await pushDelete(tomb);
}

// ----------------------------------------------------------------- 启动门

function resetRuntime(client: typeof fetch, deadline: number) {
    clearTimers();
    runtime.fetchImpl = client;
    runtime.deadline = deadline;
    runtime.pushEnabled = false;
    runtime.gateDone = false;
    runtime.serverVersion.clear();
    runtime.serverTomb.clear();
    runtime.dirty.clear();
    runtime.conflictRetried.clear();
    runtime.quarantineKey = "";
    runtime.serverSkew = 0;
}

function bindListeners() {
    if (runtime.listenersBound || typeof window === "undefined") return;
    runtime.listenersBound = true;
    // 断网时用户一直在画：联网事件 = 补推信号（V8）
    window.addEventListener("online", () => {
        if (!runtime.owner) return;
        void bootstrapCanvasProjectSync({ fetchImpl: runtime.fetchImpl });
    });
    // 卸载兜底：只同步写 localForage + 标 dirty，**不发 keepalive PUT**（64KB 上限对 MB 级项目不可用）
    const flushOnHide = () => {
        if (!isRemoteCanvasMode()) return;
        void canvasPersistQueue.flush("页面隐藏前落盘");
    };
    window.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") flushOnHide();
    });
    window.addEventListener("pagehide", flushOnHide);
}

/** 把缓存里已有的 dirty 标记读进内存（启动门先信盘上那份，合并后再按服务端 version 重算）。 */
function loadDirtyFromCache(cache: PersistedCanvasValue | null) {
    runtime.dirty.clear();
    const record = cache?.state?.dirtyIds;
    if (!record) return;
    for (const [id, value] of Object.entries(record)) {
        const ms = toMillis(value);
        if (ms !== null) runtime.dirty.set(id, ms);
    }
}

/** 跨账号「不再询问」标记键（⑤）。**不以 `FOREIGN_CACHE_PREFIX` 开头**：它不是一份载荷归档。 */
export const FOREIGN_IGNORE_KEY = "infinite-canvas:canvas_store:foreign_ignore";

type ForeignIgnoreRecord = Record<string, string>;

async function readForeignIgnore(): Promise<ForeignIgnoreRecord> {
    const raw = await canvasLocal.getItem<string>(FOREIGN_IGNORE_KEY);
    if (typeof raw !== "string" || !raw) return {};
    try {
        const parsed = JSON.parse(raw) as ForeignIgnoreRecord;
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
        return {};
    }
}

/** ⑤：用户点「不导入」→ 记住「这一对账号不再询问」（隔离载荷继续留档，不删、不上传）。 */
async function markForeignIgnored(fromOwner: string, toOwner: string): Promise<void> {
    if (!fromOwner || !toOwner) return;
    const record = await readForeignIgnore();
    record[`${fromOwner}>${toOwner}`] = new Date().toISOString();
    await canvasLocal.setItem(FOREIGN_IGNORE_KEY, JSON.stringify(record));
}

/**
 * 写隔离留档（**覆盖前先合并旧归档**，BL1②）。
 *
 * 旧实现是 `setItem(key, raw)` 直接覆盖：第二次登出时本次会话的载荷可能比第一次登出的留档更小
 * （例如这次的恢复没成功 / 用户把项目删了 / 只恢复了一部分），一覆盖就把第一次登出留下的完整数据
 * 永久抹掉。这里改成 `mergeCanvasData` 合并 + `dirtyIds` 按 id 取较大值的并集：
 * 留档只有「变多」这一种可能，永不变少。
 *
 * 不可解析的载荷**不覆盖**——「不知道是什么」比「已知的旧归档」更不可信。
 */
async function writeForeignArchive(key: string, raw: string): Promise<void> {
    const previousRaw = await canvasLocal.getItem<string>(key);
    let incoming: PersistedCanvasValue | null = null;
    try {
        incoming = JSON.parse(raw) as PersistedCanvasValue;
    } catch {
        incoming = null;
    }
    if (!incoming?.state) {
        console.warn("[canvas-project-sync] 本次载荷不可解析：保留已有隔离归档，不覆盖", key);
        return;
    }
    if (typeof previousRaw !== "string" || !previousRaw) {
        await canvasLocal.setItem(key, raw);
        return;
    }
    let previous: PersistedCanvasValue | null = null;
    try {
        previous = JSON.parse(previousRaw) as PersistedCanvasValue;
    } catch {
        previous = null;
    }
    if (!previous?.state) {
        // 旧归档读不出来（损坏）：新载荷更可信，直接替换
        await canvasLocal.setItem(key, raw);
        return;
    }
    const merged = mergeCanvasData(
        { projects: localProjects(incoming.state), deleted: localDeleted(incoming.state) },
        { projects: localProjects(previous.state), deleted: localDeleted(previous.state) },
    );
    const dirtyIds: Record<string, number> = { ...(previous.state.dirtyIds || {}) };
    for (const [id, value] of Object.entries(incoming.state.dirtyIds || {})) {
        dirtyIds[id] = Math.max(toMillis(value) ?? 0, toMillis(dirtyIds[id]) ?? 0);
    }
    const state: PersistedCanvasState = { projects: merged.projects, deletedProjects: merged.deleted };
    if (Object.keys(dirtyIds).length) state.dirtyIds = dirtyIds;
    const owner = previous.state.owner || incoming.state.owner;
    if (owner) state.owner = owner;
    const migratedAt = previous.state.migratedAt || incoming.state.migratedAt;
    if (migratedAt) state.migratedAt = migratedAt;
    const version = typeof incoming.version === "number" ? incoming.version : previous.version;
    await canvasLocal.setItem(key, JSON.stringify({ state, ...(typeof version === "number" ? { version } : {}) }));
}

/** 跨账号：把不属于当前账号的载荷挪到隔离键，返回其内容（用于弹窗计数）。**绝不并入当前账号**。 */
async function quarantineForeign(fromOwner: string, raw: string | null): Promise<PersistedCanvasState | null> {
    if (!raw) return null;
    let parsed: PersistedCanvasValue;
    try {
        parsed = JSON.parse(raw) as PersistedCanvasValue;
    } catch {
        return null;
    }
    const key = FOREIGN_CACHE_PREFIX + fromOwner;
    await writeForeignArchive(key, raw);
    await canvasLocal.removeItem(CANVAS_STORE_KEY);
    runtime.quarantineKey = key;
    return parsed.state ?? emptyState();
}

/**
 * 取回**本账号**的登出留档。
 *
 * ③（S3-B2.6）：解析失败时返回 null 但**不清键**（保守：坏归档保留，用户也许能救回来），
 * 并且必须 `console.warn` 留痕——旧实现只在 `catch { return null }` 里静默吞掉，配合同账号分支
 * 的「不弹窗」（`listPendingForeign` 跳过 `fromOwner === ownerName`），坏归档就永久滞留且
 * 无人知晓。可见性由 `scanBrokenArchives()` 汇总进 `sync.brokenArchiveCount`（顶栏提示）。
 */
async function takeQuarantine(ownerName: string): Promise<PersistedCanvasState | null> {
    const key = FOREIGN_CACHE_PREFIX + ownerName;
    const raw = await canvasLocal.getItem<string>(key);
    if (typeof raw !== "string" || !raw) return null;
    try {
        const parsed = JSON.parse(raw) as PersistedCanvasValue;
        return parsed.state ?? null;
    } catch (error) {
        console.warn(`[canvas-project-sync] 本账号留档无法解析（已保留，不删除）：${key}`, errorText(error));
        return null;
    }
}

/**
 * 只读本地：列出所有**解析不出来**的隔离归档键（S3-B2.6 ③）。
 *
 * 不判 owner、不读网络——启动门一进来就能算出来，所以**离线态也可见**（`enterOffline` 之前就已 setSync）。
 * 坏归档的策略是「保留 + 可见」：这里只统计，绝不 removeItem。
 */
async function scanBrokenArchives(): Promise<string[]> {
    let keys: string[] = [];
    try {
        keys = await canvasLocal.keys();
    } catch {
        return [];
    }
    const broken: string[] = [];
    for (const key of keys) {
        if (!key.startsWith(FOREIGN_CACHE_PREFIX)) continue;
        const raw = await canvasLocal.getItem<string>(key);
        if (typeof raw !== "string" || !raw) continue;
        try {
            const parsed = JSON.parse(raw) as PersistedCanvasValue;
            if (typeof parsed !== "object" || !parsed?.state) broken.push(key);
        } catch {
            broken.push(key);
        }
    }
    return broken.sort();
}

/**
 * 扫描隔离区里**属于其他账号**的载荷（登出留档 / 上次弹窗未决），返回全部待确认项。
 *
 * 为什么是**队列**而不是第一个命中就返回（⑤）：同一台机器上可能躺着多个账号的留档
 * （A 登出、C 登出、现在 B 登录）。只取第一条时，用户对第一条点了「不导入」后，第二条永远不会被问到。
 * 这里按 owner 名排序（确定性：`keys()` 的顺序在不同浏览器/IDB 实现下不保证），由启动门取第一条，
 * 用户处理完（接受/拒绝）下一次启动门自然递进到下一条。
 *
 * ③（S3-B2.6）：解析不出来的归档**不删、不弹窗**，但要 `console.warn` 留痕——坏归档的可见性由
 * `scanBrokenArchives()` 统一汇总进 `sync.brokenArchiveCount`（这里只是同一策略的第二处落点，
 * 免得「跳过」变成静默）。
 */
async function listPendingForeign(ownerName: string): Promise<Array<{ fromOwner: string; state: PersistedCanvasState }>> {
    let keys: string[] = [];
    try {
        keys = await canvasLocal.keys();
    } catch {
        return [];
    }
    const ignored = await readForeignIgnore();
    const pending: Array<{ fromOwner: string; state: PersistedCanvasState }> = [];
    for (const key of keys.slice().sort()) {
        if (!key.startsWith(FOREIGN_CACHE_PREFIX)) continue;
        const fromOwner = key.slice(FOREIGN_CACHE_PREFIX.length);
        if (!fromOwner || fromOwner === ownerName) continue;
        if (ignored[`${fromOwner}>${ownerName}`]) continue;
        const raw = await canvasLocal.getItem<string>(key);
        if (typeof raw !== "string" || !raw) continue;
        try {
            const parsed = JSON.parse(raw) as PersistedCanvasValue;
            const state = parsed.state ?? emptyState();
            if (!localProjects(state).length && !localDeleted(state).length) continue;
            pending.push({ fromOwner, state });
        } catch (error) {
            console.warn(`[canvas-project-sync] 隔离归档无法解析，跳过弹窗但保留在盘上：${key}`, errorText(error));
            continue;
        }
    }
    return pending;
}

async function dropQuarantine(key: string) {
    if (!key) return;
    await canvasLocal.removeItem(key);
    if (runtime.quarantineKey === key) runtime.quarantineKey = "";
}

/** 合并本地（缓存）与服务端清单；需要时按需拉单项目 content（并发 4，受启动门预算约束）。 */
export async function mergeWithServer(local: PersistedCanvasState, list: ServerProjectList, client: typeof fetch): Promise<PersistedCanvasState> {
    const localById = new Map(localProjects(local).map((project) => [project.id, project]));
    const dirtyIds = new Set(runtime.dirty.keys());
    const needContent = list.projects.filter((meta) => {
        if (meta.deleted) return false;
        if (dirtyIds.has(meta.id)) return false; // dirty 本地优先：不去拉服务端版本盖掉本地改动
        const mine = localById.get(meta.id);
        if (!mine) return true;
        return (toMillis(mine.updatedAt) ?? 0) < meta.updatedAt;
    });

    const fetched: CanvasProject[] = [];
    for (let index = 0; index < needContent.length; index += CONTENT_CONCURRENCY) {
        const slice = needContent.slice(index, index + CONTENT_CONCURRENCY);
        if (remaining() <= 0) {
            console.warn(`[canvas-project-sync] 启动门预算耗尽，剩余 ${needContent.length - index} 个项目的 content 留到下次启动再拉`);
            break;
        }
        const results = await Promise.all(
            slice.map(async (meta) => {
                try {
                    const single = await withDeadline(fetchServerProject(client, meta.id), "项目内容读取");
                    return single?.project ?? null;
                } catch (error) {
                    console.warn(`[canvas-project-sync] 读取项目 ${meta.id} 失败（保留本地版本）`, error);
                    return null;
                }
            }),
        );
        results.forEach((item) => {
            if (item) fetched.push(item);
        });
    }

    const remoteProjects: CanvasProject[] = [...fetched];
    const remoteDeleted: CanvasDeletedProject[] = [];
    for (const meta of list.projects) {
        if (meta.deleted) remoteDeleted.push({ id: meta.id, deletedAt: toIso(meta.deletedAt) });
    }

    const merged = mergeCanvasData({ projects: localProjects(local), deleted: localDeleted(local) }, { projects: remoteProjects, deleted: remoteDeleted });

    // dirty 本地优先（§2.2「dirty 本地优先补推；非 dirty 服务端为准」）：合并再把这些项目扳回本地版本
    for (const id of dirtyIds) {
        const mine = localById.get(id);
        if (!mine) continue;
        const index = merged.projects.findIndex((item) => item.id === id);
        if (index >= 0) merged.projects[index] = mine;
        else merged.projects.push(mine);
        merged.deleted = merged.deleted.filter((tomb) => tomb.id !== id);
    }
    return { projects: merged.projects, deletedProjects: merged.deleted };
}

/**
 * 启动门：渲染前 await 本函数（`main.tsx` 里与渠道启动门并行，共享 8s 预算）。
 *
 * 返回后 store 已处于最终状态：remote（合并结果已落盘、dirty 已排推）／offline（本地可写 + 待补推）／local（零改动）。
 */
export async function bootstrapCanvasProjectSync(options: { deadline?: number; fetchImpl?: typeof fetch } = {}): Promise<CanvasProjectSyncSnapshot> {
    if (typeof window === "undefined") return canvasProjectSyncSnapshot();
    const client = options.fetchImpl ?? fetch;
    const deadline = options.deadline ?? Date.now() + BOOTSTRAP_TIMEOUT_MS;
    /**
     * BL2（S3-B2.5）：启动门的 baseline 必须是**内存 ∪ 盘上**，不能只取盘上缓存。
     *
     * `online` 事件会重跑本函数，而用户此时可能正在画：set 已入 store、但还在 400ms 落盘防抖窗口里
     * （盘上还是旧值）。旧实现只 `readCanvasCache()` 当 baseline，随后 `replaceProjects(merged)` +
     * `rehydrate(primed)` 把旧值盖回内存并清掉队列 —— 在途编辑就地蒸发（内存没了、盘上也没了）。
     *
     * 这里按 `mergeCanvasData` 把两份合起来（同 id 取 `updatedAt` 新的、墓碑取 max `deletedAt`）：
     *  - 中途重跑：内存里的在途编辑更新 → 内存胜，不丢；
     *  - 冷启动/重启：内存是空的（store 还没 rehydrate）→ 盘上胜，与基线行为一致。
     * 不在这里 await flush：启动早期内存可能还没 rehydrate 完，flush 会把「空 store」写进盘里（反向丢数据）。
     */
    const memoryAtGate: PersistedCanvasState = { projects: useCanvasStore.getState().projects, deletedProjects: useCanvasStore.getState().deletedProjects };
    resetRuntime(client, deadline);
    bindListeners();
    setSync({ phase: "loading", mode: "local", error: "", importPrompt: null, brokenArchiveCount: 0 });

    /**
     * ③（S3-B2.6）：坏归档的可见性在这里就建立（纯本地、零网络），所以**离线态也看得到**。
     * 策略「保留 + 可见」：只统计并告警，绝不删键。
     */
    const brokenArchives = await scanBrokenArchives();
    if (brokenArchives.length) {
        console.warn(`[canvas-project-sync] 本机有 ${brokenArchives.length} 份隔离归档无法解析（已保留，不删除）：${brokenArchives.join(", ")}`);
        setSync({ brokenArchiveCount: brokenArchives.length });
    }

    const cache = await readCanvasCache();
    const rawCache = await readCanvasCacheRaw();
    const localOwner = cache?.state?.owner || "";
    const diskState: PersistedCanvasState = cache?.state ?? emptyState();
    const unionState = mergeCanvasData(
        { projects: localProjects(diskState), deleted: localDeleted(diskState) },
        { projects: localProjects(memoryAtGate), deleted: localDeleted(memoryAtGate) },
    );
    const gateState: PersistedCanvasState = { projects: unionState.projects, deletedProjects: unionState.deleted };
    loadDirtyFromCache(cache);

    let probe: SessionProbe | null = null;
    let probeFailed = false;
    try {
        probe = await withDeadline(probeSession(client), "登录态探测");
    } catch (error) {
        probeFailed = true;
        console.warn("[canvas-project-sync] 登录态探测失败", error);
    }

    if (!probe || probe.kind !== "logged-in") {
        if (probeFailed && localOwner) {
            // 本机曾登录过（缓存带 owner）但现在问不到会话：不是单人模式，是「问不到」——进离线态，别把账号数据当本地数据
            return enterOffline(i18n.t("canvasSync.gateTimeout"), localOwner);
        }
        // 单人模式 / 登出 / 探活失败且本机无账号痕迹：**零改动**（transport 保持 localForageStorage）
        setCanvasSyncMode("local");
        canvasPersistQueue.setTransport(localForageStorage);
        runtime.gateDone = false;
        runtime.owner = "";
        // 单人模式：零改动（不记账、不提示同步状态；invalidCount 也归零，避免上一次账号会话的残留值）
        setSync({ phase: "ready", mode: "local", owner: "", dirtyCount: 0, invalidCount: 0, brokenArchiveCount: 0, error: "" });
        return canvasProjectSyncSnapshot();
    }

    const ownerName = probe.username;
    runtime.owner = ownerName;

    let list: ServerProjectList;
    try {
        list = await withDeadline(fetchProjectList(client), "项目清单读取");
    } catch (error) {
        // 超时/断网 → 离线态（本地可写 + dirty 补推），**不退 local**（§2.3 BL-2）
        return enterOffline(errorText(error), ownerName);
    }

    // 顺手③：时间戳单位以清单为准（unit 只在清单下发）。不合约就不猜单位，直接离线态 + 明确错误。
    if (list.unit && list.unit !== "ms") {
        return enterOffline(i18n.t("canvasSync.unitMismatch", { unit: list.unit }), ownerName);
    }
    if (typeof list.serverTime === "number") {
        runtime.serverSkew = Date.now() - list.serverTime;
        if (Math.abs(runtime.serverSkew) > 5 * 60 * 1000) {
            console.warn(`[canvas-project-sync] 本机时钟与服务端相差 ${Math.round(runtime.serverSkew / 1000)}s（LWW 判序按客户端时钟，偏差只影响冲突胜率）`);
        }
    }
    for (const meta of list.projects) {
        runtime.serverVersion.set(meta.id, meta.version);
        runtime.serverTomb.set(meta.id, meta.deleted ? meta.deletedAt : 0);
    }

    // ---- 归属判定（§2.4）：本地有 owner 且不一致 → 隔离 + 弹窗，绝不自动迁 ----
    /**
     * BL1①：本账号自己的登出留档**无条件先取回来**。
     *
     * 旧实现在 `localOwner === ownerName` 分支里才 `takeQuarantine`；而 `prepareCanvasLogout()` 会把共享
     * 主键整条删掉 → 下次登录 `localOwner === ""` → 走「本地无 owner → 首次迁移」分支 → 留档**永远不恢复**
     * （且 `listPendingForeign` 会跳过 `fromOwner === ownerName`，连弹窗都不会出现 = 静默丢画布）。
     * 现在改成先读留档（与本机缓存 owner 无关），再按归属分支决定 baseline。
     */
    const ownArchive = await takeQuarantine(ownerName);
    /**
     * ④ + ①（S3-B2.6）：留档键**不在这里删**。
     *
     * 旧实现在这里就 `dropQuarantine`，而此后的 `writeCanvasCacheNow` 可能失败（配额/IDB 故障）→
     * 降级 offline：此时归档已删、共享主键早在登出时就被清空、恢复出来的数据只剩内存 ——
     * 用户一关页就永久丢。删键必须等于「恢复结果**已经落盘**」，即 `writeCanvasCacheNow` 成功之后
     * （见下方 `ownArchiveKey` 的处理）；写盘失败路径保留归档，下次启动还能再恢复一次。
     */
    const ownArchiveKey = ownArchive ? FOREIGN_CACHE_PREFIX + ownerName : "";
    let baseline: PersistedCanvasState = gateState;
    let importPrompt: CanvasImportPrompt | null = null;
    if (localOwner && localOwner !== ownerName) {
        const foreign = await quarantineForeign(localOwner, rawCache);
        // 隔离的载荷**绝不能**留在 baseline 里（无论解析是否成功）：当前账号从空开始
        baseline = emptyState();
        if (foreign) importPrompt = { count: localProjects(foreign).length, fromOwner: localOwner, toOwner: ownerName };
    } else if (!localOwner) {
        // 本机无 owner 标记 → 首次迁移（§2.4「迁移仅当本地无 owner 标记」）：合并后自然 dirty → 上推
        runtime.migratedAt = new Date().toISOString();
    }
    if (ownArchive) {
        // 同账号留档并回（不是跨账号，无需确认）——含登出时未推成功的 dirty 部分
        const mergedRestore = mergeCanvasData(
            { projects: localProjects(baseline), deleted: localDeleted(baseline) },
            { projects: localProjects(ownArchive), deleted: localDeleted(ownArchive) },
        );
        baseline = { projects: mergedRestore.projects, deletedProjects: mergedRestore.deleted };
    }

    if (!importPrompt) {
        // 隔离区里还有别的账号的载荷（上次「不导入」/ 上次登出留档）：继续给确认入口，绝不自动并入
        const pending = await listPendingForeign(ownerName);
        const first = pending[0];
        if (first) {
            runtime.quarantineKey = FOREIGN_CACHE_PREFIX + first.fromOwner;
            importPrompt = { count: localProjects(first.state).length, fromOwner: first.fromOwner, toOwner: ownerName };
        }
    }

    const merged = await mergeWithServer(baseline, list, client);

    // ---- 切 transport + prime 基线 + 才开推（§2.3 顺序不可换）----
    setCanvasSyncMode("remote");
    canvasPersistQueue.setTransport(remoteCanvasStorage);
    useCanvasStore.getState().replaceProjects(merged.projects, merged.deletedProjects);
    // 合并结果先落盘 + 记 dirty（**不触发 PUT**：pushEnabled 仍 false）
    syncDirty({ projects: merged.projects, deletedProjects: merged.deletedProjects });
    let primed: PersistedCanvasValue;
    try {
        primed = await writeCanvasCacheNow(cache?.version);
    } catch (error) {
        /**
         * polish②：合并结果写不进盘（配额/IDB 故障）→ **降级离线态**并明示，不假装 remote 正常。
         *
         * 内存里已经有合并结果（用户看得到数据），但落盘失败意味着「下次启动读不到」——
         * 这时继续 `pushEnabled=true` 会给出「正在同步」的假象，而实际写穿第一步就失败。
         * 降级后本地照常可写、顶栏可见错误，`lastFlush` 通道也会带具体错误。
         */
        setCanvasSyncMode("offline");
        runtime.gateDone = true;
        runtime.pushEnabled = false;
        publish();
        setSync({
            phase: "ready",
            mode: "offline",
            owner: ownerName,
            importPrompt,
            error: i18n.t("canvasSync.cacheWriteFailed", { message: errorText(error) }),
        });
        return canvasProjectSyncSnapshot();
    }
    /**
     * ①（S3-B2.6）：恢复结果**已经落盘**（`writeCanvasCacheNow` 成功、上面的 catch 没走），
     * 此刻删留档键才是安全的：盘上有合并结果，键只是冗余副本。
     * 不删的话每次启动都会把同一份旧快照再 merge 一遍（与当前数据冲突时反复改写）——④ 的原意保留。
     */
    if (ownArchiveKey) await dropQuarantine(ownArchiveKey);
    // 用合并结果 prime 基线（契约 §2.3 的顺序：换目标 → prime → 才开推）
    canvasPersistQueue.rehydrate(primed);
    await canvasPersistQueue.flush("S3 启动门合并结果");
    runtime.gateDone = true;
    runtime.pushEnabled = true;
    publish();
    await persistDirtyMarker();
    scheduleAllDirty();
    setSync({ phase: "ready", mode: "remote", owner: ownerName, importPrompt, error: "" });
    return canvasProjectSyncSnapshot();
}

function enterOffline(message: string, ownerName: string): CanvasProjectSyncSnapshot {
    setCanvasSyncMode("offline");
    runtime.owner = ownerName;
    // 本地可写 + dirty 记账（remote transport），但 pushEnabled=false：联网后由 online 事件重跑启动门
    canvasPersistQueue.setTransport(remoteCanvasStorage);
    runtime.gateDone = true;
    runtime.pushEnabled = false;
    publish();
    setSync({ phase: "ready", mode: "offline", owner: ownerName, error: message, importPrompt: null });
    return canvasProjectSyncSnapshot();
}

// ----------------------------------------------------------------- 跨账号弹窗动作

/** 用户点「导入到当前账号」：把隔离载荷并进当前账号并排推（**唯一**的迁移入口，必须显式确认）。 */
export async function acceptForeignCanvasImport(): Promise<void> {
    const prompt = currentImportPrompt();
    if (!prompt) return;
    const key = runtime.quarantineKey || FOREIGN_CACHE_PREFIX + prompt.fromOwner;
    const raw = await canvasLocal.getItem<string>(key);
    let restored: PersistedCanvasState | null = null;
    if (typeof raw === "string" && raw) {
        try {
            restored = (JSON.parse(raw) as PersistedCanvasValue).state ?? null;
        } catch {
            restored = null;
        }
    }
    setSync({ importPrompt: null });
    if (!restored) {
        await dropQuarantine(key);
        return;
    }
    // 并入：服务端没有这些 id → 自然 dirty（迁移载荷含 deletedProjects，墓碑一并上推，防软删复活）
    runtime.migratedAt = new Date().toISOString();
    const merged = mergeCanvasData(
        { projects: useCanvasStore.getState().projects, deleted: useCanvasStore.getState().deletedProjects },
        { projects: localProjects(restored), deleted: localDeleted(restored) },
    );
    useCanvasStore.getState().replaceProjects(merged.projects, merged.deleted);
    syncDirty({ projects: merged.projects, deletedProjects: merged.deleted });
    canvasPersistQueue.rehydrate(await writeCanvasCacheNow());
    await canvasPersistQueue.flush("跨账号导入");
    await persistDirtyMarker();
    scheduleAllDirty();
    await dropQuarantine(key);
}

/** 用户点「不导入」：隔离载荷留档（同账号再登录时自动恢复），当前账号保持干净（V9）。 */
export async function declineForeignCanvasImport(): Promise<void> {
    const prompt = currentImportPrompt();
    setSync({ importPrompt: null });
    if (!prompt) return;
    /**
     * ⑤：记住「这一对账号不再询问」。
     *
     * 隔离载荷**不删**（删了就真的丢了，同账号再登录还要靠它恢复），只是不再每次启动都弹同一个窗
     * （那是噪声，用户已经明确表态过）。`listPendingForeign` 读这个标记跳过已表态的条目，
     * 队列里**其他账号**的留档照旧会被问到。
     */
    await markForeignIgnored(prompt.fromOwner, prompt.toOwner);
}

function currentImportPrompt(): CanvasImportPrompt | null {
    return useCanvasProjectSyncStore.getState().importPrompt;
}

// ----------------------------------------------------------------- logout

/**
 * 登出（§2.4）：把本站账号的载荷**留档到隔离键**，清空共享主键，之后任何写都不落盘。
 *
 * 为什么留档而不是直接删：用户可能在断网/推送未完成时退出，直接删等于把「已画但没推上去」的部分永久丢掉；
 * 留档键按 owner 命名，只有同账号再登录才会被恢复，B 账号永远读不到（V9）。
 */
export async function prepareCanvasLogout(): Promise<void> {
    if (typeof window === "undefined") return;
    const ownerName = runtime.owner;
    clearTimers();
    runtime.pushEnabled = false;
    runtime.gateDone = false;
    if (!ownerName) {
        /**
         * ①（S3-B2.5）：没有 owner 标记 = 单人模式 / 会话还没建立起来。
         *
         * 这时共享主键上就是**本机自己的数据**：删掉它没有任何留档可恢复（`foreign:<owner>` 写不出来，
         * 因为没有 owner），旧实现直接「全量删 + 清空内存」= 一次登出静默抹掉单人用户的全部画布。
         * 这里什么都不动（连 transport 都不换）：下次启动照旧走单人零改动分支。
         */
        setSync({ phase: "ready", mode: getCanvasSyncMode(), owner: "", dirtyCount: 0, error: "", importPrompt: null });
        publish();
        return;
    }
    const raw = await readCanvasCacheRaw();
    // BL1②：留档用「先合并再写」，绝不用一份更小的载荷覆盖上一次登出的完整归档
    if (raw) await writeForeignArchive(FOREIGN_CACHE_PREFIX + ownerName, raw);
    // 先断开 transport 再清键：避免登出后的任何 set 又把数据写回共享键
    canvasPersistQueue.setTransport(sinkCanvasStorage);
    await canvasLocal.removeItem(CANVAS_STORE_KEY);
    runtime.dirty.clear();
    runtime.serverVersion.clear();
    runtime.serverTomb.clear();
    runtime.owner = "";
    setCanvasSyncMode("local");
    setSync({ phase: "ready", mode: "local", owner: "", dirtyCount: 0, error: "", importPrompt: null });
    useCanvasStore.getState().replaceProjects([], []);
}

/** 探针/排障用：当前 runtime 的可观测快照（不暴露可变引用）。 */
export function canvasSyncDiagnostics() {
    return {
        mode: getCanvasSyncMode(),
        gateDone: runtime.gateDone,
        pushEnabled: runtime.pushEnabled,
        owner: runtime.owner,
        dirty: Array.from(runtime.dirty.keys()),
        serverVersion: Object.fromEntries(runtime.serverVersion),
        serverTomb: Object.fromEntries(runtime.serverTomb),
        serverSkew: runtime.serverSkew,
        pendingTimers: runtime.timers.size,
    };
}
