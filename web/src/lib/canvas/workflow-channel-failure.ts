/**
 * 渠道失败分类（P6；审计 §5.2「429/409 与未知提交分开处理」的判据层）。
 *
 * 为什么单独一层：这是**唯一**决定「这次失败能不能自动重排」的地方。
 * 三类语义完全不同，混在一起就会重复扣费：
 *
 * | 类别 | 含义 | 队列动作 | 依据 |
 * |---|---|---|---|
 * | `rate_limited`（429） | 渠道限流，请求**被拒**、未消费额度 | 指数退避重排（有次数上限） | 429 语义 + `wanrepro/ui/server.py` 各端点同款「稍后再试」 |
 * | `slot_busy`（409） | 单槽在跑（Wan 单槽=1，`WAN_BUSY`；SDGF=2，`SDGF_JOBS`；拆解槽 `ANALYZE_BUSY`） | 指数退避重排（有次数上限） | `server.py:735 / 1135 / 3670`：`acquire(blocking=False)` 失败即 409，**未提交、未扣费** |
 * | `submit_unknown` | 请求**可能已经发出并执行**，只是回执丢了（超时/断线/5xx/状态不明） | **禁止自动重发**，进对账（先按既有后端任务续查） | D11 + A2/B6：30s 工具回执 ≠ 生成时限；晚回执 ≠ 未执行 |
 *
 * 判定优先级（顺序即结论，命中即返回）：
 * ① 调用方已给的显式分类（`error.channel` / `error.kind` / `details.channel`）——生产链路里创建函数自己知道发生了什么；
 * ② HTTP 状态码：429 → 限流；409 → 单槽忙；408/504 → 未知提交；5xx → 未知提交（保守：宁可停住交人）；
 * ③ 其它 4xx → `null`（请求形态被明确拒绝，既不是拥塞也不是未知：照实失败，不重排）；
 * ④ 文案兜底（无状态码时）：未知提交信号优先于拥塞信号（宁可停住，不可重发）。
 *
 * 文案兜底只作兜底：命中时 `evidence` 会写明「依据=文案」，探针据此区分「结构化判定」与「文案判定」。
 */

export type ChannelFailureKind = "rate_limited" | "slot_busy" | "submit_unknown";

export type ChannelFailureClass = {
    kind: ChannelFailureKind;
    /** HTTP 状态码（有就带出来，供卡上/报告复核）。 */
    status?: number;
    /** 判定依据（人能读懂；`结构化` / `状态码 429` / `文案：…`）。 */
    evidence: string;
};

const SUBMIT_UNKNOWN_PATTERNS: RegExp[] = [
    /回执/i,
    /未返回任务\s*ID/i,
    /没有返回任务\s*ID/i,
    /状态不明/,
    /勿重复提交/,
    /提交状态不明/,
    /可能已提交/,
    /timeout/i,
    /timed?\s*out/i,
    /ETIMEDOUT|ECONNRESET|ECONNABORTED|EPIPE|ENOTFOUND|EAI_AGAIN/,
    /socket hang up/i,
    /network error/i,
    /aborted?/i,
];

const SLOT_BUSY_PATTERNS: RegExp[] = [/单槽/, /只认一条/, /只认\s*1\s*条/, /已经有一个/, /已经有一条/, /槽.*占用/, /被占/, /busy/i, /稍后再试/];

const RATE_LIMIT_PATTERNS: RegExp[] = [/rate.?limit/i, /too many requests/i, /限流/, /请求过于频繁/, /频率超限/, /quota/i];

function readStatus(input: Record<string, unknown>): number | undefined {
    const candidates = [input.status, input.statusCode, input.httpStatus, input.code, (input.response as Record<string, unknown> | undefined)?.status, (input.details as Record<string, unknown> | undefined)?.httpStatus, (input.details as Record<string, unknown> | undefined)?.status];
    for (const candidate of candidates) {
        const value = Number(candidate);
        if (Number.isFinite(value) && value >= 100 && value <= 599) return value;
    }
    return undefined;
}

