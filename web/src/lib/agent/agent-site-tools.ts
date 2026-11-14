import type { NavigateFunction } from "react-router-dom";

import i18n from "@/i18n";
import { fetchPrompts } from "@/services/api/prompts";
import { uploadImage } from "@/services/image-storage";
import { imageAspectOptions, imageQualityOptions, imageScaleOptions } from "@/components/image-settings-panel";
import { videoResolutionOptions, videoSecondsRange, videoSizeOptions } from "@/components/video-settings-panel";
import { buildGenerationStatus, readNodeFullText } from "@/lib/agent/agent-node-text";
import type { CanvasAgentSnapshot } from "@/lib/canvas/canvas-agent-ops";
import { getWorkflowBatchRuntime, startBatch } from "@/lib/canvas/workflow-batch-runtime";
import {
    extractWorkflowTemplateTool,
    runWorkflowBatchTool,
    runWorkflowInstanceTool,
    type ExtractTemplateInput,
    type RunWorkflowBatchInput,
    type RunWorkflowInstanceInput,
    type WorkflowBatchMaterialInput,
    type WorkflowEntryDeps,
} from "@/lib/canvas/workflow-batch-entry";
import { clampVideoSeconds } from "@/lib/media-size";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { useAssetStore } from "@/stores/use-asset-store";
import { modelOptionLabel, modelOptionName, normalizeModelOptionValue, selectableModelsByCapability, useConfigStore } from "@/stores/use-config-store";
import { useWorkbenchAgentStore } from "@/stores/use-workbench-agent-store";

// Execute site-level Agent tools in the browser, including canvas lists, workbench generation, prompt search, and asset operations.
// Their data lives locally in the browser through localforage and Zustand, so this module accesses the relevant stores directly.

export const SITE_TOOL_NAMES = [
    "canvas_list_projects",
    "generation_get_status",
    "read_node_text",
    "extract_workflow_template",
    "run_workflow_batch",
    "run_workflow_instance",
    "workbench_image_get_config",
    "workbench_image_generate",
    "workbench_video_get_config",
    "workbench_video_generate",
    "prompts_search",
    "assets_list",
    "assets_add",
] as const;

export type SiteToolName = (typeof SITE_TOOL_NAMES)[number];

export function isSiteTool(name: string): name is SiteToolName {
    return (SITE_TOOL_NAMES as readonly string[]).includes(name);
}

function siteText(key: string, options?: Record<string, unknown>) {
    return i18n.t(`agent.siteTools.${key}`, options);
}

export const SITE_TOOL_LABELS: Record<SiteToolName, string> = {
    get canvas_list_projects() { return siteText("canvasList"); },
    get generation_get_status() { return siteText("generationStatus"); },
    get read_node_text() { return siteText("readNodeText"); },
    get extract_workflow_template() { return siteText("workflowTemplate"); },
    get run_workflow_batch() { return siteText("workflowBatch"); },
    get run_workflow_instance() { return siteText("workflowInstance"); },
    get workbench_image_get_config() { return siteText("imageConfig"); },
    get workbench_image_generate() { return siteText("imageGenerate"); },
    get workbench_video_get_config() { return siteText("videoConfig"); },
    get workbench_video_generate() { return siteText("videoGenerate"); },
    get prompts_search() { return siteText("promptSearch"); },
    get assets_list() { return siteText("assetList"); },
    get assets_add() { return siteText("assetAdd"); },
};

type SiteToolInput = Record<string, unknown>;
type SiteToolContext = { canvasSnapshot?: CanvasAgentSnapshot | null };

