# EarningsWatch — Cloud Run image
#
# 設計重點：
# 1. 多階段建置：builder 安裝編譯依賴與 pip 套件，runner 只帶執行期所需檔案 → 縮 image 體積
# 2. Embedding 走 Vertex AI text-multilingual-embedding-002（runtime 透過 API 呼叫，
#    image 內不再烤入 sentence-transformers 本地模型）
# 3. fonts-noto-cjk：CJK PDF 匯出需要（packages.txt 在 Streamlit Cloud 用，這裡顯式裝）
# 4. 走 0.0.0.0 + $PORT：Cloud Run 會注入 PORT 環境變數，預設 8080
# 5. non-root user：降低權限

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 編譯期依賴（部分 transitive wheel 需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 裝到固定 prefix，方便 stage 2 整包搬走
RUN pip install --prefix=/install -r requirements.txt


# ── Stage 2: runner ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# 執行期系統依賴：
#   fonts-noto-cjk → fpdf2 CJK PDF 匯出
#   libgomp1       → numpy / pandas 用到的 OpenMP runtime
#   curl           → 容器內 healthcheck 偵錯（選用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 從 builder 搬 site-packages
COPY --from=builder /install /usr/local

# 建立 non-root user
RUN useradd -m -u 1000 app
WORKDIR /app

# 只 COPY 真正需要的執行期檔案（其餘由 .dockerignore 排除）
COPY --chown=app:app src/ ./src/
COPY --chown=app:app scripts/ ./scripts/
COPY --chown=app:app .streamlit/ ./.streamlit/
COPY --chown=app:app cache/ ./cache/

USER app

EXPOSE 8080

# Cloud Run 會打 $PORT；Streamlit 必須綁 0.0.0.0
# --server.address 覆蓋 .streamlit/config.toml 中的 127.0.0.1
#
# 用 sh -c 包起來才能展開 ${PORT}（Cloud Run 注入），同時保留 JSON exec form：
# JSON form 讓 Streamlit 直接收到 Cloud Run 發的 SIGTERM，可以走 graceful shutdown
# （shell form 會把 sh 設成 PID 1，吃掉 SIGTERM）
CMD ["sh", "-c", "exec streamlit run src/ui/app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false"]
