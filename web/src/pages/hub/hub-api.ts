/**
 * 自动化工作台（/api/hub/*）接口层。
 *
 * 合同与 bridge 端 hub_api.py 对齐：
 * - 列表只回摘要，详情按需拉；
 * - ingest 只建任务，拆解由前端接着 POST /api/lapian/run 触发；
 * - 所有请求走同域相对路径（vite proxy / 隧道统一转发到 bridge）。
 */

export type HubTaskState = "ingested" | "discussing" | "awaiting_assets" | "rendering" | "awaiting_approval" | "approved" | "generating" | "done" | "failed";

export type HubTaskSummary = {
    local_id: string;
    state: HubTaskState | string;
    intent: string;
    created_at: string;
    video_name: string;
    gen_progress: number | null;
    has_output: boolean;
};

export type HubPairLine = {
    no: number | string;
    desc: string;
    /** 合同字段：资产名/说明；桥端实际回填时可能叫 img / name。 */
    asset?: string;
    img?: string;
    name?: string;
    url?: string;
};

/** 资产卡帧对象（automation/frames.build_frame）。url 空串 = 连首帧兜底都拿不到，前端直接出「未定位」占位块。 */
export type HubCardFrame = {
    url: string;
    秒: number;
    来源: string;
    定位: string;
    错误?: string;
};

/** 资产卡（数据合同 v3，B2：GET /api/hub/task/<id> 的 asset_cards[]；字段名与桥端中文键一一对应）。 */
export type HubAssetCard = {
    n: number;
    类别: string;
    短名: string;
    建议: string;
    核心: boolean;
    出现时段: { 起: string; 止: string }[];
    帧: HubCardFrame[];
    已传图: string[];
    描述: string;
    同实体疑似: number[];
    定位: string;
    时段来源: string;
};

/** POST /api/hub/asset 的返回（写清单后回显真源列）。 */
export type HubAssetWriteResult = {
    ok?: boolean;
    task_id?: string;
    n?: number;
    已传图?: string[];
    命名?: string;
};

export type HubTaskDetail = HubTaskSummary & {
    summary_md: string;
    checklist_md: string;
    prompt_md: string;
    /** 拆解实时阶段（bridge job stage 回写）；拆解中卡片显示。 */
    lapian_stage?: string;
    /** 拆解日志尾 15 行（排查留痕）。 */
    log_tail?: string;
    pair_lines: HubPairLine[];
    /** 资产卡（数据合同 v3）；旧 bridge 无此字段 → 归一为 [] → 前端退化回文字配对表。 */
    asset_cards: HubAssetCard[];
    /** 本会话有无置换写入（换图/沿用/品牌文字）；仅参考，前端以 asset_cards 已传图为准。 */
    has_pending_replace?: boolean;
    error: string;
    gen?: Record<string, unknown>;
};

export type HubRenderPreview = {
    duration?: number | string;
    ratio?: string;
    snapshot?: string;
    [key: string]: unknown;
};

type JsonRecord = Record<string, unknown>;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
    const response = await fetch(url, { cache: "no-store", ...init });
    const text = await response.text();
    let data: JsonRecord | null = null;
    try {
        data = text ? (JSON.parse(text) as JsonRecord) : null;
    } catch {
        data = null;
    }
    if (!response.ok) {
        const detail = data ? String(data.error || data.message || "") : "";
        throw new Error(detail || text.slice(0, 200) || `请求失败（HTTP ${response.status}）`);
    }
    if (data?.error) throw new Error(String(data.error));
    return (data || {}) as T;
}

export function errorText(error: unknown) {
    return error instanceof Error ? error.message : String(error || "未知错误");
}

export async function listHubTasks(): Promise<HubTaskSummary[]> {
    // 合同是 {tasks:[...]}；兼容桥端直接回数组。
    const data = await request<{ tasks?: HubTaskSummary[] } | HubTaskSummary[]>("/api/hub/tasks");
    if (Array.isArray(data)) return data;
    return Array.isArray(data.tasks) ? data.tasks : [];
}

export async function getHubTask(localId: string): Promise<HubTaskDetail> {
    // 合同是 {task:{...}}；同时兼容桥端直接回扁平对象，避免前后端并行期互相卡死。
    const data = await request<{ task?: HubTaskDetail } & Partial<HubTaskDetail>>(`/api/hub/task/${encodeURIComponent(localId)}`);
    const task = (data.task || (data as unknown as HubTaskDetail)) as HubTaskDetail;
    if (!task || !task.local_id) throw new Error("任务详情为空");
    return { ...task, pair_lines: Array.isArray(task.pair_lines) ? task.pair_lines : [], asset_cards: normalizeAssetCards(task.asset_cards) };
}

