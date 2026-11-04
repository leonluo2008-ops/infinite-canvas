import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { loadConfig, type HermesProviderConfig } from "../config.js";
import type { AgentAttachment } from "./types.js";
import { createAgentLogWriter } from "../utils/agent-runtime.js";
import { logger } from "../utils/logger.js";
import { errorMessage, type JsonRecord } from "../utils/value.js";
import { createProviderThreadId, registerAgentProvider, type AgentProvider, type AgentProviderEvent, type AgentProviderModel, type AgentProviderSession, type AgentProviderSessionInput, type AgentProviderTurnInput, type AgentProviderTurnOutcome } from "./provider.js";

/**
 * Hermes 适配器：每轮一个一次性子进程。
 *
 * 源协议 `hermes chat -q <prompt> --oneshot --format stream-json`（`hermes_cli/stream_json.py`）：
 * `system/init` → `text` 增量 / `tool_use` / `tool_result` → 一个终态 `result`。
 * 进程即线程（Hermes 没有常驻 turn 通道），多轮靠 `--resume <session-id>`。
 *
 * MCP 工具面**不在代码里注入**（这点与 Codex 的 `config.mcp_servers` 不同），由部署面预配：
 * `hermes mcp add infinite-canvas -- node <entry> mcp`。这里只认已经注册好的 `mcp__*` 工具名。
 *
 * 图片附件：`AgentAttachment.dataUrl` 先落成本地临时文件，再按 CLI 是否接 `--image` 二选一投递——
 * 接就用 `--image <path>` 原生附图，不接（stream-json 走网关的发行方式）就把路径写进提示词交给
 * `vision_analyze`。两条路都在本轮结束后删掉临时目录。
 */

/** 文本增量合并窗口，与 `codex-client.ts` 的 40ms 节流一致。 */
const STREAM_UPDATE_INTERVAL_MS = 40;
/** 工具输出写入事件前的截断长度，避免一次画布快照把 SSE 撑爆。 */
const TOOL_OUTPUT_CAP = 20_000;
const DEFAULT_COMMAND = "hermes";
/** 附件临时目录前缀；一轮结束整目录删除，与 codex.ts 一样放系统临时目录。 */
const ATTACHMENT_DIR_PREFIX = "infinite-canvas-hermes-";

/** Hermes 的 token 计数（`result.tokens`）。 */
export type HermesTokens = { input: number; output: number; total: number; cacheRead: number; cacheWrite: number };

/** 一行 stream-json 的归一结果；`raw` 保留非 JSON 的诊断行。 */
export type HermesStreamEvent =
    | { kind: "init"; sessionId: string; model: string }
    | { kind: "text"; text: string }
    | { kind: "tool_use"; name: string; toolCallId: string; input?: JsonRecord }
    | { kind: "tool_result"; name: string; toolCallId: string; output: string; isError: boolean; durationMs: number }
    | { kind: "result"; sessionId: string; exitCode: number; text: string; tokens: HermesTokens; durationMs: number; error?: string }
    | { kind: "raw"; text: string };

/** 画布侧 threadId → Hermes session id；进程内有效，用于下一轮 `--resume`。 */
const hermesSessions = new Map<string, string>();
let activeRun: HermesTurnRun | null = null;

