/**
 * AT-13 B3 生成设置（§2.1 / §2.2 / §2.10 + §三 数据合同）——纯逻辑层。
 *
 * 只吃纯数据（渠道描述 / 草稿 / get_task 透传字段），不 import zustand / i18n / React：
 * 探针 probe-at13-b3.mjs 用 esbuild 打包本文件直接断言真值，不复制实现。
 *
 * 覆盖：
 *   ① 协议承接矩阵：SDGF 族 + wan 族可选；gemini/openai/模型脚本族＝「画布专用」禁选（不摆「选中必错路由」）
 *   ② wan 如实化：固定 wan3.0 · 自动多图参考；model / mode 控件不渲染（不摆无效控件）
 *   ③ 参数枚举同源：画幅取 media-size.ts 的 videoRatioOptions（含 21:9 / auto / WxH），
 *      「画布显示但渠道不接受」的值照列 + 显式提示（后端 400 文案透传）
 *   ④ approve 请求体（config，无 key）/ submit 请求体（key，一次性）
 *   ⑤ 草稿按「渠道 + 模型引用」存任务草稿，且不写全局 videoModel
 *   ⑥ 提交链阻断态（active_submit / submit_unknown / dispatch / 疑似孤儿三步）→ UI 引导文案
 */
import {
    clampVideoSeconds,
    computeVideoSize,
    inferVideoRatio,
    parseAspectRatio,
    parsePixelSize,
    parseVideoResolution,
    VIDEO_SECONDS_MAX,
    VIDEO_SECONDS_MIN,
    videoRatioOptions,
} from "@/lib/media-size";

// ---------------------------------------------------------------- 协议矩阵（真源：media-size.ts + bridge 接收域）

/** 渠道承接的协议族：sdgf / wan 可派发；其余（gemini/openai/模型脚本）＝画布专用禁选。 */
export type GenFamily = "sdgf" | "wan" | "canvas-only";

/** wan 固定模型语义（§2.1：不消费 model 选择，自动多图参考，无 mode）。 */
export const GEN_WAN_FIXED_NOTE = "固定 wan3.0 · 自动多图参考";
/** 禁选项标注（§2.1：标「画布专用」，不列「选中必错路由」项）。 */
export const GEN_CANVAS_ONLY_TAG = "画布专用";

/** 画布画幅全集（与 media-size.ts videoRatioOptions 同源，探针断言两者一致）。 */
export const GEN_CANVAS_RATIOS: string[] = videoRatioOptions.map((item) => item.value);
/** 渠道真实接收域（与 wanrepro/ui/hub_api.py 的 SDGF_RATIOS / WAN_RATIOS 同源；B4 合同锁一并锁死）。 */
export const GEN_SDGF_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"];
export const GEN_WAN_RATIOS = ["16:9", "9:16", "1:1"];
/** 清晰度（bridge SDGF_RESOLUTIONS；画布可选更高，提交时显式提示）。 */
export const GEN_VIDEO_RESOLUTIONS = ["480", "720", "1080"];
export const GEN_DURATION_MIN = VIDEO_SECONDS_MIN;
export const GEN_DURATION_MAX = VIDEO_SECONDS_MAX;

/** 渠道描述（由 AiConfig.channels 映射而来，保持 lib 与 store 解耦）。 */
export type GenChannelModelDesc = { name: string; capability: string; hasScript: boolean };
export type GenChannelDesc = {
    id: string;
    name: string;
    baseUrl: string;
    /** 渠道 key：只在 submit 时一次性透传，绝不进 approve 的 config 与草稿签名（§2.1/§2.8）。 */
    apiKey: string;
    apiFormat: string;
    models: GenChannelModelDesc[];
};

export function genFamily(apiFormat: string | undefined): GenFamily {
    const fmt = String(apiFormat || "").trim().toLowerCase();
    if (fmt === "sdgf") return "sdgf";
    if (fmt === "wan") return "wan";
    return "canvas-only";
}

