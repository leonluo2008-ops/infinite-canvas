#!/usr/bin/env bash
# 赛马 S2/S3：A（cf81829 老路线）与 B（当前分支）各跑同一批片子。
# 用法：bash scripts/race_run.sh
set -u

MAIN="E:/OneDrive/工作文件夹/电商视频-廖总"
A_REPO="C:/Users/leon3/AppData/Local/hermes/cache/scratch/race-A"
OUT="C:/Users/leon3/AppData/Local/hermes/cache/scratch/race-out"

mkdir -p "$OUT/A" "$OUT/B"

FILMS=(
  "POP品宣视频 (3).mp4"
  "ai视频参考2.mp4"
  "full_00.mp4"
  "官号参考1.mp4"
  "ai卖货视频参考2.mp4"
)

run_one() {
  local root="$1" vid="$2" id="$3" outdir="$4"
  echo "=== [$id] $vid"
  ( cd "$root" && py -3 pipeline/reverse/extract.py "$vid" --id "$id" --outdir "$outdir" ) 2>&1 | sed 's/^/    /'
  echo "=== [$id] exit=$?"
}

for f in "${FILMS[@]}"; do
  base=$(basename "$f" .mp4)
  run_one "$A_REPO" "$MAIN/uploads/$f" "A-$base" "$OUT/A/$base"
  run_one "$MAIN"     "$MAIN/uploads/$f" "B-$base" "$OUT/B/$base"
done

echo "=== 全部完成"
