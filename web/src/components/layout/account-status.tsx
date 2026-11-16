import { useEffect, useState } from "react";
import { Tooltip } from "antd";
import { LogIn, LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

type SessionInfo = { username: string; expiresAt: number } | null;

/**
 * MULTI_USER=1 时画布顶栏的账号状态（S1 账号骨架）：
 * - 已登录：显示用户名 + 退出按钮
 * - 未登录：显示登录入口（跳 /multiuser/ 登录页）
 * 服务端 MULTI_USER=0 时 /api/auth/me 返回非 2xx，组件静默不渲染（单人模式零改动）。
 */
export function AccountStatus({ iconStyle, className }: { iconStyle?: React.CSSProperties; className?: string }) {
    const { i18n } = useTranslation();
    const [session, setSession] = useState<SessionInfo>(null);
    const [ready, setReady] = useState(false);

    useEffect(() => {
        let alive = true;
        fetch("/api/auth/me")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
                if (alive) setSession(d && d.username ? d : null);
                if (alive) setReady(true);
            })
            .catch(() => {
                if (alive) setReady(true);
            });
        return () => {
            alive = false;
        };
    }, []);

    if (!ready) return null;
    if (!session) {
        return (
            <Tooltip title={i18n.t("account.login")} mouseEnterDelay={0.2}>
                <a
                    href="/multiuser/"
                    className={cn(
                        "inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-stone-600 transition-colors hover:bg-black/5 hover:text-stone-950 dark:text-stone-300 dark:hover:bg-white/10 dark:hover:text-white [&_svg]:size-4",
                        className,
                    )}
                    style={iconStyle}
                    aria-label={i18n.t("account.login")}
                >
                    <LogIn className="size-4" />
                </a>
            </Tooltip>
        );
    }
    const logout = () => {
        // S3-B2 §2.4：登出前先把本站账号的画布缓存「留档到隔离键 + 清空共享主键」，
        // 否则下一个人（B 账号）在同一浏览器登录会读到 A 的项目（V9）。
        // 顺序：留档/清空完成 → 才踢会话 → 才跳转（跳转前必须落完，页面一走就写不了了）。
        void (async () => {
            try {
                const { prepareCanvasLogout } = await import("@/services/canvas-project-sync");
                await prepareCanvasLogout();
            } catch (error) {
                console.warn("[account] 登出前清空画布缓存失败（数据仍在盘上，下次登录会按 owner 重新判定）", error);
            }
            try {
                await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
            } catch {
                // 会话没踢掉也只能继续跳转：本地已清空，服务端会话过期由后端判定
            }
            location.href = "/multiuser/";
        })();
    };
    return (
        <>
            <Tooltip title={`${i18n.t("account.loggedIn")}: ${session.username}`} mouseEnterDelay={0.2}>
                <span
                    className={cn(
                        "inline-flex h-7 shrink-0 cursor-default items-center rounded-md px-1.5 text-[11px] font-semibold tracking-tight",
                        className,
                    )}
                    style={iconStyle}
                >
                    {session.username}
                </span>
            </Tooltip>
            <Tooltip title={i18n.t("account.logout")} mouseEnterDelay={0.2}>
                <button
                    type="button"
                    className={cn(
                        "inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-md transition-colors hover:bg-black/5 dark:hover:bg-white/10 [&_svg]:size-4",
                        className,
                    )}
                    style={iconStyle}
                    onClick={logout}
                    aria-label={i18n.t("account.logout")}
                >
                    <LogOut className="size-4" />
                </button>
            </Tooltip>
        </>
    );
}
