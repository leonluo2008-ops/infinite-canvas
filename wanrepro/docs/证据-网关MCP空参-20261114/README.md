# 证据 · 网关侧 MCP 空参报错归档（2026-11-14）

## 结论
反馈视频中的报错 `MCP 调用的参数必须是一个纯对象: <nil>。例如参数为 solxRun 时，参数应该是一个纯对象类似 {'solxRun': 123}`（逐字文案见 反馈视频分析-Gemini.md §四-2）：
- Go 风格 `<nil>` 格式化 + 「执行规划工件失败」措辞在 infinite-canvas 仓全源码 grep 无命中（2026-11-14 实测：web/src、wanrepro、canvas-agent、canvas-proxy 均无）。
- 根因在连接的 agent 后端（Hermes 网关，17371）执行规划工件时向 MCP 工具传了空参数。**本仓不可修**，此目录仅归档证据供网关侧排查移交。

## 一手件
- 反馈视频：E:/User/Videos/OBS-Video/2026-11-14 18-09-34.mp4（67.6s / 11.84MB，勿删——原始证据）
- 报错出现时间窗：00:34-00:50（右侧面板红字，含「创建产品宣传视频工作区节点 失败」「生成宣传视频 失败」）
- 视频逐帧 + faster-whisper 本地转写：C:/Users/leon3/AppData/Local/hermes/cache/scratch/fb2/（24h 清理目录，临时参考）
- Gemini 3.8 Flash 整片分析：本目录 反馈视频分析-Gemini.md

## 关联
- 方案：docs/方案-生成失败弹窗根治-20261114.md（卡 C）
- 同源但另案：Codex 对话框「允许初始化对话」阻塞态 = 热更新打断 MCP 连接（docs/交接-画布调试-20261114.md §一）
