/**
 * S2-B3：渠道服务端持久化（persist 层改造 + 启动门 + 一次性迁移 + 变更回写）。
 *
 * 三条约束来自任务书 v1.3 §二 B3：
 *  * **B3.1** `channelMode` 复用既有 `"remote"` 语义承载服务端模式（不新建枚举值 = 不动 store 结构）。
 *    登录态来源：bootstrap 期先查 `/api/auth/me`。
 *  * **B3.2** 启动门：渲染前先 await `/api/auth/me` + 服务端 channels，再 hydrate（persist merge 是同步函数、
 *    网络是异步，没有这道门必然闪现本地渠道 → V12）。已登录时 channels 变更 → subscribe + debounce PUT，
 *    beforeunload 用 keepalive fetch flush。
 *  * **B3.3** 一次性迁移：登录成功后先 GET —— 服务端非空 → 丢弃本地缓存不迁移（服务端权威）；
 *    服务端为空 → 才迁移**用户本地已有、带非空 key、非 wan 型（且服务端能收的 https）**的渠道项；
 *    bootstrap 预置灌入的无 key 渠道绝不上服务端（V6）。迁移后本地键永久降级为缓存。
 *
 * 单人模式（MULTI_USER=0）零改动：探针拿不到登录态、一条 setState 都不做，store 保持 persist 出来的原样。
 */
import { create } from "zustand";

import i18n from "@/i18n";
import { normalizeLapianDefaultModel, normalizeLapianModelList } from "@/lib/canvas/lapian-model";
import { maybeImportBootstrapConfig } from "@/lib/config/bootstrap-config";
import { createModelChannel, modelOptionsFromChannels, normalizeModelOptionValue, useConfigStore, type AiConfig, type ModelChannel } from "@/stores/use-config-store";

const AUTH_ME_ENDPOINT = "/api/auth/me";
const CHANNELS_ENDPOINT = "/api/user/channels";

/** 启动门超时：端点挂了也不能把首屏永久卡死（超时按单人模式放行，零改动）。 */
export const BOOTSTRAP_TIMEOUT_MS = 8000;
/** 渠道变更回写防抖窗口（窗口内连续编辑只发一次全量 PUT）。 */
export const SAVE_DEBOUNCE_MS = 800;

/** 服务端渠道视图（GET/PUT 响应同形）：key 永远只是 masked 或 `***?`，零回显。 */
export type ServerChannel = {
    id: string;
    name: string;
    baseUrl: string;
    apiFormat: string;
    models?: unknown;
    apiKey?: string;
    keyBroken?: boolean;
};

export type SessionProbe = { kind: "logged-in"; username: string } | { kind: "logged-out" } | { kind: "single-user" };

export type ChannelSyncSnapshot = {
    /** loading = 启动门未完成（配置页必须挂 loading，不得用本地陈旧数据完成首次渲染）。 */
    phase: "idle" | "loading" | "ready";
    mode: AiConfig["channelMode"];
    username: string;
    migrating: boolean;
    /** 最近一次加载/迁移/保存的失败文案（空 = 无错）。 */
    error: string;
};

export const useChannelSyncStore = create<ChannelSyncSnapshot>(() => ({ phase: "idle", mode: "local", username: "", migrating: false, error: "" }));

function setSync(patch: Partial<ChannelSyncSnapshot>) {
    useChannelSyncStore.setState(patch);
}

export function channelSyncSnapshot(): ChannelSyncSnapshot {
    return useChannelSyncStore.getState();
}

class TimeoutError extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        const timer = setTimeout(() => reject(new TimeoutError(`${label} 超时（${ms}ms）`)), ms);
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

// ------------------------------------------------------------------ 会话探针

/**
 * 登录态探针。三态区分靠 `/api/auth/me` 的状态码：
 *  * 2xx + username → 已登录（服务端模式）
 *  * 401 → MULTI_USER=1 但未登录（服务端模式登出态：不拉 bootstrap）
 *  * 其它（404 等）→ MULTI_USER=0 单人模式（零改动）
 */
