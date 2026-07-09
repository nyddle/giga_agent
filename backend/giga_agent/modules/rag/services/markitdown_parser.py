"""Blob-парсер на базе microsoft/markitdown.

Конвертирует офисные форматы (PPTX/XLSX/EPUB/DOCX) в Markdown с сохранением
структуры: заголовки, списки и таблицы остаются таблицами — чанки RAG и
контекст LLM получаются осмысленными, в отличие от плоского текста.

PDF и HTML сюда сознательно не заведены: для PDF markitdown использует тот же
pdfminer (выигрыша нет), HTML уже покрыт BS4/markdownify.
"""

from __future__ import annotations

import io
from typing import Iterator

from langchain_core.document_loaders.base import BaseBlobParser
from langchain_core.documents.base import Blob, Document

from giga_agent.core.logging import get_logger

logger = get_logger(__name__)

_markitdown = None


def _get_markitdown():
    """Ленивый singleton: MarkItDown создаёт конвертеры при инициализации."""
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown

        _markitdown = MarkItDown(enable_plugins=False)
    return _markitdown


class MarkItDownParser(BaseBlobParser):
    """Парсит blob через markitdown; при ошибке уходит в fallback-парсер."""

    def __init__(self, fallback: BaseBlobParser | None = None):
        self._fallback = fallback

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        # Kill-switch (он же A/B-переключатель бенчмарка): off → сразу фолбэк.
        import os

        if os.environ.get("GIGA_AGENT_MARKITDOWN", "on").lower() in (
            "off", "0", "false", "no"
        ):
            if self._fallback is None:
                raise ValueError(
                    "markitdown выключен (GIGA_AGENT_MARKITDOWN=off), "
                    "фолбэк-парсера для этого формата нет"
                )
            yield from self._fallback.lazy_parse(blob)
            return
        try:
            from markitdown import StreamInfo

            result = _get_markitdown().convert_stream(
                io.BytesIO(blob.as_bytes()),
                stream_info=StreamInfo(
                    mimetype=blob.mimetype,
                    filename=blob.source or None,
                ),
            )
            text = (result.markdown or "").strip()
            if not text:
                raise ValueError("markitdown returned empty content")
            yield Document(
                page_content=text,
                metadata={"source": blob.source or blob.mimetype or "document"},
            )
            return
        except Exception:
            if self._fallback is None:
                raise
            logger.exception(
                "MarkItDownParser: conversion failed (%s), using fallback",
                blob.mimetype,
            )
        yield from self._fallback.lazy_parse(blob)
