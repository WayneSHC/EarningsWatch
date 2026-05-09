"""
src/ui/views/

UI rendering views, split out of app.py to keep the entrypoint thin.

Each view is a single function that takes a UIState and renders to Streamlit.
Views must not read `st.session_state["..."]` with literal keys — go through
UIState (the typed wrapper) so a typo becomes AttributeError instead of
silently returning None.

Adding a third view? Drop it here and dispatch to it from app.py.
"""
