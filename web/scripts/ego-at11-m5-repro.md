# AT-11 M5 ego 复现脚本（合并确认窗 state 不重派生 → 确认合并发不出 POST）

> 给主代理 ego 复验用。**禁用真实任务 auto-0024**（只读）；全程用临时任务
> `ego-m5-<时间戳>`，验完删除（见 §4 清理）。
> 前置：wanrepro hub 服务已起（默认 127.0.0.1:8776，`py -3 ui/server.py`），前端 dev/preview 跑在 :3000。

## 0. 原理一句话

`MergeConfirmModal` 在 `AssetSection` 里**常驻挂载**（只靠 `open` 控显隐），useState 初值
只在首挂载算一次——首挂载时 `rows=[]` → `primary=0`。修复前点「确认合并」被 `!primary`
校验拦下，只弹小红字、不发 API（M4 ego 实测 3 秒后 sidecar 无组键）。修复后 `open` 变
true 时 useEffect 重派生初值。

## 1. 造临时测试任务（python 片段，一次性）

基于 pop-4 fixture（`analysis/reverse/pop-4/`）只读复制一份桥目录 + 手写最小 任务.json。
**不要碰 auto-0024 / auto-0012 等真实任务目录。**

```python
import json, shutil, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"E:/User/GitHub/infinite-canvas/wanrepro")
ts = time.strftime("%Y%m%d-%H%M%S")
tid = f"ego-m5-{ts}"
bridge = ROOT / "analysis" / "reverse" / tid
task_dir = ROOT / "automation" / "projects" / tid

# ① 桥目录：pop-4 的清单/sidecar/拆解/意图建议 只读复制
bridge.mkdir(parents=True)
for name in ("待确认清单.md", "待确认清单.json", "拆解.md", "意图建议.md"):
    shutil.copy2(ROOT / "analysis" / "reverse" / "pop-4" / name, bridge / name)

# ② 任务：awaiting_assets（合并操作的前置状态）
task_dir.mkdir(parents=True)
(task_dir / "任务.json").write_text(json.dumps({
    "local_id": tid,
    "bridge_id": tid,
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    "state": "awaiting_assets",
    "history": [],
    "intent": "AT-11 M5 ego 复现（临时，验完删）",
    "assets": [],
    "gen_params": {},
    "pending_replace": [],
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(tid)   # 记下这个 id，后面步骤全用它
```

## 2. 复现步骤（:3000 前端）

1. 打开 `http://localhost:3000`，进入自动化工作台，找到任务 `ego-m5-<ts>`，打开任务 drawer。
2. 在资产区点 **「合并资产」** 进入多选态（按钮带 `data-hub-merge-enter`）。
3. 多选任意 2 行（建议 #1 和 #5——pop-4 里它们同 URL，走「一张唯一图」分支）。
4. 浮条点 **「合并为一项」**（`data-hub-merge-submit`）→ 确认小窗弹出。
5. **检查窗内默认值**（M5 修复点）：
   - 主行 Select（`[data-hub-merge-primary]` 内）默认 = **行号最小的一行**（如 `#1`）；
   - 「合并后名称」输入框（`[data-hub-merge-name]`）默认 = 主行短名，**非空**。
6. 点 **「确认合并」**（`[data-hub-merge-confirm]`）。
7. 断言（全过才算 PASS）：
   - **无报错**：无红色 message.warning 弹窗（如「请选主行…」）、确认窗不残留小红字；
   - **3 秒内** GET `http://localhost:8776/api/hub/task/ego-m5-<ts>` 的
     `asset_cards[*].组`（或直接看盘上 `analysis/reverse/ego-m5-<ts>/待确认清单.json`）出现
     **『同实体确认』组键**，值形如 `[[1, 5]]`：

   ```bash
   sleep 3 && grep -A3 "同实体确认" "E:/User/GitHub/infinite-canvas/wanrepro/analysis/reverse/ego-m5-<ts>/待确认清单.json"
   ```

   期望看到 `"同实体确认": [[1, 5]]`（行号以你实际多选的为准）。

## 3. 失败排查点

- **Network 面板里 `POST /api/hub/task/ego-m5-<ts>/group` 是否发出？**
  - 没发出 + 小红字「请选主行…」 → state 重派生仍未生效（检查 useEffect 依赖/时机）；
  - 发出了但 4xx → 看响应体报错原文（如非 awaiting_assets、品牌行入组等校验）；
  - 发出了且 200 但 sidecar 无组键 → 查服务端 `hub_api.group_rows` 落盘路径。
- 确认窗默认值为空（主行 0 / 名称空） → useEffect 未在 open 后重派生。
- 别忘了硬刷新（Ctrl+Shift+R）排除旧 bundle 缓存。

## 4. 清理（验完必做）

```python
import json, shutil, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"E:/User/GitHub/infinite-canvas/wanrepro")
tid = "ego-m5-<ts>"   # 换成实际 id

# ① 先解组（把 sidecar 的『同实体确认』清掉，再删目录才不留半态）
import urllib.request
req = urllib.request.Request(
    f"http://localhost:8776/api/hub/task/{tid}/ungroup",
    data=json.dumps({"rows": [1, 5]}).encode(), method="POST",
    headers={"Content-Type": "application/json"})
print("ungroup:", json.loads(urllib.request.urlopen(req).read()))

# ② 删临时任务目录 + 专属桥目录
shutil.rmtree(ROOT / "automation" / "projects" / tid, ignore_errors=True)
shutil.rmtree(ROOT / "analysis" / "reverse" / tid, ignore_errors=True)
print("cleaned:", not (ROOT / "automation" / "projects" / tid).exists(),
      not (ROOT / "analysis" / "reverse" / tid).exists())
```

（也可走前端：先点「拆开」解组，再对任务做删除操作；盘上直接删目录与上述等价。）

## 5. 选择器速查

| 元素 | 选择器 |
|---|---|
| 进入合并多选态按钮 | `[data-hub-merge-enter]` |
| 浮条「合并为一项」按钮 | `[data-hub-merge-submit]` |
| 确认小窗「确认合并」按钮 | `[data-hub-merge-confirm]` |
| 主行 Select | `[data-hub-merge-primary]` |
| 合并后名称输入 | `[data-hub-merge-name]` |
