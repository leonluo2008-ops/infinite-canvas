import { z } from "zod";

const recordSchema = z.record(z.unknown());
const positionSchema = z.object({ x: z.number(), y: z.number() });
const viewportSchema = z.object({ x: z.number(), y: z.number(), k: z.number() });
const nodeTypeSchema = z.enum(["image", "text", "config", "video", "audio"]);
const generationModeSchema = z.enum(["text", "image", "video", "audio"]);

/** Canvas Agent 对外提供的工具名称。 */
export const toolNames = [
    "site_navigate",
    "canvas_list_projects",
    "canvas_get_state",
    "canvas_get_selection",
    "canvas_export_snapshot",
    "canvas_apply_ops",
    "canvas_create_node",
    "canvas_create_attachment_nodes",
    "canvas_create_text_node",
    "canvas_create_text_nodes",
    "canvas_create_config_node",
    "canvas_create_image_prompt_flow",
    "canvas_create_generation_flow",
    "canvas_generate_text",
    "canvas_generate_image",
    "canvas_generate_video",
    "canvas_generate_audio",
    "canvas_update_node",
    "canvas_update_node_text",
    "canvas_move_nodes",
    "canvas_resize_node",
    "canvas_delete_nodes",
    "canvas_connect_nodes",
    "canvas_select_nodes",
    "canvas_set_viewport",
    "canvas_run_generation",
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
export type ToolName = (typeof toolNames)[number];

export const canvasOpSchema = z.discriminatedUnion("type", [
    z.object({ type: z.literal("add_node"), nodeType: nodeTypeSchema.optional(), id: z.string().optional(), title: z.string().optional(), x: z.number().optional(), y: z.number().optional(), width: z.number().optional(), height: z.number().optional(), position: positionSchema.optional(), metadata: recordSchema.optional() }).passthrough(),
    z.object({ type: z.literal("update_node"), id: z.string(), patch: recordSchema.optional(), metadata: recordSchema.optional() }).passthrough(),
    z.object({ type: z.literal("delete_node"), id: z.string().optional(), ids: z.array(z.string()).optional() }).passthrough(),
    z.object({ type: z.literal("delete_connections"), id: z.string().optional(), ids: z.array(z.string()).optional(), all: z.boolean().optional() }).passthrough(),
    z.object({ type: z.literal("connect_nodes"), id: z.string().optional(), fromNodeId: z.string(), toNodeId: z.string() }).passthrough(),
    z.object({ type: z.literal("set_viewport"), viewport: viewportSchema }).passthrough(),
    z.object({ type: z.literal("select_nodes"), ids: z.array(z.string()) }).passthrough(),
    z.object({ type: z.literal("run_generation"), nodeId: z.string(), mode: generationModeSchema.optional(), prompt: z.string().optional() }).passthrough(),
]);

const textNodeSchema = z.object({
    text: z.string(),
    title: z.string().optional(),
    x: z.number().optional(),
    y: z.number().optional(),
    width: z.number().optional(),
    height: z.number().optional(),
});

const generationOptionsSchema = z.object({
    model: z.string().optional(),
    size: z.string().optional(),
    quality: z.string().optional(),
    count: z.number().optional(),
    seconds: z.string().optional(),
    vquality: z.string().optional(),
    generateAudio: z.string().optional(),
    watermark: z.string().optional(),
    videoMode: z.string().optional(),
    audioVoice: z.string().optional(),
    audioFormat: z.string().optional(),
    audioSpeed: z.string().optional(),
    audioInstructions: z.string().optional(),
});

const generationFlowSchema = z.object({
    prompt: z.string(),
    title: z.string().optional(),
    x: z.number().optional(),
    y: z.number().optional(),
    referenceNodeIds: z.array(z.string()).optional(),
});

/** 批量素材（槽①源视频必填其一：画布节点 id 或媒体键）。 */
const workflowMaterialSchema = z.object({
    sourceVideoNodeId: z.string().optional(),
    storageKey: z.string().optional(),
    title: z.string().optional(),
    intentText: z.string().optional(),
    optionalSegments: z.string().optional(),
});

/** P11：画布替换图指代（用户话语 → 槽 + 图）。slotNodeId 最精确；否则按槽名/槽类型认槽。 */
const workflowAssetBindingSchema = z.object({
    slotNodeId: z.string().optional(),
    slotName: z.string().optional(),
    slotType: z.string().optional(),
    imageNodeId: z.string(),
    reason: z.string().optional(),
});

export const toolInputSchemas = {
    site_navigate: z.object({ path: z.string() }),
    canvas_list_projects: z.object({ keyword: z.string().optional(), page: z.number().optional(), pageSize: z.number().optional() }),
    canvas_get_state: z.object({}).passthrough(),
    canvas_get_selection: z.object({}).passthrough(),
    canvas_export_snapshot: z.object({}).passthrough(),
    canvas_apply_ops: z.object({ ops: z.array(canvasOpSchema) }),
    canvas_create_node: z.object({ nodeType: nodeTypeSchema, title: z.string().optional(), x: z.number().optional(), y: z.number().optional(), width: z.number().optional(), height: z.number().optional(), metadata: recordSchema.optional() }),
    canvas_create_attachment_nodes: z.object({ attachmentIds: z.array(z.string()).min(1), x: z.number().optional(), y: z.number().optional(), gap: z.number().optional(), direction: z.enum(["row", "column"]).optional() }),
    canvas_create_text_node: z.object({ text: z.string().optional(), x: z.number().optional(), y: z.number().optional(), title: z.string().optional(), width: z.number().optional(), height: z.number().optional() }),
    canvas_create_text_nodes: z.object({ items: z.array(textNodeSchema).min(1), x: z.number().optional(), y: z.number().optional(), gap: z.number().optional(), direction: z.enum(["row", "column"]).optional() }),
    canvas_create_config_node: z.object({ prompt: z.string().optional(), mode: generationModeSchema.optional(), title: z.string().optional(), x: z.number().optional(), y: z.number().optional(), width: z.number().optional(), height: z.number().optional(), autoRun: z.boolean().optional() }).merge(generationOptionsSchema),
    canvas_create_image_prompt_flow: z.object({ prompt: z.string(), x: z.number().optional(), y: z.number().optional(), autoRun: z.boolean().optional() }).merge(generationOptionsSchema),
    canvas_create_generation_flow: generationFlowSchema.extend({ mode: generationModeSchema.optional(), autoRun: z.boolean().optional() }).merge(generationOptionsSchema),
    canvas_generate_text: generationFlowSchema.merge(generationOptionsSchema),
    canvas_generate_image: generationFlowSchema.merge(generationOptionsSchema),
    canvas_generate_video: generationFlowSchema.merge(generationOptionsSchema),
    canvas_generate_audio: generationFlowSchema.merge(generationOptionsSchema),
    canvas_update_node: z.object({ id: z.string(), patch: recordSchema.optional(), metadata: recordSchema.optional() }),
    canvas_update_node_text: z.object({ id: z.string(), text: z.string(), title: z.string().optional() }),
    canvas_move_nodes: z.object({ items: z.array(z.object({ id: z.string(), x: z.number().optional(), y: z.number().optional(), dx: z.number().optional(), dy: z.number().optional() })).min(1) }),
    canvas_resize_node: z.object({ id: z.string(), width: z.number(), height: z.number(), freeResize: z.boolean().optional() }),
    canvas_delete_nodes: z.object({ ids: z.array(z.string()).min(1) }),
    canvas_connect_nodes: z.object({ connections: z.array(z.object({ fromNodeId: z.string(), toNodeId: z.string() })).min(1) }),
    canvas_select_nodes: z.object({ ids: z.array(z.string()) }),
    canvas_set_viewport: z.object({ viewport: viewportSchema }),
    canvas_run_generation: z.object({ nodeId: z.string(), mode: generationModeSchema.optional(), prompt: z.string().optional() }),
    generation_get_status: z.object({ scope: z.enum(["all", "canvas", "image", "video"]).optional(), taskId: z.string().optional(), nodeIds: z.array(z.string()).optional(), limit: z.number().optional() }),
    read_node_text: z.object({ nodeId: z.string() }),
    extract_workflow_template: z.object({ sourceNodeIds: z.array(z.string()).optional(), name: z.string().optional(), templateId: z.string().optional() }),
    run_workflow_batch: z.object({ templateId: z.string().optional(), materials: z.array(workflowMaterialSchema).optional(), assetBindings: z.array(workflowAssetBindingSchema).optional(), concurrency: z.number().optional(), title: z.string().optional() }),
    run_workflow_instance: z.object({ templateId: z.string().optional(), material: workflowMaterialSchema, assetBindings: z.array(workflowAssetBindingSchema).optional(), title: z.string().optional() }),
    workbench_image_get_config: z.object({}).passthrough(),
    workbench_image_generate: z.object({ prompt: z.string(), model: z.string().optional(), quality: z.string().optional(), size: z.string().optional(), count: z.number().optional(), run: z.boolean().optional() }),
    workbench_video_get_config: z.object({}).passthrough(),
    workbench_video_generate: z.object({ prompt: z.string(), model: z.string().optional(), size: z.string().optional(), seconds: z.string().optional(), resolution: z.string().optional(), generateAudio: z.boolean().optional(), watermark: z.boolean().optional(), mode: z.enum(["frames", "reference"]).optional(), run: z.boolean().optional() }),
    prompts_search: z.object({ keyword: z.string().optional(), category: z.string().optional(), tags: z.array(z.string()).optional(), page: z.number().optional(), pageSize: z.number().optional() }),
    assets_list: z.object({ kind: z.enum(["all", "text", "image", "video"]).optional(), keyword: z.string().optional(), page: z.number().optional(), pageSize: z.number().optional() }),
    assets_add: z.object({ kind: z.enum(["text", "image"]), title: z.string(), content: z.string().optional(), imageUrl: z.string().optional(), tags: z.array(z.string()).optional(), source: z.string().optional(), note: z.string().optional() }),
} satisfies Record<ToolName, z.AnyZodObject>;

export const toolDescriptions: Record<ToolName, string> = {
    site_navigate: "跳转网站页面。path 可为 / (首页)、/canvas (我的画布)、/canvas/:id (指定画布)、/image (生图工作台)、/video (视频创作台)、/prompts (提示词库)、/assets (我的素材)、/config (配置)。操作画布前若不在画布页，先用本工具打开画布。",
    canvas_list_projects: "列出用户全部画布（仅标题、创建/更新时间、节点数、连线数，不含完整数据），支持 keyword 搜索和 page/pageSize 分页。返回的 id 可配合 site_navigate 跳转到 /canvas/:id 打开对应画布。",
    canvas_get_state: "读取当前网页画布的节点、连线、选区和视口。",
    canvas_get_selection: "读取当前网页画布选中的节点。",
    canvas_export_snapshot: "导出当前画布快照，用于理解布局。",
    canvas_apply_ops: "批量操作当前网页画布。ops 支持 add_node、update_node、delete_node、delete_connections、connect_nodes、set_viewport、select_nodes、run_generation。",
    canvas_create_node: "创建任意类型节点：text、image、config、video、audio。适合创建占位图、媒体占位、配置节点或自定义 metadata 节点。",
    canvas_create_attachment_nodes: "把当前对话中用户上传的图片附件创建成真实画布图片节点。attachmentIds 使用本轮附件清单中的 ID；返回的节点 ID 可传给 canvas_create_generation_flow.referenceNodeIds 作为生成参考图。",
    canvas_create_text_node: "在当前画布创建单个文本节点。",
    canvas_create_text_nodes: "批量创建文本节点，适合生成标题、段落、脚本、说明等内容块。",
    canvas_create_config_node: "创建生成配置节点，可指定 text/image/video/audio 模式和生成参数，可选择立即触发生成。",
    canvas_create_image_prompt_flow: "创建提示词文本节点和图片生成配置节点，并自动连线，可选择立即触发生图。",
    canvas_create_generation_flow: "创建通用生成流程：提示词文本节点、生成配置节点、参考节点连线，可用于文案、生图、视频或音频。",
    canvas_generate_text: "创建通用文本生成流程并立即触发生成。",
    canvas_generate_image: "创建通用图片生成流程并立即触发生成。",
    canvas_generate_video: "创建通用视频生成流程并立即触发生成。",
    canvas_generate_audio: "创建通用音频生成流程并立即触发生成。",
    canvas_update_node: "更新节点基础字段或 metadata。",
    canvas_update_node_text: "更新文本节点内容和标题。",
    canvas_move_nodes: "移动一个或多个节点，支持绝对坐标或 dx/dy 偏移。",
    canvas_resize_node: "调整节点尺寸。",
    canvas_delete_nodes: "删除指定节点及相关连线。",
    canvas_connect_nodes: "批量连接节点。",
    canvas_select_nodes: "设置当前选中节点。",
    canvas_set_viewport: "调整画布视口。",
    canvas_run_generation: "触发指定节点生成，通常用于配置节点或文本/图片/视频/音频节点。",
    generation_get_status: "查询当前活动网页的生成任务状态。默认返回画布、生图工作台和视频工作台最近任务；可用 scope 过滤来源，用 taskId 查询工作台任务，用 nodeIds 查询画布节点。返回的 prompt 只是最多 4000 字的预览，promptTruncated=true 时用 read_node_text 取全文。",
    read_node_text: "读取指定画布节点的完整文本，入参 nodeId，返回 nodeId、projectId、type、title 与 content/prompt/composerContent 全文及各自字数（length 为主文本字数）。正文与提示词逐字返回、不截断不折叠，是批量执行和自修时取完整提示词的唯一通道。图片/视频/音频节点的 content 若是内联 dataURL，只返回 contentLength 与 contentOmitted=media-data-url，不回传原始数据。",
    extract_workflow_template: "从当前画布项目抽取可复用工作流模板（模板声明落在本项目 workflowTemplate 上，随画布保存）。sourceNodeIds 缺省 = 全项目可抽象子图（源视频/拉片/指令/意图/文本/配置等骨架节点）；要连生成产物一起抽就显式列出节点 id。**name 是模板的具名**：抽的时候按用户日后的说法起名（比如用户要「用视频拆解工作流处理这些视频」，就起名「视频拆解」）——这个名字会出现在建批次回执里，也是 Agent 认「用户说的是哪个工作流」的依据；同一个名+同一批节点重抽是原地覆盖，不会多出第二份模板。返回 templateId、模板名、五槽声明（源视频/意图/资产位/生成参数/可选段）、冻结项摘要（拓扑与骨架 digest）、逐节点剥离字段与图外引用报告：图外引用非空时模板不冻结，实例化会被自足性校验拦下，需把这些节点一起选进来重抽。本工具零付费、不生成任何内容，不碰待审批队列。抽完用 run_workflow_batch 建批次（不传 templateId/materials 即自动取用本模板与画布源视频）；实例的终点是「待审批」，付费视频生成不在本工具内发生，审批与提交出片请在画布「待审批」侧栏操作。",
    run_workflow_batch: "用户说「用视频拆解工作流处理这些视频 / 处理这 N 个视频 / 把这几个视频跑一遍」时：**直接调本工具，templateId 与 materials 都不用传**——templateId 缺省自动取本画布唯一的工作流模板（回执里报模板名，先跟用户对一句用的是哪个模板）；materials 缺省自动把画布上的源视频节点（拖进来的素材，生成产物不算、模板骨架里的示范源视频也不算）解析出来，并在回执的 materials 里逐条列出 id+标题供用户核对；画布上有选中节点时以选中为准（只选中想处理的那几个即可）。**画布上的产品图/人物图会被自动识别为替换素材并绑定到对应槽位（回执逐条给「图→槽」绑定理由；显式指定的优先于自动匹配，判不准的槽留空交人）**：用户说了「人物换成这张/产品用这张」时，把指代翻成 assetBindings（slotNodeId 或 slotName/slotType + imageNodeId，可带 reason 原话）传进来，指定优先；没说时选中替换图也会优先于未选中的图。用户明确点名节点、或要改意图文案（intentText）/可选段时才显式给 materials（每项 sourceVideoNodeId 或 storageKey，可带 title/intentText/optionalSegments）。画布还没有模板时本工具会人话报错：这时不要自己猜参数，直接告诉用户「先在已跑通的『视频拆解→生成』链上抽一次模板并起名（比如『视频拆解』）」，抽完这句「处理这些视频」就能直达。执行：每素材 materializeWorkflowInstance（幂等落库，同实例不重复拉片）→ 入批次 → runBatchQueue 并发编排（默认并发 3，上限 6）。返回 batchId、模板名、本次素材清单、每实例 instanceId 与节点骨架、派生状态表、队列相位。**终点=待审批，付费视频生成不在本工具内发生**，审批与提交出片请在画布「待审批」侧栏操作。同一批素材重复调用落在同一批次（幂等，不翻倍）。",
    run_workflow_instance: "单素材单实例（调试用）：只给一个 material，走与 run_workflow_batch 完全相同的落库+编排路径（并发 1），返回该实例的 instanceId、节点骨架、槽③挂图与派生状态；画布上的产品图/人物图会被自动识别为替换素材并绑定到对应槽位（回执给绑定理由；显式指定的优先于自动匹配，判不准的槽留空交人；要指定就传 assetBindings）；终点=待审批，付费视频生成不在本工具内发生，审批与提交出片请在画布「待审批」侧栏操作。同素材重复调用幂等（同实例不重建、不重复拉片）。",
    workbench_image_get_config: "读取生图工作台的当前参数和可选项（可用模型、质量、分辨率 1k/2k/4k/auto、宽高比、张数范围），在调用 workbench_image_generate 前先了解可选值。",
    workbench_image_generate: "在生图工作台填入提示词并按需设置 model、quality、size（如 16:9 或 2048x1152）、count，run 默认 true 会自动点击生成按钮。会自动跳转到生图工作台。生成为异步过程，提交后返回 taskId，可用 generation_get_status 查询状态。",
    workbench_video_get_config: "读取视频创作台的当前参数和可选项（可用模型、清晰度 480/720/1080、比例 1:1/3:4/4:3/16:9/9:16/21:9/auto、时长 4–30 秒、首尾帧或全能参考模式、是否生成声音与水印）。",
    workbench_video_generate: "在视频创作台填入提示词并按需设置 model、size（1:1、3:4、4:3、16:9、9:16、21:9、auto 或 1280x720）、seconds（4–30）、resolution（480/720/1080）、generateAudio、watermark、mode（frames 首尾帧 / reference 全能参考），run 默认 true 会自动点击生成按钮。会自动跳转到视频创作台。生成为异步过程，提交后返回 taskId，可用 generation_get_status 查询状态。",
    prompts_search: "搜索提示词库（第三方提示词合集），支持 keyword、category、tags 过滤和 page/pageSize 分页，返回标题、提示词、分类、标签、封面等。",
    assets_list: "列出用户「我的素材」，支持 kind（text/image/video）过滤、keyword 搜索和 page/pageSize 分页。为控制体积不返回图片/视频原始 data，仅返回封面与元信息。",
    assets_add: "向「我的素材」新增素材。kind=text 时用 content 传文本内容；kind=image 时用 imageUrl 传图片地址或 dataURL。可附带 title、tags、source、note。",
};
