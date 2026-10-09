"""Markdown projection of a ChatGPT conversation JSON.

Adapted from brianjlacy/export-chatgpt `lib/formatter.js` (MIT).
Source-faithful JSON is stored separately; this markdown exists so
downstream search / RAG / grep workflows can index the conversation.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _format_date(timestamp: Any) -> str:
    from nisaba.timeutil import to_iso_utc
    iso = to_iso_utc(timestamp)
    return iso or "unknown"


def _escape_yaml(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def extract_messages_in_order(conversation: Dict[str, Any]) -> List[Dict[str, Any]]:
    mapping = conversation.get("mapping") or {}
    if not mapping:
        return []
    root_id = None
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        if not node.get("parent"):
            root_id = node_id
            break
    if not root_id:
        return []
    messages: List[Dict[str, Any]] = []
    seen = set()
    node_id: Any = root_id
    while node_id:
        if node_id in seen:
            break
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        msg = node.get("message")
        if isinstance(msg, dict) and msg.get("content"):
            messages.append(msg)
        children = node.get("children") or []
        node_id = children[0] if children else None
    return messages


def _extract_message_content(message: Dict[str, Any]) -> str:
    content = message.get("content")
    metadata = message.get("metadata") or {}
    if metadata.get("is_visually_hidden_from_conversation"):
        return ""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    ctype = content.get("content_type")
    if ctype == "text" and content.get("parts"):
        return "\n".join(p for p in content["parts"] if isinstance(p, str))
    if ctype == "code" and content.get("text"):
        return "```\n" + str(content["text"]) + "\n```"
    if ctype == "multimodal_text" and content.get("parts"):
        parts: List[str] = []
        for part in content["parts"]:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
                pointer = str(part.get("asset_pointer") or "")
                file_id = pointer.replace("sediment://", "").replace("file-service://", "")
                parts.append(f"[Image: {file_id}]" if file_id else "[Image]")
        return "\n".join(parts)
    if ctype == "tether_browsing_display":
        text = "\n".join(p for p in (content.get("parts") or []) if isinstance(p, str))
        if text.strip():
            return "> **Browsing Result:**\n>\n> " + text.replace("\n", "\n> ")
        return ""
    if ctype == "thoughts":
        text = "\n".join(p for p in (content.get("parts") or []) if isinstance(p, str))
        return text
    if ctype == "reasoning_recap":
        text = "\n".join(p for p in (content.get("parts") or []) if isinstance(p, str))
        return f"*Reasoning recap: {text}*" if text.strip() else ""
    if ctype == "model_editable_context":
        return ""
    keys = sorted(str(k) for k in content.keys())
    return (
        f"[unrendered ChatGPT content_type={ctype!r}; "
        f"keys={keys}; preserved in conversation.json]"
    )


def extract_file_references(conversation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect attachment / image / canvas pointers from mapping."""
    files: List[Dict[str, Any]] = []
    mapping = conversation.get("mapping") or {}
    conv_id = conversation.get("id") or conversation.get("conversation_id") or ""
    seen = set()
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message") or {}
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") if isinstance(msg, dict) else None
        ctype = content.get("content_type") if isinstance(content, dict) else None
        if isinstance(content, dict) and ctype == "multimodal_text":
            for part in content.get("parts") or []:
                if not isinstance(part, dict) or not part.get("asset_pointer"):
                    continue
                pointer = str(part["asset_pointer"])
                file_id = pointer.replace("sediment://", "").replace("file-service://", "")
                if not file_id or file_id in seen:
                    continue
                seen.add(file_id)
                ptype = "attachment"
                if part.get("content_type") == "image_asset_pointer":
                    ptype = "image"
                elif part.get("content_type") in ("canvas_asset_pointer", "canvas"):
                    ptype = "canvas"
                files.append({
                    "file_id": file_id,
                    "conversation_id": conv_id,
                    "type": ptype,
                    "metadata": part.get("metadata") or {},
                    "size_bytes": part.get("size_bytes"),
                })
        if isinstance(content, dict) and ctype in ("canvas", "canvas_asset_pointer") and content.get("asset_pointer"):
            pointer = str(content["asset_pointer"])
            file_id = pointer.replace("sediment://", "").replace("file-service://", "")
            if file_id and file_id not in seen:
                seen.add(file_id)
                files.append({
                    "file_id": file_id,
                    "conversation_id": conv_id,
                    "type": "canvas",
                    "metadata": content.get("metadata") or {},
                    "size_bytes": content.get("size_bytes"),
                })
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            for part in content["parts"]:
                if not isinstance(part, dict):
                    continue
                fid = str(
                    part.get("file_id")
                    or part.get("id")
                    or ""
                )
                pointer = str(part.get("asset_pointer") or "")
                if pointer:
                    fid = fid or pointer.replace("sediment://", "").replace("file-service://", "")
                if fid and fid not in seen:
                    seen.add(fid)
                    files.append({
                        "file_id": fid,
                        "conversation_id": conv_id,
                        "type": part.get("content_type") or "part_file",
                        "metadata": part.get("metadata") or {},
                        "size_bytes": part.get("size_bytes"),
                    })
        md = msg.get("metadata") if isinstance(msg, dict) else {}
        if isinstance(md, dict):
            for att in md.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                fid = str(att.get("id") or att.get("file_id") or "")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                files.append({
                    "file_id": fid,
                    "conversation_id": conv_id,
                    "type": att.get("mime_type") or att.get("type") or "attachment",
                    "metadata": att,
                    "size_bytes": att.get("size") or att.get("size_bytes"),
                    "name": att.get("name"),
                })
    return files


def conversation_to_markdown(conversation: Dict[str, Any]) -> str:
    title = conversation.get("title") or "Untitled"
    conv_id = conversation.get("id") or conversation.get("conversation_id") or ""
    lines = [
        "---",
        f'title: "{_escape_yaml(title)}"',
        f"id: {conv_id}",
        f"create_time: {_format_date(conversation.get('create_time'))}",
        f"update_time: {_format_date(conversation.get('update_time'))}",
    ]
    if conversation.get("model"):
        lines.append(f"model: {conversation['model']}")
    gizmo = conversation.get("gizmo_id") or conversation.get("project_id")
    if gizmo:
        lines.append(f"project_id: {gizmo}")
    lines.extend(["---", "", f"# {title}", ""])

    for msg in extract_messages_in_order(conversation):
        role = (msg.get("author") or {}).get("role") or "unknown"
        metadata = msg.get("metadata") or {}
        if metadata.get("is_async_task_result_message"):
            task_title = metadata.get("async_task_title") or "Research Result"
            lines.append(f"## Assistant (Deep Research: {task_title})")
            lines.append("")
            content = _extract_message_content(msg)
            if content.strip():
                lines.append(content)
                lines.append("")
            continue
        content = _extract_message_content(msg)
        if not content.strip():
            continue
        heading = {"user": "User", "assistant": "Assistant", "system": "System"}.get(
            role, (role or "unknown").replace("_", " ").title()
        )
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
