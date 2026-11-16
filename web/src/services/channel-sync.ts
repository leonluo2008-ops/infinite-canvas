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
import { isMaskedKeyValue } from "./api/channel-proxy";

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
    /** 最近一次迁移的提示（被跳过的渠道清单：wan / 无 key / 非 https / 重名），配置页只读展示。 */
    notice: string;
};

export const useChannelSyncStore = create<ChannelSyncSnapshot>(() => ({ phase: "idle", mode: "local", username: "", migrating: false, error: "", notice: "" }));

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
    const names = new Set<string>();       // 服务端重名判定（大小写敏感口径，与 _replace_channels 一致）
    const lowerNames = new Set<string>();  // 大小写探测（IMPL-4 预检：仅大小写不同的名字在部分库排序规则下会撞 UNIQUE(user_id,name)）
    const result: ModelChannel[] = [];
    for (const channel of channels || []) {
        const id = (channel?.id || "").trim();
        const name = (channel?.name || "").trim();
        const baseUrl = (channel?.baseUrl || "").trim();
        const apiKey = (channel?.apiKey || "").trim();
        if (!id || !name || !baseUrl || !apiKey) continue;
        if (isMaskedKeyValue(apiKey)) {
            // IMPL-4 告警：masked 值被静默过滤过一次，用户很可能以为这条渠道已经「迁过去了」
            console.warn(`[channel-sync] 渠道「${name}」的 API Key 是掩码值（${apiKey.slice(0, 3)}…），不是明文——迁移时跳过，请重新输入完整 API Key`);
            continue;
        }
        if (channel.apiFormat === "wan") continue;
        if (!/^https:\/\//i.test(baseUrl)) continue;
        if (names.has(name)) continue;
        const lower = name.toLowerCase();
        if (lowerNames.has(lower)) {
            // IMPL-4：重名大小写探测（迁移前预检）——重名会让服务端整批 422，先跳过并在结果里点名
            console.warn(`[channel-sync] 渠道「${name}」与已选渠道仅大小写不同，迁移时跳过（服务端重名整批 422）`);
            continue;
        }
        names.add(name);
        lowerNames.add(lower);
        result.push(channel);
    }
    return result;
}

/**
 * 「仅本地模式」渠道的运行时推导（B3.5 IMPL-3，**不加 store 字段**——store 结构不动）：
 *  * wan 型：万相 CLI bridge 同源零 key，服务端明确 422；
 *  * 无 key：bootstrap 预置渠道 / 还没填 key 的草稿，服务端必 422；
 *  * 有真 key 但非 https：服务端 SSRF 收口只收 https；
 *  * 掩码 key → **不是**仅本地（那是服务端渠道的本地缓存视图，服务端权威）。
 * 它们永远进不了服务端渠道表，remote 模式下保留在本地：可见、标「仅本地模式」、不进 PUT、模型不可选，但**不清除**。
 */
export function isLocalOnlyChannel(channel: ModelChannel | null | undefined): boolean {
    if (!channel) return true;
    const apiKey = (channel.apiKey || "").trim();
    if (channel.apiFormat === "wan") return true;
    if (!apiKey) return true;
    if (isMaskedKeyValue(apiKey)) return false;
    return !/^https:\/\//i.test((channel.baseUrl || "").trim());
}

/**
 * 服务端渠道 + 本地专有渠道合并（服务端在前=权威顺序，本地专有附在后面）。
 * id 已在服务端出现的一律以服务端视图为准，避免同一渠道出现两份。
 */
export function mergeLocalOnlyChannels(serverChannels: ModelChannel[], localChannels: ModelChannel[]): ModelChannel[] {
    const ids = new Set(serverChannels.map((channel) => channel.id));
    return [...serverChannels, ...(localChannels || []).filter((channel) => !ids.has(channel.id) && isLocalOnlyChannel(channel))];
}

/** 「仅本地模式」保留渠道的配置页提示（IMPL-3/IMPL-4：迁移后必须让人看见哪些渠道没上去）。 */
function localOnlyNotice(channels: ModelChannel[]): string {
    const names = channels.map((channel) => channel.name).filter(Boolean);
    return names.length ? i18n.t("channelSync.localOnlyKept", { names: names.join("、") }) : "";
}

// ------------------------------------------------------------------ store 落地

