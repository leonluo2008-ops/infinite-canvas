/**
 * 自动化工作台（/api/hub/*）接口层。
 *
 * 合同与 bridge 端 hub_api.py 对齐：
 * - 列表只回摘要，详情按需拉；
 * - ingest 只建任务，拆解由前端接着 POST /api/lapian/run 触发；
 * - 所有请求走同域相对路径（vite proxy / 隧道统一转发到 bridge）。
 */

import type { IngestOutcome } from "@/lib/hub/ingest-hit";
// AT-16 B5a-2：快捷入口的准入判定**复用**抽屉 submitOnly 用的同一套前置校验
// （genSubmitIssues / genResolveSize / genFamily / findGenChannel）——不另写第二套规则。
import { findGenChannel, genFamily, genResolveSize, genSubmitIssues, readApprovedSignature } from "@/lib/hub/gen-settings";
import type { GenChannelDesc, GenDraft, HubSubmitForm, HubSubmitView } from "@/lib/hub/gen-settings";

export type { HubSubmitForm, HubSubmitView };

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
    /**
     * AT-16 B4a 拆解双锚（读层真实透出；旧 bridge 缺 → 归一为三空串）。
     * 缺锚 = 计时不可用，**禁止**用 created_at / 日志尾行反推。
     */
    lapian?: HubLapianAnchors;
    /** 已换资产数 "已换/必换"（实时计算，如 "1/2"）。 */
    replaced?: string;
    has_pending_replace?: boolean;
    /**
     * AT-16 B5a-1 最小提交诊断 + 人话说明（后端 automation/submit_view.build 一处生产）。
     * 列表只靠摘要就能说清「提交到哪一步 / 下一步做什么」，不拉详情；
     * 旧 bridge 缺字段 → 归一成 {form:"none"}（不假装有结果）。
     */
    submit?: HubSubmitView;
};

/**
 * 拆解双锚（AT-16 B4a 透传合同）：
 * - started_at 拆解启动锚（job 开始时刻，重拆按新 job 重置）
 * - stage_at   当前阶段开始锚（阶段变化即重置）
 * - ended_at   终态冻结依据（成功/失败时刻；有值即停表）
 * 三值恒为字符串，缺/坏 → 空串；lapian_stage 仍是纯字符串（hubText 透传），
 * 时间戳走这三个独立字段，不从字符串解析。
 */
export type HubLapianAnchors = {
    started_at?: string;
    stage_at?: string;
    ended_at?: string;
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

/** 自检结果单条（AT-9 B5 数据合同：projects/<id>/自检.json 每条 {项,结论,证据}；结论 PASS|FAIL）。
 *  AT-16 B2：生产者（automation/selfcheck.py）追加「说明」——结论人话/原因/怎么处理/结构化发现；
 *  前端只渲染它，**不解析「证据」文字重判**（红线 25）。旧任务（如 auto-0042）没有「说明」→ 降级显示。 */
export type HubSelfCheckFinding = {
    /** 检查器自己发现的行号（没有 → null；前端不猜）。 */
    行号: number | null;
    /** 检查器自己发现的图号（没有 → null）。 */
    图号: number | null;
    文字: string;
};

export type HubSelfCheckExplain = {
    结论人话: string;
    原因: string;
    处理: string;
    发现: HubSelfCheckFinding[];
};

export type HubSelfCheckItem = {
    项: string;
    结论: string;
    证据: string;
    /** AT-16 B2 话术（生产者侧同源）；旧结构缺失 → undefined（读层降级，不改历史文件）。 */
    说明?: HubSelfCheckExplain;
};

/** 批准前音频门禁结果（AT-17 B2b）：服务端 generate.run_audio_gate 落任务文件、get_task 透出。
 *  verdict=FAIL 时「批准」会被拒（只拒批准，不回滚渲染）；发现=失败原因原文，处理=怎么修。 */
export type HubAudioGate = {
    verdict: string;
    audio_mode?: string;
    checks?: { id: string; verdict: string; msg: string }[];
    发现?: string[];
    处理?: string;
    at?: string;
};

export type HubTaskDetail = HubTaskSummary & {
    summary_md: string;
    checklist_md: string;
    prompt_md: string;
    /** AT-9 B5 自检三项（渲染完成自动跑）；旧任务无文件 → null → 前端显「未检查」。 */
    自检?: HubSelfCheckItem[] | null;
    /** 拆解实时阶段（bridge job stage 回写）；拆解中卡片显示。 */
    lapian_stage?: string;
    /** 批准前音频门禁结果（AT-17 B2b）；没跑过/老任务 → null（前端不显示，不当成通过）。 */
    audio_gate?: HubAudioGate | null;
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
    // ---- AT-13 B2/B3 提交链透出（§2.3/§2.4/§三；旧 bridge 缺 → 归一为空/false） ----
    /** 失败/未知原因原文（服务端已脱敏）；与 error 分开：error 是 failed 态的 history 尾注。 */
    gen_error?: string;
    /** 未知收单持久阻断标记（非状态）：{at,reason,dispatch}；未阻断 → null。 */
    submit_unknown?: HubSubmitUnknown | null;
    /** 提交意图阶段标记：prepared → dispatched → confirmed/cleared。 */
    dispatch?: HubDispatch | null;
    /** 云端任务号（wan=记录.json.taskId，SDGF=渠道 id）；续查/找回只认它。 */
    cloud_task_id?: string;
    /** 该任务是否有活跃提交占用（HUB_JOBS 运行中）——孤儿三步第①步。 */
    active_submit?: boolean;
    /** 疑似孤儿三步判定结果（服务端 diagnose，只判定不动状态）。 */
    submit_lost?: HubSubmitLost | null;
};

/** 提交意图标记（AT-13 §2.3；兼容早期扁平字符串写法）。 */
export type HubDispatch = { phase?: string; at?: string; note?: string; job?: string; carrier?: string };

/** 未知收单阻断标记（AT-13 §2.3）。 */
export type HubSubmitUnknown = { at?: string; reason?: string; dispatch?: string };

/** 疑似孤儿三步判定（AT-13 §2.4）。 */
export type HubSubmitLost = {
    state?: string;
    apiFormat?: string;
    active_submit?: boolean;
    cloud_task_id?: string;
    cloud_in_flight?: boolean;
    has_video?: boolean;
    dispatch?: string;
    submit_unknown?: boolean;
    /** 1=提交进行中（禁重试）/ 2=有云端 taskId 或成片（续查）/ 3=无在途证据（阻断+核对）。 */
    step?: number;
    submit_lost?: boolean;
    reason?: string;
};

/** approve 携带的画布配置（§2.1/§三；**无 key**）：批准时固化并算 v2 快照。 */
export type HubApproveConfig = {
    渠道身份: { baseUrl: string; apiFormat: string; 渠道名: string };
    model: string;
    mode: string;
    params: { duration: number; size: string; vquality: string; audio: boolean };
};

export type HubApproveResult = {
    state?: string;
    snapshot_version?: number;
    gen_params?: Record<string, unknown>;
    gen_channel?: Record<string, unknown>;
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
        stage: contractText(row.stage),
        replaced: hubText(row.replaced),
        bridge_id: hubText(row.bridge_id) || undefined,
        lapian: normalizeLapianAnchors(row.lapian),
        // 列表只靠摘要就能说清提交结果（5 秒轮询整表替换也不会把错误信息抹掉）
        submit: normalizeSubmitView(row.submit),
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
        // AT-13 B3：提交链字段逐字段归一（旧 bridge / 畸形值一律兜底，不让读层炸页面）
        gen_error: hubText(task.gen_error),
        submit_unknown: normalizeSubmitUnknown(task.submit_unknown),
        dispatch: normalizeDispatch(task.dispatch),
        cloud_task_id: hubText(task.cloud_task_id),
        active_submit: Boolean(task.active_submit),
        submit_lost: normalizeSubmitLost(task.submit_lost),
        lapian: normalizeLapianAnchors(task.lapian),
        // 与摘要同一条生产函数（_summary → submit_view.build）：详情头/抽屉顶部/列表三处同源
        submit: normalizeSubmitView(task.submit),
        // 阶段文案与锚同一条透传口径（异构历史值 → 空串，不出 repr）
        stage: contractText(task.stage),
        lapian_stage: contractText(task.lapian_stage),
    };
}