/** 解析一行 `--format stream-json` 输出；纯函数，便于单测。 */
export function parseHermesStreamLine(line: string): HermesStreamEvent | null {
    const text = line.trim();
    if (!text) return null;
    let value: unknown;
    try {
        value = JSON.parse(text);
    } catch {
        return { kind: "raw", text };
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return { kind: "raw", text };
    const record = value as JsonRecord;
    const type = String(record.type || "");
    if (type === "system") {
        // 只认 init；其余 system 事件（网关提示等）不产生时间线条目。
        if (String(record.subtype || "") !== "init") return null;
        return { kind: "init", sessionId: String(record.session_id || ""), model: String(record.model || "") };
    }
    if (type === "text") return { kind: "text", text: String(record.text ?? "") };
    if (type === "tool_use") {
        const input = record.input && typeof record.input === "object" && !Array.isArray(record.input) ? record.input as JsonRecord : undefined;
        return { kind: "tool_use", name: String(record.name || "unknown"), toolCallId: String(record.tool_call_id || ""), ...(input ? { input } : {}) };
    }
    if (type === "tool_result") {
        return {
            kind: "tool_result",
            name: String(record.name || "unknown"),
            toolCallId: String(record.tool_call_id || ""),
            output: String(record.output ?? ""),
            isError: Boolean(record.is_error),
            durationMs: Number(record.duration_ms) || 0,
        };
    }
    if (type === "result") {
        const tokens = record.tokens && typeof record.tokens === "object" ? record.tokens as JsonRecord : {};
        return {
            kind: "result",
            sessionId: String(record.session_id || ""),
            exitCode: Number(record.exit_code) || 0,
            text: String(record.text ?? ""),
            tokens: {
                input: Number(tokens.input) || 0,
                output: Number(tokens.output) || 0,
                total: Number(tokens.total) || 0,
                cacheRead: Number(tokens.cache_read) || 0,
                cacheWrite: Number(tokens.cache_write) || 0,
            },
            durationMs: Number(record.duration_ms) || 0,
            ...(record.error ? { error: String(record.error) } : {}),
        };
    }
    return null;
}

/** 把 Hermes 的 `mcp__<server>__<tool>` 还原成画布工具名；其余工具原样返回。 */
export function normalizeHermesToolName(name: string) {
    const match = /^mcp__(.+)__(.+)$/.exec(name);
    return match ? match[2] : name;
}

/** 把 Hermes 的 token 计数翻译成前端 `usage` 字段（`agent-event-formatters.ts` 的 eventUsage）。 */
function hermesUsage(tokens: HermesTokens) {
    return { input_tokens: tokens.input, output_tokens: tokens.output, cached_input_tokens: tokens.cacheRead };
}

/** 读取服务端 Hermes 配置（`~/.infinite-canvas/canvas-agent.json` 的 hermes 段）。 */
function hermesConfig(): HermesProviderConfig {
    try {
        return loadConfig().hermes || {};
    } catch {
        return {};
    }
}

/** 解析可执行文件：显式配置优先，其次在 PATH 里找 hermes.exe/hermes.cmd。 */
export function resolveHermesCommand(configured?: string) {
    if (configured?.trim()) return configured.trim();
    if (process.env.CANVAS_AGENT_HERMES_COMMAND?.trim()) return process.env.CANVAS_AGENT_HERMES_COMMAND.trim();
    if (process.platform !== "win32") return DEFAULT_COMMAND;
    const dirs = (process.env.PATH || "").split(path.delimiter).filter(Boolean);
    for (const dir of dirs) {
        for (const name of ["hermes.exe", "hermes.cmd", "hermes.bat"]) {
            try {
                const candidate = path.join(dir, name);
                if (fs.existsSync(candidate)) return candidate;
            } catch {
                // 单个路径不可读时继续找下一个。
            }
        }
    }
    return DEFAULT_COMMAND;
}

/** 组装 `hermes chat` 参数；prompt 作为独立 argv 传入，不经过 shell 解释。 */
export function hermesChatArgs(input: { prompt: string; sessionId?: string; config?: HermesProviderConfig; images?: string[] }) {
    const config = input.config || {};
    const args = ["chat", "-q", input.prompt, "--oneshot", "--format", "stream-json"];
    if (input.sessionId) args.push("--resume", input.sessionId);
    if (config.model) args.push("--model", config.model);
    if (config.toolsets) args.push("--toolsets", config.toolsets);
    if (config.reasoning) args.push("--reasoning", config.reasoning);
    // 一张图一个 `--image`；无附件时不加参数，行为与接入附件前完全一致。
    for (const image of input.images || []) args.push("--image", image);
    return args;
}

/** 已落盘的附件：目录用于整目录清理，`files` 是交给 CLI 或提示词的绝对路径。 */
export type HermesAttachmentFiles = { dir: string; files: string[] };

/**
 * 把 dataUrl 图片落成本地临时文件。
 *
 * Hermes 的图片入口只认本地路径（`--image <path>`），不收 base64；
 * 非图片附件（未来可能有 pdf 等）直接跳过，与 codex 的 `writeAttachmentFiles` 同口径。
 */
export async function writeHermesAttachmentFiles(attachments: AgentAttachment[]): Promise<HermesAttachmentFiles> {
    const images = attachments.filter((item) => item.dataUrl?.startsWith("data:image/"));
    if (!images.length) return { dir: "", files: [] };
    const dir = await fs.promises.mkdtemp(path.join(os.tmpdir(), ATTACHMENT_DIR_PREFIX));
    const files: string[] = [];
    for (const [index, item] of images.entries()) {
        const [, meta = "", data = ""] = item.dataUrl?.match(/^data:([^;]+);base64,(.+)$/) || [];
        if (!data) throw new Error(`图片附件无效：${item.name || "未命名图片"}`);
        const file = path.join(dir, `image-${index + 1}.${imageExt(meta || item.type)}`);
        await fs.promises.writeFile(file, Buffer.from(data, "base64"));
        files.push(file);
    }
    return { dir, files };
}

/** 删除本轮附件目录；turn 结束（进程已读不到文件）后调用，失败不影响终态。 */
export async function removeHermesAttachmentFiles(dir: string) {
    if (!dir) return;
    await fs.promises.rm(dir, { recursive: true, force: true }).catch(() => undefined);
}

/** 根据图片 MIME 或文件名猜扩展名；Hermes 只按扩展名判类型。 */
function imageExt(type = "") {
    const text = type.toLowerCase();
    if (text.includes("png")) return "png";
    if (text.includes("webp")) return "webp";
    if (text.includes("gif")) return "gif";
    return "jpg";
}

/**
 * `--image` 被当前 Hermes 发行方式拒收时的提示词替代写法。
 *
 * 新版 Hermes 的 `--format stream-json` 一律交给网关传输，网关会拒绝 `--image`
 * （`Unsupported gateway CLI options: --image`），但 CLI 仍有 `vision_analyze` 工具能直接读本地文件。
 * 把路径写进提示词，图片内容照样进模型，代价是多一次工具调用。
 */
export function appendHermesAttachmentRefs(prompt: string, files: string[]) {
    if (!files.length) return prompt;
    const list = files.map((file, index) => `${index + 1}. ${file}`).join("\n");
    return `${prompt}\n\n本轮附带 ${files.length} 张图片，已存到本机（临时文件，本轮结束后删除）：\n${list}\n先用 vision_analyze 逐张看图（image_url 直接传上面的路径），再动手做用户要的事；用户说「这张图」「这几张图」时指的就是它们。`;
}

/** 判断探测输出是否属于「网关不支持 --image」；纯函数，便于单测。 */
export function hermesImageFlagRejected(output: string) {
    return /Unsupported gateway CLI options/i.test(output);
}

/** `--image` 是否可用；null 表示尚未探测。每个进程只探一次。 */
let imageFlagSupport: boolean | null = null;

/**
 * 真跑一次 CLI 看它接不接 `--image`，返回原始输出。
 *
 * `--help` 会列出 `--image`，但网关形态的执行路径会在参数校验阶段拒绝它，所以只能实测。
 * 探测用「不存在的图片路径」：可用时 CLI 在读图阶段就报 `Image file not found`，
 * 不可用时在参数校验阶段报 `Unsupported gateway CLI options`——两种情况都在起模型之前退出，不花钱。
 */
function probeImageFlag(command: string, cwd: string) {
    return new Promise<string>((resolve) => {
        const missing = path.join(os.tmpdir(), `infinite-canvas-hermes-probe-${process.pid}.png`);
        let output = "";
        let settled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;
        const finish = () => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            resolve(output);
        };
        try {
            const child = spawn(command, ["chat", "-q", "probe", "--oneshot", "--format", "stream-json", "--image", missing], {
                cwd,
                stdio: ["ignore", "pipe", "pipe"],
                windowsHide: true,
                shell: process.platform === "win32" && /\.(cmd|bat)$/i.test(command),
            });
            timer = setTimeout(() => {
                child.kill();
                finish();
            }, 30_000);
            child.stdout?.on("data", (chunk) => (output += chunk.toString()));
            child.stderr?.on("data", (chunk) => (output += chunk.toString()));
            child.on("error", finish);
            child.on("close", finish);
        } catch {
            // 探不动就当可用：真的起不来时，turn 自己的错误更具体。
            resolve("");
        }
    });
}