/** 渠道下拉行：sdgf / wan 可选；其余禁选并标「画布专用」。 */
export type GenChannelRow = {
    id: string;
    name: string;
    apiFormat: string;
    family: GenFamily;
    selectable: boolean;
    note: string;
};

export function genChannelRows(channels: GenChannelDesc[]): GenChannelRow[] {
    return (channels || []).map((channel) => {
        const family = genFamily(channel.apiFormat);
        return {
            id: channel.id,
            name: channel.name,
            apiFormat: String(channel.apiFormat || ""),
            family,
            selectable: family !== "canvas-only",
            note: family === "canvas-only" ? `${GEN_CANVAS_ONLY_TAG}（hub 只承接 SDGF / 万相）` : "",
        };
    });
}

export function findGenChannel(channels: GenChannelDesc[], channelId: string | undefined): GenChannelDesc | null {
    if (!channelId) return null;
    return (channels || []).find((channel) => channel.id === channelId) || null;
}

/**
 * 默认渠道：优先「全局 videoModel 所在渠道」（且必须是 sdgf/wan），否则第一个 SDGF，再否则第一个万相。
 * 不在无配置时瞎猜 gemini/openai（那会是「选中必错路由」）。
 */
export function genDefaultChannelId(channels: GenChannelDesc[], preferredChannelId?: string): string {
    const preferred = findGenChannel(channels, preferredChannelId);
    if (preferred && genFamily(preferred.apiFormat) !== "canvas-only") return preferred.id;
    const sdgf = (channels || []).find((channel) => genFamily(channel.apiFormat) === "sdgf");
    if (sdgf) return sdgf.id;
    const wan = (channels || []).find((channel) => genFamily(channel.apiFormat) === "wan");
    return wan ? wan.id : "";
}

/** 模型行（仅当前渠道内）：视频能力模型全列；带模型脚本的标「画布专用（模型脚本）」禁选。 */
export type GenModelRow = { name: string; value: string; selectable: boolean; note: string };

export function genModelRows(channel: GenChannelDesc | null): GenModelRow[] {
    if (!channel) return [];
    if (genFamily(channel.apiFormat) !== "sdgf") return []; // wan＝固定模型；画布专用渠道不可选
    return (channel.models || [])
        .filter((model) => String(model.capability || "") === "video")
        .map((model) => ({
            name: model.name,
            value: `${channel.id}::${model.name}`,
            selectable: !model.hasScript,
            note: model.hasScript ? `${GEN_CANVAS_ONLY_TAG}（模型脚本）` : "",
        }));
}

/** wan 如实化：model 下拉与 mode 切换不渲染（不摆无效控件）。 */
export function genUiFlags(channel: GenChannelDesc | null): { showModel: boolean; showMode: boolean; fixedNote: string } {
    const family = channel ? genFamily(channel.apiFormat) : "sdgf";
    if (family === "wan") return { showModel: false, showMode: false, fixedNote: GEN_WAN_FIXED_NOTE };
    return { showModel: true, showMode: family === "sdgf", fixedNote: "" };
}

/** 画幅行（枚举同源 media-size.videoRatioOptions，含 21:9 / auto）：accepted=false 时照列 + 说明渠道不接。 */
export type GenRatioRow = { value: string; label: string; accepted: boolean; note: string };

export function genRatioRows(channel: GenChannelDesc | null): GenRatioRow[] {
    const family = channel ? genFamily(channel.apiFormat) : "sdgf";
    return GEN_CANVAS_RATIOS.map((value) => {
        const label = value === "auto" ? "自适应" : value;
        if (family === "wan") {
            return { value, label, accepted: GEN_WAN_RATIOS.includes(value), note: GEN_WAN_RATIOS.includes(value) ? "" : `万相只接 ${GEN_WAN_RATIOS.join(" / ")}` };
        }
        if (value === "auto") return { value, label, accepted: true, note: "" };
        const accepted = GEN_SDGF_RATIOS.includes(value);
        return { value, label, accepted, note: accepted ? "" : `${value} 属画布专用，SDGF 渠道不接（支持 ${GEN_SDGF_RATIOS.join("/")}/自适应）` };
    });
}