/**
 * 拆解双锚归一（AT-16 B4a）：只认字符串真值，缺/畸形一律空串——空串 = 「计时不可用」，
 * 读层（hub_api._lapian_anchors）与本函数同口径，两边都不造锚。
 */
export function normalizeLapianAnchors(value: unknown): HubLapianAnchors {
    const raw = (value && typeof value === "object" ? value : {}) as JsonRecord;
    return {
        started_at: contractText(raw.started_at),
        stage_at: contractText(raw.stage_at),
        ended_at: contractText(raw.ended_at),
    };
}

/**
 * 合同字符串归一（AT-16 B4a 透传口径）：只认字符串，其余（数字/对象/null）→ 空串。
 *
 * 与读层 `_stage_text()` 同口径：阶段文案与锚字段都是**合同字符串**，宁可空着（前端显示
 * 「计时不可用」/不显示阶段），也绝不把 `[object Object]` 这类 repr 糊到界面上。
 */
function contractText(value: unknown): string {
    return typeof value === "string" ? value.trim() : "";
}

/** 未知收单标记归一：只有真对象/true 才算（字符串/空 → null，UI 视为未阻断）。 */
function normalizeSubmitUnknown(value: unknown): HubSubmitUnknown | null {
    if (value === true) return { at: "", reason: "", dispatch: "" };
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    return { at: hubText(raw.at), reason: hubText(raw.reason), dispatch: hubText(raw.dispatch) };
}

/** 提交意图标记归一：兼容早期扁平字符串（"prepared"）→ {phase}。 */
function normalizeDispatch(value: unknown): HubDispatch | null {
    if (typeof value === "string") return value ? { phase: value } : null;
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    const phase = hubText(raw.phase);
    if (!phase) return null;
    return { phase, at: hubText(raw.at), note: hubText(raw.note), job: hubText(raw.job), carrier: hubText(raw.carrier) };
}

/** 孤儿三步判定归一：只保留 step 1-3 的合法值（其余 → null，UI 不摆假引导）。 */
function normalizeSubmitLost(value: unknown): HubSubmitLost | null {
    if (!value || typeof value !== "object") return null;
    const raw = value as JsonRecord;
    const step = Number(raw.step);
    const reason = hubText(raw.reason);
    if (!Number.isFinite(step) && !reason) return null;
    return {
        state: hubText(raw.state),
        apiFormat: hubText(raw.apiFormat),
        active_submit: Boolean(raw.active_submit),
        cloud_task_id: hubText(raw.cloud_task_id),
        cloud_in_flight: Boolean(raw.cloud_in_flight),
        has_video: Boolean(raw.has_video),
        dispatch: hubText(raw.dispatch),
        submit_unknown: Boolean(raw.submit_unknown),
        step: Number.isFinite(step) ? Math.round(step) : undefined,
        submit_lost: Boolean(raw.submit_lost),
        reason,
    };
}

/**
 * 提交结果诊断归一（AT-16 B5a-1）：只认后端 `submit_view.build` 的合法形态，
 * 形态不在四种之内（旧 bridge / 畸形值）→ 一律 `{form:"none"}`（不假装有提交结果）。
 * 文案数组逐项取非空字符串——空串/非字符串直接丢，不把 undefined 印到界面上。
 */
export function normalizeSubmitView(value: unknown): HubSubmitView {
    const empty: HubSubmitView = {
        form: "none",
        label: "",
        conclusion: "",
        causes: [],
        guidance: [],
        error_code: "",
        raw: "",
        dispatch_phase: "",
        submit_unknown: false,
        active_submit: false,
        has_gen_error: false,
        cloud_task_id: "",
        blocks_retry: false,
        can_resume: false,
    };
    if (!value || typeof value !== "object") return empty;
    const raw = value as JsonRecord;
    const form = hubText(raw.form).trim() as HubSubmitForm;
    const forms: HubSubmitForm[] = ["none", "unknown", "rejected", "inflight", "failed"];
    if (!forms.includes(form)) return empty;
    const texts = (input: unknown): string[] =>
        Array.isArray(input) ? input.map((item) => hubText(item).trim()).filter(Boolean) : [];
    return {
        form,
        label: hubText(raw.label).trim(),
        conclusion: hubText(raw.conclusion).trim(),
        causes: texts(raw.causes),
        guidance: texts(raw.guidance),
        error_code: hubText(raw.error_code).trim(),
        raw: hubText(raw.raw),
        dispatch_phase: hubText(raw.dispatch_phase).trim(),
        submit_unknown: Boolean(raw.submit_unknown),
        active_submit: Boolean(raw.active_submit),
        has_gen_error: Boolean(raw.has_gen_error),
        cloud_task_id: hubText(raw.cloud_task_id).trim(),
        blocks_retry: Boolean(raw.blocks_retry),
        can_resume: Boolean(raw.can_resume),
    };
}

