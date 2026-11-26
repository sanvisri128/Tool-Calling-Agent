"""
app.py – Streamlit front-end for the Student Deadline & Study Planner Agent.

Features
--------
* Sidebar: Gemini API key input (password-masked, never stored to disk).
* Chat interface with full conversation history.
* Expandable agent-steps pane showing every tool call and result.
* Step-limit warning banner when STEP_LIMIT is reached.
* Clear chat button.

Bug fixes applied
-----------------
1. Removed `icon=` kwarg from st.warning() – not supported, caused TypeError.
2. Fixed double-render of user messages: removed the redundant immediate
   st.chat_message("user") block; messages are shown only via the history loop.
3. Fixed step-expander placement: expanders now rendered inside the assistant
   chat bubble so they are visually attached to the right message.
4. Fixed step-index mapping: track assistant_index explicitly instead of
   relying on idx//2 (which breaks if history grows non-uniformly).
5. Removed unused `os` import from agent_core.py (see that file).
"""

from __future__ import annotations

import streamlit as st

from agent_core import STEP_LIMIT, build_agent, run_turn

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Student Planner Agent",
    page_icon="📚",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_state() -> None:
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []   # list[dict] {role, content}
    if "lc_history" not in st.session_state:
        st.session_state.lc_history = []     # list[BaseMessage]
    if "all_steps" not in st.session_state:
        st.session_state.all_steps = []      # list[list[dict]] — one per assistant turn
    if "agent" not in st.session_state:
        st.session_state.agent = None
    if "api_key" not in st.session_state:
        st.session_state.api_key = ""
    if "limit_hit" not in st.session_state:
        st.session_state.limit_hit = False

_init_state()

# ---------------------------------------------------------------------------
# Sidebar – API key + controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚙️ Configuration")
    st.markdown("---")

    api_key_input = st.text_input(
        "🔑 Gemini API Key",
        type="password",
        placeholder="Paste your Google Gemini API key here",
        help=(
            "Your key is used only for this session and is never saved to disk "
            "or transmitted anywhere other than the Google Gemini API."
        ),
    )

    if api_key_input and api_key_input != st.session_state.api_key:
        st.session_state.api_key = api_key_input
        # Rebuild agent whenever the key changes
        with st.spinner("Initialising agent…"):
            try:
                st.session_state.agent = build_agent(api_key_input)
                st.success("✅ Agent ready!")
            except Exception as exc:
                st.error(f"Failed to initialise agent: {exc}")
                st.session_state.agent = None

    st.markdown("---")
    st.markdown(f"**Step limit:** {STEP_LIMIT} steps per query")
    st.markdown(
        "**Model:** Gemini 2.5 Flash  \n"
        "**Tools:** get\\_upcoming\\_deadlines, calculate\\_daily\\_hours, draft\\_study\\_plan"
    )

    st.markdown("---")
    if st.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state.chat_history = []
        st.session_state.lc_history = []
        st.session_state.all_steps = []
        st.session_state.limit_hit = False
        st.rerun()

    st.markdown("---")
    st.caption(
        "⚠️ Your API key is kept only in your browser session memory "
        "and is never logged or saved."
    )

# ---------------------------------------------------------------------------
# Main area – title + step-limit warning
# ---------------------------------------------------------------------------

st.title("📚 Student Deadline & Study Planner Agent")
st.markdown(
    "Ask me about your upcoming deadlines, how many hours to study each day, "
    "or to draft a full study plan.  \n"
    "_This agent reads from `data/deadlines.csv` – it never guesses or invents data._"
)

# FIX 1: Removed unsupported icon= kwarg from st.warning()
if st.session_state.limit_hit:
    st.warning(
        f"⚠️ **Step limit reached ({STEP_LIMIT} steps).** "
        "The agent stopped early to prevent runaway loops. "
        "Please rephrase your question or break it into smaller parts."
    )

# ---------------------------------------------------------------------------
# Chat display
# ---------------------------------------------------------------------------
# FIX 2: Track assistant_index explicitly instead of using idx // 2,
#         which is fragile when history grows non-uniformly.
# FIX 3: Render the step expander INSIDE the assistant chat_message block
#         so it is visually attached to the correct bubble.
# ---------------------------------------------------------------------------

chat_container = st.container()

with chat_container:
    assistant_index = 0
    for msg in st.session_state.chat_history:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
        else:
            with st.chat_message("assistant"):
                st.markdown(msg["content"])

                # Show tool-call steps inside the same bubble
                if assistant_index < len(st.session_state.all_steps):
                    step_set = st.session_state.all_steps[assistant_index]
                    if step_set:
                        with st.expander(
                            f"🔍 Agent steps ({len(step_set)} steps)",
                            expanded=False,
                        ):
                            for step in step_set:
                                if step["type"] == "tool":
                                    st.markdown(f"**🔧 Tool called:** `{step['name']}`")
                                    st.code(step.get("args", ""), language="json")
                                    st.markdown("**Result:**")
                                    st.code(step["content"], language="text")
                                    st.markdown("---")
                                else:
                                    if step["content"] and step["content"] != "(thinking…)":
                                        st.markdown(f"**🤖 Agent thinking:** {step['content']}")

            assistant_index += 1

# ---------------------------------------------------------------------------
# Input box
# ---------------------------------------------------------------------------

placeholder_text = (
    "e.g. What deadlines do I have this week? "
    "or: How many hours/day do I need for my Calculus Final?"
)

user_input = st.chat_input(placeholder_text, disabled=st.session_state.agent is None)

if user_input:
    if not st.session_state.api_key:
        st.error("Please enter your Gemini API key in the sidebar first.")
        st.stop()

    if st.session_state.agent is None:
        st.error("Agent not initialised. Check your API key.")
        st.stop()

    # FIX 4: Append to history FIRST, then rerun so the loop above renders
    #         the user message — avoids the double-render ghost message.
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    # Run agent and show a spinner while waiting
    with st.spinner("Thinking…"):
        try:
            answer, steps, updated_lc_history, limit_hit = run_turn(
                st.session_state.agent,
                user_input,
                st.session_state.lc_history,
            )
        except Exception as exc:
            answer = f"❌ An error occurred: {exc}"
            steps = []
            updated_lc_history = st.session_state.lc_history
            limit_hit = False

    # Persist assistant reply
    st.session_state.lc_history = updated_lc_history
    st.session_state.chat_history.append({"role": "assistant", "content": answer})
    st.session_state.all_steps.append(steps)
    st.session_state.limit_hit = limit_hit

    # Rerun so the full chat loop re-renders cleanly (no duplicate bubbles)
    st.rerun()
