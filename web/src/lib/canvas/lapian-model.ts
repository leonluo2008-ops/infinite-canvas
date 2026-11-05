/**
 * 拉片分析模型选择（M6-T7-B）：纯逻辑层。
 * 职责：多模态模型过滤分组（T7-A 的消费端）、选中值解析、/api/lapian/run 请求体装配（T7-B）。
 * 不读 i18n、不碰 React/zustand 单例——node 里可直接 import（esbuild bundle 探针先例 probe-t3g2.mjs）。
 * 产品中立：不含任何模型名/品牌字样，值全部来自用户渠道配置。
 * 渠道引用格式与 use-config-store 的 encodeChannelModel/decodeChannelModel 一致：`channelId::model`。
 */
import type { ModelChannel } from "@/stores/use-config-store";

const CHANNEL_MODEL_SEPARATOR = "::";

/** 渠道引用编码：`channelId::model`（与 use-config-store 的 encodeChannelModel 同格式）。 */
export function encodeChannelModelRef(channelId: string, model: string) {
    return `${channelId}${CHANNEL_MODEL_SEPARATOR}${model.trim()}`;
}

/** 渠道引用解码；无分隔符的裸值 → null（当作裸模型名处理，不猜渠道）。 */
export function decodeChannelModelRef(value: string) {
    const index = value.indexOf(CHANNEL_MODEL_SEPARATOR);
    if (index < 0) return null;
    return { channelId: value.slice(0, index), model: value.slice(index + CHANNEL_MODEL_SEPARATOR.length) };
}

export type LapianModelGroup = {
    channelId: string;
    channelName: string;
    /** 渠道内带 multimodal 能力标签的模型（保持渠道内原序）。 */
    models: Array<{ name: string; value: string }>;
};

/** 所有渠道中带 multimodal 能力标签的模型，按渠道分组；组序=渠道序，无 multimodal 模型的渠道不出现。 */
export function multimodalModelGroups(channels: ModelChannel[] | undefined): LapianModelGroup[] {
    return (channels || [])
        .map((channel) => ({
            channelId: channel.id,
            channelName: channel.name,
            models: (channel.models || [])
                .filter((model) => model.capability === "multimodal")
                .map((model) => ({ name: model.name, value: encodeChannelModelRef(channel.id, model.name) })),
        }))
        .filter((group) => group.models.length > 0);
}

export type LapianSelectionCheck = { ok: boolean; reason?: "not-found" | "incomplete" };

/** 校验已选分析模型：渠道还在且 baseUrl/key 齐全才放行；不选（空值）恒过。 */
export function checkLapianSelection(selection: string | undefined, channels: ModelChannel[] | undefined): LapianSelectionCheck {
    const trimmed = (selection || "").trim();
    if (!trimmed) return { ok: true };
    const ref = decodeChannelModelRef(trimmed);
    const channel = (channels || []).find((item) => item.id === ref?.channelId);
    if (!channel) return { ok: false, reason: "not-found" };
    if (!channel.baseUrl.trim() || !channel.apiKey.trim()) return { ok: false, reason: "incomplete" };
    return { ok: true };
}

export type LapianChannelRef = { baseUrl: string; key: string; model: string };
export type LapianRunBody = { id: string; intent: string; model?: string; channel?: LapianChannelRef };

/**
 * 装配 /api/lapian/run 请求体。不选模型时与现状逐字节一致（只 {id,intent}——旧链路零回归）；
 * 选了则补 `{model, channel:{baseUrl,key,model}}`（参照 services/api/video.ts 生成节点的渠道透传）。
 * 防御：选中值缺分隔符当裸模型名；渠道已删/配置不全时绝不发半截请求（不带 model/channel，交由上层校验拦）。
 */
export function buildLapianRunBody(id: string, intent: string, selection: string | undefined, channels: ModelChannel[] | undefined): LapianRunBody {
    const body: LapianRunBody = { id, intent };
    const trimmed = (selection || "").trim();
    if (!trimmed) return body;
    const ref = decodeChannelModelRef(trimmed);
    const model = ref?.model || trimmed;
    const channel = (channels || []).find((item) => item.id === ref?.channelId);
    const baseUrl = channel?.baseUrl.trim();
    const apiKey = channel?.apiKey.trim();
    if (!channel || !baseUrl || !apiKey) return body;
    return { ...body, model, channel: { baseUrl, key: apiKey, model } };
}
