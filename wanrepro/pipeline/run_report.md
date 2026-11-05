# D4/D5 实际运行报告

最终版本按镜头单元输出；先前整片输出已覆盖。

## 生成命令

```bash
PYTHONIOENCODING=utf-8 py -3 'E:/OneDrive/工作文件夹/电商视频-廖总/pipeline/gen_prompt.py' --product 'E:/OneDrive/工作文件夹/电商视频-廖总/pipeline/products/pop_sku01.json' --mechanism '静物取走型' --platform tiktok --goal brand --variants 3 --out 'E:/OneDrive/工作文件夹/电商视频-廖总/prompts'
```

退出码：0

```text
WROTE E:/OneDrive/工作文件夹/电商视频-廖总/prompts/pop_sku01_tiktok_A.md
VARIANT A | 静物取走型 | 5 shots | ABBBC | 平台号导流字卡
WROTE E:/OneDrive/工作文件夹/电商视频-廖总/prompts/pop_sku01_tiktok_B.md
VARIANT B | 柜门分人设型 | 5 shots | CCAAC | 平台号导流字卡
WROTE E:/OneDrive/工作文件夹/电商视频-廖总/prompts/pop_sku01_tiktok_C.md
VARIANT C | 剧情打断型 | 5 shots | BAAAC | 平台号导流字卡
OK variants=3 total_duration=15 unit_duration=3 ratio=9:16; C shots blocked
```

## 验证命令

```bash
PYTHONIOENCODING=utf-8 py -3 'E:/OneDrive/工作文件夹/电商视频-廖总/pipeline/verify_prompts.py'
```

退出码：0

```text
PASS asset: selected single-bottle PNG is 1254x1254
PASS D5: 3 distinct reference mechanisms; 5 shots x 3 seconds; table first; A/B/C grades
PASS templates: 11 skeletons; 33 platform renders; C units have no prompt
PASS timing: interview product first at 3 seconds; other mechanisms at 0 seconds; endings explicit
PASS facts: unconfirmed excluded; confirmed accepted
ALL CHECKS PASSED; no video generation or live API request performed
```

当前限制：平台账号与渠道未确认；C级需实拍/后期；未调用线上视频API，未生成视频文件。
