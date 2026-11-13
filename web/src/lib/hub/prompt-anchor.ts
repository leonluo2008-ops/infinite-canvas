/** 正文定位的**纯下标运算**（第 1 批落地，第 2 批保留并复用）。
 *
 *  为什么锚正文而不是锚资产置换区：资产置换区只在待置换态渲染，而质检红字面板在待审批/已交付态
 *  才是使用者真正看的地方——两个区域的生命周期不重叠。正文容器（`data-hub-prompt-body`）**所有
 *  状态恒渲染**，所以定位只能挂它。
 *
 *  第 2 批起：高亮改成**常驻 + 字符级**（见 `prompt-highlight.ts`），本模块只留「在一段文本里找命中
 *  区间」这一个纯函数——常驻高亮遇到 markdown 把句子切碎时，也靠它退到最长前缀。临时高亮
 *  （包 mark → 2 秒后摘掉）那条路径已随升级删除。
 */

/** 前缀回退的下限：短于此长度宁可判「没找到」，也不拿半个词去命中（会把高亮落到无关位置）。 */
const MIN_PREFIX = 8;

export type HitRange = { start: number; end: number };

/** 在一段文本里找命中区间：先整句精确命中，再退到最长可用前缀。
 *
 *  为什么要前缀回退：正文是整段 markdown，`上下文` 里的 `@图片1`、强调号在渲染后可能被拆进别的
 *  元素，整句 `indexOf` 落空，但句首一段仍在同一个文本节点里——退到前缀能定位到「这句话开头」，
 *  对「滚到这一句」这个目的足够。**不做归一化/不比对字面替换**：命中不到就如实说找不到。
 */
export function promptHitRange(haystack: string, needle: string): HitRange | null {
    const hay = String(haystack ?? "");
    const raw = String(needle ?? "").trim();
    if (!hay || raw.length < 2) return null;
    const at = hay.indexOf(raw);
    if (at >= 0) return { start: at, end: at + raw.length };
    const floor = Math.min(MIN_PREFIX, raw.length - 1);
    for (let len = raw.length - 1; len >= floor; len -= 1) {
        const hit = hay.indexOf(raw.slice(0, len));
        if (hit >= 0) return { start: hit, end: hit + len };
    }
    return null;
}


