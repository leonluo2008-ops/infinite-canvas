import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { appendHermesAttachmentRefs, appendHermesCanvasReferences, buildHermesPrompt, hermesChatArgs, hermesImageFlagRejected, normalizeHermesToolName, parseHermesStreamLine, removeHermesAttachmentFiles, writeHermesAttachmentFiles } from "./hermes.js";
import type { AgentCanvasReference } from "./types.js";
import { loadAgentProvider } from "./provider.js";

test("hermes stream-json 的 init/text/tool/result 四类行都有归一结果", () => {
    assert.deepEqual(parseHermesStreamLine('{"type":"system","subtype":"init","model":"","session_id":"","timestamp":1}'), { kind: "init", sessionId: "", model: "" });
    assert.deepEqual(parseHermesStreamLine('{"type":"text","text":"OK","timestamp":2}'), { kind: "text", text: "OK" });
    assert.deepEqual(parseHermesStreamLine('{"type":"tool_use","name":"mcp__infinite-canvas__canvas_create_text_node","tool_call_id":"call-1","input":{"text":"hello"}}'), {
        kind: "tool_use",
        name: "mcp__infinite-canvas__canvas_create_text_node",
        toolCallId: "call-1",
        input: { text: "hello" },
    });
    assert.deepEqual(parseHermesStreamLine('{"type":"tool_result","name":"canvas_create_text_node","tool_call_id":"call-1","output":"{\\"ok\\":true}","duration_ms":12,"is_error":false}'), {
        kind: "tool_result",
        name: "canvas_create_text_node",
        toolCallId: "call-1",
        output: '{"ok":true}',
        isError: false,
        durationMs: 12,
    });
    assert.deepEqual(parseHermesStreamLine('{"type":"result","session_id":"local-abc","exit_code":0,"text":"OK","tokens":{"input":22642,"output":3,"total":22645,"cache_read":666,"cache_write":0},"duration_ms":5073}'), {
        kind: "result",
        sessionId: "local-abc",
        exitCode: 0,
        text: "OK",
        tokens: { input: 22642, output: 3, total: 22645, cacheRead: 666, cacheWrite: 0 },
        durationMs: 5073,
    });
});

test("非 JSON 诊断行不丢，按 raw 交给日志", () => {
    assert.deepEqual(parseHermesStreamLine("Session: local-95b1"), { kind: "raw", text: "Session: local-95b1" });
    assert.equal(parseHermesStreamLine("   "), null);
    assert.equal(parseHermesStreamLine('{"type":"system","subtype":"other"}'), null);
});

test("result 的 error 字段会带到归一结果上，供 turn.completed 判失败", () => {
    const line = parseHermesStreamLine('{"type":"result","session_id":"local-abc","exit_code":1,"text":"","tokens":{},"error":"model overloaded"}');
    assert.equal(line?.kind, "result");
    assert.equal(line && line.kind === "result" ? line.error : "", "model overloaded");
    assert.equal(line && line.kind === "result" ? line.exitCode : -1, 1);
});

test("MCP 工具名还原成画布工具名，非 MCP 工具原样保留", () => {
    assert.equal(normalizeHermesToolName("mcp__infinite-canvas__canvas_create_text_node"), "canvas_create_text_node");
    assert.equal(normalizeHermesToolName("mcp__other__do_thing"), "do_thing");
    assert.equal(normalizeHermesToolName("terminal"), "terminal");
});

test("多轮续跑才追加 --resume", () => {
    const first = hermesChatArgs({ prompt: "hello" });
    assert.deepEqual(first, ["chat", "-q", "hello", "--oneshot", "--format", "stream-json"]);
    const resumed = hermesChatArgs({ prompt: "again", sessionId: "local-abc", config: { model: "anthropic/claude-sonnet-4" } });
    assert.deepEqual(resumed, ["chat", "-q", "again", "--oneshot", "--format", "stream-json", "--resume", "local-abc", "--model", "anthropic/claude-sonnet-4"]);
});

test("provider 注册表能按 id 加载三家实现，未知 id 返回 null", async () => {
    assert.equal((await loadAgentProvider("hermes"))?.id, "hermes");
    assert.equal((await loadAgentProvider("codex"))?.id, "codex");
    assert.equal((await loadAgentProvider("claude"))?.id, "claude");
    assert.equal(await loadAgentProvider("nope"), null);
});

test("hermes 的能力申报：无原生线程、无 model/list、收图片附件、可中断、可续跑", async () => {
    const provider = await loadAgentProvider("hermes");
    assert.ok(provider);
    assert.deepEqual(provider.capabilities, { session: false, threads: false, models: false, attachments: true, approvals: false, interrupt: true, resume: true });
    const models = await provider.listModels();
    assert.equal(models.models.length, 1);
    assert.equal(models.models[0].isDefault, true);
});

test("一张图一个 --image，无附件时不追加任何参数", () => {
    assert.deepEqual(hermesChatArgs({ prompt: "看这两张", images: ["C:/tmp/a.png", "C:/tmp/b.jpg"] }), [
        "chat", "-q", "看这两张", "--oneshot", "--format", "stream-json", "--image", "C:/tmp/a.png", "--image", "C:/tmp/b.jpg",
    ]);
    const bare = hermesChatArgs({ prompt: "无图", sessionId: "local-abc", config: { model: "m" } });
    assert.equal(bare.includes("--image"), false);
    assert.deepEqual(bare, ["chat", "-q", "无图", "--oneshot", "--format", "stream-json", "--resume", "local-abc", "--model", "m"]);
});

