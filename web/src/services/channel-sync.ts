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
import { nanoid } from "nanoid";
import { create } from "zustand";

import i18n from "@/i18n";
import { normalizeLapianDefaultModel, normalizeLapianModelList } from "@/lib/canvas/lapian-model";
import { maybeImportBootstrapConfig } from "@/lib/config/bootstrap-config";
import { channelNameKey, rewriteChannelRefs, serverNameKey } from "@/lib/config/preset-merge";
import {
    isPresetTemplateId,
    presetAliases,
    presetTemplateIdList,
    recordAlias,
    recordTombstone,
    templateIdOfMinted,
} from "@/lib/config/preset-meta";
import { createModelChannel, modelOptionsFromChannels, normalizeModelOptionValue, useConfigStore, type AiConfig, type ModelChannel } from "@/stores/use-config-store";
import { isMaskedKeyValue } from "./api/channel-proxy";

// S3.5：预置层纯函数在 @/lib/config/preset-merge（探针直接 import 该模块断言）。

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
    /** B0：服务端对预置行下发 isPreset 布尔（预置名保留、不可删的判定来源）。 */
    isPreset?: boolean;
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
            isPreset: Boolean(channel.isPreset),
        }),
    );
}

// ------------------------------------------------------------------ （已删除：一次性迁移筛选）

/**
 * 「进不了服务端渠道表」的**内部路由判定**（B3.5 IMPL-3，**不加 store 字段**——store 结构不动）：
 *  * wan 型：万相 CLI bridge 同源零 key，服务端明确 422；
 *  * 无 key：bootstrap 预置渠道 / 还没填 key 的草稿，服务端必 422；
 *  * 有真 key 但非 https：服务端 SSRF 收口只收 https；
 *  * 掩码 key → **不算**（那是服务端渠道的本地缓存视图，服务端权威）。
 * 用在：remote 模式下这些渠道留在本地可见、不进 PUT、模型不进候选，但**不清除**。
 * 这层是门控，用户看不见；用户可见徽标一律走 `channelBadgeFor`（说法是事实陈述，没有「本地模式」概念）。
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
 * 渠道徽标（**只决定用户可见的文案与置灰，不参与任何路由/保存判定**）。
 * 三种说法各自陈述事实，界面上不存在「本地模式」这个概念：
 *  * 未填 Key —— 填了就能用，并且会保存到账号；
 *  * 服务器不可达 —— wan / 非 https，服务端真的访问不到那个地址；
 *  * 脚本渠道 —— 脚本自己带 Key 发请求，服务端不下发 Key。
 */
export type ChannelBadgeKind = "missingKey" | "serverUnreachable" | "script";

export type ChannelBadge = {
    kind: ChannelBadgeKind;
    /** 徽标文字（中性灰，不是警告）。 */
    label: string;
    /** 卡片上的一行事实说明。 */
    hint: string;
};

/**
 * 渠道徽标的展示判定。与 `isLocalOnlyChannel`（门控）**刻意分开**：
 * 门控回答「能不能进 PUT」，这里只回答「该跟用户说哪句实话」。
 */
export function channelBadgeFor(channel: ModelChannel | null | undefined, options: { serverMode?: boolean } = {}): ChannelBadge | null {
    if (!channel) return null;
    const apiKey = (channel.apiKey || "").trim();
    const baseUrl = (channel.baseUrl || "").trim();
    if (channel.apiFormat === "wan") {
        return { kind: "serverUnreachable", label: i18n.t("config.channels.badgeServerUnreachable"), hint: i18n.t("config.channels.badgeWanHint") };
    }
    if (!apiKey) {
        return { kind: "missingKey", label: i18n.t("config.channels.badgeMissingKey"), hint: i18n.t("config.channels.badgeMissingKeyHint") };
    }
    if (baseUrl && !/^https:\/\//i.test(baseUrl)) {
        return { kind: "serverUnreachable", label: i18n.t("config.channels.badgeServerUnreachable"), hint: i18n.t("config.channels.badgeInsecureHint") };
    }
    const hasScript = (channel.models || []).some((model) => Boolean(model?.script?.trim()));
    if (hasScript && options.serverMode) {
        return { kind: "script", label: i18n.t("config.channels.badgeScript"), hint: i18n.t("config.channels.badgeScriptHint") };
    }
    return null;
}

