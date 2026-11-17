/**
 * S3.5-B2：预置模板渠道合并（纯逻辑层，任务书 v1.2 §2.2 / §2.3）。
 *
 * 三个纯函数组成「预置层」的全部判断力，node 探针直接 import 断言（不读 i18n、不碰 store 单例）：
 *  * `mergePresetChannels`   四分支合并（tombstone 跳过 / 合入队尾 / 空 key 跟随刷新 / key 非空永不动）
 *                            + 同名策略（strip 后大小写不敏感）+ trim 空名预检；
 *  * `mintPromotedChannels`  升格派生 id（模板共享常量 id 严禁进全局主键表：填 key 即换新 id）；
 *  * `rewriteChannelRefs`    换 id 后同步重写 config 七处 `channelId::model` 引用串。
 */

import type { AiConfig, ModelChannel } from "@/stores/use-config-store";

const CHANNEL_MODEL_SEPARATOR = "::";

/** 服务端重名判定口径：**strip 后大小写不敏感**（服务端 UNIQUE(user_id,name) 在部分排序规则下同样如此）。 */
export function channelNameKey(name: string | undefined | null): string {
    return (name || "").trim().toLowerCase();
}

/**
 * **服务端 UNIQUE(user_id,name) 的真实口径**（`channel_store._replace_channels`：先 `str(...).strip()`
 * 再 `if name in names` 查重，落库列用 SQLite 默认 BINARY 排序规则）：
 *  * 比较前 strip（服务端两边都 strip，所以「空格差异」会撞）；
 *  * **不做大小写归一**——`MyChan` 与 `mychan` 是两条不同的行，服务端照收。
 *
 * 提交前判重必须用这个口径：拿 channelNameKey（大小写不敏感）判，会把服务端本来收得下的一对
 * 静默删成一条（B2.5 BL②）。预置模板「同名跳过」仍用 channelNameKey（任务书 §2.2 明确要求
 * strip 后大小写不敏感），两者是两套判据，别合并。
 */
export function serverNameKey(name: string | undefined | null): string {
    return (name || "").trim();
}

/** 有 key（非空即算）——**masked（`***` 开头）也算非空**：登录态服务端视图全是 masked，漏判会覆盖服务端权威渠道。 */
export function hasAnyKey(channel: Pick<ModelChannel, "apiKey"> | null | undefined): boolean {
    return Boolean((channel?.apiKey || "").trim());
}

/** 可用于升格的 key：非空且**不是** masked（masked = 服务端视图缓存，不是用户新填的明文）。 */
export function hasUsableKey(channel: Pick<ModelChannel, "apiKey"> | null | undefined): boolean {
    const value = (channel?.apiKey || "").trim();
    return Boolean(value) && !/^\*{2,}/.test(value);
}

export type PresetMergeOptions = {
    /** 用户删过的模板 id（preset_meta.tombstones）。 */
    tombstones?: string[];
    /** templateId → mintedId（preset_meta.aliases）：升格后的渠道已不再用共享 id。 */
    aliases?: Record<string, string>;
    /**
     * 预置到位后让位的本地内置占位渠道（V1「旧占位消失」）：
     * 命中且无 key 的本地渠道被移除，有 key 的一律保留（BYOK）。
     */
    dropPlaceholder?: (channel: ModelChannel) => boolean;
};

export type PresetMergeResult = {
    channels: ModelChannel[];
    /** 本次合入队尾的模板 id。 */
    added: string[];
    /** 本次跟随刷新的本地渠道 id（空 key 的模板渠道）。 */
    updated: string[];
    /** 同名跳过（模板名与本地/服务端渠道同名）。 */
    skippedSameName: string[];
    /** tombstone 跳过。 */
    skippedTombstones: string[];
    /** trim 后空名跳过（服务端 422）。 */
    skippedEmptyName: string[];
    /** 已升格过但本地已不存在（用户删了升格渠道）：不按共享 id 复活。 */
    skippedAliased: string[];
    /**
     * 同名跳过分支里认领的 alias（templateId → 本地已有渠道 id）。
     * 跨设备场景：设备 A 上模板渠道填 key 升格成 mintedId（alias 只存在于 A 的 preset_meta），
     * 设备 B 上没有 alias，模板 id 因同名被跳过 → B 上画布节点里的 `tpl::model` 永远解不出渠道。
     * 同名（strip 后相等）且 baseUrl 相等 = 就是同一条渠道，调用方据此补 recordAlias。
     */
    aliasClaims: Array<{ from: string; to: string }>;
    /** 让位移除的内置占位渠道 id。 */
    droppedPlaceholder: string[];
    /** 是否存在任何改动（版本相同/无变更时调用方据此零写入）。 */
    changed: boolean;
};