test("dataUrl 落成本地临时文件：按 MIME 给扩展名，非图片附件跳过，收尾整目录删除", async () => {
    const png = Buffer.from("png-bytes").toString("base64");
    const jpeg = Buffer.from("jpeg-bytes").toString("base64");
    const written = await writeHermesAttachmentFiles([
        { name: "a", type: "image/png", dataUrl: `data:image/png;base64,${png}` },
        { name: "b", type: "image/jpeg", dataUrl: `data:image/jpeg;base64,${jpeg}` },
        { name: "note", type: "text/plain", dataUrl: "data:text/plain;base64,aGk=" },
        { name: "no-data" },
    ]);
    try {
        assert.equal(written.files.length, 2);
        assert.match(written.files[0], /image-1\.png$/);
        assert.match(written.files[1], /image-2\.jpg$/);
        assert.equal(fs.readFileSync(written.files[0], "utf8"), "png-bytes");
        assert.equal(fs.readFileSync(written.files[1], "utf8"), "jpeg-bytes");
        assert.ok(fs.existsSync(written.dir));
    } finally {
        await removeHermesAttachmentFiles(written.dir);
    }
    assert.equal(fs.existsSync(written.dir), false);
    assert.deepEqual(await writeHermesAttachmentFiles([]), { dir: "", files: [] });
});

test("没有 --image 通道时把本地路径写进提示词，交给 vision_analyze", () => {
    assert.equal(appendHermesAttachmentRefs("原prompt", []), "原prompt");
    const text = appendHermesAttachmentRefs("画一张图", [path.join("C:", "tmp", "image-1.png"), path.join("C:", "tmp", "image-2.jpg")]);
    assert.ok(text.startsWith("画一张图"));
    assert.match(text, /附带 2 张图片/);
    assert.ok(text.includes(path.join("C:", "tmp", "image-1.png")));
    assert.ok(text.includes(path.join("C:", "tmp", "image-2.jpg")));
    assert.match(text, /vision_analyze/);
});

test("网关拒绝 --image 的报错要能被认出来，否则不能当成可用", () => {
    assert.equal(hermesImageFlagRejected("Error: Unsupported gateway CLI options: --image. No local fallback or policy changes were made."), true);
    assert.equal(hermesImageFlagRejected("Image file not found: /tmp/x.png"), false);
    assert.equal(hermesImageFlagRejected(""), false);
});

test("@素材引用里的视频/音频按路径注入：localPath 优先，无则 previewUrl，图片与文本不掺进来", () => {
    const videoPath = path.join("E:", "uploads", "clip.mp4");
    const references: AgentCanvasReference[] = [
        { nodeId: "n1", label: "@视频1", title: "clip.mp4", kind: "video", localPath: videoPath, previewUrl: "https://cdn.example.com/clip.mp4" },
        { nodeId: "n2", label: "@音频1", title: "voice.mp3", kind: "audio", previewUrl: "https://cdn.example.com/voice.mp3" },
        { nodeId: "n3", label: "@图片1", title: "cover.png", kind: "image", localPath: "E:/uploads/cover.png", previewUrl: "https://cdn.example.com/cover.png" },
        { nodeId: "n4", label: "@文本1", title: "notes", kind: "text", text: "一些文字" },
        { nodeId: "n5", label: "@视频2", title: "missing.mp4", kind: "video" },
    ];
    assert.equal(appendHermesCanvasReferences("原prompt", []), "原prompt");
    const text = appendHermesCanvasReferences("看看这些素材", references);
    assert.ok(text.startsWith("看看这些素材"));
    assert.match(text, /本轮引用画布素材/);
    // localPath 优先：同一个素材不会同时出现远程地址。
    assert.ok(text.includes(`1. @视频1「clip.mp4」 · video · 本地路径：${videoPath}`));
    assert.equal(text.includes("https://cdn.example.com/clip.mp4"), false);
    // 无 localPath 时退到 previewUrl。
    assert.ok(text.includes("2. @音频1「voice.mp3」 · audio · 远程地址：https://cdn.example.com/voice.mp3"));
    // 图片走附件、文本已在正文，不进素材清单。
    assert.equal(text.includes("cover.png"), false);
    assert.equal(text.includes("@文本1"), false);
    // 路径缺失要说清楚，不能编一个地址。
    assert.ok(text.includes("3. @视频2「missing.mp4」 · video · 路径缺失"));
    // 工具指导必须点名。
    assert.match(text, /video_vision MCP/);
    assert.match(text, /analyze_video/);
    assert.match(text, /whisper/);
});

test("buildHermesPrompt 同时组装图片附件路径与视频素材引用，互不覆盖", () => {
    const imagePath = path.join("C:", "tmp", "infinite-canvas-hermes-x", "image-1.png");
    const videoPath = path.join("E:", "uploads", "clip.mp4");
    const prompt = buildHermesPrompt({
        prompt: "拆一下这条片子",
        attachmentFiles: [imagePath],
        references: [{ nodeId: "n1", label: "@视频1", title: "clip.mp4", kind: "video", localPath: videoPath }],
    });
    assert.ok(prompt.includes(imagePath));
    assert.match(prompt, /vision_analyze/);
    assert.ok(prompt.includes(videoPath));
    assert.match(prompt, /video_vision MCP/);
    // 只有引用、没有附件时也要能注入。
    assert.match(buildHermesPrompt({ prompt: "看视频", references: [{ nodeId: "n1", label: "@视频1", title: "a.mp4", kind: "video", previewUrl: "https://x/a.mp4" }] }), /本轮引用画布素材/);
});
