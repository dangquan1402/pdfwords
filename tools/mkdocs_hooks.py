# SPDX-License-Identifier: Apache-2.0
"""MkDocs hooks: build the home page, quickstart and changelog from the repository's
README.md / CHANGELOG.md, so they are never duplicated by hand."""
import os
import re

from mkdocs.structure.files import File

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/dangquan1402/pdfwords/blob/main/"


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _fix_links(md):
    # README links point into docs/ (served at the site root) or at repository files
    md = re.sub(r"\]\(docs/([^)]+)\)", r"](\1)", md)
    md = re.sub(r"\]\((LICENSE|NOTICE|CONTRIBUTING\.md|CHANGELOG\.md)\)", lambda m: f"]({REPO}{m.group(1)})", md)
    md = md.replace("](CHANGELOG.md)", "](changelog.md)")
    return md


def _section(md, start, end):
    a = md.index(start)
    b = md.index(end, a)
    return md[a:b]


def on_files(files, config):
    readme = _fix_links(_read("README.md"))
    quick = "# Quickstart\n\n" + _section(readme, "## Install", "## Links, annotations")
    pages = {
        "index.md": readme,
        "quickstart.md": quick,
        "changelog.md": _read("CHANGELOG.md"),
    }
    for name, content in pages.items():
        files.append(File.generated(config, name, content=content))
    return files
