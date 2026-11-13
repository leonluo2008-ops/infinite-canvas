import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import assert from "node:assert/strict";
import test from "node:test";

import { CanvasSession } from "./session.js";

test("付费闸：未确认时零次下发，确认只由服务端闸门决定", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "video", model: "wan-2.2" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "video" });
    const confirmation = page.event("paid_confirmation");
    assert.equal(field(confirmation, "name"), "canvas_apply_ops");
    const actions = field(confirmation, "actions") as Array<Record<string, unknown>>;
    assert.equal(actions.length, 1);
    assert.equal(actions[0].nodeId, "config-1");
    assert.equal(actions[0].mode, "video");
    assert.equal(actions[0].channel, "wan-2.2");
    assert.equal(actions[0].estimate, null);
    // 未确认 → 页面拿不到可执行 ops、也没有 tool_call：视频创建调用次数=0。
    assert.equal(field(confirmation, "input"), undefined);
    assert.deepEqual(page.events("tool_call"), []);

    session.confirmPaidActions("first", { requestId: String(field(confirmation, "requestId")), approvedActionIds: [] });
    await assert.rejects(call, /未执行/);
    assert.deepEqual(page.events("tool_call"), []);
});

test("付费闸：确认 1 次恰好下发 1 次生成", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "video" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "video" });
    const confirmation = page.event("paid_confirmation");
    const requestId = String(field(confirmation, "requestId"));
    session.ackToolRequest("first", { requestId });

    const decision = session.confirmPaidActions("first", { requestId, approvedActionIds: ["run_generation:0"] });
    assert.equal(decision.executed, true);
    // 页面只有在确认回执里才拿到 ops：确认 1 次 → 恰好 1 条 run_generation。
    const ops = field(decision.input, "ops") as Array<Record<string, unknown>>;
    assert.equal(ops.filter((op) => op.type === "run_generation").length, 1);
    assert.deepEqual(page.events("tool_call"), []);

    session.resolveResult("first", { requestId, result: { projectId: "canvas-paid" } });
    const result = (await call) as Record<string, unknown>;
    assert.equal(field(result, "projectId"), "canvas-paid");
    const paid = field(result, "paidActions") as Record<string, unknown>;
    assert.equal((paid.executed as unknown[]).length, 1);
    assert.equal(paid.estimate, null);
});

test("付费闸：一包 3 个 run_generation 逐条确认，被拒项零调用", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "image" }, { id: "config-2", mode: "video" }, { id: "config-3", mode: "image", count: 4 }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_apply_ops", {
        ops: [
            { type: "update_node", id: "config-1", metadata: { composerContent: "新提示词" } },
            { type: "run_generation", nodeId: "config-1", mode: "image" },
            { type: "run_generation", nodeId: "config-2", mode: "video" },
            { type: "run_generation", nodeId: "config-3", mode: "image" },
        ],
    });
    const confirmation = page.event("paid_confirmation");
    const actions = field(confirmation, "actions") as Array<Record<string, unknown>>;
    assert.equal(actions.length, 3);
    assert.deepEqual(actions.map((action) => action.nodeId), ["config-1", "config-2", "config-3"]);
    assert.deepEqual(actions.map((action) => action.count), [1, 1, 4]);
    assert.deepEqual(page.events("tool_call"), []);

    const requestId = String(field(confirmation, "requestId"));
    session.ackToolRequest("first", { requestId });
    const decision = session.confirmPaidActions("first", { requestId, approvedActionIds: ["run_generation:2", "run_generation:3"] });
    const ops = field(decision.input, "ops") as Array<Record<string, unknown>>;
    const generationNodeIds = ops.filter((op) => op.type === "run_generation").map((op) => op.nodeId);
    // 只放行勾选的 2、3 号；1 号被拒 → 零调用；非付费 update_node 原样保留。
    assert.deepEqual(generationNodeIds, ["config-2", "config-3"]);
    assert.equal(ops.some((op) => op.type === "update_node"), true);
    assert.deepEqual((decision.rejected as Array<Record<string, unknown>>).map((action) => action.nodeId), ["config-1"]);

    session.resolveResult("first", { requestId, result: { projectId: "canvas-paid" } });
    await call;
});

test("付费闸：确认挂起不计超时，放行后单态执行", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 30 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "video" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "video" });
    const confirmation = page.event("paid_confirmation");
    const requestId = String(field(confirmation, "requestId"));
    assert.equal(session.ackToolRequest("first", { requestId }), true);

    const settled = trackCall(call);
    await delay(80);
    // 挂起超过回执超时后：既不失败（无 Agent 视角失败），也不下发（无页面执行），不存在双态。
    assert.deepEqual(settled, []);
    assert.deepEqual(page.events("tool_call"), []);
    assert.deepEqual(page.events("tool_cancel"), []);

    const decision = session.confirmPaidActions("first", { requestId, approvedActionIds: ["run_generation:0"] });
    assert.equal(decision.executed, true);
    assert.deepEqual(settled, []);
    session.resolveResult("first", { requestId, result: { ok: true } });
    await call;
    assert.deepEqual(settled, ["resolved"]);
});

