/**
 * 卡C2b · 变体直绑通道（方案 docs/方案-卡C一拆多用变体派生-20261115.md §三 D2b；路2 N1 BLOCKING 修正）。
 *
 * 为什么不能走 `bindInstanceToHub.ingest`（workflow-instance-run.ts:294）：桥侧 ingest 幂等键=视频 sha，
 * 变体与源任务同一原片 → 幂等命中**源任务** localId，正文/自检/快照写穿到源任务，击穿 D7④「源零改动」。
 * 本集成层**跳过 ingest**：直接持有 fork 返回的 localId/bridgeId（POST /api/hub/task/<id>/fork 回执），
 * 消费 fork 出生态 awaiting_assets → awaiting_approval 的推进（资产上传 → 确认置换 → rendering 后台渲染
 * → 待批），终点=awaiting_approval 停待批（付费红线不变：本文件**不发** approve/submit/generate）。
 *
 * 消费的端点（hub 既有合同，零引擎改动）：
 * - GET  /api/hub/task/<id>                → 现查状态（§5.3：状态一律现查，不落本文件）
 * - POST /api/hub/assets/<id>（FormData）   → 变体资产槽图上传（写 pending_replace，确认时生效）
 * - POST /api/hub/confirm/<id>             → 确认置换（empty_confirm/按原样）→ rendering → 后台渲染
 * - POST /api/hub/task/<id>/prompt          → 正文保存（唯一安全区=awaiting_approval，自带快照重算+自检）
 *
 * 纯 Node 可打包（不读 i18n、不摸 React、不碰 store），探针可直接断言。
 */

/** 直绑通道错误：code ∈ identity（fork 身份不成立）/ state（状态不允许该动作）/ confirm / timeout / prompt_save。 */
export type VariantBindErrorCode = "identity" | "state" | "confirm" | "timeout" | "prompt_save";

export class VariantBindError extends Error {
    readonly code: VariantBindErrorCode;
    readonly details: Record<string, unknown>;
    constructor(code: VariantBindErrorCode, message: string, details: Record<string, unknown> = {}) {
        super(message);
        this.name = "VariantBindError";
        this.code = code;
        this.details = details;
    }
}

/** fork 回执身份（POST /api/hub/task/<源id>/fork → {local_id, bridge_id, derived_from.local_id}）。 */
export type VariantForkIdentity = {
    /** fork 新任务号（变体一切后续动作都挂它，绝不重 ingest）。 */
    localId: string;
    /** fork 共享的拆解 bridge id（与源同桥；回执里带，现查复核用）。 */
    bridgeId: string;
    /** 来源任务号（derived_from.local_id；回执与审计痕迹）。 */
    sourceLocalId: string;
    boundAt: string;
};

/**
 * hub 直绑客户端（桥既有端点的最小面；页面实现=fetch 直连桥，探针=stub）。
 * 刻意**不含** ingest / approve / submit / generate——变体直绑通道的法律边界就是这三条不出现。
 */
export type VariantHubDirectClient = {
    /** 现查任务状态（GET /api/hub/task/<id> → task.state；桥不存在/已删 → 非法态由实现如实抛）。 */
    getTask: (localId: string) => Promise<{ state: string; bridgeId?: string }>;
    /** 变体资产槽图上传（POST /api/hub/assets/<id>，写 pending_replace；确认置换时落清单）。 */
    uploadAssets?: (localId: string, files: { blob: Blob; filename: string }[]) => Promise<{ filled?: unknown[] }>;
    /** 确认置换（POST /api/hub/confirm/<id>）→ rendering + 后台渲染（前端零执行责任，只等）。 */
    confirmReplace: (localId: string, input?: { emptyConfirm?: boolean; ackRows?: number[] }) => Promise<{ state?: string; jobId?: string }>;
    /** 正文保存（POST /api/hub/task/<id>/prompt；awaiting_approval 唯一安全区，自带自检重跑）。 */
    savePrompt: (input: { localId: string; text: string }) => Promise<{ state?: string; approvalSnapshot?: string; selfcheck?: unknown[] }>;
};

