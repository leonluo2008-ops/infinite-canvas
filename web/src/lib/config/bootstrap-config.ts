import { normalizeLapianDefaultModel, normalizeLapianModelList } from "@/lib/canvas/lapian-model";
import { mergePresetChannels } from "@/lib/config/preset-merge";
import { readPresetMeta, recordAlias, setLastPresetVersion, setPresetTemplateIdList } from "@/lib/config/preset-meta";
import {
    CONFIG_STORE_KEY,
    createModelChannel,
    defaultConfig,
    defaultWebdavSyncConfig,
    encodeChannelModel,
    guessCapability,
    modelCapabilityOf,
    modelOptionsFromChannels,
    modelOptionName,
    normalizeModelOptionValue,
    type AiConfig,
    type ModelCapability,
    type ModelChannel,
} from "@/stores/use-config-store";

/**
 * 预置模板同步（S3.5：首启导入 + 版本化更新，单人模式路径）。
 *
 * 基线语义只剩「首拉一次」是设计级缺陷——存量浏览器永远停在旧「默认渠道」（任务书 §一）。
 * 现在**每次启动都全量 GET**（KB 级，send_body 恒 no-store，304 管道不存在），口径是
 * 「零写不零请求」：
 *
 *  * 版本相同（`presetVersion === preset_meta.lastPresetVersion`）→ **零写入**（V6）；
 *  * 首启（本地无信封）→ 写完整 persist 信封（defaultConfig + 预置渠道）→ reload 一次；
 *  * 版本更新 → **只重写渠道域**（channels/models/默认模型引用/lapian 两项）合并进现有信封，
 *    用户非渠道设置（videoSeconds/audioVoice/proxyUrl/…）与 webdav 原样保留（V10），写盘 → reload；
 *  * 合并无改动（全 BYOK / 全 tombstone）→ 只记版本，不写配置不 reload。
 *
 * 失败语义（零兜底）：端点 404 / 离线 / 坏载荷 / 坏信封一律静默放弃，本地照常起；
 * `setItem` 抛异常（配额）→ **不写版本不 reload**（防 reload 死循环）。
 * `lastPresetVersion` 只在合并真正落地后才写（登录态读取失败退 local 时不写，任务书 §2.1）。
 *
 * 首启与版本更新走**同一条 merge 路径**（去掉了首拉的独立 reload 分叉，任务书 §2.4/V7）。
 */
const BOOTSTRAP_CONFIG_ENDPOINT = "/api/bootstrap-config";
// use-config-store's persist declares no version → zustand default is 0.
const PERSIST_VERSION = 0;
/**
 * 内置占位渠道 id（defaultConfig.channels[0]）：预置渠道到位且它还没填 key 时让位，
 * 这就是 V1 说的「旧占位消失」。填过 key 的一律保留（BYOK 红线优先）。
 * 导出给登录模式（channel-sync 的 mergePresetLayer）复用同一个让位判定——两条路径必须同口径。
 */
export const PRESET_PLACEHOLDER_ID = "default";

export type BootstrapPresetPayload = { channels: ModelChannel[]; presetVersion: string };

function firstModelOptionOf(channels: ModelChannel[], capability: ModelCapability) {
    for (const channel of channels) {
        const model = channel.models.find((item) => item.capability === capability);
        if (model) return encodeChannelModel(channel.id, model.name);
    }
    return "";
}

/**
 * 拉预置文件（含 `presetVersion` = 服务端对模板文件现算的 sha1 前 8 位）。
 * **永不抛**：任何失败都回 null，调用方按「本地缓存照常起」降级（不塞进启动门的 withTimeout 失败路径）。
 */
export async function fetchPresetPayload(fetchImpl: typeof fetch = fetch): Promise<BootstrapPresetPayload | null> {
    try {
        const response = await fetchImpl(BOOTSTRAP_CONFIG_ENDPOINT);
        if (!response || !response.ok) return null;
        const payload = (await response.json().catch(() => null)) as { channels?: unknown; presetVersion?: unknown } | null;
        if (!payload || !Array.isArray(payload.channels)) return null;
        // Drop malformed entries (null/string) so a sloppy preset file cannot produce
        // placeholder channels; a normalized empty list imports nothing.
        const channels = payload.channels.filter((item) => Boolean(item) && typeof item === "object").map((channel) => createModelChannel(channel as Partial<ModelChannel>));
        if (!channels.length) return null;
        // 模板 id 集合落到 preset-meta（删渠道记墓碑 / 填 key 升格换 id 的唯一真源）
        setPresetTemplateIdList(channels.map((channel) => channel.id));
        const presetVersion = typeof payload.presetVersion === "string" ? payload.presetVersion : "";
        return { channels, presetVersion };
    } catch {
        // Missing endpoint or offline — keep the local config, stay silent.
        return null;
    }
}

/** 只重写渠道域（V10）：channels / models / 五个默认模型引用 / lapian 两项，其余字段原样带过。 */
export function rebuildChannelDomain(current: AiConfig, channels: ModelChannel[]): AiConfig {
    const models = modelOptionsFromChannels(channels);
    const primary = channels[0];
    const lapianModels = normalizeLapianModelList(current.lapianModels, channels);
    const lapianDefaultModel = normalizeLapianDefaultModel(current.lapianDefaultModel, lapianModels, channels);
    return {
        ...current,
        channels,
        models,
        baseUrl: primary?.baseUrl || current.baseUrl,
        apiFormat: primary?.apiFormat || current.apiFormat,
        lapianModels,
        lapianDefaultModel,
        model: resolveDomainRef(current.model || current.imageModel, current, channels) || models[0] || "",
        imageModel: resolveDomainRef(current.imageModel || current.model, current, channels, "image"),
        videoModel: resolveDomainRef(current.videoModel, current, channels, "video"),
        textModel: resolveDomainRef(current.textModel || current.model, current, channels, "text"),
        audioModel: resolveDomainRef(current.audioModel, current, channels, "audio"),
    };
}