/**
 * 四分支合并（顺序固定：服务端视图 → localOnly → preset 队尾）。
 *
 * | 本地状态 | 动作 |
 * |---|---|
 * | tombstone 命中 | 跳过 |
 * | 本地不存在该 id（且不在服务端视图/localOnly 里） | 合入队尾（无 key） |
 * | 本地有、apiKey 为空 | 跟随更新（name/baseUrl/apiFormat/models 按模板刷；id/key 保留） |
 * | 本地有、apiKey 非空（含 masked） | 永不动（BYOK） |
 *
 * 「本地不存在」按 alias 换算后的 id 判定：已 mint 的渠道以新 id 存在于本地，绝不会
 * 因为模板 id 不在本地而再灌一条共享 id 的重复渠道（V9）。
 */
export function mergePresetChannels(current: ModelChannel[], preset: ModelChannel[], options: PresetMergeOptions = {}): PresetMergeResult {
    const tombstones = new Set((options.tombstones || []).map((id) => (id || "").trim()).filter(Boolean));
    const aliases = options.aliases || {};
    const keep: ModelChannel[] = [];
    const droppedPlaceholder: string[] = [];
    for (const channel of current || []) {
        if (!channel) continue;
        if (options.dropPlaceholder && !hasAnyKey(channel) && options.dropPlaceholder(channel)) {
            droppedPlaceholder.push(channel.id);
            continue;
        }
        keep.push(channel);
    }

    const indexById = new Map<string, number>();
    keep.forEach((channel, index) => indexById.set(channel.id, index));
    const names = new Set<string>();
    /** 名字键 → 该渠道在 keep 里的下标（同名跳过时用来认领 alias）。 */
    const byName = new Map<string, number>();
    keep.forEach((channel, index) => {
        const key = channelNameKey(channel.name);
        if (!key) return;
        names.add(key);
        if (!byName.has(key)) byName.set(key, index);
    });

    const added: string[] = [];
    const updated: string[] = [];
    const skippedSameName: string[] = [];
    const skippedTombstones: string[] = [];
    const skippedEmptyName: string[] = [];
    const skippedAliased: string[] = [];
    const aliasClaims: Array<{ from: string; to: string }> = [];

    for (const template of preset || []) {
        if (!template || typeof template !== "object") continue;
        const templateId = (template.id || "").trim();
        if (!templateId) continue;
        const nameKey = channelNameKey(template.name);
        // trim 后空名：服务端 (user_id,name) 会整批 422，宁可跳过并点名
        if (!nameKey) {
            skippedEmptyName.push(templateId);
            continue;
        }
        if (tombstones.has(templateId)) {
            skippedTombstones.push(templateId);
            continue;
        }
        const aliasedId = aliases[templateId] || "";
        const targetId = aliasedId || templateId;
        const existingIndex = indexById.has(targetId) ? indexById.get(targetId)! : indexById.has(templateId) ? indexById.get(templateId)! : undefined;

        if (existingIndex === undefined) {
            // 已升格过但本地渠道没了（用户删掉）：不按共享 id 复活（复活=同 id 再撞服务端主键）
            if (aliasedId) {
                skippedAliased.push(templateId);
                continue;
            }
            if (names.has(nameKey)) {
                skippedSameName.push(templateId);
                // 同名 + 同 baseUrl = 就是同一条渠道（多半是另一台设备上已升格过的这条模板渠道）：
                // 认领 alias 让本设备的画布节点引用 lazy 自愈。不同名不同址一律不认领（防误映射）。
                const claimedIndex = byName.get(nameKey);
                const claimed = claimedIndex === undefined ? undefined : keep[claimedIndex];
                if (claimed && claimed.id !== templateId && !aliases[templateId] && (claimed.baseUrl || "").trim() === (template.baseUrl || "").trim()) {
                    aliasClaims.push({ from: templateId, to: claimed.id });
                }
                continue;
            }
            keep.push({ ...template, id: templateId });
            indexById.set(templateId, keep.length - 1);
            names.add(nameKey);
            added.push(templateId);
            continue;
        }

        const existing = keep[existingIndex];
        // BYOK 红线：填过 key（masked 也算）的渠道永不被模板覆盖
        if (hasAnyKey(existing)) continue;

        const existingKey = channelNameKey(existing.name);
        const nameTaken = names.has(nameKey) && existingKey !== nameKey;
        // 模板名被别的渠道占用时保留本地原名（否则保存链会撞服务端重名 422）
        const nextName = nameTaken ? existing.name : template.name;
        const nextChannel: ModelChannel = {
            ...existing,
            name: nextName,
            baseUrl: template.baseUrl,
            apiFormat: template.apiFormat,
            models: template.models,
        };
        // 内容没变＝不是一次写入（版本相同/模板未动时调用方要能判出零写入，V6）
        const unchanged =
            existing.name === nextChannel.name &&
            existing.baseUrl === nextChannel.baseUrl &&
            existing.apiFormat === nextChannel.apiFormat &&
            JSON.stringify(existing.models) === JSON.stringify(nextChannel.models);
        if (unchanged) continue;
        keep[existingIndex] = nextChannel;
        if (channelNameKey(nextName) !== existingKey) {
            if (existingKey) names.delete(existingKey);
            names.add(channelNameKey(nextName));
        }
        updated.push(existing.id);
    }

    return {
        channels: keep,
        added,
        updated,
        skippedSameName,
        skippedTombstones,
        skippedEmptyName,
        skippedAliased,
        aliasClaims,
        droppedPlaceholder,
        changed: added.length > 0 || updated.length > 0 || droppedPlaceholder.length > 0,
    };
}

