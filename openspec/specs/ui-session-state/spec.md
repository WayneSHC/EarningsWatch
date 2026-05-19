# ui-session-state Specification

## Purpose

定義 `src/ui/state.py` 的 `UIState` dataclass：所有 Streamlit `session_state` 欄位的單一宣告位置、`get()` classmethod 的 setdefault 語意（跨 rerun 維持實例）、欄位語意分組（cooldown / 結果分派 / 單公司 / 多公司 / PDF 快取）。本 spec 確保新增欄位有單一修改點、字串 key typo 暴露為 `AttributeError`、view 模組對 state 形狀有可信賴的契約。

## Requirements

### Requirement: `UIState` SHALL 為 dataclass，包含所有 session 欄位

`UIState` MUST 以 `@dataclass` 宣告，包含下列欄位（含預設值）：

- `last_run_time: float = 0.0`
- `mode: Literal["single", "multi"] | None = None`
- `result: dict | None = None`
- `meta: dict | None = None`
- `multi_results: dict | None = None`
- `multi_companies: list[str]`（`field(default_factory=list)`）
- `multi_topic: str = ""`
- `multi_quarters: list[str]`（`field(default_factory=list)`）
- `multi_custom_query: str = ""`
- `pdf_cache_key: str | None = None`
- `single_pdf_bytes: bytes | None = None`
- `multi_pdf_bytes: bytes | None = None`

無參數實例化 MUST 不 raise。

#### Scenario: 預設構造
- **WHEN** `UIState()` 被呼叫
- **THEN** 物件成立，`last_run_time == 0.0`、`mode is None`、`multi_companies == []`、`single_pdf_bytes is None`

#### Scenario: 列表欄位獨立 default
- **GIVEN** 兩個 `UIState()` 實例 a 與 b
- **WHEN** `a.multi_companies.append("x")`
- **THEN** `b.multi_companies == []`（`field(default_factory=list)` 確保各實例獨立 list）

### Requirement: `UIState.get()` SHALL 用 `st.session_state.setdefault` 取單例

`UIState.get()` classmethod MUST：
1. 從 streamlit 模組讀取 `session_state`（lazy import streamlit 以支援無 streamlit 環境的 import）
2. 用 `session_state.setdefault("_ui_state", cls())` 取得或建立 instance
3. 在同一 Streamlit session 內多次呼叫 MUST 回傳同一物件

#### Scenario: 同 session 重用同一實例
- **GIVEN** 一個假的 `session_state` dict
- **WHEN** `UIState.get()` 被呼叫兩次（同一 session_state）
- **THEN** 兩次回傳同一物件（`is` 相同）

#### Scenario: mutation 跨呼叫持續
- **GIVEN** `UIState.get()` 取得實例 s1，並 `s1.multi_topic = "AI"`
- **WHEN** 再次 `UIState.get()`
- **THEN** 回傳值的 `multi_topic == "AI"`

### Requirement: Streamlit 缺席時 SHALL 不影響 import

`from src.ui.state import UIState` MUST 在 `streamlit` 未安裝時仍可成功（`get()` 的 streamlit import 為函數內 lazy）。`UIState` 本身的構造、欄位存取 MUST 不依賴 streamlit。

#### Scenario: 無 streamlit 環境可 import
- **GIVEN** `streamlit` 不在 sys.modules（或 import 失敗）
- **WHEN** `from src.ui.state import UIState` 被執行
- **AND** `UIState()` 被呼叫
- **THEN** 不 raise

### Requirement: 不存在欄位 MUST raise `AttributeError`

`UIState` MUST 不允許動態未宣告欄位 — 取用未宣告欄位 MUST raise `AttributeError`（dataclass 預設行為）。此 spec 透過此保證：sprinkled 字串 key typo（`state.muti_topic`）會立刻暴露為 error，而非默默讀到 `None`。

#### Scenario: typo 欄位 raise
- **WHEN** `UIState().some_typo_field` 被讀取
- **THEN** raise `AttributeError`
