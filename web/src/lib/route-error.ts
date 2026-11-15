/**
 * 路由级异常的人话归因（纯函数，无 React / DOM 依赖）。
 *
 * 背景：开发态 vite 重新预构建依赖后，浏览器里开着的旧页面还引用旧 hash 的 chunk，
 * 动态 import（例如 streamdown 的代码高亮块）会 404/504，抛出
 * `Failed to fetch dynamically imported module: ...`。React Router 没有 errorElement 时
 * 这类异常直接落到裸错误页（整页白屏 + 英文堆栈），用户看到的是「崩溃」。
 *
 * 本文件只负责归因（决定说哪句人话）；兜底行为与归因无关——
 * 任何路由异常都由 RouteErrorPage 出同一张可刷新的人话页。
 */

/** stale = 页面版本过期 / 资源加载失败；notFound / forbidden = 路由 404 / 无权限；unknown = 其它。 */
export type RouteErrorKind = "stale" | "notFound" | "forbidden" | "unknown";

/** 浏览器与打包器在「动态 import 的 chunk 取不到」时给的典型措辞（英文原文，用于识别，不用于展示）。 */
const STALE_MODULE_PATTERNS = [
    "failed to fetch dynamically imported module",
    "error loading dynamically imported module",
    "dynamically imported module",
    "importing a module script failed",
    "unable to preload css",
    "chunkloaderror",
    "loading chunk",
];

/** 是否为「页面版本过期 / chunk 取不到」。 */
export function isStaleModuleError(error: unknown): boolean {
    const text = errorTextOf(error).toLowerCase();
    if (!text) return false;
    return STALE_MODULE_PATTERNS.some((pattern) => text.includes(pattern));
}

/** 归因。判断顺序：HTTP 状态优先（路由 404/403 有自己的说法），其次才是 chunk 失效。 */
export function routeErrorKind(error: unknown): RouteErrorKind {
    const status = routeErrorStatus(error);
    if (status === 404) return "notFound";
    if (status === 401 || status === 403) return "forbidden";
    if (isStaleModuleError(error)) return "stale";
    return "unknown";
}

/** React Router 的 ErrorResponse（throw new Response(...) / data router 404）带 status；普通 Error 没有。 */
function routeErrorStatus(error: unknown): number {
    if (!error || typeof error !== "object") return 0;
    const status = Number((error as { status?: unknown }).status);
    return Number.isFinite(status) ? status : 0;
}

/** 摊平 name/message 与 cause 链（chunk 失败常被包在 cause 里）。 */
function errorTextOf(error: unknown): string {
    if (typeof error === "string") return error;
    if (!error || typeof error !== "object") return "";
    const parts: string[] = [];
    for (const key of ["name", "message"] as const) {
        const value = (error as Record<string, unknown>)[key];
        if (typeof value === "string") parts.push(value);
    }
    const cause = (error as { cause?: unknown }).cause;
    if (cause && cause !== error) parts.push(errorTextOf(cause));
    return parts.join(" ");
}
