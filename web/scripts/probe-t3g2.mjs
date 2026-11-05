/**
 * T3 二阶段免费探针（G3b/G4 指令段、G1 清单序、G2 四级 fallback / 幂等、H1 资产解析 / 空回退、H5 品牌槽）。
 *
 * 直接转译真模块（esbuild 打包 src/lib/canvas/*.ts，不走 app、不连 bridge、不花钱）：
 *   node scripts/probe-t3g2.mjs
 * 断言口径见 docs/nodes/M6-T3二阶段.md 与宿主仓 docs/nodes/M6-H衍生资产位专项.md；任何一条不成立就退出码 1。
 */
import { build } from "esbuild";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workdir = mkdtempSync(path.join(tmpdir(), "probe-t3g2-"));

async function loadModule(relativePath, name) {
    const outfile = path.join(workdir, `${name}.mjs`);
    await build({
        entryPoints: [path.join(root, relativePath)],
        outfile,
        bundle: true,
        format: "esm",
        platform: "node",
        logLevel: "silent",
        alias: { "@": path.join(root, "src") },
    });
    return import(pathToFileURL(outfile).href);
}

const assets = await loadModule("src/lib/canvas/lapian-assets.ts", "lapian-assets");
const templates = await loadModule("src/lib/canvas/lapian-prompt-templates.ts", "lapian-prompt-templates");

const LABELS = {
    slotPrefix: "资产位",
    original: "原片对应物",
    attachButton: "挂图",
    attachHint: "选中节点后点工具栏【挂图】选一张替换用的图；不挂图就沿用原片上这个资产。",
    unnamed: "未命名参考图",
    manifestPrefix: "本次参考图：",
};

let passed = 0;
const failures = [];
function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        return;
    }
    failures.push(`${name}${detail ? ` —— ${detail}` : ""}`);
}
function equal(name, actual, expected) {
    check(name, JSON.stringify(actual) === JSON.stringify(expected), `实际 ${JSON.stringify(actual)} / 期望 ${JSON.stringify(expected)}`);
}

// ---------------------------------------------------------------------------
// 1. G3b + G4：五套指令都带意图段与图号防御段
// ---------------------------------------------------------------------------
const list = templates.LAPIAN_PROMPT_TEMPLATES;
equal("五套模板齐全", list.map((item) => item.id), ["lumi-4section", "timeline-narrative", "employee-checklist", "seven-section", "seedance-flow"]);
for (const item of list) {
    const text = item.instruction;
    check(`[${item.id}] G4 图号纪律段`, text.includes("# 图号纪律"), "缺「# 图号纪律」段");
    check(`[${item.id}] G4 只准用清单图号`, /只准用清单里出现过/.test(text), "缺「只准用清单里出现过」");
    check(`[${item.id}] G4 无清单不写图号`, /没有清单|清单是空的/.test(text) && /一个 @图片N 都不写/.test(text), "缺无清单回退");
    check(`[${item.id}] G4 无图号实体文字描述`, /文字描述/.test(text), "缺「文字描述」");
    check(`[${item.id}] G4 不越界`, /不越过最后一位/.test(text), "缺不越界约束");
    check(`[${item.id}] G3b 意图段`, text.includes("# 用户意图"), "缺「# 用户意图」段");
    check(`[${item.id}] G3b 意图优先于拉片`, /以意图为准/.test(text), "缺「以意图为准」");
    check(`[${item.id}] G3b 占位当没写`, text.includes(templates.LAPIAN_INTENT_PLACEHOLDER), "缺占位提示口径");
    check(`[${item.id}] 参考图短说明规则仍在`, /2–6 字/.test(text), "缺 2–6 字短说明规则");
}

