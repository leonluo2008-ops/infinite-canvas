/**
 * S2.7 拉片耗时摘要（纯逻辑，无 React 依赖，esbuild 探针可直接 import）。
 *
 * 背景：服务器拉片比 PC 慢，节点上只有「已用时 3:12」这一个数，看不出时间花在抽帧还是模型调用。
 * 数据源是 bridge 的 `GET /api/lapian/timing?task=<id>`（server 侧包装层写的 `耗时.json`）：
 * 只在**服务端渠道模式**（`channelMode === "remote"`，即多账户登录态）下存在——单人模式该端点
 * 一律 404，本模块据此返回 null，节点不渲染任何新东西（单人模式零改动）。
 *
 * 口径：任何异常/不可用（404、401、available:false、坏 JSON、网络错）都归成 null，
 * 耗时摘要永远只是附注，不影响节点主流程（正文/错误/重查照旧）。
 */

export type LapianTimingView = {
    task?: string;
    available?: boolean;
    reason?: string;
    missing?: string[];
    "总耗时"?: number | null;
    "抽帧耗时"?: number | null;
    "大拆解等待"?: number | null;
    "门禁重拆轮次"?: number | null;
};

const BRIDGE = ""; // 同域相对路径（与 lapian-execute / lapian-node 同值）

/** 秒 → `m:ss`；缺失/非法 → 破折号（不猜 0，免得把「不知道」画成「没花时间」）。 */
export function formatTimingSeconds(value: number | null | undefined): string {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
    const total = Math.round(value);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * 一行摘要：`总 3:12｜抽帧 0:18｜模型等待 2:40｜重拆 1 轮`。
 * 缺失的分量整段省略（不写 0）；一个分量都没有 → 空串（调用方据此不渲染）。
 */
export function buildTimingSummary(view: LapianTimingView | null | undefined): string {
    if (!view || view.available !== true) return "";
    const parts: string[] = [];
    if (typeof view["总耗时"] === "number") parts.push(`总 ${formatTimingSeconds(view["总耗时"])}`);
    if (typeof view["抽帧耗时"] === "number") parts.push(`抽帧 ${formatTimingSeconds(view["抽帧耗时"])}`);
    if (typeof view["大拆解等待"] === "number") parts.push(`模型等待 ${formatTimingSeconds(view["大拆解等待"])}`);
    if (typeof view["门禁重拆轮次"] === "number") parts.push(`重拆 ${view["门禁重拆轮次"]} 轮`);
    return parts.join("｜");
}

/** 悬停明细：把每个分量写全（含不可用的原因），与一行摘要同一份数据。 */
export function buildTimingTitle(view: LapianTimingView | null | undefined): string {
    if (!view || view.available !== true) return String(view?.reason || "耗时数据不可用");
    const lines = [
        `总耗时 ${formatTimingSeconds(view["总耗时"])}`,
        `抽帧 ${formatTimingSeconds(view["抽帧耗时"])}`,
        `大拆解（模型等待）${formatTimingSeconds(view["大拆解等待"])}`,
        `门禁重拆 ${typeof view["门禁重拆轮次"] === "number" ? view["门禁重拆轮次"] : "—"} 轮`,
    ];
    return lines.join("\n");
}

/**
 * 取一次耗时分解。**不抛**：非 2xx（单人模式 404 / 未登录 401）、available:false、解析失败、
 * 网络异常一律返回 null。调用方拿 null 就什么都不画。
 */
export async function fetchLapianTiming(
    taskId: string,
    deps: { fetch: (url: string, init?: RequestInit) => Promise<Response> },
): Promise<LapianTimingView | null> {
    const id = String(taskId || "").trim();
    if (!id) return null;
    try {
        const response = await deps.fetch(`${BRIDGE}/api/lapian/timing?task=${encodeURIComponent(id)}`);
        if (!response.ok) return null;
        const payload = (await response.json()) as LapianTimingView | null;
        if (!payload || payload.available !== true) return null;
        return payload;
    } catch {
        return null;
    }
}
