/**
 * 拉片 → 视频生成提示词：转换指令的五套模板（T3-C）。
 *
 * 这里的正文不是「终稿提示词」，而是写进「转换指令」文本节点的任务书：
 * 生成期配置节点把拉片正文与指令正文一起贴进请求（canvas-node-generation.ts buildComposerGenerationContext），
 * 模型按本指令把九层 IR 改写成一条可提交的视频提示词。
 *
 * 模板结构依据 wan-repro 仓 templates/*.md（lumi-4section / timeline-narrative /
 * employee-checklist / seven-section / seedance-flow）；本文件只存「怎么改写」的指令，
 * 不存渲染骨架、不读远端数据源（不复用 CanvasPromptLibrary——那是远端 GitHub 提示词库）。
 *
 * 指令正文遵守项目硬约束 17：参考图用 @图片N + 2–6 字固定短说明，禁逐镜重复描写细节。
 * 指令正文里的 `@图片N` 是写给模型的图号写法，不是画布 token——画布 token 只在配置节点组装串里出现。
 */

export type LapianPromptTemplateId = "lumi-4section" | "timeline-narrative" | "employee-checklist" | "seven-section" | "seedance-flow";

export type LapianPromptTemplate = {
    id: LapianPromptTemplateId;
    /** 显示名的 i18n key（zh-CN / en-US 均有）。 */
    labelKey: string;
    /** 指令正文：写进指令节点 metadata.content / metadata.prompt。 */
    instruction: string;
};

export const DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID: LapianPromptTemplateId = "lumi-4section";

const REFERENCE_RULES = `# 参考图怎么用
- 用 @图片1 @图片2 … 指代参考图，编号就是组装提示词里资产的出场序。
- 每个实体跟一个 2–6 字的固定短说明，例如 @图片1 香水、@图片2 女性、@图片3 洗手台——让模型一眼认出这是谁、这是什么。
- 外形、颜色、材质、发型、服装只写一次，由参考图定住；后面一律用短说明指代，不逐镜重复描写细节。

# 写的时候
- 陈述句、现在时、动词驱动；只写看得见的东西，看不见的不猜。
- 一条动作一句话，句子连着写，不写「很震撼」「极具冲击力」这类评价。
- 不用 JSON、不用表格、不回显字段名（不写「站位：」「运动形态：」）。
- 要约束就写成一句看得见的指令（「全程机位不动，只有瓶身在画面中央旋转」），不列禁止清单。

# 输出
只输出提示词正文。不解释、不加「以下是」、不写附注、不写参数位。
输入里的「# 附注」小节、模板依据说明、字段名回显都只当背景，不写进输出。`;

const TASK_PREAMBLE = (structureName: string) => `# 任务
你会收到这段拉片正文：目标片子的九层拆解（场景 / 产品 / 光影 / 镜头 / 运动 / 意境 / 分镜表 / 声音 / 资产）。
把它改写成一条可直接提交给视频生成模型的提示词：保留原片的创意与镜头语言，主体换成我们自己的产品与人物。
骨架按「${structureName}」写，段落顺序与标题不要改。`;