// ---------------------------------------------------------------------------
// 2. H1：IR 资产块解析（两种真实写法 + 空资产回退）
// ---------------------------------------------------------------------------
// ai-2 / demo1 实读形态：一块一行，行内嵌换行，`1. 名：描述`
const NUMBERED = [
    { title: "画幅载体", lines: ["16:9｜9s"], dna: true },
    {
        title: "人物/动物固有资产",
        lines: ["1. 几何凹坑渐变香水瓶：必须锁住黑色球形盖、瓶身多个凹圆结构、红黑渐变及白色文字。\n2. 浅金色缎面细吊带服饰：必须锁住面料反光特性与细吊带结构。\n3. 浅金色菱格纹链条翻盖包：必须锁住金属包链、菱格压纹与金属锁扣造型。\n4. 黑色镜面反光板：必须锁住高反射率与无尘黑色镜面。\n5. 暖黄线性及点状光源：必须锁住色温约3000K的轮廓光与散景光斑。"],
        dna: false,
    },
];
const numbered = assets.parseLapianAssetBlocks(NUMBERED);
equal("H1 数值条目数", numbered.length, 5);
equal("H1 类型取块名", numbered.map((item) => item.type), ["人物/动物", "人物/动物", "人物/动物", "人物/动物", "人物/动物"]);
equal("H1 资产名取冒号前", numbered.map((item) => item.name), ["几何凹坑渐变香水瓶", "浅金色缎面细吊带服饰", "浅金色菱格纹链条翻盖包", "黑色镜面反光板", "暖黄线性及点状光源"]);
equal("H1 资产序连续", numbered.map((item) => item.order), [0, 1, 2, 3, 4]);
equal("H1 描述去掉重复名", numbered[0].desc, "必须锁住黑色球形盖、瓶身多个凹圆结构、红黑渐变及白色文字。");

// e2e 实读形态：`- 子类标题：` + 明细条目
const BULLETED = [
    {
        title: "人物/动物固有资产",
        lines: [
            "- 底衬道具：\n- 粉色弧形软包边（必须锁住：边缘弧度、色彩饱和度）。\n- 灰蓝色粗纹理织物地垫（必须锁住：表面颗粒感、右侧硬光投影线位置）。\n- 遮挡构件：\n- 不明棕色圆角条形长臂（必须锁住：右下侧旋转铰点、宽度完全覆盖单瓶）。",
        ],
        dna: false,
    },
];
const bulleted = assets.parseLapianAssetBlocks(BULLETED);
equal("H1 子类标题不当资产", bulleted.length, 3);
equal("H1 子类标题作类型前缀", bulleted.map((item) => item.type), ["底衬道具", "底衬道具", "遮挡构件"]);
equal("H1 括号前截名", bulleted[0].name, "粉色弧形软包边");

// 单行无记号兜底：`产品：香水瓶` → 一条；分号堆叠的一句话（e2e 的 核心主体资产）→ 不硬拆
const SINGLE = [{ title: "核心主体资产", lines: ["产品：香水瓶"], dna: true }];
const single = assets.parseLapianAssetBlocks(SINGLE);
equal("H1 单行兜底成一条", single.map((item) => item.name), ["香水瓶"]);
equal("H1 单行兜底留原片描述", single[0]?.desc, "产品：香水瓶");
const STACKED = [{ title: "核心主体资产", lines: ["底衬道具；粉色弧形软包边（必须锁住；灰蓝色粗纹理织物地垫（必须锁住；遮挡构件；展示产品（图号绑定〔未取证〕——无已备图实体，未发 @图片N）"], dna: true }];
equal("H1 分号堆叠不硬拆", assets.parseLapianAssetBlocks(STACKED), []);

// 空资产回退（决策 5）：只有缺项三态 → 0 条
const EMPTY = [
    { title: "核心主体资产", lines: ["〔未取证〕（九 资产层为空且无待确认清单）"], dna: true },
    { title: "人物/动物固有资产", lines: ["〔未取证〕（九 资产层缺失或为空）"], dna: false },
];
equal("H1 空资产回退", assets.parseLapianAssetBlocks(EMPTY), []);
equal("H1 空 blocks 回退", assets.parseLapianAssetBlocks([]), []);