export function genResolutionRows(): { value: string; label: string; accepted: boolean }[] {
    return GEN_VIDEO_RESOLUTIONS.map((value) => ({ value, label: `${value}p`, accepted: true }));
}

/** 画幅是否被当前渠道接受（WxH 一律接受：bridge 按最大公约数归一成比例）。 */
export function genSizeAccepted(size: string, channel: GenChannelDesc | null): boolean {
    const value = String(size || "").trim();
    if (!value) return false;
    const pixels = parsePixelSize(value);
    if (pixels) return pixels.width > 0 && pixels.height > 0;
    const row = genRatioRows(channel).find((item) => item.value === value);
    return Boolean(row?.accepted);
}

export function genSizeNote(size: string, channel: GenChannelDesc | null): string {
    const value = String(size || "").trim();
    if (parsePixelSize(value)) {
        const parsed = parseAspectRatio(value);
        return parsed ? "" : `渠道按宽高最大公约数归一到比例（${value} 提交后按比例派发）`;
    }
    return genRatioRows(channel).find((item) => item.value === value)?.note || "";
}

/** 画布字面画幅 → 提交用 size：比例值照传（auto 由桥归一 adaptive），WxH 照传。 */
export function genSizeForSubmit(size: string, vquality: string): string {
    const value = String(size || "").trim();
    if (!value) return "auto";
    if (parsePixelSize(value)) return value;
    const ratio = inferVideoRatio(value);
    if (ratio === "auto") return "auto";
    return computeVideoSize(parseVideoResolution(vquality), ratio);
}

// ---------------------------------------------------------------- 草稿（按任务存，不写全局 videoModel）

export type GenDraft = {
    /** 渠道 id（画布 AiConfig.channels[].id）。 */
    channelId: string;
    /** 裸模型名（SDGF 必填；wan 恒空：固定 wan3.0）。 */
    model: string;
    /** frames | reference（仅 SDGF；wan 恒空）。 */
    mode: "" | "frames" | "reference";
    /** 画幅字面值：videoRatioOptions 之一，或 "WxH" 自定义。 */
    size: string;
    /** 清晰度（480 / 720 / 1080，画布可更高）。 */
    vquality: string;
    duration: number;
    audio: boolean;
};

export type GenDraftSeed = { videoSeconds?: string; vquality?: string; size?: string; videoMode?: string; videoGenerateAudio?: string };

/** 从画布全局默认派生初始草稿（只读取，不回写全局 videoModel / videoSeconds）。 */
export function defaultGenDraft(channel: GenChannelDesc | null, seed: GenDraftSeed = {}): GenDraft {
    const family = channel ? genFamily(channel.apiFormat) : "sdgf";
    const rows = genModelRows(channel);
    const seedSize = String(seed.size || "").trim();
    const ratio = seedSize && !parsePixelSize(seedSize) && GEN_CANVAS_RATIOS.includes(seedSize) ? seedSize : inferVideoRatio(seedSize || "auto");
    const duration = Math.max(GEN_DURATION_MIN, Math.min(GEN_DURATION_MAX, Math.floor(Number(clampVideoSeconds(seed.videoSeconds || "6")) || 6)));
    return {
        channelId: channel?.id || "",
        model: family === "sdgf" ? rows.find((row) => row.selectable)?.name || "" : "",
        mode: family === "sdgf" ? (seed.videoMode === "reference" ? "reference" : "frames") : "",
        size: ratio,
        vquality: parseVideoResolution(seed.vquality),
        duration,
        audio: String(seed.videoGenerateAudio ?? "true") !== "false",
    };
}

