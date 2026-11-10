/**
 * 自动化工作台（/api/hub/*）接口层。
 *
 * 合同与 bridge 端 hub_api.py 对齐：
 * - 列表只回摘要，详情按需拉；
 * - ingest 只建任务，拆解由前端接着 POST /api/lapian/run 触发；
 * - 所有请求走同域相对路径（vite proxy / 隧道统一转发到 bridge）。
 */

import type { IngestOutcome } from "@/lib/hub/ingest-hit";

export type HubTaskState = "ingested" | "discussing" | "awaiting_assets" | "rendering" | "awaiting_approval" | "approved" | "generating" | "done" | "failed";

/** 摄取（建任务）结果形状：AT-10 C5 起后端带回 hit_existing/result/video_name。 */
export type { IngestOutcome };

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

/**
 * 资产卡代表帧（AT-10 C1 数据合同 {url, 秒, 镜次, 定位}）：
 * 定位=精确|未定位；未定位时镜次=null、不画框不标镜次。
 */
export type HubCardRepresentativeFrame = {
    url: string;
    秒: number;
    镜次: number | null;
    定位: string;
    来源?: string;
    错误?: string;
};

/** 帧内框（AT-10 C2 数据合同）：某帧所属镜的框集合中的一框；本卡=true 表示属于当前卡。 */
export type HubCardBox = {
    行: number;
    短名: string;
    /** 该行「建议」列原文（与 AT-8 卡片角标同源）。 */
    角标: string;
    格: number;
    "x%": number;
    "y%": number;
    "w%": number;
    "h%": number;
    本卡: boolean;
};

/** AT-11 M1 组卡投影（§3.1.2 单帧单框）：成员卡挂同一对象；无组 → null。 */
export type HubCardGroup = {
    /** 组键（成员升序连字，如 "1-5"，= sidecar『组元数据』的键）。 */
    键: string;
    成员: number[];
    /** 主行（『组元数据』落盘值；无 → 成员最小行）。代表帧/单框同源自主行。 */
    主行: number;
    /** 『组元数据』组名（确认窗可改；空=未命名）。 */
    组名: string;
    代表帧: HubCardRepresentativeFrame;
    /** 主行自身框（单帧单框）；null+单框降级=true = 主行未定位（诚实降级，不搬他人框）。 */
    单框: HubCardBox | null;
    单框降级: boolean;
    /** 成员位置键镜次 ∪ 意图建议镜次（升序）。 */
    镜次并集: number[];
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
    /** 资产卡代表帧（AT-10 C1 合同 {url,秒,镜次,定位}；未定位时镜次=null、不画框不标镜次）。 */
    代表帧: HubCardRepresentativeFrame;
    /** AT-10 C2：本行位置键正指代表帧所属镜 ∧ 本行框确实生成（恒等于「帧内框有本卡框」）。 */
    自身框: boolean;
    /** AT-10 C2 帧内框集合：该帧所属镜的全部框，同帧多卡共享同一集合、仅 本卡 标记不同。 */
    帧内框: HubCardBox[];
    已传图: string[];
    描述: string;
    同实体疑似: number[];
    /** 同实体确认组（B2 写路径联动；空数组=不在任何确认组）。 */
    同实体确认: number[];
    /** 九宫格粗定位；旧 sidecar/缺值 → null（前端描述首句常驻+「未能标注」）。 */
    画面位置: HubCardPosition | null;
    定位: string;
    时段来源: string;
    /** AT-11 M1 组卡投影；无组 → null。 */
    组: HubCardGroup | null;
    /** AT-11/AT-12 行决策覆盖：keep_origin（旧·按原片）/ dismissed（已移除·按原片）或 ""（无覆盖）。 */
    决策: string;
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
    /** AT-11/AT-12 行决策回显：keep_origin | dismissed | restore + 全量覆盖表 + 锁点剩余必换行。 */
    决策?: string;
    覆盖?: Record<string, string>;
    锁点剩余?: number[];
};

/** 自检结果单条（AT-9 B5 数据合同：projects/<id>/自检.json 每条 {项,结论,证据}；结论 PASS|FAIL）。 */
export type HubSelfCheckItem = {
    项: string;
    结论: string;
    证据: string;
};

