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
    /** 渠道 baseUrl（如 `https://ollama.com/v1`）；裁剪 subpath 前缀用，不回落 store。 */
    baseUrl?: string;
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
 * 渠道 baseUrl 的路径前缀（去尾斜杠）。解析失败 / 无路径 / 非 http(s) → 空串（不裁剪）。
 * 服务端合同是「subpath 相对 baseUrl」（channel_store._forward 拼 `base.path + "/" + subpath`），
 * 所以前端必须剥掉 baseUrl 自带路径（如 `https://ollama.com/v1` 的 `/v1`），否则上游收到 `/v1/v1/…`。
 */
function stripBasePath(config: ChannelTarget | null | undefined, pathname: string): string {
    // 不回落 store：回落会让不同调用点语义不一致，调用方（resolveModelRequestConfig）已自带 baseUrl
    const raw = (config?.baseUrl || "").trim();
    let basePath = "";
    try {
        basePath = new URL(raw, "https://channel.invalid").pathname.replace(/\/+$/, "");
    } catch {
        basePath = "";
    }
    if (!basePath || basePath === "/") return pathname;
    const lower = pathname.toLowerCase();
    const baseLower = basePath.toLowerCase();
    if (lower === baseLower) return "/";
    if (lower.startsWith(`${baseLower}/`)) return pathname.slice(basePath.length);
    return pathname;
}

/**
 * 绝对上游地址 → 同域代转发地址。
 * `/api/proxy/<channelId>/<相对 baseUrl 的 subpath>` —— 服务端做 `baseUrl.path + "/" + subpath` 拼接，
 * 所以这里传「绝对地址 pathname 剥掉 baseUrl 路径前缀」的相对路径（query 原样保留）。
 */
export function channelProxyUrl(config: ChannelTarget | null | undefined, absoluteUrl: string): string {
    const channelId = channelIdOf(config);
    if (!channelId) throw new Error(i18n.t("apiErrors.channelMissing"));
    const parsed = new URL(absoluteUrl, typeof window !== "undefined" ? window.location.origin : "https://channel.invalid");
    const upstreamPath = `${stripBasePath(config, parsed.pathname || "/")}${parsed.search}`;
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

// ------------------------------------------------------------------ 成品（视频/音频）下载地址（B3.5 IMPL-1）

/** 掩码 key（服务端只回显 `***abcd` / `***?`）：**绝不能**发给上游——上游收到的就是字面量 `***`，必 401。 */
export function isMaskedKeyValue(value: string | null | undefined): boolean {
    return /^\*{2,}/.test((value || "").trim());
}

/**
 * 剥掉 query 里的掩码 `key=` 参数。
 * 掩码值留在地址里只会被原样发上游（401）；真 key 必须由服务端代转发注入。
 * 没有掩码 key 时原样返回（不做任何重编码，避免动到签名类查询串）。
 */
export function stripMaskedKeyParam(url: string): string {
    if (!/^https?:\/\//i.test(url) || !/[?&]key=/.test(url)) return url;
    const parsed = new URL(url, "https://channel.invalid");
    let removed = false;
    for (const [name, value] of Array.from(parsed.searchParams.entries())) {
        if (name === "key" && isMaskedKeyValue(value)) {
            parsed.searchParams.delete(name);
            removed = true;
        }
    }
    if (!removed) return url;
    const query = parsed.searchParams.toString();
    return `${parsed.origin}${parsed.pathname}${query ? `?${query}` : ""}`;
}

/** 上游签发的一次性直链常见的凭据参数（S3/GCS/各家 token 风格）。 */
const MEDIA_CREDENTIAL_PARAMS = new Set(["key", "access_token", "auth", "auth_key", "token", "sig", "signature", "x-amz-signature", "x-amz-credential", "x-goog-signature"]);

/** 地址是否「自含真凭据」（完整可下载直链）：这种地址再经代转发反而会丢掉签名。 */
export function isSelfCredentialedMediaUrl(url: string): boolean {
    if (!/^https?:\/\//i.test(url)) return false;
    const parsed = new URL(url, "https://channel.invalid");
    for (const [name, value] of Array.from(parsed.searchParams.entries())) {
        if (MEDIA_CREDENTIAL_PARAMS.has(name.toLowerCase()) && (value || "").trim() && !isMaskedKeyValue(value)) return true;
    }
    return false;
}

export type ChannelMediaTarget = { url: string; viaProxy: boolean };

function hostOf(url: string): string {
    try {
        return new URL(url).host;
    } catch {
        return "";
    }
}

/**
 * 成品（视频/音频）下载地址择路（B3.5 IMPL-1）。两条硬判据：
 *  ① **不向前端泄 key**：服务端模式任何情况下都不在地址里带明文 key（要带就由服务端注入）；
 *  ② **不把掩码 key 发给上游**：`***abcd` 一律剥掉，改走代转发由服务端注入真 key。
 * 择路：
 *  * 本地模式 → 与基线**字节级一致**（`withLocalProxy(url)`，单人零改动）。
 *  * 服务端模式：
 *    - 地址自含真凭据（`?key=` 真值 / `X-Amz-Signature` 等）→ 原样直连：这是上游签发的
 *      一次性直链，代转发只会按渠道 baseUrl 重拼 host+path，签名必失效。
 *    - 异源（host ≠ 渠道 baseUrl host）→ 原样直连：代转发只会打到渠道 host（见 `_forward`
 *      里 `host = base.hostname`），异源 CDN 走代理必错。
 *    - 其余（同源且不带凭据）→ `/api/proxy/<channelId>/<path>`，key 由服务端注入。
 */
export function channelMediaUrl(config: (AiConfig & ChannelTarget) | null | undefined, absoluteUrl: string): ChannelMediaTarget {
    if (!isServerChannelMode(config)) return { url: withLocalProxy(absoluteUrl), viaProxy: false };
    const cleaned = stripMaskedKeyParam(absoluteUrl);
    if (isSelfCredentialedMediaUrl(cleaned)) return { url: cleaned, viaProxy: false };
    const baseHost = hostOf((config?.baseUrl || "").trim());
    const targetHost = hostOf(cleaned);
    if (!baseHost || (targetHost && targetHost !== baseHost)) return { url: cleaned, viaProxy: false };
    return { url: channelProxyUrl(config, cleaned), viaProxy: true };
}