/** 草稿归一（localStorage 旧值/畸形值兜底；渠道或模型不再存在时如实保留 → 由 UI 显式提示失效引用）。 */
export function normalizeGenDraft(raw: unknown): GenDraft | null {
    if (!raw || typeof raw !== "object") return null;
    const value = raw as Record<string, unknown>;
    const channelId = String(value.channelId || "").trim();
    const model = String(value.model || "").trim();
    const modeRaw = String(value.mode || "").trim();
    const size = String(value.size || "").trim() || "auto";
    const vquality = String(value.vquality || "").trim() || "720";
    const duration = Math.floor(Number(value.duration));
    return {
        channelId,
        model,
        mode: modeRaw === "reference" ? "reference" : modeRaw === "frames" ? "frames" : "",
        size,
        vquality,
        duration: Number.isFinite(duration) ? Math.max(GEN_DURATION_MIN, Math.min(GEN_DURATION_MAX, duration)) : 6,
        audio: value.audio === undefined ? true : Boolean(value.audio),
    };
}

/** 失效引用：任务草稿记了渠道但当前画布配置里已经解析不到（渠道被删/改 id）。 */
export function genDraftStaleNote(draft: GenDraft | null, channels: GenChannelDesc[]): string {
    if (!draft) return "";
    if (!draft.channelId) return "还没选生成渠道——到画布「设置 → 渠道」里配好 SDGF 或万相渠道后回来选。";
    if (findGenChannel(channels, draft.channelId)) return "";
    return "草稿里的渠道已经不在画布配置里（渠道被删或改了），请重新选渠道 + 模型。";
}

// ---------------------------------------------------------------- 请求体（§2.2 approve config 无 key / §三 submit 带 key）

export type GenApproveConfig = {
    渠道身份: { baseUrl: string; apiFormat: string; 渠道名: string };
    model: string;
    mode: string;
    params: { duration: number; size: string; vquality: string; audio: boolean };
};

/** approve 请求体（**无 key**：批准只固化非敏感身份；改任一有效字段＝快照失效需重批）。 */
export function buildApproveConfig(draft: GenDraft, channel: GenChannelDesc | null): GenApproveConfig {
    const family = channel ? genFamily(channel.apiFormat) : "canvas-only";
    return {
        渠道身份: {
            baseUrl: String(channel?.baseUrl || "").trim(),
            apiFormat: family === "wan" ? "wan" : family === "sdgf" ? "sdgf" : String(channel?.apiFormat || ""),
            渠道名: String(channel?.name || "").trim(),
        },
        model: family === "wan" ? "" : String(draft.model || "").trim(),
        mode: family === "wan" ? "" : String(draft.mode || ""),
        params: {
            duration: Math.floor(Number(draft.duration) || 0),
            size: genSizeForSubmit(draft.size, draft.vquality),
            vquality: String(draft.vquality || "").trim(),
            audio: Boolean(draft.audio),
        },
    };
}

/** submit 请求体（key 一次性透传，不落盘；wan 允许空 key）。 */
export function buildSubmitBody(apiKey: string | null | undefined): { key: string } {
    return { key: String(apiKey || "").trim() };
}

/** 快照签名（前端口径）：渠道身份 + model + mode + params；**key 不入签名**（只换 key 不失效）。 */
export function genDraftSignature(draft: GenDraft, channel: GenChannelDesc | null): string {
    const config = buildApproveConfig(draft, channel);
    return JSON.stringify({
        channelId: String(channel?.id || ""),
        baseUrl: config.渠道身份.baseUrl,
        apiFormat: config.渠道身份.apiFormat,
        渠道名: config.渠道身份.渠道名,
        model: config.model,
        mode: config.mode,
        params: {
            duration: config.params.duration,
            size: config.params.size,
            vquality: config.params.vquality,
            audio: config.params.audio,
        },
    });
}

