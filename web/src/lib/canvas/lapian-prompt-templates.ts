/**
 * 拉片 → 视频生成提示词：转换指令的五套模板（T3-C）。
 *
 * 这里的正文不是「终稿提示词」，而是写进「转换指令」文本节点的任务书：
 * 生成期配置节点把拉片正文与指令正文一起贴进请求（canvas-node-generation.ts buildComposerGenerationContext），
 * 模型按本指令把十层 IR 改写成一条可提交的视频提示词。
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

export const DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID: LapianPromptTemplateId = "seedance-flow";

const REFERENCE_RULES = `# 参考图怎么用
- 用 @图片1 @图片2 … 指代参考图，编号就是「本次参考图」清单里的编号，从 1 连续排到 N；清单里没有的号不写。
- 每个实体跟一个 2–6 字的固定短说明，例如 @图片1 香水、@图片2 女性、@图片3 洗手台——让模型一眼认出这是谁、这是什么。
- 外形、颜色、材质、发型、服装只写一次，由参考图定住；后面一律用短说明指代，不逐镜重复描写细节。

# 写的时候
- 陈述句、现在时、动词驱动；只写看得见的东西，看不见的不猜。
- 一条动作一句话，句子连着写。
- 不用 JSON、不用表格、不回显字段名（不写「站位：」「运动形态：」）。
- 要约束就写成一句看得见的指令（「全程机位不动，只有瓶身在画面中央旋转」），不列禁止清单。

# 输出
只输出提示词正文。不解释、不加「以下是」、不写附注、不写参数位。
输入里的「# 附注」小节、模板依据说明、字段名回显都只当背景，不写进输出。`;

const TASK_PREAMBLE = (structureName: string) => `# 任务
你会收到这段拉片正文：目标片子的十层拆解（场景 / 产品 / 光影 / 镜头 / 运动 / 意境 / 分镜表 / 声音 / 资产）。
把它改写成一条可直接提交给视频生成模型的提示词：保留原片的创意与镜头语言，主体换成我们自己的产品与人物。
骨架按「${structureName}」写，段落顺序与标题不要改。`;

/** 意图节点预填正文（占位）：用户不改它时，指令必须当「没有意图」处理。 */
export const LAPIAN_INTENT_PLACEHOLDER = "（可选）写你的改造意图…";

/**
 * 意图段（T3-G3b）：意图文本节点是独立节点，正文以【文本N】贴进请求。
 * 用户意图优先于拉片；意图为空或只有占位提示时，行为必须与没有意图节点时一致。
 */
const INTENT_RULES = `# 用户意图
输入里会有一段「用户意图」（正文以【文本N】开头）：写的是这次要改成什么样——换角色、换产品、换图、改台词、改节奏。
- 有意图就按意图改写，意图优先级最高：拉片和意图冲突时以意图为准（拉片写黑裙、意图写换红裙，就写红裙）。
- 意图只交代了一部分，其余照拉片改写，不要把没提到的也一起改掉。
- 意图为空、或者只有「${LAPIAN_INTENT_PLACEHOLDER}」这句占位提示，就当从来没写过意图，完全照拉片改写。`;

/**
 * 图号防御段（T3-G4）：图号只能来自本次清单，清单外一律文字描述。
 * 这是可机器核对的硬事实（编造图号会让生成请求与正文对不上），所以允许用「只准 / 不写」表达。
 */
const IMAGE_NUMBER_RULES = `# 图号纪律
参考图在输入里以「本次参考图：图片1=香水、图片2=女性」这种清单给出：清单里有几个号，请求里就有几张图，号从 1 连续到 N。
- 只准用清单里出现过的图号：写到 @图片N 就回头对一遍清单，清单里没有这个号，就改成文字描述。
- 没有清单、或清单是空的，正文一个 @图片N 都不写：没挂图就用文字写主体长什么样、场景是什么样。
- 清单里没有对应图号的实体（场景、陈设、光线、道具）一律文字描述，不编号、不借用别人的号。
- 清单里有几个号就用到几号，不越过最后一位；清单外的号一个都不发明。`;

/**
 * 镜头语言段（B8 契约单一事实源）：与拆解侧共用 wanrepro/contracts/camera-contract.md。
 * ts 里不另抄词表——import 契约原文切片（§1 选词表 + §2 感受表述），改契约文件即两侧同时生效。
 * 这是正向选词指导（红线 15）：告诉模型从哪里选词，不列禁词。
 */
