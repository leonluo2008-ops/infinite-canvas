/**
 * 运镜覆盖度（B4a″ / B7b）——改写产物到底有没有把拉片里的运镜写进去。
 *
 * 口径（2026-11-07 定稿：标准镜头语言）：
 * ① 分母只认「镜头调度：」行（行首，允许 `- ` / `*` / `·` 项目符号与 `**` 加粗）。
 *    值里带〔缺字段〕/〔旧格式无运镜列〕的整行跳过——这类行的兜底文字常引用别的层
 *    （如「五层镜1 兜底：…微推…」），计入就会把「这层压根没识别出来」的片子算成有运镜。
 * ② 词表 = 标准镜头语言，与 wanrepro/prompts/视频拆解.md 两处选词指导同源：
 *    基础族 推 / 拉 / 摇 / 移 / 跟 / 升 / 降 / 环绕 / 甩镜 / 变焦 / 旋转 / 固定机位；
 *    复合与方向 推近 / 拉远 / 上摇 / 横移 / 跟拍 / 后退 / 升起 …；
 *    机位状态 手持 / 稳定器 / 仰角 / 平视 / 低机位 / 鸟瞰 …。
 *    同义写法归一成一个词（固定机位 ⇄ 机位固定 ⇄ 裸「固定」），其余复合词各自成词——
 *    拆解与改写用同一张表，词对词比对才不用猜。
 * ③ 兼容层：存量拉片是自然语言（「仰角后退并上摇」），旧词表里的 平移 仍单列成词；
 *    后退 / 前进 / 跟随 / 贴近 / 远离 / 抬升 这些旧复合词本就在标准表里，不需要另立。
 * ④ 误命中防线（只加在产物侧）：命中处前后 14 字内要有镜头主语——
 *    镜头 / 机位 / 画面 / 视角 / 运镜。产物句「瓶体在中央悬停旋转」「人物后退一步」不算。
 *    调度行整行本身就是镜头句子（行首自带「镜头调度：」），不做这层窗，否则长句里靠后的
 *    词会被误杀。
 * ⑤ 时间节奏与景别不进词表：升格是假朋友（单字「升」在「升格」里不算）；
 *    特写 / 近景 / 中景 / 全景 / 远景是景别，不进镜头运动。
 * ⑥ 命中判据是产物**全文**出现该词（全文词频，不逐镜对齐）：对齐要判语义，误报比粗粒度可见性更糟。
 * ⑦ total = 所有有值行的去重词总数；产物里找不到的词去重后进 missing。
 */

export type CameraCoverage = {
    covered: number;
    total: number;
    missing: string[];
};

/** 一个词族：认几种写法（surfaces），进分母 / missing 的只有一个词名（word）。 */
type CameraWord = {
    word: string;
    /** 拆解侧认的写法：分母只从这些写法里抽词。 */
    surfaces: readonly string[];
    /**
     * 产物侧额外认的等价写法。复合方向词与它的基础族互认（推近 ⇄ 推）——
     * 两个写法都在标准表里，产物写「缓推靠近」不该被判成没写运镜；
     * 这只放宽命中判定，不改分母（分母仍是拉片原词）。
     */
    accepts?: readonly string[];
};

/** 基础族 12 个。「固定机位」一族把同义写法都收进来：机位固定、裸「固定」。 */
const BASE_FAMILIES: readonly CameraWord[] = [
    { word: "推", surfaces: ["推"] },
    { word: "拉", surfaces: ["拉"] },
    { word: "摇", surfaces: ["摇"] },
    { word: "移", surfaces: ["移"] },
    { word: "跟", surfaces: ["跟"] },
    { word: "升", surfaces: ["升"] },
    { word: "降", surfaces: ["降"] },
    { word: "环绕", surfaces: ["环绕"] },
    { word: "甩镜", surfaces: ["甩镜"] },
    { word: "变焦", surfaces: ["变焦"] },
    { word: "旋转", surfaces: ["旋转"] },
    { word: "固定机位", surfaces: ["固定机位", "机位固定", "固定"] },
];

/** 复合与方向：基础族的「方向化说法」，归到哪个基础族就认哪个族名（accepts）。 */
const COMPOUND_BY_BASE: Record<string, readonly string[]> = {
    推: ["推近", "推进", "前推"],
    拉: ["拉远", "后拉"],
    摇: ["上摇", "下摇", "横摇", "左摇", "右摇"],
    移: ["横移", "左移", "右移"],
    跟: ["跟拍", "跟摇", "跟随"],
    升: ["升起", "上升"],
    降: ["下降"],
};

/** 复合词里没有单一基础族的几个：退 / 进 / 靠近 / 远离 / 下压 / 上抬，各自成词。 */
const STANDALONE_COMPOUND = ["贴近", "远离", "前进", "后退", "俯冲", "抬升"] as const;

/** 机位状态：回答「机器架在哪、什么角度」，同样属于镜头运动列。 */
const CAMERA_STATE_WORDS = [
    "手持", "稳定器",
    "仰拍", "仰角", "俯拍", "俯角", "平视",
    "低机位", "低角度", "高机位", "高角度", "鸟瞰",
] as const;

const withSelfSurface = (words: readonly string[]): readonly CameraWord[] =>
    words.map((word) => ({ word, surfaces: [word] }));

/**
 * 兼容层：存量拉片用旧词表（推 / 拉 / 摇 / 移 / 跟 / 环绕 / 平移 / 甩镜 / 变焦）写的自然语言。
 * 除「平移」外都已并入标准表；「平移」保留单列，避免和单字「移」重复计一次。
 */
const LEGACY_FAMILIES: readonly CameraWord[] = withSelfSurface(["平移"]);