export const LAPIAN_PROMPT_TEMPLATES: LapianPromptTemplate[] = [
    {
        id: "lumi-4section",
        labelKey: "canvas.lapianPrompt.templateLumi4",
        instruction: `${TASK_PREAMBLE("Lumi 四段")}

# 结构（四段，逐段落位）
1. 风格（Style）——一个维度一句，20–60 字：
   质感（材质、工艺、写实程度）、光（光质、方向、对比）、色彩（主色、点缀色、冷暖）、氛围（情绪词 + 参考类型，如「商业广告」「时尚大片」）。
2. 摄影与镜头（Cinematography）：
   机位与运动（固定 / 手持 / 稳定器；俯拍 / 平视 / 低角度；推拉摇移）、焦段与景别（35mm / 85mm / 微距；特写 / 中景 / 全景）、布光（主光位置与软硬、轮廓光）、情绪递进。
3. 场景分镜（Scene Breakdown）：
   每个场景一行标题，带绝对时间码区间（00:00–00:03）；下面写可拍的连续动作链——谁、在哪、做什么、物体怎么变、镜头怎么接；转场写在场景开头并标时刻。
4. 声音与收束：
   对白有无、音乐风格与配器、音效点对点挂在动作上（哪个动作配什么声）、音乐怎么收。
   全片时长与画幅只在原片确实写明了才写，写成末尾一行。

${REFERENCE_RULES}`,
    },
    {
        id: "timeline-narrative",
        labelKey: "canvas.lapianPrompt.templateTimelineNarrative",
        instruction: `${TASK_PREAMBLE("时段叙事体")}

# 结构（四段，逐段落位）
1. 一句话总述与影调：
   一句说全片要让观众记住的那一件事，一句说暗与冷暖，一句说时间的松紧（哪一秒是重音、收尾减速还是定格），一句说情绪基调。
2. 时段叙事：
   一句一个时段，时间码写在句首（00:00–00:02）；一句之内依次说完景别、主体（@图片N 紧跟主体）、站位、朝向、视线、镜头运动、画面内容。
   句内只用「，」，不用分号与竖线；一句到底，不换行、不分条。
3. 全片连续性与首帧：
   一行给判定「一镜到底：是」或「一镜到底：否」；一行给第 0 秒的起始状态——画面里的人站在哪个方位、占多大、身体朝哪边、视线落在哪。
4. 声音与收尾：
   台词状态（有无、语种、逐句大意）、音乐骨架（风格、配器、第几秒有重音）、音效挂点（第几秒 + 什么声）。没有的写「无」，不编。

${REFERENCE_RULES}`,
    },
    {
        id: "employee-checklist",
        labelKey: "canvas.lapianPrompt.templateEmployeeChecklist",
        instruction: `${TASK_PREAMBLE("甲方清单式")}

# 结构（七行，逐行照写，行首符号与全角竖线一起写）
✅ 片长：<秒数> 秒（<画幅>｜<宽>p）
✅ 人物：<年龄感><气质词><性别>，<发型>，身穿<服装>
✅ 产品：<产品>，严格保持形状、标签、尺寸比例、标签文字不变，全片稳定不变
✅ 镜头：<剪辑方式>，<机位稳定性>，<景别>
✅ 口播：<语种（地区变体）>，<收音方式>，口型对位，<有无 BGM>，<有无字幕>
✅ 分镜（N个镜头，0–10 秒）
  其下每行一个说话人一句话：[画外音] | 说话人：台词原文 —— 台词照原文语言逐字写，不译、不注音、不加动作提示。
✅ 首帧：<第 0 秒画面与主体状态>，<主体>始终在画面内

# 这一版的三个特征
- 声音不单独成段：全部塞进「口播」行与「分镜」行的台词里。
- 顺序先人物、后产品，不要调换。
- 产品行是全清单唯一的强命令式（两个「不变」：属性不变、全片时间上不变）。

${REFERENCE_RULES}`,
    },
    {
        id: "seven-section",
        labelKey: "canvas.lapianPrompt.templateSevenSection",
        instruction: `${TASK_PREAMBLE("七段备选")}

# 结构（七个标题逐字照抄，不改字、不加副标题、不调顺序；某段没有素材就保留标题、正文写「无」）
0 总述：一行说清总时长、画幅、单镜头还是多镜头、风格一句话、情绪一句话。
一 产品参考：产品外观的唯一依据（@图片N），形状/标签/比例三项不可变，并写明「不参考该图的背景与人物」。
二 场景构图光线：环境与陈设（只写看得见的形状与位置）、机位与构图（高度、角度、景别、主体在画面的位置）、光线（光质、方向、色温、有无硬阴影）。
三 核心机制：这条片子唯一的那个机关——触发（什么动作启动）、变化过程（怎么变、沿什么轨迹）、落定状态、时间位置（第几秒到第几秒）。
四 逐秒分镜：每段一行，时间码（起–止）+ 画面动作 + 产品状态 + 镜头是否在动；时间码通篇一种格式。
五 声音：台词（有/无，有则语种 + 逐句原文 + 说话人）、音效（点对点挂动作）、BGM（有/无，风格与配器）、环境声（留什么、压什么）。
六 全程约束：只写不想要的——产品不变形、人不换脸、机位锁定、不加人加字、时长画幅不变；不重复前六段已写明的正向内容。

${REFERENCE_RULES}`,
    },
    {
        id: "seedance-flow",
        labelKey: "canvas.lapianPrompt.templateSeedanceFlow",
        instruction: `${TASK_PREAMBLE("单段流式")}

# 结构（整篇一段话：不换行、不加标题、不加附注）
1. 全局设定与主体指认：一句开场给画幅 + 影像质感 + 主体指认——每个人物、产品、场景各给一个图号与 2–6 字短说明，一句到底，外形细节不写。
2. 时段连续推进：按「N-M秒」一段一句推进，先说景别与场景，再说主体（@图片N 紧跟主体）与动作朝向，接镜头运动，最后是画面内容与转场；句与句直接相接成一段，中间不空行。
3. 全程一致性收尾：末尾一句收尾——运镜平滑流畅、转场自然、色调过渡连贯、人物形象与所引图一致、产品外观全程不变形。

${REFERENCE_RULES}`,
    },
];

export function getLapianPromptTemplate(id?: string | null): LapianPromptTemplate {
    return LAPIAN_PROMPT_TEMPLATES.find((item) => item.id === id) ?? LAPIAN_PROMPT_TEMPLATES[0];
}
