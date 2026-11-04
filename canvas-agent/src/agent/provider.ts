import type { JsonRecord } from "../utils/value.js";
import type { AgentAttachment, AgentCanvasReference, AgentEmit, AgentPermissionMode } from "./types.js";

/**
 * 对话 Agent 的 provider 抽象层。
 *
 * 边界取自 `runCodexTurn`（`codex.ts`）：上面是会话与 turn 生命周期，下面是各家 CLI 自己的协议。
 * 前端只认识 6 类事件（`thread.started / turn.started / item.started / item.updated /
 * item.completed / turn.completed`，见 `web/src/components/agent/agent-event-formatters.ts`）。
 * 新增一家 Agent 只允许实现本接口，不得新增事件类型、消息体系或审批门。
 */

/** 已接入的 Agent 实现。新增一家时同步补 `loadAgentProvider` 与前端下拉。 */
export type AgentProviderId = "codex" | "hermes" | "claude";

/** 与 `codex-protocol.ts` 同名的推理档位；provider 不认识时忽略即可。 */
export type AgentReasoningEffort = "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra";

/** 前端共用的 6 类事件，两个 provider 必须归一到同一套。 */
export type AgentProviderEventType = "thread.started" | "turn.started" | "item.started" | "item.updated" | "item.completed" | "turn.completed";

/** 时间线条目；字段名沿用 Codex 的叫法，避免前端为第二家分叉。 */
export type AgentProviderItem = JsonRecord & {
    id: string;
    type: string;
    status?: string;
    text?: string;
    delta?: string;
    tool?: string;
    server?: string;
    arguments?: unknown;
    result?: unknown;
    error?: { message?: string };
};

/** 一条 provider 事件；通过 `agent_event` 广播。 */
export type AgentProviderEvent = {
    type: AgentProviderEventType;
    thread_id: string;
    turn_id?: string;
    item?: AgentProviderItem;
    status?: string;
    error?: unknown;
    usage?: unknown;
    metadata?: JsonRecord;
    /** 本轮耗时（毫秒）；`turn.completed` 上用于前端 turnSummary。 */
    duration_ms?: number;
};

/**
 * provider 的模型条目；字段名与 Codex `model/list` 对齐，前端无需为第二家分叉。
 * Hermes 用服务端配置的默认模型，不回 `model/list`。
 */
export type AgentProviderModel = JsonRecord & {
    id: string;
    model?: string;
    displayName?: string;
    description?: string;
    isDefault?: boolean;
    defaultReasoningEffort?: AgentReasoningEffort;
    supportedReasoningEfforts?: Array<{ reasoningEffort: AgentReasoningEffort; description?: string }>;
};

/** 会话句柄。`threadId` 是画布侧身份，`sessionId` 是 provider 自己的续跑凭据（可能为空）。 */
export type AgentProviderSession = { provider: AgentProviderId; threadId: string; sessionId?: string; model?: string; resumed?: boolean; resumable?: boolean };

/** 能力表：前端据此隐藏/禁用入口，后端据此在路由层返回 501。 */
export type AgentProviderCapabilities = {
    /** 不跑 turn 也能建会话（Codex 有常驻线程，Hermes 只能惰性建） */
    session: boolean;
    /** 能列出历史线程 */
    threads: boolean;
    /** 能从运行时拉模型列表 */
    models: boolean;
    /** 支持图片附件 */
    attachments: boolean;
    /** 有原生审批请求 */
    approvals: boolean;
    /** 能中断正在跑的 turn */
    interrupt: boolean;
    /** 支持多轮续跑 */
    resume: boolean;
};

/** 启动或恢复会话的参数。 */
export type AgentProviderSessionInput = { threadId?: string; cwd?: string; model?: string; permissionMode?: AgentPermissionMode; emit: AgentEmit };

/** 执行一轮的参数；`emit` 是生命周期事件出口（`agent_event` / `agent_log` / `agent_error` / `agent_done`）。 */
export type AgentProviderTurnInput = {
    threadId?: string;
    prompt: string;
    attachments?: AgentAttachment[];
    /**
     * 本轮 @ 引用的画布素材。
     *
     * 图片的内容由前端转成附件走 `attachments`；视频/音频体积大，只在这里传实体
     * （`localPath` 优先，其次 `previewUrl`），由 provider 决定怎么让模型读到。
     */
    references?: AgentCanvasReference[];
    cwd?: string;
    model?: string;
    effort?: AgentReasoningEffort;
    permissionMode?: AgentPermissionMode;
    emit: AgentEmit;
    onThread?: (threadId: string) => void;
    onTurn?: (turnId: string) => void;
};

/** 一轮的终态；`status` 必须反映真实结果，不得把失败写成 completed。 */
export type AgentProviderTurnOutcome = { provider: AgentProviderId; threadId: string; turnId: string; status: "completed" | "failed" | "interrupted"; text?: string; usage?: unknown };

/** 一家 Agent 的实现面。 */
export interface AgentProvider {
    readonly id: AgentProviderId;
    readonly label: string;
    readonly capabilities: AgentProviderCapabilities;
    startSession(input: AgentProviderSessionInput): Promise<AgentProviderSession>;
    resume(input: AgentProviderSessionInput & { threadId: string }): Promise<AgentProviderSession>;
    turn(input: AgentProviderTurnInput): Promise<AgentProviderTurnOutcome>;
    interrupt(options?: { threadId?: string }): Promise<boolean>;
    listModels(options?: { emit?: AgentEmit }): Promise<{ models: AgentProviderModel[]; raw?: unknown }>;
}

const providers = new Map<AgentProviderId, AgentProvider>();

/** 已知的 provider 与前端下拉顺序；新增一家只改这里 + `loadAgentProvider` 的加载分支。 */
const AGENT_PROVIDER_IDS: AgentProviderId[] = ["codex", "hermes", "claude"];

/** 注册一家实现；同一 id 覆盖注册，便于测试替换。 */
export function registerAgentProvider(provider: AgentProvider) {
    providers.set(provider.id, provider);
    return provider;
}

/** 全部已知 provider（含尚未加载模块的），用于能力表与报错文案。 */
export function agentProviderIds() {
    return [...AGENT_PROVIDER_IDS];
}

/** 已加载模块的 provider id。 */
export function registeredAgentProviderIds() {
    return [...providers.keys()];
}

/** 判断字符串是否是已知 provider。 */
export function isAgentProviderId(value: unknown): value is AgentProviderId {
    return AGENT_PROVIDER_IDS.includes(value as AgentProviderId);
}

/** 按需加载并返回 provider；模块在加载时自注册，避免 provider.ts 反向依赖具体实现。 */
export async function loadAgentProvider(id: unknown): Promise<AgentProvider | null> {
    if (!isAgentProviderId(id)) return null;
    if (!providers.has(id)) {
        if (id === "codex") await import("./codex.js");
        else if (id === "hermes") await import("./hermes.js");
        else await import("./claude.js");
    }
    return providers.get(id) || null;
}

/** 生成画布侧的会话 id；provider 没有原生线程时用它兜住前端的分组。 */
export function createProviderThreadId(provider: AgentProviderId) {
    return `${provider}-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 10)}`;
}