/** 自检归一：无文件/畸形 → null（「未检查」）；条目缺字段逐项兜底，结论非 PASS 一律按 ⚠ 显示。
 *  AT-16 B2：生产者话术字段（说明）**原样透传**——不在这里读「证据」文字派生任何结论
 *  （红线 25）；缺说明（旧结构）→ undefined，由展示层降级。 */
export function normalizeSelfCheck(value: unknown): HubSelfCheckItem[] | null {
    if (!Array.isArray(value) || !value.length) return null;
    return value.map((raw) => {
        const item = (raw && typeof raw === "object" ? raw : {}) as JsonRecord;
        const row: HubSelfCheckItem = {
            项: hubText(item["项"]) || "未命名检查项",
            结论: hubText(item["结论"]) || "FAIL",
            证据: hubText(item["证据"]),
        };
        const 说明 = normalizeSelfCheckExplain(item["说明"]);
        if (说明) row.说明 = 说明;
        return row;
    });
}

/** 行号/图号归一：正整数才算（字符串 "3" 也收，畸形 → null；前端不猜数字）。 */
function selfCheckNo(value: unknown): number | null {
    const n = Number(value);
    return Number.isInteger(n) && n > 0 ? n : null;
}

/** 说明归一：只搬生产者字段 + 结构化发现；一个真字段都没有 → undefined（=旧结构）。 */
function normalizeSelfCheckExplain(value: unknown): HubSelfCheckExplain | undefined {
    if (!value || typeof value !== "object") return undefined;
    const raw = value as JsonRecord;
    const 发现: HubSelfCheckFinding[] = (Array.isArray(raw["发现"]) ? raw["发现"] : [])
        .map((entry) => (entry && typeof entry === "object" ? (entry as JsonRecord) : {}))
        .map((entry) => ({ 行号: selfCheckNo(entry["行号"]), 图号: selfCheckNo(entry["图号"]), 文字: hubText(entry["文字"]) }))
        .filter((entry) => entry.文字 || entry.行号 !== null || entry.图号 !== null);
    const explain: HubSelfCheckExplain = {
        结论人话: hubText(raw["结论人话"]),
        原因: hubText(raw["原因"]),
        处理: hubText(raw["处理"]),
        发现,
    };
    const hasText = explain.结论人话 || explain.原因 || explain.处理 || explain.发现.length;
    return hasText ? explain : undefined;
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

/** 本地引用串形态（与 automation/local_assets.py 的 REF_RE 逐字对齐）：local:任务号/asset-uploads/16位hex.扩展名 */
const LOCAL_REF_RE = /^local:[0-9A-Za-z_-]{1,64}\/asset-uploads\/[0-9a-f]{16}\.(?:png|jpg|jpeg|webp)$/;

export function isLocalAssetRef(value: string): boolean {
    return LOCAL_REF_RE.test(String(value || "").trim());
}

/** 本地引用 → 前端可直接 <img>/<a> 的服务端取图 URL；非本地引用原样返回（显示层唯一换算点）。 */
export function hubAssetDisplayUrl(value: string): string {
    const u = String(value || "").trim();
    const m = LOCAL_REF_RE.exec(u);
    if (!m) return u;
    const parts = u.slice("local:".length).split("/");
    return `/api/hub/asset-file/${encodeURIComponent(parts[0])}/${encodeURIComponent(parts[2])}`;
}

/** 受控出口视图（AT-16 B1a）：数据层 ref 原串 → 一份渲染属性。
 *
 *  img src 与大图 a href 同源取 `display`，`ref` 原串原样带回——健康态（AssetStateContext）
 *  按 ref 查键，**不**按换算后 URL 查（服务端 asset-probe 也同时回 ref/display 两键，两边一致）。
 *  全 drawer 只允许受控出口组件（task-detail-drawer.tsx 的 hub-asset-exit 标记区内）消费本视图。
 */
export type HubAssetView = { ref: string; display: string; src: string; href: string };

export function hubAssetRefView(value: string): HubAssetView {
    const ref = String(value || "").trim();
    const display = hubAssetDisplayUrl(ref);
    return { ref, display, src: display, href: display };
}

/**
 * 单图上传（POST /api/asset/upload）。
 *
 * AT-14 B3：带 taskId（X-Task-Id 头）→ 服务端只落本地引用、不碰图床；提交时才由后端装配成
 * 渠道要的形态。返回 ref 写进任务数据（pending_replace[].url），display 供 <img> 直接用。
 * 不带 taskId 或旧服务端回了公网直链 → ref=display=https 直链（向旧行为兼容，非静默降级：
 * source 如实带回，调用方按需展示）。
 */
export async function uploadHubImage(file: File, taskId?: string): Promise<{ ref: string; display: string; source: string }> {
    const name = encodeURIComponent(file.name || `ref-${Date.now()}.png`);
    const headers: Record<string, string> = { "X-Filename": name, "Content-Type": "application/octet-stream" };
    if (taskId) headers["X-Task-Id"] = encodeURIComponent(taskId);
    const data = await request<{ url?: string; local?: string; source?: string }>("/api/asset/upload", {
        method: "POST",
        headers,
        body: file,
    });
    const local = typeof data.local === "string" ? data.local.trim() : "";
    if (isLocalAssetRef(local)) return { ref: local, display: hubAssetDisplayUrl(local), source: "local" };
    const url = typeof data.url === "string" ? data.url : "";
    if (!/^https:\/\//.test(url)) throw new Error(`上传未返回本地引用或公网 HTTPS 直链：${JSON.stringify(data).slice(0, 120)}`);
    return { ref: url, display: url, source: typeof data.source === "string" ? data.source : "" };
}

/** 资产引用健康态（POST /api/hub/asset-probe/{id}）：ref=数据层原串，state=local|ok|expired|unknown，display=显示 URL。 */
export type HubAssetProbe = { ref: string; state: string; display: string };

export async function probeHubAssets(localId: string): Promise<HubAssetProbe[]> {
    const data = await request<{ refs?: unknown[] }>(`/api/hub/asset-probe/${encodeURIComponent(localId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
    });
    const rows = Array.isArray(data.refs) ? data.refs : [];
    return rows
        .map((raw) => (raw && typeof raw === "object" ? (raw as JsonRecord) : {}))
        .map((item) => ({
            ref: hubText(item.ref),
            state: hubText(item.state) || "unknown",
            display: hubText(item.display),
        }))
        .filter((item) => item.ref || item.display);
}

export async function renderHubTask(localId: string): Promise<{ state?: string; preview?: HubRenderPreview | null; prompt_md?: string }> {
    return request<{ state?: string; preview?: HubRenderPreview | null; prompt_md?: string }>(`/api/hub/render/${encodeURIComponent(localId)}`);
}

export async function approveHubTask(localId: string, config?: HubApproveConfig): Promise<HubApproveResult> {
    // AT-13 §2.2/§三：body 带 config（渠道身份/model/mode/params，**无 key**）→ 服务端固化 + v2 快照。
    // 不带 config 仍是旧语义（v1 快照，老任务/CLI）——但 B3 的 UI 一律带，改任一有效字段＝需重新批准。
    return request<HubApproveResult>(`/api/hub/approve/${encodeURIComponent(localId)}`, {
        method: "POST",
        ...(config
            ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config }) }
            : {}),
    });
}

export async function submitHubTask(localId: string, key?: string | null): Promise<{ job_id?: string }> {
    // AT-13 §三：key 一次性透传（不落盘、不进 argv/URL/日志）；wan 允许空 key。
    return request<{ job_id?: string }>(`/api/hub/submit/${encodeURIComponent(localId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: String(key || "").trim() }),
    });
}

/** 撤销批准（AT-13 §2.2/§2.4：approved → awaiting_approval 受控回退，改配置后重批用；不自动重批）。 */
export async function revokeHubTask(localId: string, note?: string): Promise<{ ok?: boolean; state?: string; note?: string }> {
    return request<{ ok?: boolean; state?: string; note?: string }>(`/api/hub/task/${encodeURIComponent(localId)}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: note || "撤销批准（改配置后重批）" }),
    });
}

/**
 * 解除「未知收单」阻断（AT-16 B5b ①；后端端点 **早已存在**：POST /api/hub/task/<id>/release
 * → hub_api.release_submit_unknown，人工动作必须 confirm:true）。
 *
 * 这里只是把既有端点接到界面上——**不新造机制**（红线 25）：阻断判定与清理仍在后端同一处，
 * 前端不猜结果、不清标记。调用方必须先让所有者到渠道控制台核对「没有这一单」。
 */
export async function releaseHubTask(localId: string, note?: string): Promise<{ ok?: boolean; state?: string; dispatch?: unknown }> {
    return request<{ ok?: boolean; state?: string; dispatch?: unknown }>(`/api/hub/task/${encodeURIComponent(localId)}/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            confirm: true,                                  // 人工动作：后端无 confirm 一律 400，前端永远显式带上
            note: note || "人工核对渠道无单后解除阻断",
        }),
    });
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

