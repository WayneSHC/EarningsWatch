#!/bin/bash
# EarningsWatch 一鍵啟動腳本
# 用法：./start.sh
#   前台執行 Streamlit（按 Ctrl+C 停止）
#   資料庫已遷移至 GCP BigQuery Serverless，無需本機啟動資料庫容器。
#
# ⚠ 安全注意（部署到網際網路前必讀）：
#   - Streamlit 若需對外，請在前端加 nginx 反向代理並啟用 HTTPS

set -euo pipefail
cd "$(dirname "$0")"

# ── 1. 確認 GCP 環境變數 ───────────────────────────────────────────────────
if [ -z "${GOOGLE_CLOUD_PROJECT:-}" ]; then
    echo "⚠ 警告: 尚未設定 GOOGLE_CLOUD_PROJECT 環境變數。"
    echo "  程式將嘗試使用預設的 GCP 專案（若環境有設定 ADC）。"
fi

# ── 2. 啟動 Streamlit（前台，Ctrl+C 停止）──────────────────────────────────
# [f] 本機開發：server.address 設為 127.0.0.1 僅本機可存取
#     對外部署：改 127.0.0.1 → 0.0.0.0，但必須搭配 nginx HTTPS 反向代理
echo "🚀 啟動 Streamlit → http://localhost:8501"
echo "   （按 Ctrl+C 停止）"
echo "   ⚠  若要對外服務，請確認已設定 nginx HTTPS 反向代理"
echo ""

source venv/bin/activate
exec streamlit run src/ui/app.py \
    --server.port 8501 \
    --server.headless true \
    --server.address 127.0.0.1 \
    --browser.gatherUsageStats false
