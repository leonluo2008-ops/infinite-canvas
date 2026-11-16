/**
 * S2.5（B1.2）：登录态角色（is_admin）—— 模块级状态，**不进 zustand store**（不动 store 结构）。
 *
 * 为什么要这个模块：Agent 入口只对管理员开放（`/api/auth/me` 的 is_admin），而现有
 * `channel-sync.ts` 的 `probeSession` 只带回 username（该文件属请求层红线，不改）。
 * 所以这里做一次同源、只读的 `/api/auth/me` 探活，把 is_admin 存成模块级快照，
 * 供「顶栏 Agent 按钮 / 画布工具栏 Agent 按钮 / Agent 面板」三处门控消费。
 *
 * 四态口径（与 channel-sync 的会话探针一致，只多一维 admin）：
 *  * `2xx` + `is_admin:true`  → `admin`
 *  * `2xx` + `is_admin:false` → `user`
 *  * `401`                    → `logged-out`（MULTI_USER=1 未登录）
 *  * 其它（`404` 等）         → `single-user`（MULTI_USER=0 单人模式，零改动基线）
 *  * 请求异常/未完成          → `unknown`（绝不因为探活失败改变单人模式 UI）
 */
import { useSyncExternalStore } from "react";

import { useConfigStore } from "@/stores/use-config-store";

export type SessionRole = "unknown" | "single-user" | "logged-out" | "user" | "admin";

export type SessionRoleSnapshot = { role: SessionRole; username: string };

const AUTH_ME_ENDPOINT = "/api/auth/me";

let snapshot: SessionRoleSnapshot = { role: "unknown", username: "" };
const listeners = new Set<() => void>();
let inflight: Promise<SessionRoleSnapshot> | null = null;

export function getSessionRoleSnapshot(): SessionRoleSnapshot {
    return snapshot;
}

export function subscribeSessionRole(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

function publish(next: SessionRoleSnapshot) {
    if (next.role === snapshot.role && next.username === snapshot.username) return;
    snapshot = next;
    for (const listener of Array.from(listeners)) listener();
}

/** 探活响应 → 角色（纯函数；探针直接断言四态映射，无需起浏览器）。 */
export function roleFromMe(status: number, payload: { username?: unknown; is_admin?: unknown } | null): SessionRole {
    if (status >= 200 && status < 300) {
        if (!payload || typeof payload.username !== "string" || !payload.username) return "single-user";
        return payload.is_admin === true ? "admin" : "user";
    }
    return status === 401 ? "logged-out" : "single-user";
}

/**
 * 唯一入口：读一次 `/api/auth/me` 并写入模块级快照。
 * 同一个进行中的请求会被复用（三处门控同时挂载也只发一次）。
 */
export function probeSessionRole(fetchImpl: typeof fetch = fetch): Promise<SessionRoleSnapshot> {
    if (inflight) return inflight;
    inflight = (async () => {
        try {
            const response = await fetchImpl(AUTH_ME_ENDPOINT, { headers: { Accept: "application/json" }, credentials: "same-origin" });
            const payload = response.ok ? ((await response.json().catch(() => null)) as { username?: unknown; is_admin?: unknown } | null) : null;
            const next: SessionRoleSnapshot = {
                role: roleFromMe(response.status, payload),
                username: typeof payload?.username === "string" ? payload.username : "",
            };
            publish(next);
            return next;
        } catch {
            // 探活失败 = 不知道 → 保持 unknown（单人模式基线：Agent 入口照旧可见）
            return snapshot;
        } finally {
            inflight = null;
        }
    })();
    return inflight;
}

/** 探针/登出清理用：把状态打回未探活。业务代码不需要调用。 */
export function resetSessionRole() {
    inflight = null;
    publish({ role: "unknown", username: "" });
}

/**
 * Agent 入口可见性（B1.2 / V1-V4）。
 *
 *  * `admin`                 → 可见（owner 自用通道）
 *  * `user` / `logged-out`   → 不可见（按钮不渲染，而不是渲染后禁用 → V2）
 *  * `single-user`           → 可见（单人模式是原有功能，V4 对照基线）
 *  * `unknown` + 非 remote    → 可见（探活未完成时不得让单人模式按钮闪没 → V4）
 *  * `unknown` + remote       → 不可见（已登录但 is_admin 未定：宁可晚一点出现，也不要先露后藏）
 */
export function agentEntryVisible(role: SessionRole, remoteMode: boolean): boolean {
    if (role === "admin") return true;
    if (role === "user" || role === "logged-out") return false;
    return !remoteMode;
}

/**
 * 本地代理锁（B1.1 / V5-V6）。
 * remote（已登录，服务端代转发接管）→ 锁定；local（单人/未登录）→ 一律 false，逐字节保持基线。
 */
export function localProxyLocked(remoteMode: boolean, role: SessionRole): boolean {
    if (role === "user" || role === "admin") return true;
    return remoteMode;
}

export function useSessionRole(): SessionRole {
    // 第三参（server snapshot）返回当前快照而不是常量：本项目是纯 CSR，
    // 常量会让「探活已完成的角色」在 SSR/探针渲染里看不到，反而制造假阴性。
    return useSyncExternalStore(subscribeSessionRole, () => snapshot.role, () => snapshot.role);
}

/** remote 语义直接取既有 `channelMode === "remote"`（S2-B3 已把它定为服务端模式标记，不新建字段）。 */
function useRemoteChannelMode(): boolean {
    return useConfigStore((state) => state.config.channelMode === "remote");
}

/** Agent 入口三处门控统一消费的 hook（顶栏按钮 / 画布工具栏按钮 / Agent 面板）。 */
export function useAgentEntryVisible(): boolean {
    return agentEntryVisible(useSessionRole(), useRemoteChannelMode());
}

/** 设置弹窗「本地代理」tab 的门控 hook。 */
export function useLocalProxyLocked(): boolean {
    return localProxyLocked(useRemoteChannelMode(), useSessionRole());
}

/**
 * 模块加载即探活（不等组件挂载）：main 的启动门本来就要 await `/api/auth/me`，
 * 这样两边并行，首帧渲染时 is_admin 基本已就绪，避免「先渲染后隐藏」的按钮闪现。
 */
if (typeof window !== "undefined") void probeSessionRole();