/** 探测并缓存 `--image` 支持情况；探测本身不打断事件循环（画布 SSE 还在往外写）。 */
async function detectImageFlagSupport(command: string, cwd: string) {
    if (imageFlagSupport !== null) return imageFlagSupport;
    const output = await probeImageFlag(command, cwd);
    imageFlagSupport = !hermesImageFlagRejected(output);
    return imageFlagSupport;
}

/** 一轮 Hermes 子进程的事件搬运与终态管理。 */
class HermesTurnRun {
    private buffer = "";
    private readonly stderr: ReturnType<typeof createAgentLogWriter>;
    private threadStarted = false;
    private turnStarted = false;
    private messageId = "";
    private messageText = "";
    private messageSeq = 0;
    private deltaBuffer = "";
    private deltaTimer: ReturnType<typeof setTimeout> | null = null;
    private toolSeq = 0;
    private lastToolId = "";
    private sessionId = "";
    private resultSeen = false;
    private interrupted = false;
    private settled = false;
    private resolve!: (outcome: AgentProviderTurnOutcome) => void;
    readonly done: Promise<AgentProviderTurnOutcome>;

    constructor(private child: ChildProcess, private input: AgentProviderTurnInput, readonly threadId: string, private turnId: string) {
        this.stderr = createAgentLogWriter((text) => input.emit("agent_log", { text }));
        this.done = new Promise((resolve) => (this.resolve = resolve));
    }