const COMPOUND_FAMILIES: readonly CameraWord[] = [
    ...Object.entries(COMPOUND_BY_BASE).flatMap(([base, words]) =>
        words.map((word) => ({ word, surfaces: [word], accepts: [base] })),
    ),
    ...withSelfSurface(STANDALONE_COMPOUND),
];

const ALL_FAMILIES: readonly CameraWord[] = [
    ...BASE_FAMILIES,
    ...COMPOUND_FAMILIES,
    ...withSelfSurface(CAMERA_STATE_WORDS),
    ...LEGACY_FAMILIES,
];

const FAMILY_BY_WORD = new Map(ALL_FAMILIES.map((family) => [family.word, family]));

/**
 * 扫描顺序：长写法优先，命中后占位。
 * 「固定机位」不会再被「固定」加一次；「平移」不会再被「移」加一次；「推近」不会再被「推」加一次。
 */
const SCAN_SURFACES: readonly { surface: string; word: string }[] = ALL_FAMILIES.flatMap((family) =>
    family.surfaces.map((surface) => ({ surface, word: family.word })),
).sort((a, b) => b.surface.length - a.surface.length);

/**
 * 单字族的非运镜高频搭配：命中处落在这些词里就不算。
 * 移：移动 / 移步 / 移位是日常词；升：升格是时间节奏（慢动作），不是镜头运动。
 */
const SINGLE_CHAR_FALSE_FRIENDS: Record<string, readonly string[]> = {
    移: ["移动", "移步", "移位"],
    升: ["升格"],
};

/** 产物侧的镜头主语：命中处前后 14 字内得有一个，才认这是镜头在动。 */
const CAMERA_SUBJECTS = ["镜头", "机位", "画面", "视角", "运镜"] as const;
const SUBJECT_WINDOW = 14;

/** 「镜头调度：」行（含 `- 镜头调度：`）。整行不匹配 = 不是逐镜调度行。 */
const DISPATCH_LINE = /^\s*(?:[-*·]\s*)?(?:\*\*)?镜头调度(?:\*\*)?\s*[:：]\s*(.*)$/;

/** 这两类值说明本层没有真正识别出镜头运动，整行不进分母。 */
const NO_VALUE_MARKERS = ["〔缺字段〕", "〔旧格式无运镜列〕"];

type Span = { start: number; end: number };

const overlaps = (taken: Span[], start: number, end: number): boolean =>
    taken.some((span) => start < span.end && end > span.start);

/** 抽一段文字里的运镜词（按词名去重，按首次出现位置排序——长词先扫，但输出仍是文本顺序）。 */
function cameraWordsOfText(text: string): string[] {
    const words = new Map<string, number>();
    const taken: Span[] = [];

    // 假朋友先占位：「移动」里的「移」、「升格」里的「升」都不算。
    for (const friends of Object.values(SINGLE_CHAR_FALSE_FRIENDS)) {
        for (const friend of friends) {
            for (let at = text.indexOf(friend); at >= 0; at = text.indexOf(friend, at + friend.length)) {
                taken.push({ start: at, end: at + friend.length });
            }
        }
    }

    for (const { surface, word } of SCAN_SURFACES) {
        for (let at = text.indexOf(surface); at >= 0; at = text.indexOf(surface, at + surface.length)) {
            if (overlaps(taken, at, at + surface.length)) continue;
            taken.push({ start: at, end: at + surface.length });
            if (!words.has(word)) words.set(word, at);
        }
    }
    return [...words.entries()].sort((a, b) => a[1] - b[1]).map(([word]) => word);
}

/** 命中处前后 SUBJECT_WINDOW 字内有没有镜头主语（区间重叠算 0 距离）。 */
function hasCameraSubjectNear(text: string, start: number, end: number): boolean {
    for (const subject of CAMERA_SUBJECTS) {
        for (let at = text.indexOf(subject); at >= 0; at = text.indexOf(subject, at + 1)) {
            const gap = Math.max(0, Math.max(start, at) - Math.min(end, at + subject.length));
            if (gap <= SUBJECT_WINDOW) return true;
        }
    }
    return false;
}

/** 产物里认不认这个词族：任一写法（含等价写法）出现过，且那次出现挨着镜头主语。 */
function promptMentions(prompt: string, family: CameraWord): boolean {
    return [...family.surfaces, ...(family.accepts ?? [])].some((surface) => {
        for (let at = prompt.indexOf(surface); at >= 0; at = prompt.indexOf(surface, at + surface.length)) {
            if (hasCameraSubjectNear(prompt, at, at + surface.length)) return true;
        }
        return false;
    });
}

/**
 * 拉片逐镜「镜头调度」行的运镜词，在改写产物全文里的命中情况。
 * 无调度行 / 全部〔缺字段〕/ 有值但无运镜词 → total 0，missing 空（不算失败，只是没得比）。
 */
export function cameraCoverage(lapianText: string, promptText: string): CameraCoverage {
    const prompt = promptText || "";
    let covered = 0;
    let total = 0;
    const missing: string[] = [];
    const seenMissing = new Set<string>();

    for (const line of (lapianText || "").split(/\r?\n/)) {
        const matched = line.match(DISPATCH_LINE);
        if (!matched) continue;
        const value = matched[1].trim();
        if (!value) continue;
        if (NO_VALUE_MARKERS.some((marker) => value.includes(marker))) continue;
        for (const word of cameraWordsOfText(value)) {
            total += 1;
            const family = FAMILY_BY_WORD.get(word);
            if (family && promptMentions(prompt, family)) {
                covered += 1;
                continue;
            }
            if (seenMissing.has(word)) continue;
            seenMissing.add(word);
            missing.push(word);
        }
    }

    return { covered, total, missing };
}