// 幂等：同名资产只留一条
const DUP = [{ title: "人物/动物固有资产", lines: ["1. 香水瓶：A。\n2. 香水瓶：B。\n3. 手：C。"], dna: false }];
const dup = assets.parseLapianAssetBlocks(DUP);
equal("H1 按名去重", dup.map((item) => item.name), ["香水瓶", "手"]);

// 资产位节点描述
const slots = assets.buildLapianAssetSlots(numbered, LABELS);
equal("H1 资产位标题", slots[0].title, "资产位·人物/动物·几何凹坑渐变香水瓶");
check("H1 资产位正文含原片描述", slots[0].body.includes("原片对应物：") && slots[0].body.includes("黑色球形盖"));
check("H1 资产位正文含挂图提示", slots[0].body.includes("挂图："));

// ---------------------------------------------------------------------------
// 2b. M6-H1 槽源归并：九 资产层一对一；占位摘要/分镜引用/品牌置换位不产槽；降级归并
// ---------------------------------------------------------------------------
// pop-1--2 实读形态（bridge 投影）：九层 5 条 + 「核心主体资产」分号占位行 → 槽只出九层 5 条
const POP12 = [
    {
        title: "核心主体资产",
        lines: ["人物（出现于：00:00-00:03）；服装A（出现于：00:00-00:03）；道具A（出现于：00:01-00:03）；道具B（出现于：全片）；场景A（出现于：全片）（图号绑定〔未取证〕——无已备图实体，未发 @图片N）"],
        dna: true,
    },
    {
        title: "人物/动物固有资产",
        lines: [
            "- 人物（出现于：00:00-00:03）：20-25岁欧美女性，浅金色复古大波浪及肩卷发，精致瓜子脸，纤细高挑体型。\n- 服装A（出现于：00:00-00:03）：黑底白点深V挂脖无袖上衣 + 米白色高腰A字超短裙。\n- 道具A（出现于：00:01-00:03）：手持贴于右耳通话的黑色扁平长方形智能设备。\n- 道具B（出现于：全片）：核心产品——便携圆柱形香水喷雾管。\n- 场景A（出现于：全片）：欧美都市户外街景十字路口。",
        ],
        dna: false,
    },
];
const pop12 = assets.parseLapianAssetBlocks(POP12);
equal("M6-H1 九层一对一 = 5 槽", pop12.length, 5);
equal("M6-H1 槽名 = 九层条目", pop12.map((item) => item.name), ["人物", "服装A", "道具A", "道具B", "场景A"]);
equal("M6-H1 槽序连续", pop12.map((item) => item.order), [0, 1, 2, 3, 4]);

// 分镜子块（「时段子块 ×N」）的逐镜实体引用不产槽
const STORYBOARD = [
    ...POP12,
    { title: "时段子块 ×N", lines: ["0-1秒｜原片镜1\n- 画面主体与表演：人物A双腿与手持道具B；低机位特写\n- 镜头调度：移+摇，特写\n3-4秒｜原片镜3\n- 画面主体与表演：人物B、服装B；道具C冲向镜头"], dna: false },
];
equal("M6-H1 分镜逐镜引用不产槽", assets.parseLapianAssetBlocks(STORYBOARD).length, 5);

// 「核心主体资产」@图片N 占位行不再单独产槽（九层在场时只认九层）
const WITH_SUMMARY = [
    { title: "核心主体资产", lines: ["@图片1 粉色细长款 PIVOINE"], dna: true },
    { title: "人物/动物固有资产", lines: ["- 人物：30 岁女性。\n- 道具A：香水瓶。"], dna: false },
];
equal("M6-H1 占位摘要不产槽", assets.parseLapianAssetBlocks(WITH_SUMMARY).map((item) => item.name), ["人物", "道具A"]);

