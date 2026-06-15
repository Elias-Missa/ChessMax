"""File helpers shared by offline pipelines."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO
import urllib.request

import zstandard as zstd


@contextmanager
def open_text(path: Path) -> Iterator[TextIO]:
    """Open plain text or zstandard-compressed text by file extension."""

    if path.suffix == ".zst":
        with path.open("rb") as raw:
            reader = zstd.ZstdDecompressor().stream_reader(raw)
            text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace", newline="")
            try:
                yield text
            finally:
                text.detach()
                reader.close()
    else:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as text:
            yield text


@contextmanager
def open_text_source(source: str | Path) -> Iterator[TextIO]:
    """Open a local path or HTTPS URL, including ``.zst`` streams."""

    source_text = str(source)
    if source_text.startswith(("http://", "https://")):
        with urllib.request.urlopen(source_text) as response:
            if source_text.endswith(".zst"):
                reader = zstd.ZstdDecompressor().stream_reader(response)
                text = io.TextIOWrapper(
                    reader,
                    encoding="utf-8",
                    errors="replace",
                    newline="",
                )
                try:
                    yield text
                finally:
                    text.detach()
                    reader.close()
            else:
                text = io.TextIOWrapper(
                    response,
                    encoding="utf-8",
                    errors="replace",
                    newline="",
                )
                try:
                    yield text
                finally:
                    text.detach()
        return

    with open_text(Path(source_text)) as text:
        yield text
