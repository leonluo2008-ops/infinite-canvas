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

export type HubTaskDetail = HubTaskSummary & {
    summary_md: string;
    checklist_md: string;
    prompt_md: string;
    /** 拆解实时阶段（bridge job stage 回写）；拆解中卡片显示。 */
    lapian_stage?: string;
    /** 拆解日志尾 15 行（排查留痕）。 */
    log_tail?: string;
    pair_lines: HubPairLine[];
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
    return { ...task, pair_lines: Array.isArray(task.pair_lines) ? task.pair_lines : [] };
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
