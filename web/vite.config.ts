import { readdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

import { parseChangelog } from "./src/lib/release";

const webDir = dirname(fileURLToPath(import.meta.url));
const localVersion = readFileSync(resolve(webDir, "../VERSION"), "utf8").trim() || "dev";
const localChangelog = readFileSync(resolve(webDir, "../CHANGELOG.md"), "utf8");

// Expose /plugins/index.json with local plugin files from public/plugins.
// The frontend can discover and list them when enabled; development reads the directory live, while builds emit a static registry.
function localPluginsManifest(): Plugin {
    const pluginsDir = resolve(webDir, "public/plugins");
    const listLocalPlugins = () => {
        try {
            return readdirSync(pluginsDir)
                .filter((file) => file.endsWith(".js"))
                .sort()
                .map((file) => `/plugins/${file}`);
        } catch {
            return [];
        }
    };
    return {
        name: "local-plugins-manifest",
        configureServer(server) {
            server.middlewares.use("/plugins/index.json", (_req, res) => {
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify(listLocalPlugins()));
            });
        },
        generateBundle() {
            this.emitFile({ type: "asset", fileName: "plugins/index.json", source: JSON.stringify(listLocalPlugins()) });
        },
    };
}

// 画布是唯一入口（2026-11-05 起）：`npm run dev` 起画布时自动拉起 wan-repro bridge
// （拉片/上传/万相视频生成的后端，ui/server.py :8779）。已活则复用不重启；vite 退出不杀 bridge。
// bridge 仓库定位：WANREPRO_ROOT 环境变量优先，否则试本机默认路径。
function autoStartBridge(): Plugin {
    const bridgePort = 8779;
    const probe = async () => {
        try {
            const res = await fetch(`http://127.0.0.1:${bridgePort}/api/tasks`);
            return res.ok;
        } catch {
            return false;
        }
    };
    return {
        name: "auto-start-wanrepro-bridge",
        configureServer(server) {
            void (async () => {
                if (await probe()) {
                    server.config.logger.info(`[bridge] :${bridgePort} 已在跑，复用`);
                    return;
                }
                const candidates = [
                    process.env.WANREPRO_ROOT,
                    "E:/OneDrive/工作文件夹/电商视频-廖总",
                ].filter(Boolean) as string[];
                const root = candidates.find((dir) => {
                    try {
                        return readFileSync(resolve(dir, "ui/server.py")) !== undefined;
                    } catch {
                        return false;
                    }
                });
                if (!root) {
                    server.config.logger.warn(`[bridge] 未找到 wan-repro 仓库（试过 ${candidates.join(", ")}）；设 WANREPRO_ROOT 后重启。画布的拉片/生成接口将不可用。`);
                    return;
                }
                const { spawn } = await import("node:child_process");
                for (const cmd of [["python3"], ["python"], ["py", "-3"]]) {
                    try {
                        const child = spawn(cmd[0], [...cmd.slice(1), "ui/server.py", "--port", String(bridgePort), "--no-browser"], {
                            cwd: root,
                            detached: true,
                            stdio: "ignore",
                            windowsHide: true,
                        });
                        child.unref();
                    } catch {
                        continue;
                    }
                    for (let i = 0; i < 15; i += 1) {
                        await new Promise((ok) => setTimeout(ok, 1000));
                        if (await probe()) {
                            server.config.logger.info(`[bridge] 已自动拉起 :${bridgePort}（${root}）`);
                            return;
                        }
                    }
                }
                server.config.logger.warn(`[bridge] 拉起失败（${root}）——手动跑：python3 ui/server.py --port ${bridgePort} --no-browser`);
            })();
        },
    };
}

export default defineConfig({
    base: process.env.VITE_BASE || "/",
    server: {
        // 公网同域部署（Cloudflare 隧道 wanrepro.aistar.work → vite :3000）：
        // 前端对 bridge（ui/server.py :8779）的请求一律走同域相对路径，由这里代理转发；
        // 开发机由 autoStartBridge 自动拉起 bridge，公网隧道形态同理（同域转发到 8779）。
        proxy: {
            "/api": "http://127.0.0.1:8779",
        },
        allowedHosts: ["wanrepro.aistar.work", "localhost", "127.0.0.1"],
    },
    plugins: [react(), localPluginsManifest(), autoStartBridge()],
    resolve: {
        alias: {
            "@": resolve(webDir, "src"),
        },
    },
    define: {
        __APP_VERSION__: JSON.stringify(localVersion),
        __APP_RELEASES__: JSON.stringify(parseChangelog(localChangelog)),
    },
});
