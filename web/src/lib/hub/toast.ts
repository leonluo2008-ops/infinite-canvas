/** Toast 同文案去重（交互设计 §4）：同一条文案 1000ms 内只弹第一条。
 *
 *  为什么收在工具层：连点叠 N 条是 antd `message` 系列的**公共**病（保存失败 / 保留失败 / 强制结束
 *  都走它），逐个按钮防抖必然漏——一个按钮忘了加，缺陷就在别的按钮上复发。
 *  口径：key = 文案原文；判重只看「上一条真弹出去的时刻」，窗口内被吞掉的点击**不续期**
 *  （续期会让连点无限延长静默期：手一直点就永远看不到第二次提示）。
 *  不改任何文案与语义：拦下的只是「同一句话的第二条」，内容一字不动。
 *
 *  纯函数 + 显式时钟：探针直接喂时间戳断言，不读 `Date.now()`、不摸 DOM。
 */

export const HUB_TOAST_DEDUPE_MS = 1000;

/** 判重表上限（长会话里文案会变，不能只增不减；超限先清过期项）。 */
const LOG_LIMIT = 200;

export type ToastLog = Map<string, number>;

/** 这一条现在允许弹吗？（允许时写判重表并返回 true） */
export function claimToast(
    log: ToastLog,
    text: string,
    now: number,
    windowMs: number = HUB_TOAST_DEDUPE_MS,
): boolean {
    const key = String(text ?? "");
    if (!key) return true;                     // 非文本载荷（ReactNode 等）不拦，原样交给 antd
    const last = log.get(key);
    if (last !== undefined && now - last < windowMs) return false;
    log.set(key, now);
    if (log.size > LOG_LIMIT) {
        for (const [old, at] of log) if (now - at >= windowMs) log.delete(old);
    }
    return true;
}
