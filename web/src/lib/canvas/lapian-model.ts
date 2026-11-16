/**
 * 拉片模型选择（M6-T7-R）：纯逻辑层。
 * 职责：全局拉片模型清单归一（跨渠道勾选，独立清单不动渠道归属）、默认传导、
 *       渠道引用编解码、/api/lapian/run 请求体装配（契约与 c4d9b8a 一致，bridge 不动）。
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

/** 引用是否仍指向真实存在的渠道模型（渠道还在且渠道内确有该模型）。 */
export function isKnownChannelModelRef(ref: string | undefined, channels: ModelChannel[] | undefined) {
    const decoded = decodeChannelModelRef((ref || "").trim());
    if (!decoded) return false;
    const channel = (channels || []).find((item) => item.id === decoded.channelId);
    return Boolean(channel && channel.models.some((model) => model.name === decoded.model));
}

/**
 * 归一全局拉片模型清单：只留仍指向真实渠道模型的引用、去重、保序。
 * 渠道被删/模型被改名后，存量勾选自动失效（store merge 与保存前共用此口径）。
 */
export function normalizeLapianModelList(refs: string[] | undefined, channels: ModelChannel[] | undefined) {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const ref of refs || []) {
        const value = (ref || "").trim();
        if (!value || seen.has(value) || !isKnownChannelModelRef(value, channels)) continue;
        seen.add(value);
        result.push(value);
    }
    return result;
}

/** 归一全局默认拉片模型：默认必须是清单内且仍指向真实渠道模型的引用，否则回空（=走 bridge 默认）。 */
export function normalizeLapianDefaultModel(defaultRef: string | undefined, refs: string[] | undefined, channels: ModelChannel[] | undefined) {
    const value = (defaultRef || "").trim();
    if (!value) return "";
    return normalizeLapianModelList([value], channels).length && (refs || []).includes(value) ? value : "";
}

/**
 * 默认传导（T7-R ③）：节点显式换过模型（metadata.lapianModel）优先，否则跟随全局默认；都没有 → 空（走 bridge 默认）。
 */
export function resolveEffectiveLapianModel(nodeOverride: string | undefined, globalDefault: string | undefined) {
    return (nodeOverride || "").trim() || (globalDefault || "").trim();
}

export type LapianModelGroup = {
    channelId: string;
    channelName: string;
    models: Array<{ name: string; value: string }>;
};

/**
 * 全局清单按渠道分组（组序=渠道序，渠道内保持清单勾选序）：
 * 供节点「模型」弹窗与下拉 optgroup 使用；未勾选/引用已失效的模型不出现。
 */
export function lapianModelGroups(channels: ModelChannel[] | undefined, refs: string[] | undefined): LapianModelGroup[] {
    const groups: LapianModelGroup[] = [];
    for (const channel of channels || []) {
        const models = (refs || [])
            .filter((ref) => decodeChannelModelRef(ref)?.channelId === channel.id)
            .filter((ref) => channel.models.some((model) => model.name === decodeChannelModelRef(ref)?.model))
            .map((ref) => ({ name: decodeChannelModelRef(ref)!.model, value: ref }));
        if (models.length) groups.push({ channelId: channel.id, channelName: channel.name, models });
    }
    return groups;
}

export type LapianChannelRef = { baseUrl: string; key: string; model: string };
export type LapianRunBody = { id: string; intent: string; model?: string; channel?: LapianChannelRef; channelId?: string };

/** 掩码 key（服务端只回显 `***last4` / `***?`）：**绝不能**发给上游——上游收到的就是字面量 `***`，必 401。
 *  与 `services/api/channel-proxy.isMaskedKeyValue` 同一口径（此处内联一份：本模块要能被 node 直接
 *  import，不引入 store/i18n 依赖链）。 */
function isMaskedKeyValue(value: string | null | undefined) {
    return /^\*{2,}/.test((value || "").trim());
}

/**
 * 装配 /api/lapian/run 请求体（与 c4d9b8a 同契约，bridge 不动）。
 * 未配置/未选模型时与现状逐字节一致（只 {id,intent}——旧链路零回归）；选了则分两态（S2.6）：
 *  * **local**（单人模式，默认）：补 `{model, channel:{baseUrl,key,model}}`——带明文 key，逐字节不变；
 *  * **remote**（服务端渠道模式）：只补 `{channelId, model}`——**前端零 key**，服务端按登录用户的
 *    渠道解密后注入子进程环境；masked key（服务端视图回显）也只可能出现在这一态，因此不再因
 *    「key 不是明文」把渠道丢掉（旧防御分支把 remote 用户整条渠道丢了 → 回落服务器单租户密钥）。
 * 防御：选中值缺分隔符当裸模型名；渠道已删/配置不全/模型已改名时绝不发半截请求（不带 model/channel）。
 */
export function buildLapianRunBody(
    id: string,
    intent: string,
    selection: string | undefined,
    channels: ModelChannel[] | undefined,
    channelMode: "local" | "remote" = "local",
): LapianRunBody {
    const body: LapianRunBody = { id, intent };
    const trimmed = (selection || "").trim();
    if (!trimmed) return body;
    const ref = decodeChannelModelRef(trimmed);
    const model = ref?.model || trimmed;
    const channel = (channels || []).find((item) => item.id === ref?.channelId);
    const baseUrl = channel?.baseUrl.trim();
    if (!channel || !baseUrl) return body;
    // T7-R 防御：引用指向的模型已被改名/删除 → 绝不发半截请求（零字段，走 bridge 默认）。
    if (!channel.models.some((item) => item.name === model)) return body;
    if (channelMode === "remote" || isMaskedKeyValue(channel.apiKey)) {
        // 服务端渠道模式：渠道 id 就是全部凭据；masked key 说明这是服务端视图，同样走 id
        if (!channel.id) return body;
        return { ...body, model, channelId: channel.id };
    }
    const apiKey = channel.apiKey.trim();
    if (!apiKey) return body;
    return { ...body, model, channel: { baseUrl, key: apiKey, model } };
}
