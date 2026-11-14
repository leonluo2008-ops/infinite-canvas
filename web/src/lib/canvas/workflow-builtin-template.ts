/**
 * 内置工作流模板（P12）：「视频拆解」= **系统预置资产**，任何画布开箱即用。
 *
 * 为什么有这个文件（P11 之后所有者点破的架构错误）：
 * 早期形态要求用户先在一条已跑通的「拆解→生成」链上 `extract_workflow_template` 抽出模板，新画布没有模板时
 * Agent 只能人话引导「先抽模板」——把系统本该内置的东西推给用户手工做一遍。正确形态：
 * 模板是系统资产，用户只管放素材 + 一句话；抽取降级为「想自定义工作流」时的高级能力。
 *
 * 设计约束（本卡红线）：
 * - **禁第二份骨架定义**：本文件只声明一份「标准拆解→生成链」的原型图（节点类型 + 连线 + 槽语义），
 *   模板声明（五槽 / 冻结项 / 图外引用报告）**由 P2 `extractWorkflowTemplate` 在同一份原型上算出来**，
 *   实例化仍走 P3/P4 引擎（`templatePlanInput` → `materializeWorkflowInstance` → `runWorkflowInstance`）。
 *   也就是说：内置模板 = P2 抽取函数对代码原型跑一次的产物，不是手写的第二套声明。
 * - 原型节点 id 一律 `bva-` 前缀（builtin video analysis），与任何画布节点 id 不会撞；
 *   实例落地时照 P3 确定性重映射成 `tbuiltin-video-analysis-<序号>-<旧id>`。
 * - 零素材字节依赖：原型里没有视频/图片字节（`extractWorkflowTemplate` 会按角色把运行态与字节剥干净），
 *   模板随代码分发，不需要从任何项目里抽。
 * - 指令正文**不复制**：指令节点正文取自 `lapian-prompt-templates.ts` 的默认模板（单一真源），
 *   默认模板将来改了，内置模板的下一次构建自动跟随（声明与节点在同一次构建里算，F2 永远自洽）。
 *
 * 纯函数层：不读 i18n、不摸 React、不碰 store（探针可 esbuild 打包真跑）。
 */

import { DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID, getLapianPromptTemplate } from "@/lib/canvas/lapian-prompt-templates";
import { extractWorkflowTemplate, TEMPLATE_INTENT_PLACEHOLDER, type CanvasWorkflowTemplate } from "@/lib/canvas/workflow-template";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

/** 内置模板 id：随代码分发的固定值（批次记录的 templateId 也用它）。 */
export const BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID = "builtin-video-analysis";
/** 内置模板名：用户口中的「视频拆解工作流」。 */
export const BUILTIN_VIDEO_ANALYSIS_TEMPLATE_NAME = "视频拆解";
/** 内置模板显示名（§2.6）：回执/侧栏/抽屉统一带「（内置）」后缀，与项目内同名自定义模板可区分。 */
export const BUILTIN_VIDEO_ANALYSIS_DISPLAY_NAME = `${BUILTIN_VIDEO_ANALYSIS_TEMPLATE_NAME}（内置）`;

/**
 * 原型图节点 id（`bva-` 前缀；彼此不构成后缀包含关系，F2 的 `endsWith(-id)` 定位不会串）。
 * 资产槽 id 也进模板（槽③ targets），语义与 P11 绑定口径一致：人物槽 / 产品槽。
 */
export const BUILTIN_VIDEO_ANALYSIS_NODE_IDS = {
    sourceVideo: "bva-src",
    lapian: "bva-lap",
    instruction: "bva-ins",
    intent: "bva-intent",
    promptConfig: "bva-cfg-prompt",
    finalText: "bva-final",
    videoConfig: "bva-cfg-video",
    personSlot: "bva-slot-person",
    productSlot: "bva-slot-product",
} as const;

const PROTOTYPE_PROJECT_ID = "builtin-video-analysis-source";
const PROTOTYPE_PROJECT_TITLE = "系统内置 · 视频拆解";
/** 固定创建时间：模板声明随代码分发，必须逐次运行一致（禁 `new Date()`，否则 digest 与审计漂移）。 */
const PROTOTYPE_CREATED_AT = "2026-11-14T00:00:00.000Z";

