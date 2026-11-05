export type Position = {
    x: number;
    y: number;
};

export type ViewportTransform = {
    x: number;
    y: number;
    k: number;
};

export enum CanvasNodeType {
    Image = "image",
    Text = "text",
    Config = "config",
    Video = "video",
    Audio = "audio",
    Group = "group",
    Lapian = "lapian",   // 拉片节点（wan-repro M3）：即梦结构拉片展示
}

// Node types are open strings: built-ins use CanvasNodeType and plugins use "<pluginId>:<name>".
export type CanvasNodeTypeId = CanvasNodeType | (string & {});

export type CanvasNodeStatus = "idle" | "success" | "loading" | "error";
export type CanvasGenerationMode = "text" | "image" | "video" | "audio";
export type CanvasImageGenerationType = "generation" | "edit";

export type CanvasNodeImage = {
    id: string;
    status: CanvasNodeStatus;
    errorDetails?: string;
    content: string;
    storageKey?: string;
    naturalWidth: number;
    naturalHeight: number;
    bytes: number;
    mimeType: string;
};

export type CanvasNodeText = {
    id: string;
    status: CanvasNodeStatus;
    errorDetails?: string;
    content: string;
};

export type CanvasNodeMetadata = {
    content?: string;
    composerContent?: string;
    prompt?: string;
    status?: CanvasNodeStatus;
    errorDetails?: string;
    fontSize?: number;
    taskId?: string;
    jobId?: string;
    intent?: string;
    lapPhase?: string;
    /** 转换指令节点用的模板 id（lib/canvas/lapian-prompt-templates.ts）；有值即说明这是拉片链的指令节点。 */
    promptTemplate?: string;
    /** 资产位节点（T3-H1）：来源拉片节点 id。 */
    assetSlotSourceId?: string;
    /** 资产位序（IR 资产序，0-based）：图号按此排序，稳定不变。 */
    assetSlotOrder?: number;
    /** 资产位名（IR 资产名）：短说明 fallback 第二级。 */
    assetSlotName?: string;
    /** 资产位类型（核心主体 / 人物/动物 / 块内子类名）。 */
    assetSlotType?: string;
    /** 资产位已挂图：画布图片节点 id（不靠连线，T3-H2 决策 4）。 */
    attachedImageId?: string;
    /** 资产位已挂图的短说明（2–6 字，清单口径）。 */
    attachedImageDesc?: string;
    /**
     * 资产位（T3-J2）：资产位节点是官方 Image 节点，图=原片截帧，title=资产位名。
     * 原片描述/时间区间/挂图 id/短说明没有别的地方放，全部收在这里（审计 §5 拍板）。
     */
    assetSlot?: {
        desc: string;
        /** 原片出现区间原文（`00:00-02.03、04.07-06.00`）；没有就空串。 */
        span: string;
        /** 截帧地址（bridge /api/asset-frame）；截帧失败时没有这个字段，节点降级成 Text。 */
        frameUrl?: string;
        /** 截帧时刻（秒）。 */
        frameAt?: number;
        /** 没解析出出现区间时按首帧截的，标出来不冒充精确。 */
        approxFrame?: boolean;
        /** 挂图 id 镜像（真源仍是扁平 attachedImageId）。 */
        attachedImageId?: string;
        /** 短说明镜像（真源仍是扁平 attachedImageDesc）。 */
        caption?: string;
    };
    /** 挂图对照节点（T3-J3）：资产位右侧那张「替换后」的图，源图节点 id。 */
    assetGraftOfImageId?: string;
    /** 挂图对照节点归属的资产位 id。 */
    assetGraftForSlotId?: string;
    /** 意图节点标记（T3-G3a）：不是终稿，不给【生成视频】。 */
    lapianIntent?: boolean;
    /** 拉片派生节点的来源拉片 id（T3-G1）：挂图/摘图时据此重算清单行。 */
    lapianSourceId?: string;
    /** 可选段（T6-B5 画布侧）：渲染提示词时要打开的可选段字母（如 "A,C"，逗号分隔）；空/缺省 = 全关（现状行为）。 */
    lapianOptional?: string;
    /** 拉片模型（M6-T7-R）：本节点实际用的模型引用，格式 `channelId::model`（全局清单同款编码）；空/缺省 = 跟随全局默认（仍未设 = 走服务端默认模型）。 */
    lapianModel?: string;
    generationMode?: CanvasGenerationMode;
    generationType?: CanvasImageGenerationType;
    model?: string;
    reasoningEffort?: "auto" | "low" | "medium" | "high" | "xhigh";
    size?: string;
    quality?: string;
    background?: string;
    count?: number;
    textCount?: number;
    texts?: CanvasNodeText[];
    primaryTextId?: string;
    seconds?: string;
    vquality?: string;
    generateAudio?: string;
    watermark?: string;
    videoMode?: string;
    audioVoice?: string;
    audioFormat?: string;
    audioSpeed?: string;
    audioInstructions?: string;
    references?: string[];
    naturalWidth?: number;
    naturalHeight?: number;
    freeResize?: boolean;
    images?: CanvasNodeImage[];
    primaryImageId?: string;
    storageKey?: string;
    mimeType?: string;
    bytes?: number;
    durationMs?: number;
    videoTaskId?: string;
    videoTaskProvider?: "openai" | "gemini";
    groupId?: string;
    interactive?: boolean; // Plugin node interaction/move state; see CanvasNodeDefinition.interactionToggle.
};

export type CanvasNodeData = {
    id: string;
    type: CanvasNodeTypeId;
    title: string;
    position: Position;
    width: number;
    height: number;
    metadata?: CanvasNodeMetadata;
};

export type CanvasConnection = {
    id: string;
    fromNodeId: string;
    toNodeId: string;
};

export type CanvasAssistantReference = {
    id: string;
    type: CanvasNodeTypeId;
    title: string;
    dataUrl?: string;
    storageKey?: string;
    text?: string;
};

export type CanvasAssistantImage = {
    id: string;
    dataUrl: string;
    storageKey?: string;
    prompt: string;
};

export type CanvasAssistantMessage = {
    id: string;
    role: "user" | "assistant" | "system" | "tool" | "error";
    title?: string;
    text: string;
    meta?: string;
    detail?: unknown;
    references?: CanvasAssistantReference[];
};

export type CanvasAssistantSession = {
    id: string;
    title: string;
    messages: CanvasAssistantMessage[];
    createdAt: string;
    updatedAt: string;
};

export type ConnectionHandle = {
    nodeId: string;
    handleType: "source" | "target";
};

export type SelectionBox = {
    startWorldX: number;
    startWorldY: number;
    currentWorldX: number;
    currentWorldY: number;
    additive: boolean;
    initialSelectedNodeIds: string[];
};

export type ContextMenuState =
    | {
          type: "node";
          x: number;
          y: number;
          nodeId: string;
      }
    | {
          type: "connection";
          x: number;
          y: number;
          connectionId: string;
      };