    /** 挂接子进程输出并返回终态 promise。 */
    start() {
        this.child.stdout?.on("data", (chunk) => this.read(chunk.toString()));
        this.child.stderr?.on("data", (chunk) => this.stderr.write(chunk.toString()));
        this.child.on("error", (error) => {
            this.stderr.flush();
            const message = `无法启动 Hermes：${error.message}`;
            this.input.emit("agent_error", { message });
            this.settle("failed", { error: message });
        });
        this.child.on("close", (code) => this.onClose(code));
        return this.done;
    }

    /** 中断当前 turn：Hermes 一次性进程没有 interrupt 通道，只能结束进程。 */
    interrupt() {
        this.interrupted = true;
        this.child.kill();
        return true;
    }

    private read(chunk: string) {
        this.buffer += chunk;
        const lines = this.buffer.split(/\r?\n/);
        this.buffer = lines.pop() || "";
        lines.forEach((line) => this.consume(line));
    }

    private consume(line: string) {
        const event = parseHermesStreamLine(line);
        if (!event) return;
        if (event.kind === "raw") {
            // 标准输出里偶尔混着网关横幅与结尾的 session_id 行；空白与重复的 session 行不进日志面板。
            const text = event.text.trim();
            if (!text || /^session_id:/i.test(text)) return;
            this.input.emit("agent_log", { text });
            return;
        }
        if (event.kind === "init") return this.onInit(event.sessionId, event.model);
        if (event.kind === "text") return this.onText(event.text);
        if (event.kind === "tool_use") return this.onToolUse(event.name, event.toolCallId, event.input);
        if (event.kind === "tool_result") return this.onToolResult(event.name, event.toolCallId, event.output, event.isError, event.durationMs);
        return this.onResult(event);
    }