// 资产块内的「品牌视觉（置换位）」占位行不产九资产槽（品牌槽只由十层聚合产生，见 2c）
const WITH_BRAND = [
    { title: "人物/动物固有资产", lines: ["- 人物：30 岁女性。\n- 品牌视觉（置换位）：客户品牌字样待填。\n- 道具A：香水瓶。"], dna: false },
];
equal("M6-H1 品牌占位行不冒九资产槽", assets.parseLapianAssetBlocks(WITH_BRAND).map((item) => item.name), ["人物", "道具A"]);

// 降级：九层缺失 → 旧逻辑（核心主体资产单行兜底仍在）
equal("M6-H1 九层缺失降级旧逻辑", assets.parseLapianAssetBlocks(SINGLE).map((item) => item.name), ["香水瓶"]);

// 降级归并：九层缺失（旧产物：九层块〔未取证〕，条目只在摘要块里）→ 旧逻辑 + 同尾缀「人物A+服装A」并回人物槽
const OLD_IR = [
    {
        title: "核心主体资产",
        lines: ["- 人物A（出现于：00:00-00:03）：20-25岁女性，金发。\n- 服装A（出现于：00:00-00:03）：黑底白波点上衣+A字裙。\n- 人物B（出现于：00:01-00:03）：亚洲男性。\n- 服装B（出现于：00:01-00:03）：深灰长袖衬衫。\n- 道具A（出现于：00:00-00:05）：玻璃喷雾管。\n- 场景A（出现于：全片）：城市街道。"],
        dna: true,
    },
    { title: "人物/动物固有资产", lines: ["〔未取证〕（九 资产层缺失或为空）"], dna: false },
];
const oldIr = assets.parseLapianAssetBlocks(OLD_IR);
equal("M6-H1 降级尾缀配对归并", oldIr.map((item) => item.name), ["人物A", "人物B", "道具A", "场景A"]);
check("M6-H1 归并后人物槽描述含服装", oldIr[0].desc.includes("黑底白波点") && oldIr[1].desc.includes("深灰长袖"));
equal("M6-H1 归并后槽序连续", oldIr.map((item) => item.order), [0, 1, 2, 3]);

// 降级归并：唯一人物槽时无尾缀服装也并（旧 IR 一人一片形态）
const SINGLE_PERSON = [
    {
        title: "核心主体资产",
        lines: ["- 人物-复古时尚女性（出现于：00:00-00:04）：金发女郎。\n- 服装-复古女郎装（出现于：00:00-00:04）：红色连衣裙。\n- 道具-粉色便携香水：圆柱喷雾管。\n- 场景-纽约街头十字路口：斑马线。"],
        dna: true,
    },
];
equal(
    "M6-H1 唯一人物槽并无尾缀服装",
    assets.parseLapianAssetBlocks(SINGLE_PERSON).map((item) => item.name),
    ["人物-复古时尚女性", "道具-粉色便携香水", "场景-纽约街头十字路口"],
);

// 多人物且服装无尾缀可配 → 不猜归属，保持独立槽
const AMBIGUOUS = [
    { title: "核心主体资产", lines: ["- 人物A：女性。\n- 人物B：男性。\n- 服装：礼服一套。"], dna: true },
];
equal("M6-H1 多人物不猜归属", assets.parseLapianAssetBlocks(AMBIGUOUS).map((item) => item.name), ["人物A", "人物B", "服装"]);