/** 服务端模式下这条渠道的编辑入口是否停用（脚本渠道：服务端拿不到脚本要的 Key）。 */
export function isScriptChannelBadge(badge: ChannelBadge | null): boolean {
    return badge?.kind === "script";
}

/*
 * 【已删除：预置模板层（S3.5 登录态部分）】
 *
 * B0/B1（S3.9）上线后，登录态渠道只来自服务端返回值（GET 原样进 store，见 bootstrapChannelSync），
 * 本地不存渠道、预置不可删——因此登录分支的预置层合并/升格/迁移整层无调用点，B2 批删除：
 *   presetTemplateIds / isTemplateChannelId / presetPayloadVersion / rememberPresetPayload /
 *   presetChannelNames / presetMergeNotice / mergePresetLayer / commitAliasClaims / applyPresetLayer /
 *   commitPresetVersion / mintPromotedOnBoot / selectMigratableChannels / mergeLocalOnlyChannels。
 * 单机版路径仍在用：preset-meta.ts 全文件、preset-layer-complete.ts、maybeImportBootstrapConfig、
 * startPresetWatch（仅保留墓碑分支）。
 */

/*
 * 【已移除：启动门/保存链的「服务端访问不到」点名】unsavableNotice()
 *
 * 这里原来每次 boot 都把 wan 型 / 非 https 渠道点名进 notice，用户删掉万相后它又会被写回来
 * （本地自建渠道每次启动都重新出现在视图里），于是那条黄色提示永远挂着——对「自己建这种渠道、
 * 本来就知道它在干嘛」的用户纯属噪音，实测被体验成恐吓。
 *
 * 现在的口径：这类渠道**不再主动点名**。事实仍然可见但不骚扰——
 *   · 渠道卡片上的「服务器不可达」徽标（channelBadgeFor）只在该渠道被点开时才看到，保留；
 *   · 渠道确实没进 PUT（isLocalOnlyChannel 门控）不变，数据不会丢；
 *   · 用户第一次建/第一次迁移时，卡片本身就是提示（wan 型在卡上写明走本地 bridge）。
 * i18n 的 channelSync.wanUnreachable / insecureUnreachable 文案保留但不再被启动门调用，防外部引用。
 */

/* 【已删除：S3.5 登录态预置层函数（见上文删除清单注释）。 */

function joinNotices(...parts: string[]): string {
    return parts.filter(Boolean).join("；");
}

/**
 * 两处 PUT 入口的防御断言：`id ∈ 模板 id 集合` 一律先 mint 再提交。
 * 迁移分支（selectMigratableChannels → putServerChannels）与 flushChannelSync 都从这里过，
 * 防「模板共享常量 id 进全局主键表」的 409 死循环（任务书 §2.3 第三道防线）。
 */
export function mintTemplateIdsBeforePut(channels: ModelChannel[]): ModelChannel[] {
    if (!presetTemplateIdList().length) return channels;
    const aliases = presetAliases();
    const used = new Set((channels || []).map((channel) => channel.id));
    const aliasMap: Record<string, string> = {};
    let touched = false;
    const next = (channels || []).map((channel) => {
        if (!channel || !isPresetTemplateId(channel.id)) return channel;
        let to = aliases[channel.id];
        if (!to) {
            to = nanoid();
            while (used.has(to)) to = nanoid();
            used.add(to);
            recordAlias(channel.id, to);
        }
        aliasMap[channel.id] = to;
        touched = true;
        return { ...channel, id: to };
    });
    if (!touched) return channels;
    // ① 先把 store 的 channels/引用换新（挂起自动回写防回环）；
    //    必须包进 mintingGuard：store 写入会触发预置 watch，不屏蔽时墓碑分支会把「模板 id 消失」
    //    误判成「用户删除」→ 记下模板 id 墓碑 → 该模板渠道以后永不复活（B2.5 IF5）。
    const previousGuard = mintingGuard;
    mintingGuard = true;
    try {
        applyAliasesToStore(aliasMap);
    } finally {
        mintingGuard = previousGuard;
    }
    // ② 防抖窗口里的待写批次同步换 id（少一层「旧 id 被防御断言再 mint 一次」的耦合，P3）
    remapPendingChannels(aliasMap);
    return next;
}

