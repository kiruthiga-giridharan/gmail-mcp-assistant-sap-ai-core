import asyncio
import html
import json
import re
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any

import streamlit as st
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from initialize_model import get_model


# ============================================================
# CONFIGURATION
# ============================================================

APP_TITLE = "Gmail MCP Assistant"

USER_AVATAR = "🧑‍💼"
ASSISTANT_AVATAR = "📧"

MODEL_OPTIONS = {
    "Claude": "claude",
    "GPT": "gpt",
    "Gemini": "gemini",
}

PROJECT_DIR = Path(__file__).resolve().parent
MCP_SERVER_PATH = PROJECT_DIR / "gmail_mcp_server.py"
SEND_TOOL_NAMES = {"send_email", "reply_to_thread", "reply_to_message"}

QUICK_ACTIONS = {
    "📥 Recent emails": (
        "Get my five most recent emails. For each email, show the sender, "
        "subject, date, and a concise summary."
    ),
    "🔔 Unread summary": (
        "Find my unread inbox emails and summarize the five most important ones."
    ),
    "🏷️ Show labels": (
        "List all labels in my Gmail account."
    ),
    "✍️ Draft an email": (
        "Help me draft a professional email. Ask me for any missing recipient, "
        "subject, or purpose before preparing the draft."
    ),
}


EMAIL_AGENT_SYSTEM_PROMPT = """
You are a professional Gmail assistant and also image generation tool connected to Gmail through MCP tools.
You can search, read, summarize, draft, reply to, and send email.

GENERAL RULES

- Use Gmail tools whenever account data is required. Never invent inbox data.
- Be concise, accurate, and professional.
- When showing email search results, include sender, subject, date, and a brief summary.
- Never expose Gmail message IDs or thread IDs to the user unless troubleshooting requires it.
- Never claim an email was sent unless a Gmail sending tool returned status "sent".

THREAD WORKFLOW

When the user asks to reply, continue a conversation, or send something in the
same thread:

1. Search for the conversation with search_threads. Use Gmail search syntax
   such as `to:person@example.com OR from:person@example.com` and include any
   subject or date clues supplied by the user.
2. If exactly one relevant thread is found, call get_thread to understand the
   conversation and retain its thread_id in the agent history.
3. If several plausible threads are found, ask the user which one they mean.
4. Draft the reply for review. Do not send it yet.
5. After explicit confirmation, call reply_to_thread with the retained
   thread_id. The tool automatically preserves the Gmail thread metadata and
   appends the prior conversation as quoted history.

OUTBOUND EMAIL CONFIRMATION WORKFLOW

Any action that sends an outbound email, including a new email or a reply,
must follow this workflow strictly.

1. DRAFT

Compose the complete email and display it exactly in this structure:

**To:** recipient@example.com
**Subject:** Subject line
**Body:**

Full email body

Then ask:

"Would you like me to send this, or would you like changes?"

Do not call send_email, reply_to_thread, or reply_to_message at this stage.

2. REVISE

If the user requests changes, revise the complete draft, show it again, and
ask for confirmation again.

3. SEND

Only send after an explicit confirmation such as "send it", "yes, send",
"proceed", or "go ahead".

- For a new conversation, call send_email.
- For an existing conversation, call reply_to_thread.
- If the required recipient, draft, or thread is unclear, ask for clarification
  instead of sending.

4. CONFIRM

After a successful tool call, confirm the recipient and subject.

READ-ONLY ACTIONS

Searching, reading, summarizing, getting the profile, and listing labels do not
need confirmation.
"""


# ============================================================
# PAGE SETUP
# ============================================================

