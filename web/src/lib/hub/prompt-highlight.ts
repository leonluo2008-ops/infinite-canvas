/** 正文内**字符级常驻高亮**（质检面板就地处理闭环 §2.5 验收 3 的升级版 / 第 2 批）。
 *
 *  与第 1 批「临时高亮 2 秒」的区别：命中段是**常驻**的——正文一渲染出来就带上高亮，不自动消失，
 *  也不靠点击才出现；「看正文里的这一句」退化成「滚到已经存在的那个高亮段」。
 *
 *  为什么渲染后再包 mark，而不是按字符切 markdown 源：
 *   · 正文是整段 markdown（`<Streamdown>{prompt}</Streamdown>`）。按 `上下文` 的字符下标去切源串，
 *     会把段落/强调/代码块切成互不相连的碎片（每片各自成段），markdown 结构与空白全变——违反验收 3；
 *   · 所以保持 Streamdown 的输出**原样**，只在它渲染出的**真实文本节点**上做字符级包裹（`<mark>`），
 *     `片段` 给的结构化 `起/止` 直接当区间用，重叠/嵌套取并集、不重复渲染。
 *
 *  生命周期红线：mark 是包在 React 自己渲染出来的文本节点上的，所以**必须在 React 改动这棵子树之前
 *  展开回去**。做法是让渲染件在 `useLayoutEffect` 里包、在它的清理函数里拆——React 在 mutation 阶段
 *  先跑 layout effect 的 destroy 再做 DOM 改动，因此重渲染（正文变了）与卸载都不会踩到
 *  「The node to be removed is not a child of this node」。
 *
 *  纯下标运算（`buildPromptHighlightPlan` / `promptHighlightSpans`）与 DOM 动作（`applyPromptHighlights`
 *  / `unwrapPromptHighlights` / `scrollToPromptHighlight`）分开，前者可离线断言（probe-at23-b2.mjs）。
 */
import { promptHitRange } from "./prompt-anchor";

/** 常驻高亮的锚点属性（沿用第 1 批的 `data-hub-prompt-hit`：真机探针与外部检查都认这一个键）。 */
export const PROMPT_HL_ATTR = "data-hub-prompt-hit";
/** 取值＝persistent：与第 1 批的临时高亮（"1"）区分开，探针一眼能看出这是哪一批的产物。 */
export const PROMPT_HL_STATE = "persistent";

/** 命中段配色：amber 底色 + amber 下划线（红字报警用红色，两者一眼可分）；深色模式单独一档。 */
export const PROMPT_HL_CLASS = "rounded bg-amber-200/70 px-0.5 text-stone-900 underline decoration-amber-500 decoration-2 underline-offset-2 dark:bg-amber-800/60 dark:text-amber-50 dark:decoration-amber-400";

/** finding 里本模块真正消费的三个槽位（结构化字段，不做文字解析）。 */
type FindingLike = {
    上下文?: string;
    词?: string;
    片段?: Array<{ 起?: number; 止?: number; 高亮?: boolean }>;
};
type ItemLike = { 说明?: { 发现?: FindingLike[] } };

export type PromptHighlightPiece = { 起: number; 止: number };
export type PromptHighlightItem = {
    /** 命中句原文（prompt_md 原样子串，生产者产出）。 */
    上下文: string;
    /** 命中词（只在 `上下文` 整句都找不到时兜底：旧数据没有 v2 上下文）。 */
    词: string[];
    /** 同一句里真正命中的区间（相对 `上下文` 的下标）。 */
    片段: PromptHighlightPiece[];
};
/** 文本节点内的命中区间（绝对下标）。 */
export type PromptHighlightSpan = { 起: number; 止: number };

/** 从自检束里收集高亮计划：每个 finding 一条（`上下文` 优先，`词` 兜底），同 `上下文+词` 去重。
 *  只读结构化槽位，不拼话术、不改判定——这里只决定「哪几段字该上色」。 */
export function buildPromptHighlightPlan(items: readonly ItemLike[] | null | undefined): PromptHighlightItem[] {
    const plan: PromptHighlightItem[] = [];
    const seen = new Set<string>();
    for (const item of items ?? []) {
        for (const finding of item?.说明?.发现 ?? []) {
            const context = String(finding?.上下文 ?? "");
            const word = String(finding?.词 ?? "").trim();
            const hasContext = context.trim().length >= 2;
            if (!hasContext && word.length < 3) continue;
            const key = `${context}\u0000${word}`;
            if (seen.has(key)) continue;
            seen.add(key);
            const pieces: PromptHighlightPiece[] = [];
            for (const piece of finding?.片段 ?? []) {
                if (!piece || !piece.高亮) continue;
                const 起 = Number(piece.起);
                const 止 = Number(piece.止);
                if (!Number.isFinite(起) || !Number.isFinite(止) || 止 <= 起) continue;
                pieces.push({ 起, 止 });
            }
            plan.push({ 上下文: context, 词: word ? [word] : [], 片段: pieces });
        }
    }
    return plan;
}

/** 区间并集：重叠与相邻的都合成一段（避免同一处被包两层 mark、也不出现「断成两截」的高亮）。 */
function mergeSpans(spans: PromptHighlightSpan[]): PromptHighlightSpan[] {
    if (spans.length < 2) return spans;
    const sorted = [...spans].sort((a, b) => a.起 - b.起 || a.止 - b.止);
    const out: PromptHighlightSpan[] = [];
    for (const span of sorted) {
        const tail = out[out.length - 1];
        if (tail && span.起 <= tail.止) {
            if (span.止 > tail.止) tail.止 = span.止;
        } else {
            out.push({ ...span });
        }
    }
    return out;
}

