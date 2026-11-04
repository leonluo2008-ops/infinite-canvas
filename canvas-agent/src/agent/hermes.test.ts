import assert from "node:assert/strict";
import test from "node:test";

import { hermesChatArgs, normalizeHermesToolName, parseHermesStreamLine } from "./hermes.js";
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

test("hermes 的能力申报：无原生线程、无 model/list、可中断、可续跑", async () => {
    const provider = await loadAgentProvider("hermes");
    assert.ok(provider);
    assert.deepEqual(provider.capabilities, { session: false, threads: false, models: false, attachments: false, approvals: false, interrupt: true, resume: true });
    const models = await provider.listModels();
    assert.equal(models.models.length, 1);
    assert.equal(models.models[0].isDefault, true);
});
