"""
src/ui/styles.py

集中放置 Streamlit 自訂 CSS。
抽出此檔的目的：
  - app.py 有 1000+ 行，把 inline style 拉出去後更容易閱讀
  - 樣式調整時不必滾過 import / 商業邏輯
  - 未來若要支援 dark/light theme 切換，這裡是單一改動點
"""

# 卡片：矛盾警告（黃）/ 一致確認（綠）/ 步驟 log（灰）
CUSTOM_CSS = """
<style>
.contradiction-card {
    background: #fff3cd;
    border-left: 4px solid #ffc107;
    padding: 12px 16px;
    border-radius: 4px;
    margin: 8px 0;
}
.ok-card {
    background: #d4edda;
    border-left: 4px solid #28a745;
    padding: 12px 16px;
    border-radius: 4px;
    margin: 8px 0;
}
.step-log {
    font-family: monospace;
    font-size: 13px;
    background: #f8f9fa;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 2px 0;
}
</style>
"""
