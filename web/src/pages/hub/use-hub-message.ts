import { App } from "antd";
import type { JointContent, MessageInstance, MessageType } from "antd/es/message/interface";
import { useMemo } from "react";

import { claimToast } from "@/lib/hub/toast";

/** 判重表是**模块级**的：同一条文案不论从哪个按钮、哪个组件弹，1 秒内都只出一条。 */
const TOAST_LOG = new Map<string, number>();
const LEVELS = ["info", "success", "warning", "error"] as const;

/** Toast 薄封装（交互设计 §4）：只拦「同一句文案 1s 内的第二条」，其余原样交给 antd。
 *
 *  为什么单独导出这个包装函数：真机探针要在**浏览器里**验「连点 5 次只出 1 条」——把包装层做成
 *  可直接调用的函数，探针就能拿假 api 打真代码（不必为了验一条防抖去点一个会写任务的按钮）。
 */
export function withToastDedupe(message: MessageInstance, clock: () => number = () => Date.now()): MessageInstance {
    const wrapped = { ...message } as MessageInstance;
    for (const level of LEVELS) {
        const original = message[level];
        wrapped[level] = ((
            content: JointContent,
            duration?: number | VoidFunction,
            onClose?: VoidFunction,
        ): MessageType => {
            const text = typeof content === "string" ? content : "";
            if (!claimToast(TOAST_LOG, text, clock())) return (() => undefined) as unknown as MessageType;
            return original(content, duration, onClose);
        }) as MessageInstance[typeof level];
    }
    return wrapped;
}

/** 用法与 `App.useApp().message` 完全一致（`message.info/success/warning/error` 同签名），
 *  所以调用点只需换一行取值：`const message = useHubMessage();`——文案与语义一字不改。
 *  被拦下的那一次返回一个空的关闭句柄（调用方拿它当返回值时不会炸，也不会有第二条出现）。
 */
export function useHubMessage(): MessageInstance {
    const { message } = App.useApp();
    return useMemo(() => withToastDedupe(message), [message]);
}
