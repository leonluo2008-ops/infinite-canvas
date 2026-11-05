/**
 * 运镜覆盖度（B4a″）——改写产物到底有没有把拉片里的运镜写进去。
 *
 * 口径（审计定稿，2026-11-06）：
 * ① 分母只认「镜头调度：」行（行首，允许 `- ` / `*` / `·` 项目符号与 `**` 加粗）。
 *    值里带〔缺字段〕/〔旧格式无运镜列〕的整行跳过——这类行的兜底文字常引用别的层
 *    （如「五层镜1 兜底：…微推…」），计入就会把「这层压根没识别出来」的片子算成有运镜。
 * ② 词族：单字 推 / 拉 / 摇 / 移 / 跟，整词 环绕 / 平移 / 甩镜 / 变焦 / 固定机位 / 机位固定。
 *    一行可能命中多个，取去重后的词集合（同一种运镜在一行里写几次只算一个）。
 *    注意「固定」单字**不算**：拉片里「固定，特写」这类值很常见，算进去会把景别描述虚记成运镜；
 *    只认整词「固定机位 / 机位固定」。
 * ③ 命中判据是产物**全文**出现该词（全文词频，不逐镜对齐）：对齐要判语义，误报比粗粒度可见性更糟。
 * ④ total = 所有有值行的去重词总数；产物里找不到的词去重后进 missing。
 */

export type CameraCoverage = {
    covered: number;
    total: number;
    missing: string[];
};

/** 整词族：先于单字扫描，命中后占位，避免「平移」再被单字「移」重复计一次。 */
const MULTI_WORD_FAMILIES = ["环绕", "平移", "甩镜", "变焦", "固定机位", "机位固定"] as const;

/** 单字族：中文字之间没有词边界，误命中靠下方的 false friend 表逐个排除。 */
const SINGLE_WORD_FAMILIES = ["推", "拉", "摇", "移", "跟"] as const;

/**
 * 单字族的非运镜高频搭配：命中处落在这些词里就不算（「移动/移步/移位」是日常词，不是运镜）。
 * 表按字存放，新增排除项只改这里，不动扫描逻辑。
 */
const SINGLE_CHAR_FALSE_FRIENDS: Record<string, readonly string[]> = {
    移: ["移动", "移步", "移位"],
};

/** 「镜头调度：」行（含 `- 镜头调度：`）。整行不匹配 = 不是逐镜调度行。 */
const DISPATCH_LINE = /^\s*(?:[-*·]\s*)?(?:\*\*)?镜头调度(?:\*\*)?\s*[:：]\s*(.*)$/;

/** 这两类值说明本层没有真正识别出镜头运动，整行不进分母。 */
const NO_VALUE_MARKERS = ["〔缺字段〕", "〔旧格式无运镜列〕"];

type Span = { start: number; end: number };

/** 抽一行调度值里的运镜词（去重，保留首次出现顺序）。 */
function cameraWordsOfValue(value: string): string[] {
    const words = new Set<string>();
    const taken: Span[] = [];
    const take = (start: number, length: number) => taken.push({ start, end: start + length });

    for (const family of MULTI_WORD_FAMILIES) {
        for (let at = value.indexOf(family); at >= 0; at = value.indexOf(family, at + family.length)) {
            words.add(family);
            take(at, family.length);
        }
    }
    for (const friends of Object.values(SINGLE_CHAR_FALSE_FRIENDS)) {
        for (const friend of friends) {
            for (let at = value.indexOf(friend); at >= 0; at = value.indexOf(friend, at + friend.length)) take(at, friend.length);
        }
    }
    for (const family of SINGLE_WORD_FAMILIES) {
        for (let at = value.indexOf(family); at >= 0; at = value.indexOf(family, at + family.length)) {
            // 已落在整词族或排除词（「移动」里的「移」）占的位置上 → 不算。
            if (taken.some((span) => at >= span.start && at < span.end)) continue;
            words.add(family);
        }
    }
    return [...words];
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
        const words = cameraWordsOfValue(value);
        total += words.length;
        for (const word of words) {
            if (prompt.includes(word)) {
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
