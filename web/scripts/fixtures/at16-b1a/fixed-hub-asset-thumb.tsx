/**
 * AT-16 B1a 修复版夹具：受控出口形态（与 task-detail-drawer.tsx 的 hub-asset-exit 标记区同形）。
 *
 * 用途：证明检查器**修后绿**——ref 原串进、hubAssetRefView() 出 src/href，标记区外无裸赋值。
 */
import { hubAssetDisplayUrl, hubAssetRefView } from "../../../src/pages/hub/hub-api";

/* hub-asset-exit:begin */
export function HubAssetThumb({ assetRef, alt }: { assetRef: string; alt: string }) {
    const view = hubAssetRefView(assetRef);
    return (
        <a href={view.href} target="_blank" rel="noreferrer" className="block">
            <img src={view.src} alt={alt} className="aspect-video w-40 rounded-md" />
        </a>
    );
}
/* hub-asset-exit:end */

export function FramePreview({ card }: { card: { 代表帧: { url: string } } }) {
    return <img src={hubAssetDisplayUrl(card.代表帧.url)} alt="原片帧" className="aspect-video" />;
}
