#!/usr/bin/env bash
# 把分卷合并回完整的离线部署包，并校验完整性。
#
# 为什么分包：完整包约 6.6GB，超过很多传输通道/网盘 1GB 的单文件上限。
# 用 split 切成 900MiB 的分卷，逐个传输后在目标机器上合并回去，内容与原始包完全一致
# （合并后 sha256 与原包相同，脚本会校验）。
#
# 用法：
#   bash join-parts.sh                     # 合并本目录下的分卷
#   bash join-parts.sh <分卷前缀>           # 指定前缀（不含 .part-NN 后缀）
#
# 例：分卷为 huizhi-assistant-full-20260928.tar.gz.part-00 … part-07
#     bash join-parts.sh huizhi-assistant-full-20260928.tar.gz
#
# 兼容 bash 3.2（macOS 自带版本，无 mapfile/关联数组）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}"

PREFIX="${1:-}"
if [[ -z "${PREFIX}" ]]; then
  # 自动识别：本目录里所有 *.part-NN 去掉后缀后的唯一前缀
  unique=""
  for candidate in *.part-[0-9][0-9]; do
    [[ -e "${candidate}" ]] || continue
    base="${candidate%.part-[0-9][0-9]}"
    case " ${unique} " in
      *" ${base} "*) ;;
      *) unique="${unique}${unique:+ }${base}" ;;
    esac
  done
  if [[ -z "${unique}" ]]; then
    echo "错误：本目录没有找到 *.part-NN 分卷文件。" >&2
    exit 1
  fi
  # 多个前缀时无法自动决定，要求显式指定
  if [[ "${unique}" == *" "* ]]; then
    echo "错误：本目录有多个不同的分卷前缀，请显式指定一个：" >&2
    for u in ${unique}; do echo "  ${u}" >&2; done
    exit 1
  fi
  PREFIX="${unique}"
fi

echo "==> 分卷前缀：${PREFIX}"

# 收集分卷。split -d 生成等宽数字后缀，字典序即正确顺序。
parts=()
for p in "${PREFIX}".part-[0-9][0-9]; do
  [[ -e "${p}" ]] || continue
  parts+=("${p}")
done
if [[ ${#parts[@]} -eq 0 ]]; then
  echo "错误：没有找到 ${PREFIX}.part-NN 分卷。" >&2
  exit 1
fi

# 校验分卷编号连续（缺一个就会拼出损坏的包，必须提前拦住）
expected=0
for p in "${parts[@]}"; do
  n=$((10#$(echo "${p}" | sed -E 's/.*\.part-([0-9]+)$/\1/')))
  if [[ ${n} -ne ${expected} ]]; then
    echo "错误：分卷编号不连续，期望 ${PREFIX}.part-$(printf '%02d' ${expected})，实际到 ${p}" >&2
    echo "      请确认所有分卷都已传输完整。" >&2
    exit 1
  fi
  expected=$((expected + 1))
done
echo "    分卷数量：${#parts[@]}（part-00 … part-$(printf '%02d' $((expected - 1)))），编号连续 OK"

echo "==> 合并中（约 6.6GB，需要几十秒到几分钟）…"
cat "${parts[@]}" > "${PREFIX}"

# 用打包时留下的 sha256 校验合并结果，确保与原始包逐字节一致
if [[ -f "${PREFIX}.sha256" ]]; then
  echo "==> 校验 sha256"
  expected_sum="$(cut -d' ' -f1 < "${PREFIX}.sha256")"
  actual_sum="$(shasum -a 256 "${PREFIX}" | cut -d' ' -f1)"
  if [[ "${expected_sum}" != "${actual_sum}" ]]; then
    echo "错误：sha256 不匹配，合并结果损坏！" >&2
    echo "  期望 ${expected_sum}" >&2
    echo "  实际 ${actual_sum}" >&2
    echo "  请重新传输并再次执行本脚本。" >&2
    exit 1
  fi
  echo "    sha256 OK: ${actual_sum}"
else
  echo "    警告：未找到 ${PREFIX}.sha256，跳过校验（不推荐）。" >&2
fi

SIZE="$(du -h "${PREFIX}" | cut -f1)"
echo
echo "==> 完成：${HERE}/${PREFIX} (${SIZE})"
echo
echo "下一步（在部署目录 /data 下）："
echo "  cd /data && tar -xf ${PREFIX}"
echo "  cd /data/huizhi-assistant && bash scripts/load-images.sh"
echo
echo "确认无误后可以删除分卷以释放空间："
echo "  rm -f ${HERE}/${PREFIX}.part-*"