// ---------------------------------------------------------------------------
// 2c. M6-H5 品牌视觉槽：十层「图文与收束」有品牌露出 → 末尾聚合 1 槽；无 → 不出
// ---------------------------------------------------------------------------
// pop-1--3 实读形态（bridge /api/lapian/pop-1--3 投影原文照抄）：九层 5 条 + 「片尾收束」块
// 7 条品牌露出行（包装字A-E、品牌字、标语）+ 「结束交接状态」散文提及「文字标」（不是品牌条目）
const POP13_BRAND_LINE =
    "- 收束：类型=产品与品牌字同框；版式=粉色瓶体垂直居中悬浮，红色品牌字与标语悬于上方，虚化城市街景为底；动效时序=产品升格翻转并定格于画面中央，顶部品牌字及标语在00:05同步淡入定格。\n- 包装字A（出现于：00:03-00:06）：「POP!」｜粗无衬线大写英文字｜白色｜瓶身标签中央纵向｜无\n- 包装字B（出现于：00:03-00:06）：「28」｜圆圈内细无衬线数字｜白色｜瓶身标签左下部｜无\n- 包装字C（出现于：00:03-00:06）：「YES TO FLOWER」｜双行无衬线大写英文字｜白色｜瓶身标签右下部｜无\n- 包装字D（出现于：00:03-00:06）：「EAU DE PARFUM」｜细小无衬线大写英文字｜白色｜瓶身标签底端上方｜无\n- 包装字E（出现于：00:03-00:06）：「MADE IN FRANCE」｜细小无衬线大写英文字｜白色｜瓶身标签最底端｜无\n- 品牌字（出现于：00:05-00:06）：「POP!」｜超粗无衬线大字｜红色半透明｜画面顶部水平居中｜淡入\n- 标语（出现于：00:05-00:06）：「PERFUME OF PEOPLE」｜无衬线细体大写英文字｜红色半透明｜顶部大字正下方居中｜淡入";
const POP13 = [
    {
        title: "核心主体资产",
        lines: ["人物A（出现于：00:00-00:03）；服装A（出现于：00:00-00:03）；道具A（出现于：00:01-00:02）；道具B（出现于：00:00-00:06）；场景A（出现于：00:00-00:06）（图号绑定〔未取证〕——无已备图实体，未发 @图片N）"],
        dna: true,
    },
    { title: "片尾收束", lines: [POP13_BRAND_LINE], dna: true },
    {
        title: "人物/动物固有资产",
        lines: [
            "- 人物A（出现于：00:00-00:03）：20-25岁欧美女性，浅白肤色，金色微卷及肩发，身形纤细修长\n- 服装A（出现于：00:00-00:03）：黑底白波点深V挂脖背心 + 米白高腰A字短裙 + 白色短袜 + 红色漆皮一字带玛丽珍鞋\n- 道具A（出现于：00:01-00:02）：用于通话手持的深色扁平智能手机，只到手部\n- 道具B（出现于：00:00-00:06）：洋红色便携圆柱香水瓶，顶配银色金属盖，正面带排版标签\n- 场景A（出现于：00:00-00:06）：都市柏油马路十字路口斑马线，背景带黄色出租车与高层建筑",
        ],
        dna: false,
    },
    { title: "结束交接状态", lines: ["尾镜画面：瓶体在中央近距离悬停翻转展示，背景都市虚化，顶部出现文字标\n独立交接契约〔未取证〕（IR 无独立结束状态层，此为末镜画面内容，非生成端交接约束）"], dna: false },
];
const pop13 = assets.parseLapianAssetBlocks(POP13);
equal("M6-H5 pop-1--3 → 6 槽", pop13.length, 6);
equal("M6-H5 槽名（品牌槽挂末尾）", pop13.map((item) => item.name), ["人物A", "服装A", "道具A", "道具B", "场景A", "品牌视觉·置换位"]);
equal("M6-H5 槽序连续", pop13.map((item) => item.order), [0, 1, 2, 3, 4, 5]);
equal("M6-H5 品牌槽类型显示建议", pop13[5].type, "品牌");
equal("M6-H5 品牌槽唯一（7 条品牌行聚合 1 槽）", pop13.filter((item) => item.type === "品牌").length, 1);
check("M6-H5 品牌槽描述含全部包装字", pop13[5].desc.includes("包装字A") && pop13[5].desc.includes("包装字E"));
check("M6-H5 品牌槽描述含品牌字与标语", pop13[5].desc.includes("品牌字") && pop13[5].desc.includes("标语"));
check("M6-H5 品牌槽描述不含收束行", !pop13[5].desc.includes("收束：类型"));
const pop13Slots = assets.buildLapianAssetSlots(pop13, LABELS);
equal("M6-H5 品牌槽标题", pop13Slots[5].title, "资产位·品牌·品牌视觉·置换位");
equal("M6-H5 品牌槽出现区间取首条", pop13Slots[5].span, "00:03-00:06");
equal("M6-H5 品牌槽截帧按品牌条目名", pop13Slots[5].frameName, "包装字A");
check("M6-H5 普通槽无截帧查名", pop13Slots.slice(0, 5).every((slot) => !slot.frameName));

