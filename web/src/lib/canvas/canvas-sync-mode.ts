/**
 * 画布项目同步模式（S3-B2）——**无依赖的模块级单值**，供不能 import 服务层的模块读取。
 *
 * 为什么要有它：`services/app-sync.ts`（WebDAV 同步）必须知道「画布项目现在由账号服务端负责」才能
 * 跳过画布域（任务书 §2.5：remote 模式禁用 WebDAV 项目同步，两真源冲突）。但 app-sync 不能 import
 * `services/canvas-project-sync.ts`（后者要用 app-sync 的合并语义）—— 抽一个叶子模块打断环。
 *
 * `local` 是初值：单人模式与「探针拿不到登录态」恒为 local，local 分支逐字节不变（§2.5）。
 */
export type CanvasSyncMode = "local" | "remote" | "offline";

let mode: CanvasSyncMode = "local";

export function getCanvasSyncMode(): CanvasSyncMode {
    return mode;
}

export function setCanvasSyncMode(next: CanvasSyncMode): void {
    mode = next;
}

/** 只有账号服务端模式才算「有另一个真源」。离线态仍属于服务端模式（联网后补推）。 */
export function isRemoteCanvasMode(): boolean {
    return mode === "remote" || mode === "offline";
}
