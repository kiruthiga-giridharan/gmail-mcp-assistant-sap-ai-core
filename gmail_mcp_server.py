from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from mcp.server.fastmcp import FastMCP


PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = PROJECT_DIR / "token.json"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
HTTP_TIMEOUT = httpx.Timeout(45.0, connect=15.0)

mcp = FastMCP("gmail")


class _HTMLTextExtractor(HTMLParser):
    """Very small HTML-to-text helper used when a message has no text/plain part."""

    BLOCK_TAGS = {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "table",
        "section",
        "article",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _load_credentials() -> Credentials:
    """Load Gmail OAuth credentials, refresh them when needed, and persist refreshes."""

    if not TOKEN_FILE.is_file():
        raise FileNotFoundError(
            f"Gmail token file was not found at {TOKEN_FILE}. Run auth.py first."
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))

    if creds.expired:
        if not creds.refresh_token:
            raise RuntimeError(
                "The Gmail access token has expired and no refresh token is available. "
                "Run auth.py again."
            )

        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    if not creds.valid or not creds.token:
        raise RuntimeError("The Gmail OAuth credentials are not valid. Run auth.py again.")

    return creds


@contextmanager
def _client() -> Iterator[httpx.Client]:
    """Create a short-lived authenticated Gmail HTTP client."""

    creds = _load_credentials()
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Accept": "application/json",
    }

    with httpx.Client(
        headers=headers,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    ) as client:
        yield client