/** 批准前的前置校验（显式提示，不静默换渠道/不静默改值）。 */
export function genApproveIssues(draft: GenDraft | null, channel: GenChannelDesc | null): string[] {
    const issues: string[] = [];
    if (!draft || !channel) {
        return ["先选生成渠道（SDGF 或万相）——hub 只承接这两个协议族。"];
    }
    const family = genFamily(channel.apiFormat);
    if (family === "canvas-only") issues.push("这个渠道是画布专用协议（gemini/openai/模型脚本），hub 不承接——换 SDGF 或万相渠道。");
    if (!String(channel.baseUrl || "").trim()) issues.push("渠道缺 baseUrl（画布「设置 → 渠道」里补）。");
    if (family === "sdgf" && !String(draft.model || "").trim()) issues.push("SDGF 渠道必须选模型。");
    if (!genSizeAccepted(draft.size, channel)) {
        const note = genSizeNote(draft.size, channel);
        issues.push(note || `画幅 ${draft.size} 不在渠道接收域内。`);
    }
    if (!GEN_VIDEO_RESOLUTIONS.includes(String(draft.vquality))) issues.push(`清晰度只能是 ${GEN_VIDEO_RESOLUTIONS.join(" / ")}（当前 ${draft.vquality}）。`);
    if (!(draft.duration >= GEN_DURATION_MIN && draft.duration <= GEN_DURATION_MAX)) {
        issues.push(`时长必须在 ${GEN_DURATION_MIN}–${GEN_DURATION_MAX} 秒（当前 ${draft.duration}）。`);
    }
    return issues;
}

/** 提交前的前置校验：在批准校验之上加 key（wan 允许空 key）。 */
export function genSubmitIssues(draft: GenDraft | null, channel: GenChannelDesc | null, apiKey: string | null): string[] {
    const issues = genApproveIssues(draft, channel);
    if (channel && genFamily(channel.apiFormat) !== "wan" && !String(apiKey || "").trim()) {
        issues.push("渠道缺 API Key（提交时一次性透传，不落盘）——到画布「设置 → 渠道」补上。");
    }
    return issues;
}

// ---------------------------------------------------------------- 草稿存储（localStorage，按任务）

export const GEN_DRAFT_PREFIX = "hub-gen-draft:";
export const GEN_APPROVED_PREFIX = "hub-gen-approved:";

export type GenDraftStorage = {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
    removeItem(key: string): void;
};

/** 默认存储：浏览器 localStorage；不可用（SSR/隐私模式/探针 node）→ null，调用方按「无草稿」处理。 */
export function defaultGenStorage(): GenDraftStorage | null {
    try {
        if (typeof window === "undefined" || !window.localStorage) return null;
        return window.localStorage;
    } catch {
        return null;
    }
}

export function genDraftKey(localId: string) {
    return `${GEN_DRAFT_PREFIX}${localId}`;
}

export function genApprovedKey(localId: string) {
    return `${GEN_APPROVED_PREFIX}${localId}`;
}

export function readGenDraft(localId: string, storage: GenDraftStorage | null = defaultGenStorage()): GenDraft | null {
    if (!storage || !localId) return null;
    try {
        const raw = storage.getItem(genDraftKey(localId));
        return raw ? normalizeGenDraft(JSON.parse(raw)) : null;
    } catch {
        return null;
    }
}

export function saveGenDraft(localId: string, draft: GenDraft, storage: GenDraftStorage | null = defaultGenStorage()) {
    if (!storage || !localId) return;
    try {
        storage.setItem(genDraftKey(localId), JSON.stringify(draft));
    } catch {
        /* 存储满/禁用：草稿丢在内存里，不阻断操作 */
    }
}

export function clearGenDraft(localId: string, storage: GenDraftStorage | null = defaultGenStorage()) {
    if (!storage || !localId) return;
    try {
        storage.removeItem(genDraftKey(localId));
    } catch {
        /* ignore */
    }
}

/** 批准时记下的签名（用于「批准后配置被改 → 快照作废」提示；只换 key 不触发）。 */
export function readApprovedSignature(localId: string, storage: GenDraftStorage | null = defaultGenStorage()): string {
    if (!storage || !localId) return "";
    try {
        return storage.getItem(genApprovedKey(localId)) || "";
    } catch {
        return "";
    }
}

export function saveApprovedSignature(localId: string, signature: string, storage: GenDraftStorage | null = defaultGenStorage()) {
    if (!storage || !localId) return;
    try {
        storage.setItem(genApprovedKey(localId), signature);
    } catch {
        /* ignore */
    }
}

