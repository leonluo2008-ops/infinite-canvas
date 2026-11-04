/**
 * 拉片 → 资产位（T3-H1/H2）与参考图清单（T3-G1/G2）的纯函数层。
 *
 * 数据源是 bridge `/api/lapian/<taskId>` 的 blocks（实读 schema：`{title, lines[], dna}`，
 * 资产块两块：「核心主体资产」= 待确认清单里已备图的主体，「人物/动物固有资产」= 九 资产逐条）。
 * 这里只做三件事，全部无副作用、不读 i18n、不摸 React，便于探针直接转译真模块跑断言：
 *   1. parseLapianAssetBlocks：blocks → 资产条目（类型 / 名 / 原片描述 / IR 资产序）；
 *   2. collectLapianAssetReferences：画布节点 → 已挂图的资产位（按资产位序排、连续编号）；
 *   3. buildLapianAssetManifest / upsertLapianAssetManifest：清单行（token 序 = 图号序 = images 数组序）。
 *
 * 硬口径（T3 二阶段设计决策 1/6）：
 * - 图号 = 按资产位序（IR 资产序）排序后、已挂图资产的连续编号 1..N；未挂不出项、删图不出幽灵项。
 * - 清单行只放 `@[node:图片节点id]=短说明`：图号由生成期 token 展开时分配（图片1、图片2…），
 *   这样清单里的号与请求 images 数组的号由同一处代码产生，不会对不上。
 */

import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

/** bridge 投影出来的一块：`dna=true` 是 `- 名：…` 型，资产块都在 dna=false 段里。 */
export type LapianBlock = {
    title?: string;
    lines?: string[];
    dna?: boolean;
};

export type LapianAssetItem = {
    /** 资产类型：IR 块名归一（核心主体 / 人物/动物）或块内子标题（底衬道具…）。 */
    type: string;
    /** 资产名：IR 条目冒号前的短名。 */
    name: string;
    /** 原片对应物描述：IR 条目原文。 */
    desc: string;
    /** IR 资产序（0-based）：图号排序键，稳定不变。 */
    order: number;
};

export type LapianAssetSlot = {
    /** 去重键（类型+名）：重复衍生的幂等依据。 */
    key: string;
    order: number;
    type: string;
    name: string;
    /** 节点标题：资产位·〈类型〉·〈名〉。 */
    title: string;
    /** 节点正文：原片对应物 + 挂图提示（截帧失败时整段落到纯文字卡上）。 */
    body: string;
    /** 原片对应物原文（IR 条目），进 metadata.assetSlot.desc。 */
    desc: string;
    /** 原片出现区间原文（`00:00-02.03、04.07-06.00`），进 metadata.assetSlot.span；没有就是空串。 */
    span: string;
};

/** IR 资产行里的「出现于：…」区间原文；截帧时刻由 bridge 侧解析，这里只留给人看。 */
const SPAN_TEXT = /出现于[：:]\s*([^）)\n]+)/;

export type LapianAssetLabels = {
    /** 「资产位」 */
    slotPrefix: string;
    /** 「原片对应物」 */
    original: string;
    /** 「挂图」 */
    attachButton: string;
    /** 挂图提示整句（不含按钮名）。 */
    attachHint: string;
    /** 「未命名参考图」 */
    unnamed: string;
    /** 「本次参考图：」 */
    manifestPrefix: string;
};

export type LapianAssetReference = {
    slotId: string;
    slotName: string;
    imageId: string;
    caption: string;
    /** 1-based 图号（= 请求 images 数组下标 + 1）。 */
    index: number;
};

/** 短说明上限（项目硬约束 17：2–6 字；本模块只管上限截断）。 */
export const LAPIAN_ASSET_CAPTION_MAX = 6;

