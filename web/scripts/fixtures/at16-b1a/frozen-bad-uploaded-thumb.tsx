/**
 * AT-16 B1a 坏例夹具（**冻结的修复前代码**，逐字取自 task-detail-drawer.tsx@dd433c0 第 1614-1637 行）。
 *
 * 用途：证明防再漏检查器**会红**——`url` 经 props 间接传入（`{ url, state, n }` 解构），
 * 组件内 `<a href={url}>` / `<img src={url}>` 直接把 ref 原串当显示 URL 用，就是「上传后图不显示」
 * 的真实缺陷形态。禁止修改本文件（冻结证据），修好后另建 fixed- 夹具。
 */
function UploadedThumb({ url, state, n }: { url: string; state: string; n: number }) {
    if (state === "expired" || state === "unknown") {
        return <div data-hub-asset-state={state}>占位</div>;
    }
    return (
        <a href={url} target="_blank" rel="noreferrer" className="block">
            <img src={url} alt={`#${n} 换图`} className="aspect-video w-40 rounded-md" />
        </a>
    );
}