def configure_page() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="📧",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def initialize_state() -> None:
    defaults = {
        # Human-readable chat messages shown in the Streamlit UI.
        "messages": [],
        # Full LangChain message state, including tool calls and tool results.
        # Keeping this is essential for "send it" and thread-aware replies.
        "agent_messages": [],
        "pending_prompt": None,
        "tool_names": [],
        "selected_model": "Claude",
        "last_error": "",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def render_html(markup: str) -> None:
    """
    Render HTML directly.

    st.html() prevents Streamlit Markdown from interpreting indented HTML
    as a code block. A compact Markdown fallback is included for older
    Streamlit versions.
    """

    cleaned = textwrap.dedent(markup).strip()

    if hasattr(st, "html"):
        st.html(cleaned)
    else:
        compact = re.sub(r"\s*\n\s*", " ", cleaned)
        st.markdown(compact, unsafe_allow_html=True)


def inject_css() -> None:
    render_html(
        """
        <style>
        :root {
            --primary: #6558e8;
            --primary-dark: #5045cc;
            --purple: #8b5cf6;
            --blue: #0ea5e9;
            --green: #16a34a;
            --amber: #f59e0b;
            --text: #172033;
            --muted: #697086;
            --border: rgba(70, 78, 110, 0.14);
            --surface: rgba(255, 255, 255, 0.86);
        }

        html,
        body,
        [class*="css"] {
            font-family:
                Inter,
                ui-sans-serif,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;
        }

        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(
                    circle at 8% 3%,
                    rgba(101, 88, 232, 0.15),
                    transparent 28%
                ),
                radial-gradient(
                    circle at 94% 5%,
                    rgba(14, 165, 233, 0.14),
                    transparent 27%
                ),
                linear-gradient(
                    180deg,
                    #f9f9ff 0%,
                    #f3f5fb 100%
                );
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        [data-testid="stMainBlockContainer"] {
            max-width: 1120px;
            padding-top: 2rem;
            padding-bottom: 10rem;
        }

        [data-testid="stSidebar"] {
            background:
                linear-gradient(
                    180deg,
                    rgba(255, 255, 255, 0.98),
                    rgba(247, 248, 253, 0.98)
                );
            border-right: 1px solid var(--border);
        }

        [data-testid="stSidebarContent"] {
            padding-top: 1.5rem;
        }

        .gmail-hero {
            position: relative;
            overflow: hidden;
            padding: 38px;
            margin-bottom: 24px;
            border: 1px solid rgba(101, 88, 232, 0.16);
            border-radius: 28px;
            background:
                linear-gradient(
                    135deg,
                    rgba(255, 255, 255, 0.97),
                    rgba(247, 246, 255, 0.96)
                );
            box-shadow:
                0 22px 65px rgba(36, 42, 72, 0.11);
        }

        .gmail-hero::before {
            content: "";
            position: absolute;
            width: 280px;
            height: 280px;
            right: -95px;
            top: -140px;
            border-radius: 50%;
            background:
                linear-gradient(
                    135deg,
                    rgba(101, 88, 232, 0.28),
                    rgba(139, 92, 246, 0.06)
                );
        }

        .gmail-hero::after {
            content: "";
            position: absolute;
            width: 180px;
            height: 180px;
            right: 120px;
            bottom: -140px;
            border-radius: 50%;
            background: rgba(14, 165, 233, 0.10);
        }

        .hero-heading {
            position: relative;
            z-index: 2;
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .hero-logo {
            display: grid;
            place-items: center;
            width: 55px;
            height: 55px;
            flex-shrink: 0;
            border-radius: 18px;
            color: white;
            font-size: 26px;
            background:
                linear-gradient(
                    135deg,
                    var(--primary),
                    var(--purple)
                );
            box-shadow:
                0 15px 32px rgba(101, 88, 232, 0.32);
        }

        .hero-eyebrow {
            margin-bottom: 7px;
            color: var(--primary);
            font-size: 11px;
            font-weight: 850;
            letter-spacing: 0.14em;
            text-transform: uppercase;
        }

        .gmail-hero h1 {
            margin: 0;
            color: var(--text);
            font-size: clamp(2.1rem, 4vw, 3.2rem);
            line-height: 1;
            font-weight: 850;
            letter-spacing: -0.055em;
        }

        .hero-description {
            position: relative;
            z-index: 2;
            max-width: 750px;
            margin: 21px 0 0;
            color: var(--muted);
            font-size: 1rem;
            line-height: 1.75;
        }

        .badge-row {
            position: relative;
            z-index: 2;
            display: flex;
            flex-wrap: wrap;
            gap: 9px;
            margin-top: 23px;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            padding: 8px 12px;
            border: 1px solid rgba(101, 88, 232, 0.14);
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.84);
            color: #4c536d;
            font-size: 12px;
            font-weight: 750;
            backdrop-filter: blur(8px);
        }

        .online-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--green);
            box-shadow:
                0 0 0 4px rgba(22, 163, 74, 0.10);
        }

        .section-heading {
            margin: 29px 0 13px;
            color: #535b75;
            font-size: 11px;
            font-weight: 850;
            letter-spacing: 0.13em;
            text-transform: uppercase;
        }

        .dashboard-card {
            height: 100%;
            min-height: 135px;
            padding: 22px;
            border: 1px solid var(--border);
            border-radius: 21px;
            background: var(--surface);
            box-shadow:
                0 12px 36px rgba(40, 46, 73, 0.06);
            transition:
                transform 160ms ease,
                box-shadow 160ms ease;
        }

        .dashboard-card:hover {
            transform: translateY(-3px);
            box-shadow:
                0 19px 46px rgba(40, 46, 73, 0.10);
        }

        .card-icon {
            margin-bottom: 13px;
            font-size: 25px;
        }

        .card-title {
            margin-bottom: 7px;
            color: var(--text);
            font-size: 16px;
            font-weight: 820;
            letter-spacing: -0.02em;
        }

        .card-description {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.6;
        }

        .feature-card {
            min-height: 170px;
        }

        [data-testid="stChatMessage"] {
            margin-bottom: 0.9rem;
            padding: 1.1rem 1.2rem;
            border: 1px solid var(--border);
            border-radius: 21px;
            background: rgba(255, 255, 255, 0.89);
            box-shadow:
                0 12px 34px rgba(40, 46, 73, 0.055);
            backdrop-filter: blur(10px);
        }

        [data-testid="stChatMessage"]
        [data-testid="stMarkdownContainer"] {
            color: var(--text);
            line-height: 1.7;
        }

        [data-testid="stChatMessageAvatarUser"],
        [data-testid="stChatMessageAvatarAssistant"] {
            border-radius: 14px;
            box-shadow:
                0 8px 20px rgba(42, 48, 78, 0.13);
        }

        [data-testid="stChatInput"] {
            border: 1px solid rgba(101, 88, 232, 0.20);
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.97);
            box-shadow:
                0 18px 55px rgba(38, 43, 70, 0.16);
        }

        [data-testid="stChatInput"] textarea {
            color: var(--text);
        }

        .draft-heading {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            margin-bottom: 9px;
            color: var(--primary);
            font-size: 11px;
            font-weight: 850;
            letter-spacing: 0.10em;
            text-transform: uppercase;
        }

        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color:
                rgba(101, 88, 232, 0.20) !important;
            border-radius: 18px !important;
            background:
                linear-gradient(
                    180deg,
                    rgba(249, 249, 255, 0.96),
                    rgba(255, 255, 255, 0.96)
                );
            box-shadow:
                inset 4px 0 0 rgba(101, 88, 232, 0.83);
        }

        .stButton > button {
            min-height: 43px;
            border: 1px solid rgba(101, 88, 232, 0.16);
            border-radius: 13px;
            background: rgba(255, 255, 255, 0.90);
            color: #414961;
            font-weight: 700;
            box-shadow:
                0 7px 20px rgba(40, 46, 73, 0.05);
            transition:
                transform 140ms ease,
                box-shadow 140ms ease,
                border-color 140ms ease;
        }

        .stButton > button:hover {
            border-color: rgba(101, 88, 232, 0.52);
            color: var(--primary);
            transform: translateY(-1px);
            box-shadow:
                0 12px 27px rgba(101, 88, 232, 0.14);
        }

        .sidebar-brand {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 21px;
        }

        .sidebar-logo {
            display: grid;
            place-items: center;
            width: 45px;
            height: 45px;
            flex-shrink: 0;
            border-radius: 15px;
            color: white;
            font-size: 21px;
            background:
                linear-gradient(
                    135deg,
                    var(--primary),
                    var(--purple)
                );
            box-shadow:
                0 11px 26px rgba(101, 88, 232, 0.27);
        }

        .sidebar-title {
            color: var(--text);
            font-size: 17px;
            font-weight: 850;
            letter-spacing: -0.025em;
        }

        .sidebar-subtitle {
            margin-top: 3px;
            color: var(--muted);
            font-size: 11px;
        }

        .connection-card {
            margin-bottom: 19px;
            padding: 16px;
            border: 1px solid var(--border);
            border-radius: 17px;
            background: rgba(255, 255, 255, 0.86);
            box-shadow:
                0 9px 25px rgba(40, 46, 73, 0.05);
        }

        .connection-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }

        .connection-title {
            color: var(--text);
            font-size: 13px;
            font-weight: 820;
        }

        .connection-state {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            font-weight: 820;
        }

        .connection-dot {
            display: inline-block;
            width: 7px;
            height: 7px;
            border-radius: 50%;
        }

        .connection-copy {
            margin-top: 9px;
            color: var(--muted);
            font-size: 11px;
            line-height: 1.55;
        }

        .tool-pill {
            display: inline-block;
            margin: 3px 3px 0 0;
            padding: 5px 8px;
            border-radius: 999px;
            background: rgba(101, 88, 232, 0.09);
            color: #5650bb;
            font-size: 10px;
            font-weight: 760;
        }

        .privacy-note {
            margin-top: 33px;
            color: #898fa2;
            text-align: center;
            font-size: 11px;
        }

        #MainMenu,
        footer {
            visibility: hidden;
        }

        @media (max-width: 760px) {
            [data-testid="stMainBlockContainer"] {
                padding-top: 1rem;
            }

            .gmail-hero {
                padding: 25px 22px;
                border-radius: 22px;
            }

            .gmail-hero h1 {
                font-size: 2.05rem;
            }

            .hero-heading {
                align-items: flex-start;
            }

            .dashboard-card {
                min-height: auto;
            }
        }
        </style>
        """
    )


# ============================================================
# RESPONSE HELPERS
# ============================================================

def normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        output: list[str] = []

        for block in content:
            if isinstance(block, str):
                output.append(block)

            elif isinstance(block, dict):
                block_text = (
                    block.get("text")
                    or block.get("content")
                )

                if block_text:
                    output.append(str(block_text))

        return "\n\n".join(output)

    return str(content)


def looks_like_email_draft(content: str) -> bool:
    normalized = content.lower()

    has_to = (
        "**to:**" in normalized
        or normalized.strip().startswith("to:")
    )

    has_subject = (
        "**subject:**" in normalized
        or "\nsubject:" in normalized
    )

    has_body = (
        "**body:**" in normalized
        or "\nbody:" in normalized
    )

    return has_to and has_subject and has_body


def render_assistant_content(content: str) -> None:
    if looks_like_email_draft(content):
        with st.container(border=True):
            render_html(
                """
                <div class="draft-heading">
                    ✉️ Email draft · awaiting confirmation
                </div>
                """
            )

            st.markdown(content)

            st.caption(
                "This email will not be sent until you explicitly confirm."
            )

    else:
        st.markdown(content)


def render_message(message: dict[str, Any]) -> None:
    role = message["role"]
    avatar = USER_AVATAR if role == "user" else ASSISTANT_AVATAR

    with st.chat_message(role, avatar=avatar):
        if message.get("kind") == "error":
            st.error(message["content"])
            detail = message.get("detail", "")
            if detail:
                with st.expander("Technical details"):
                    st.code(detail)
        elif role == "assistant":
            render_assistant_content(message["content"])
        else:
            st.markdown(message["content"])


def queue_prompt(prompt: str) -> None:
    st.session_state.pending_prompt = prompt


# ============================================================
# SIDEBAR
# ============================================================

def render_sidebar() -> str:
    with st.sidebar:
        render_html(
            """
            <div class="sidebar-brand">
                <div class="sidebar-logo">✦</div>
                <div>
                    <div class="sidebar-title">Gmail MCP</div>
                    <div class="sidebar-subtitle">
                        Intelligent email workspace
                    </div>
                </div>
            </div>
            """
        )

        token_exists = Path("token.json").exists()
        credentials_exist = Path("credentials.json").exists()
        gmail_ready = token_exists and credentials_exist

        if gmail_ready:
            status = "Ready"
            status_color = "#15803d"
            dot_color = "#16a34a"
        else:
            status = "Setup needed"
            status_color = "#b45309"
            dot_color = "#f59e0b"

        render_html(
            f"""
            <div class="connection-card">
                <div class="connection-header">
                    <div class="connection-title">
                        Gmail connection
                    </div>
                    <div
                        class="connection-state"
                        style="color: {status_color};"
                    >
                        <span
                            class="connection-dot"
                            style="background: {dot_color};"
                        ></span>
                        {status}
                    </div>
                </div>
                <div class="connection-copy">
                    Gmail access is handled locally through OAuth
                    and your MCP server.
                </div>
            </div>
            """
        )

        st.markdown("##### AI model")

        selected_label = st.selectbox(
            "Choose AI model",
            options=list(MODEL_OPTIONS.keys()),
            key="selected_model",
            label_visibility="collapsed",
        )

        st.caption(
            "Change models without changing your Gmail tools."
        )

        st.markdown("##### Quick actions")

        for label, prompt in QUICK_ACTIONS.items():
            if st.button(
                label,
                key=f"sidebar_{label}",
                use_container_width=True,
            ):
                queue_prompt(prompt)

        st.markdown("##### MCP tools")

        if st.session_state.tool_names:
            pills = "".join(
                (
                    '<span class="tool-pill">'
                    f"{html.escape(tool_name)}"
                    "</span>"
                )
                for tool_name in st.session_state.tool_names
            )

            render_html(pills)

        else:
            st.caption(
                "Tools will appear after the first request."
            )

        st.markdown("")

        if st.button(
            "🗑️ Clear conversation",
            use_container_width=True,
        ):
            st.session_state.messages = []
            st.session_state.agent_messages = []
            st.session_state.pending_prompt = None
            st.session_state.tool_names = []
            st.session_state.last_error = ""
            st.rerun()

        render_html(
            """
            <div class="privacy-note">
                Streamlit · Gmail API · MCP · SAP AI Core
            </div>
            """
        )

    return MODEL_OPTIONS[selected_label]


# ============================================================
# MAIN UI
# ============================================================

def render_hero(model_key: str) -> None:
    model_label = next(
        label
        for label, key in MODEL_OPTIONS.items()
        if key == model_key
    )

    render_html(
        f"""
        <div class="gmail-hero">
            <div class="hero-heading">
                <div class="hero-logo">✦</div>
                <div>
                    <div class="hero-eyebrow">
                        Intelligent email operations
                    </div>
                    <h1>Gmail MCP Assistant</h1>
                </div>
            </div>

            <p class="hero-description">
                Search your inbox, understand long conversations,
                prepare professional drafts, and continue existing
                Gmail threads from one secure AI workspace.
            </p>

            <div class="badge-row">
                <span class="status-badge">
                    <span class="online-dot"></span>
                    Gmail OAuth ready
                </span>

                <span class="status-badge">
                    ⚡ MCP tools enabled
                </span>

                <span class="status-badge">
                    🧠 {html.escape(model_label)} active
                </span>

                <span class="status-badge">
                    🛡️ Confirmation before sending
                </span>
            </div>
        </div>
        """
    )

    columns = st.columns(3)

    cards = [
        (
            columns[0],
            "🔎",
            "Search and understand",
            "Find messages, inspect conversations, and summarize your inbox.",
        ),
        (
            columns[1],
            "✍️",
            "Draft with confidence",
            "Review and revise every outbound email before it is sent.",
        ),
        (
            columns[2],
            "🧵",
            "Reply in context",
            "Continue conversations inside their original Gmail thread.",
        ),
    ]

    for column, icon, title, description in cards:
        with column:
            render_html(
                f"""
                <div class="dashboard-card">
                    <div class="card-icon">{icon}</div>
                    <div class="card-title">{title}</div>
                    <div class="card-description">
                        {description}
                    </div>
                </div>
                """
            )


def render_empty_state() -> None:
    render_html(
        """
        <div class="section-heading">
            What you can do
        </div>
        """
    )

    columns = st.columns(3)

    features = [
        (
            columns[0],
            "📥",
            "Inbox intelligence",
            (
                "Find recent, unread, important, or sender-specific "
                "emails and receive concise summaries."
            ),
        ),
        (
            columns[1],
            "📝",
            "Professional drafting",
            (
                "Turn a simple instruction into a polished email "
                "with a mandatory approval step."
            ),
        ),
        (
            columns[2],
            "↩️",
            "Thread-aware replies",
            (
                "Continue an existing conversation while preserving "
                "its Gmail subject and thread."
            ),
        ),
    ]

    for column, icon, title, description in features:
        with column:
            render_html(
                f"""
                <div class="dashboard-card feature-card">
                    <div class="card-icon">{icon}</div>
                    <div class="card-title">{title}</div>
                    <div class="card-description">
                        {description}
                    </div>
                </div>
                """
            )

    render_html(
        """
        <div class="section-heading">
            Try a prompt
        </div>
        """
    )

    button_columns = st.columns(2)

    for index, (label, prompt) in enumerate(QUICK_ACTIONS.items()):
        with button_columns[index % 2]:
            if st.button(
                label,
                key=f"main_{label}",
                use_container_width=True,
            ):
                queue_prompt(prompt)


# ============================================================
# MCP AGENT
# ============================================================

def is_explicit_send_confirmation(text: str) -> bool:
    """
    Return True when the user clearly approves a previously displayed
    email draft.

    Plain responses such as "yes" are accepted only when an assistant
    message in the conversation contains an email draft or asks for
    confirmation to send.
    """

    normalized = re.sub(
        r"\s+",
        " ",
        text.strip().lower(),
    ).strip(".! ")

    confirmation_patterns = [
        r"yes",
        r"yes please",
        r"send it(?: now)?",
        r"send this(?: email| draft)?(?: now)?",
        r"send the (?:email|draft)(?: now)?",
        r"yes[, ]+(?:please )?send(?: it| this| the email| the draft)?(?: now)?",
        r"please send it(?: now)?",
        r"go ahead(?: and send(?: it| this| the email| the draft)?)?",
        r"proceed(?: and send(?: it| this| the email| the draft)?)?",
    ]

    confirmation_detected = any(
        re.fullmatch(pattern, normalized)
        for pattern in confirmation_patterns
    )

    if not confirmation_detected:
        return False

    # Make sure the confirmation relates to a previously prepared draft.
    for message in reversed(st.session_state.messages):
        if message.get("role") != "assistant":
            continue

        content = message.get("content", "")
        content_lower = content.lower()

        draft_was_displayed = looks_like_email_draft(content)

        send_confirmation_was_requested = (
            "would you like me to send this" in content_lower
            or "would you like me to send it" in content_lower
            or "would you like me to send" in content_lower
            or "awaiting confirmation" in content_lower
        )

        return (
            draft_was_displayed
            or send_confirmation_was_requested
        )

    return False


def _last_assistant_text(messages: list[BaseMessage]) -> str:
    """Extract a final answer even when a provider returns content blocks."""

    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = normalize_content(message.content).strip()
            if text:
                return text

    # Defensive fallback: if the model ended immediately after a successful tool,
    # turn the tool's JSON response into a useful confirmation.
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            raw = normalize_content(message.content).strip()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            if payload.get("status") == "sent":
                recipient = payload.get("to", "the recipient")
                subject = payload.get("subject", "")
                subject_text = f' with subject "{subject}"' if subject else ""
                return f"The email was sent to **{recipient}**{subject_text}."

    return "The request completed, but the model returned no displayable text."


async def execute_agent(
    history: list[BaseMessage],
    question: str,
    model_key: str,
    allow_send_tools: bool,
) -> tuple[str, list[str], list[BaseMessage]]:
    """Run one complete LangChain/MCP turn and return the updated agent state."""

    if not MCP_SERVER_PATH.is_file():
        raise FileNotFoundError(f"MCP server not found: {MCP_SERVER_PATH}")

    client = MultiServerMCPClient(
        {
            "gmail": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(MCP_SERVER_PATH)],
            }
        }
    )

    all_tools = await client.get_tools()
    tool_names = [tool.name for tool in all_tools]

    # This is a real safety gate, not merely a prompt instruction. Sending tools
    # are physically unavailable to the agent until the user explicitly approves
    # the already displayed draft.
    active_tools = [
        tool
        for tool in all_tools
        if allow_send_tools or tool.name not in SEND_TOOL_NAMES
    ]

    model = get_model(model_key)
    agent = create_agent(
        model,
        active_tools,
        system_prompt=EMAIL_AGENT_SYSTEM_PROMPT,
    )

    input_messages: list[BaseMessage] = [
        *history,
        HumanMessage(content=question),
    ]

    result = await agent.ainvoke(
        {"messages": input_messages},
        config={"recursion_limit": 25},
    )

    updated_messages = list(result["messages"])
    answer = _last_assistant_text(updated_messages)
    return answer, tool_names, updated_messages


