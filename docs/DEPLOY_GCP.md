# GCP Cloud Run 部署指南

把 EarningsWatch（Streamlit + LangGraph Agent）部署到 GCP Cloud Run，
Vector DB 走 Qdrant Cloud Free tier，密鑰用 Secret Manager 管理。

## 架構概覽

```
使用者瀏覽器
    ↓ HTTPS (auto by Cloud Run)
Cloud Run (Streamlit container)
    ├─ Secret Manager   ← OPENAI_API_KEY / GEMINI_API_KEY / ...
    ├─ Qdrant Cloud     ← 向量檢索（Free tier 1GB）
    ├─ OpenAI / Gemini / Cohere  ← LLM
    ├─ Tavily           ← 即時新聞
    └─ Yahoo Finance    ← 股價
```

## 預估費用（30–50 人 demo，3 個月）

| 元件 | 方案 | 月費 |
|---|---|---|
| Cloud Run | 2 vCPU / 2GiB / min=0 / max=2 | $0 ~ $3（在免費額度內機率高） |
| Qdrant Cloud | Free 1GB cluster | $0 |
| Artifact Registry | < 0.5GB image storage | $0 |
| Secret Manager | < 6 secrets, < 10k 讀取/月 | $0 |
| Cloud Logging | 預設 retention | $0（前 50 GiB/月免費） |
| **預估月費** | | **$0 ~ $3** |

GCP 開新帳號送 $300 / 90 天試用，這個案例完全用不到，留著做意外保險。

---

## 一次性前置作業

### 0. 安裝 gcloud CLI（已裝可跳過）

```bash
brew install --cask google-cloud-sdk
gcloud init                                # 登入 Google 帳號 + 選 project
gcloud auth configure-docker               # 讓本機 docker 能 push 到 Artifact Registry
```

### 1. 建立 GCP Project + 啟用 Billing

