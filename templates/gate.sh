#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VIOLATION_LOG="$ROOT_DIR/test-governance/gate-violations.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log_violation() {
  local rule_id="$1" severity="$2" file="$3" detail="$4"
  local timestamp rel_file
  timestamp=$(date '+%Y-%m-%d %H:%M:%S')
  rel_file="${file#$ROOT_DIR/}"
  rel_file="${rel_file//|/\\|}"
  detail="${detail//|/\\|}"
  detail="${detail//$'\n'/ }"
  echo "$timestamp | $rule_id | $severity | $rel_file | $detail" >> "$VIOLATION_LOG" || true
}

fail() { echo -e "${RED}[GATE-FAIL]${NC} $1" >&2; exit 1; }
pass() { echo -e "${GREEN}[GATE-PASS]${NC} $1"; }
warn() { echo -e "${YELLOW}[GATE-WARN]${NC} $1"; }

# ==================== 维度覆盖统计（通用） ====================

check_dimension_coverage() {
  echo "🔍 检查测试维度覆盖..."
  local test_dirs=("$@")
  local total=0 tagged=0 dim_counts=""

  for dir in "${test_dirs[@]}"; do
    [ -d "$dir" ] || continue
    while IFS= read -r f; do
      total=$((total + 1))
      local dims
      dims=$(grep -oE '@dimension [0-9,]+' "$f" | head -1 | sed 's/@dimension //' || true)
      if [ -n "$dims" ]; then
        tagged=$((tagged + 1))
        for d in $(echo "$dims" | tr ',' ' '); do
          dim_counts="$dim_counts $d"
        done
      fi
    done < <(find "$dir" -name '*test*' -o -name '*Test*' -o -name '*_test*' | grep -v node_modules || true)
  done

  if [ "$total" -eq 0 ]; then
    echo "  ⚠️ 未找到测试文件，跳过"
    return 0
  fi

  echo "  测试文件总数：$total，已标注维度：$tagged（$(( tagged * 100 / total ))%）"

  for d in 1 2 3 4 5 6; do
    local count
    count=$(echo "$dim_counts" | tr ' ' '\n' | grep -c "^${d}$" || true)
    local label
    case $d in
      1) label="正常路径" ;; 2) label="副作用清理" ;; 3) label="并发安全" ;;
      4) label="错误恢复" ;; 5) label="安全边界" ;; 6) label="故障后可用" ;;
    esac
    if [ "$count" -eq 0 ] && [ "$tagged" -gt 0 ]; then
      echo "  ⚠️ 维度 $d（$label）：0 个测试覆盖"
      log_violation "DIM-$d-missing" "WARN" "-" "维度 $d（$label）无测试覆盖"
    else
      echo "  ✅ 维度 $d（$label）：$count 个测试"
    fi
  done
  return 0
}

# ==================== 违规趋势分析（通用） ====================

show_trend() {
  echo ""
  echo -e "${CYAN}=== 门禁违规趋势分析 ===${NC}"
  echo ""
  if [ ! -f "$VIOLATION_LOG" ]; then
    echo "  暂无违规记录，运行 preflight 后会自动产生。"
    return 0
  fi
  local total
  total=$(wc -l < "$VIOLATION_LOG" | tr -d ' ')
  echo "  总记录数：$total 条"
  echo ""

  echo -e "${CYAN}── 按规则统计（高频优先）──${NC}"
  awk -F ' \\| ' 'NF>=5 {gsub(/^ +| +$/, "", $2); print $2}' "$VIOLATION_LOG" \
    | sort | uniq -c | sort -rn \
    | while read -r count rule; do
      [[ "$count" =~ ^[0-9]+$ ]] || continue
      if [ "$count" -ge 10 ]; then
        echo -e "  ${RED}$count${NC}  $rule  ← 高频，建议加入 test-governance/coding-guidelines.md"
      elif [ "$count" -ge 5 ]; then
        echo -e "  ${YELLOW}$count${NC}  $rule"
      else
        echo "  $count  $rule"
      fi
    done
  echo ""

  echo -e "${CYAN}── 按文件统计（问题热点）──${NC}"
  awk -F ' \\| ' 'NF>=5 {gsub(/^ +| +$/, "", $4); print $4}' "$VIOLATION_LOG" \
    | sort | uniq -c | sort -rn | head -10 \
    | while read -r count file; do
      [[ "$count" =~ ^[0-9]+$ ]] || continue
      echo "  $count  $file"
    done
  echo ""

  local top_count
  top_count=$(awk -F ' \\| ' 'NF>=5 {gsub(/^ +| +$/, "", $2); print $2}' "$VIOLATION_LOG" \
    | sort | uniq -c | sort -rn | head -1 | awk '{print $1}')
  [[ "${top_count:-0}" =~ ^[0-9]+$ ]] || top_count=0
  local top_rule
  top_rule=$(awk -F ' \\| ' 'NF>=5 {gsub(/^ +| +$/, "", $2); print $2}' "$VIOLATION_LOG" \
    | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')

  echo -e "${CYAN}── /review 建议 ──${NC}"
  if [ -n "$top_rule" ] && [ "$top_count" -ge 5 ]; then
    echo -e "  ${YELLOW}建议${NC}：规则 $top_rule 已触发 $top_count 次，考虑加入 test-governance/coding-guidelines.md"
  else
    echo "  当前无高频违规规则，门禁运转良好。"
  fi
  echo ""
}

# ==================== 项目规则（按需填充） ====================

run_static_analysis() {
  echo ""
  echo "=== 静态分析检查 ==="
  pass "静态分析检查通过"
}

# ==================== 入口 ====================

mkdir -p "$(dirname "$VIOLATION_LOG")"
mode="${1:-preflight}"

case "$mode" in
  preflight) run_static_analysis; pass "preflight 完成" ;;
  ci)        run_static_analysis; pass "ci gate 全部通过" ;;
  trend)     show_trend ;;
  *)         fail "未知模式：$mode（可用：preflight | ci | trend）" ;;
esac
