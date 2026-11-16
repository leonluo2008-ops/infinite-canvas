import React from "react";
import { createRoot } from "react-dom/client";
import "antd/dist/reset.css";
import "streamdown/styles.css";
import "./styles/globals.css";
import { RouterProvider } from "react-router-dom";

import { AppProviders } from "@/components/layout/app-providers";
import "@/i18n";
import { initAnalytics } from "@/lib/analytics";
import { bootstrapChannelSync } from "@/services/channel-sync";
import { bootstrapCanvasProjectSync, BOOTSTRAP_TIMEOUT_MS } from "@/services/canvas-project-sync";
import { router } from "@/router";

/**
 * 启动门（S2-B3.2 + S3-B2）：先 await 登录态 + 服务端渠道 + 画布项目，再渲染。
 *
 * zustand persist 的 merge 是**同步**函数（store 创建时就把 localStorage/IndexedDB 里的本地数据灌进内存），
 * 而登录态/服务端数据只能网络拿 —— 网络结果回来之前渲染，首帧必然是「本地陈旧数据 + local 模式」，
 * 随后被服务端数据覆盖（V12 / V5 说的闪现）。这里把 render 放到 await 之后，首帧即最终状态。
 *
 * S3-B2 §2.3：两个启动门**并行**（`Promise.all`，不是串行相加），共享同一个 **8s 总预算**。
 * 超时不退「本地模式」（那是 BL-2 的丢失路径），而是进入离线态：本地缓存可写 + dirty 补推 + 顶栏提示。
 *
 * 单人模式（MULTI_USER=0）由两个启动门内部判定并直接放行：只多两次 `/api/auth/me` 的 404 探测，
 * store 与落盘行为与基线逐字节一致。
 */
async function main() {
    initAnalytics();
    const deadline = Date.now() + BOOTSTRAP_TIMEOUT_MS;
    await Promise.all([
        bootstrapChannelSync({ timeoutMs: BOOTSTRAP_TIMEOUT_MS }).catch((error) => {
            // 渠道门内部已兜住自己的失败；这里再兜一层，绝不让画布门因为渠道门异常而拿不到渲染机会
            console.warn("[main] 渠道启动门异常（画布门照常放行）", error);
        }),
        bootstrapCanvasProjectSync({ deadline }).catch((error) => {
            console.warn("[main] 画布启动门异常", error);
        }),
    ]);

    document.body.style.fontFamily = '"SF Pro Display","SF Pro Text","PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif';

    createRoot(document.getElementById("root")!).render(
        <React.StrictMode>
            <AppProviders>
                <RouterProvider router={router} />
            </AppProviders>
        </React.StrictMode>,
    );
}

void main();