export type HubTaskDetail = HubTaskSummary & {
    summary_md: string;
    checklist_md: string;
    prompt_md: string;
    /** AT-9 B5 自检三项（渲染完成自动跑）；旧任务无文件 → null → 前端显「未检查」。 */
    自检?: HubSelfCheckItem[] | null;
    /** 拆解实时阶段（bridge job stage 回写）；拆解中卡片显示。 */
    lapian_stage?: string;
    /** 拆解日志尾 15 行（排查留痕）。 */
    log_tail?: string;
    pair_lines: HubPairLine[];
    /** 资产卡（数据合同 v3）；旧 bridge 无此字段 → 归一为 [] → 前端退化回文字配对表。 */
    asset_cards: HubAssetCard[];
    /** AT-12 N1：按原片行（keep_origin ∪ dismissed，置换区折叠归此）；旧 bridge 缺 → 前端由卡决策派生。 */
    按原片行?: number[];
    /** AT-12 N1：已移除行（dismissed，不需要替换）；旧 bridge 缺 → 前端由卡决策派生。 */
    已移除行?: number[];
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
        自检: normalizeSelfCheck(task.自检),
    };
}

/** 自检归一：无文件/畸形 → null（「未检查」）；条目缺字段逐项兜底，结论非 PASS 一律按 ⚠ 显示。 */
function normalizeSelfCheck(value: unknown): HubSelfCheckItem[] | null {
    if (!Array.isArray(value) || !value.length) return null;
    return value.map((raw) => {
        const item = (raw && typeof raw === "object" ? raw : {}) as JsonRecord;
        return {
            项: hubText(item["项"]) || "未命名检查项",
            结论: hubText(item["结论"]) || "FAIL",
            证据: hubText(item["证据"]),
        };
    });
}

/** 代表帧归一（AT-10 C1 合同）：url 文本、秒 有限数字否则 0、镜次 正整数否则 null、定位 文本；缺 → 未定位兜底（旧 bridge 不炸页面）。 */
function normalizeRepresentativeFrame(value: unknown): HubCardRepresentativeFrame {
    const raw = (value && typeof value === "object" ? value : {}) as JsonRecord;
    const shot = Number(raw["镜次"]);
    const frame: HubCardRepresentativeFrame = {
        url: hubText(raw.url),
        秒: typeof raw["秒"] === "number" && Number.isFinite(raw["秒"]) ? raw["秒"] : 0,
        镜次: Number.isFinite(shot) && Number.isInteger(shot) && shot > 0 ? shot : null,
        定位: hubText(raw["定位"]) || "未定位",
    };
    const source = hubText(raw["来源"]);
    if (source) frame.来源 = source;
    if (raw["错误"] !== undefined && raw["错误"] !== null) frame.错误 = hubText(raw["错误"]);
    return frame;
}

/** 帧内框归一（AT-10 C2 合同）：数值字段有限数字兜底 0、本卡 Boolean；格 非 1-9 的框丢弃（前端不画坏框）。 */
function normalizeBox(value: unknown): HubCardBox | null {
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    const cell = Number(raw["格"]);
    if (!Number.isFinite(cell) || cell < 1 || cell > 9) return null;
    const num = (key: string): number => {
        const n = Number(raw[key]);
        return typeof raw[key] === "number" && Number.isFinite(n) ? n : 0;
    };
    return {
        行: num("行"),
        短名: hubText(raw["短名"]),
        角标: hubText(raw["角标"]),
        格: Math.round(cell),
        "x%": num("x%"),
        "y%": num("y%"),
        "w%": num("w%"),
        "h%": num("h%"),
        本卡: Boolean(raw["本卡"]),
    };
}

/** AT-11 组卡投影归一：非对象 → null；单框逐字段复用 normalizeBox；畸形成员丢弃不猜。 */
function normalizeGroup(value: unknown): HubCardGroup | null {
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    const primary = Number(raw["主行"]);
    const frame = normalizeRepresentativeFrame(raw["代表帧"]);
    const members = Array.isArray(raw["成员"])
        ? (raw["成员"] as unknown[]).map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0)
        : [];
    if (!members.length || !Number.isFinite(primary) || primary <= 0) return null;
    const box = normalizeBox(raw["单框"]);
    const shots = Array.isArray(raw["镜次并集"])
        ? (raw["镜次并集"] as unknown[]).map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0)
        : [];
    return {
        键: hubText(raw["键"]),
        成员: members,
        主行: Math.round(primary),
        组名: hubText(raw["组名"]),
        代表帧: frame,
        单框: box,
        单框降级: Boolean(raw["单框降级"]),
        镜次并集: [...new Set(shots)].sort((a, b) => a - b),
    };
}

/**
 * 读层入口集中归一（项目硬约束 14 同款思路）：asset_cards 缺失/畸形不炸页面，逐字段兜底。
 * export 供探针做「真实后端包 → normalize」字段保真核验（AT-10 C2），生产调用点不变。
 */