/** 标准链节点（左→右布局；实例链落地位置按这份原型位置，与 P10/P11 同口径）。 */
function prototypeNodes(): CanvasNodeData[] {
    const id = BUILTIN_VIDEO_ANALYSIS_NODE_IDS;
    const instruction = getLapianPromptTemplate(DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID).instruction;
    return [
        {
            id: id.sourceVideo,
            type: CanvasNodeType.Video,
            title: "源视频（每个视频一条实例）",
            position: { x: 0, y: 0 },
            width: 320,
            height: 200,
            metadata: { storageKey: "", content: "", mimeType: "video/mp4" },
        },
        {
            id: id.lapian,
            type: CanvasNodeType.Lapian,
            title: "拉片拆解",
            position: { x: 400, y: 0 },
            width: 420,
            height: 520,
            metadata: {},
        },
        {
            id: id.instruction,
            type: CanvasNodeType.Text,
            title: "转换指令",
            position: { x: 900, y: 0 },
            width: 320,
            height: 240,
            metadata: { content: instruction, prompt: instruction, promptTemplate: DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID },
        },
        {
            id: id.intent,
            type: CanvasNodeType.Text,
            title: "改造意图（可选）",
            position: { x: 900, y: 300 },
            width: 320,
            height: 200,
            metadata: { content: TEMPLATE_INTENT_PLACEHOLDER, prompt: TEMPLATE_INTENT_PLACEHOLDER, lapianIntent: true },
        },
        {
            id: id.promptConfig,
            type: CanvasNodeType.Config,
            title: "提示词组装（拉片 + 指令 + 意图）",
            position: { x: 1300, y: 0 },
            width: 360, 
            height: 240,
            metadata: {
                generationMode: "text",
                lapianSourceId: id.lapian,
                composerContent: `拉片：@[node:${id.lapian}]\n\n转换指令：@[node:${id.instruction}]\n\n改造意图：@[node:${id.intent}]`,
            },
        },
        {
            id: id.finalText,
            type: CanvasNodeType.Text,
            title: "提示词终稿（本次产物）",
            position: { x: 1720, y: 0 },
            width: 360,
            height: 240,
            metadata: { fontSize: 14 },
        },
        {
            id: id.videoConfig,
            type: CanvasNodeType.Config,
            title: "视频生成配置",
            position: { x: 2140, y: 0 },
            width: 360,
            height: 200,
            metadata: { generationMode: "video", composerContent: `提示词：@[node:${id.finalText}]` },
        },
        {
            // 槽③资产位（人物）：随模板分发，实例期按 P11 三级口径绑定画布人物图。
            id: id.personSlot,
            type: CanvasNodeType.Image,
            title: "资产位·人物",
            position: { x: 520, y: 620 },
            width: 200,
            height: 200,
            metadata: {
                assetSlotSourceId: id.lapian,
                assetSlotOrder: 0,
                assetSlotName: "人物",
                assetSlotType: "人物",
                assetSlot: { desc: "原片出镜的人物／角色／模特：换成画布上的人物图", span: "" },
            },
        },
        {
            // 槽③资产位（产品）：与人物槽同构，语义键 `产品|产品`。
            id: id.productSlot,
            type: CanvasNodeType.Image,
            title: "资产位·产品",
            position: { x: 780, y: 620 },
            width: 200,
            height: 200,
            metadata: {
                assetSlotSourceId: id.lapian,
                assetSlotOrder: 1,
                assetSlotName: "产品",
                assetSlotType: "产品",
                assetSlot: { desc: "原片主体产品／商品：换成画布上的产品图", span: "" },
            },
        },
    ];
}

/** 标准链连线（与 P4 编排消费的两条 token 链一一对应：组装串 token 必须有连线候选集）。 */
function prototypeConnections(): CanvasConnection[] {
    const id = BUILTIN_VIDEO_ANALYSIS_NODE_IDS;
    return [
        { id: "bva-e1", fromNodeId: id.sourceVideo, toNodeId: id.lapian },
        { id: "bva-e2", fromNodeId: id.lapian, toNodeId: id.promptConfig },
        { id: "bva-e3", fromNodeId: id.instruction, toNodeId: id.promptConfig },
        { id: "bva-e4", fromNodeId: id.intent, toNodeId: id.promptConfig },
        { id: "bva-e5", fromNodeId: id.promptConfig, toNodeId: id.finalText },
        { id: "bva-e6", fromNodeId: id.finalText, toNodeId: id.videoConfig },
    ];
}

type BuiltinBundle = {
    template: CanvasWorkflowTemplate;
    /** 已剥运行态的模板节点（P3 `planWorkflowInstance` 的 templateNodes）。 */
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
};

let cached: BuiltinBundle | null = null;

/**
 * 构建内置模板（**唯一一次**构建，之后按引用复用）：
 * 同一份原型 + 同一个 P2 抽取函数 → 模板声明与模板节点一次性产出，二者永远自洽（F1-F4 不会自相矛盾）。
 */
function buildBuiltinBundle(): BuiltinBundle {
    if (cached) return cached;
    const nodes = prototypeNodes();
    const connections = prototypeConnections();
    const extracted = extractWorkflowTemplate({
        sourceProject: { id: PROTOTYPE_PROJECT_ID, title: PROTOTYPE_PROJECT_TITLE, nodes, connections },
        selectedNodeIds: nodes.map((node) => node.id),
        templateId: BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID,
        name: BUILTIN_VIDEO_ANALYSIS_TEMPLATE_NAME,
        createdAt: PROTOTYPE_CREATED_AT,
    });
    cached = { template: extracted.template, nodes: extracted.project.nodes, connections: extracted.project.connections };
    return cached;
}

