import { spawn } from "node:child_process";

import { AGENT_PROMPT } from "../config.js";
import { createAgentLogWriter } from "../utils/agent-runtime.js";
import { errorMessage } from "../utils/value.js";
import { createProviderThreadId, registerAgentProvider, type AgentProvider, type AgentProviderModel, type AgentProviderSession, type AgentProviderSessionInput, type AgentProviderTurnInput, type AgentProviderTurnOutcome } from "./provider.js";
import type { AgentEmit } from "./types.js";

/**
 * Claude CLI 适配器（占位实现）。
 *
 * 保留原因：证明 `provider.ts` 的接口不是照着 Codex/Hermes 之一裁出来的。
 * 现状：事件仍是 Claude 自己的 stream-json（`assistant` / `result`）直转，**没有**归一成
 * `thread.started … turn.completed` 6 类，所以前端没接线（README「只保留代码」）。
 * 要真正启用，需要照 `hermes.ts` 的写法补一层 `item.*` 映射，不需要动路由。
 */

/** 使用 Claude CLI 执行一次带 Canvas Agent 工具的任务，返回进程退出码。 */
export async function runClaudeTurn(prompt: string, emit: AgentEmit): Promise<number> {
    const fullPrompt = withAgentPrompt(prompt);
    if (!fullPrompt) return 0;
    const child = spawnAgent("claude", ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--allowedTools", "mcp__infinite-canvas__*", fullPrompt], emit);
    if (!child) return 1;
    return await pipeJsonLines(child, emit, "claude");
}

/** 为 Claude CLI 请求拼接 Canvas Agent 指令。 */
function withAgentPrompt(prompt: string) {
    return prompt.trim() ? `${AGENT_PROMPT}\n\n用户请求：${prompt}` : "";
}

/** 将 Claude CLI 的 JSON Lines 输出转换为 Agent 事件，并在进程结束时返回退出码。 */
function pipeJsonLines(child: ReturnType<typeof spawn>, emit: AgentEmit, agent: string) {
    let out = "";
    const stderr = createAgentLogWriter((text) => emit("agent_log", { text }));
    child.stdout?.on("data", (chunk) => {
        out += chunk.toString();
        const lines = out.split(/\r?\n/);
        out = lines.pop() || "";
        lines.filter(Boolean).forEach((line) => {
            try {
                emit("agent_event", { agent, ...JSON.parse(line) });
            } catch {
                emit("agent_event", { agent, type: "raw", text: line });
            }
        });
    });
    child.stderr?.on("data", (chunk) => stderr.write(chunk.toString()));
    return new Promise<number>((resolve) => {
        child.on("error", (error) => {
            stderr.flush();
            emit("agent_error", { message: error.message });
            resolve(1);
        });
        child.on("close", (code) => {
            stderr.flush();
            emit("agent_done", { agent, code });
            resolve(code ?? 0);
        });
    });
}

/** 启动外部 Agent CLI，并将同步启动异常转换为事件。 */
function spawnAgent(name: string, args: string[], emit: AgentEmit) {
    try {
        return spawn(name, args, { stdio: ["ignore", "pipe", "pipe"], shell: process.platform === "win32", windowsHide: true });
    } catch (error) {
        emit("agent_error", { message: errorMessage(error) });
        return null;
    }
}

/** Claude 的占位实现：只有 `turn` 是真接线，其余能力按「没有」申报。 */
export const claudeProvider: AgentProvider = {
    id: "claude",
    label: "Claude Code",
    capabilities: { session: false, threads: false, models: false, attachments: false, approvals: false, interrupt: false, resume: false },

    /** Claude CLI 没有会话句柄，只分配画布侧 id。 */
    async startSession(input: AgentProviderSessionInput): Promise<AgentProviderSession> {
        return { provider: "claude", threadId: input.threadId || createProviderThreadId("claude"), resumable: false };
    },

    async resume(input: AgentProviderSessionInput & { threadId: string }): Promise<AgentProviderSession> {
        return { provider: "claude", threadId: input.threadId, resumed: false, resumable: false };
    },

    /** 跑一轮；事件未归一（见文件头注释），退出码如实反映到 status。 */
    async turn(input: AgentProviderTurnInput): Promise<AgentProviderTurnOutcome> {
        const threadId = input.threadId || createProviderThreadId("claude");
        const turnId = `claude-turn-${Date.now().toString(36)}`;
        input.onThread?.(threadId);
        input.onTurn?.(turnId);
        const code = await runClaudeTurn(input.prompt, input.emit);
        return { provider: "claude", threadId, turnId, status: code === 0 ? "completed" : "failed" };
    },

    async interrupt(): Promise<boolean> {
        return false;
    },

    /** 占位实现不提供模型列表。 */
    async listModels(): Promise<{ models: AgentProviderModel[] }> {
        return { models: [] };
    },
};

registerAgentProvider(claudeProvider);