export async function probeSession(fetchImpl: typeof fetch = fetch): Promise<SessionProbe> {
    const response = await fetchImpl(AUTH_ME_ENDPOINT, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (response.ok) {
        const payload = (await response.json().catch(() => null)) as { username?: string } | null;
        return payload?.username ? { kind: "logged-in", username: payload.username } : { kind: "single-user" };
    }
    return response.status === 401 ? { kind: "logged-out" } : { kind: "single-user" };
}

// ------------------------------------------------------------------ 服务端渠道读写

export async function fetchServerChannels(fetchImpl: typeof fetch = fetch): Promise<ServerChannel[]> {
    const response = await fetchImpl(CHANNELS_ENDPOINT, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!response.ok) throw new Error(i18n.t("channelSync.loadFailed", { status: response.status }));
    const payload = (await response.json().catch(() => null)) as { channels?: unknown } | null;
    const list = Array.isArray(payload?.channels) ? payload!.channels : [];
    return list.filter((item): item is ServerChannel => Boolean(item) && typeof item === "object" && typeof (item as ServerChannel).id === "string");
}

function toPutPayload(channels: ModelChannel[]) {
    // 整集全量替换（服务端 PUT 语义）；masked key 原样回写，服务端按 id 沿用原密文
    return channels.map((channel) => ({
        id: channel.id,
        name: channel.name,
        baseUrl: channel.baseUrl,
        apiFormat: channel.apiFormat,
        models: channel.models,
        apiKey: channel.apiKey,
    }));
}

export async function putServerChannels(channels: ModelChannel[], fetchImpl: typeof fetch = fetch): Promise<ServerChannel[]> {
    const response = await fetchImpl(CHANNELS_ENDPOINT, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ channels: toPutPayload(channels) }),
    });
    const payload = (await response.json().catch(() => null)) as { channels?: unknown; error?: string } | null;
    if (!response.ok) throw new Error(payload?.error || i18n.t("channelSync.saveFailed", { status: response.status }));
    const list = Array.isArray(payload?.channels) ? payload!.channels : [];
    return list.filter((item): item is ServerChannel => Boolean(item) && typeof item === "object" && typeof (item as ServerChannel).id === "string");
}

export function toModelChannels(serverChannels: ServerChannel[]): ModelChannel[] {
    return serverChannels.map((channel) =>
        createModelChannel({
            id: channel.id,
            name: channel.name,
            baseUrl: channel.baseUrl,
            apiFormat: channel.apiFormat as ModelChannel["apiFormat"],
            models: Array.isArray(channel.models) ? (channel.models as ModelChannel["models"]) : [],
            apiKey: channel.apiKey || "",
        }),
    );
}

// ------------------------------------------------------------------ 迁移筛选

/**
 * 一次性迁移的候选筛选（V6 的硬规则集中在这一个纯函数里，便于探针直接断言）：
 *  * 必须有 id / name / baseUrl
 *  * 必须带**非空且非 masked** 的 key（bootstrap 预置渠道无 key → 不上服务端）
 *  * 不能是 wan 型（万相同源零 key，服务端 422 拒收，标 localOnly）
 *  * baseUrl 必须 https（服务端 SSRF 收口只接受 https）
 *  * 渠道名唯一（服务端 (user_id,name) 唯一约束，重复整批 422）
 */