/** 推进门（纯函数）：变体直绑在当前态允许的下一个动作。 */
export function variantAdvanceGate(state: string): { action: "verify_identity" | "upload_and_confirm" | "wait_render" | "ready" | "failed" | "blocked"; reason: string } {
    const s = String(state || "").trim();
    if (s === "awaiting_assets") return { action: "upload_and_confirm", reason: "fork 出生态：上传变体资产（可空）→ 确认置换推进" };
    if (s === "rendering") return { action: "wait_render", reason: "渲染中：等后台渲染推进到待批" };
    if (s === "awaiting_approval") return { action: "ready", reason: "已在待批：直接保存正文/交人工审批" };
    if (s === "failed") return { action: "failed", reason: "失败态：如实上报（可 retry，不自动）" };
    if (s === "ingested" || s === "discussing") return { action: "verify_identity", reason: "拆解还没跑完：fork 任务不该停在这（源产物复用语义被破坏），拒绝推进" };
    return { action: "blocked", reason: `付费/运行态（${s || "未知"}）：直绑通道只消费 awaiting_assets→awaiting_approval，其余一律不动` };
}

export type VariantBindAdvance = {
    fork: VariantForkIdentity;
    /** 终态（= awaiting_approval 或推进中途如实失败态）。 */
    state: string;
    /** 是否走了「全沿用」空确认（无资产上传时的唯一放行通道）。 */
    confirmedEmpty: boolean;
    assetsUploaded: number;
    /** 渲染等待耗时（确认→待批；未等待=0）。 */
    waitedMs: number;
    promptSaved: boolean;
    approvalSnapshot?: string;
    selfcheck?: unknown[];
    warnings: string[];
};

export type BindVariantToHubInput = {
    /** fork 回执身份（调用方先 fork 成功——D7⑤ 顺序由调用方保证，本函数绝不代替 fork）。 */
    fork: VariantForkIdentity;
    /** 变体资产槽绑定图（画布侧 deriveWorkflowVariant.bindings 的落图字节；空=全沿用确认）。 */
    assets?: { blob: Blob; filename: string }[];
    /** 确认参数；缺省 emptyConfirm=true（无上传时唯一放行通道，replace.py 硬拒空确认的对应口径）。 */
    confirm?: { emptyConfirm?: boolean; ackRows?: number[] };
    /** 待批后要保存的正文（变体组装产物文本；缺省=不保存，停待批等人）。 */
    promptText?: string;
    /** 渲染等待上限/轮询间隔（缺省 120s / 2s；超时如实报 timeout，绝不半途假装成功）。 */
    timeoutMs?: number;
    intervalMs?: number;
};

/**
 * 变体直绑主入口（C2b）：
 * ① 身份复核：任务存在、bridge_id 与 fork 回执一致、状态经 variantAdvanceGate 允许——**零 ingest**；
 * ② 资产上传（有则传，槽绑定同步为清单 pending_replace）；
 * ③ 确认置换 → rendering（后台渲染）→ 轮询至 awaiting_approval / failed / 超时；
 * ④ 终点 awaiting_approval：promptText 有则保存（自带快照+自检），否则停待批等人。
 * 付费红线：本函数从不调用 approve/submit/generate。
 */
