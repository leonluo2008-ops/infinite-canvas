import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import assert from "node:assert/strict";
import test from "node:test";

import { collectPaidActions } from "./paid-actions.js";
import { toolDescriptions, toolInputSchemas, toolNames } from "./schemas.js";
import { CanvasSession } from "./session.js";

const WORKFLOW_TOOLS = ["extract_workflow_template", "run_workflow_batch", "run_workflow_instance"] as const;

test("P9 三工具已注册：名称/入参 schema/中文描述齐全（注册面不漏）", () => {
    for (const name of WORKFLOW_TOOLS) {
        assert.ok((toolNames as readonly string[]).includes(name), `${name} 不在 toolNames`);
        assert.ok(toolInputSchemas[name], `${name} 没有入参 schema`);
        const description = toolDescriptions[name];
        assert.ok(description && description.length > 40, `${name} 描述过短`);
        // 卡面要求 5：描述里必须写明「终点=待审批 + 付费视频生成不在本工具内发生」
        assert.match(description, /待审批/, `${name} 描述缺「待审批」口径`);
        if (name !== "extract_workflow_template") assert.match(description, /付费视频生成不在本工具内发生/, `${name} 描述缺付费口径`);
    }
});

test("P9 extract_workflow_template 走页面通道（当前激活网页执行并回执）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    const second = connect(session, "second");
    t.after(() => {
        first.close();
        second.close();
    });
    session.activateClient("second");

    const result = session.callTool("extract_workflow_template", { sourceNodeIds: ["v1", "lap-1"], name: "夹具模板" });
    const call = second.event("tool_call");
    assert.equal(first.event("tool_call"), undefined);
    assert.equal(field(call, "name"), "extract_workflow_template");
    assert.deepEqual(field(call, "input"), { sourceNodeIds: ["v1", "lap-1"], name: "夹具模板" });

    const pageResult = { ok: true, templateId: "wabc123", nodeCount: 2, slots: [], externalReferences: [], frozenOk: true };
    session.resolveResult("second", { requestId: String(field(call, "requestId")), result: pageResult });
    assert.deepEqual(await result, pageResult);
});

test("P9 run_workflow_batch 入参经 zod 校验后原样下发（素材组不被改写）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const input = { templateId: "wabc123", materials: [{ sourceVideoNodeId: "v1", title: "素材一" }, { storageKey: "video:k2", optionalSegments: "A" }], concurrency: 2 };
    const result = session.callTool("run_workflow_batch", input);
    const call = first.event("tool_call");
    assert.equal(field(call, "name"), "run_workflow_batch");
    assert.deepEqual(field(call, "input"), input);

    const pageResult = { ok: true, batchId: "batch-1", instances: [{ instanceId: "i1", status: "awaiting_approval" }], paidVideoCreations: 0 };
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: pageResult });
    assert.deepEqual(await result, pageResult);
});

test("P9 入参校验拒绝空素材/缺 material（不把坏调用下发给页面）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    await assert.rejects(() => session.callTool("run_workflow_batch", { materials: [] }));
    await assert.rejects(() => session.callTool("run_workflow_instance", {}));
    await assert.rejects(() => session.callTool("run_workflow_instance", { material: "not-an-object" }));
    // 校验失败必须在页面侧零下发（页面没收到任何 tool_call）
    assert.equal(first.event("tool_call"), undefined);
});

test("P9 三工具不触发付费确认队列（付费闸不被绕过，也不会被误挂起）", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const state = { projectId: "p1", title: "p1", nodes: [{ id: "v1", type: "video", position: { x: 0, y: 0 }, width: 100, height: 100, metadata: { generationMode: "video", count: 4 } }], connections: [] } as never;
    assert.deepEqual(collectPaidActions("extract_workflow_template", { sourceNodeIds: ["v1"] }, state), []);
    assert.deepEqual(collectPaidActions("run_workflow_batch", { materials: [{ sourceVideoNodeId: "v1" }], concurrency: 3 }, state), []);
    assert.deepEqual(collectPaidActions("run_workflow_instance", { material: { sourceVideoNodeId: "v1" } }, state), []);

    const pending = session.callTool("run_workflow_batch", { materials: [{ sourceVideoNodeId: "v1" }] });
    const call = first.event("tool_call");
    assert.equal(first.event("paid_confirmation"), undefined, "付费工具才会走 paid_confirmation，本工具不该挂起");
    session.resolveResult("first", { requestId: String(field(call, "requestId")), result: { ok: true } });
    assert.deepEqual(await pending, { ok: true });
});

test("既有画布工具零回归：read_node_text / generation_get_status 仍走页面通道", async (t) => {
    const session = new CanvasSession();
    const first = connect(session, "first");
    t.after(() => first.close());
    session.activateClient("first");

    const text = session.callTool("read_node_text", { nodeId: "text-1" });
    const textCall = first.event("tool_call");
    assert.equal(field(textCall, "name"), "read_node_text");
    session.resolveResult("first", { requestId: String(field(textCall, "requestId")), result: { nodeId: "text-1", length: 3 } });
    assert.deepEqual(await text, { nodeId: "text-1", length: 3 });

    const status = session.callTool("generation_get_status", { scope: "canvas" });
    const statusCall = first.events("tool_call")[1];
    assert.equal(field(statusCall, "name"), "generation_get_status");
    session.resolveResult("first", { requestId: String(field(statusCall, "requestId")), result: { total: 0, tasks: [] } });
    assert.deepEqual(await status, { total: 0, tasks: [] });
});

/** 连接一个模拟网页客户端。 */
function connect(session: CanvasSession, clientId: string) {
    const response = new FakeSseResponse();
    session.openEvents(new URL(`http://127.0.0.1/events?clientId=${clientId}`), response as unknown as ServerResponse, "");
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

    event(type: string) {
        return this.events(type)[0];
    }

    events(type: string) {
        return this.chunks.flatMap((chunk) => {
            if (!chunk.startsWith(`event: ${type}\n`)) return [];
            const data = chunk.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
            return data ? [JSON.parse(data) as Record<string, unknown>] : [];
        });
    }

    /** 触发连接关闭事件（与 session.test.ts 的假响应同形）。 */
    close() {
        this.emit("close");
    }
}
