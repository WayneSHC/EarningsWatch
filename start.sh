#!/bin/bash
# EarningsWatch 一鍵啟動腳本
# 用法：./start.sh
#   前台執行 Streamlit（按 Ctrl+C 停止）
#   Qdrant 以 Docker daemon 模式在背景持續運行
#
# ⚠ 安全注意（部署到網際網路前必讀）：
#   - Qdrant 綁定 127.0.0.1，僅供本機 Streamlit 存取，外部無法直接連線
#   - Streamlit 若需對外，請在前端加 nginx 反向代理並啟用 HTTPS
#   - 對外部署建議使用 Qdrant Cloud（QDRANT_URL + QDRANT_API_KEY），
#     此時此腳本中的 Docker 區塊可省略

set -euo pipefail
cd "$(dirname "$0")"

# ── 1. 啟動 Qdrant（背景 Docker，僅綁定 localhost）──────────────────────────
# [f] -p 127.0.0.1:6333:6333：Qdrant 只對本機開放，外部無法直接存取 admin API
echo "🐳 啟動 Qdrant..."
if docker start qdrant 2>/dev/null; then
    echo "   （使用既有容器）"
else
    echo "   （建立新容器，Qdrant 僅綁定 127.0.0.1）"
    docker run -d --name qdrant \
        --restart unless-stopped \
        -p 127.0.0.1:6333:6333 \
        -v "$(pwd)/qdrant_storage:/qdrant/storage" \
        qdrant/qdrant
fi

# ── 2. 等待 Qdrant 就緒（最多 30 秒）───────────────────────────────────────
echo "⏳ 等待 Qdrant 就緒..."
TIMEOUT=30
COUNT=0
until curl -sf http://localhost:6333/healthz >/dev/null; do
    sleep 1
    COUNT=$((COUNT + 1))
    if [ "$COUNT" -ge "$TIMEOUT" ]; then
        echo "❌ Qdrant 啟動逾時（${TIMEOUT} 秒）。請確認 Docker 是否正常運行。"
        exit 1
    fi
done
echo "✅ Qdrant 就緒（${COUNT}s）"

# ── 3. 啟動 Streamlit（前台，Ctrl+C 停止）──────────────────────────────────
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
