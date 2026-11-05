import io
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Set, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.conversation import Conversation


ALLOWED_EXTENSIONS: Set[str] = {".pdf", ".docx", ".txt", ".md", ".markdown"}


def sanitize_filename(filename: str) -> str:
    """
    Sanitize user-provided filename to prevent path traversal and shell injection.
    Only allows legitimate document characters; strips ../ and control characters.
    """
    if not filename:
        return f"attachment_{uuid.uuid4().hex[:8]}"

    # Extract clean basename
    clean = Path(filename).name.strip()
    # Replace dangerous path characters
    clean = clean.replace("/", "_").replace("\\", "_").replace("\x00", "")
    # Remove leading dots or slashes
    clean = re.sub(r"^\.+", "", clean).strip()
    if not clean:
        clean = f"attachment_{uuid.uuid4().hex[:8]}"
    return clean


def validate_file_metadata(filename: str, file_size: int) -> Tuple[str, str]:
    """
    Validate file name, extension whitelist, and size bounds.
    Returns: (sanitized_filename, extension)
    """
    clean_name = sanitize_filename(filename)
    ext = Path(clean_name).suffix.lower()

    if not ext or ext not in ALLOWED_EXTENSIONS:
        allowed_str = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ValueError(
            f"不支持的文件格式 '{ext or '未知'}'。仅支持上传: {allowed_str}"
        )

    if file_size <= 0:
        raise ValueError("上传的文件内容为空 (0 字节)")

    max_bytes = settings.MAX_ATTACHMENT_SIZE_MB * 1024 * 1024
    if file_size > max_bytes:
        raise ValueError(
            f"文件大小 ({file_size / 1024 / 1024:.2f}MB) 超过上限限制 ({settings.MAX_ATTACHMENT_SIZE_MB}MB)"
        )

    return clean_name, ext


def verify_file_content_and_mime(file_bytes: bytes, ext: str) -> str:
    """
    Verify magic bytes and actual file integrity, refusing MIME spoofing.
    Never relies solely on client Content-Type headers.
    Returns verified standard MIME type string.
    """
    if not file_bytes:
        raise ValueError("文件内容为空")

    if ext == ".pdf":
        # PDF magic bytes: %PDF-
        if not file_bytes.startswith(b"%PDF-"):
            raise ValueError("文件格式校验失败：内容不是合法的 PDF 文件 (缺少 %PDF- 魔数)")
        return "application/pdf"

    elif ext == ".docx":
        # DOCX is a ZIP container: PK\x03\x04
        if not file_bytes.startswith(b"PK\x03\x04"):
            raise ValueError("文件格式校验失败：内容不是合法的 DOCX 文件 (缺少 ZIP 魔数)")
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
                names = zf.namelist()
                if "word/document.xml" not in names:
                    raise ValueError("文件格式校验失败：DOCX 文件损坏或缺失 word/document.xml")
        except zipfile.BadZipFile as exc:
            raise ValueError(f"DOCX 文件归档已损坏: {str(exc)}") from exc
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    elif ext in (".txt", ".md", ".markdown"):
        # Text and Markdown: ensure no binary null bytes (disguised binaries)
        if b"\x00" in file_bytes[:4096]:
            raise ValueError("文本文件内容检测包含二进制非法字符，拒绝上传")

        # Verify decodability under standard text encodings
        decoded = False
        for enc in ["utf-8", "utf-8-sig", "gb18030", "gbk"]:
            try:
                file_bytes.decode(enc)
                decoded = True
                break
            except UnicodeDecodeError:
                continue

        if not decoded:
            raise ValueError("文本文件内容无法通过 UTF-8 或常见中文编码解析")

        return "text/markdown" if ext in (".md", ".markdown") else "text/plain"

    else:
        raise ValueError(f"未知的文件扩展名: {ext}")