export function selectMigratableChannels(channels: ModelChannel[]): ModelChannel[] {
    const names = new Set<string>();
    const result: ModelChannel[] = [];
    for (const channel of channels || []) {
        const id = (channel?.id || "").trim();
        const name = (channel?.name || "").trim();
        const baseUrl = (channel?.baseUrl || "").trim();
        const apiKey = (channel?.apiKey || "").trim();
        if (!id || !name || !baseUrl || !apiKey) continue;
        if (apiKey.startsWith("***")) continue; // 已是 masked 值（缓存的服务端视图），不是明文
        if (channel.apiFormat === "wan") continue;
        if (!/^https:\/\//i.test(baseUrl)) continue;
        if (names.has(name)) continue;
        names.add(name);
        result.push(channel);
    }
    return result;
}

// ------------------------------------------------------------------ store 落地

/**
 * 用服务端渠道重建 config（对齐 persist merge 的归一规则：models 汇总 / 四个默认模型引用 / 拉片引用）。
 * 服务端模式下**清空顶层 apiKey**（那是单渠道兼容字段，留着等于把明文 key 留在 localStorage → V2）。
 */
export function buildConfigWithChannels(current: AiConfig, channels: ModelChannel[], mode: AiConfig["channelMode"]): AiConfig {
    const models = modelOptionsFromChannels(channels);
    const lapianModels = normalizeLapianModelList(current.lapianModels, channels);
    const lapianDefaultModel = normalizeLapianDefaultModel(current.lapianDefaultModel, lapianModels, channels);
    return {
        ...current,
        channelMode: mode,
        channels,
        models,
        baseUrl: channels[0]?.baseUrl || current.baseUrl,
        apiFormat: channels[0]?.apiFormat || current.apiFormat,
        ...(mode === "remote" ? { apiKey: "" } : {}),
        lapianModels,
        lapianDefaultModel,
        model: normalizeModelOptionValue(current.model || current.imageModel, channels),
        imageModel: normalizeModelOptionValue(current.imageModel || current.model, channels),
        videoModel: normalizeModelOptionValue(current.videoModel, channels),
        textModel: normalizeModelOptionValue(current.textModel || current.model, channels),
        audioModel: normalizeModelOptionValue(current.audioModel, channels),
    };
}

let suspendAutoSave = false;
/** 服务端当前已有渠道 id：空 key 的**已有**渠道可以提交（服务端沿用原密文），**新**渠道空 key 必 422 → 跳过。 */
let knownServerIds = new Set<string>();

function rememberServerIds(channels: ServerChannel[]) {
    knownServerIds = new Set(channels.map((channel) => channel.id));
}

/**
 * 分拣可提交渠道：新渠道还没填 key 时提交会让服务端整批 422（B1 masked 回写护栏的硬语义），
 * 那会把「其它渠道的正常改动」一起拖死——所以这种草稿渠道先留在本地，不参与 PUT。
 */
export function splitSavableChannels(channels: ModelChannel[]): { savable: ModelChannel[]; skipped: ModelChannel[] } {
    const savable: ModelChannel[] = [];
    const skipped: ModelChannel[] = [];
    for (const channel of channels || []) {
        const hasKey = Boolean((channel.apiKey || "").trim());
        if (!hasKey && !knownServerIds.has(channel.id)) skipped.push(channel);
        else savable.push(channel);
    }
    return { savable, skipped };
}

function applyChannels(channels: ModelChannel[], mode: AiConfig["channelMode"]) {
    suspendAutoSave = true;
    try {
        useConfigStore.setState((state) => ({ config: buildConfigWithChannels(state.config, channels, mode) }));
    } finally {
        suspendAutoSave = false;
    }
}

// ------------------------------------------------------------------ 变更回写

let debounceMs = SAVE_DEBOUNCE_MS;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let pendingChannels: ModelChannel[] | null = null;
let unsubscribeAutoSave: (() => void) | null = null;
let saveInFlight: Promise<void> | null = null;
let fetchImpl: typeof fetch = fetch;

function scheduleSave(channels: ModelChannel[]) {
    pendingChannels = channels;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
        saveTimer = null;
        void flushChannelSync();
    }, debounceMs);
}

/** 立刻把待写渠道推给服务端（防抖窗口结束 / beforeunload 都会调）。 */
export async function flushChannelSync(): Promise<void> {
    if (!pendingChannels) return;
    if (saveInFlight) {
        await saveInFlight;
        if (!pendingChannels) return;
    }
    const channels = pendingChannels;
    pendingChannels = null;
    const { savable, skipped } = splitSavableChannels(channels);
    if (!savable.length) {
        // 全部是还没填 key 的新草稿渠道：不发 PUT（发了必 422），只提示
        setSync({ error: i18n.t("channelSync.missingKey", { names: skipped.map((channel) => channel.name).join("、") }) });
        return;
    }
    const run = (async () => {
        try {
            const saved = await putServerChannels(savable, fetchImpl);
            rememberServerIds(saved);
            // 回写服务端视图（masked key），未保存的草稿渠道原样留在本地继续编辑
            applyChannels([...toModelChannels(saved), ...skipped], "remote");
            setSync({ error: skipped.length ? i18n.t("channelSync.missingKey", { names: skipped.map((channel) => channel.name).join("、") }) : "" });
        } catch (error) {
            // 失败不丢本地数据：留着 pending 等下次变更/下一次 flush 重试，错误文案给配置页
            pendingChannels = pendingChannels || channels;
            setSync({ error: error instanceof Error ? error.message : String(error) });
        }
    })();
    saveInFlight = run;
    try {
        await run;
    } finally {
        saveInFlight = null;
    }
}

/** 页面卸载时的最后一抖：fetch keepalive（sendBeacon 只能 POST，PUT 必须走 keepalive fetch）。 */
function flushOnUnload() {
    if (!pendingChannels) return;
    const body = JSON.stringify({ channels: toPutPayload(pendingChannels) });
    pendingChannels = null;
    try {
        void fetch(CHANNELS_ENDPOINT, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body,
            keepalive: true,
        });
    } catch {
        // 卸载路径失败只能放弃（本地缓存仍在）
    }
}

