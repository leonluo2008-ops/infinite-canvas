import { Home, RotateCw, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useRouteError } from "react-router-dom";

import { routeErrorKind } from "@/lib/route-error";

/**
 * 全局路由兜底页（挂在 router.tsx 的 errorElement 上）。
 *
 * 目标：任何路由级异常（含懒加载 chunk 失效 = 页面版本过期）都出这张人话页，
 * 绝不把英文堆栈甩给用户；主按钮永远是「刷新页面」，因为这类异常 99% 刷新即恢复。
 * 归因逻辑在 `@/lib/route-error`（纯函数，探针可直接测）。
 */
export default function RouteErrorPage() {
    const error = useRouteError();
    const { t } = useTranslation();
    const kind = routeErrorKind(error);
    const hint = t(`routeError.${kind}.hint`);

    return (
        <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground" data-route-error={kind}>
            <main className="flex h-full min-h-0 items-center justify-center overflow-y-auto bg-background bg-[radial-gradient(#e5e7eb_1px,transparent_1px)] px-6 py-10 text-stone-900 [background-size:16px_16px] dark:bg-[radial-gradient(rgba(245,245,244,.16)_1px,transparent_1px)] dark:text-stone-100">
                <section className="w-full max-w-md text-center">
                    <div className="mx-auto mb-6 flex size-16 items-center justify-center rounded-lg border border-stone-200 bg-white text-2xl font-semibold shadow-sm dark:border-stone-800 dark:bg-stone-900">
                        <TriangleAlert className="size-7 text-amber-500" />
                    </div>
                    <h1 className="text-2xl font-semibold tracking-normal" data-route-error-title>
                        {t(`routeError.${kind}.title`)}
                    </h1>
                    <p className="mt-3 text-sm leading-6 opacity-70">{t(`routeError.${kind}.description`)}</p>
                    {hint ? <p className="mt-2 text-xs leading-5 opacity-50">{hint}</p> : null}
                    <div className="mt-8 flex flex-wrap justify-center gap-3">
                        {/* 刷新用原生 <a href=当前地址> 做整页重载：既是「刷新」语义，也保证不依赖任何 JS 状态还能用 */}
                        <a
                            href={window.location.href}
                            className="inline-flex h-10 items-center gap-2 rounded-lg px-4 text-sm font-medium transition hover:opacity-90"
                            style={{ background: "var(--foreground)", color: "var(--background)" }}
                            data-route-error-reload
                        >
                            <RotateCw className="size-4" />
                            {t("routeError.refresh")}
                        </a>
                        <Link
                            to="/"
                            className="inline-flex h-10 items-center gap-2 rounded-lg px-4 text-sm font-medium transition hover:opacity-80"
                            style={{ border: "1px solid var(--border)", color: "var(--foreground)" }}
                        >
                            <Home className="size-4" />
                            {t("routeError.home")}
                        </Link>
                    </div>
                </section>
            </main>
        </div>
    );
}