export function clearApprovedSignature(localId: string, storage: GenDraftStorage | null = defaultGenStorage()) {
    if (!storage || !localId) return;
    try {
        storage.removeItem(genApprovedKey(localId));
    } catch {
        /* ignore */
    }
}

/** 批准后编辑检测：签名变过 → true（UI 提示需重新批准，禁止直接提交）。 */
export function genNeedsReapproval(localId: string, draft: GenDraft, channel: GenChannelDesc | null, storage: GenDraftStorage | null = defaultGenStorage()): boolean {
    const saved = readApprovedSignature(localId, storage);
    if (!saved) return false;                       // 别处批准（CLI/另一浏览器）：没有本地基线，不做假判断
    return saved !== genDraftSignature(draft, channel);
}

// ---------------------------------------------------------------- 提交链阻断态（get_task 透传字段 → UI 引导）

/** get_task 透传的提交链字段（B1/B2 已就绪）。 */
export type GenIssueDetail = {
    state?: string;
    gen_error?: string;
    submit_unknown?: { reason?: string; at?: string; dispatch?: string } | null;
    dispatch?: { phase?: string; at?: string; note?: string } | string | null;
    cloud_task_id?: string;
    active_submit?: boolean;
    submit_lost?: {
        step?: number;
        reason?: string;
        submit_lost?: boolean;
        cloud_in_flight?: boolean;
        has_video?: boolean;
        cloud_task_id?: string;
        dispatch?: string;
    } | null;
};

export type GenIssueKind = "none" | "active" | "blocked" | "orphan";

export type GenIssue = {
    kind: GenIssueKind;
    title: string;
    detail: string;
    steps: string[];
    cloudTaskId: string;
    /** 允许「按云端 taskId 续查」（只查不建，后端 generating 分支保证不创建）。 */
    canResume: boolean;
    /** 禁止直接重提（阻断/在途一律 true）。 */
    blocksRetry: boolean;
};

export function genDispatchPhase(detail: GenIssueDetail | null | undefined): string {
    const raw = detail?.dispatch;
    if (typeof raw === "string") return raw;
    if (raw && typeof raw === "object") return String((raw as { phase?: string }).phase || "");
    return "";
}