def save_attachment_file_atomic(
    conversation_id: str,
    attachment_id: str,
    ext: str,
    file_bytes: bytes,
    owner_id: Optional[str] = None,
) -> Tuple[Path, str]:
    """
    Atomically writes attachment file to disk using UUID filename.
    Storage path: data/uploads/attachments/{owner_id}/{conversation_id}/{attachment_id}/{stored_uuid}{ext}
    Returns: (absolute_file_path, relative_storage_key)
    """
    if owner_id and owner_id != settings.DEFAULT_LOCAL_USER_ID:
        target_dir = settings.ATTACHMENTS_DIR / owner_id / conversation_id / attachment_id
        storage_key = f"attachments/{owner_id}/{conversation_id}/{attachment_id}"
    else:
        target_dir = settings.ATTACHMENTS_DIR / conversation_id / attachment_id
        storage_key = f"attachments/{conversation_id}/{attachment_id}"

    target_dir.mkdir(parents=True, exist_ok=True)

    stored_filename = f"{uuid.uuid4().hex}{ext}"
    final_path = target_dir / stored_filename
    temp_path = target_dir / f"{stored_filename}.tmp"

    try:
        with open(temp_path, "wb") as f:
            f.write(file_bytes)
            f.flush()
            os.fsync(f.fileno())
        # Atomic rename
        temp_path.replace(final_path)
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise IOError(f"保存附件物理文件失败: {str(exc)}") from exc

    # Relative storage key
    storage_key = f"{storage_key}/{stored_filename}"
    return final_path, storage_key


def delete_attachment_physical_dir(
    conversation_id: str,
    attachment_id: str,
    owner_id: Optional[str] = None,
) -> None:
    """
    Delete physical storage directory for a specific attachment.
    Supports both user-partitioned paths and legacy root paths.
    """
    dirs_to_clean = []
    if owner_id:
        dirs_to_clean.append(settings.ATTACHMENTS_DIR / owner_id / conversation_id / attachment_id)
    dirs_to_clean.append(settings.ATTACHMENTS_DIR / conversation_id / attachment_id)

    for att_dir in dirs_to_clean:
        if att_dir.exists():
            try:
                shutil.rmtree(att_dir, ignore_errors=True)
                logger.info(f"Cleaned up physical attachment dir: {att_dir}")
            except Exception as exc:
                logger.warning(f"Failed to remove attachment dir {att_dir}: {exc}")

    # Remove conversation attachment dir if empty
    conv_dirs = []
    if owner_id:
        conv_dirs.append(settings.ATTACHMENTS_DIR / owner_id / conversation_id)
    conv_dirs.append(settings.ATTACHMENTS_DIR / conversation_id)

    for conv_dir in conv_dirs:
        if conv_dir.exists():
            try:
                if not any(conv_dir.iterdir()):
                    conv_dir.rmdir()
            except Exception:
                pass


def delete_conversation_physical_attachments(
    conversation_id: str,
    owner_id: Optional[str] = None,
) -> None:
    """
    Delete all physical attachment files for an entire conversation.
    """
    conv_dirs = []
    if owner_id:
        conv_dirs.append(settings.ATTACHMENTS_DIR / owner_id / conversation_id)
    conv_dirs.append(settings.ATTACHMENTS_DIR / conversation_id)

    for conv_dir in conv_dirs:
        if conv_dir.exists():
            try:
                shutil.rmtree(conv_dir, ignore_errors=True)
                logger.info(f"Cleaned up conversation attachments dir: {conv_dir}")
            except Exception as exc:
                logger.warning(f"Failed to remove conversation attachments dir {conv_dir}: {exc}")
    conv_dir = settings.ATTACHMENTS_DIR / conversation_id
    if conv_dir.exists():
        try:
            shutil.rmtree(conv_dir, ignore_errors=True)
            logger.info(f"Cleaned up all physical attachments for conversation {conversation_id}")
        except Exception as exc:
            logger.warning(f"Failed to clean up attachments for conversation {conversation_id}: {exc}")


async def cleanup_orphan_attachments(db: AsyncSession) -> int:
    """
    Scan data/uploads/attachments/ directory, identify folders belonging to
    non-existent conversations, and delete them.
    Returns count of cleaned conversation directories.
    """
    if not settings.ATTACHMENTS_DIR.exists():
        return 0

    cleaned_count = 0
    stmt = select(Conversation.id)
    res = await db.execute(stmt)
    valid_conv_ids = set(res.scalars().all())

    for item in settings.ATTACHMENTS_DIR.iterdir():
        if item.is_dir():
            conv_id = item.name
            if conv_id not in valid_conv_ids:
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    cleaned_count += 1
                    logger.info(f"Cleaned up orphan attachment directory: {item}")
                except Exception as exc:
                    logger.warning(f"Failed to remove orphan dir {item}: {exc}")

    return cleaned_count
