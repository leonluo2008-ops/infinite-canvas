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
import { router } from "@/router";

/**
 * 启动门（S2-B3.2）：先 await 登录态 + 服务端渠道，再渲染。
 *
 * zustand persist 的 merge 是**同步**函数（store 创建时就把 localStorage 里的本地渠道灌进内存），
 * 而登录态/服务端渠道只能网络拿 —— 网络结果回来之前渲染，首帧必然是「本地渠道 + local 模式」，
 * 随后被服务端数据覆盖（V12 说的闪现）。这里把 render 放到 await 之后，首帧即最终模式。
 *
 * 单人模式（MULTI_USER=0）由 bootstrapChannelSync 内部判定并直接放行：
 * 只多一次 /api/auth/me 的 404 探测，store 与请求层行为与基线一致。
 */
async function main() {
    initAnalytics();
    await bootstrapChannelSync();

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