export function genIssue(detail: GenIssueDetail | null | undefined): GenIssue {
    const none: GenIssue = { kind: "none", title: "", detail: "", steps: [], cloudTaskId: "", canResume: false, blocksRetry: false };
    if (!detail) return none;
    const state = String(detail.state || "");
    const cloudTaskId = String(detail.cloud_task_id || detail.submit_lost?.cloud_task_id || "").trim();
    const phase = genDispatchPhase(detail);
    const active = Boolean(detail.active_submit);

    if (active) {
        return {
            kind: "active",
            title: "提交进行中",
            detail: "这个任务已有提交在跑（排队 / 已发出等云端回执）——别重复点，重复提交会重复扣费。",
            steps: ["进度 5 秒自动刷新；结果出来会变成成片或失败红条。", "这期间「提交生成」与重试都不可用。"],
            cloudTaskId,
            canResume: false,
            blocksRetry: true,
        };
    }

    const unknown = detail.submit_unknown;
    if (unknown) {
        return {
            kind: "blocked",
            title: "提交结果未知 · 已阻断重提",
            detail: String(unknown.reason || "已尝试发出但没拿到确定回执：云端可能已收单。"),
            steps: [
                "到渠道控制台按时间 / 提示词核对有没有这一单（不许直接重提，会重复扣费）。",
                cloudTaskId ? `核对到单后按云端 taskId ${cloudTaskId} 续查（只查不重提）。` : "核对到单后拿到云端 taskId，按该 id 续查（只查不重提）。",
                "确认渠道确实没有这一单 → 人工解除阻断后才能重新提交（解除动作会写进任务历史）。",
            ],
            cloudTaskId,
            canResume: Boolean(cloudTaskId),
            blocksRetry: true,
        };
    }

    if (phase === "prepared" || phase === "dispatched") {
        return {
            kind: "blocked",
            title: `提交未拿到确定回执（dispatch=${phase}）`,
            detail: phase === "prepared" ? "提交意图已落盘，但服务端没记录到「已发出」——云端大概率没收单，仍需人工核对。" : "请求已发出，但没拿到回执——云端可能已收单。",
            steps: [
                "到渠道控制台核对有没有这一单。",
                cloudTaskId ? `有单 → 按云端 taskId ${cloudTaskId} 续查。` : "有单 → 拿到云端 taskId 后按 id 续查。",
                "没单 → 人工解除阻断后再提交。",
            ],
            cloudTaskId,
            canResume: Boolean(cloudTaskId),
            blocksRetry: true,
        };
    }

    const lost = detail.submit_lost;
    const lostStep = Number(lost?.step) || 0;
    if (state === "generating" && lost && (lost.submit_lost || lostStep === 1 || lostStep === 2)) {
        if (lostStep === 1) {
            return {
                kind: "active",
                title: "提交进行中",
                detail: String(lost.reason || "有活跃提交占用。"),
                steps: ["等回执，别重复提交。", "这期间「提交生成」与重试都不可用。"],
                cloudTaskId,
                canResume: false,
                blocksRetry: true,
            };
        }
        if (lostStep === 2) {
            return {
                kind: "orphan",
                title: "有云端在途任务 · 按 taskId 续查",
                detail: String(lost.reason || (cloudTaskId ? `云端 taskId ${cloudTaskId} 仍在跑/已出片。` : "云端仍有在途证据。")),
                steps: [cloudTaskId ? `按云端 taskId ${cloudTaskId} 续查（只查不重提）。` : "按云端 taskId 续查（只查不重提）。", "别重新提交——云端那份已经在跑，重提会重复扣费。"],
                cloudTaskId,
                canResume: true,
                blocksRetry: true,
            };
        }
        return {
            kind: "orphan",
            title: "疑似孤儿（无在途证据、无成片）",
            detail: String(lost.reason || "generating 但无活跃提交、无云端 taskId、无成片。"),
            steps: [
                "先到渠道控制台核对有没有这一单（只读核对，别急着点提交）。",
                "确认没单 → 走「受控恢复」回到待批准（人工动作，会记任务历史）。",
                "恢复后可带新配置重新批准；不许直接重提。",
            ],
            cloudTaskId,
            canResume: false,
            blocksRetry: true,
        };
    }

    return none;
}

/** wan 失败语义分层文案（§2.3：超时未知 ≠ 失败，不靠错误文案子串猜分类）。 */
export function genFailureHint(channel: GenChannelDesc | null | undefined, state: string | undefined): string {
    const family = channel ? genFamily(channel.apiFormat) : "sdgf";
    if (family === "wan") {
        return state === "failed"
            ? "万相：只有渠道明确终态失败才转 failed；等待超时不算失败。"
            : "万相：等待超时 ≠ 失败（云端可能仍在跑）——不要重提，按云端 taskId 续查。";
    }
    return state === "failed"
        ? "SDGF：云端明确失败才会转 failed；错误原文已脱敏。"
        : "SDGF：前端超时 ≠ 云端失败——按 taskId 续查即可找回成片。";
}

/** 生成设置面板的一句话摘要（审批/生成中回显）。 */
export function genDraftSummary(draft: GenDraft | null, channel: GenChannelDesc | null): string {
    if (!draft || !channel) return "未选渠道";
    const family = genFamily(channel.apiFormat);
    const parts = [channel.name];
    if (family === "wan") parts.push(GEN_WAN_FIXED_NOTE, genSizeForSubmit(draft.size, draft.vquality));
    else parts.push(draft.model || "未选模型", draft.mode === "reference" ? "参考图模式" : "首尾帧模式", genSizeForSubmit(draft.size, draft.vquality));
    parts.push(`${draft.vquality}p`, `${draft.duration}s`, draft.audio ? "带声音" : "静音");
    return parts.filter(Boolean).join(" · ");
}