在 [Cloud Console](https://console.cloud.google.com) 操作：

1. 新建 project，例如 `earningswatch-demo`
2. 連結 Billing Account（$300 試用會自動套用）
3. 把 PROJECT_ID 記下來，後面所有指令會用到

```bash
# 在 terminal 切到該 project
export PROJECT_ID=earningswatch-demo        # ← 改成你的
export REGION=asia-east1                    # 台北用 asia-east1（彰化）；若主要用美國觀眾改 us-central1
gcloud config set project $PROJECT_ID
gcloud config set run/region $REGION
```

### 2. 啟用必要 API

```bash
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudbuild.googleapis.com
```

### 3. 建立 Artifact Registry（存 Docker image）

```bash
gcloud artifacts repositories create earningswatch \
    --repository-format=docker \
    --location=$REGION \
    --description="EarningsWatch container images"
```

### 4. 申請 Qdrant Cloud Free cluster

1. 到 https://cloud.qdrant.io 註冊
2. 新建 1GB Free cluster（選最近的 region）
3. 取得 `QDRANT_URL`（例：`https://xxxx.aws.cloud.qdrant.io`）和 `QDRANT_API_KEY`

### 5. 把本地 Qdrant 資料遷到雲端

```bash
# 在本機 .env 暫時填入 QDRANT_URL / QDRANT_API_KEY
# 然後跑遷移腳本（從本地 Docker Qdrant 同步全部向量到 Cloud）
source venv/bin/activate
python scripts/migrate_to_cloud.py
```

確認雲端 collection `earnings_calls` 的 points_count 跟本地一致。

### 6. 把所有 secrets 寫進 Secret Manager

每個 secret 一行一個指令（避免 key 進 shell history 用 `--data-file=-` + stdin）：

```bash
# 依你實際持有的 key，沒有的就跳過

printf '%s' 'sk-...你的key...' | gcloud secrets create OPENAI_API_KEY --data-file=-
printf '%s' '你的key' | gcloud secrets create GEMINI_API_KEY --data-file=-
printf '%s' '你的key' | gcloud secrets create COHERE_API_KEY --data-file=-
printf '%s' 'tvly-...' | gcloud secrets create TAVILY_API_KEY --data-file=-
printf '%s' 'llx-...' | gcloud secrets create LLAMA_CLOUD_API_KEY --data-file=-

printf '%s' 'https://xxxx.aws.cloud.qdrant.io' | gcloud secrets create QDRANT_URL --data-file=-
printf '%s' 'qdrant-api-key' | gcloud secrets create QDRANT_API_KEY --data-file=-

# ⚠ 強烈建議設一組 APP_PASSWORD（公開 demo 必備）
printf '%s' '請改成你的長亂碼密碼' | gcloud secrets create APP_PASSWORD --data-file=-
```

未來要更新某個 key：

```bash
printf '%s' '新的key' | gcloud secrets versions add OPENAI_API_KEY --data-file=-
# Cloud Run 用 :latest 引用，下次新請求會自動拿到新版（不用 redeploy）
```

### 7. 授權 Cloud Run service account 讀取 secrets

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for secret in OPENAI_API_KEY GEMINI_API_KEY COHERE_API_KEY \
              TAVILY_API_KEY LLAMA_CLOUD_API_KEY \
              QDRANT_URL QDRANT_API_KEY APP_PASSWORD; do
    gcloud secrets add-iam-policy-binding $secret \
        --member="serviceAccount:$SA" \
        --role="roles/secretmanager.secretAccessor" \
        --quiet 2>/dev/null || true     # 不存在的 secret 直接跳過
done
```

---

## 部署流程（每次 release 重複）

### 方案 A：Cloud Build 雲端建置（推薦，第一次就用）

不用本機裝 docker，cloud build 直接 git checkout → docker build → push：

```bash
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/earningswatch/app:latest"

gcloud builds submit --tag $IMAGE
```

第一次建置約 8–15 分鐘（要拉 torch、烤 embedding 模型）。後續有 layer cache 會快很多。

### 方案 B：本機 docker build + push

```bash
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/earningswatch/app:latest"

# arm64 Mac 必須指定 --platform，否則 Cloud Run 跑不起來
docker buildx build --platform linux/amd64 -t $IMAGE --push .
```

### 部署到 Cloud Run

```bash
gcloud run deploy earningswatch \
    --image=$IMAGE \
    --region=$REGION \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=2Gi \
    --cpu=2 \
    --min-instances=0 \
    --max-instances=2 \
    --concurrency=20 \
    --timeout=600 \
    --session-affinity \
    --set-env-vars="LLM_BACKEND=openai,LLM_HYDE_ENABLED=false" \
    --set-secrets="OPENAI_API_KEY=OPENAI_API_KEY:latest,\
GEMINI_API_KEY=GEMINI_API_KEY:latest,\
COHERE_API_KEY=COHERE_API_KEY:latest,\
TAVILY_API_KEY=TAVILY_API_KEY:latest,\
LLAMA_CLOUD_API_KEY=LLAMA_CLOUD_API_KEY:latest,\
QDRANT_URL=QDRANT_URL:latest,\
QDRANT_API_KEY=QDRANT_API_KEY:latest,\
APP_PASSWORD=APP_PASSWORD:latest"
```

部署完會印出 service URL，例如：
```
Service URL: https://earningswatch-xxxxxx-de.a.run.app
```

打開瀏覽器即可使用。

### 旗標解釋（為什麼這樣設）

| 旗標 | 值 | 理由 |
|---|---|---|
| `--memory=2Gi` | 2GB | torch + sentence-transformers + LangGraph + Streamlit，1GB 不夠 |
| `--cpu=2` | 2 vCPU | embedding 計算 + 並行 retrieval，1 vCPU 會卡 |
| `--min-instances=0` | 0 | 沒人用就 scale to zero，省錢；代價是 cold start ≈ 8–15s |
| `--max-instances=2` | 2 | 限制爆量上限，30–50 人 demo 夠用，避免被惡意請求拖爆 budget |
| `--concurrency=20` | 20 | Streamlit 每個 session 持續吃 WebSocket，不能像 stateless API 設 80 |
| `--timeout=600` | 10 分鐘 | Agent 偶爾 LLM 慢（self-reflection retry），預設 5 分鐘可能會中斷 |
| `--session-affinity` | on | Streamlit WebSocket 必須黏同一個 instance，否則狀態會掉 |
| `--allow-unauthenticated` | on | 公開 demo 用；安全性靠 `APP_PASSWORD` 把關 |

---

## 部署後驗證

```bash
# 1. 取得 URL
SERVICE_URL=$(gcloud run services describe earningswatch \
    --region=$REGION --format='value(status.url)')
echo $SERVICE_URL

# 2. Health check
curl -sf "$SERVICE_URL/_stcore/health" && echo OK

# 3. 即時 log
gcloud run services logs tail earningswatch --region=$REGION
```

UI 上測試：
1. 輸入 `APP_PASSWORD` 通過
2. 跑一個 single-company 查詢（例：TSMC 4Q24 vs 4Q25 庫存策略變化）
3. 確認 sidebar telemetry 有 token / cost 數字
4. 匯出 PDF 確認 CJK 字型正常（不是空白方塊）

---

## 維運常見動作

### 更新某個 API key

```bash
printf '%s' '新key' | gcloud secrets versions add OPENAI_API_KEY --data-file=-
# 不用 redeploy；新請求會自動拿到 :latest
```

### 推新版 code

```bash
gcloud builds submit --tag $IMAGE
gcloud run deploy earningswatch --image=$IMAGE --region=$REGION
# 環境變數 / secret 設定保留，不會被覆蓋
```

### 看 cold start 時間 / 流量

Cloud Console → Cloud Run → earningswatch → Metrics tab。
重點看 `Container startup latency`（cold start）和 `Request count`。

### 暫停服務（demo 結束後省到底）

```bash
# 把 max-instances 設 0，container 不會啟動，URL 會回 503
gcloud run services update earningswatch --region=$REGION --max-instances=0

# 想恢復
gcloud run services update earningswatch --region=$REGION --max-instances=2
```

或更乾脆：

```bash
gcloud run services delete earningswatch --region=$REGION
```

Image 與 secrets 還在，未來重部署只要重跑 deploy 指令。

---

## 故障排除

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| `Container failed to start. Failed to start and then listen on the port defined by the PORT environment variable.` | Streamlit 沒綁 0.0.0.0 / 沒讀 $PORT | 確認 Dockerfile CMD 有 `--server.port=${PORT}` 與 `--server.address=0.0.0.0` |
| Cold start 後第一次查詢就 OOM | 2GB 不夠（embedding 模型載入瞬間吃尖峰 1.6GB） | 升 `--memory=4Gi`（會脫離免費額度） |
| 部署成功但畫面空白 / WebSocket 一直斷 | 沒開 session affinity | `--session-affinity` 旗標補上 |
| PDF 匯出顯示空白方塊 | image 沒裝 fonts-noto-cjk | Dockerfile 已包含；確認沒誤改 |
| Qdrant 查詢報 401 | secret 拼錯 / 沒授權 service account | 重跑前置作業 step 6, 7 |
| 想用自訂網域 | Cloud Run 支援 custom domain | `gcloud run domain-mappings create`，需先在 Search Console 驗證網域所有權 |

---

## 安全提醒

- `APP_PASSWORD` 一定要設，否則任何人都能透過你的 LLM key 燒錢
- 建議在 OpenAI / Gemini / Cohere 各自的 console 設「**月度 hard limit**」（例如 $20），即使 APP_PASSWORD 外洩也有止血上限
- Cloud Run 服務 URL 不要公開貼在 GitHub README，盡量私下傳給要 demo 的人
- 看 Cloud Run logs 時注意：使用者輸入的 query 會被記錄到 stdout，避免在 demo 前蒐集到敏感資料