// ---------------------------------------------------------------- AT-16 B5a-2 共享重提 / 续查入口

/**
 * 快捷入口（AT-16 B5a-2，缺陷 D6 动作面）：列表行内 + 抽屉顶部同一份「重提 / 续查」。
 *
 * 红线 25：**不新造提交链**——动作仍是抽屉 submitOnly 那一条（POST /api/hub/submit，key 一次性
 * 透传），这里只把三件事收口成一份共享实现：
 *   ① `hubQuickAction`  纯函数：状态 + 摘要 + 画布渠道 + 批准快照 → 可点否 / 用哪条渠道的 key（探针直测）
 *   ② `runHubQuickAction` 执行：HTTP 受理只报「提交处理中」；state 转 generating/done 才报「云端已收单」，
 *      后端已给结论（submit.form = unknown / rejected / failed）则立刻按该形态收尾（AT-16 B10）
 *   ③ `hubQuickPhaseText` 阶段人话（两处入口同一份文案，禁各写一套 toast）
 * 两个入口（index.tsx 行内按钮、task-detail-drawer.tsx 顶部入口与底部 submitOnly）都只调这三个，
 * 所以「同承接（同一 API + 同一 key 解析）／同凭据（按批准渠道取 key）／同守卫（blocksRetry /
 * sizeBlocked / active）三个断言在代码上只有一份实现。
 */

/** 画布渠道最小形状（结构类型：接口层不 import store 类型，渠道映射由 hubQuickChannels 一处做）。 */
export type HubQuickChannel = { id: string; name: string; baseUrl: string; apiFormat: string; apiKey: string };

/**
 * 画布 AiConfig.channels → 快捷入口用的渠道表（缺字段一律空串，不猜）。
 *
 * 只取身份与 key；模型候选不走这里（AT-14 §3.2 同源规则不变）。
 */
export function hubQuickChannels(config: { channels?: unknown } | null | undefined): HubQuickChannel[] {
    const rows = config && Array.isArray(config.channels) ? (config.channels as unknown[]) : [];
    return rows
        .map((raw) => (raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {}))
        .map((row) => ({
            id: hubText(row.id).trim(),
            name: hubText(row.name).trim(),
            baseUrl: hubText(row.baseUrl).trim(),
            apiFormat: hubText(row.apiFormat).trim(),
            apiKey: hubText(row.apiKey).trim(),
        }))
        .filter((row) => row.id);
}

/** 快捷入口动作：submit=重提（提交生成）/ resume=按 taskId 续查（只查不重提）/ none=此任务没有入口。 */
export type HubQuickKind = "none" | "submit" | "resume";

export type HubQuickAction = {
    kind: HubQuickKind;
    /** 可点否（false 时按钮禁用，原因见 reason——不空发、不静默） */
    enabled: boolean;
    label: string;
    /** 可点时的补充说明（渠道来源等，空=无需说明） */
    hint: string;
    /** 不可点的原因（人话，含真实配置入口指引） */
    reason: string;
    channelId: string;
    channelName: string;
    /** 凭据来源：approved=批准时固化的渠道（真源）/ draft=本机任务草稿 / none=都没对上 */
    source: "approved" | "draft" | "none";
    /** 一次性提交 key（wan 允许空串；缺 key 时 enabled=false 且 needsConfig=true） */
    key: string;
    /** 缺渠道/缺 key/配置不合法 → 必须先到画布「设置 → 渠道」或详情改配置（禁止空发） */
    needsConfig: boolean;
    /** 当前画幅渠道不生效（与抽屉 sizeBlocked 同源：genResolveSize） */
    sizeBlocked: boolean;
    cloudTaskId: string;
};

export type HubQuickInput = {
    localId: string;
    state: string;
    /** 摘要/详情同一份提交诊断（B5a-1 的 submit 字段）；旧 bridge 缺 → null */
    submit?: HubSubmitView | null;
    /** 详情透出的云端 taskId（列表摘要里用 submit.cloud_task_id） */
    cloudTaskId?: string;
    /** 画布渠道表（hubQuickChannels(config)） */
    channels: HubQuickChannel[];
    /** 批准时点签名（readApprovedSignature(localId)）——「按批准渠道匹配」的真源 */
    approvedSignature?: string;
    /** 本机任务草稿（readGenDraft(localId)） */
    draft?: GenDraft | null;
    /** 本 UI 上一次点击还没结束 */
    busy?: boolean;
};