    private onInit(sessionId: string, model: string) {
        // init 在网关分配 session 之前写出，session id 通常为空；真正可用的是 result 里的那个。
        if (sessionId) this.sessionId ||= sessionId;
        if (model) this.input.emit("agent_log", { text: `Hermes 模型：${model}` });
        this.ensureThread();
        this.ensureTurn();
    }

    private onText(text: string) {
        if (!text) return;
        this.ensureThread();
        this.ensureTurn();
        if (!this.messageId) {
            this.messageId = `${this.turnId}:msg-${++this.messageSeq}`;
            this.emit("item.started", { item: { id: this.messageId, type: "agent_message", text: "" } });
        }
        this.messageText += text;
        this.deltaBuffer += text;
        if (this.deltaTimer) return;
        this.deltaTimer = setTimeout(() => this.flushDelta(), STREAM_UPDATE_INTERVAL_MS);
    }

    private onToolUse(name: string, toolCallId: string, input?: JsonRecord) {
        this.ensureThread();
        this.ensureTurn();
        // 文本段先收尾，工具条目才不会插进流式文本中间。
        this.closeMessage();
        const id = toolCallId || `${this.turnId}:tool-${++this.toolSeq}`;
        this.lastToolId = id;
        this.emit("item.started", { item: { id, type: "mcp_tool_call", tool: normalizeHermesToolName(name), status: "running", arguments: input ?? {} } });
    }

    private onToolResult(name: string, toolCallId: string, output: string, isError: boolean, durationMs: number) {
        const id = toolCallId || this.lastToolId || `${this.turnId}:tool-${++this.toolSeq}`;
        const text = output.length > TOOL_OUTPUT_CAP ? `${output.slice(0, TOOL_OUTPUT_CAP)}…` : output;
        this.emit("item.completed", {
            item: {
                id,
                type: "mcp_tool_call",
                tool: normalizeHermesToolName(name),
                status: isError ? "failed" : "completed",
                result: text,
                ...(isError ? { error: { message: text || "工具执行失败" } } : {}),
                ...(durationMs ? { duration_ms: durationMs } : {}),
            },
        });
    }

    private onResult(event: Extract<HermesStreamEvent, { kind: "result" }>) {
        this.resultSeen = true;
        if (event.sessionId) {
            this.sessionId = event.sessionId;
            hermesSessions.set(this.threadId, event.sessionId);
        }
        this.ensureThread();
        this.ensureTurn();
        this.closeMessage(event.text || this.messageText);
        const failed = Boolean(event.error) || event.exitCode !== 0;
        this.emit("turn.completed", {
            status: failed ? "failed" : "completed",
            ...(failed ? { error: { message: event.error || `Hermes exit code ${event.exitCode}` } } : {}),
            usage: hermesUsage(event.tokens),
            duration_ms: event.durationMs,
            // tokens 落在 metadata：前端 eventUsage 与历史落盘都能取到。
            metadata: {
                provider: "hermes",
                session_id: event.sessionId,
                duration_ms: event.durationMs,
                tokens: { input: event.tokens.input, output: event.tokens.output, total: event.tokens.total, cache_read: event.tokens.cacheRead, cache_write: event.tokens.cacheWrite },
            },
        });
        this.settle(failed ? "failed" : "completed", {
            text: event.text || this.messageText,
            usage: hermesUsage(event.tokens),
            ...(failed ? { error: event.error || `Hermes exit code ${event.exitCode}` } : {}),
        });
    }

    private onClose(code: number | null) {
        this.stderr.flush();
        if (this.resultSeen) return;
        this.ensureThread();
        this.ensureTurn();
        this.closeMessage();
        const status = this.interrupted ? "interrupted" : "failed";
        const message = this.interrupted ? "Hermes turn 已被中断" : `Hermes 进程在返回结果前退出（exit code ${code ?? 0}）`;
        this.emit("turn.completed", { status, error: { message } });
        if (!this.interrupted) this.input.emit("agent_error", { message });
        this.settle(status, { error: message });
    }

    private emit(type: AgentProviderEvent["type"], payload: Partial<AgentProviderEvent>) {
        this.input.emit("agent_event", { agent: "hermes", type, thread_id: this.threadId, turn_id: this.turnId, ...payload });
    }

