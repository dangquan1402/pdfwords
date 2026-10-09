# SPDX-License-Identifier: Apache-2.0
"""LlamaIndex reader (pip install "pdfwords[llamaindex]").

    from pdfwords.integrations.llama_index import PdfwordsReader
    docs = PdfwordsReader(mode="markdown_page").load_data("paper.pdf")

    # or with SimpleDirectoryReader:
    SimpleDirectoryReader("dir/", file_extractor={".pdf": PdfwordsReader()})

mode: "page" (default) | "single" | "markdown_page" | "markdown" | "chunks" (with provenance).
"""
from __future__ import annotations

from llama_index.core.readers.base import BaseReader
from llama_index.core.schema import Document

from ._docs import iter_texts


class PdfwordsReader(BaseReader):
    def __init__(self, mode="page", *, max_chars=2000, sort="xycut", **kwargs):
        super().__init__()
        self.mode, self.max_chars, self.sort, self.kwargs = mode, max_chars, sort, kwargs

    def load_data(self, file, extra_info=None, fs=None, *, pages=None, password=None):
        if fs is not None:
            with fs.open(str(file), "rb") as f:
                file_src = f.read()
        else:
            file_src = file
        out = []
        for text, md in iter_texts(file_src, self.mode, pages=pages, password=password, max_chars=self.max_chars,
                                   **self.kwargs):
            if fs is not None:
                md["source"] = str(file)
            md.update(extra_info or {})
            # nested provenance lists would bloat embeddings: keep them out of the embedded metadata
            excl = [k for k in ("provenance", "pages", "headings") if k in md]
            out.append(Document(text=text, metadata=md, excluded_embed_metadata_keys=excl,
                                excluded_llm_metadata_keys=[k for k in excl if k != "headings"]))
        return out