/** 缺项三态行：〔未取证〕〔不适用〕〔缺字段〕——不是资产，直接跳过。 */
const LACK_MARKER = /^〔(?:未取证|不适用|缺字段)〕/;
/** 条目记号：`1. ` / `1、` / `- ` / `· `（IR 两种写法都出现过）。 */
const ITEM_MARKER = /^\s*(?:\d+[.、)）]|[-*·•])\s+(.*)$/;
/** 短名截断点：冒号、括号、逗号、句号、分号、破折号。 */
const NAME_STOP = /[：:（(，,。；;—]/;
/** 文件名样式：相机/截图默认名（带序号），或在去扩展名后只剩数字/符号的裸文件名。 */
const FILE_NAME = /\.(?:png|jpe?g|webp|gif|bmp|avif|heic|tiff?|mp4)$/i;
const CAMERA_NAME = /^(?:img|image|dsc|dscn|pxl|screenshot|screen[\s_-]?shot|photo|untitled|微信图片|截屏|截图)[\s_-]*\d+/i;
const JUNK_STEM = /^[\d\s_.\-()+]+$/;

/** 资产块判定：块名里带「资产」（核心主体资产 / 人物动物固有资产）。 */
export function isLapianAssetBlock(title?: string): boolean {
    return String(title || "").includes("资产");
}

/** 块名 → 类型前缀：人物/动物 优先（「人物/动物固有资产」），其次核心主体，其余去掉「资产」二字。 */
export function lapianAssetTypeOf(title: string): string {
    const text = String(title || "");
    if (/人物|动物/.test(text)) return "人物/动物";
    if (text.includes("核心主体")) return "核心主体";
    return text.replace(/资产/g, "").trim() || "资产";
}

function assetNameOf(body: string): string {
    const cut = body.search(NAME_STOP);
    const head = (cut >= 0 ? body.slice(0, cut) : body).trim();
    const name = head || body.trim();
    return name.length > 20 ? `${name.slice(0, 20)}` : name;
}

/**
 * blocks → 资产条目（已按块序、条目序排好并去重）。
 *
 * 解析规则全部来自实读样本：
 * - demo1/ai-2：「人物/动物固有资产」一块一行内含 `1. 名：描述\n2. …`（行内嵌换行）；
 * - e2e：「- 底衬道具：」这类「子类标题 + 明细」两层结构，子类标题当类型前缀。
 * 兜底：整块只有一个不带记号、也不带分号逗号堆叠的短行（`产品：香水瓶`）→ 当成一条资产，
 * 免得写成一句话的 IR 被当成「无可替换资产」。
 */
export function parseLapianAssetBlocks(blocks?: LapianBlock[] | null): LapianAssetItem[] {
    const items: Omit<LapianAssetItem, "order">[] = [];
    for (const block of blocks || []) {
        if (!block || !isLapianAssetBlock(block.title)) continue;
        const blockType = lapianAssetTypeOf(String(block.title || ""));
        const lines = (block.lines || []).flatMap((line) => String(line ?? "").split("\n"));
        let pendingType = "";
        let current: Omit<LapianAssetItem, "order"> | null = null;
        let produced = 0;
        const plainLines: string[] = [];
        for (const raw of lines) {
            const text = String(raw || "").trim();
            if (!text || LACK_MARKER.test(text)) {
                current = null;
                continue;
            }
            plainLines.push(text);
            const marked = ITEM_MARKER.exec(text);
            const body = (marked ? marked[1] : text).trim();
            if (!body) continue;
            if (!marked) {
                // 无记号的续行：并入上一条描述（IR 里「必须锁住…」常换行）。
                if (current) current.desc = `${current.desc} ${body}`.trim();
                continue;
            }
            if (/[：:]$/.test(body)) {
                // 「底衬道具：」——子类标题，作下一条的类型前缀，本身不是资产。
                pendingType = body.replace(/[：:]+$/, "").trim();
                current = null;
                continue;
            }
            const name = assetNameOf(body);
            if (!name) continue;
            // 描述里重复的资产名去掉（`背景地垫：灰蓝短绒…` → `灰蓝短绒…`），清单/正文不重复同一句。
            const desc = body.startsWith(name) ? body.slice(name.length).replace(/^[：:]+\s*/, "").trim() || body : body;
            current = { type: pendingType || blockType, name, desc };
            items.push(current);
            produced += 1;
        }
        if (!produced && plainLines.length === 1 && !/[；;]/.test(plainLines[0]) && plainLines[0].length <= 120) {
            // 单行无记号：`类型：值` 取值当资产名（`产品：香水瓶` → 香水瓶），整行留作原片描述。
            const line = plainLines[0];
            const colon = line.search(/[：:]/);
            const name = assetNameOf(colon >= 0 ? line.slice(colon + 1).trim() || line : line);
            if (name) items.push({ type: blockType, name, desc: line });
        }
    }
    const seen = new Set<string>();
    const unique = items.filter((item) => {
        const key = `${item.type}|${item.name}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
    return unique.map((item, index) => ({ ...item, order: index }));
}

/** 资产条目 → 资产位描述（标题 / 正文 / 去重键）。 */
export function buildLapianAssetSlots(items: LapianAssetItem[], labels: LapianAssetLabels): LapianAssetSlot[] {
    return items.map((item) => ({
        key: `${item.type}|${item.name}`,
        order: item.order,
        type: item.type,
        name: item.name,
        title: `${labels.slotPrefix}·${item.type}·${item.name.length > 12 ? item.name.slice(0, 12) : item.name}`,
        body: `${labels.original}：${item.desc}\n${labels.attachButton}：${labels.attachHint}`,
        desc: item.desc,
        span: SPAN_TEXT.exec(item.desc)?.[1]?.trim() || "",
    }));
}

/** 短说明截断：按码点截，压空白；空串原样返回（交给下一级 fallback）。 */
export function truncateAssetCaption(text: string, max = LAPIAN_ASSET_CAPTION_MAX): string {
    const clean = String(text || "").replace(/\s+/g, " ").trim();
    return clean ? Array.from(clean).slice(0, max).join("") : "";
}

/** 文件名样式判定：相机/截图默认名这类标题不许进清单（硬约束：清单永不出现裸文件名）。 */
export function looksLikeImageFileName(title: string): boolean {
    const text = String(title || "").trim();
    if (!text) return true;
    return CAMERA_NAME.test(text);
}

/** 图片节点名 → 短说明候选：去扩展名、丢掉纯数字残名、下划线转空格后再截断。 */
export function imageTitleCaption(title: string): string {
    const text = String(title || "").trim();
    if (!text || looksLikeImageFileName(text)) return "";
    const stem = text.replace(FILE_NAME, "").trim();
    if (!stem || JUNK_STEM.test(stem)) return "";
    return truncateAssetCaption(stem.replace(/[_-]+/g, " ").trim());
}

/**
 * 短说明四级 fallback（设计决策：用户输入 > 资产位名 > 图片节点名 > 未命名参考图）。
 * 每一级都过 6 字截断；空串/文件名样式视为该级没有值。
 */
export function resolveAssetCaption(input: { userInput?: string; slotName?: string; imageTitle?: string; unnamed: string }): string {
    return truncateAssetCaption(input.userInput || "") || truncateAssetCaption(input.slotName || "") || imageTitleCaption(input.imageTitle || "") || input.unnamed;
}

/**
 * 资产位当前挂的图 id：扁平字段是唯一真源，`metadata.assetSlot.attachedImageId` 只是审计口径的镜像。
 * （T3-J2 起资产位节点是官方 Image 节点，挂图字段仍写在 metadata，节点型不影响这里。）
 */
export function attachedImageIdOf(slot: CanvasNodeData): string {
    return String(slot.metadata?.attachedImageId || slot.metadata?.assetSlot?.attachedImageId || "");
}

/**
 * 收集已挂图的资产位（按资产位序排 → 连续编号）。
 * 只认「资产位节点 + attachedImageId 指向的画布图片节点仍存在且有内容」；图被删就不出项（无幽灵节点）。
 * 反向同步（路B#9）：用户在画布上删掉那张 Image → 这里校验不到 → 该项自动从清单消失，不需要额外的同步事件。
 */
export function collectLapianAssetReferences(lapianNodeId: string, nodes: CanvasNodeData[], unnamed: string): LapianAssetReference[] {
    if (!lapianNodeId) return [];
    const slots = nodes
        .filter((node) => node.metadata?.assetSlotSourceId === lapianNodeId && Boolean(attachedImageIdOf(node)))
        .sort((left, right) => (left.metadata?.assetSlotOrder ?? 0) - (right.metadata?.assetSlotOrder ?? 0));
    const references: LapianAssetReference[] = [];
    for (const slot of slots) {
        const imageId = attachedImageIdOf(slot);
        const image = nodes.find((node) => node.id === imageId && node.type === CanvasNodeType.Image && Boolean(node.metadata?.content));
        if (!image) continue;
        references.push({
            slotId: slot.id,
            slotName: String(slot.metadata?.assetSlotName || ""),
            imageId: image.id,
            caption: resolveAssetCaption({ userInput: slot.metadata?.attachedImageDesc, slotName: slot.metadata?.assetSlotName, imageTitle: image.title, unnamed }),
            index: references.length + 1,
        });
    }
    return references;
}

/**
 * 清单行：`本次参考图：@[node:图片1]=香水、@[node:图片2]=女性`。
 * token 展开后就是 `本次参考图：图片1=香水、图片2=女性`，号由生成期按出现序分配，与 images 数组一一对应。
 * 未挂任何图 → 返回空串（清单行不存在）。
 */
export function buildLapianAssetManifest(references: LapianAssetReference[], manifestPrefix: string): string {
    if (!references.length) return "";
    return `${manifestPrefix}${references.map((reference) => `@[node:${reference.imageId}]=${reference.caption}`).join("、")}`;
}

/**
 * 组装串里的清单行原地替换（幂等）：已有清单行 → 换掉；没有 → 追加到末尾；清单为空 → 删掉该行。
 * 用于挂图/摘图后把已派生的配置节点清单刷成最新，避免「清单里还有已删资产」的幽灵项。
 */
export function upsertLapianAssetManifest(composer: string, manifest: string, manifestPrefix: string): string {
    const text = String(composer || "");
    const lines = text.split("\n");
    const index = lines.findIndex((line) => line.trim().startsWith(manifestPrefix));
    if (index >= 0) {
        if (manifest) lines[index] = manifest;
        else lines.splice(index, 1);
        return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
    }
    if (!manifest) return text;
    return `${text.replace(/\s+$/, "")}\n\n${manifest}`;
}