import cameraContractRaw from "../../../../wanrepro/contracts/camera-contract.md?raw";

function contractSection(markers: [string, string]): string {
    const start = cameraContractRaw.indexOf(markers[0]);
    const end = cameraContractRaw.indexOf(markers[1]);
    if (start < 0 || end < 0 || end <= start) {
        throw new Error("camera-contract.md 缺段落标记，无法切片：" + markers.join(" / "));
    }
    return cameraContractRaw.slice(start, end).trim();
}

const CAMERA_LANGUAGE_RULES = `# 镜头语言
镜头运动用词沿用拉片「镜头调度」行的标准镜头语言，不自创同义说法。拉片与这里用的是同一份契约（原文如下）：

${contractSection(["## §1 选词表", "## §3 口吻"])}

${contractSection(["## §2 镜头感受与灯光表述", "## §3 口吻"]).replace("## §2 镜头感受与灯光表述\n", "")}

提示词侧落点：感受与灯光词写进它修饰的那个镜头句。词对词写下来，覆盖度才对得上，模型也才认得出这一镜机器在怎么动。`;

/** 五套共用的收尾规则：参考图用法 + 镜头语言 + 用户意图 + 图号纪律。 */
const COMMON_RULES = `${REFERENCE_RULES}

${CAMERA_LANGUAGE_RULES}

${INTENT_RULES}

${IMAGE_NUMBER_RULES}`;

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

${COMMON_RULES}`,
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

${COMMON_RULES}`,
    },
    {
        id: "employee-checklist",
        labelKey: "canvas.lapianPrompt.templateEmployeeChecklist",
        instruction: `${TASK_PREAMBLE("甲方清单式")}

# 结构（七行，逐行照写，行首符号与全角竖线一起写）
✅ 片长：<秒数> 秒（<画幅>｜<宽>p）
✅ 人物：<年龄感><气质词><性别>，<发型>，身穿<服装>
✅ 产品：<产品>，严格保持形状、标签、尺寸比例、标签文字不变，全片稳定不变
✅ 镜头：<剪辑方式>，<运镜方式>，<机位稳定性>，<景别>
✅ 口播：<语种（地区变体）>，<收音方式>，口型对位，<有无 BGM>，<有无字幕>
✅ 分镜（N个镜头，0–10 秒）
  其下每行一个说话人一句话：[画外音] | 说话人：台词原文 —— 台词照原文语言逐字写，不译、不注音、不加动作提示。
✅ 首帧：<第 0 秒画面与主体状态>，<主体>始终在画面内

# 这一版的三个特征
- 声音不单独成段：全部塞进「口播」行与「分镜」行的台词里。
- 顺序先人物、后产品，不要调换。
- 产品行是全清单唯一的强命令式（两个「不变」：属性不变、全片时间上不变）。

${COMMON_RULES}`,
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

${COMMON_RULES}`,
    },
    {
        id: "seedance-flow",
        labelKey: "canvas.lapianPrompt.templateSeedanceFlow",
        instruction: `${TASK_PREAMBLE("单段流式")}

# 结构（整篇一段话：不换行、不加标题、不加附注）
1. 全局设定与主体指认：一句开场给画幅 + 影像质感 + 主体指认——每个人物、产品、场景各给一个图号与 2–6 字短说明，一句到底，外形细节不写。
2. 时段连续推进：按「N-M秒」一段一句推进，先说景别与场景，再说主体（@图片N 紧跟主体）与动作朝向，接镜头状态（每个时段句必带：运动方式+方向或速度；原片机位不动就写机位固定），最后是画面内容与转场；句与句直接相接成一段，中间不空行。
3. 全程一致性收尾：末尾一句收尾——运镜平滑流畅、转场自然、色调过渡连贯、人物形象与所引图一致、产品外观全程不变形。

${COMMON_RULES}`,
    },
];

export function getLapianPromptTemplate(id?: string | null): LapianPromptTemplate {
    return LAPIAN_PROMPT_TEMPLATES.find((item) => item.id === id) ?? LAPIAN_PROMPT_TEMPLATES.find((item) => item.id === DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID) ?? LAPIAN_PROMPT_TEMPLATES[0];
}