    private ensureThread() {
        if (this.threadStarted) return;
        this.threadStarted = true;
        this.emit("thread.started", {});
    }

    private ensureTurn() {
        if (this.turnStarted) return;
        this.turnStarted = true;
        this.emit("turn.started", {});
        this.input.onTurn?.(this.turnId);
    }

    /** 输出缓冲中的增量，保持前端流式渲染与 Codex 一致。 */
    private flushDelta() {
        if (this.deltaTimer) clearTimeout(this.deltaTimer);
        this.deltaTimer = null;
        const delta = this.deltaBuffer;
        this.deltaBuffer = "";
        if (!delta || !this.messageId) return;
        this.emit("item.updated", { item: { id: this.messageId, type: "agent_message", delta } });
    }

    /** 结束当前 assistant 段落；`result` 已带全量文本时用它校正合并后的增量。 */
    private closeMessage(text = this.messageText) {
        this.flushDelta();
        if (!this.messageId) return;
        this.emit("item.completed", { item: { id: this.messageId, type: "agent_message", text } });
        this.messageId = "";
        this.messageText = "";
    }

    private settle(status: AgentProviderTurnOutcome["status"], extra: { text?: string; usage?: unknown; error?: string } = {}) {
        if (this.settled) return;
        this.settled = true;
        if (this.deltaTimer) clearTimeout(this.deltaTimer);
        this.deltaTimer = null;
        this.resolve({ provider: "hermes", threadId: this.threadId, turnId: this.turnId, status, ...(extra.text ? { text: extra.text } : {}), ...(extra.usage ? { usage: extra.usage } : {}) });
        this.input.emit("agent_done", { agent: "hermes", status, thread_id: this.threadId, turn_id: this.turnId, ...(extra.error ? { error: { message: extra.error } } : {}) });
    }
}

/** 串行队列：Hermes 每轮一个进程，同一时刻只允许一轮，避免同一 thread 的事件交叉。 */
let hermesQueue: Promise<unknown> = Promise.resolve();

/** 排队执行一轮；行为对齐 codex 的 `runCodexTurn`，失败不会污染后续队列。 */
export function runHermesTurn(input: AgentProviderTurnInput) {
    const queued = hermesQueue.catch(() => undefined).then(() => startHermesTurn(input));
    hermesQueue = queued.catch(() => undefined);
    return queued;
}