/**
 * 用服务端渠道重建 config（对齐 persist merge 的归一规则：models 汇总 / 四个默认模型引用 / 拉片引用）。
 * 服务端模式下**清空顶层 apiKey**（那是单渠道兼容字段，留着等于把明文 key 留在 localStorage → V2）。
 */
export function buildConfigWithChannels(current: AiConfig, channels: ModelChannel[], mode: AiConfig["channelMode"]): AiConfig {
    // 服务端模式下「仅本地模式」渠道（wan / 无 key / 非 https）照旧留在 channels 里可见，但模型不进候选（不可选）
    const pickable = mode === "remote" ? channels.filter((channel) => !isLocalOnlyChannel(channel)) : channels;
    const primary = pickable[0];
    const models = modelOptionsFromChannels(pickable);
    const lapianModels = normalizeLapianModelList(current.lapianModels, channels);
    const lapianDefaultModel = normalizeLapianDefaultModel(current.lapianDefaultModel, lapianModels, channels);
    return {
        ...current,
        channelMode: mode,
        channels,
        models,
        baseUrl: primary?.baseUrl || current.baseUrl,
        apiFormat: primary?.apiFormat || current.apiFormat,
        ...(mode === "remote" ? { apiKey: "" } : {}),
        lapianModels,
        lapianDefaultModel,
        model: normalizeModelOptionValue(current.model || current.imageModel, pickable),
        imageModel: normalizeModelOptionValue(current.imageModel || current.model, pickable),
        videoModel: normalizeModelOptionValue(current.videoModel, pickable),
        textModel: normalizeModelOptionValue(current.textModel || current.model, pickable),
        audioModel: normalizeModelOptionValue(current.audioModel, pickable),
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
        // 「仅本地模式」渠道（wan / 无 key / 非 https）永远不进 PUT：服务端会整批 422，拖死合法渠道
        if (isLocalOnlyChannel(channel)) {
            skipped.push(channel);
            continue;
        }
        const hasKey = Boolean((channel.apiKey || "").trim());
        if (!hasKey && !knownServerIds.has(channel.id)) skipped.push(channel);
        else savable.push(channel);
    }
    return { savable, skipped };
}

/**
 * 未提交渠道的提示文案：区分「还没填 key」（用户要补）与「仅本地模式」（服务端根本不收）。
 * 两条都是配置页可见的提示，不能合成一句骗人的「没填 key」。
 */
