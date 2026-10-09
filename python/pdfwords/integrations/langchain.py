# SPDX-License-Identifier: Apache-2.0
"""LangChain document loader (pip install "pdfwords[langchain]").

    from pdfwords.integrations.langchain import PdfwordsLoader
    docs = PdfwordsLoader("paper.pdf", mode="chunks", max_chars=1500).load()
    docs[0].metadata   # {"source", "page", "pages", "headings", "provenance": [{"page", "bbox", "type"}], ...}

mode: "page" (default, one Document per page) | "single" | "markdown_page" | "markdown" | "chunks".
"""
from __future__ import annotations

from typing import Iterator

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from ._docs import iter_texts


class PdfwordsLoader(BaseLoader):
    def __init__(self, file_path, mode="page", *, pages=None, password=None, max_chars=2000, **kwargs):
        self.file_path, self.mode, self.pages, self.password = file_path, mode, pages, password
        self.max_chars, self.kwargs = max_chars, kwargs

    def lazy_load(self) -> Iterator[Document]:
        for text, md in iter_texts(self.file_path, self.mode, pages=self.pages, password=self.password,
                                   max_chars=self.max_chars, **self.kwargs):
            yield Document(page_content=text, metadata=md)