/** 真正起进程的一轮；终态由 `result` 事件或进程退出决定。 */
async function startHermesTurn(input: AgentProviderTurnInput): Promise<AgentProviderTurnOutcome> {
    const threadId = input.threadId || createProviderThreadId("hermes");
    const turnId = `hermes-turn-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
    const config = hermesConfig();
    const command = resolveHermesCommand(config.command);
    const cwd = input.cwd || process.cwd();
    const resumeId = hermesSessions.get(threadId) || "";
    // 附件先落盘：`--image` 与提示词引用都需要真实文件路径，dataUrl 本身进不去 CLI。
    let attachmentDir = "";
    let files: string[] = [];
    let images: string[] = [];
    try {
        const written = await writeHermesAttachmentFiles(input.attachments || []);
        attachmentDir = written.dir;
        files = written.files;
    } catch (error) {
        const message = errorMessage(error);
        input.emit("agent_error", { message });
        return { provider: "hermes", threadId, turnId, status: "failed" };
    }
    if (files.length) {
        // 网关形态的 Hermes 不认 `--image`（stream-json 一律走网关），改把路径写进提示词喂给 vision_analyze。
        if (await detectImageFlagSupport(command, cwd)) images = files;
        else input.emit("agent_log", { text: `当前 Hermes 不接 --image，已把 ${files.length} 张图片的本地路径写进提示词供 vision_analyze 读取` });
        input.emit("agent_log", { text: `已附带 ${files.length} 张图片` });
    }
    const prompt = files.length && !images.length ? appendHermesAttachmentRefs(input.prompt, files) : input.prompt;
    const args = hermesChatArgs({ prompt, sessionId: resumeId, config: { ...config, ...(input.model ? { model: input.model } : {}) }, images });
    logger.info("Hermes turn starting", { threadId, turnId, command, model: input.model || config.model || "default", promptLength: prompt.length, attachmentCount: files.length, imageFlag: images.length > 0, resumed: Boolean(resumeId) });
    input.onThread?.(threadId);
    let child: ChildProcess;
    try {
        child = spawn(command, args, { cwd, stdio: ["ignore", "pipe", "pipe"], windowsHide: true, shell: process.platform === "win32" && /\.(cmd|bat)$/i.test(command) });
    } catch (error) {
        const message = `无法启动 Hermes：${errorMessage(error)}`;
        await removeHermesAttachmentFiles(attachmentDir);
        input.emit("agent_error", { message });
        return { provider: "hermes", threadId, turnId, status: "failed" };
    }
    const run = new HermesTurnRun(child, input, threadId, turnId);
    activeRun = run;
    // 进程退出后才删临时目录：图片要活到本轮的 vision_analyze 读完。
    child.once("close", () => void removeHermesAttachmentFiles(attachmentDir));
    try {
        return await run.start();
    } finally {
        if (activeRun === run) activeRun = null;
        await removeHermesAttachmentFiles(attachmentDir);
    }
}

/** Hermes 实现：子进程 + 逐行 JSONL，事件归一到 6 类。 */
export const hermesProvider: AgentProvider = {
    id: "hermes",
    label: "Hermes",
    capabilities: { session: false, threads: false, models: false, attachments: true, approvals: false, interrupt: true, resume: true },

    /** Hermes 没有常驻会话，这里只分配画布侧 id，进程留到 turn 时再起。 */
    async startSession(input: AgentProviderSessionInput): Promise<AgentProviderSession> {
        const threadId = input.threadId || createProviderThreadId("hermes");
        return { provider: "hermes", threadId, resumable: hermesSessions.has(threadId), ...(input.model ? { model: input.model } : {}) };
    },

    /** 续跑已有会话：session id 由上一轮 `result` 事件记在同一进程内。 */
    async resume(input: AgentProviderSessionInput & { threadId: string }): Promise<AgentProviderSession> {
        const sessionId = hermesSessions.get(input.threadId) || "";
        return { provider: "hermes", threadId: input.threadId, resumed: Boolean(sessionId), resumable: Boolean(sessionId), ...(sessionId ? { sessionId } : {}) };
    },

    /** 起一个一次性进程跑完这一轮；队列保证同一时刻只有一轮。 */
    turn(input: AgentProviderTurnInput) {
        return runHermesTurn(input);
    },

    /** 结束 Hermes 一次性进程；没有可用的软中断通道。 */
    async interrupt(options?: { threadId?: string }) {
        if (!activeRun) return false;
        if (options?.threadId && activeRun.threadId !== options.threadId) return false;
        return activeRun.interrupt();
    },

    /** Hermes 不做 `model/list`：只把服务端配置的默认模型报给前端。 */
    async listModels(): Promise<{ models: AgentProviderModel[]; raw?: unknown }> {
        const model = hermesConfig().model?.trim() || "";
        const id = model || "hermes-default";
        return {
            models: [{
                id,
                model: id,
                displayName: model || "Hermes 默认模型",
                description: model
                    ? "由 ~/.infinite-canvas/canvas-agent.json 的 hermes.model 指定"
                    : "由 Hermes 自身配置决定；需要固定模型时在 canvas-agent.json 的 hermes.model 填写",
                isDefault: true,
                defaultReasoningEffort: "medium",
                supportedReasoningEfforts: [{ reasoningEffort: "medium" }],
            }],
        };
    },
};

registerAgentProvider(hermesProvider);

/** 读取已知会话映射，供排障「为什么没走 --resume」。 */
export function hermesSessionId(threadId: string) {
    return hermesSessions.get(threadId) || "";
}
