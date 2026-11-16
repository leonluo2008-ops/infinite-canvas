/**
 * S2-B3：渠道请求层的服务端代转发改道（`/api/proxy/<channelId>/<path>`）。
 *
 * 三条硬约束（任务书 v1.3 §二 B3.5）：
 *  1. 只有**渠道 API 请求**改道：生成请求（image/video/audio）与模型列表拉取。
 *     image-storage / file-storage / webdav / 成片 CDN 下载等非渠道请求一律不走这里。
 *  2. 服务端模式**前端零携带 key**：Authorization / x-goog-api-key 由服务端按渠道密文注入，
 *     gemini 的 `?key=` 同样由服务端拼（前端不带）。
 *  3. 路径必须是「上游绝对路径去掉 origin」：服务端代转发是 `baseUrl.path + "/" + subpath`，
 *     自己不加 `/v1`。所以这里用 buildApiAbsoluteUrl 先算出带 `/v1`（gemini 带 `/v1beta`）的
 *     绝对地址，再剥 origin —— 与本地直连时命中同一个上游 URL。
 */
import i18n from "@/i18n";
import { buildApiAbsoluteUrl, buildApiUrl, decodeChannelModel, useConfigStore, withLocalProxy, type AiConfig } from "@/stores/use-config-store";

export type ChannelTarget = {
    /** 缺省时回落到 store 当前模式（fetchImageModels 这类只拿 baseUrl/key 的老调用点因此自动跟随）。 */
    channelMode?: AiConfig["channelMode"];
    /** resolveModelRequestConfig 注入的渠道 id；缺省时从 `channelId::model` 引用串里解。 */
    channelId?: string;
    model?: string;
};

export const CHANNEL_PROXY_PREFIX = "/api/proxy";

export function channelModeOf(config?: ChannelTarget | null): AiConfig["channelMode"] {
    return config?.channelMode || useConfigStore.getState().config.channelMode;
}

/** 服务端渠道模式（复用既有 "remote" 语义，见任务书 B3.1）。 */
export function isServerChannelMode(config?: ChannelTarget | null): boolean {
    return channelModeOf(config) === "remote";
}

export function channelIdOf(config?: ChannelTarget | null): string {
    const injected = (config?.channelId || "").trim();
    if (injected) return injected;
    return decodeChannelModel((config?.model || "").trim())?.channelId || "";
}

/**
 * 绝对上游地址 → 同域代转发地址。
 * `/api/proxy/<channelId>/<baseUrl.path + /v1… + path>` —— 服务端只做 baseUrl.path 拼接，
 * 其余路径（含 `/v1`、`?alt=sse` 之类 query）都由这里带过去。
 */
export function channelProxyUrl(config: ChannelTarget | null | undefined, absoluteUrl: string): string {
    const channelId = channelIdOf(config);
    if (!channelId) throw new Error(i18n.t("apiErrors.channelMissing"));
    const parsed = new URL(absoluteUrl, typeof window !== "undefined" ? window.location.origin : "https://channel.invalid");
    const upstreamPath = `${parsed.pathname || "/"}${parsed.search}`;
    return `${CHANNEL_PROXY_PREFIX}/${encodeURIComponent(channelId)}${upstreamPath.startsWith("/") ? "" : "/"}${upstreamPath}`;
}

/** 渠道 API 地址（相对 path）：服务端模式改道 /api/proxy，本地模式原样 buildApiUrl（含本地转发前缀）。 */
export function channelApiUrl(config: AiConfig & ChannelTarget, path: string): string {
    if (!isServerChannelMode(config)) return buildApiUrl(config.baseUrl, path);
    return channelProxyUrl(config, buildApiAbsoluteUrl(config.baseUrl, path));
}

/** 渠道 API 地址（调用方自己拼好的绝对上游地址，如 gemini 的 /v1beta/…）。 */
export function channelAbsoluteUrl(config: AiConfig & ChannelTarget, absoluteUrl: string): string {
    return isServerChannelMode(config) ? channelProxyUrl(config, absoluteUrl) : withLocalProxy(absoluteUrl);
}

/** OpenAI 型鉴权头：服务端模式下不携带（key 由服务端注入）。 */
export function channelAuthHeaders(config: AiConfig & ChannelTarget, contentType?: string): Record<string, string> {
    const headers: Record<string, string> = {};
    if (!isServerChannelMode(config)) headers.Authorization = `Bearer ${config.apiKey}`;
    if (contentType) headers["Content-Type"] = contentType;
    return headers;
}

/** Gemini 型鉴权头：服务端模式下不携带（服务端注入 `?key=`）。 */
export function geminiChannelHeaders(config: AiConfig & ChannelTarget): Record<string, string> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (!isServerChannelMode(config)) headers["x-goog-api-key"] = config.apiKey;
    return headers;
}
