/**
 * 派生节点的画布布局（T3-J1）：一处收敛「往右派生 + 避让」的全部几何。
 *
 * 所有者口径（R11）：画布操作是从左到右，派生节点一律向右；新节点绝不能压住前序节点。
 * 三个弹出点（资产位 / 终稿 / 视频配置）各自算坐标就会再出遮挡，所以这里只用四个纯函数：
 *   placeRightOf / placeGridRightOf / placeRowRightOf + nudgeIntoFree。
 * 判据可量化（路B#7）：结果 AABB 与任何已占用节点之间至少留 gap，即
 *   final.x >= config.x + config.width + gap 且两者纵向不交。
 *
 * 无副作用、不读 i18n、不摸 React——探针可以直接跑。
 */

export type LayoutRect = { x: number; y: number; width: number; height: number };
export type LayoutSize = { width: number; height: number };
/** 画布节点只用到这四个字段，结构上兼容 CanvasNodeData，便于直接传节点。 */
export type LayoutNodeLike = { position: { x: number; y: number }; width: number; height: number };

/** 列间距：派生列与父节点的距离（所有者认可的 96px）。 */
export const DERIVED_GAP = 96;
/** 同类卡片之间的行/列间距。 */
export const DERIVED_GUTTER = 24;

export function rectOfNode(node: LayoutNodeLike): LayoutRect {
    return { x: node.position.x, y: node.position.y, width: node.width, height: node.height };
}

/** 两框是否相交（gap>0 时要求至少隔开 gap；相切算不相交）。 */
export function rectsOverlap(a: LayoutRect, b: LayoutRect, gap = 0): boolean {
    const shrink = Math.max(0, gap);
    return !(a.x + a.width + shrink <= b.x || b.x + b.width + shrink <= a.x || a.y + a.height + shrink <= b.y || b.y + b.height + shrink <= a.y);
}

/**
 * 把期望位置推到不压任何已占用节点的位置：只往右让（横向推开），不动纵向——
 * 纵向跳动会打乱「同一行读数」的视觉基线，而向右是所有者要的主方向。
 */
export function nudgeIntoFree(desired: LayoutRect, occupied: LayoutRect[], gap = DERIVED_GAP): LayoutRect {
    let rect = { ...desired };
    const blockers = occupied.filter((item) => item.width > 0 && item.height > 0);
    for (let attempt = 0; attempt < 32; attempt += 1) {
        const hits = blockers.filter((item) => rectsOverlap(rect, item, gap));
        if (!hits.length) break;
        rect = { ...rect, x: Math.max(...hits.map((item) => item.x + item.width)) + gap };
    }
    return rect;
}

/** 右邻一个节点：纵向默认与锚点居中对齐，横向留 gap 并避让。 */
export function placeRightOf(anchor: LayoutRect, size: LayoutSize, occupied: LayoutRect[], options: { gap?: number; align?: "center" | "top" } = {}): LayoutRect {
    const gap = options.gap ?? DERIVED_GAP;
    const y = options.align === "top" ? anchor.y : anchor.y + (anchor.height - size.height) / 2;
    return nudgeIntoFree({ x: anchor.x + anchor.width + gap, y, ...size }, occupied, gap);
}

/**
 * 右邻网格：整块先避让、再切格——先逐格避让会把网格推歪，行/列就散了。
 * columns 由调用方定（资产位按资产数开方取，1–3 列）。
 */
export function placeGridRightOf(
    anchor: LayoutRect,
    count: number,
    cell: LayoutSize,
    occupied: LayoutRect[],
    options: { columns?: number; gap?: number; gapX?: number; gapY?: number } = {},
): LayoutRect[] {
    if (count <= 0) return [];
    const gap = options.gap ?? DERIVED_GAP;
    const gapX = options.gapX ?? DERIVED_GUTTER;
    const gapY = options.gapY ?? DERIVED_GUTTER;
    const columns = Math.max(1, Math.min(options.columns ?? Math.ceil(Math.sqrt(count)), count));
    const rows = Math.ceil(count / columns);
    const blockSize = { width: columns * cell.width + (columns - 1) * gapX, height: rows * cell.height + (rows - 1) * gapY };
    const block = nudgeIntoFree({ x: anchor.x + anchor.width + gap, y: anchor.y, ...blockSize }, occupied, gap);
    return Array.from({ length: count }, (_, index) => ({
        x: block.x + (index % columns) * (cell.width + gapX),
        y: block.y + Math.floor(index / columns) * (cell.height + gapY),
        width: cell.width,
        height: cell.height,
    }));
}

/** 右邻一排：整排一起避让（指令 → 配置这种横向链），后续节点的 x 由前一个的右缘推。 */
export function placeRowRightOf(anchor: LayoutRect, sizes: LayoutSize[], occupied: LayoutRect[], options: { gap?: number } = {}): LayoutRect[] {
    if (!sizes.length) return [];
    const gap = options.gap ?? DERIVED_GAP;
    const rowWidth = sizes.reduce((sum, size) => sum + size.width, 0) + (sizes.length - 1) * gap;
    const rowHeight = Math.max(...sizes.map((size) => size.height));
    const block = nudgeIntoFree(
        { x: anchor.x + anchor.width + gap, y: anchor.y + (anchor.height - rowHeight) / 2, width: rowWidth, height: rowHeight },
        occupied,
        gap,
    );
    let cursor = block.x;
    return sizes.map((size) => {
        const rect = { x: cursor, y: block.y + (rowHeight - size.height) / 2, width: size.width, height: size.height };
        cursor += size.width + gap;
        return rect;
    });
}