/** 待写批次里的渠道 id 同步换新（防抖窗口内 mint 时用；pendingChannels 是模块内队列，不是第二个真源）。 */
function remapPendingChannels(aliasMap: Record<string, string>) {
    if (!pendingChannels) return;
    pendingChannels = pendingChannels.map((channel) => (channel && aliasMap[channel.id] ? { ...channel, id: aliasMap[channel.id] } : channel));
}


/** 把 alias 落到 store（channels id + config 七处引用串），期间挂起自动回写防回环。 */
function applyAliasesToStore(aliasMap: Record<string, string>) {
    if (!Object.keys(aliasMap).length) return;
    suspendAutoSave = true;
    try {
        useConfigStore.setState((state) => ({
            config: rewriteChannelRefs({ ...state.config, channels: state.config.channels.map((channel) => (aliasMap[channel.id] ? { ...channel, id: aliasMap[channel.id] } : channel)) }, aliasMap),
        }));
    } finally {
        suspendAutoSave = false;
    }
}

// ---- store 写入边界监听：墓碑记录 + 升格 mint（任务书 §2.3 / §2.5）----

let presetWatchUnsubscribe: (() => void) | null = null;
let mintingGuard = false;

/** 监听渠道集合变化：删模板渠道记墓碑；模板渠道 key 空 → 非空时 mint 换 id。 */
export function startPresetWatch() {
    if (typeof window === "undefined") return;
    if (presetWatchUnsubscribe) return;
    presetWatchUnsubscribe = useConfigStore.subscribe((state, previous) => {
        if (mintingGuard) return;
        if (state.config.channels === previous.config.channels) return;
        const before = previous.config.channels || [];
        const after = state.config.channels || [];
        // ① 墓碑：用户删了 id ∈ 当前模板 id 集合的渠道（升格后的 mintedId 反查模板 id 一并记）
        const remaining = new Set(after.map((channel) => channel.id));
        for (const channel of before) {
            if (!channel?.id || remaining.has(channel.id)) continue;
            if (isPresetTemplateId(channel.id)) recordTombstone(channel.id);
            else {
                const templateId = templateIdOfMinted(channel.id);
                if (templateId) recordTombstone(templateId);
            }
        }
        // 【已删除：升格 mint 分支】B0/B1 后登录态渠道只来自服务端（本地不存渠道、预置不可删），
        // 登录态「模板渠道 key 空→非空即 mint」的升格路径无触发条件，B2 批删除。单机版不需要它
        // （单机渠道不上服务端，无全局主键约束）。
    });
}