/** 批准快照（genDraftSignature 落盘的那份 JSON）：渠道身份 + 模型 + 参数，**不含 key**。 */
type ApprovedSnapshot = { channelId: string; baseUrl: string; apiFormat: string; name: string; draft: GenDraft };

function approvedSnapshotOf(raw: string | undefined): ApprovedSnapshot | null {
    const text = String(raw || "").trim();
    if (!text) return null;
    let value: unknown = null;
    try {
        value = JSON.parse(text);
    } catch {
        return null;                                  // 畸形签名 = 没有批准基线（不拿它猜渠道）
    }
    if (!value || typeof value !== "object") return null;
    const row = value as Record<string, unknown>;
    const params = (row.params && typeof row.params === "object" ? row.params : {}) as Record<string, unknown>;
    const channelId = hubText(row.channelId).trim();
    const baseUrl = hubText(row.baseUrl).trim();
    const apiFormat = hubText(row.apiFormat).trim();
    const name = hubText(row["渠道名"]).trim();
    if (!channelId && !name) return null;
    const duration = Number(params.duration);
    const mode = hubText(row.mode).trim();
    return {
        channelId,
        baseUrl,
        apiFormat,
        name,
        draft: {
            channelId,
            model: hubText(row.model).trim(),
            mode: mode === "reference" ? "reference" : mode === "frames" ? "frames" : "",
            size: hubText(params.size).trim() || "auto",
            rawSize: hubText(params.rawSize).trim(),
            vquality: hubText(params.vquality).trim() || "720",
            duration: Number.isFinite(duration) && duration > 0 ? Math.round(duration) : 0,
            audio: params.audio === undefined ? true : Boolean(params.audio),
        },
    };
}

/**
 * 按**批准渠道**匹配画布渠道：先按 id，id 变了再按批准时固化的身份三元组（渠道名 + baseUrl + apiFormat）。
 * 匹配不到 → null（由调用方给「去配置」的引导，**绝不**从全局默认挑一条顶上）。
 */
function matchApprovedChannel(channels: GenChannelDesc[], snap: ApprovedSnapshot): GenChannelDesc | null {
    if (snap.channelId) {
        const byId = findGenChannel(channels, snap.channelId);
        if (byId) return byId;
    }
    if (!snap.name) return null;
    return (
        channels.find(
            (channel) =>
                channel.name.trim() === snap.name &&
                channel.baseUrl.trim() === snap.baseUrl &&
                String(channel.apiFormat || "").trim().toLowerCase() === snap.apiFormat.trim().toLowerCase(),
        ) || null
    );
}

/**
 * 本机「批准时记下的配置基线」（AT-16 B5b ④ 无草稿边界用）：读 hub-gen-approved:<id> 那份签名。
 *
 * 用途只有一个——草稿丢了的时候**如实**告诉界面「这份是已批准配置，不是待改草稿」，
 * 而不是拿画布全局默认派生一份顶上冒充任务配置。返回 null = 本机确实没有基线（换过浏览器 /
 * 在命令行批的），调用方必须明说，不许假装读到快照。
 */
export function readApprovedDraft(localId: string): { draft: GenDraft; channelId: string; channelName: string; model: string } | null {
    const snap = approvedSnapshotOf(readApprovedSignature(localId));
    if (!snap) return null;
    return { draft: snap.draft, channelId: snap.channelId, channelName: snap.name, model: snap.draft.model };
}

const HUB_QUICK_EMPTY: Omit<HubQuickAction, "kind" | "label"> = {
    enabled: false,
    hint: "",
    reason: "",
    channelId: "",
    channelName: "",
    source: "none",
    key: "",
    needsConfig: false,
    sizeBlocked: false,
    cloudTaskId: "",
};

/**
 * 快捷入口准入判定（纯函数，探针直测；两个入口同一份）。
 *
 * 凭据口径（卡片红线）：取**草稿 / 批准渠道**的 key——批准快照优先（那是服务端真正固化的配置），
 * 没有批准基线才退本机草稿；**从不**按全局默认渠道（genDefaultChannelId）挑一条。
 * 守卫口径：active（提交在跑）/ blocks_retry（后端说禁重提）/ sizeBlocked（画幅渠道不接）/ 缺 key
 * 任一命中 → enabled=false + 人话原因；缺 key 的文案直接复用 genSubmitIssues 的生产者原文
 * （「到画布「设置 → 渠道」补上」），所以「缺 key → 引导配置、不空发」不是新写的一套话术。
 */