export async function runSiteTool(name: SiteToolName, input: SiteToolInput, navigate: NavigateFunction, context: SiteToolContext = {}): Promise<unknown> {
    switch (name) {
        case "canvas_list_projects":
            return listCanvasProjects(input);
        case "generation_get_status":
            return getGenerationStatus(input, context.canvasSnapshot);
        case "read_node_text":
            return readNodeTextTool(input, context.canvasSnapshot);
        case "extract_workflow_template":
            return await extractWorkflowTemplateTool(extractTemplateInput(input), workflowEntryDeps(context));
        case "run_workflow_batch":
            return await runWorkflowBatchTool(batchInput(input), workflowEntryDeps(context));
        case "run_workflow_instance":
            return await runWorkflowInstanceTool(instanceInput(input), workflowEntryDeps(context));
        case "workbench_image_get_config":
            return getImageConfig();
        case "workbench_image_generate":
            return runImageWorkbench(input, navigate);
        case "workbench_video_get_config":
            return getVideoConfig();
        case "workbench_video_generate":
            return runVideoWorkbench(input, navigate);
        case "prompts_search":
            return searchPrompts(input);
        case "assets_list":
            return listAssets(input);
        case "assets_add":
            return addAsset(input);
        default:
            throw new Error(siteText("unknownTool", { name }));
    }
}

function getGenerationStatus(input: SiteToolInput, canvasSnapshot?: CanvasAgentSnapshot | null) {
    return buildGenerationStatus(input, canvasSnapshot, useWorkbenchAgentStore.getState().tasks);
}

/** read_node_text：把节点正文/提示词逐字全文回给 Agent（批量执行与自修的唯一全文通道）。 */
function readNodeTextTool(input: SiteToolInput, canvasSnapshot?: CanvasAgentSnapshot | null) {
    const nodeId = typeof input.nodeId === "string" ? input.nodeId.trim() : "";
    if (!nodeId) throw new Error(siteText("nodeIdRequired"));
    const text = readNodeFullText({ nodeId, canvasSnapshot, projects: useCanvasStore.getState().projects });
    if (!text) throw new Error(siteText("nodeNotFound", { nodeId }));
    return text;
}

// ─────────────────────────────────────────────── P9 批量入口三工具（只做「取参 → 调引擎壳」）

function stringList(value: unknown): string[] {
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim()) : [];
}