export function normalizeAssetCards(value: unknown): HubAssetCard[] {
    if (!Array.isArray(value)) return [];
    return value.map((raw) => {
        const item = (raw && typeof raw === "object" ? raw : {}) as JsonRecord;
        const spans = Array.isArray(item["出现时段"]) ? (item["出现时段"] as unknown[]) : [];
        const frames = Array.isArray(item["帧"]) ? (item["帧"] as unknown[]) : [];
        const boxes = Array.isArray(item["帧内框"]) ? (item["帧内框"] as unknown[]) : [];
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
            // AT-10 C2 新字段逐字段归一透传（旧 normalize 逐字段构造会丢未知键——C2 修的字段保真断链）
            代表帧: normalizeRepresentativeFrame(item["代表帧"]),
            自身框: Boolean(item["自身框"]),
            帧内框: boxes
                .map((box) => normalizeBox(box))
                .filter((box): box is HubCardBox => box !== null),
            已传图: urls.map((url) => hubText(url)).filter(Boolean),
            描述: hubText(item["描述"]),
            同实体疑似: twins.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
            同实体确认: group.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
            画面位置: normalizePosition(item["画面位置"]),
            定位: hubText(item["定位"]),
            时段来源: hubText(item["时段来源"]),
            // AT-11 M1 新键逐字段透传（防逐字段构造丢新键——同 C2 教训）
            组: normalizeGroup(item["组"]),
            决策: hubText(item["决策"]),
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

export async function ingestVideo(video: File, intent: string): Promise<IngestOutcome> {
    const body = new FormData();
    body.append("video", video);
    body.append("intent", intent);
    return request<IngestOutcome>("/api/hub/ingest", { method: "POST", body });
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
 * 确认置换 → 后端直接推进 rendering 并后台续跑渲染（AT-9 §四/§五：前端零执行责任，确认后前端只关界面）。
 * empty_confirm=true = 全沿用（空置换显式确认；无任何上传时唯一放行通道，replace.py 硬拒空确认）。
 * ackRows = 确认时点「知道，按原样」勾选的未换行号（自检③锁点对账的确认记录）。
 */
export async function confirmHubReplace(localId: string, emptyConfirm = false, ackRows: number[] = []): Promise<{ state?: string; 全沿用?: boolean; job_id?: string }> {
    return request<{ state?: string; 全沿用?: boolean; job_id?: string }>(`/api/hub/confirm/${encodeURIComponent(localId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ empty_confirm: emptyConfirm, 按原样: ackRows }),
    });
}

/**
 * 置换台逐卡定向写（POST /api/hub/asset，数据合同 v3 写合同）：
 *  - {n, image_url} 换图（重传覆盖）；image_url 必须是公网 HTTPS（先走 uploadHubImage）
 *  - {n, clear:true} 「沿用」= 清空该行绑定
 *  - {n, text}       品牌视觉卡的文字，写清单「命名」列
 *  - {n, decision}   AT-11/AT-12 行决策：keep_origin（旧·按原片）/ dismissed（移除，不需要替换）/
 *                    restore（恢复，后端清键），与上三者互斥（单行降级/移除用）
 */
export async function postHubAsset(localId: string, payload: { n: number; image_url?: string; clear?: boolean; text?: string; decision?: string }): Promise<HubAssetWriteResult> {
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

/** failed 态续跑入口（AT-9 B5：POST /api/hub/task/<id>/retry；非 failed 桥端 409，错误原文透出）。 */
export async function retryHubTask(localId: string): Promise<{ state?: string; job_id?: string }> {
    return request<{ state?: string; job_id?: string }>(`/api/hub/task/${encodeURIComponent(localId)}/retry`, { method: "POST" });
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

/**
 * 成组（同实体确认组，≥2 行；组内一传全组联动）。rows=清单行号。
 * AT-11 M2 确认小窗：可选主行/组名 → 后端落盘『组元数据』（缺省主行=最小行）。
 */
export async function groupHubRows(localId: string, rows: number[], opts?: { primary?: number; name?: string }): Promise<{ ok?: boolean; 组?: number[][]; 组元数据?: Record<string, { 主行?: number; 组名?: string }> }> {
    return request<{ ok?: boolean; 组?: number[][]; 组元数据?: Record<string, { 主行?: number; 组名?: string }> }>(`/api/hub/task/${encodeURIComponent(localId)}/group`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows, 主行: opts?.primary ?? null, 组名: opts?.name ?? "" }),
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
