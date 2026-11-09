/**
 * AT-10 C3 帧标注几何（纯函数：无 React / 无 DOM / 无 i18n，可被 esbuild 打包探针直接断言）。
 *
 * 坐标系铁律（L1 C07 几何反例）：帧内框的 `x%/y%/w%/h%` 是**相对 object-contain 后实际内容盒**的百分比，
 * 不是外框（224 的外框 contain 竖屏片后格宽差 3 倍）。所以这里只做两件事：
 *   ① 舞台尺寸 + contain 内容盒——overlay 必须与图片实际内容区共用同一 rect；
 *   ② 图例（行号 / 短名 / 角标）竖向 rail 的 rect 与引线端点——逐条可见可分、互不相交、不出舞台。
 *
 * 三态诚实（AT-10 §三）：未定位 → 不画框、不标镜次；无自身位置 → 帧与镜次照给、他人框照画、本卡不出框。
 */

export type Rect = { x: number; y: number; w: number; h: number };
export type Stage = { w: number; h: number };

/** 图未加载时按 16:9 占位（加载完成立刻按 naturalWidth/naturalHeight 重算）。 */
export const FALLBACK_RATIO = 16 / 9;
/** 舞台高固定：窗口 resize 只影响外层布局，不改标注几何（不会出现缩放漂移）。 */
export const STAGE_H = 208;
export const STAGE_MIN_W = 108;
export const STAGE_MAX_W = 240;
/** 图例 rail（在舞台右侧，独立于图片容器 → 永不被 overflow 裁掉）。 */
export const LEGEND_GAP = 6;
export const LEGEND_W = 112;
export const LEGEND_CHIP_H = 26;
export const LEGEND_CHIP_H_MIN = 18;
export const LEGEND_CHIP_GAP = 4;
/** 框内「#行号」角标的最小可用尺寸（小于它只画框，标识交给图例 rail）。 */
export const BADGE_MIN_W = 30;
export const BADGE_MIN_H = 16;

/** 图片元素的最小可判定面（HTMLImageElement / new Image() 都满足）：只读 complete + 自然尺寸。 */
export type ImageLike = { complete?: boolean; naturalWidth?: number; naturalHeight?: number };

/**
 * 从图片元素取真实宽高比：只有「已加载完成且自然尺寸有效」才返回，否则 null（= 仍需等 load 事件）。
 * 为什么不能只靠 onLoad：① antd Image（@rc-component/image）根本不透传 onLoad——内部 `<img>` 只吃
 * COMMON_PROPS 白名单（不含 onLoad）+ 自己的 srcAndOnload；② 命中浏览器缓存时 load 事件早于 React 挂载。
 * 两种情形下 onLoad 都永不触发，ratio 恒 null → 舞台退回 16:9，竖屏帧的框就落到 letterbox 黑边上。
 * 所以挂载期必须主动查一次 complete（本函数就是这次查询的唯一判定口）。
 */
export function ratioFromImage(img?: ImageLike | null): number | null {
    if (!img || !img.complete) return null;
    const w = img.naturalWidth || 0;
    const h = img.naturalHeight || 0;
    return w > 0 && h > 0 ? w / h : null;
}

/** 比例兜底：非有限正数/缺值 → 16:9。 */
export function safeRatio(ratio?: number | null): number {
    return typeof ratio === "number" && Number.isFinite(ratio) && ratio > 0 ? ratio : FALLBACK_RATIO;
}

/** 舞台像素尺寸：高固定，宽按帧真实宽高比（竖屏窄、横屏宽），夹在 [MIN_W, MAX_W]。 */
export function stageSize(ratio?: number | null): Stage {
    const h = STAGE_H;
    const w = Math.min(STAGE_MAX_W, Math.max(STAGE_MIN_W, Math.round(h * safeRatio(ratio))));
    return { w, h };
}

/**
 * contain 后实际内容盒（相对舞台左上角，px，保留小数）。
 * 舞台尺寸本就按真实比例定 → 内容盒=整框；只有比例对不上（图未加载 / 尺寸被夹取）才出现 letterbox。
 * 判等容差 0.5% 是为了避免四舍五入在末列留 1px 缝（与 C2 取 66.67 而非 66.66 同源问题）。
 */
export function containRect(ratio: number | null | undefined, stage: Stage): Rect {
    const r = safeRatio(ratio);
    const stageRatio = stage.w / stage.h;
    if (Math.abs(stageRatio - r) < 0.005) return { x: 0, y: 0, w: stage.w, h: stage.h };
    if (stageRatio >= r) {
        const w = stage.h * r;
        return { x: (stage.w - w) / 2, y: 0, w, h: stage.h };
    }
    const h = stage.w / r;
    return { x: 0, y: (stage.h - h) / 2, w: stage.w, h };
}

const clamp01 = (value: number) => (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0);

/** 九宫格百分比框 → 内容盒内 px rect；越界一律夹取到内容盒内（不画到框外）。 */
export function boxRect(box: { "x%": number; "y%": number; "w%": number; "h%": number }, content: Rect): Rect {
    const x = content.x + clamp01(box["x%"] / 100) * content.w;
    const y = content.y + clamp01(box["y%"] / 100) * content.h;
    const w = Math.min(clamp01(box["w%"] / 100) * content.w, content.x + content.w - x);
    const h = Math.min(clamp01(box["h%"] / 100) * content.h, content.y + content.h - y);
    return { x, y, w: Math.max(0, w), h: Math.max(0, h) };
}