export function hubQuickAction(input: HubQuickInput): HubQuickAction {
    const state = String(input.state || "");
    const channels = (input.channels || []) as unknown as GenChannelDesc[];
    const submit = input.submit && input.submit.form !== "none" ? input.submit : null;
    const cloudTaskId = String(input.cloudTaskId || submit?.cloud_task_id || "").trim();
    const active = Boolean(submit?.active_submit);
    const busy = Boolean(input.busy) || hubQuickRunning(input.localId);

    // ① 凭据：批准快照优先（按批准渠道匹配），其次本机草稿；都没有 → source=none（不许猜）
    const snap = approvedSnapshotOf(input.approvedSignature);
    let source: HubQuickAction["source"] = "none";
    let channel: GenChannelDesc | null = null;
    let configDraft: GenDraft | null = null;
    if (snap) {
        source = "approved";                          // 有批准基线就只认它（哪怕没匹配上渠道，也不退草稿）
        channel = matchApprovedChannel(channels, snap);
        configDraft = snap.draft;
    } else if (input.draft && String(input.draft.channelId || "").trim()) {
        channel = findGenChannel(channels, String(input.draft.channelId).trim());
        configDraft = channel ? input.draft : null;
        source = channel ? "draft" : "none";
    }

    const family = channel ? genFamily(channel.apiFormat) : "";
    const key = family === "wan" ? "" : String(channel?.apiKey || "").trim();
    const sizeInfo = configDraft ? genResolveSize(configDraft.size, channel) : null;
    const sizeBlocked = Boolean(sizeInfo && (!sizeInfo.valid || !sizeInfo.accepted));

    // ② 动作种类：approved → 重提（提交生成）；有确定云端 taskId 才给续查（只查不重提）
    const canResume = submit ? Boolean(submit.can_resume) : state === "generating" && Boolean(cloudTaskId);
    let kind: HubQuickKind = "none";
    if (state === "approved") kind = "submit";
    else if (canResume && cloudTaskId) kind = "resume";
    if (kind === "none") return { ...HUB_QUICK_EMPTY, kind, label: "", reason: "", cloudTaskId };

    const label = kind === "submit" ? "提交生成" : "续查";
    const hint = channel
        ? `用${source === "approved" ? "批准时的渠道" : "任务草稿的渠道"}「${channel.name || channel.id}」${family === "wan" ? "（万相固定模型 · 允许空 key）" : "的 key"}提交（一次性透传，不落盘）`
        : "";

    // ③ 守卫（顺序=人先看最要紧的那条）：本 UI 在处理 → active → 后端禁重提 → 凭据/配置 → 画幅
    if (busy) {
        return { ...HUB_QUICK_EMPTY, kind, label, hint, reason: "提交处理中：上一次点击还在处理，别重复点（重复提交会重复扣费）", channelId: channel?.id || "", channelName: channel?.name || "", source, key, sizeBlocked, cloudTaskId };
    }
    if (active) {
        return { ...HUB_QUICK_EMPTY, kind, label, hint, reason: "这个任务已有提交在跑（排队 / 已发出等回执）——别重复点，等它出结果", channelId: channel?.id || "", channelName: channel?.name || "", source, key, sizeBlocked, cloudTaskId };
    }
    if (kind === "resume") {
        // 续查只查不重提：key/画幅都不影响（后端 generating 分支不创建），但必须有云端 taskId
        return { ...HUB_QUICK_EMPTY, kind, label, hint, enabled: true, sizeBlocked, channelId: channel?.id || "", channelName: channel?.name || "", source, key, cloudTaskId };
    }
    if (submit?.blocks_retry) {
        const why = submit.conclusion || submit.label || "提交被阻断";
        return { ...HUB_QUICK_EMPTY, kind, label, hint, reason: `${why}——按上面的指引核对 / 续查，解除阻断后才能重提`, channelId: channel?.id || "", channelName: channel?.name || "", source, key, sizeBlocked, cloudTaskId };
    }
    // 凭据来源：批准快照匹配不到渠道 / 本机根本没有批准基线 → 明确说清，不许拿「全局默认渠道」顶上
    if (!channel) {
        const reason = snap
            ? "批准时的渠道已不在画布配置里（被删或改了 id）——到画布「设置 → 渠道」恢复它，或撤销批准后重新挑渠道批准。"
            : "本机没有这条任务的批准配置（换过浏览器 / 在命令行批的）——先在这台浏览器里批准一次，或撤销批准后重新批准；别用别处的渠道 key 顶。";
        return {
            ...HUB_QUICK_EMPTY,
            kind,
            label,
            hint,
            reason,
            needsConfig: Boolean(snap),               // 渠道被删=真配置问题；本机无基线=要先重新批准
            source,
            key,
            sizeBlocked,
            cloudTaskId,
        };
    }
    const issues = genSubmitIssues(configDraft, channel, key);
    if (issues.length) {
        return {
            ...HUB_QUICK_EMPTY,
            kind,
            label,
            hint,
            reason: issues[0],
            needsConfig: true,
            channelId: channel.id,
            channelName: channel.name,
            source,
            key,
            sizeBlocked,
            cloudTaskId,
        };
    }
    return { ...HUB_QUICK_EMPTY, kind, label, hint, enabled: true, channelId: channel?.id || "", channelName: channel?.name || "", source, key, sizeBlocked, cloudTaskId };
}

// ---------------------------------------------------------------- 撤销重批起态门（AT-16 B5b ①/②/③/④）

/** 起态门形态：none=这条任务没有「撤销重批」这条路 / allowed=允许态（有阻断也没有）/ blocked=提交在跑 / unknown=结果未知。 */
export type HubReworkMode = "none" | "allowed" | "blocked" | "unknown";

/** 阻断种类（顺序与后端 hub_api._submit_block_kind 一致：active → dispatch → unknown；空=无阻断）。 */
export type HubReworkBlockKind = "" | "active" | "dispatch" | "unknown";

export type HubReworkGate = {
    mode: HubReworkMode;
    /** 可否展示并启用「撤销批准」（起态门**唯一**判据；未知态一律 false，不许引导撤销） */
    allowed: boolean;
    blockKind: HubReworkBlockKind;
    title: string;
    /** 人话原因 + 下一步（未知态明确说「先核对渠道」） */
    reason: string;
    /** 未知态才出现的「人工解除阻断」（既有 POST /api/hub/task/<id>/release，不是新机制） */
    showRelease: boolean;
    /** 未知态下是否给「按云端 taskId 续查」（只查不重提） */
    canResume: boolean;
    cloudTaskId: string;
    /** 允许态的三步顺序（撤销 → 改模型 → 批准提交）；其它形态为空 */
    steps: string[];
    /** 心血不作废承诺（允许态才给；与后端探针的逐字节断言同口径） */
    keep: string;
};

export type HubReworkInput = {
    state: string;
    /** B5a-1 的提交诊断（摘要/详情同源）；旧 bridge 缺 → 由下面三个兜底事实判定 */
    submit?: HubSubmitView | null;
    cloudTaskId?: string;
    /** 兜底：活跃提交（后端 _active_submit） */
    activeSubmit?: boolean;
    /** 兜底：未知收单标记 */
    submitUnknown?: boolean;
    /** 兜底：dispatch 阶段（prepared / dispatched = 结果未知） */
    dispatchPhase?: string;
};

const HUB_REWORK_NONE: HubReworkGate = {
    mode: "none",
    allowed: false,
    blockKind: "",
    title: "",
    reason: "",
    showRelease: false,
    canResume: false,
    cloudTaskId: "",
    steps: [],
    keep: "",
};

/** 允许态的三步（真机顺序：撤销 → 改模型 → 批准提交；非 awaiting_approval 态配置只读，所以必须先撤销）。 */
const HUB_REWORK_STEPS = [
    "①「撤销批准」：回到待批准（配置快照作废，配置从只读变可编辑）。",
    "② 换模型 / 渠道 / 参数：只在待批准态能改，改完即落成本任务草稿。",
    "③「批准出片」：用新配置重算快照 v2（版本仍是 2，不递增）并提交生成。",
];

/** 心血不作废口径（与 Python 探针逐字节断言一一对应，禁止只写一句安慰话）。 */
const HUB_REWORK_KEEP =
    "心血不作废：撤销批准只作废「配置快照」——资产确认 / 上传流水 / 置换确认 / 提示词.md 逐字节不动，行决策也不动；变的只有渠道 / 模型 / 参数和本次批准的快照 hash。";

