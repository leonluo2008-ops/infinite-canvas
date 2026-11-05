# pipeline/ —— 通用管线脚本

不含任何具体产品。所有产品参数从 `projects/<id>/product.json` 读。

| 目录 | 作用 |
|---|---|
| `reverse/dissect.py` | 参考视频确定性拆解（ffprobe + 场景检测 + 逐秒帧差 + 音频 RMS）→ `tech.json` |
| `reverse/render_prompt.py` | `spec.json` + `product.json` → 万相提示词草稿 |
| `reverse/extract_assets.py` | 从参考视频抽帧、选帧、生成素材清单 |
| `reverse/gen_image.py` | 图片生成（默认 dry-run，`--execute` 需 `--gate`） |
| `gen/wan_video.py` | **视频提交入口**（计划 / 预演 / 提交 / 等待，花钱要 `--confirm`） |
| `gen/wan_submit.py` | **提交节点**：平台约束、`@图片N` 前缀补齐、回显核对、令牌、提交/等待 |
| `reverse/gate.py` | 生成前审计门禁（15 项），全 PASS 才签 token |
| `reverse/prompt_lint.py` | 结构/词表/负向减法体检 |
| `reverse/delogo_local.py` / `local_inpaint.py` | 去水印 / 擦除局部（cv2） |

> 旧规则库工具链 `basis/`（`build_basis.py` / `check_basis.py`）与 `library/`（`build_library.py`）已于 2026-10-29 由所有者裁定整体删除，重建后另立版本。

品牌资产类脚本（字标提取、lockup、叠片）属于**项目级**，放在 `projects/<id>/tools/`。