/** 读层入口集中归一（项目硬约束 14 同款思路）：asset_cards 缺失/畸形不炸页面，逐字段兜底。 */
function normalizeAssetCards(value: unknown): HubAssetCard[] {
    if (!Array.isArray(value)) return [];
    return value.map((raw) => {
        const item = (raw && typeof raw === "object" ? raw : {}) as JsonRecord;
        const spans = Array.isArray(item["出现时段"]) ? (item["出现时段"] as unknown[]) : [];
        const frames = Array.isArray(item["帧"]) ? (item["帧"] as unknown[]) : [];
        const urls = Array.isArray(item["已传图"]) ? (item["已传图"] as unknown[]) : [];
        const twins = Array.isArray(item["同实体疑似"]) ? (item["同实体疑似"] as unknown[]) : [];
        const card: HubAssetCard = {
            n: Number(item.n) || 0,
            类别: hubText(item["类别"]),
            短名: hubText(item["短名"]),
            建议: hubText(item["建议"]),
            核心: Boolean(item["核心"]),
            出现时段: spans.map((span) => {
                const s = (span && typeof span === "object" ? span : {}) as JsonRecord;
                return { 起: hubText(s["起"]), 止: hubText(s["止"]) };
            }),
            帧: frames.map((shot) => {
                const f = (shot && typeof shot === "object" ? shot : {}) as JsonRecord;
                const frame: HubCardFrame = {
                    url: hubText(f.url),
                    秒: typeof f["秒"] === "number" && Number.isFinite(f["秒"]) ? f["秒"] : 0,
                    来源: hubText(f["来源"]),
                    定位: hubText(f["定位"]),
                };
                if (f["错误"] !== undefined && f["错误"] !== null) frame.错误 = hubText(f["错误"]);
                return frame;
            }),
            已传图: urls.map((url) => hubText(url)).filter(Boolean),
            描述: hubText(item["描述"]),
            同实体疑似: twins.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
            定位: hubText(item["定位"]),
            时段来源: hubText(item["时段来源"]),
        };
        return card;
    });
}

function hubText(value: unknown): string {
    return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

export async function ingestVideo(video: File, intent: string): Promise<{ local_id: string; bridge_id: string }> {
    const body = new FormData();
    body.append("video", video);
    body.append("intent", intent);
    return request<{ local_id: string; bridge_id: string }>("/api/hub/ingest", { method: "POST", body });
}

/** 建任务后立即触发拆解（hub 专用：内部走 automation 状态机 ingested→awaiting_assets，非裸 lapian/run）。 */
export async function startLapianRun(localId: string, _intent: string) {
    return request<{ job_id?: string }>(`/api/hub/discuss/${encodeURIComponent(localId)}`, {
        method: "POST",
    });
}

export async function uploadHubAssets(localId: string, files: File[]): Promise<{ filled: HubPairLine[]; state?: string }> {
    const body = new FormData();
    for (const file of files) body.append("file", file);
    const data = await request<{ filled?: HubPairLine[]; state?: string }>(`/api/hub/assets/${encodeURIComponent(localId)}`, { method: "POST", body });
    return { filled: Array.isArray(data.filled) ? data.filled : [], state: data.state };
}

export async function confirmHubReplace(localId: string): Promise<{ state?: string }> {
    return request<{ state?: string }>(`/api/hub/confirm/${encodeURIComponent(localId)}`, { method: "POST" });
}

/**
 * 置换台逐卡定向写（POST /api/hub/asset，数据合同 v3 写合同）：
 *  - {n, image_url} 换图（重传覆盖）；image_url 必须是公网 HTTPS（先走 uploadHubImage）
 *  - {n, clear:true} 「沿用」= 清空该行绑定
 *  - {n, text}       品牌视觉卡的文字，写清单「命名」列
 */
export async function postHubAsset(localId: string, payload: { n: number; image_url?: string; clear?: boolean; text?: string }): Promise<HubAssetWriteResult> {
    return request<HubAssetWriteResult>("/api/hub/asset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: localId, ...payload }),
    });
}

/**
 * 单图 → 图床公网 HTTPS 直链（POST /api/asset/upload）。
 * 通道收原始字节 + X-Filename 头（非 multipart），与 services/api/video.ts 的 SDGF 参考图同一条桥端路由。
 */
export async function uploadHubImage(file: File): Promise<string> {
    const name = encodeURIComponent(file.name || `ref-${Date.now()}.png`);
    const data = await request<{ url?: string }>("/api/asset/upload", {
        method: "POST",
        headers: { "X-Filename": name, "Content-Type": "application/octet-stream" },
        body: file,
    });
    const url = typeof data.url === "string" ? data.url : "";
    if (!/^https:\/\//.test(url)) throw new Error(`图床未返回公网 HTTPS 直链：${JSON.stringify(data).slice(0, 120)}`);
    return url;
}

export async function renderHubTask(localId: string): Promise<{ state?: string; preview?: HubRenderPreview | null; prompt_md?: string }> {
    return request<{ state?: string; preview?: HubRenderPreview | null; prompt_md?: string }>(`/api/hub/render/${encodeURIComponent(localId)}`);
}

export async function approveHubTask(localId: string): Promise<{ state?: string }> {
    return request<{ state?: string }>(`/api/hub/approve/${encodeURIComponent(localId)}`, { method: "POST" });
}

export async function submitHubTask(localId: string): Promise<{ job_id?: string }> {
    return request<{ job_id?: string }>(`/api/hub/submit/${encodeURIComponent(localId)}`, { method: "POST" });
}

/** 重试端点可能尚未上桥；调用方按「失败仅刷新」处理。 */
export async function retryHubTask(localId: string): Promise<{ state?: string }> {
    return request<{ state?: string }>(`/api/hub/retry/${encodeURIComponent(localId)}`, { method: "POST" });
}

export function hubOutputUrl(localId: string) {
    return `/api/hub/file/${encodeURIComponent(localId)}`;
}
