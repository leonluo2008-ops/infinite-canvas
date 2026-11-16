/**
 * 时间戳域转换（S3-B2，任务书 §2.1 v1.2）。
 *
 * 两个域必须显式转换，**不做单位猜测**：
 *  - store 既有模型：`CanvasProject.updatedAt` / `CanvasDeletedProject.deletedAt` 是 **ISO 字符串**（不改模型）；
 *  - 服务端：`updatedAt` / `baseUpdatedAt` / `deletedAt` / `version` / `serverTime` 统一 **int epoch 毫秒**
 *    （域 `1e11 ≤ ts ≤ 4e15`，秒级/ISO 串一律 422 —— 服务端不替客户端猜单位）。
 *
 * 放在 lib 层而不是服务层：`use-canvas-store` 的删除 clamp 也要用它，而 store 不能反向 import 服务层。
 */

/** 服务端时间戳域（与服务端 `project_store.TS_MIN/TS_MAX` 同口径）。 */
export const TS_MIN = 1e11;
export const TS_MAX = 4e15;

/** ISO / 数字 / 数字串 → int ms；不可解析返 null（调用方据此拒绝推送，绝不代填）。 */
export function toMillis(value: unknown): number | null {
    if (typeof value === "number" && Number.isFinite(value)) return Math.round(value);
    if (typeof value === "string") {
        const trimmed = value.trim();
        if (!trimmed) return null;
        if (/^\d+$/.test(trimmed)) {
            const parsed = Number(trimmed);
            return Number.isFinite(parsed) ? parsed : null;
        }
        const parsed = Date.parse(trimmed);
        return Number.isNaN(parsed) ? null : parsed;
    }
    return null;
}

/** int ms → ISO（store 模型口径）。 */
export function toIso(ms: number): string {
    return new Date(ms).toISOString();
}

/** 是否落在服务端可接收的毫秒域（前端推送前自检，省一次必然 422 的往返）。 */
export function inMillisDomain(ms: number | null): ms is number {
    return ms !== null && ms >= TS_MIN && ms <= TS_MAX;
}

/**
 * 删除时序 clamp（任务书 §2.1 v1.2 复审定案）：
 * `deletedAt = max(now, project.updatedAt)`。
 *
 * 「旧设备 PUT 不复活」只在 `deletedAt ≥ updatedAt` 时成立，窗口宽度 = 两设备时钟差；
 * 本机时钟落后于项目最后改写时刻时，直接用 now 会把墓碑做旧，另一台设备随后的 PUT 就能复活项目（V3 漏口）。
 */
export function clampDeletedAtIso(updatedAtIso: string | undefined, nowMs: number): string {
    const projectMs = toMillis(updatedAtIso) ?? 0;
    return toIso(Math.max(nowMs, projectMs));
}