/**
 * 撤销重批起态门（纯函数，探针直测；抽屉里撤销按钮与指引段**只**读它）。
 *
 * 判定源与后端同一套事实（B5a-1 的 submit 透出；缺字段用兜底事实），顺序也照后端
 * `hub_api._submit_block_kind`：active → dispatch → unknown。三者任一命中都**不引导撤销**
 * （撤销会连根清掉 approval + 提交链运行时键，等于绕过「未知收单先核对」防线）：
 *   · active   → 等结果，撤销按钮不出现
 *   · dispatch → 未知态：先核对渠道 → 「解除阻断」→ 需要时续查
 *   · unknown  → 同上（解除用的是既有 release 端点，不新造）
 * 允许态（approved 且无任何阻断）才给出「撤销 → 改模型 → 批准提交」三步 + 心血不作废承诺。
 * 非 approved（待批准 / 生成中 / 已完成…）→ none：待批准态配置本来就可编辑，没有要撤销的东西。
 */
export function hubReworkGate(input: HubReworkInput): HubReworkGate {
    const state = String(input.state || "");
    const submit = input.submit && input.submit.form !== "none" ? input.submit : null;
    const cloudTaskId = String(input.cloudTaskId || submit?.cloud_task_id || "").trim();

    if (state !== "approved") return { ...HUB_REWORK_NONE };

    const phase = String(submit?.dispatch_phase || input.dispatchPhase || "").trim();
    const active = submit ? Boolean(submit.active_submit) : Boolean(input.activeSubmit);
    const unknown = submit ? Boolean(submit.submit_unknown) : Boolean(input.submitUnknown);
    const dispatchStuck = phase === "prepared" || phase === "dispatched";

    if (active) {
        return {
            ...HUB_REWORK_NONE,
            mode: "blocked",
            blockKind: "active",
            title: "提交在跑：等结果，这时不能撤销批准",
            reason: "这个任务有提交在排队 / 执行中——撤销会和正在跑的提交抢配置。等它出结果（成片或失败红条）再改配置。",
            cloudTaskId,
        };
    }
    if (dispatchStuck) {
        return {
            ...HUB_REWORK_NONE,
            mode: "unknown",
            blockKind: "dispatch",
            title: `结果未知（dispatch=${phase || "未完结"}）：先核对渠道，别撤销批准`,
            reason: "上次提交没拿到确定回执，云端可能已收单——撤销批准会把「未知收单」标记一起清掉，可能重复扣费。"
                + "先到渠道控制台按时间 / 提示词核对有没有这一单。",
            showRelease: true,
            canResume: Boolean(cloudTaskId),
            cloudTaskId,
        };
    }
    if (unknown) {
        return {
            ...HUB_REWORK_NONE,
            mode: "unknown",
            blockKind: "unknown",
            title: "提交结果未知：先核对渠道，别撤销批准",
            reason: `${submit?.conclusion || "云端可能已收单，不能当作没提交"}——撤销批准会连「未知收单」标记一起清掉，可能重复扣费。`,
            showRelease: true,
            canResume: Boolean(cloudTaskId),
            cloudTaskId,
        };
    }
    return {
        mode: "allowed",
        allowed: true,
        blockKind: "",
        title: "要换模型 / 渠道？按这个顺序来（资产与提示词不会作废）",
        reason: "",
        showRelease: false,
        canResume: false,
        cloudTaskId,
        steps: HUB_REWORK_STEPS,
        keep: HUB_REWORK_KEEP,
    };
}

/** 未知态的三步指引（核对 → 解除 → 续查/再撤销）；允许态用 HUB_REWORK_STEPS。 */
export function hubReworkUnknownSteps(gate: HubReworkGate): string[] {
    if (gate.mode !== "unknown" && gate.mode !== "blocked") return [];
    if (gate.mode === "blocked") return ["进度 5 秒自动刷新；结果出来（成片 / 失败）后再改配置。", "这期间撤销批准与重提都不可用。"];
    return [
        "到渠道控制台按时间 / 提示词核对有没有这一单（只读核对，不许直接重提）。",
        gate.canResume ? `核对到单 → 按云端 taskId 续查（只查不重提）。` : "核对到单 → 先拿到云端 taskId，再按该 id 续查。",
        "确认渠道确实没有这一单 → 点「已核对，解除阻断」；解除后配置才会重新开放编辑（换模型就先改配置，再批准）。",
    ];
}

/** 正在处理的快捷入口（同一任务第二次点击直接拒，不发第二个请求）。 */
const HUB_QUICK_RUNNING = new Set<string>();

export function hubQuickRunning(localId: string): boolean {
    return HUB_QUICK_RUNNING.has(String(localId || ""));
}

/**
 * 执行阶段（AT-16 B10 起五态）：
 *   accepted   HTTP 受理（请求接住了，不等于云端收单）
 *   confirmed  state 自己推进了（generating / done / failed）
 *   concluded  后端已给结论的形态（unknown / rejected / failed）→ 按该形态立刻收尾，不再空跑
 *   timeout    真在途：state 没推进、submit 也没有结论（既有文案不动）
 *   rejected   入口不可用（准入判定没过 / 重复点击 / 无可用动作）——零请求
 */
export type HubQuickPhase = "accepted" | "confirmed" | "concluded" | "timeout" | "rejected";

/** 后端已经给出结论的三种提交形态（与 automation/submit_view.py 的 FORMS 同字面量）。 */
export type HubQuickConclusionForm = "unknown" | "rejected" | "failed";

export type HubQuickRunResult = {
    phase: HubQuickPhase;
    state: string;
    note: string;
    /** 仅 concluded 有：按哪种后端形态收的尾（unknown / rejected / failed）。 */
    form?: HubQuickConclusionForm;
};

/**
 * 提交形态 → 收尾结论（AT-16 B10 的**唯一判定点**，探针直测；两个入口不各写一套）。
 *
 * 病根（B8 施工中发现）：收尾只看 `state ∈ {generating, done, failed}`，而提交落到
 * unknown / rejected 时 `state` 仍停在 `approved` / `awaiting_approval` → 循环跑满
 * 20 轮 × 2s = 40 秒落 `timeout`，两处入口弹「提交仍在处理中」；此时主状态条（B8）早在
 * 2-5 秒内按后端 `label` 说了「结果未知·先核对」——同一任务同一时刻两块 UI 两套话。
 *
 * 判据只读后端 B5a-1 的 `submit`（摘要/详情同一生产源 `automation/submit_view.build`）：
 *   · 形态 ∈ {unknown, rejected, failed} → 立刻收尾，note **逐字**取后端 `conclusion`
 *     （缺则 `label`）；前端不新写劝退话（红线 25：判据与文案同源生产）
 *   · inflight（真在途）/ none（没提交过）/ 畸形值 → null，继续轮询（timeout 只留给它们）
 */