export function startChannelAutoSave(options: { debounceMs?: number; fetchImpl?: typeof fetch } = {}) {
    if (typeof window === "undefined") return;
    debounceMs = options.debounceMs ?? SAVE_DEBOUNCE_MS;
    fetchImpl = options.fetchImpl ?? fetch;
    if (unsubscribeAutoSave) return;
    unsubscribeAutoSave = useConfigStore.subscribe((state, previous) => {
        if (suspendAutoSave) return;
        if (state.config.channelMode !== "remote") return;
        if (state.config.channels === previous.config.channels) return;
        scheduleSave(state.config.channels);
    });
    window.addEventListener("beforeunload", flushOnUnload);
}

export function stopChannelAutoSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = null;
    pendingChannels = null;
    unsubscribeAutoSave?.();
    unsubscribeAutoSave = null;
    if (typeof window !== "undefined") window.removeEventListener("beforeunload", flushOnUnload);
}

// ------------------------------------------------------------------ 启动门

export type BootstrapOptions = { timeoutMs?: number; debounceMs?: number; fetchImpl?: typeof fetch };

/**
 * 启动门：渲染前 await 本函数。
 * 返回后 store 已经处在最终模式（remote 或 local），首帧不会闪现本地渠道（V12）。
 */
export async function bootstrapChannelSync(options: BootstrapOptions = {}): Promise<ChannelSyncSnapshot> {
    if (typeof window === "undefined") return channelSyncSnapshot();
    const timeoutMs = options.timeoutMs ?? BOOTSTRAP_TIMEOUT_MS;
    const client = options.fetchImpl ?? fetch;
    setSync({ phase: "loading", mode: "local", username: "", migrating: false, error: "" });

    let probe: SessionProbe;
    try {
        probe = await withTimeout(probeSession(client), timeoutMs, "登录态探测");
    } catch (error) {
        // 探针失败/超时：按单人模式放行（宁可零改动，也不要把首屏卡死或误判成服务端模式）
        console.warn("[channel-sync] 登录态探测失败，按本地模式继续", error);
        setSync({ phase: "ready", mode: "local", username: "" });
        return channelSyncSnapshot();
    }

    if (probe.kind !== "logged-in") {
        // 单人模式：首启预置导入照旧（不 await，保持基线时序）；服务端模式登出态不拉 bootstrap
        if (probe.kind === "single-user") void maybeImportBootstrapConfig();
        setSync({ phase: "ready", mode: "local", username: "" });
        return channelSyncSnapshot();
    }

    // ---- 已登录：服务端权威 ----
    let server: ServerChannel[];
    try {
        server = await withTimeout(fetchServerChannels(client), timeoutMs, "服务端渠道读取");
    } catch (error) {
        // 读不到服务端渠道：不迁移、不动本地数据（避免误清空），退回本地模式并留错误文案
        const message = error instanceof Error ? error.message : String(error);
        console.warn("[channel-sync] 服务端渠道读取失败，保持本地模式", error);
        setSync({ phase: "ready", mode: "local", username: probe.username, error: message });
        return channelSyncSnapshot();
    }

    rememberServerIds(server);
    if (server.length) {
        // B3.3：服务端非空 → 丢弃本地缓存，不迁移
        applyChannels(toModelChannels(server), "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "" });
        startChannelAutoSave({ ...options, fetchImpl: client });
        return channelSyncSnapshot();
    }

    // 服务端为空 → 一次性迁移
    const localChannels = useConfigStore.getState().config.channels;
    const eligible = selectMigratableChannels(localChannels);
    if (!eligible.length) {
        // 没有可迁候选（全是预置无 key 渠道 / wan 型 / http 地址）→ 服务端就是空的
        applyChannels([], "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "" });
        startChannelAutoSave({ ...options, fetchImpl: client });
        return channelSyncSnapshot();
    }

    setSync({ migrating: true });
    try {
        const saved = await withTimeout(putServerChannels(eligible, client), timeoutMs, "渠道迁移");
        rememberServerIds(saved);
        applyChannels(toModelChannels(saved.length ? saved : eligible), "remote");
        setSync({ migrating: false, phase: "ready", mode: "remote", username: probe.username, error: "" });
        console.info(`[channel-sync] 已迁移 ${eligible.length} 个渠道到服务端账号`);
        startChannelAutoSave({ ...options, fetchImpl: client });
    } catch (error) {
        // 迁移失败：绝不把用户本地 key 弄丢 —— 保持本地数据与本地模式（下次登录重试迁移）
        const message = error instanceof Error ? error.message : String(error);
        console.warn("[channel-sync] 渠道迁移失败，保持本地模式", error);
        setSync({ migrating: false, phase: "ready", mode: "local", username: probe.username, error: message });
    }
    return channelSyncSnapshot();
}