function textField(value: unknown): string | undefined {
    return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function extractTemplateInput(input: SiteToolInput): ExtractTemplateInput {
    const sourceNodeIds = stringList(input.sourceNodeIds);
    const name = textField(input.name);
    const templateId = textField(input.templateId);
    return { ...(sourceNodeIds.length ? { sourceNodeIds } : {}), ...(name ? { name } : {}), ...(templateId ? { templateId } : {}) };
}

function materialInput(value: unknown): WorkflowBatchMaterialInput {
    const raw = value && typeof value === "object" ? (value as SiteToolInput) : {};
    const material: WorkflowBatchMaterialInput = {};
    const nodeId = textField(raw.sourceVideoNodeId);
    const storageKey = textField(raw.storageKey);
    const title = textField(raw.title);
    const intentText = textField(raw.intentText);
    const optionalSegments = textField(raw.optionalSegments);
    if (nodeId) material.sourceVideoNodeId = nodeId;
    if (storageKey) material.storageKey = storageKey;
    if (title) material.title = title;
    if (intentText) material.intentText = intentText;
    if (optionalSegments) material.optionalSegments = optionalSegments;
    return material;
}

function batchInput(input: SiteToolInput): RunWorkflowBatchInput {
    const materials = Array.isArray(input.materials) ? input.materials.map(materialInput) : [];
    const templateId = textField(input.templateId);
    const title = textField(input.title);
    const concurrency = Number(input.concurrency);
    return {
        // P10：素材缺省（含空数组）= 自动解析画布源视频，交引擎壳按「选中优先 → 画布全部源视频」处理。
        ...(materials.length ? { materials } : {}),
        ...(templateId ? { templateId } : {}),
        ...(title ? { title } : {}),
        ...(Number.isFinite(concurrency) && concurrency > 0 ? { concurrency } : {}),
    };
}

function instanceInput(input: SiteToolInput): RunWorkflowInstanceInput {
    const templateId = textField(input.templateId);
    const title = textField(input.title);
    return { material: materialInput(input.material), ...(templateId ? { templateId } : {}), ...(title ? { title } : {}) };
}

/**
 * 三工具的依赖：**全部**来自画布页注册的批次运行时（`registerWorkflowBatchRuntime`）。
 * 未注册（不是画布页 / 页面还没装载）→ 人话报错，绝不假跑。
 */
function workflowEntryDeps(context: SiteToolContext = {}): WorkflowEntryDeps {
    const runtime = getWorkflowBatchRuntime();
    if (!runtime) throw new Error(siteText("workflowRuntimeMissing"));
    return {
        project: () => runtime.project(),
        applyOps: (opList) => runtime.applyOps(opList),
        getBlob: (storageKey) => runtime.getBlob(storageKey),
        hub: runtime.hub,
        ...(runtime.lapianConfig ? { lapianConfig: runtime.lapianConfig } : {}),
        vetoes: () => runtime.vetoes(),
        identities: () => {
            const projectId = runtime.project()?.id;
            return useCanvasStore.getState().projects.find((item) => item.id === projectId)?.workflowInstances;
        },
        saveTemplate: (template) => {
            const projectId = runtime.project()?.id;
            if (!projectId) throw new Error(siteText("workflowRuntimeMissing"));
            useCanvasStore.getState().updateProject(projectId, { workflowTemplate: template });
        },
        saveBatches: (batches) => {
            const projectId = runtime.project()?.id;
            if (!projectId) throw new Error(siteText("workflowRuntimeMissing"));
            useCanvasStore.getState().updateProject(projectId, { workflowBatches: batches });
        },
        flush: (reason) => runtime.flush(reason),
        runBatch: (batchId) => startBatch(batchId),
        // 素材可来自源画布：按节点 id 跨项目查找（同一个浏览器里所有画布）。
        findNode: (nodeId) => {
            for (const project of useCanvasStore.getState().projects) {
                const found = project.nodes.find((node) => node.id === nodeId);
                if (found) return found;
            }
            return undefined;
        },
        // P10：素材缺省时「以选中为准」——选中态在画布快照里（useAgentBridge 随状态变化发布）。
        selectedNodeIds: () => context.canvasSnapshot?.selectedNodeIds || [],
        log: (message) => console.debug(`[canvas-agent] ${message}`),
    };
}

function listCanvasProjects(input: SiteToolInput) {
    const { projects, hydrated } = useCanvasStore.getState();
    if (!hydrated) throw new Error(siteText("canvasLoading"));
    const keyword = String(input.keyword || "").trim().toLowerCase();
    const filtered = keyword ? projects.filter((project) => project.title.toLowerCase().includes(keyword)) : projects;
    const { page, pageSize, start, end } = paginate(input, filtered.length, 20);
    const items = filtered.slice(start, end).map((project) => ({
        id: project.id,
        title: project.title,
        createdAt: project.createdAt,
        updatedAt: project.updatedAt,
        nodeCount: project.nodes.length,
        connectionCount: project.connections.length,
    }));
    return { total: filtered.length, page, pageSize, items, hint: siteText("canvasHint") };
}

function getImageConfig() {
    const { config } = useConfigStore.getState();
    const model = config.imageModel || config.model;
    return {
        current: { model, modelName: modelOptionName(model), quality: config.quality || "auto", size: config.size || "1:1", count: config.count || "1" },
        models: selectableModelsByCapability(config, "image").map((value) => ({ value, label: modelOptionLabel(config, value) })),
        qualityOptions: imageQualityOptions,
        scaleOptions: imageScaleOptions,
        sizeOptions: imageAspectOptions,
        countRange: { min: 1, max: 15 },
    };
}

function runImageWorkbench(input: SiteToolInput, navigate: NavigateFunction) {
    const configStore = useConfigStore.getState();
    const applied: Record<string, unknown> = {};
    if (typeof input.model === "string" && input.model.trim()) {
        const value = normalizeModelOptionValue(input.model, configStore.config.channels) || input.model;
        configStore.updateConfig("imageModel", value);
        applied.model = value;
    }
    if (typeof input.quality === "string" && input.quality.trim()) {
        configStore.updateConfig("quality", input.quality);
        applied.quality = input.quality;
    }
    if (typeof input.size === "string" && input.size.trim()) {
        configStore.updateConfig("size", input.size);
        applied.size = input.size;
    }
    if (input.count != null) {
        const count = String(Math.max(1, Math.min(15, Math.floor(Number(input.count)) || 1)));
        configStore.updateConfig("count", count);
        applied.count = count;
    }
    const prompt = typeof input.prompt === "string" ? input.prompt : undefined;
    const run = input.run !== false;
    navigate("/image");
    const taskId = useWorkbenchAgentStore.getState().dispatchImage({ prompt, run });
    return { ok: true, navigated: "/image", prompt, run, taskId, applied, note: siteText(run ? "imageGenerationStarted" : "imageConfigApplied") };
}

function getVideoConfig() {
    const { config } = useConfigStore.getState();
    const model = config.videoModel || config.model;
    return {
        current: {
            model,
            modelName: modelOptionName(model),
            size: config.size || "1280x720",
            seconds: config.videoSeconds || "6",
            resolution: config.vquality || "720",
            generateAudio: config.videoGenerateAudio !== "false",
            watermark: config.videoWatermark === "true",
            mode: config.videoMode === "reference" ? "reference" : "frames",
        },
        models: selectableModelsByCapability(config, "video").map((value) => ({ value, label: modelOptionLabel(config, value) })),
        sizeOptions: videoSizeOptions,
        secondsRange: videoSecondsRange,
        resolutionOptions: videoResolutionOptions,
        modeOptions: [
            { value: "frames", label: i18n.t("settingsPanels.video.modes.frames") },
            { value: "reference", label: i18n.t("settingsPanels.video.modes.reference") },
        ],
    };
}

function runVideoWorkbench(input: SiteToolInput, navigate: NavigateFunction) {
    const configStore = useConfigStore.getState();
    const applied: Record<string, unknown> = {};
    if (typeof input.model === "string" && input.model.trim()) {
        const value = normalizeModelOptionValue(input.model, configStore.config.channels) || input.model;
        configStore.updateConfig("videoModel", value);
        applied.model = value;
    }
    if (typeof input.size === "string" && input.size.trim()) {
        configStore.updateConfig("size", input.size);
        applied.size = input.size;
    }
    if (input.seconds != null && String(input.seconds).trim()) {
        const seconds = clampVideoSeconds(String(input.seconds));
        configStore.updateConfig("videoSeconds", seconds);
        applied.seconds = seconds;
    }
    if (typeof input.resolution === "string" && input.resolution.trim()) {
        configStore.updateConfig("vquality", input.resolution);
        applied.resolution = input.resolution;
    }
    if (typeof input.generateAudio === "boolean") {
        configStore.updateConfig("videoGenerateAudio", String(input.generateAudio));
        applied.generateAudio = input.generateAudio;
    }
    if (typeof input.watermark === "boolean") {
        configStore.updateConfig("videoWatermark", String(input.watermark));
        applied.watermark = input.watermark;
    }
    if (input.mode === "frames" || input.mode === "reference") {
        configStore.updateConfig("videoMode", input.mode);
        applied.mode = input.mode;
    }
    const prompt = typeof input.prompt === "string" ? input.prompt : undefined;
    const run = input.run !== false;
    navigate("/video");
    const taskId = useWorkbenchAgentStore.getState().dispatchVideo({ prompt, run });
    return { ok: true, navigated: "/video", prompt, run, taskId, applied, note: siteText(run ? "videoGenerationStarted" : "videoConfigApplied") };
}

async function searchPrompts(input: SiteToolInput) {
    const page = Math.max(1, Math.floor(Number(input.page)) || 1);
    const pageSize = Math.max(1, Math.min(50, Math.floor(Number(input.pageSize)) || 20));
    const tags = Array.isArray(input.tags) ? input.tags.filter((tag): tag is string => typeof tag === "string") : [];
    const result = await fetchPrompts({ keyword: String(input.keyword || ""), category: String(input.category || i18n.t("common.all")), tag: tags, page, pageSize });
    return {
        total: result.total,
        page,
        pageSize,
        categories: result.categories,
        tags: result.tags.slice(0, 60),
        items: result.items.map((prompt) => ({ id: prompt.id, title: prompt.title, prompt: prompt.prompt, category: prompt.category, tags: prompt.tags, coverUrl: prompt.coverUrl, githubUrl: prompt.githubUrl })),
    };
}

function listAssets(input: SiteToolInput) {
    const { assets, hydrated } = useAssetStore.getState();
    if (!hydrated) throw new Error(siteText("assetsLoading"));
    const kind = input.kind === "text" || input.kind === "image" || input.kind === "video" ? input.kind : "all";
    const keyword = String(input.keyword || "").trim().toLowerCase();
    const filtered = assets.filter((asset) => {
        if (kind !== "all" && asset.kind !== kind) return false;
        if (!keyword) return true;
        return [asset.title, asset.note, asset.source, ...asset.tags].filter(Boolean).join(" ").toLowerCase().includes(keyword);
    });
    const { page, pageSize, start, end } = paginate(input, filtered.length, 20);
    const items = filtered.slice(start, end).map((asset) => ({
        id: asset.id,
        kind: asset.kind,
        title: asset.title,
        tags: asset.tags,
        source: asset.source,
        note: asset.note,
        createdAt: asset.createdAt,
        updatedAt: asset.updatedAt,
        coverUrl: asset.coverUrl || undefined,
        content: asset.kind === "text" ? asset.data.content : undefined,
    }));
    return { total: filtered.length, page, pageSize, items };
}

async function addAsset(input: SiteToolInput) {
    const kind = input.kind;
    const title = String(input.title || "").trim();
    if (!title) throw new Error(siteText("assetTitleRequired"));
    const tags = Array.isArray(input.tags) ? input.tags.filter((tag): tag is string => typeof tag === "string") : [];
    const source = typeof input.source === "string" ? input.source : "Agent";
    const note = typeof input.note === "string" ? input.note : undefined;
    const store = useAssetStore.getState();
    if (kind === "text") {
        const content = String(input.content || "").trim();
        if (!content) throw new Error(siteText("textContentRequired"));
        const id = store.addAsset({ kind: "text", title, coverUrl: "", tags, source, note, data: { content } });
        return { ok: true, id, kind: "text" };
    }
    if (kind === "image") {
        const imageUrl = String(input.imageUrl || "").trim();
        if (!imageUrl) throw new Error(siteText("imageUrlRequired"));
        let stored;
        try {
            stored = await uploadImage(imageUrl);
        } catch {
            throw new Error(siteText("imageReadFailed"));
        }
        const id = store.addAsset({ kind: "image", title, coverUrl: stored.url, tags, source, note, data: { dataUrl: stored.url, storageKey: stored.storageKey, width: stored.width, height: stored.height, bytes: stored.bytes, mimeType: stored.mimeType } });
        return { ok: true, id, kind: "image" };
    }
    throw new Error(siteText("assetKindUnsupported"));
}

function paginate(input: SiteToolInput, total: number, defaultSize: number) {
    const pageSize = Math.max(1, Math.min(100, Math.floor(Number(input.pageSize)) || defaultSize));
    const maxPage = Math.max(1, Math.ceil(total / pageSize));
    const page = Math.min(maxPage, Math.max(1, Math.floor(Number(input.page)) || 1));
    const start = (page - 1) * pageSize;
    return { page, pageSize, start, end: start + pageSize };
}