export function hubQuickSubmitConclusion(submit?: HubSubmitView | null): { form: HubQuickConclusionForm; note: string } | null {
    const view = normalizeSubmitView(submit);
    const form = view.form;
    if (form !== "unknown" && form !== "rejected" && form !== "failed") return null;
    return { form, note: view.conclusion || view.label };
}

/** 阶段人话（两处入口同一份；颜色由调用方按 level 映射到 message.xxx）。 */
export function hubQuickPhaseText(phase: HubQuickPhase, state = ""): { level: "success" | "info" | "warning"; text: string } {
    if (phase === "accepted") {
        // HTTP 200 只代表「服务端接住了请求」——不在这里说「云端已收单」（D6 的原病灶）
        return { level: "info", text: "提交处理中：请求已受理，等云端回执（确认收单后才算成功）" };
    }
    if (phase === "confirmed") {
        if (state === "failed") return { level: "warning", text: "云端已回执：失败——按详情原文处理（可重试）" };
        return { level: "success", text: state === "done" ? "云端已收单：成片已出（云端 taskId 已确认）" : "云端已收单：已转 generating，等成片" };
    }
    if (phase === "concluded") {
        // 文案不在前端生产：调用方用 note（= 后端 conclusion，见 hubQuickSubmitConclusion）覆盖；
        // 这里只定语气（要人处理 = warning）。拿不到后端结论时宁可不弹，也不编一句劝退话。
        return { level: "warning", text: "" };
    }
    if (phase === "timeout") {
        return { level: "info", text: "提交仍在处理中：还没转成 generating——前端超时 ≠ 云端失败，5 秒刷新会自己跟上；按 taskId 可续查" };
    }
    return { level: "warning", text: "" };            // rejected：由 reason 覆盖
}

export type HubQuickRunDeps = {
    /** 提交（默认=既有 submitHubTask，POST /api/hub/submit；续查也是它——后端 generating 分支只查不建） */
    submit?: (localId: string, key: string) => Promise<unknown>;
    /** 读任务（默认=getHubTask；轮询确认用——**带 submit**，收尾判定要读后端结论） */
    fetchTask?: (localId: string) => Promise<{ state?: string; submit?: HubSubmitView | null }>;
    sleep?: (ms: number) => Promise<void>;
    intervalMs?: number;
    maxPolls?: number;
    onPhase?: (phase: HubQuickPhase, state: string, note: string) => void;
};

/**
 * 执行快捷动作（两个入口共用这一条链）。
 *
 * 节奏是本次验收的核心：POST 返回 → 只报「提交处理中」（accepted）；再轮询详情，按两级收尾：
 *   ① state 自己推进到 generating / done → 报「云端已收单」；failed（且 submit 没给结论）→ 如实回执失败
 *   ② **后端已给结论**（submit.form ∈ unknown / rejected / failed，B5a-1 同一生产源）→ 立刻按该形态
 *      收尾（concluded），note 逐字取后端 conclusion——AT-16 B10：这几种形态下 state 停在
 *      approved / awaiting_approval，旧实现会白跑 40 秒后弹「仍在处理中」，与主状态条打架
 * 只有「state 没推进、submit 也没有结论」的真在途才落 timeout（文案不动）。
 * 重复点击：同任务在跑直接 rejected（不发第二个请求）；后端还有 active 409 兜底。
 * 提交失败（如 409 已有提交在跑）**原样抛出**，由调用方把后端原文透出（不吞、不美化）。
 */
export async function runHubQuickAction(localId: string, action: HubQuickAction, deps: HubQuickRunDeps = {}): Promise<HubQuickRunResult> {
    const say = deps.onPhase || (() => undefined);
    const reject = (note: string): HubQuickRunResult => {
        say("rejected", "", note);
        return { phase: "rejected", state: "", note };
    };
    if (action.kind === "none") return reject("这个任务现在没有可用的重提 / 续查入口");
    if (!action.enabled) return reject(action.reason || "这个入口现在不可用");
    if (HUB_QUICK_RUNNING.has(localId)) return reject("提交处理中：上一次点击还在处理，别重复点（重复提交会重复扣费）");

    HUB_QUICK_RUNNING.add(localId);
    const submit = deps.submit || ((id: string, key: string) => submitHubTask(id, key));
    const fetchTask = deps.fetchTask || ((id: string) => getHubTask(id));
    const sleep = deps.sleep || ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    const intervalMs = deps.intervalMs ?? 2000;
    // 真在途（state=generating、submit=inflight）仍按既有上限轮询——timeout 只留给它们
    const maxPolls = deps.maxPolls ?? 20;
    try {
        await submit(localId, action.key);            // 受理（HTTP 2xx）
        say("accepted", "", "");
        for (let i = 0; i < maxPolls; i += 1) {
            await sleep(intervalMs);
            let task: { state?: string; submit?: HubSubmitView | null } | null = null;
            try {
                task = await fetchTask(localId);
            } catch {
                continue;                             // 读失败不是终态：继续轮询，别把网络抖动说成「已收单」
            }
            const state = String(task?.state || "");
            // ① state 自己推进了（正向终态 / 生成中）——既有语义与文案一行不动
            if (state === "generating" || state === "done") {
                say("confirmed", state, "");
                return { phase: "confirmed", state, note: "" };
            }
            // ② 后端已给结论（unknown / rejected / failed）→ 立刻按该形态收尾，不再空跑到 40s。
            //    主状态条（B8）消费的是同一份 submit.label——toast 与状态条从此同一结论、同一文案源。
            const concluded = hubQuickSubmitConclusion(task?.submit);
            if (concluded) {
                say("concluded", state, concluded.note);
                return { phase: "concluded", state, note: concluded.note, form: concluded.form };
            }
            // ③ 明确失败但 submit 没给结论（如旧 bridge 缺字段）→ 沿用既有「云端已回执：失败」文案
            if (state === "failed") {
                say("confirmed", state, "");
                return { phase: "confirmed", state, note: "" };
            }
        }
        say("timeout", "", "");
        return { phase: "timeout", state: "", note: "" };
    } finally {
        HUB_QUICK_RUNNING.delete(localId);
    }
}