// 无品牌露出 → 不出品牌槽（三种真实形态）
// ① 旧格式：十层块缺失内容（demo1/ai-2/e2e/pop-4--2 实读：〔未取证〕占位行）
const NO_BRAND_OLD = [
    ...POP12,
    { title: "片尾收束", lines: ["〔未取证〕（十 图文与收束层缺失（旧格式拆解无这一层））"], dna: true },
    { title: "结束交接状态", lines: ["尾镜画面：香水稳稳落在潮湿沙滩前景，后景女人站立虚化，出现红色品牌字样\n独立交接契约〔未取证〕（IR 无独立结束状态层，此为末镜画面内容，非生成端交接约束）"], dna: false },
];
equal("M6-H5 旧格式十层占位行不出品牌槽", assets.parseLapianAssetBlocks(NO_BRAND_OLD).map((item) => item.name), ["人物", "服装A", "道具A", "道具B", "场景A"]);
// ② 十层块整体缺失（连占位块都没有）
equal("M6-H5 十层块缺失不出品牌槽", assets.parseLapianAssetBlocks(POP12).map((item) => item.name), ["人物", "服装A", "道具A", "道具B", "场景A"]);
// ③ 十层在场但全片无品牌露出（拆解层规定的无图文写法）
const NO_BRAND_NONE = [
    ...POP12,
    { title: "片尾收束", lines: ["- 图文：无（全片画面无持久性文字元素）\n- 收束：无品牌露出，画面直接结束"], dna: true },
];
equal("M6-H5 图文无+收束无品牌露出 → 不出品牌槽", assets.parseLapianAssetBlocks(NO_BRAND_NONE).length, 5);

// 降级路径（九层缺失）同样追加品牌槽：旧 IR 4 条归并槽 + 品牌 = 5
const OLD_IR_WITH_BRAND = [
    ...OLD_IR,
    { title: "片尾收束", lines: [POP13_BRAND_LINE], dna: true },
];
equal(
    "M6-H5 降级路径也追加品牌槽",
    assets.parseLapianAssetBlocks(OLD_IR_WITH_BRAND).map((item) => item.name),
    ["人物A", "人物B", "道具A", "场景A", "品牌视觉·置换位"],
);

// 资产块内的「品牌视觉（置换位）」占位行依旧不是九资产槽（品牌槽只来自十层聚合）
equal(
    "M6-H5 资产块品牌占位行不冒槽",
    assets.parseLapianAssetBlocks(WITH_BRAND).map((item) => item.name),
    ["人物", "道具A"],
);

// 品牌槽进清单：挂图后按资产位序参与连续编号（挂最后一位 → 图号最后一位）
const pop13SlotNodes = pop13Slots.map((item, index) => ({
    id: `pop13-slot-${index}`,
    type: "image",
    title: item.title,
    position: { x: 0, y: 0 },
    width: 340,
    height: 240,
    metadata: { assetSlotSourceId: "lapian-pop13", assetSlotOrder: item.order, assetSlotName: item.name, assetSlotType: item.type },
}));
const brandImageNode = { id: "img-brand", type: "image", title: "客户品牌图", position: { x: 0, y: 0 }, width: 340, height: 240, metadata: { content: "data:image/png;base64,x" } };
const onlyBrand = pop13SlotNodes.map((node, index) => (index === 5 ? { ...node, metadata: { ...node.metadata, attachedImageId: brandImageNode.id, attachedImageDesc: "品牌" } } : node));
const refsBrand = assets.collectLapianAssetReferences("lapian-pop13", [...onlyBrand, brandImageNode], LABELS.unnamed);
equal("M6-H5 品牌槽挂图 → 1 项", refsBrand.length, 1);
equal("M6-H5 品牌槽短说明取用户输入", refsBrand[0].caption, "品牌");
equal("M6-H5 品牌槽清单行", assets.buildLapianAssetManifest(refsBrand, LABELS.manifestPrefix), "本次参考图：@[node:img-brand]=品牌");