/** 框里放得下「#行号」角标吗（放不下就只画框体）。 */
export function badgeFits(rect: Rect): boolean {
    return rect.w >= BADGE_MIN_W && rect.h >= BADGE_MIN_H;
}

export type LegendItem = { 行: number; box: Rect };
export type LegendChip = { 行: number; rect: Rect; compact: boolean };
export type LegendLink = { 行: number; x1: number; y1: number; x2: number; y2: number };
export type LegendLayout = { x: number; w: number; chips: LegendChip[]; links: LegendLink[]; height: number };

/**
 * 图例竖排 rail：行号升序逐条堆叠（同一格的多行也各占一条，不按格去重身份），
 * 因此 chip rect 恒不相交；引线从 chip 左缘中点指向该行框右缘中点，同格多框引线汇聚=提示「这些都在同一格」。
 * 行数太多放不下时缩 chip 高度（到下限后超出舞台，但 wrapper 高度随之增长，标签仍不被裁）。
 */
export function layoutLegend(items: LegendItem[], stage: Stage): LegendLayout {
    const sorted = [...items].sort((a, b) => a.行 - b.行);
    const x = stage.w + LEGEND_GAP;
    if (!sorted.length) return { x, w: 0, chips: [], links: [], height: stage.h };

    const n = sorted.length;
    const raw = Math.floor((stage.h - LEGEND_CHIP_GAP * (n - 1)) / n);
    const chipH = Math.max(LEGEND_CHIP_H_MIN, Math.min(LEGEND_CHIP_H, raw));
    const chips: LegendChip[] = sorted.map((item, index) => ({
        行: item.行,
        compact: chipH < 24,
        rect: { x, y: index * (chipH + LEGEND_CHIP_GAP), w: LEGEND_W, h: chipH },
    }));
    const links: LegendLink[] = sorted.map((item, index) => {
        const chip = chips[index];
        return {
            行: item.行,
            x1: chip.rect.x,
            y1: chip.rect.y + chip.rect.h / 2,
            x2: item.box.x + item.box.w,
            y2: item.box.y + item.box.h / 2,
        };
    });
    const totalH = n * chipH + (n - 1) * LEGEND_CHIP_GAP;
    return { x, w: LEGEND_W, chips, links, height: Math.max(stage.h, totalH) };
}

// ---------------------------------------------------------------- 三态

export type FrameStateInput = {
    代表帧?: { url?: string; 秒?: number; 镜次?: number | null; 定位?: string } | null;
    自身框?: boolean;
    帧内框?: { 本卡?: boolean }[];
    帧?: { url?: string }[];
    画面位置?: { 格: number; 镜次: number } | null;
};

/** precise=帧+镜次+本卡框；no-own-box=帧+镜次+他人框（本卡无框）；unlocated=不画框不标镜次；legacy=旧字段驱动。 */
export type FrameState = "precise" | "no-own-box" | "unlocated" | "legacy";

export function frameState(card: FrameStateInput): FrameState {
    const url = (card.代表帧?.url || "").trim();
    const legacyUrl = (card.帧 || []).map((frame) => (frame.url || "").trim()).find(Boolean) || "";
    if (!url) return legacyUrl ? "legacy" : "unlocated";
    if ((card.代表帧?.定位 || "").trim() !== "精确") return "unlocated";
    const hasOwn = Boolean(card.自身框) || (card.帧内框 || []).some((box) => Boolean(box.本卡));
    return hasOwn ? "precise" : "no-own-box";
}

export type FrameCaption = {
    /** 帧上主标注（未定位=诚实兜底文案，绝不标镜次）。 */
    main: string;
    /** 诚实补语（无自身位置 → 「未能标注位置」；其余为空）。 */
    honest: string;
    /** 操作性提示（点图放大）。 */
    hint: string;
};

/** 秒数文本：保小数去尾零（5.35 → 5.35s、1.0 → 1s）。 */
export function formatSeconds(second?: number | null): string {
    if (typeof second !== "number" || !Number.isFinite(second) || second <= 0) return "";
    const text = second.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
    return `${text}s`;
}

/** 帧标注文案（三态诚实）。 */
export function frameCaption(card: FrameStateInput, state: FrameState = frameState(card)): FrameCaption {
    if (state === "unlocated") {
        return { main: "未能定位到该资产出现的镜头 · 暂用整片画面", honest: "", hint: "" };
    }
    if (state === "legacy") {
        const position = card.画面位置;
        const head = position ? `九宫格第${position.格}格` : "";
        const shot = position && position.镜次 > 0 ? `基于第${position.镜次}镜` : "";
        return { main: [head, shot, "AI粗标"].filter(Boolean).join(" · "), honest: "", hint: "点图放大" };
    }
    const shot = card.代表帧?.镜次;
    const head = typeof shot === "number" && shot > 0 ? `第${shot}镜` : "";
    const second = formatSeconds(card.代表帧?.秒);
    const main = [head, second, "AI粗标"].filter(Boolean).join(" · ");
    return { main, honest: state === "no-own-box" ? "未能标注位置" : "", hint: "点图放大" };
}

/** 同帧其他资产条数（图例里 本卡=false 的行）——「同画面其他资产」与确认组区分开。 */
export function otherBoxCount(boxes: { 本卡: boolean }[]): number {
    return boxes.filter((box) => !box.本卡).length;
}
