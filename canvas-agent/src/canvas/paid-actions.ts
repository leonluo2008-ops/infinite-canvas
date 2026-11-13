import type { ToolName } from "./schemas.js";
import type { CanvasNode, CanvasSnapshot } from "./types.js";

export type PaidActionMode = "text" | "image" | "video" | "audio";

/**
 * 一次付费动作清单条目：确认卡逐条展示，一次确认只放行清单内被勾选的条目。
 * `estimate` 恒为 null —— 当前没有渠道报价能力，禁止在实现里自填经验值，
 * 前端按「按渠道计价」占位展示（P0b 审计红线）。
 */
export type PaidAction = {
    id: string;
    tool: string;
    kind: "run_generation" | "generation_tool";
    /** 在 ops 数组中的下标，仅 canvas_apply_ops 的 run_generation 条目有值。 */
    opIndex?: number;
    nodeId?: string;
    mode: PaidActionMode;
    /** 渠道/模型名（取自节点 metadata.model 或工具参数），取不到为空串。 */
    channel: string;
    /** 参数摘要，只用于人读，不参与下发。 */
    prompt: string;
    /** 本次预计创建次数：图片取节点 count，其余为 1。 */
    count: number;
    estimate: null;
};

export type PaidDecision = {
    /** 确认后实际下发的工具名；null 表示全部拒绝，不向页面下发任何动作。 */
    dispatchName: string | null;
    dispatchInput: Record<string, unknown> | null;
    approved: PaidAction[];
    rejected: PaidAction[];
};

const PROMPT_SUMMARY_MAX = 120;
const PAID_SITE_TOOLS = new Set<string>(["workbench_image_generate", "workbench_video_generate"]);

function findNode(state: CanvasSnapshot | null, id: string): CanvasNode | undefined {
    return (state?.nodes || []).find((node) => node.id === id);
}

function metadataOf(node: CanvasNode | undefined): Record<string, unknown> {
    return node?.metadata && typeof node.metadata === "object" ? node.metadata : {};
}

function paidMode(value: unknown, fallback: unknown): PaidActionMode {
    const mode = value ?? fallback;
    return mode === "text" || mode === "video" || mode === "audio" ? mode : "image";
}

function paidCount(mode: PaidActionMode, metadata: Record<string, unknown>): number {
    if (mode !== "image") return 1;
    const count = Number(metadata.count);
    return Number.isFinite(count) && count > 1 ? Math.floor(count) : 1;
}

function summarizePrompt(value: string): string {
    const text = value.replace(/\s+/g, " ").trim();
    return text.length > PROMPT_SUMMARY_MAX ? `${text.slice(0, PROMPT_SUMMARY_MAX)}…` : text;
}

/**
 * 判定一次画布工具调用里是否含付费动作，并列出逐条清单。
 * 判据只看「真正会向渠道提交付费任务」的动作：
 * - canvas_apply_ops 内的 run_generation（所有 flow/create/generate 工具最终都归一到它）；
 * - 工作台生图/生视频（run !== false 即会自动点生成）。
 * 其余写操作（add_node/update_node/connect_nodes 等）不算付费动作。
 */
export function collectPaidActions(name: string, input: Record<string, unknown>, state: CanvasSnapshot | null): PaidAction[] {
    if (name === "canvas_apply_ops") {
        const ops = Array.isArray(input.ops) ? (input.ops as Array<Record<string, unknown>>) : [];
        return ops.flatMap((op, index): PaidAction[] => {
            if (!op || op.type !== "run_generation") return [];
            const nodeId = typeof op.nodeId === "string" ? op.nodeId : "";
            const metadata = metadataOf(findNode(state, nodeId));
            const mode = paidMode(op.mode, metadata.generationMode);
            const prompt = String(op.prompt || metadata.composerContent || metadata.prompt || "");
            return [{
                id: `run_generation:${index}`,
                tool: name,
                kind: "run_generation",
                opIndex: index,
                nodeId,
                mode,
                channel: typeof metadata.model === "string" ? metadata.model : "",
                prompt: summarizePrompt(prompt),
                count: paidCount(mode, metadata),
                estimate: null,
            }];
        });
    }
    if (PAID_SITE_TOOLS.has(name) && input.run !== false) {
        const prompt = String(input.prompt || "");
        return [{
            id: `${name}:0`,
            tool: name,
            kind: "generation_tool",
            mode: name === "workbench_video_generate" ? "video" : "image",
            channel: typeof input.model === "string" ? input.model : "",
            prompt: summarizePrompt(prompt),
            count: paidCount(name === "workbench_video_generate" ? "video" : "image", input),
            estimate: null,
        }];
    }
    return [];
}

/**
 * 按人工勾选结果裁剪下发内容：被拒条目一律不下发，未列入清单的动作原样保留（非付费写操作）。
 * 全部拒绝时返回 dispatchName=null，调用方不得向页面下发任何动作。
 */
export function applyPaidDecision(name: string, input: Record<string, unknown>, actions: PaidAction[], approvedIds: string[]): PaidDecision {
    const known = new Map(actions.map((action) => [action.id, action]));
    const approved: PaidAction[] = [];
    const seen = new Set<string>();
    approvedIds.forEach((id) => {
        const action = known.get(id);
        if (!action || seen.has(id)) return;
        seen.add(id);
        approved.push(action);
    });
    const rejected = actions.filter((action) => !seen.has(action.id));
    if (!approved.length) return { dispatchName: null, dispatchInput: null, approved, rejected };
    if (name === "canvas_apply_ops") {
        const approvedIndexes = new Set(approved.map((action) => action.opIndex));
        const ops = Array.isArray(input.ops) ? (input.ops as Array<Record<string, unknown>>) : [];
        const kept = ops.filter((op, index) => !(op && op.type === "run_generation") || approvedIndexes.has(index));
        return { dispatchName: name, dispatchInput: { ...input, ops: kept }, approved, rejected };
    }
    return { dispatchName: name, dispatchInput: input, approved, rejected };
}