// ---------------------------------------------------------------------------
// 3. G2：短说明四级 fallback + 6 字截断 + 禁裸文件名
// ---------------------------------------------------------------------------
equal("G2 用户输入优先", assets.resolveAssetCaption({ userInput: "香水", slotName: "几何凹坑渐变香水瓶", imageTitle: "产品图", unnamed: "未命名参考图" }), "香水");
equal("G2 无输入落资产位名", assets.resolveAssetCaption({ userInput: "", slotName: "几何凹坑渐变香水瓶", imageTitle: "产品图", unnamed: "未命名参考图" }), "几何凹坑渐变");
equal("G2 无资产位名落图片名", assets.resolveAssetCaption({ slotName: "", imageTitle: "香水瓶正面图", unnamed: "未命名参考图" }), "香水瓶正面图");
equal("G2 全部缺失落未命名", assets.resolveAssetCaption({ userInput: "  ", slotName: "", imageTitle: "", unnamed: "未命名参考图" }), "未命名参考图");
equal("G2 裸文件名不进清单", assets.resolveAssetCaption({ slotName: "", imageTitle: "IMG_20240101_093012.jpg", unnamed: "未命名参考图" }), "未命名参考图");
equal("G2 截图名不进清单", assets.resolveAssetCaption({ slotName: "", imageTitle: "截图 2026-11-04 093012", unnamed: "未命名参考图" }), "未命名参考图");
equal("G2 中文名去扩展名", assets.resolveAssetCaption({ slotName: "", imageTitle: "香水.png", unnamed: "未命名参考图" }), "香水");
equal("G2 六字截断上限", Array.from(assets.truncateAssetCaption("一二三四五六七八")).length, 6);

// ---------------------------------------------------------------------------
// 4. G1 + H2：只挂第 3 位 → 清单 1 项、图号 1、token 1
// ---------------------------------------------------------------------------
const slotNodes = numbered.map((item, index) => ({
    id: `slot-${index}`,
    type: "text",
    title: slots[index].title,
    position: { x: 0, y: 0 },
    width: 340,
    height: 240,
    metadata: { assetSlotSourceId: "lapian-1", assetSlotOrder: item.order, assetSlotName: item.name, assetSlotType: item.type },
}));
const imageNode = { id: "img-3", type: "image", title: "产品替换图", position: { x: 0, y: 0 }, width: 340, height: 240, metadata: { content: "data:image/png;base64,x" } };
const thirdOnly = slotNodes.map((node, index) => (index === 2 ? { ...node, metadata: { ...node.metadata, attachedImageId: imageNode.id, attachedImageDesc: "香水" } } : node));
const refsThird = assets.collectLapianAssetReferences("lapian-1", [...thirdOnly, imageNode], LABELS.unnamed);
equal("G1 只挂第 3 位 → 1 项", refsThird.length, 1);
equal("G1 编号从 1 起", refsThird[0].index, 1);
equal("G1 短说明取用户输入", refsThird[0].caption, "香水");
const manifestThird = assets.buildLapianAssetManifest(refsThird, LABELS.manifestPrefix);
equal("G1 清单行只有一项", manifestThird, "本次参考图：@[node:img-3]=香水");
check("G1 清单不出现 @图片2", !manifestThird.includes("图片2"));