export async function bindVariantToHub(input: BindVariantToHubInput, deps: { client: VariantHubDirectClient; now?: () => string; sleep?: (ms: number) => Promise<void>; log?: (message: string) => void }): Promise<VariantBindAdvance> {
    const now = deps.now || (() => new Date().toISOString());
    const sleep = deps.sleep || ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
    const fork = input.fork;
    const localId = String(fork?.localId || "").trim();
    const bridgeId = String(fork?.bridgeId || "").trim();
    if (!localId || !bridgeId || !String(fork?.sourceLocalId || "").trim()) {
        throw new VariantBindError("identity", "fork 身份不完整（localId/bridgeId/sourceLocalId 任一为空）：拒绝只造影子壳，更不走 ingest 补", { fork });
    }
    const warnings: string[] = [];

    // ① 身份复核：现查 + bridge 一致性（fork 任务的拆解产物与源同桥；对不上=身份不成立）
    const task = await deps.client.getTask(localId);
    const gate = variantAdvanceGate(task.state);
    if (String(task.bridgeId || "") && String(task.bridgeId) !== bridgeId) {
        throw new VariantBindError("identity", `任务 ${localId} 的 bridge_id=${task.bridgeId} 与 fork 回执 ${bridgeId} 不一致：这不是这份 fork，拒绝推进`, { localId, taskBridgeId: task.bridgeId, forkBridgeId: bridgeId });
    }
    if (gate.action === "blocked" || gate.action === "failed" || gate.action === "verify_identity") {
        throw new VariantBindError("state", `任务 ${localId} 当前 ${task.state || "未知"}：${gate.reason}`, { localId, state: task.state, action: gate.action });
    }

    // ② 资产上传（变体槽绑定 → hub 清单 pending_replace；缺省跳过）
    let assetsUploaded = 0;
    const files = (input.assets || []).filter((file) => file && file.blob && String(file.filename || "").trim());
    if (files.length && !deps.client.uploadAssets) throw new VariantBindError("confirm", `带了 ${files.length} 个变体资产但客户端未提供 uploadAssets：拒绝静默丢资产`, { count: files.length });
    if (files.length) {
        const uploaded = await deps.client.uploadAssets!(localId, files);
        assetsUploaded = Array.isArray(uploaded?.filled) ? uploaded.filled.length : files.length;
        if (assetsUploaded <= 0) throw new VariantBindError("confirm", `变体资产上传回执为空（${files.length} 个文件）：拒绝在零资产回执上确认`, { localId });
    } else if (gate.action === "upload_and_confirm") {
        warnings.push("无变体资产上传：将走「全沿用」空确认（empty_confirm=true）");
    }

    // ③ 确认置换 → 等 awaiting_approval（已在待批/rendering 则跳过确认——rendering 重复确认会被桥 409，只等）
    let state = task.state;
    let waitedMs = 0;
    const timeoutMs = Math.max(1_000, input.timeoutMs ?? 120_000);
    const intervalMs = Math.max(200, input.intervalMs ?? 2_000);
    if (state === "awaiting_assets") {
        const emptyConfirm = input.confirm?.emptyConfirm ?? files.length === 0;
        try {
            await deps.client.confirmReplace(localId, { emptyConfirm, ackRows: input.confirm?.ackRows || [] });
        } catch (error) {
            throw new VariantBindError("confirm", `确认置换被拒（${localId}）：${error instanceof Error ? error.message : String(error)}`, { localId, emptyConfirm });
        }
    }
    if (state !== "awaiting_approval") {
        const deadline = Date.now() + timeoutMs;
        while (true) {
            await sleep(intervalMs);
            waitedMs += intervalMs;
            const current = await deps.client.getTask(localId);
            state = String(current.state || "");
            if (state === "awaiting_approval") break;
            if (state === "failed") throw new VariantBindError("confirm", `渲染推进落 failed（${localId}）：如实上报，不自动重试`, { localId, state });
            if (Date.now() >= deadline) throw new VariantBindError("timeout", `渲染等待超时（${timeoutMs}ms，最后态 ${state || "未知"}）：如实上报，不停留在半套`, { localId, waitedMs, lastState: state });
        }
    }

    // ④ 终点=awaiting_approval：正文保存（自带快照+自检）；不给就停待批等人（付费红线）
    let approvalSnapshot: string | undefined;
    let selfcheck: unknown[] | undefined;
    let promptSaved = false;
    const text = String(input.promptText ?? "").trim();
    if (text) {
        if (state !== "awaiting_approval") throw new VariantBindError("prompt_save", `任务 ${localId} 不在待批（${state}）：正文只在 awaiting_approval 落`, { localId, state });
        try {
            const saved = await deps.client.savePrompt({ localId, text });
            approvalSnapshot = String(saved.approvalSnapshot || "") || undefined;
            selfcheck = Array.isArray(saved.selfcheck) ? saved.selfcheck : [];
            promptSaved = true;
            if (!approvalSnapshot) warnings.push("正文保存回执缺 approval_snapshot（快照是批准的唯一绑定物）：待批成立但快照留空，审批前需现查");
        } catch (error) {
            throw new VariantBindError("prompt_save", `正文保存被拒（${localId}）：${error instanceof Error ? error.message : String(error)}`, { localId, state });
        }
    } else {
        warnings.push("未提供 promptText：变体停在 awaiting_approval 等人（不自动批、不自动生成）");
    }
    deps.log?.(`变体 ${localId} 直绑完成：state=${state} 资产=${assetsUploaded} 等待=${waitedMs}ms 正文=${promptSaved}`);
    return { fork: { ...fork, boundAt: fork.boundAt || now() }, state, confirmedEmpty: files.length === 0, assetsUploaded, waitedMs, promptSaved, approvalSnapshot, selfcheck, warnings };
}