def _raise_for_gmail_error(response: httpx.Response) -> None:
    """Raise an informative error when Gmail returns a non-success response."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:1500]
        raise RuntimeError(
            f"Gmail API request failed with HTTP {response.status_code}: {detail}"
        ) from exc


def _headers(payload: dict) -> dict[str, str]:
    return {
        item.get("name", ""): item.get("value", "")
        for item in payload.get("headers", [])
        if item.get("name")
    }


def _get_header(headers: dict[str, str], name: str) -> str:
    """Perform a case-insensitive email-header lookup."""

    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def _decode_base64url(data: str) -> bytes:
    if not data:
        return b""
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _is_attachment(part: dict) -> bool:
    if part.get("filename"):
        return True

    headers = _headers(part)
    disposition = _get_header(headers, "Content-Disposition").lower()
    return disposition.startswith("attachment")


def _collect_body_parts(payload: dict, mime_type: str) -> list[str]:
    """Recursively collect non-attachment body parts of a requested MIME type."""

    collected: list[str] = []

    if not payload or _is_attachment(payload):
        return collected

    if payload.get("mimeType") == mime_type:
        encoded = payload.get("body", {}).get("data", "")
        if encoded:
            charset = "utf-8"
            content_type = _get_header(_headers(payload), "Content-Type")
            match = re.search(r"charset=[\"']?([^;\"']+)", content_type, re.I)
            if match:
                charset = match.group(1).strip()

            raw = _decode_base64url(encoded)
            try:
                collected.append(raw.decode(charset, errors="replace"))
            except LookupError:
                collected.append(raw.decode("utf-8", errors="replace"))

    for child in payload.get("parts", []) or []:
        collected.extend(_collect_body_parts(child, mime_type))

    return collected


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _extract_message_body(payload: dict) -> str:
    """Extract readable text from nested Gmail MIME payloads."""

    plain_parts = _collect_body_parts(payload, "text/plain")
    if plain_parts:
        return "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()

    html_parts = _collect_body_parts(payload, "text/html")
    if html_parts:
        return "\n\n".join(
            _html_to_text(part) for part in html_parts if part.strip()
        ).strip()

    return ""


_QUOTE_BOUNDARIES = [
    re.compile(r"^On .+ wrote:\s*$", re.I),
    re.compile(r"^-+\s*Original Message\s*-+$", re.I),
    re.compile(r"^From:\s+.+$", re.I),
    re.compile(r"^Sent:\s+.+$", re.I),
]


def _strip_existing_quote(body: str) -> str:
    """Keep only the newly authored portion of a message to avoid quote explosion."""

    output: list[str] = []

    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()

        if stripped.startswith(">"):
            break

        if any(pattern.match(stripped) for pattern in _QUOTE_BOUNDARIES):
            break

        output.append(line.rstrip())

    while output and not output[-1].strip():
        output.pop()

    cleaned = "\n".join(output).strip()
    return cleaned or body.strip()


def _message_timestamp(message: dict) -> int:
    try:
        return int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return 0


def _fetch_thread(client: httpx.Client, thread_id: str, *, full: bool) -> dict:
    params: dict[str, object] = {"format": "full" if full else "metadata"}

    if not full:
        params["metadataHeaders"] = [
            "From",
            "To",
            "Cc",
            "Subject",
            "Date",
            "Message-ID",
            "References",
        ]

    response = client.get(f"{GMAIL_BASE}/threads/{thread_id}", params=params)
    _raise_for_gmail_error(response)
    return response.json()


def _thread_quote(thread: dict, history_limit: int) -> tuple[str, str]:
    """Build plain-text and HTML versions of the prior thread history."""

    messages = sorted(thread.get("messages", []), key=_message_timestamp)
    if history_limit > 0:
        messages = messages[-history_limit:]

    plain_sections: list[str] = []
    html_sections: list[str] = []

    for message in messages:
        headers = _headers(message.get("payload", {}))
        sender = _get_header(headers, "From") or "Unknown sender"
        date = _get_header(headers, "Date") or "an earlier date"
        body = _strip_existing_quote(
            _extract_message_body(message.get("payload", {}))
        )

        if not body:
            body = message.get("snippet", "").strip()

        quoted_lines = "\n".join(
            f"> {line}" if line else ">"
            for line in body.splitlines()
        )
        plain_sections.append(f"On {date}, {sender} wrote:\n{quoted_lines}")

        safe_body = html.escape(body).replace("\n", "<br>")
        html_sections.append(
            '<div class="gmail_quote" style="margin-top:16px">'
            f"<div>On {html.escape(date)}, {html.escape(sender)} wrote:</div>"
            '<blockquote style="margin:8px 0 0 12px;padding-left:12px;'
            'border-left:1px solid #cccccc">'
            f"{safe_body}"
            "</blockquote></div>"
        )

    plain = "\n\n".join(plain_sections)
    html_value = "".join(html_sections)
    return plain, html_value


def _parse_addresses(value: str) -> list[tuple[str, str]]:
    return [(name, address) for name, address in getaddresses([value]) if address]


def _contains_address(value: str, wanted: str) -> bool:
    wanted_lower = wanted.casefold()
    return any(address.casefold() == wanted_lower for _, address in _parse_addresses(value))


def _remove_address(value: str, unwanted: str) -> str:
    unwanted_lower = unwanted.casefold()
    remaining = [
        (name, address)
        for name, address in _parse_addresses(value)
        if address.casefold() != unwanted_lower
    ]
    return ", ".join(formataddr(pair) for pair in remaining)


def _get_own_email(client: httpx.Client) -> str:
    response = client.get(f"{GMAIL_BASE}/profile")
    _raise_for_gmail_error(response)
    return response.json().get("emailAddress", "")


def _build_mime_message(
    to: str,
    subject: str,
    body: str,
    *,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    attachments: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """Build and Base64URL-encode an RFC-compliant MIME email."""

    if not to.strip():
        raise ValueError("At least one recipient is required.")

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject

    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    message.set_content(body)

    if html_body:
        message.add_alternative(html_body, subtype="html")

    for item in attachments or []:
        path = Path(item).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")

        guessed_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (guessed_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _send_reply(
    *,
    thread_id: str,
    body: str,
    html_body: str = "",
    attachments: list[str] | None = None,
    reply_all: bool = False,
    include_thread_history: bool = True,
    history_limit: int = 50,
) -> str:
    """Internal implementation shared by reply_to_thread and reply_to_message."""

    with _client() as client:
        own_email = _get_own_email(client)
        thread = _fetch_thread(client, thread_id, full=True)
        messages = sorted(thread.get("messages", []), key=_message_timestamp)

        if not messages:
            raise ValueError(f"No messages were found in Gmail thread {thread_id}.")

        latest = messages[-1]
        latest_headers = _headers(latest.get("payload", {}))

        original_from = _get_header(latest_headers, "From")
        original_to = _get_header(latest_headers, "To")
        original_cc = _get_header(latest_headers, "Cc")
        original_subject = _get_header(latest_headers, "Subject")
        original_message_id = _get_header(latest_headers, "Message-ID")
        original_references = _get_header(latest_headers, "References")

        sent_by_me = _contains_address(original_from, own_email)

        if sent_by_me:
            to = _remove_address(original_to, own_email) or original_to
            cc = _remove_address(original_cc, own_email) if reply_all else ""
        else:
            to = original_from
            if reply_all:
                combined = ", ".join(filter(None, [original_to, original_cc]))
                cc = _remove_address(combined, own_email)
            else:
                cc = ""

        # Gmail requires the subject to match when inserting a message into a thread.
        subject = original_subject

        reference_items = original_references.split()
        if original_message_id and original_message_id not in reference_items:
            reference_items.append(original_message_id)
        references = " ".join(reference_items)

        full_body = body.rstrip()
        full_html = html_body.rstrip()

        if include_thread_history:
            quoted_plain, quoted_html = _thread_quote(thread, max(1, history_limit))
            if quoted_plain:
                full_body = f"{full_body}\n\n{quoted_plain}"
            if full_html and quoted_html:
                full_html = f"{full_html}{quoted_html}"

        raw = _build_mime_message(
            to=to,
            subject=subject,
            body=full_body,
            cc=cc,
            html_body=full_html,
            attachments=attachments,
            in_reply_to=original_message_id,
            references=references,
        )

        response = client.post(
            f"{GMAIL_BASE}/messages/send",
            headers={"Content-Type": "application/json"},
            json={"raw": raw, "threadId": thread_id},
        )
        _raise_for_gmail_error(response)
        data = response.json()

    return json.dumps(
        {
            "message_id": data.get("id"),
            "thread_id": data.get("threadId"),
            "status": "sent",
            "to": to,
            "cc": cc,
            "subject": subject,
            "history_included": include_thread_history,
        },
        indent=2,
    )


@mcp.tool()
def get_profile() -> str:
    """Return the authenticated Gmail account address and mailbox statistics."""

    with _client() as client:
        response = client.get(f"{GMAIL_BASE}/profile")
        _raise_for_gmail_error(response)
        data = response.json()

    return json.dumps(
        {
            "email_address": data.get("emailAddress", ""),
            "messages_total": data.get("messagesTotal", 0),
            "threads_total": data.get("threadsTotal", 0),
        },
        indent=2,
    )


@mcp.tool()
def search_threads(query: str = "in:inbox", page_size: int = 5) -> str:
    """Search Gmail conversations.

    Returns each thread's thread_id and latest_message_id. Use get_thread to read
    the conversation. Use reply_to_thread with thread_id to continue it.
    Gmail search syntax is supported, for example:
    ``to:abc@gmail.com OR from:abc@gmail.com newer_than:30d``.
    """

    page_size = max(1, min(int(page_size), 50))

    with _client() as client:
        response = client.get(
            f"{GMAIL_BASE}/threads",
            params={"q": query, "maxResults": page_size},
        )
        _raise_for_gmail_error(response)
        thread_refs = response.json().get("threads", [])

        results: list[dict] = []

        for ref in thread_refs:
            thread = _fetch_thread(client, ref["id"], full=False)
            messages = sorted(thread.get("messages", []), key=_message_timestamp)
            latest = messages[-1] if messages else {}
            latest_headers = _headers(latest.get("payload", {}))

            participants: list[str] = []
            for message in messages:
                message_headers = _headers(message.get("payload", {}))
                participants.extend(
                    address
                    for _, address in _parse_addresses(
                        ", ".join(
                            filter(
                                None,
                                [
                                    _get_header(message_headers, "From"),
                                    _get_header(message_headers, "To"),
                                    _get_header(message_headers, "Cc"),
                                ],
                            )
                        )
                    )
                )

            results.append(
                {
                    "thread_id": ref["id"],
                    "latest_message_id": latest.get("id", ""),
                    "message_count": len(messages),
                    "from": _get_header(latest_headers, "From"),
                    "to": _get_header(latest_headers, "To"),
                    "cc": _get_header(latest_headers, "Cc"),
                    "subject": _get_header(latest_headers, "Subject"),
                    "date": _get_header(latest_headers, "Date"),
                    "participants": sorted(set(participants)),
                    "snippet": latest.get("snippet", thread.get("snippet", "")),
                }
            )

    return json.dumps(results, indent=2)


@mcp.tool()
def get_thread(
    thread_id: str,
    max_body_chars_per_message: int = 6000,
) -> str:
    """Read all messages in a Gmail thread in chronological order.

    The response includes thread_id, each Gmail message_id, headers, snippet,
    and readable body text. The latest message ID is also returned explicitly.
    """

    max_body_chars_per_message = max(500, min(int(max_body_chars_per_message), 20000))

    with _client() as client:
        thread = _fetch_thread(client, thread_id, full=True)

    messages = sorted(thread.get("messages", []), key=_message_timestamp)
    output_messages: list[dict] = []

    for message in messages:
        headers = _headers(message.get("payload", {}))
        body = _extract_message_body(message.get("payload", {}))
        output_messages.append(
            {
                "message_id": message.get("id", ""),
                "thread_id": message.get("threadId", thread_id),
                "from": _get_header(headers, "From"),
                "to": _get_header(headers, "To"),
                "cc": _get_header(headers, "Cc"),
                "subject": _get_header(headers, "Subject"),
                "date": _get_header(headers, "Date"),
                "snippet": message.get("snippet", ""),
                "body": body[:max_body_chars_per_message],
            }
        )

    return json.dumps(
        {
            "thread_id": thread_id,
            "latest_message_id": messages[-1].get("id", "") if messages else "",
            "message_count": len(messages),
            "messages": output_messages,
        },
        indent=2,
    )


@mcp.tool()
def get_message(message_id: str, max_body_chars: int = 10000) -> str:
    """Read one Gmail message by its Gmail message ID."""

    max_body_chars = max(500, min(int(max_body_chars), 30000))

    with _client() as client:
        response = client.get(
            f"{GMAIL_BASE}/messages/{message_id}",
            params={"format": "full"},
        )
        _raise_for_gmail_error(response)
        data = response.json()

    headers = _headers(data.get("payload", {}))
    body = _extract_message_body(data.get("payload", {}))

    return json.dumps(
        {
            "message_id": message_id,
            "thread_id": data.get("threadId", ""),
            "from": _get_header(headers, "From"),
            "to": _get_header(headers, "To"),
            "cc": _get_header(headers, "Cc"),
            "subject": _get_header(headers, "Subject"),
            "date": _get_header(headers, "Date"),
            "snippet": data.get("snippet", ""),
            "body": body[:max_body_chars],
        },
        indent=2,
    )


@mcp.tool()
def list_labels() -> str:
    """List Gmail labels in the authenticated account."""

    with _client() as client:
        response = client.get(f"{GMAIL_BASE}/labels")
        _raise_for_gmail_error(response)
        labels = response.json().get("labels", [])

    return json.dumps(
        [
            {
                "id": label.get("id", ""),
                "name": label.get("name", ""),
                "type": label.get("type", ""),
            }
            for label in labels
        ],
        indent=2,
    )


@mcp.tool()
def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    attachments: list[str] | None = None,
) -> str:
    """Create, but do not send, a Gmail draft."""

    raw = _build_mime_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html_body=html_body,
        attachments=attachments,
    )

    with _client() as client:
        response = client.post(
            f"{GMAIL_BASE}/drafts",
            headers={"Content-Type": "application/json"},
            json={"message": {"raw": raw}},
        )
        _raise_for_gmail_error(response)
        data = response.json()

    return json.dumps(
        {
            "draft_id": data.get("id"),
            "message_id": data.get("message", {}).get("id"),
            "thread_id": data.get("message", {}).get("threadId"),
            "status": "created",
        },
        indent=2,
    )


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    attachments: list[str] | None = None,
) -> str:
    """Send a new email immediately.

    This starts a new conversation. To continue an existing Gmail conversation,
    use reply_to_thread instead.
    """

    raw = _build_mime_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html_body=html_body,
        attachments=attachments,
    )

    with _client() as client:
        response = client.post(
            f"{GMAIL_BASE}/messages/send",
            headers={"Content-Type": "application/json"},
            json={"raw": raw},
        )
        _raise_for_gmail_error(response)
        data = response.json()

    return json.dumps(
        {
            "message_id": data.get("id"),
            "thread_id": data.get("threadId"),
            "status": "sent",
            "to": to,
            "cc": cc,
            "subject": subject,
        },
        indent=2,
    )


@mcp.tool()
def reply_to_thread(
    thread_id: str,
    body: str,
    html_body: str = "",
    attachments: list[str] | None = None,
    reply_all: bool = False,
    include_thread_history: bool = True,
    history_limit: int = 50,
) -> str:
    """Reply to the latest message in an existing Gmail thread.

    The original thread ID, subject, References, and In-Reply-To headers are
    preserved. By default, readable prior messages are appended as quoted history.
    Obtain thread_id from search_threads or get_thread.
    """

    return _send_reply(
        thread_id=thread_id,
        body=body,
        html_body=html_body,
        attachments=attachments,
        reply_all=reply_all,
        include_thread_history=include_thread_history,
        history_limit=history_limit,
    )


@mcp.tool()
def reply_to_message(
    message_id: str,
    body: str,
    html_body: str = "",
    attachments: list[str] | None = None,
    reply_all: bool = False,
    include_thread_history: bool = True,
    history_limit: int = 50,
) -> str:
    """Reply within the thread that contains a specified Gmail message ID.

    Prefer reply_to_thread when a thread_id is already available.
    """

    with _client() as client:
        response = client.get(
            f"{GMAIL_BASE}/messages/{message_id}",
            params={"format": "minimal"},
        )
        _raise_for_gmail_error(response)
        thread_id = response.json().get("threadId", "")

    if not thread_id:
        raise ValueError(f"No thread ID was found for Gmail message {message_id}.")

    return _send_reply(
        thread_id=thread_id,
        body=body,
        html_body=html_body,
        attachments=attachments,
        reply_all=reply_all,
        include_thread_history=include_thread_history,
        history_limit=history_limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