def process_question(question: str, model_key: str) -> None:
    """Execute a user request, persist all state, and rerun the clean UI."""

    question = question.strip()
    if not question:
        return

    st.session_state.messages.append(
        {
            "role": "user",
            "content": question,
        }
    )

    allow_send_tools = is_explicit_send_confirmation(question)

    with st.spinner("Connecting to Gmail and processing your request..."):
        try:
            answer, tool_names, updated_agent_messages = asyncio.run(
                execute_agent(
                    history=list(st.session_state.agent_messages),
                    question=question,
                    model_key=model_key,
                    allow_send_tools=allow_send_tools,
                )
            )

            st.session_state.agent_messages = updated_agent_messages
            st.session_state.tool_names = tool_names
            st.session_state.last_error = ""
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": answer,
                }
            )

        except Exception as error:
            technical_detail = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            st.session_state.last_error = technical_detail
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "kind": "error",
                    "content": (
                        "I could not complete that Gmail request. "
                        "Open the technical details below, correct the configuration, "
                        "and try again."
                    ),
                    "detail": technical_detail,
                }
            )

    st.rerun()


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

def main() -> None:
    configure_page()
    initialize_state()
    inject_css()

    model_key = render_sidebar()

    render_hero(model_key)

    if st.session_state.messages:
        render_html(
            """
            <div class="section-heading">
                Conversation
            </div>
            """
        )

        for message in st.session_state.messages:
            render_message(message)

    else:
        render_empty_state()

    question = st.chat_input(
        "Ask about your inbox, draft an email, "
        "or reply to a conversation..."
    )

    if not question and st.session_state.pending_prompt:
        question = st.session_state.pending_prompt
        st.session_state.pending_prompt = None

    if question:
        process_question(
            question=question,
            model_key=model_key,
        )

    render_html(
        """
        <div class="privacy-note">
            Outbound emails require your explicit confirmation before sending.
        </div>
        """
    )


if __name__ == "__main__":
    main()
