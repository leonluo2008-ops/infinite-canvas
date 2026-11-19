/**
 * S3.12 B3：媒体出口（图床首选源）前端 API 客户端。
 *
 *  * fetchMediaHost → GET /api/user/media-host：四源清单 + 当前 selected（null = 按默认顺序）；
 *  * saveMediaHost  → PUT /api/user/media-host：body 只有一个键 selected；
 *  * testMediaHost  → POST /api/user/media-host/test：body 只有一个键 id。
 *
 * 请求样式照抄 `channel-sync.ts`（Accept: application/json + credentials: same-origin）。
 * HTTP 非 2xx 一律把响应体里的 error 字段原文当 Error message 抛出（含 429 冷却文案，不改写）。
 */

const MEDIA_HOST_ENDPOINT = "/api/user/media-host";
const MEDIA_HOST_TEST_ENDPOINT = "/api/user/media-host/test";

export type MediaHostSource = {
    id: string;
    label: string;
    configured: boolean;
    default: boolean;
};

export type MediaHostState = {
    mode: string;
    selected: string | null;
    sources: MediaHostSource[];
};

export type MediaHostTestResult = {
    ok: boolean;
    id: string;
    url?: string;
    ms?: number;
    reason?: string;
};

async function readErrorPayload(response: Response): Promise<string> {
    const payload = (await response.json().catch(() => null)) as { error?: unknown } | null;
    return typeof payload?.error === "string" && payload.error ? payload.error : `HTTP ${response.status}`;
}

export async function fetchMediaHost(fetchImpl: typeof fetch = fetch): Promise<MediaHostState> {
    const response = await fetchImpl(MEDIA_HOST_ENDPOINT, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!response.ok) throw new Error(await readErrorPayload(response));
    return (await response.json().catch(() => null)) as MediaHostState;
}

export async function saveMediaHost(selected: string | null, fetchImpl: typeof fetch = fetch): Promise<{ ok: boolean; selected: string | null }> {
    const response = await fetchImpl(MEDIA_HOST_ENDPOINT, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ selected }),
    });
    const payload = (await response.json().catch(() => null)) as { ok?: unknown; selected?: unknown; error?: unknown } | null;
    if (!response.ok) throw new Error(await readErrorPayload(response));
    return {
        ok: payload?.ok === true,
        // 服务端回读值是唯一权威：成功后用它刷新本地选中项
        selected: typeof payload?.selected === "string" || payload?.selected === null ? (payload.selected as string | null) : null,
    };
}

export async function testMediaHost(id: string, fetchImpl: typeof fetch = fetch): Promise<MediaHostTestResult> {
    const response = await fetchImpl(MEDIA_HOST_TEST_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ id }),
    });
    // 失败也是 200（ok 字段承载结果）；仅 4xx/5xx（如 429 冷却）走抛错，error 原文不改写
    if (!response.ok) throw new Error(await readErrorPayload(response));
    return (await response.json().catch(() => null)) as MediaHostTestResult;
}
