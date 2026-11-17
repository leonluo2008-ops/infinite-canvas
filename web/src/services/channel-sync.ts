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
import { fetchPresetPayload, maybeImportBootstrapConfig, PRESET_PLACEHOLDER_ID, type BootstrapPresetPayload } from "@/lib/config/bootstrap-config";
import { channelNameKey, hasAnyKey, hasUsableKey, mergePresetChannels, mintPromotedChannels, rewriteChannelRefs, serverNameKey } from "@/lib/config/preset-merge";
import {
    isPresetTemplateId,
    presetAliases,
    presetTemplateIdList,
    readPresetMeta,
    recordAlias,
    recordTombstone,
    setLastPresetVersion,
    setPresetTemplateIdList,
    templateIdOfMinted,
    type PresetMeta,
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

/**
 * 服务端渠道 + 本地专有渠道合并（服务端在前=权威顺序，本地专有附在后面）。
 * id 已在服务端出现的一律以服务端视图为准，避免同一渠道出现两份。
 */
export function mergeLocalOnlyChannels(serverChannels: ModelChannel[], localChannels: ModelChannel[]): ModelChannel[] {
    const ids = new Set(serverChannels.map((channel) => channel.id));
    return [...serverChannels, ...(localChannels || []).filter((channel) => !ids.has(channel.id) && isLocalOnlyChannel(channel))];
}

/**
 * 没同步到账号的渠道提示（IMPL-3/IMPL-4：迁移后必须让人看见哪些渠道没上去）。
 * 只点名**服务端确实访问不到**的两类事实：wan 本地地址 / 非 https。
 * 没填 Key 的渠道不在这里点名——它没丢东西，卡片上的「未填 Key」徽标已经说清了，填了就会保存。
 */
function unsavableNotice(channels: ModelChannel[]): string {
    const names = (list: ModelChannel[]) => list.map((channel) => channel.name).filter(Boolean).join("、");
    const list = channels || [];
    const wan = list.filter((channel) => channel.apiFormat === "wan");
    const insecure = list.filter((channel) => channel.apiFormat !== "wan" && !/^https:\/\//i.test((channel.baseUrl || "").trim()));
    const parts: string[] = [];
    if (wan.length) parts.push(i18n.t("channelSync.wanUnreachable", { names: names(wan) }));
    if (insecure.length) parts.push(i18n.t("channelSync.insecureUnreachable", { names: names(insecure) }));
    return parts.join("；");
}

// ------------------------------------------------------------------ 预置模板层（S3.5）

/** 启动时拉到的预置载荷（每次 boot 一份；失败 null = 本地照常起，不进 withTimeout 失败路径）。 */
let presetPayload: BootstrapPresetPayload | null = null;
/** 当前模板渠道 id 集合（探针/测试读）：真源在 preset-meta（单人/登录两条启动路径共用）。 */
export function presetTemplateIds(): string[] {
    return presetTemplateIdList();
}

/** id 是否属于当前模板（用户删它要记墓碑；填了 key 要升格换 id）。 */
export function isTemplateChannelId(id: string | undefined | null): boolean {
    return isPresetTemplateId(id);
}

/** 最近一次拉到的模板版本（空 = 端点没给版本，按「版本未知」处理）。 */
export function presetPayloadVersion(): string {
    return presetPayload?.presetVersion || "";
}

function rememberPresetPayload(payload: BootstrapPresetPayload | null) {
    presetPayload = payload;
    // 拉取成功才更新模板 id 集合（失败不动上一份，别让一次网络抖动把墓碑/升格判定清空）
    if (payload) setPresetTemplateIdList(payload.channels.map((channel) => channel.id));
}

function presetChannelNames(channels: ModelChannel[], ids: string[]): string {
    const lookup = new Map((channels || []).map((channel) => [channel.id, channel.name]));
    return ids.map((id) => lookup.get(id) || id).filter(Boolean).join("、");
}

/**
 * 预置合并的可见提示（复用 localOnly 的 notice 单通道，配置页只读展示）。
 * `localChannels` 是合并**前**的本地视图——让位的占位渠道已经不在 merged.channels 里，点名要从这里查。
 */
function presetMergeNotice(merged: ReturnType<typeof mergePresetChannels>, preset: ModelChannel[], localChannels: ModelChannel[]): string {
    const parts: string[] = [];
    if (merged.added.length) parts.push(i18n.t("channelSync.presetMerged", { count: merged.added.length, names: presetChannelNames(preset, merged.added) }));
    if (merged.updated.length) parts.push(i18n.t("channelSync.presetRefreshed", { count: merged.updated.length, names: presetChannelNames(preset, merged.updated) }));
    if (merged.skippedSameName.length) parts.push(i18n.t("channelSync.presetSameNameSkipped", { names: presetChannelNames(preset, merged.skippedSameName) }));
    if (merged.skippedEmptyName.length) parts.push(i18n.t("channelSync.presetEmptyNameSkipped", { count: merged.skippedEmptyName.length }));
    if (merged.droppedPlaceholder.length) parts.push(i18n.t("channelSync.presetPlaceholderDropped", { names: presetChannelNames(localChannels, merged.droppedPlaceholder) }));
    return parts.join("；");
}


function joinNotices(...parts: string[]): string {
    return parts.filter(Boolean).join("；");
}

/**
 * 登录模式预置层：`服务端视图 → localOnly → preset 队尾`（任务书 §2.4）。
 * **版本相同 → 零写入**：直接返回原数组（调用方据此跳过 applyChannels，config 引用不变、subscribe 不触发）。
 */
export function mergePresetLayer(channels: ModelChannel[]): { channels: ModelChannel[]; notice: string; changed: boolean; version: string } {
    const payload = presetPayload;
    if (!payload) return { channels, notice: "", changed: false, version: "" };
    const meta: PresetMeta = readPresetMeta();
    if (payload.presetVersion && payload.presetVersion === meta.lastPresetVersion) {
        return { channels, notice: "", changed: false, version: payload.presetVersion };
    }
    const merged = mergePresetChannels(channels, payload.channels, {
        tombstones: meta.tombstones,
        aliases: meta.aliases,
        // 内置占位渠道让位（V1「旧占位消失」）与单人路径**共用同一个回调**——登录模式漏传时
        // 存量浏览器的旧「默认渠道」会永远留在配置页（B2.5 IF2）。
        dropPlaceholder: (channel) => channel.id === PRESET_PLACEHOLDER_ID,
    });
    commitAliasClaims(merged.aliasClaims);
    return { channels: merged.changed ? merged.channels : channels, notice: presetMergeNotice(merged, payload.channels, channels), changed: merged.changed, version: payload.presetVersion };
}

/**
 * 同名跳过分支认领的 alias 落盘（跨设备：设备 B 没有 A 的 alias，靠这条让画布旧引用 lazy 自愈）。
 * 写盘失败不致命（下次启动再认领一次），绝不让它打断启动门。
 */
function commitAliasClaims(claims: Array<{ from: string; to: string }> | undefined) {
    if (!claims || !claims.length) return;
    for (const claim of claims) {
        try {
            recordAlias(claim.from, claim.to);
        } catch {
            // 配额/隐私模式：alias 记不下只会让下次启动再认领一遍（幂等）
        }
    }
}


/**
 * 预置层 + 启动 normalize 升格一起算完：返回最终渠道视图、notice、模板版本。
 * @returns changed=false 时 channels 引用与入参一致（调用方据此零写入）
 */
export function applyPresetLayer(channels: ModelChannel[], startupNoticeText = ""): { channels: ModelChannel[]; notice: string; version: string; changed: boolean } {
    const layer = mergePresetLayer(channels);
    const minted = mintPromotedOnBoot(layer.channels);
    return {
        channels: minted,
        notice: joinNotices(startupNoticeText, layer.notice),
        version: layer.version,
        changed: layer.changed || minted !== layer.channels,
    };
}

/** 模板版本落地：合并路径真正走完（没有退回 local 模式）后才记，写失败不致命。 */
export function commitPresetVersion(version: string) {
    if (!version) return;
    try {
        // 版本没变就不 setItem（V6「零写入」的毛边：内容相同也算一次写盘，配额紧张时白耗）
        if (readPresetMeta().lastPresetVersion === version) return;
        setLastPresetVersion(version);
    } catch {
        // 记不下版本只会让下次启动再评估一遍（幂等无副作用）
    }
}

/**
 * 启动 normalize 的升格（任务书 §2.3）：`templateId ∧ hasKey → 立即 mint`。
 * 只处理**明文** key（masked 是服务端视图缓存，换 id 需服务端行迁移，交给两处 PUT 入口的防御）。
 */
function mintPromotedOnBoot(channels: ModelChannel[]): ModelChannel[] {
    const templateIds = presetTemplateIdList();
    if (!templateIds.length) return channels;
    const { channels: minted, minted: promotions } = mintPromotedChannels(channels, {
        templateIds,
        aliases: presetAliases(),
        mintId: () => nanoid(),
    });
    if (!promotions.length) return channels;
    for (const item of promotions) recordAlias(item.from, item.to);
    return minted;
}

function aliasMapOf(promotions: Array<{ from: string; to: string }>): Record<string, string> {
    const map: Record<string, string> = {};
    for (const item of promotions) map[item.from] = item.to;
    return map;
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
        // ② 升格 mint：只在登录态（有服务端渠道表=有全局主键约束）做
        if (state.config.channelMode !== "remote" || !presetTemplateIdList().length) return;
        const beforeById = new Map(before.map((channel) => [channel.id, channel]));
        const promotions = after.filter((channel) => channel && isPresetTemplateId(channel.id) && hasUsableKey(channel) && !hasAnyKey(beforeById.get(channel.id)));
        if (!promotions.length) return;
        // 推迟一轮：本次 setState 的监听器快照里还有自动回写（它拿的是旧 channels），
        // 同步 setState 会让自动回写用旧模板 id 排一次 PUT。
        const run = () => {
            if (mintingGuard) return;
            const current = useConfigStore.getState().config;
            const { channels: minted, minted: aliases } = mintPromotedChannels(current.channels, {
                templateIds: presetTemplateIdList(),
                aliases: presetAliases(),
                mintId: () => nanoid(),
            });
            if (!aliases.length) return;
            for (const item of aliases) recordAlias(item.from, item.to);
            mintingGuard = true;
            try {
                applyAliasesToStore(aliasMapOf(aliases));
            } finally {
                mintingGuard = false;
            }
            void minted;
        };
        if (typeof queueMicrotask === "function") queueMicrotask(run);
        else setTimeout(run, 0);
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
 * 未提交渠道的提示文案：区分「还没填 key」（用户补上就保存）与「服务端访问不到」
 * （wan 本地地址 / 非 https，补 key 也没用）。
 * 两条都是配置页可见的提示，不能合成一句骗人的「没填 key」。
 */
export function describeSkippedChannels(skipped: ModelChannel[]): string {
    const list = skipped || [];
    const missingKey = list.filter((channel) => !(channel.apiKey || "").trim()).map((channel) => channel.name).filter(Boolean);
    const wan = list.filter((channel) => channel.apiFormat === "wan" && Boolean((channel.apiKey || "").trim())).map((channel) => channel.name).filter(Boolean);
    const insecure = list
        .filter((channel) => channel.apiFormat !== "wan" && Boolean((channel.apiKey || "").trim()) && !/^https:\/\//i.test((channel.baseUrl || "").trim()))
        .map((channel) => channel.name)
        .filter(Boolean);
    const parts: string[] = [];
    if (missingKey.length) parts.push(i18n.t("channelSync.missingKey", { names: missingKey.join("、") }));
    if (wan.length) parts.push(i18n.t("channelSync.wanUnreachable", { names: wan.join("、") }));
    if (insecure.length) parts.push(i18n.t("channelSync.insecureUnreachable", { names: insecure.join("、") }));
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
function applyChannels(channels: ModelChannel[], mode: AiConfig["channelMode"]): boolean {
    const current = useConfigStore.getState().config;
    const next = buildConfigWithChannels(current, channels, mode);
    if (channelDomainEqual(next, current)) return false;
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
            // 回写服务端视图（masked key），未保存的草稿/重名/无名渠道原样留在本地继续编辑
            applyChannels([...toModelChannels(saved), ...skipped, ...unnamedSkipped, ...duplicates], "remote");
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
    // S3.5：预置文件与服务端渠道**并行**拉。presetPromise 永不 reject（失败=没有模板层，
    // 本地照常起），所以它不进 withTimeout 的失败路径——那条路是「渠道链路读不到服务端」的语义。
    const presetPromise = fetchPresetPayload(client);
    let server: ServerChannel[];
    try {
        server = await withTimeout(fetchServerChannels(client), timeoutMs, "服务端渠道读取");
    } catch (error) {
        // 读不到服务端渠道：不迁移、不动本地数据（避免误清空），退回本地模式并留错误文案
        // （此路径**不写** lastPresetVersion——没合并成功就记版本=该浏览器永久错过本次模板）
        const message = error instanceof Error ? error.message : String(error);
        console.warn("[channel-sync] 服务端渠道读取失败，保持本地模式", error);
        setSync({ phase: "ready", mode: "local", username: probe.username, error: message });
        return channelSyncSnapshot();
    }

    rememberServerIds(server);
    rememberPresetPayload(await presetPromise);
    const localChannels = useConfigStore.getState().config.channels;
    if (server.length) {
        // B3.3：服务端非空 → 丢弃本地缓存（服务端权威、不迁移），但「仅本地模式」渠道（wan / 无 key / 非 https）
        // 永远进不了服务端渠道表，丢掉就再也找不回来 → 合并保留（IMPL-3：可见、标仅本地、不可选、不清除）
        const serverView = toModelChannels(server);
        const merged = mergeLocalOnlyChannels(serverView, localChannels);
        const keptLocalOnly = merged.slice(serverView.length);
        // S3.5：预置层追加在最后（服务端视图 → localOnly → preset），版本相同=零写入
        const layer = applyPresetLayer(merged, unsavableNotice(keptLocalOnly));
        applyChannels(layer.channels, "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "", notice: layer.notice });
        startPresetWatch();
        startChannelAutoSave({ ...options, fetchImpl: client });
        commitPresetVersion(layer.version);
        return channelSyncSnapshot();
    }

    // 服务端为空 → 一次性迁移
    const eligible = selectMigratableChannels(localChannels);
    const migratedIds = new Set(eligible.map((channel) => channel.id));
    // 迁移不上去的本地渠道：仅本地口径的留着（可见、不清除），其余（有真 key 的 https 渠道）视为下次再迁
    const retained = localChannels.filter((channel) => !migratedIds.has(channel.id) && isLocalOnlyChannel(channel));
    if (!eligible.length) {
        // 没有可迁候选（全是预置无 key 渠道 / wan 型 / http 地址）→ 服务端就是空的
        const layer = applyPresetLayer(retained, unsavableNotice(retained));
        applyChannels(layer.channels, "remote");
        setSync({ phase: "ready", mode: "remote", username: probe.username, error: "", notice: layer.notice });
        startPresetWatch();
        startChannelAutoSave({ ...options, fetchImpl: client });
        commitPresetVersion(layer.version);
        return channelSyncSnapshot();
    }

    setSync({ migrating: true });
    try {
        // 防御断言（§2.3 第三道防线）：迁移分支绕开了 splitSavableChannels —— 提交前先 mint，
        // 模板共享常量 id 绝不进服务端全局主键表（否则撞 id → 409 整批回滚 → 死循环）。
        const guardedEligible = mintTemplateIdsBeforePut(eligible);
        const saved = await withTimeout(putServerChannels(guardedEligible, client), timeoutMs, "渠道迁移");
        rememberServerIds(saved);
        const serverView = toModelChannels(saved.length ? saved : guardedEligible);
        const layer = applyPresetLayer([...serverView, ...retained], unsavableNotice(retained));
        applyChannels(layer.channels, "remote");
        // IMPL-4：迁移结果列出被跳过渠道清单（配置页可见）
        setSync({ migrating: false, phase: "ready", mode: "remote", username: probe.username, error: "", notice: layer.notice });
        console.info(`[channel-sync] 已迁移 ${guardedEligible.length} 个渠道到服务端账号${retained.length ? `；仅本地保留：${retained.map((channel) => channel.name).join("、")}` : ""}`);
        startPresetWatch();
        startChannelAutoSave({ ...options, fetchImpl: client });
        commitPresetVersion(layer.version);
    } catch (error) {
        // 迁移失败：绝不把用户本地 key 弄丢 —— 保持本地数据与本地模式（下次登录重试迁移）
        const message = error instanceof Error ? error.message : String(error);
        console.warn("[channel-sync] 渠道迁移失败，保持本地模式", error);
        setSync({ migrating: false, phase: "ready", mode: "local", username: probe.username, error: message, notice: "" });
    }
    return channelSyncSnapshot();
}
