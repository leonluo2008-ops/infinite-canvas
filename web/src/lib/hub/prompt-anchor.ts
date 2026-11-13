/** 正文定位（交互设计 §2.4 不可编辑态的「看正文里的这一句」）。
 *
 *  为什么锚正文而不是锚资产置换区：资产置换区只在待置换态渲染，而质检红字面板在待审批/已交付态
 *  才是使用者真正看的地方——两个区域的生命周期不重叠。正文容器（`data-hub-prompt-body`）**所有
 *  状态恒渲染**，所以定位只能挂它。
 *
 *  本批是**简版**（设计 §5 第 2 批才做常驻/字符级高亮）：命中即滚进视野 + 加临时高亮 class，
 *  2 秒后原样摘掉——不常驻、不改正文渲染路径、不动提示词数据。
 *
 *  两层职责分开：
 *   · `promptHitRange()` 纯下标运算（可离线断言）；
 *   · `flashPromptHit()` 只做「找到文本节点 → 包 mark → 滚动 → 定时摘掉」的 DOM 动作。
 */

export const PROMPT_HIT_MS = 2000;
export const PROMPT_HIT_ATTR = "data-hub-prompt-hit";

/** 前缀回退的下限：短于此长度宁可判「没找到」，也不拿半个词去命中（会把高亮落到无关位置）。 */
const MIN_PREFIX = 8;

const HIT_CLASS = "rounded bg-amber-100 px-0.5 text-stone-800 underline decoration-red-500 decoration-2 underline-offset-2 dark:bg-amber-900/50 dark:text-amber-100";

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

type TextHit = { node: Text; start: number; end: number };

function findTextHit(root: HTMLElement, needle: string): TextHit | null {
    const doc = root.ownerDocument;
    if (!doc) return null;
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentElement;
        const tag = parent ? parent.tagName : "";
        if (tag === "SCRIPT" || tag === "STYLE") continue;
        const text = node.nodeValue || "";
        if (!text) continue;
        const range = promptHitRange(text, needle);
        if (range) return { node: node as Text, start: range.start, end: range.end };
    }
    return null;
}

/** 临时高亮摘除：把 mark 展开回纯文本（mark 已被 React 重渲染换掉时静默跳过）。 */
export function unwrapPromptHit(mark: Element | null): void {
    if (!mark || !mark.isConnected) return;
    const parent = mark.parentNode;
    if (!parent) return;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    parent.normalize();
}

/** 在正文容器里定位并临时高亮：候选串按序试（整句 → 词），第一个命中即止。
 *  返回 true = 真的滚过去且高亮了；false = 这段正文里没有这一句（调用方决定怎么如实说）。 */
export function flashPromptHit(
    root: HTMLElement | null,
    candidates: readonly string[],
    windowMs: number = PROMPT_HIT_MS,
): boolean {
    if (!root || typeof document === "undefined") return false;
    const list = (candidates ?? [])
        .map((text) => String(text ?? "").trim())
        .filter((text) => text.length >= 2);
    for (const needle of list) {
        const hit = findTextHit(root, needle);
        if (!hit) continue;
        const range = document.createRange();
        try {
            range.setStart(hit.node, hit.start);
            range.setEnd(hit.node, hit.end);
        } catch {
            continue;
        }
        const mark = document.createElement("mark");
        mark.setAttribute(PROMPT_HIT_ATTR, "1");
        mark.className = HIT_CLASS;
        try {
            range.surroundContents(mark);   // 区间落在单个文本节点内，不会跨元素
        } catch {
            continue;
        }
        mark.scrollIntoView({ block: "center" });
        if (typeof window !== "undefined") window.setTimeout(() => unwrapPromptHit(mark), windowMs);
        return true;
    }
    return false;
}