export function stopPresetWatch() {
    presetWatchUnsubscribe?.();
    presetWatchUnsubscribe = null;
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
export type SavableChannelSplit = {
    savable: ModelChannel[];
    skipped: ModelChannel[];
    /** trim 后无名的渠道：服务端 (user_id,name) 会整批 422，单条不提交（S3.5 §2.2）。 */
    unnamedSkipped: ModelChannel[];
};

export function splitSavableChannels(channels: ModelChannel[]): SavableChannelSplit {
    const savable: ModelChannel[] = [];
    const skipped: ModelChannel[] = [];
    const unnamedSkipped: ModelChannel[] = [];
    for (const channel of channels || []) {
        // 「仅本地模式」渠道（wan / 无 key / 非 https）永远不进 PUT：服务端会整批 422，拖死合法渠道
        if (isLocalOnlyChannel(channel)) {
            skipped.push(channel);
            continue;
        }
        // S3.5 §2.2：trim 后空名挡在提交前（服务端重名校验会 422 整批回滚）
        if (!channelNameKey(channel?.name)) {
            unnamedSkipped.push(channel);
            continue;
        }
        const hasKey = Boolean((channel.apiKey || "").trim());
        if (!hasKey && !knownServerIds.has(channel.id)) skipped.push(channel);
        else savable.push(channel);
    }
    return { savable, skipped, unnamedSkipped };
}

/**
 * S3.5 §2.2：待提交集内的重名预检。**判重口径 = 服务端 UNIQUE(user_id,name)**（`serverNameKey`：
 * strip 后**完全相等**，大小写敏感——服务端是 SQLite BINARY，`MyChan`/`mychan` 两条都收）。
 *
 * 三种重名后果完全不同，必须分开：
 *  * `duplicates` 纯本地草稿互相重名（整组都不在服务端表里）：单条跳过，不提交也不删任何行；
 *  * `caseVariants` 仅大小写不同：服务端视为不同名，**两条都进提交体**，只进提示文案；
 *  * `conflicts` 重名组里**任一条 id ∈ 服务端已有行**：PUT 是整集全量替换，把重名者剔掉提交
 *    等于**静默删掉服务端那一行**（B2.5 BL②）。这种批次一条都不许提交，整批走错误通道。
 *
 * 独立成函数（而不是塞进 splitSavableChannels 的分拣条件）的原因：分拣只回答
 * 「这条渠道能不能上服务端」，重名是「同一批内部互相撞」——批量层面的判据，
 * 由两个 PUT 入口（flushChannelSync / 卸载回写）显式调用。
 */
export type SavableDedupe = {
    savable: ModelChannel[];
    /** 纯本地草稿重名：单条跳过（不涉及服务端已有行，跳过不删任何行）。 */
    duplicates: ModelChannel[];
    /** 重名且涉及服务端已有行：整批不得提交（否则整集替换会删掉服务端那一行）。 */
    conflicts: ModelChannel[];
    /** 仅大小写不同（服务端 BINARY 照收）：两条都在 savable 里，只进提示。 */
    caseVariants: ModelChannel[];
};

export function dedupeSavableChannels(channels: ModelChannel[], serverIds: Iterable<string> = knownServerIds): SavableDedupe {
    const serverIdSet = serverIds instanceof Set ? serverIds : new Set(serverIds);
    const groups = new Map<string, ModelChannel[]>();
    for (const channel of channels || []) {
        if (!channel) continue;
        const key = serverNameKey(channel.name);
        const bucket = groups.get(key);
        if (bucket) bucket.push(channel);
        else groups.set(key, [channel]);
    }

    const savable: ModelChannel[] = [];
    const duplicates: ModelChannel[] = [];
    const conflicts: ModelChannel[] = [];
    for (const bucket of groups.values()) {
        if (bucket.length === 1) {
            savable.push(bucket[0]);
            continue;
        }
        // 重名组里只要有一条是服务端已有行 → 整批不能提交（剔谁都是删行）
        if (bucket.some((channel) => serverIdSet.has(channel.id))) {
            conflicts.push(...bucket);
            continue;
        }
        savable.push(bucket[0]);
        duplicates.push(...bucket.slice(1));
    }

    // 仅大小写不同的一对对：服务端 BINARY 收得下，两条都在 savable 里，只为提示文案记一笔
    const lowerSeen = new Map<string, ModelChannel[]>();
    for (const channel of savable) {
        const key = serverNameKey(channel.name).toLowerCase();
        const bucket = lowerSeen.get(key);
        if (bucket) bucket.push(channel);
        else lowerSeen.set(key, [channel]);
    }
    const caseVariants: ModelChannel[] = [];
    for (const bucket of lowerSeen.values()) {
        if (bucket.length < 2) continue;
        const keys = new Set(bucket.map((channel) => serverNameKey(channel.name)));
        if (keys.size > 1) caseVariants.push(...bucket);
    }
    return { savable, duplicates, conflicts, caseVariants };
}

/** 重名且涉及服务端已有行：整批未提交的显式报错（说清真实后果：服务端那一行没被删）。 */
export function describeSaveConflict(conflicts: ModelChannel[]): string {
    const names = Array.from(new Set((conflicts || []).map((channel) => channel.name).filter(Boolean)));
    return names.length ? i18n.t("channelSync.duplicateNameBlocked", { names: names.join("、") }) : "";
}

/** 仅大小写不同的渠道：服务端按大小写区分，两条都会保存（只是提示，不是错误）。 */
export function describeCaseVariants(caseVariants: ModelChannel[]): string {
    const names = Array.from(new Set((caseVariants || []).map((channel) => channel.name).filter(Boolean)));
    return names.length ? i18n.t("channelSync.caseVariantNames", { names: names.join("、") }) : "";
}


/** 保存链被跳过渠道的提示：未填 key / 仅本地 / 重名 / 无名各自点名（不许合成一句骗人的「没填 key」）。 */
export function describeSaveSkips(duplicates: ModelChannel[], unnamed: ModelChannel[]): string {
    const parts: string[] = [];
    const dupNames = (duplicates || []).map((channel) => channel.name).filter(Boolean);
    if (dupNames.length) parts.push(i18n.t("channelSync.duplicateName", { names: dupNames.join("、") }));
    if ((unnamed || []).length) parts.push(i18n.t("channelSync.unnamedSkipped", { count: unnamed.length }));
    return parts.join("；");
}

/**
 * 未提交渠道的提示文案：**只点名「还没填 key」**（用户补上就会保存，这条有用）。
 *
 * B2.7：wan 本地地址 / 非 https 两类渠道**不再在这里点名**。它们每次都进不了 PUT，
 * 点名等于每次保存都弹一遍同一句警告；用户自己建这类渠道（万相走本地 bridge）本就知道它在干嘛，
 * 渠道卡片上的「服务器不可达」徽标足以说明事实。i18n 的 wanUnreachable / insecureUnreachable
 * 文案保留但不再被本函数引用（防外部引用）。
 */
export function describeSkippedChannels(skipped: ModelChannel[]): string {
    const list = skipped || [];
    const missingKey = list.filter((channel) => !(channel.apiKey || "").trim()).map((channel) => channel.name).filter(Boolean);
    const parts: string[] = [];
    if (missingKey.length) parts.push(i18n.t("channelSync.missingKey", { names: missingKey.join("、") }));
    return parts.join("；");
}

/** 渠道域字段白名单：只有这些字段的变化才值得写一次 store（V6「config 引用相等」的判据）。 */
const CHANNEL_DOMAIN_FIELDS = ["channelMode", "channels", "models", "baseUrl", "apiFormat", "apiKey", "lapianModels", "lapianDefaultModel", "model", "imageModel", "videoModel", "textModel", "audioModel"] as const;

function channelDomainEqual(a: AiConfig, b: AiConfig): boolean {
    return CHANNEL_DOMAIN_FIELDS.every((field) => JSON.stringify(a[field] ?? null) === JSON.stringify(b[field] ?? null));
}

/**
 * 落地渠道视图。**内容无变化时零写入**（S3.5 V6：版本相同/服务端视图未变 → 不 setState，
 * config 引用不变、subscriber 不触发 → 不会排任何 PUT）。
 * @returns 是否真的写了一次 store
 */
function applyChannels(channels: ModelChannel[], mode: AiConfig["channelMode"], options: { force?: boolean } = {}): boolean {
    const current = useConfigStore.getState().config;
    const next = buildConfigWithChannels(current, channels, mode);
    // B1：force=true 跳过内容等价早退（启动门重放服务端视图时必须真实落一次 store）
    if (!options.force && channelDomainEqual(next, current)) return false;
    suspendAutoSave = true;
    try {
        useConfigStore.setState({ config: next });
    } finally {
        suspendAutoSave = false;
    }
    return true;
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

/** B1：清掉待写渠道（启动门拿到服务端权威视图后调用，避免把启动前的本地残差 PUT 上去）。 */
export function resetPendingChannels() {
    pendingChannels = null;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = null;
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
    const { savable, skipped, unnamedSkipped } = splitSavableChannels(channels);
    const { savable: savableUnique, duplicates, conflicts, caseVariants } = dedupeSavableChannels(savable);
    // 重名且涉及服务端已有行：**整批一条都不发**（PUT 是整集全量替换，剔掉重名者＝删掉服务端那一行）。
    // 本地编辑全部留在 store/pending 里不动，走错误通道点名——零删除 + 显式报错，恢复基线行为。
    const conflictNotice = describeSaveConflict(conflicts);
    if (conflictNotice) {
        pendingChannels = channels;
        setSync({ error: conflictNotice });
        return;
    }
    // 防御断言（任务书 §2.3 第三道防线）：模板共享常量 id 严禁进 PUT —— 先 mint 换 id 再提交
    const guarded = mintTemplateIdsBeforePut(savableUnique);
    const skipNotice = joinNotices(describeSkippedChannels(skipped), describeSaveSkips(duplicates, unnamedSkipped), describeCaseVariants(caseVariants));
    if (!guarded.length) {
        // 全部是仅本地/未填 key/重名的渠道：不发 PUT（发了必 422），只提示
        setSync({ error: skipNotice });
        return;
    }
    const run = (async () => {
        try {
            const saved = await putServerChannels(guarded, fetchImpl);
            rememberServerIds(saved);
            // 服务端视图是权威（B0 起 PUT 会连「未提交的预置行」一起返回），
            // 未保存的草稿/重名/无名渠道按 id 与名称双重去重后再并入，避免预置卡重复出现两次。
            const savedView = toModelChannels(saved);
            const savedIds = new Set(savedView.map((channel) => channel.id));
            const savedNames = new Set(savedView.map((channel) => (channel.name || "").trim()));
            const leftover = [...skipped, ...unnamedSkipped, ...duplicates].filter(
                (channel) => !savedIds.has(channel.id) && !savedNames.has((channel.name || "").trim()),
            );
            applyChannels([...savedView, ...leftover], "remote");
            setSync({ error: skipNotice });
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
 *  * **重名且涉及服务端已有行 → `conflict`**：keepalive 这一发同样会静默删行，宁可不发
 *    （本地缓存还在，下次打开走错误通道点名，用户改名后再保存）；
 *  * 体积超过 keepalive 上限 → `too-large`（连一个合法渠道都没有 → `empty`）。
 */
export function buildUnloadPayload(channels: ModelChannel[]): { ok: true; body: string } | { ok: false; reason: "empty" | "too-large" | "conflict"; bytes: number } {
    const { savable } = splitSavableChannels(channels);
    const { savable: unique, conflicts } = dedupeSavableChannels(savable);
    if (conflicts.length) return { ok: false, reason: "conflict", bytes: 0 };
    // 防御断言（§2.3）：卸载回写同样不许把模板共享 id 送进服务端表
    const guarded = mintTemplateIdsBeforePut(unique);
    if (!guarded.length) return { ok: false, reason: "empty", bytes: 0 };
    const body = JSON.stringify({ channels: toPutPayload(guarded) });
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
        // 单人模式：启动门里**每 boot 一次**预置同步（版本比对 → 零写 / 写盘 + reload，S3.5 V7）。
        // 不 await：保持基线时序（单人 loading 门零改动，async 合并=接受一次闪现）；
        // 预置拉取失败只是「本地照常起」，绝不进 withTimeout 的退 local 失败路径。
        // 登出态不拉 bootstrap（B-09 既有判定不变）。
        if (probe.kind === "single-user") {
            void maybeImportBootstrapConfig(client);
            startPresetWatch();
        }
        setSync({ phase: "ready", mode: "local", username: "" });
        return channelSyncSnapshot();
    }

    // ---- 已登录：服务端权威 ----
    // B1（S3.9）：登录态本地不存渠道——渠道数据只存服务器。直接拉服务端视图落地，
    // 不再做预置层合并 / 本地迁移 / 预置 watch；预置行的 isPreset 由服务端下发（B0）。
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
    applyChannels(toModelChannels(server), "remote", { force: true });
    resetPendingChannels();
    startChannelAutoSave({ ...options, fetchImpl: client });
    setSync({ phase: "ready", mode: "remote", username: probe.username, error: "", notice: "" });
    return channelSyncSnapshot();
}
