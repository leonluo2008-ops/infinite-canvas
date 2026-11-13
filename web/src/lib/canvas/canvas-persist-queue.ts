/**
 * 落盘队列（P5；P0a §5.4 ③ + 收敛裁决 A8/G14）：防抖全量序列化 + 显式 flush。
 *
 * 为什么单列一层：`use-canvas-store` 的持久化是「set → 400ms 防抖 → JSON.stringify(全量) → localforage」。
 * 防抖窗口内崩溃就丢最新状态（实例跑到待批却查不到成员），而 flush 又必须能**立即**落盘。
 * 把「排队/合并/立即写/失败可见」抽成不依赖浏览器运行时的纯逻辑，页面用真实 store 接，
 * 探针用真 storage（真写文件）跑同一份代码——不复制第二套写盘逻辑（红线 25 精神）。
 *
 * 语义边界：
 * - `setItem` = 入队（合并到最新一份，绝不排队多份旧快照）；
 * - `prime`  = 恢复基线（刚从磁盘读上来的值，已知已落盘，不排队）；
 * - `flush`  = 丢掉防抖计时器，立刻写当前最新值；写完才算成功，失败**原样返回错误**并保留待写值
 *              （下一次 flush/防抖仍会重试），绝不静默吞掉。
 */

export type PersistQueueStorage = {
    setItem: (name: string, value: string) => unknown;
};

export type PersistQueueOptions<T> = {
    name: string;
    storage: PersistQueueStorage;
    /** 防抖窗口（与既有 store 一致：400ms）。 */
    delayMs?: number;
    /** 引用级去重：与「最后排队/最后写入」相同则不入队（zustand 每次 set 都会把同一份 state 交过来）。 */
    isEqual?: (previous: T, next: T) => boolean;
    /** 写失败回调（页面把错误挂到 store 上，UI 可见；探针据此断言「失败可见」）。 */
    onError?: (error: Error, value: T) => void;
    setTimer?: (callback: () => void, ms: number) => unknown;
    clearTimer?: (handle: unknown) => void;
};

export type FlushResult = {
    ok: boolean;
    /** 本次是否真的写了盘（没有待写值时为 false——不代表失败）。 */
    written: boolean;
    /** 写入字节数（UTF-16 字符数，与 JSON 串长度同口径）。 */
    bytes: number;
    error?: string;
};

export type PersistQueue<T> = {
    /** 入队（防抖）。 */
    setItem: (value: T) => void;
    /** 恢复基线：视为「已落盘」，清空待写。 */
    prime: (value: T) => void;
    /** 立即落盘当前最新值（不等防抖窗口）。 */
    flush: (reason?: string) => Promise<FlushResult>;
    /** 是否有待写值（防抖窗口内 = true）。 */
    pending: () => boolean;
    /** 最近一次成功写入的字节数。 */
    lastWrittenBytes: () => number;
    /** 最近一次写入错误（成功后清空）。 */
    lastError: () => string | null;
    /** flush 调用次数（探针据此断言编排关键节点真的调了）。 */
    flushCount: () => number;
};

const defaultSetTimer = (callback: () => void, ms: number) => setTimeout(callback, ms);
const defaultClearTimer = (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>);

export function createPersistQueue<T>(options: PersistQueueOptions<T>): PersistQueue<T> {
    const delayMs = Math.max(0, Number(options.delayMs ?? 400));
    const setTimer = options.setTimer ?? defaultSetTimer;
    const clearTimer = options.clearTimer ?? defaultClearTimer;

    let queued: { value: T; json: string } | null = null;
    let timer: unknown = null;
    /** 最近一次落盘的值（prime 或成功写入）——去重基线。 */
    let persisted: T | null = null;
    let lastErrorText: string | null = null;
    let lastBytes = 0;
    let flushCalls = 0;
    /** flush 串行链：避免两次 flush 并发写同一份值（后一次只需确认已写完）。 */
    let chain: Promise<FlushResult> = Promise.resolve({ ok: true, written: false, bytes: 0 });

    const cancelTimer = () => {
        if (timer === null) return;
        clearTimer(timer);
        timer = null;
    };

    const writeOnce = async (): Promise<FlushResult> => {
        if (!queued) return { ok: true, written: false, bytes: lastBytes };
        const target = queued;
        try {
            await options.storage.setItem(options.name, target.json);
            if (queued === target) queued = null;
            persisted = target.value;
            lastBytes = target.json.length;
            lastErrorText = null;
            return { ok: true, written: true, bytes: target.json.length };
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error ?? "未知错误");
            // 失败不丢待写值：下一次 flush / 防抖重试仍拿同一份最新状态。
            lastErrorText = message;
            options.onError?.(new Error(message), target.value);
            return { ok: false, written: false, bytes: 0, error: message };
        }
    };

    return {
        setItem(value) {
            const reference = queued ? queued.value : persisted;
            if (reference !== null && options.isEqual && options.isEqual(reference, value)) return;
            queued = { value, json: JSON.stringify(value) };
            cancelTimer();
            timer = setTimer(() => {
                timer = null;
                void writeOnce();
            }, delayMs);
        },
        prime(value) {
            cancelTimer();
            queued = null;
            persisted = value;
        },
        flush() {
            cancelTimer();
            flushCalls += 1;
            chain = chain.then(() => writeOnce());
            return chain;
        },
        pending: () => queued !== null,
        lastWrittenBytes: () => lastBytes,
        lastError: () => lastErrorText,
        flushCount: () => flushCalls,
    };
}