/**
 * 升格派生 id（任务书 §2.3 第一道防线）：模板 id 是跨用户共享常量，而 user_channels.id 是
 * 全局 PRIMARY KEY——把它带进服务端表必然与别的用户撞 id。**用户给模板渠道填了 key（apiKey
 * 空 → 非空）就必须换新 id**；alias 已存在则沿用，不再 mint（幂等：连续 flush id 不变）。
 *
 * 只处理「非空且非 masked」的 key：masked 是服务端视图缓存，换 id 需先确认服务端行迁移，
 * 这条留给两处 PUT 入口的防御断言（提交前必然经过）。
 */
export function mintPromotedChannels(
    channels: ModelChannel[],
    options: { templateIds: Iterable<string>; aliases?: Record<string, string>; mintId: () => string },
): { channels: ModelChannel[]; minted: Array<{ from: string; to: string }> } {
    const templateIds = new Set(Array.from(options.templateIds || []).map((id) => (id || "").trim()).filter(Boolean));
    if (!templateIds.size) return { channels: channels || [], minted: [] };
    const aliases = options.aliases || {};
    const minted: Array<{ from: string; to: string }> = [];
    const used = new Set((channels || []).map((channel) => channel?.id).filter(Boolean) as string[]);
    const next = (channels || []).map((channel) => {
        if (!channel || !templateIds.has(channel.id)) return channel;
        if (!hasUsableKey(channel)) return channel;
        const existing = aliases[channel.id];
        if (existing) {
            // 已有 alias：直接对齐（同一渠道第二次经过这里不会再 mint）
            if (!used.has(existing)) used.add(existing);
            return { ...channel, id: existing };
        }
        let to = options.mintId();
        while (used.has(to)) to = options.mintId();
        used.add(to);
        minted.push({ from: channel.id, to });
        return { ...channel, id: to };
    });
    return { channels: next, minted };
}

/** config 里嵌 `channelId::model` 引用的七处（use-config-store 的 encodeChannelModel 口径）。 */
export const CHANNEL_REF_FIELDS = ["model", "imageModel", "videoModel", "textModel", "audioModel", "lapianDefaultModel"] as const;

export type ChannelRefConfig = Pick<AiConfig, (typeof CHANNEL_REF_FIELDS)[number] | "lapianModels">;

function remapRef(value: string | undefined, aliasMap: Record<string, string>): string {
    const ref = value || "";
    const index = ref.indexOf(CHANNEL_MODEL_SEPARATOR);
    if (index < 0) return ref;
    const channelId = ref.slice(0, index);
    const next = aliasMap[channelId];
    return next ? `${next}${ref.slice(index)}` : ref;
}

/**
 * mint 后同步重写 config 的全部引用串：model / imageModel / videoModel / textModel / audioModel
 * + lapianModels / lapianDefaultModel（共七处）。画布节点里的引用不在全量重写范围内——
 * 那由 decode 处的 lazy 自愈接管（任务书 §2.3 方案 b）。
 */
export function rewriteChannelRefs<T extends ChannelRefConfig>(config: T, aliasMap: Record<string, string>): T {
    if (!aliasMap || !Object.keys(aliasMap).length) return config;
    const next = { ...config } as ChannelRefConfig;
    const record = next as unknown as Record<string, unknown>;
    for (const field of CHANNEL_REF_FIELDS) record[field] = remapRef(config[field], aliasMap);
    next.lapianModels = (config.lapianModels || []).map((ref) => remapRef(ref, aliasMap));
    return next as T;
}
