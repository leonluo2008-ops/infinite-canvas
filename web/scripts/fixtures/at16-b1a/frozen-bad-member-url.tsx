/**
 * AT-16 B1a 坏例夹具②：成员访问形态（非 props 解构）。
 *
 * 用途：证明检查器不只抓 props 间接传值——`card.代表帧.url` 这类 `.url` 成员表达式直接赋给
 * `<img src=>` 同样判红（修复前 drawer 里 FrameWithPosition/GroupFrame 就是这形态）。
 */
function FramePreview({ card }: { card: { 代表帧: { url: string } } }) {
    return <img src={card.代表帧.url} alt="原片帧" className="aspect-video" />;
}