test("付费闸：页面未回执即超时，撤销页面动作且不自动重试", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 20 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "video" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "video" });
    const confirmation = page.event("paid_confirmation");
    const requestId = String(field(confirmation, "requestId"));
    await assert.rejects(call, /画布操作超时/);
    // 超时同时向页面发撤销信号，页面收到后不得再执行。
    assert.equal(page.events("tool_cancel").length, 1);
    assert.deepEqual(page.events("tool_call"), []);
    // 过期后再确认：拒绝受理，零下发。
    assert.equal(session.confirmPaidActions("first", { requestId, approvedActionIds: ["run_generation:0"] }).ok, false);
    await delay(40);
    assert.deepEqual(page.events("tool_call"), []);
});

test("付费闸：取消后零自动重发", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "image" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "image" });
    const confirmation = page.event("paid_confirmation");
    const requestId = String(field(confirmation, "requestId"));
    session.ackToolRequest("first", { requestId });
    const decision = session.confirmPaidActions("first", { requestId, approvedActionIds: [] });
    assert.equal(decision.executed, false);
    await assert.rejects(call, /未执行/);
    await delay(40);
    assert.deepEqual(page.events("tool_call"), []);
    assert.equal(page.events("paid_confirmation").length, 1);
});

test("付费闸：未获放行前页面回传的执行结果被拒收", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-paid", [{ id: "config-1", mode: "video" }]), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_run_generation", { nodeId: "config-1", mode: "video" });
    const confirmation = page.event("paid_confirmation");
    const requestId = String(field(confirmation, "requestId"));
    assert.equal(session.resolveResult("first", { requestId, result: { ok: true } }), false);

    session.ackToolRequest("first", { requestId });
    session.confirmPaidActions("first", { requestId, approvedActionIds: ["run_generation:0"] });
    assert.equal(session.resolveResult("first", { requestId, result: { ok: true } }), true);
    await call;
});

test("付费闸：工作台生视频工具同样进入确认队列", async (t) => {
    const session = new CanvasSession("", { paidAckTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.activateClient("first");

    const call = session.callTool("workbench_video_generate", { prompt: "卖点演示" }).catch(() => undefined);
    const confirmation = page.event("paid_confirmation");
    assert.equal(field(confirmation, "name"), "workbench_video_generate");
    const actions = field(confirmation, "actions") as Array<Record<string, unknown>>;
    assert.equal(actions.length, 1);
    assert.equal(actions[0].mode, "video");
    assert.deepEqual(page.events("tool_call"), []);
    session.confirmPaidActions("first", { requestId: String(field(confirmation, "requestId")), approvedActionIds: [] });
    await call;
});

test("非付费写工具语义不变：直接下发，不进入确认队列", async (t) => {
    const session = new CanvasSession("", { canvasToolTimeoutMs: 1000 });
    const page = connect(session, "first");
    t.after(() => page.close());
    session.updateState(paidSnapshot("canvas-plain", []), "first");
    session.activateClient("first");

    const call = session.callTool("canvas_create_text_node", { text: "无付费" });
    assert.deepEqual(page.events("paid_confirmation"), []);
    const dispatched = page.events("tool_call");
    assert.equal(dispatched.length, 1);
    assert.equal(field(dispatched[0], "name"), "canvas_apply_ops");
    session.resolveResult("first", { requestId: String(field(dispatched[0], "requestId")), result: { ok: true } });
    assert.deepEqual(await call, { ok: true });
});

/** 构造含生成配置节点的画布快照（付费闸探针用）。 */
function paidSnapshot(projectId: string, nodes: Array<{ id: string; mode: "text" | "image" | "video" | "audio"; model?: string; count?: number }>) {
    return {
        projectId,
        title: projectId,
        nodes: nodes.map((node) => ({ id: node.id, type: "config" as const, title: node.id, position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { generationMode: node.mode, composerContent: `提示词-${node.id}`, model: node.model, count: node.count } })),
        connections: [],
        selectedNodeIds: [],
        viewport: { x: 0, y: 0, k: 1 },
    };
}

/** 收集一次画布调用的终态，用于断言「是否已结束」。 */
function trackCall(promise: Promise<unknown>) {
    const settled: string[] = [];
    void promise.then(() => settled.push("resolved"), (error: Error) => settled.push(`rejected:${error.message}`));
    return settled;
}

function delay(ms: number) {
    return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

/** 创建用于测试的画布 SSE 连接。 */
function connect(session: CanvasSession, clientId: string, activeThreadId = "") {
    const response = new FakeSseResponse();
    session.openEvents(new URL(`http://127.0.0.1/events?clientId=${clientId}`), response as unknown as ServerResponse, activeThreadId);
    return response;
}

/** 安全读取测试对象字段。 */
function field(value: unknown, key: string) {
    return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

/** 模拟 Node SSE 响应并提供事件读取能力。 */
class FakeSseResponse extends EventEmitter {
    private chunks: string[] = [];

    writeHead() {
        return this;
    }

    write(chunk: string) {
        this.chunks.push(chunk);
        return true;
    }

    /** 读取指定类型的首个 SSE 事件数据。 */
    event(type: string) {
        return this.events(type)[0];
    }

    /** 读取指定类型的全部 SSE 事件数据。 */
    events(type: string) {
        return this.chunks.flatMap((chunk) => {
            if (!chunk.startsWith(`event: ${type}\n`)) return [];
            const data = chunk.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
            return data ? [JSON.parse(data) as unknown] : [];
        });
    }

    close() {
        this.emit("close");
    }
}
