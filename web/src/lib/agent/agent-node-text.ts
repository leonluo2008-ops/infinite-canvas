import type { CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";

// 画布节点全文读取 + 生成任务提示词预览的纯函数层。
//
// 为什么单列一层：agent-site-tools.ts 依赖浏览器 store（zustand / localforage / i18n），
// 逻辑留在那里就没法在 node 侧真跑探针。这里不 import 任何浏览器运行时，
// agent-site-tools.ts 只是把 store 数据喂进来，所以探针跑的就是生产代码本身。

/** generation_get_status 的 prompt 预览阈值：超过即截断并置 promptTruncated=true；全文用 read_node_text 取。 */
export const PROMPT_PREVIEW_LIMIT = 4000;

export type GenerationStatus = "idle" | "queued" | "running" | "succeeded" | "failed";

export type GenerationStatusItem = {
    id: string;
    source: "canvas" | "image" | "video";
    status: GenerationStatus;
    kind?: string;
    title?: string;
    prompt?: string;
    /** 预览是否被截断（true 时 prompt 只有前 PROMPT_PREVIEW_LIMIT 字，全文走 read_node_text）。 */
    promptTruncated?: boolean;
    /** 截断前的完整字数（未截断时等于 prompt.length）。 */
    promptFullLength?: number;
    projectId?: string;
    createdAt?: string;
    updatedAt?: string;
    successCount?: number;
    failCount?: number;
    error?: string;
};

/** 工作台任务的结构下限（useWorkbenchAgentStore 的任务对象天然满足）。 */
export type WorkbenchTaskLike = {
    id: string;
    kind: "image" | "video";
    status: GenerationStatus;
    prompt?: string;
    createdAt?: string;
    updatedAt?: string;
    successCount?: number;
    failCount?: number;
    error?: string;
};

/** 画布节点/项目的结构下限，避免本模块反向依赖 store 类型。 */
export type CanvasNodeLike = {
    id: string;
    type: string;
    title?: string;
    metadata?: Record<string, unknown>;
};

export type CanvasProjectLike = {
    id: string;
    nodes?: CanvasNodeLike[];
};

export type NodeFullText = {
    nodeId: string;
    projectId: string;
    type: string;
    title: string;
    /** 文本类节点正文（逐字全文）；图片/视频/音频节点的 dataURL 只回长度不回原文，见 contentOmitted。 */
    content?: string;
    /** 生成提示词（逐字全文）。 */
    prompt?: string;
    /** 配置节点的提示词编辑器内容（逐字全文）。 */
    composerContent?: string;
    contentLength: number;
    promptLength: number;
    composerContentLength: number;
    /** 主文本字数（content → prompt → composerContent 第一个非空），Agent 可直接与写入值对账。 */
    length: number;
    /** 主文本取自哪个字段。 */
    field?: "content" | "prompt" | "composerContent";
    /** 媒体节点 content 是内联 dataURL 时不回原文（防撑爆上下文），这里给出省略原因。 */
    contentOmitted?: "media-data-url";
};

type GenerationStatusInput = Record<string, unknown>;

/** 组装 generation_get_status 的返回体：画布节点 + 工作台任务，prompt 走预览并带截断标记。 */
export function buildGenerationStatus(input: GenerationStatusInput, canvasSnapshot?: CanvasAgentSnapshot | null, workbenchTasks: WorkbenchTaskLike[] = []) {
    const scope = input.scope === "canvas" || input.scope === "image" || input.scope === "video" ? input.scope : "all";
    const taskId = typeof input.taskId === "string" ? input.taskId : "";
    const nodeIds = new Set(Array.isArray(input.nodeIds) ? input.nodeIds.filter((id): id is string => typeof id === "string") : []);
    const limit = Math.max(1, Math.min(100, Math.floor(Number(input.limit)) || 20));
    const tasks: GenerationStatusItem[] = [];
    const includeCanvas = (scope === "all" || scope === "canvas") && (!taskId || nodeIds.size > 0);
    const includeWorkbench = !nodeIds.size || Boolean(taskId);

    if (includeCanvas && canvasSnapshot) {
        canvasSnapshot.nodes.forEach((node) => {
            const status = normalizeCanvasGenerationStatus(node.metadata?.status);
            if (!status || (nodeIds.size && !nodeIds.has(node.id))) return;
            const metadata = node.metadata || {};
            if (!nodeIds.size && node.type !== "config" && status !== "running" && status !== "failed" && !metadata.generationMode && !metadata.generationType && !metadata.model) return;
            const preview = previewPrompt(metadata.prompt || metadata.composerContent);
            tasks.push({ id: node.id, source: "canvas", status, kind: metadata.generationMode || node.type, title: node.title, ...preview, projectId: canvasSnapshot.projectId, error: metadata.errorDetails });
        });
    }

    if (includeWorkbench) {
        workbenchTasks.forEach((task) => {
            if ((scope === "image" || scope === "video") && task.kind !== scope) return;
            if (scope === "canvas" || (taskId && task.id !== taskId)) return;
            const preview = previewPrompt(task.prompt);
            tasks.push({ ...task, source: task.kind, ...preview });
        });
    }

    tasks.sort((a, b) => generationStatusOrder(a.status) - generationStatusOrder(b.status) || (b.updatedAt || "").localeCompare(a.updatedAt || ""));
    const summary: Record<GenerationStatus, number> = { idle: 0, queued: 0, running: 0, succeeded: 0, failed: 0 };
    tasks.forEach((task) => (summary[task.status] += 1));
    return { total: tasks.length, summary, tasks: tasks.slice(0, limit) };
}

function generationStatusOrder(status: GenerationStatus) {
    return status === "running" ? 0 : status === "queued" ? 1 : 2;
}

function normalizeCanvasGenerationStatus(status: unknown): GenerationStatus | null {
    if (status === "idle") return "idle";
    if (status === "loading") return "running";
    if (status === "success") return "succeeded";
    if (status === "error") return "failed";
    return null;
}

/** prompt 预览：超 PROMPT_PREVIEW_LIMIT 才截断，并带上截断标记与完整字数。 */
export function previewPrompt(prompt: unknown): Pick<GenerationStatusItem, "prompt" | "promptTruncated" | "promptFullLength"> {
    const value = typeof prompt === "string" ? prompt.trim() : "";
    if (!value) return { prompt: undefined, promptTruncated: false, promptFullLength: 0 };
    const truncated = value.length > PROMPT_PREVIEW_LIMIT;
    return { prompt: truncated ? `${value.slice(0, PROMPT_PREVIEW_LIMIT)}...` : value, promptTruncated: truncated, promptFullLength: value.length };
}

/** 按 nodeId 读节点全文：优先当前打开的画布快照，找不到再落到已持久化项目里找。 */
export function readNodeFullText(params: { nodeId: string; canvasSnapshot?: CanvasAgentSnapshot | null; projects?: CanvasProjectLike[] }): NodeFullText | null {
    const nodeId = typeof params.nodeId === "string" ? params.nodeId.trim() : "";
    if (!nodeId) return null;
    const snapshot = params.canvasSnapshot;
    const snapshotNode = snapshot?.nodes?.find((node) => node.id === nodeId);
    if (snapshotNode) return buildNodeFullText(snapshotNode, snapshot?.projectId || "");
    for (const project of params.projects || []) {
        const node = project.nodes?.find((item) => item.id === nodeId);
        if (node) return buildNodeFullText(node, project.id);
    }
    return null;
}

const MEDIA_NODE_TYPES = new Set(["image", "video", "audio"]);

/** 单节点全文抽取：text/prompt/composerContent 一律逐字原文，不 trim 不 slice 不折叠。 */
function buildNodeFullText(node: CanvasNodeLike, projectId: string): NodeFullText {
    const metadata = (node.metadata || {}) as Record<string, unknown>;
    const raw = (key: "content" | "prompt" | "composerContent") => (typeof metadata[key] === "string" ? (metadata[key] as string) : undefined);
    const rawContent = raw("content");
    const prompt = raw("prompt");
    const composerContent = raw("composerContent");
    // 媒体节点的 content 是内联 dataURL（常达数 MB）。逐字回传只会把上下文撑爆，
    // 对读提示词毫无用处，所以只回标记 + 字数；提示词字段本身永远全文。
    const mediaDataUrl = rawContent !== undefined && MEDIA_NODE_TYPES.has(node.type) && rawContent.startsWith("data:");
    const content = mediaDataUrl ? undefined : rawContent;
    const primary: Array<["content" | "prompt" | "composerContent", string | undefined]> = [["content", content], ["prompt", prompt], ["composerContent", composerContent]];
    const hit = primary.find(([, value]) => Boolean(value && value.trim()));
    return {
        nodeId: node.id,
        projectId,
        type: node.type,
        title: node.title || "",
        ...(content !== undefined ? { content } : {}),
        ...(prompt !== undefined ? { prompt } : {}),
        ...(composerContent !== undefined ? { composerContent } : {}),
        contentLength: rawContent?.length || 0,
        promptLength: prompt?.length || 0,
        composerContentLength: composerContent?.length || 0,
        length: hit ? (hit[1] as string).length : 0,
        ...(hit ? { field: hit[0] } : {}),
        ...(mediaDataUrl ? { contentOmitted: "media-data-url" as const } : {}),
    };
}
