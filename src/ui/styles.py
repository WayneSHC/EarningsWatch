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

/* ───────────────────────── RWD：mobile (≤768px) ─────────────────────────
   Streamlit 在窄螢幕下不一定會自動把 st.columns 堆疊，且 metric / tab /
   卡片字級在手機上偏大。下面用媒體查詢強制堆疊欄位、縮字、橫向捲動 tab，
   以及限制圖表 / 動畫寬度，避免整頁水平捲動。Desktop 完全不受影響。 */
@media (max-width: 768px) {
    /* 主容器內距收緊，避免兩側留白吃掉內容空間 */
    .main .block-container,
    [data-testid="stAppViewContainer"] .main .block-container {
        padding-left: 0.75rem !important;
        padding-right: 0.75rem !important;
        padding-top: 1rem !important;
    }

    /* st.columns 在手機強制單欄堆疊。
       針對 stHorizontalBlock 的 flex 容器，把每個 column 拉到 100%。 */
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
        gap: 0.5rem !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
    [data-testid="stHorizontalBlock"] > div[data-testid^="column"] {
        flex: 0 0 100% !important;
        width: 100% !important;
        min-width: 100% !important;
    }

    /* 標題縮小避免換行過多佔版面 */
    h1 { font-size: 1.5rem !important; }
    h2 { font-size: 1.25rem !important; }
    h3 { font-size: 1.1rem !important; }
    h4 { font-size: 1rem !important; }

    /* st.metric 數值字體縮小 */
    [data-testid="stMetricValue"] {
        font-size: 1.4rem !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.8rem !important;
    }

    /* Tabs 列在手機可能塞不下，允許橫向捲動而非折行 */
    [data-testid="stTabs"] [role="tablist"] {
        overflow-x: auto !important;
        flex-wrap: nowrap !important;
        scrollbar-width: thin;
        -webkit-overflow-scrolling: touch;
    }
    [data-testid="stTabs"] [role="tab"] {
        flex-shrink: 0 !important;
        white-space: nowrap !important;
        font-size: 0.9rem !important;
        padding: 0.4rem 0.7rem !important;
    }

    /* 卡片內距收緊 */
    .contradiction-card,
    .ok-card {
        padding: 12px 14px !important;
        font-size: 14px !important;
    }
    .step-log {
        font-size: 12px !important;
        padding: 8px 10px !important;
        word-break: break-word;
    }

    /* Primary / secondary button 在手機縮小 padding，避免換行 */
    [data-testid="baseButton-primary"],
    [data-testid="baseButton-secondary"] {
        padding: 8px 16px !important;
        font-size: 14px !important;
    }

    /* 無限載入動畫 SVG 在手機縮放，避免水平捲動 */
    [data-testid="stMarkdownContainer"] svg {
        max-width: 100% !important;
        height: auto !important;
    }

    /* DataFrame 在手機允許水平捲動而非壓縮欄寬 */
    [data-testid="stDataFrame"] {
        overflow-x: auto !important;
    }

    /* Sidebar 開啟時佔滿全寬，避免和主內容擠在一起 */
    [data-testid="stSidebar"] {
        min-width: 85vw !important;
        max-width: 85vw !important;
    }
}
</style>
"""