/**
 * 渠道域引用解析；**解析为空时回退「该能力的第一个可用模型」**——与首启路径
 * （buildFirstLaunchConfig 的 firstModelOptionOf）同口径（B2.5 IF3）。
 *
 * 不回退时的旧症状：模板渠道换 id / 渠道被删后 `normalizeModelOptionValue` 返回 ""，
 * 单人模式升级到新模板后 model/videoModel 直接空掉（首启路径却会选上模型，两条路径不一致）。
 * 空串入参（用户本来就没选）不算解析失败，仍返回 ""——不替用户凭空选模型。
 */
function resolveDomainRef(value: string | undefined, current: AiConfig, channels: ModelChannel[], capability?: ModelCapability): string {
    const resolved = normalizeModelOptionValue(value, channels);
    if (resolved) return resolved;
    if (!value || !channels.length) return "";
    // capability 优先取引用里那个模型的真实能力（它还在本地渠道里时），否则按模型名 guessCapability 推
    const inferred = capability || modelCapabilityOf(current, value) || guessCapability(modelOptionName(value));
    return firstModelOptionOf(channels, inferred);
}

function buildFirstLaunchConfig(channels: ModelChannel[]): AiConfig {
    const models = modelOptionsFromChannels(channels);
    return {
        ...defaultConfig,
        channels,
        models,
        model: firstModelOptionOf(channels, "image") || models[0] || "",
        imageModel: firstModelOptionOf(channels, "image"),
        videoModel: firstModelOptionOf(channels, "video"),
        textModel: firstModelOptionOf(channels, "text"),
        audioModel: firstModelOptionOf(channels, "audio"),
    };
}

type PersistedEnvelope = { state?: { config?: Partial<AiConfig>; webdav?: unknown } } & Record<string, unknown>;

function readEnvelope(raw: string): PersistedEnvelope | null {
    try {
        const parsed = JSON.parse(raw) as unknown;
        if (!parsed || typeof parsed !== "object") return null;
        const envelope = parsed as PersistedEnvelope;
        if (!envelope.state || typeof envelope.state !== "object") return null;
        return envelope;
    } catch {
        return null;
    }
}

/** 写盘 + 记版本 + reload（`void` 不 await 时序不变：写入是同步 setItem）。 */
function commitAndReload(storage: Storage, envelope: unknown, presetVersion: string, channels: ModelChannel[]) {
    try {
        storage.setItem(CONFIG_STORE_KEY, JSON.stringify(envelope));
    } catch {
        // 配额/隐私模式写入失败：不记版本、不 reload（否则下次启动又判「版本不同」→ reload 死循环）
        return;
    }
    try {
        setLastPresetVersion(presetVersion);
    } catch {
        // 版本记不下来最多下次再合一次（幂等），仍允许 reload
    }
    console.info(`[bootstrap] synced ${channels.length} channel(s) from ${BOOTSTRAP_CONFIG_ENDPOINT}`);
    window.location.reload();
}

export async function maybeImportBootstrapConfig(fetchImpl: typeof fetch = fetch): Promise<void> {
    try {
        if (typeof window === "undefined" || !window.localStorage) return;
        const payload = await fetchPresetPayload(fetchImpl);
        if (!payload) return;

        const storage = window.localStorage;
        let raw: string | null = null;
        try {
            raw = storage.getItem(CONFIG_STORE_KEY);
        } catch {
            return;
        }

        // ---- 首启：本地无信封 → 写完整 persist 信封（唯一保留的整包路径）----
        if (!raw) {
            const config = buildFirstLaunchConfig(payload.channels);
            commitAndReload(storage, { state: { config, webdav: { ...defaultWebdavSyncConfig } }, version: PERSIST_VERSION }, payload.presetVersion, payload.channels);
            return;
        }

        // ---- 版本比对：相同 = 零写入（V6）----
        const meta = readPresetMeta();
        if (payload.presetVersion && payload.presetVersion === meta.lastPresetVersion) return;

        const envelope = readEnvelope(raw);
        const persistedConfig = envelope?.state?.config;
        if (!envelope || !persistedConfig) return; // 坏信封：不碰用户数据，下次启动再说

        const current = { ...defaultConfig, ...persistedConfig } as AiConfig;
        const merged = mergePresetChannels(current.channels || [], payload.channels, {
            tombstones: meta.tombstones,
            aliases: meta.aliases,
            dropPlaceholder: (channel) => channel.id === PRESET_PLACEHOLDER_ID,
        });
        // 同名 + 同址认领的 alias（跨设备：本设备没有另一台机上 minted 的记录时，靠这条让
        // 画布节点的旧 `templateId::model` 引用 lazy 自愈）。写盘失败不致命。
        for (const claim of merged.aliasClaims || []) {
            try {
                recordAlias(claim.from, claim.to);
            } catch {
                // 配额/隐私模式：下次启动再认领一次（幂等）
            }
        }
        if (!merged.changed) {
            // 模板新版本已评估完、无渠道变化（全 BYOK / 全 tombstone / 全同名）：只记版本，零写入配置
            if (payload.presetVersion) {
                try {
                    setLastPresetVersion(payload.presetVersion);
                } catch {
                    // 记不下版本只会让下次再评估一遍（幂等无副作用）
                }
            }
            return;
        }

        const config = rebuildChannelDomain(current, merged.channels);
        commitAndReload(storage, { ...envelope, state: { ...envelope.state, config } }, payload.presetVersion, merged.channels);
    } catch {
        // Missing endpoint or offline — keep the default config, stay silent.
    }
}
