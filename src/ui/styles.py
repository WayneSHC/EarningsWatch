"""
src/ui/styles.py

集中放置 Streamlit 自訂 CSS。
抽出此檔的目的：
  - app.py 有 1000+ 行，把 inline style 拉出去後更容易閱讀
  - 樣式調整時不必滾過 import / 商業邏輯
  - 未來若要支援 dark/light theme 切換，這裡是單一改動點
"""

# 卡片：矛盾警告（黃）/ 一致確認（綠）/ 步驟 log（灰），並套用 Google Material Design 風格
CUSTOM_CSS = """
<style>
/* Import Google Fonts */
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Roboto+Mono&display=swap');

/* Apply font to overall app */
html, body, [class*="css"] {
    font-family: 'Roboto', sans-serif !important;
}

/* Typography */
h1, h2, h3 {
    color: #202124 !important;
    font-weight: 500 !important;
}

p, span, div {
    color: #3C4043;
}

/* Material Design Color Palette for Cards */
.contradiction-card {
    background-color: #FFF8E1; /* Material Amber 50 */
    border-left: 4px solid #FBBC05; /* Google Yellow */
    padding: 16px 20px;
    border-radius: 8px;
    margin: 12px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24);
    color: #202124;
    font-size: 15px;
}

.ok-card {
    background-color: #E6F4EA; /* Material Green 50 */
    border-left: 4px solid #34A853; /* Google Green */
    padding: 16px 20px;
    border-radius: 8px;
    margin: 12px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24);
    color: #202124;
    font-size: 15px;
}

.step-log {
    font-family: 'Roboto Mono', monospace !important;
    font-size: 13px;
    background: #F8F9FA; /* Google Light Gray */
    padding: 10px 14px;
    border-radius: 6px;
    margin: 4px 0;
    color: #5F6368; /* Google Gray */
    border: 1px solid #DADCE0; /* Google Border Color */
}

/* Primary Button Styling (Google Red)
   [修正] Streamlit 按鈕 DOM 使用 data-testid，不是 kind 屬性，
          必須用 [data-testid="baseButton-primary"] 才能正確選中。 */
[data-testid="baseButton-primary"] {
    background-color: #EA4335 !important; /* Google Red */
    color: #FFFFFF !important;            /* 純白文字，對比度 4.5:1（WCAG AA） */
    border: none !important;
    border-radius: 24px !important;
    font-family: 'Roboto', sans-serif !important;
    font-size: 15px !important;           /* 加大字體，提升可讀性 */
    font-weight: 600 !important;          /* Medium-Bold，讓文字更清晰 */
    letter-spacing: 0.3px !important;     /* 微調字距，提升辨識度 */
    padding: 10px 28px !important;
    transition: background-color 0.2s, box-shadow 0.2s !important;
    box-shadow: 0 1px 2px 0 rgba(60,64,67,0.3), 0 1px 3px 1px rgba(60,64,67,0.15) !important;
}

[data-testid="baseButton-primary"]:hover {
    background-color: #C5221F !important; /* Google Red Dark（hover 加深） */
    box-shadow: 0 1px 3px 0 rgba(60,64,67,0.3), 0 4px 8px 3px rgba(60,64,67,0.15) !important;
    color: #FFFFFF !important;
}

/* Standard Button Styling
   [修正] 同上，改用正確的 data-testid 選擇器。 */
[data-testid="baseButton-secondary"] {
    background-color: white !important;
    color: #1A73E8 !important;
    border: 1px solid #DADCE0 !important;
    border-radius: 24px !important;
    font-weight: 500 !important;
    padding: 10px 24px !important;
    transition: background-color 0.2s !important;
}

[data-testid="baseButton-secondary"]:hover {
    background-color: #F8F9FA !important;
    border: 1px solid #D2E3FC !important;
}

/* Inputs styling */
.stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stTextArea textarea {
    border-radius: 8px !important;
    border: 1px solid #DADCE0 !important;
    background-color: white !important;
}

.stTextInput input:focus, .stSelectbox div[data-baseweb="select"] > div:focus-within, .stTextArea textarea:focus {
    border: 2px solid #1A73E8 !important;
    box-shadow: none !important;
}

/* Divider */
hr {
    border-top: 1px solid #DADCE0 !important;
}

/* Expander styling */
.streamlit-expanderHeader {
    font-weight: 500 !important;
    color: #202124 !important;
    background-color: #F8F9FA !important;
    border-radius: 8px !important;
}
</style>
"""