export function describeSkippedChannels(skipped: ModelChannel[]): string {
    const missingKey = skipped.filter((channel) => !(channel.apiKey || "").trim()).map((channel) => channel.name).filter(Boolean);
    const localOnly = skipped.filter((channel) => Boolean((channel.apiKey || "").trim())).map((channel) => channel.name).filter(Boolean);
    const parts: string[] = [];
    if (missingKey.length) parts.push(i18n.t("channelSync.missingKey", { names: missingKey.join("、") }));
    if (localOnly.length) parts.push(i18n.t("channelSync.localOnlyKept", { names: localOnly.join("、") }));
    return parts.join("；");
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
        // 全部是仅本地/未填 key 的渠道：不发 PUT（发了必 422），只提示
        setSync({ error: describeSkippedChannels(skipped) });
        return;
    }
    const run = (async () => {
        try {
            const saved = await putServerChannels(savable, fetchImpl);
            rememberServerIds(saved);
            // 回写服务端视图（masked key），未保存的草稿渠道原样留在本地继续编辑
            applyChannels([...toModelChannels(saved), ...skipped], "remote");
            setSync({ error: describeSkippedChannels(skipped) });
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

/**
 * keepalive 请求体上限：浏览器对 keepalive 请求的 body 有约 64KB 的硬上限，超了整个请求直接被拒
 * （静默丢改动）。渠道 models 很大时先判体积，跳过并留痕，别让「有 flush 代码」变成假安心。
 */
export const KEEPALIVE_BODY_LIMIT = 64 * 1024;

function byteLength(text: string): number {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(text).length;
    return text.length; // 兜底：探针/老环境没有 TextEncoder 时按字符数（只会更宽松，不会误判成超限）
}

/**
 * 卸载回写的请求体（纯函数，便于探针断言）：
 *  * 先按 flushChannelSync 同一套规则分拣，未填 key 的新草稿 / 仅本地渠道不进 PUT；
 *  * 体积超过 keepalive 上限 → `too-large`（连一个合法渠道都没有 → `empty`）。
 */
export function buildUnloadPayload(channels: ModelChannel[]): { ok: true; body: string } | { ok: false; reason: "empty" | "too-large"; bytes: number } {
    const { savable } = splitSavableChannels(channels);
    if (!savable.length) return { ok: false, reason: "empty", bytes: 0 };
    const body = JSON.stringify({ channels: toPutPayload(savable) });
    const bytes = byteLength(body);
    if (bytes > KEEPALIVE_BODY_LIMIT) return { ok: false, reason: "too-large", bytes };
    return { ok: true, body };
}

/** 页面卸载时的最后一抖：fetch keepalive（sendBeacon 只能 POST，PUT 必须走 keepalive fetch）。 */
function flushOnUnload() {
    if (!pendingChannels) return;
    const payload = buildUnloadPayload(pendingChannels);
    pendingChannels = null;
    if (!payload.ok) {
        if (payload.reason === "too-large") {
            console.warn(`[channel-sync] 卸载回写体过大（${payload.bytes} 字节 > ${KEEPALIVE_BODY_LIMIT}），已跳过 keepalive 回写；改动仍留在本地，下次打开会重新提交`);
        }
        return;
    }
    try {
        void fetch(CHANNELS_ENDPOINT, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: payload.body,
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
    setSync({ phase: "loading", mode: "local", username: "", migrating: false, error: "", notice: "" });

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
    const localChannels = useConfigStore.getState().config.channels;
    if (server.length) {
        // B3.3：服务端非空 → 丢弃本地缓存（服务端权威、不迁移），但「仅本地模式」渠道（wan / 无 key / 非 https）
        // 永远进不了服务端渠道表，丢掉就再也找不回来 → 合并保留（IMPL-3：可见、标仅本地、不可选、不清除）
        const serverView = toModelChannels(server);
        const merged = mergeLocalOnlyChannels(serverView, localChannels);
        const keptLocalOnly = merged.slice(serverView.length);
        applyChannels(merged, "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "", notice: localOnlyNotice(keptLocalOnly) });
        startChannelAutoSave({ ...options, fetchImpl: client });
        return channelSyncSnapshot();
    }

    // 服务端为空 → 一次性迁移
    const eligible = selectMigratableChannels(localChannels);
    const migratedIds = new Set(eligible.map((channel) => channel.id));
    // 迁移不上去的本地渠道：仅本地口径的留着（可见、不清除），其余（有真 key 的 https 渠道）视为下次再迁
    const retained = localChannels.filter((channel) => !migratedIds.has(channel.id) && isLocalOnlyChannel(channel));
    if (!eligible.length) {
        // 没有可迁候选（全是预置无 key 渠道 / wan 型 / http 地址）→ 服务端就是空的
        applyChannels(retained, "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "", notice: localOnlyNotice(retained) });
        startChannelAutoSave({ ...options, fetchImpl: client });
        return channelSyncSnapshot();
    }

    setSync({ migrating: true });
    try {
        const saved = await withTimeout(putServerChannels(eligible, client), timeoutMs, "渠道迁移");
        rememberServerIds(saved);
        const serverView = toModelChannels(saved.length ? saved : eligible);
        applyChannels([...serverView, ...retained], "remote");
        // IMPL-4：迁移结果列出被跳过渠道清单（配置页可见）
        setSync({ migrating: false, phase: "ready", mode: "remote", username: probe.username, error: "", notice: localOnlyNotice(retained) });
        console.info(`[channel-sync] 已迁移 ${eligible.length} 个渠道到服务端账号${retained.length ? `；仅本地保留：${retained.map((channel) => channel.name).join("、")}` : ""}`);
        startChannelAutoSave({ ...options, fetchImpl: client });
    } catch (error) {
        // 迁移失败：绝不把用户本地 key 弄丢 —— 保持本地数据与本地模式（下次登录重试迁移）
        const message = error instanceof Error ? error.message : String(error);
        console.warn("[channel-sync] 渠道迁移失败，保持本地模式", error);
        setSync({ migrating: false, phase: "ready", mode: "local", username: probe.username, error: message, notice: "" });
    }
    return channelSyncSnapshot();
}
