from __future__ import annotations

from pathlib import Path


TEXT_SUFFIXES = {
    "",
    ".csv",
    ".html",
    ".log",
    ".md",
    ".markdown",
    ".rst",
    ".text",
    ".tsv",
    ".txt",
}

UNSUPPORTED_BINARY_SUFFIXES = {
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}


class UnsupportedInputFileError(ValueError):
    pass


def decode_input_file(content: bytes, filename: str | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in UNSUPPORTED_BINARY_SUFFIXES:
        raise UnsupportedInputFileError(f"Unsupported file type for text ingestion: {suffix}")
    if suffix not in TEXT_SUFFIXES:
        raise UnsupportedInputFileError(f"Unsupported file type for text ingestion: {suffix or '<none>'}")
    for bom, encoding in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig")):
        if content.startswith(bom):
            text = content.decode(encoding)
            if text.strip():
                return text
            raise UnsupportedInputFileError("Uploaded text file is empty")
    if looks_binary(content):
        raise UnsupportedInputFileError("Uploaded file appears to be binary")
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.strip():
            return text
    raise UnsupportedInputFileError("Could not decode uploaded text file")


def read_input_file(path: str | Path) -> str:
    file_path = Path(path)
    return decode_input_file(file_path.read_bytes(), file_path.name)


def looks_binary(content: bytes) -> bool:
    if not content:
        return False
    sample = content[:4096]
    if b"\x00" in sample:
        return True
    control_count = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return control_count / len(sample) > 0.05
