# 變更摘要：2026-05-08 ~ 2026-05-13

> 涵蓋上週五（2026-05-08，星期五）至今天（2026-05-13，星期三）。
> 統計：47 檔變動、+3,462 / −1,636 行。

---

## 一句話總結

完成 **GCP Serverless 遷移收尾**（BigQuery + Vertex AI + Cloud Run + Secret Manager），
同步補上 **UI 體驗強化**（Material Design、語音輸入、infinity loading）、
**Agent 路由修正**（前瞻型查詢轉 Tavily）與 **Anthropic backend 回歸**。

---

## 主題分類

### 1. ☁ GCP 部署與密鑰管理

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-10 | `8261697` | feat(deploy): 新增 GCP Cloud Run 部署設定（含 `Dockerfile`, `docs/DEPLOY_GCP.md`） |
| 2026-05-13 | `3802d6e` | feat(security): 整合 GCP Secret Manager 作 API key 存放（`src/core/secrets.py`） |
| 2026-05-13 | `8d66187` | feat(scripts): 新增 `scripts/rotate_secret.sh` 助手 |
| 2026-05-13 | `986287b` | feat(scripts): `rotate_secret.sh` 同時支援初次建立 |
| 2026-05-13 | `f98389e` | feat(secrets): `LANGSMITH_API_KEY` 從 Secret Manager 自動橋接到 env |
| — | — | 配套新檔：`scripts/setup_gcp_secrets.sh`, `.gitleaks.toml`, `.githooks/pre-commit` |

### 2. 🔐 安全性強化

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-10 | `04e8d27` | fix(llm): 防止 raw HTTP body 洩漏到 UI；補強 demo cache 保底機制 |
| 2026-05-13 | `6db8863` | feat(security): 多層 API-key 洩漏防護（gitleaks + pre-commit hook） |

### 3. 🤖 LLM 後端 / Agent 邏輯

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-08 | `992f77a` | fix: 校正 `BACKEND_MODELS` 模型名稱 — `gpt-5` / `gemini-2.5-flash` |
| 2026-05-09 | `5776bdb` | chore: 補上 `scripts/probe_llm_models.py` + 標註 gpt-5 估算定價 |
| 2026-05-10 | `6eb1e59` | fix(agent): `intent_classifier` 保留使用者輸入的英文縮寫 |
| 2026-05-10 | `ab7e362` | fix(llm): 修正 quota 用盡被誤判為速率限制 |
| 2026-05-11 | `2c54622` | fix(agent): `query_decomposer` 也保留使用者輸入的縮寫 |
| 2026-05-13 | `2860f50` | feat(llm): 重新啟用 Anthropic backend（Claude Sonnet 4.6 / Haiku 4.5） |
| 2026-05-13 | `5e17e09` | fix(agent): 前瞻型查詢自動路由到 Tavily 即時新聞節點 |

### 4. 🎨 UI / UX

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-09 | `3717df6` | refactor(P1-8): `app.py` `session_state` 集中至 `UIState` dataclass |
| 2026-05-09 | `d0201f8` | refactor(ui): `app.py` 拆分為 `views/single.py` + `views/multi.py` |
| 2026-05-09 | `ce90ad8` | feat(P2): 主題改為可選欄位（自動推導） |
| 2026-05-11 | `f8298a8` | docs: 專案描述同步「RAG Agent」用詞 |
| 2026-05-11 | `ea455a8` | chore(ui): Agentic RAG → RAG Agent；分析主題 → 建議主題 |
| 2026-05-12 | `9355781` | feat(ui): 套用 Google Material Design + 修 button CSS selector |
| 2026-05-12 | `a5812f4` | feat(ui): loading spinner 改為 infinity SVG 動畫 |
| 2026-05-13 | `6f5e41f` | feat(ui): 自訂查詢欄位支援 Web Speech API 語音輸入 |

### 5. 🧪 CI / 工程實踐

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-09 | `2acdc7b` | ci: 新增 Streamlit smoke test（捕捉 `set_page_config` / import 順序錯誤） |
| 2026-05-09 | `8f51e8a` | chore: 用 `docker-compose` 取代 `start.sh` 的 `docker run` fallback（之後伴隨 BigQuery 遷移再度移除本機 DB） |

### 6. 📚 文件 / Roadmap

| 日期 | Commit | 內容 |
|---|---|---|
| 2026-05-09 | `32e92f5` | chore: ROADMAP #8 / #9 / #10 quick wins |
| 2026-05-09 | `d0e225e` | docs(roadmap): 新增 #6–#10（test 補強 + 文件 / 原子寫入修補） |
| 2026-05-09 | `78f6f80` | docs(roadmap): #3 / #4 加上 GCP 部署情境註記 |
| 2026-05-09 | `7b546e6` | docs(roadmap): #1 補登已完成 |
| 2026-05-09 | `96d4d07` | docs(roadmap): 標記 #2 / #5 完成 |
| 2026-05-09 | `222a539` | docs(roadmap): 補上 #8/#9/#10 commit hash |

---

## 主要新檔（節錄）

```
.dockerignore
.gitleaks.toml
.githooks/pre-commit
.github/workflows/ci.yml
Dockerfile
docs/DEPLOY_GCP.md
src/core/bq_client.py
src/core/secrets.py
src/ui/quarters.py
src/ui/state.py
src/ui/views/single.py
src/ui/views/multi.py
scripts/build_demo_cache.py
scripts/probe_llm_models.py
scripts/rotate_secret.sh
scripts/setup_gcp_secrets.sh
scripts/install-hooks.sh
tests/test_nodes.py
```

## 主要移除檔

```
src/core/qdrant_client.py
scripts/migrate_to_cloud.py
docker-compose.yml
```

---

## 對接手成員的影響

1. **本機跑專案不再需要 Docker**。`start.sh` 只負責啟動 Streamlit；資料庫已 Serverless 化。
2. **必填環境變數新增 `GOOGLE_CLOUD_PROJECT`**；本機需 `gcloud auth application-default login`。
3. **Secret Manager 為部署首選**：production 設 `GCP_SECRET_PROJECT` 即可，env var 為 fallback。
4. **UI 路徑變動**：要改畫面前先確認是 `src/ui/views/single.py` 還是 `views/multi.py`，`app.py` 已純粹當 entrypoint。
5. **Anthropic 回歸**：若 `OPENAI_API_KEY` 用完，cascade 順序為 openai → gemini → anthropic → cohere。