function readExplicitKind(input: Record<string, unknown>): ChannelFailureKind | null {
    const candidates = [input.channel, input.kind, (input.details as Record<string, unknown> | undefined)?.channel];
    for (const candidate of candidates) {
        if (!candidate || typeof candidate !== "object") continue;
        const kind = String((candidate as Record<string, unknown>).kind || "");
        if (kind === "rate_limited" || kind === "slot_busy" || kind === "submit_unknown") return kind;
    }
    for (const candidate of candidates) {
        const kind = String(candidate || "");
        if (kind === "rate_limited" || kind === "slot_busy" || kind === "submit_unknown") return kind;
    }
    return null;
}

function readMessage(input: Record<string, unknown>): string {
    const parts = [input.message, input.reason, input.error, (input.details as Record<string, unknown> | undefined)?.reason, (input.details as Record<string, unknown> | undefined)?.message, (input.response as Record<string, unknown> | undefined)?.data];
    return parts
        .map((part) => (typeof part === "string" ? part : part && typeof part === "object" ? safeStringify(part) : ""))
        .filter(Boolean)
        .join(" | ");
}

function safeStringify(value: unknown): string {
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}

function firstMatch(patterns: RegExp[], text: string): RegExp | null {
    return patterns.find((pattern) => pattern.test(text)) || null;
}

/**
 * 渠道失败分类。`input` 可以是 Error、axios 错误、`{status, message}`、或编排器 failure 对象。
 * 返回 `null` = 「不属于渠道拥塞，也不是未知提交」——调用方照实失败，不重排不发呆。
 */
export function classifyChannelFailure(input: unknown): ChannelFailureClass | null {
    if (!input) return null;
    const record: Record<string, unknown> = typeof input === "object" ? (input as Record<string, unknown>) : { message: String(input) };

    // ① 调用方显式分类（最可靠：创建函数自己知道）
    const explicit = readExplicitKind(record);
    const explicitStatus = readStatus(record);
    if (explicit) return { kind: explicit, status: explicitStatus, evidence: "结构化（调用方显式标注 channel.kind）" };

    // ② 状态码
    const status = readStatus(record);
    if (status) {
        if (status === 429) return { kind: "rate_limited", status, evidence: `状态码 ${status}` };
        if (status === 409) return { kind: "slot_busy", status, evidence: `状态码 ${status}（单槽/并发上限）` };
        if (status === 408 || status === 504) return { kind: "submit_unknown", status, evidence: `状态码 ${status}（请求已发出但回执超时）` };
        if (status >= 500) return { kind: "submit_unknown", status, evidence: `状态码 ${status}（服务端错误：无法证明未执行，按未知处理）` };
        // 其它 4xx = 请求被明确拒绝：既没提交成功也不是拥塞，照实失败
        if (status >= 400) return null;
    }

    // ③ 文案兜底：未知提交优先（宁可停住不可重发）→ 明确状态数字 → 限流词 → 单槽词。
    //    顺序理由：429/409 这两个数字是渠道契约里最硬的信号；「稍后再试」这种文案两边都在用，不能当判据。
    const message = readMessage(record);
    if (!message) return null;
    const unknown = firstMatch(SUBMIT_UNKNOWN_PATTERNS, message);
    if (unknown) return { kind: "submit_unknown", evidence: `文案：${unknown.source}` };
    if (/\b429\b/.test(message)) return { kind: "rate_limited", evidence: "文案：429" };
    if (/\b409\b/.test(message)) return { kind: "slot_busy", evidence: "文案：409" };
    const limited = firstMatch(RATE_LIMIT_PATTERNS, message);
    if (limited) return { kind: "rate_limited", evidence: `文案：${limited.source}` };
    const busy = firstMatch(SLOT_BUSY_PATTERNS, message);
    if (busy) return { kind: "slot_busy", evidence: `文案：${busy.source}` };
    return null;
}

/** 拥塞类（可自动重排）——未知提交**不在**此列（D11：自动层永不重发付费提交）。 */
export function isCongestion(kind: ChannelFailureKind | null | undefined): kind is "rate_limited" | "slot_busy" {
    return kind === "rate_limited" || kind === "slot_busy";
}
