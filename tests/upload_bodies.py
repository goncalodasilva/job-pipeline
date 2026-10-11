"""Build ``multipart/form-data`` bodies by hand for the upload tests (M3.1).

Written out byte by byte so a test controls exactly what the server receives: the part
names, the client file names and the order. The boundary is fixed, so bodies are deterministic.
"""

from dataclasses import dataclass

BOUNDARY = "jp-test-boundary-7d3a"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


@dataclass(frozen=True)
class FilePart:
    filename: str
    data: bytes
    name: str = "file"
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class FieldPart:
    value: str
    name: str = "text"


def file_part_head(part: FilePart) -> bytes:
    """The boundary line and headers that come before a file part's bytes."""
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{part.name}"; filename="{part.filename}"\r\n'
        f"Content-Type: {part.content_type}\r\n\r\n"
    ).encode()


def closing() -> bytes:
    return f"\r\n--{BOUNDARY}--\r\n".encode()


def multipart_body(*parts: FilePart | FieldPart) -> bytes:
    """A complete body with ``parts`` in order."""
    body = b""
    for part in parts:
        if isinstance(part, FilePart):
            body += file_part_head(part) + part.data + b"\r\n"
        else:
            body += (
                (
                    f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{part.name}"\r\n\r\n'
                ).encode()
                + part.value.encode("utf-8")
                + b"\r\n"
            )
    return body + f"--{BOUNDARY}--\r\n".encode()