/** 内置模板声明（只读；调用方不得改它的槽/冻结项）。 */
export function builtinVideoAnalysisTemplate(): CanvasWorkflowTemplate {
    return buildBuiltinBundle().template;
}

/** 按 id 取内置模板；不是内置 id → undefined（调用方据此走项目模板路径）。 */
export function builtinWorkflowTemplate(templateId?: string): CanvasWorkflowTemplate | undefined {
    const wanted = String(templateId || "").trim();
    return wanted && wanted === BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID ? builtinVideoAnalysisTemplate() : undefined;
}

/** 内置模板的「已剥运行态」节点与连线（P3 计划入参；画布上不需要真的存在这些原型节点）。 */
export function builtinWorkflowTemplateSource(templateId?: string): { nodes: CanvasNodeData[]; connections: CanvasConnection[] } | undefined {
    const wanted = String(templateId || "").trim();
    if (!wanted || wanted !== BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID) return undefined;
    const bundle = buildBuiltinBundle();
    return {
        nodes: bundle.nodes.map((node) => ({ ...node, position: { ...node.position }, metadata: JSON.parse(JSON.stringify(node.metadata || {})) })),
        connections: bundle.connections.map((connection) => ({ ...connection })),
    };
}

/** 是否内置模板 id（缺省 = 空串 → false）。 */
export function isBuiltinTemplateId(templateId?: string): boolean {
    return String(templateId || "").trim() === BUILTIN_VIDEO_ANALYSIS_TEMPLATE_ID;
}

/** 内置模板的显示名（按 id；未知名 → undefined，调用方回落到项目模板名）。侧栏/抽屉/回执同源（§2.6）。 */
export function builtinTemplateNameOf(templateId?: string): string | undefined {
    return isBuiltinTemplateId(templateId) ? BUILTIN_VIDEO_ANALYSIS_DISPLAY_NAME : undefined;
}

/**
 * 显示用模板名（批次头 / 实例行 / 回执）：项目内模板优先，其次内置模板（带「（内置）」后缀），最后空串（调用方再回落 templateId）。
 * P12：内置批次的批次头要显示「视频拆解（内置）」，不能露出 `builtin-video-analysis` 这种内部 id，
 * 也不能与项目内同名「视频拆解」自定义模板混淆。
 */
export function workflowTemplateDisplayName(project: { workflowTemplate?: CanvasWorkflowTemplate } | undefined, templateId?: string): string {
    const own = project?.workflowTemplate;
    if (own?.templateId && (!templateId || String(own.templateId) === String(templateId))) {
        return String(own.name || own.templateId).trim();
    }
    return builtinTemplateNameOf(templateId) || "";
}

export type TemplateCompleteness = {
    ok: boolean;
    nodeCount: number;
    problems: string[];
};

/**
 * 模板完整性判定（P12 v2 §2.2.4 三项合并判据）：够不够格当一条完整的「视频拆解→生成」工作流。
 *
 * 判据只看**引擎真实消费的合同**（frozen 冻结项 + 槽①声明），不看节点数/五槽 kind/双模式
 * （节点数阈值拍脑袋误伤小模板；五槽 kind 由 `buildTemplateDeclaration` 无条件返回，恒真）：
 * 1. `frozen.instructions` 非空——实例化器 F2 校验逐条消费它，空 = 没有指令冻结；
 * 2. `frozen.topology.edgeCount > 0`——F1 比对基准，0 = 骨架没有连线（节点是一盘散沙）；
 * 3. 源视频槽 targets 非空——没有输入口，素材无处注入（有指令有连线但无源视频的模板照样跑不通）。
 * 报错按实际缺项报人话（缺什么说什么，不按节点数描述）。
 */
export function assessTemplateCompleteness(template: CanvasWorkflowTemplate | undefined): TemplateCompleteness {
    if (!template || !template.templateId) return { ok: false, nodeCount: 0, problems: ["画布上没有模板声明"] };
    const nodeCount = Array.isArray(template.frozen?.topology?.nodeIds) ? template.frozen.topology.nodeIds.length : 0;
    const problems: string[] = [];
    if (!Array.isArray(template.frozen?.instructions) || template.frozen.instructions.length === 0) {
        problems.push("缺指令冻结（模板里没有可复用的转换指令节点）");
    }
    if (!(Number(template.frozen?.topology?.edgeCount) > 0)) {
        problems.push("缺拓扑连线（骨架节点之间没有任何连线，实例链跑不起来）");
    }
    const sourceVideoTargets = (template.slots || []).find((slot) => slot.kind === "sourceVideo")?.targets?.length || 0;
    if (sourceVideoTargets === 0) {
        problems.push("缺源视频入口（模板里没有可换素材的视频节点，素材无处注入）");
    }
    return { ok: problems.length === 0, nodeCount, problems };
}