// 空清单：未挂任何图 → 清单行不存在
equal("G1 未挂图无清单行", assets.buildLapianAssetManifest(assets.collectLapianAssetReferences("lapian-1", [...slotNodes, imageNode], LABELS.unnamed), LABELS.manifestPrefix), "");

// 挂 5 位里的第 1、5 位 → 连续 1、2，与 images 数组一对一对齐
const twoAttached = slotNodes.map((node, index) =>
    index === 0 || index === 4 ? { ...node, metadata: { ...node.metadata, attachedImageId: `img-${index}`, attachedImageDesc: "" } } : node,
);
const images5 = [0, 4].map((index) => ({ id: `img-${index}`, type: "image", title: "", position: { x: 0, y: 0 }, width: 340, height: 240, metadata: { content: "data:image/png;base64,x" } }));
const refsTwo = assets.collectLapianAssetReferences("lapian-1", [...twoAttached, ...images5], LABELS.unnamed);
equal("G1 两位挂图 → 连续号", refsTwo.map((item) => item.index), [1, 2]);
equal("G1 图号顺序 = 资产位序", refsTwo.map((item) => item.imageId), ["img-0", "img-4"]);
equal("G1 短说明落资产位名", refsTwo.map((item) => item.caption), ["几何凹坑渐变", "暖黄线性及点"]);
const manifestTwo = assets.buildLapianAssetManifest(refsTwo, LABELS.manifestPrefix);
equal("G1 token 序 = images 序", [...manifestTwo.matchAll(/@\[node:([^\]]+)\]/g)].map((match) => match[1]), refsTwo.map((item) => item.imageId));
equal("G1 token 数 = images 数", manifestTwo.match(/@\[node:/g)?.length, refsTwo.length);

// 图片节点被删 → 幽灵项不出（决策 6）
const ghost = assets.collectLapianAssetReferences("lapian-1", [...twoAttached], LABELS.unnamed);
equal("G1 图被删不出幽灵项", ghost.length, 0);

// 组装串编号一致性：token 按出现序展开成「图片N」，N 必须等于清单里的 index
let imageCursor = 0;
const labelByNode = new Map();
const expanded = manifestTwo.replace(/@\[node:([^\]]+)\]/g, (_all, id) => {
    if (!labelByNode.has(id)) labelByNode.set(id, `图片${++imageCursor}`);
    return labelByNode.get(id);
});
equal("G1 展开后图号与清单序一致", expanded, "本次参考图：图片1=几何凹坑渐变、图片2=暖黄线性及点");
equal("G1 展开后 requests images 数 = 清单项数", imageCursor, refsTwo.length);

// ---------------------------------------------------------------------------
// 5. 幂等：清单 upsert 不叠行、组装串可重复刷
// ---------------------------------------------------------------------------
const composer = "拉片正文：@[node:lapian-1]\n任务说明：@[node:ins-1]\n用户意图：@[node:intent-1]";
const once = assets.upsertLapianAssetManifest(composer, manifestTwo, LABELS.manifestPrefix);
const twice = assets.upsertLapianAssetManifest(once, manifestTwo, LABELS.manifestPrefix);
equal("幂等 upsert 不叠行", twice, once);
equal("幂等 upsert 保留原组装串", once.startsWith(composer), true);
check("幂等 upsert 清单在末尾", once.endsWith(manifestTwo));
const cleared = assets.upsertLapianAssetManifest(once, "", LABELS.manifestPrefix);
equal("幂等 摘掉全部挂图 → 清单行删除", cleared.includes(LABELS.manifestPrefix), false);
equal("幂等 删除后回到原组装串", cleared, composer);

// ---------------------------------------------------------------------------
console.log(`\n通过 ${passed} 条断言${failures.length ? `，失败 ${failures.length} 条` : "，全部通过"}`);
if (failures.length) {
    for (const failure of failures) console.log(`  ✗ ${failure}`);
    process.exit(1);
}
console.log("OK T3 二阶段探针（G1/G2/G3b/G4/H1/H2）");