/** 一个文本节点该怎么上色：`上下文` 整句精确命中 → 用 `片段` 的结构化区间（没有片段就整句）；
 *  整句落空（markdown 把句子切碎了）→ 退到最长前缀，如实把能找到的那一段上色；再落空 →
 *  `词` 兜底（只做精确匹配，短词绝不拿前缀猜）。 */
export function promptHighlightSpans(text: string, plan: readonly PromptHighlightItem[]): PromptHighlightSpan[] {
    const spans: PromptHighlightSpan[] = [];
    const hay = String(text ?? "");
    if (!hay) return spans;
    for (const item of plan) {
        const base = item.上下文;
        let matched = false;
        if (base.trim().length >= 2) {
            let from = 0;
            for (;;) {
                const at = hay.indexOf(base, from);
                if (at < 0) break;
                matched = true;
                if (item.片段.length) {
                    for (const piece of item.片段) {
                        const 起 = at + Math.max(0, piece.起);
                        const 止 = at + Math.min(base.length, piece.止);
                        if (止 > 起) spans.push({ 起, 止 });
                    }
                } else {
                    spans.push({ 起: at, 止: at + base.length });
                }
                from = at + base.length;
            }
            if (!matched) {
                const fallback = promptHitRange(hay, base);
                if (fallback) {
                    matched = true;
                    spans.push({ 起: fallback.start, 止: fallback.end });
                }
            }
        }
        if (matched) continue;
        for (const word of item.词) {
            if (word.length < 3) continue;
            const at = hay.indexOf(word);
            if (at >= 0) spans.push({ 起: at, 止: at + word.length });
        }
    }
    return mergeSpans(spans);
}

/** 在正文容器里把计划落到真实文本节点上：返回本轮包出来的 mark（清理时按这个数组原样展开）。
 *  只处理「还没被包过」的文本节点，跳过 script/style 与空节点。 */
export function applyPromptHighlights(
    root: Element | null | undefined,
    plan: readonly PromptHighlightItem[],
): HTMLElement[] {
    const marks: HTMLElement[] = [];
    if (!root || !plan.length || typeof document === "undefined") return marks;
    const doc = root.ownerDocument ?? document;
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node as Text;
        const parent = text.parentElement;
        if (!parent) continue;
        const tag = parent.tagName;
        if (tag === "SCRIPT" || tag === "STYLE") continue;
        if (parent.closest(`mark[${PROMPT_HL_ATTR}]`)) continue;
        if (!(text.nodeValue ?? "").trim()) continue;
        nodes.push(text);
    }
    for (const text of nodes) {
        const spans = promptHighlightSpans(text.nodeValue ?? "", plan);
        // 倒着包：先包靠后的区间，靠前区间的下标才不会被 split 挪走。
        for (let index = spans.length - 1; index >= 0; index -= 1) {
            const range = doc.createRange();
            try {
                range.setStart(text, spans[index].起);
                range.setEnd(text, spans[index].止);
            } catch {
                continue;
            }
            const mark = doc.createElement("mark");
            mark.setAttribute(PROMPT_HL_ATTR, PROMPT_HL_STATE);
            mark.className = PROMPT_HL_CLASS;
            try {
                range.surroundContents(mark);   // 区间落在单个文本节点内，不跨元素
            } catch {
                continue;
            }
            marks.push(mark);
        }
    }
    return marks;
}

/** 把 mark 展开回纯文本（React 已被换掉的节点静默跳过），保住 markdown 的原始文本节点结构。 */
export function unwrapPromptHighlights(marks: readonly Element[] | null | undefined): void {
    for (const mark of marks ?? []) {
        if (!mark || !mark.isConnected) continue;
        const parent = mark.parentNode;
        if (!parent) continue;
        while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
        parent.removeChild(mark);
        parent.normalize();
    }
}

/** 「看正文里的这一句」的升级版：常驻高亮已经在正文里，这里只负责把那个高亮段滚进视野。
 *  候选串按序试（整句 → 词）：命中段文字被候选串包含、或候选串以它开头都算同一处。
 *  返回 null＝正文里根本没有这一处（调用方如实说没找到，不瞎滚）。 */
export function scrollToPromptHighlight(
    root: Element | null | undefined,
    candidates: readonly string[],
): HTMLElement | null {
    if (!root) return null;
    const marks = [...root.querySelectorAll(`mark[${PROMPT_HL_ATTR}]`)] as HTMLElement[];
    if (!marks.length) return null;
    const wants = (candidates ?? [])
        .map((text) => String(text ?? "").trim())
        .filter((text) => text.length >= 2);
    let target: HTMLElement | null = null;
    for (const want of wants) {
        const hit = marks.find((mark) => {
            const text = (mark.textContent ?? "").trim();
            if (text.length < 2) return false;
            return want.includes(text) || text.includes(want.slice(0, Math.min(want.length, 16)));
        });
        if (hit) {
            target = hit;
            break;
        }
    }
    target = target ?? marks[0];
    target.scrollIntoView({ block: "center", inline: "nearest" });
    return target;
}
