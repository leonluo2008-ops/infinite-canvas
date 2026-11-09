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
    bridge_id?: string;
    state: HubTaskState | string;
    intent: string;
    created_at: string;
    video_name: string;
    gen_progress: number | null;
    has_output: boolean;
    /** AT-9 B1 摘要三字段（旧 bridge 缺 → 归一为空，前端兜底显示）。 */
    task_name?: string;
    /** discussing 时的拆解阶段（实时计算，不落盘）。 */
    stage?: string;
    /** 已换资产数 "已换/必换"（实时计算，如 "1/2"）。 */
    replaced?: string;
    has_pending_replace?: boolean;
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

/** 资产卡「画面位置」（AT-9 B1，九宫格 1-9 行主序：1=左上…9=右下；绑定展示帧所属镜次）。 */
export type HubCardPosition = { 格: number; 镜次: number };

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
    /** 同实体确认组（B2 写路径联动；空数组=不在任何确认组）。 */
    同实体确认: number[];
    /** 九宫格粗定位；旧 sidecar/缺值 → null（前端描述首句常驻+「未能标注」）。 */
    画面位置: HubCardPosition | null;
    定位: string;
    时段来源: string;
};

/** 意图建议（AT-9 B1，复用 intent.py 解析；清单每行一条）。 */
export type HubSuggestion = {
    n: number;
    /** 必须换 | 建议准备 | 可沿用；待核对行恒空。 */
    档位: string;
    理由: string;
    镜次: number[];
    /** 缺文件/解析失败/行号不匹配 → true 且档位恒空（禁自动归可沿用）。 */
    待核对: boolean;
};

/** POST /api/hub/asset 的返回（写清单后回显真源列；该行在确认组里 → 组联动回显全组行）。 */
export type HubAssetWriteResult = {
    ok?: boolean;
    task_id?: string;
    n?: number;
    已传图?: string[];
    命名?: string;
    /** 组写入时：组内行号列表 + 全组行回显。 */
    组?: number[];
    rows?: { n: number; 已传图: string[]; 命名: string }[];
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
    /** 意图建议（AT-9 B1）；旧 bridge 无此字段 → 归一为 [] → 前端显式「尚无建议·待核对」。 */
    suggestions: HubSuggestion[];
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
    const rows = Array.isArray(data) ? data : Array.isArray(data.tasks) ? data.tasks : [];
    return rows.map((row) => ({
        ...row,
        // AT-9 B1 摘要三字段：旧 bridge 缺 → 归一兜底（计算式字段，缺了就空串，表格列自己再兜）。
        task_name: hubText(row.task_name) || row.local_id,
        stage: hubText(row.stage),
        replaced: hubText(row.replaced),
        bridge_id: hubText(row.bridge_id) || undefined,
    }));
}

export async function getHubTask(localId: string): Promise<HubTaskDetail> {
    // 合同是 {task:{...}}；同时兼容桥端直接回扁平对象，避免前后端并行期互相卡死。
    const data = await request<{ task?: HubTaskDetail } & Partial<HubTaskDetail>>(`/api/hub/task/${encodeURIComponent(localId)}`);
    const task = (data.task || (data as unknown as HubTaskDetail)) as HubTaskDetail;
    if (!task || !task.local_id) throw new Error("任务详情为空");
    return {
        ...task,
        pair_lines: Array.isArray(task.pair_lines) ? task.pair_lines : [],
        asset_cards: normalizeAssetCards(task.asset_cards),
        suggestions: normalizeSuggestions(task.suggestions),
    };
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
        const group = Array.isArray(item["同实体确认"]) ? (item["同实体确认"] as unknown[]) : [];
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
            同实体确认: group.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
            画面位置: normalizePosition(item["画面位置"]),
            定位: hubText(item["定位"]),
            时段来源: hubText(item["时段来源"]),
        };
        return card;
    });
}

/** 画面位置 {格,镜次}；缺值/畸形（含旧任务 null）→ null，前端按「未能标注」兜底。 */
function normalizePosition(value: unknown): HubCardPosition | null {
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    const cell = Number(raw["格"]);
    const shot = Number(raw["镜次"]);
    if (!Number.isFinite(cell) || cell < 1 || cell > 9) return null;
    return { 格: Math.round(cell), 镜次: Number.isFinite(shot) && shot > 0 ? Math.round(shot) : 0 };
}

/** 建议归一：非数组 → []（前端显式「尚无建议·待核对」，不自动归可沿用）。 */
function normalizeSuggestions(value: unknown): HubSuggestion[] {
    if (!Array.isArray(value)) return [];
    return value.map((raw) => {
        const item = (raw && typeof raw === "object" ? raw : {}) as JsonRecord;
        const shots = Array.isArray(item["镜次"]) ? (item["镜次"] as unknown[]) : [];
        return {
            n: Number(item.n) || 0,
            档位: hubText(item["档位"]),
            理由: hubText(item["理由"]),
            镜次: shots.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
            待核对: Boolean(item["待核对"]),
        };
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

/**
 * 确认置换 → 后端直接推进 rendering（AT-9 §四：前端零执行责任，确认后前端只关界面）。
 * empty_confirm=true = 全沿用（空置换显式确认；无任何上传时唯一放行通道，replace.py 硬拒空确认）。
 */
export async function confirmHubReplace(localId: string, emptyConfirm = false): Promise<{ state?: string; 全沿用?: boolean }> {
    return request<{ state?: string; 全沿用?: boolean }>(`/api/hub/confirm/${encodeURIComponent(localId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ empty_confirm: emptyConfirm }),
    });
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

// ---------------------------------------------------------------- AT-9 B2 任务级端点

/**
 * 删除任务（AT-9 §六删除合同）。前端须按状态预检（本函数只管发请求，错误原文透出）：
 *  - ingested/failed/done 免确认；awaiting_assets/awaiting_approval 必带 confirm_name（任务号或完整任务名）；
 *  - 运行中（ingesting/discussing/rendering/generating）与排队 JOB → 桥端 409。
 */
export async function deleteHubTask(localId: string, confirmName?: string): Promise<{ ok?: boolean; deleted?: string; state_was?: string }> {
    return request<{ ok?: boolean; deleted?: string; state_was?: string }>(`/api/hub/task/${encodeURIComponent(localId)}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_name: confirmName ?? null }),
    });
}

/** 退回置换（受控回退边）：awaiting_approval → awaiting_assets，丢弃当前提示词。 */
export async function rollbackHubTask(localId: string): Promise<{ state?: string; note?: string }> {
    return request<{ state?: string; note?: string }>(`/api/hub/task/${encodeURIComponent(localId)}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
    });
}

/** 成组（同实体确认组，≥2 行；组内一传全组联动）。rows=清单行号。 */
export async function groupHubRows(localId: string, rows: number[]): Promise<{ ok?: boolean; 组?: number[][] }> {
    return request<{ ok?: boolean; 组?: number[][] }>(`/api/hub/task/${encodeURIComponent(localId)}/group`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
    });
}

/** 解组：把行从所在确认组摘出。rows=清单行号。 */
export async function ungroupHubRows(localId: string, rows: number[]): Promise<{ ok?: boolean; 组?: number[][] }> {
    return request<{ ok?: boolean; 组?: number[][] }>(`/api/hub/task/${encodeURIComponent(localId)}/ungroup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
    });
}
