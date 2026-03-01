from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from typing import Any

try:
    from fcp3.node import ConnectionRefused, FCPException, FCPNode, FCPNodeFailure
except Exception as import_error:  # pragma: no cover - import-time guard
    FCPNode = None
    FCPException = RuntimeError
    FCPNodeFailure = RuntimeError
    ConnectionRefused = RuntimeError
    _IMPORT_ERROR: Exception | None = import_error
else:
    _IMPORT_ERROR = None

LOGGER = logging.getLogger(__name__)


class FCPClientError(RuntimeError):
    """It's raised for FCP connectivity or operation failures."""


class FCPClient:
    def __init__(self, *, host: str | None = None, port: int | None = None, verbosity: int = 0):
        self.host = host or os.getenv("FCP_HOST", "127.0.0.1")
        resolved_port = str(port) if port is not None else os.getenv("FCP_PORT", "9481")
        self.port = int(resolved_port)
        self.verbosity = max(0, verbosity)
        self._node: Any | None = None

    def __enter__(self) -> "FCPClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self._node is not None:
            return
        if FCPNode is None:
            raise FCPClientError(f"pyFreenet3 is not importable: {_IMPORT_ERROR}")
        try:
            self._node = FCPNode(host=self.host, port=self.port, verbosity=self.verbosity)
        except Exception as err:  # pragma: no cover - network interaction
            raise FCPClientError(
                f"Failed to connect to FCP service at {self.host}:{self.port}."
            ) from err

    def close(self) -> None:
        if self._node is None:
            return
        try:
            self._node.shutdown()
        finally:
            self._node = None

    def put_file_chk(
        self,
        path: Path,
        *,
        priority: int,
        persistence: str,
        global_queue: bool,
        identifier: str | None = None,
    ) -> str:
        return self._put_path(
            "CHK@",
            path,
            priority=priority,
            persistence=persistence,
            global_queue=global_queue,
            identifier=identifier,
        )

    def put_file_to_uri(
        self,
        uri: str,
        path: Path,
        *,
        priority: int,
        persistence: str,
        global_queue: bool,
        identifier: str | None = None,
    ) -> str:
        return self._put_path(
            uri,
            path,
            priority=priority,
            persistence=persistence,
            global_queue=global_queue,
            identifier=identifier,
        )

    def put_bytes_to_uri(
        self,
        uri: str,
        data: bytes,
        *,
        priority: int,
        persistence: str,
        global_queue: bool,
        identifier: str | None = None,
    ) -> str:
        node = self._require_node()
        request_identifier = identifier or _new_fcp_identifier()
        codecs = self._preferred_insert_codecs(node)
        put_kwargs: dict[str, Any] = {
            "data": data,
            "priority": priority,
            "persistence": persistence,
            "Global": global_queue,
            "async": False,
            "id": request_identifier,
        }
        if codecs:
            put_kwargs["Codecs"] = codecs
        LOGGER.info(
            "FCP put start id=%s mode=direct uri=%s size=%d persistence=%s global=%s priority=%d codecs=%s",
            request_identifier,
            uri,
            len(data),
            persistence,
            global_queue,
            priority,
            codecs if codecs else "<node-default>",
        )
        started_at = time.monotonic()
        try:
            put_result = node.put(uri, **put_kwargs)
        except Exception as err:
            elapsed = time.monotonic() - started_at
            LOGGER.error(
                "FCP put failed id=%s mode=direct uri=%s elapsed_s=%.3f",
                request_identifier,
                uri,
                elapsed,
            )
            raise FCPClientError(f"Failed to insert bytes to {uri!r}.") from err
        chk = _normalize_uri(put_result)
        elapsed = time.monotonic() - started_at
        LOGGER.info(
            "FCP put complete id=%s mode=direct uri=%s chk=%s elapsed_s=%.3f",
            request_identifier,
            uri,
            chk,
            elapsed,
        )
        return chk

    def get_bytes(self, uri: str, *, timeout_s: int) -> bytes:
        node = self._require_node()
        try:
            get_result = node.get(
                uri,
                timeout=timeout_s,
                persistence="connection",
                Global=False,
                nodata=False,
            )
        except Exception as err:
            raise FCPClientError(f"Failed to retrieve URI {uri!r}.") from err
        return _extract_get_payload(get_result, uri)

    def check_retrievable(self, uri: str, *, timeout_s: int) -> bool:
        node = self._require_node()
        try:
            node.get(
                uri,
                timeout=timeout_s,
                persistence="connection",
                Global=False,
                nodata=True,
            )
        except (FCPException, FCPNodeFailure, ConnectionRefused):
            return False
        return True

    def generate_usk_keypair(self) -> tuple[str, str]:
        node = self._require_node()
        try:
            public_ssk, private_ssk = node.genkey()
        except Exception as err:
            raise FCPClientError("Failed to generate staging key pair via FCP.") from err

        private_usk_root = _to_usk_root(private_ssk)
        try:
            public_usk_root = node.invertprivate(private_usk_root)
        except (FCPException, FCPNodeFailure, ConnectionRefused):
            public_usk_root = _to_usk_root(public_ssk)

        private_usk_base = _to_info_base(private_usk_root)
        public_usk_base = _to_info_base(public_usk_root)
        return private_usk_base, public_usk_base

    def to_public_usk_base(self, private_usk_base: str) -> str:
        node = self._require_node()
        private_root = _to_usk_root(_info_base_to_root(private_usk_base))
        try:
            public_root = node.invertprivate(private_root)
        except Exception as err:
            raise FCPClientError("Failed to derive public USK from private staging key.") from err
        return _to_info_base(_to_usk_root(public_root))

    def _put_path(
        self,
        uri: str,
        path: Path,
        *,
        priority: int,
        persistence: str,
        global_queue: bool,
        identifier: str | None = None,
    ) -> str:
        node = self._require_node()
        absolute_path = path.resolve()
        if not absolute_path.exists():
            raise FCPClientError(f"File does not exist for insert: {absolute_path}")
        request_identifier = identifier or _new_fcp_identifier()

        put_kwargs: dict[str, Any] = {
            "priority": priority,
            "persistence": persistence,
            "Global": global_queue,
            "async": False,
            "id": request_identifier,
        }
        codecs = self._preferred_insert_codecs(node)
        if codecs:
            put_kwargs["Codecs"] = codecs

        dda_enabled = self._can_use_dda(absolute_path)
        upload_mode = "disk"
        if dda_enabled:
            put_kwargs["file"] = str(absolute_path)
        else:
            upload_mode = "direct_fallback"
            LOGGER.warning(
                "FCP DDA check failed for %s; falling back to direct upload in-memory.",
                absolute_path,
            )
            put_kwargs["data"] = absolute_path.read_bytes()

        file_size = absolute_path.stat().st_size
        LOGGER.info(
            "FCP put start id=%s mode=%s uri=%s path=%s size=%d persistence=%s global=%s priority=%d codecs=%s",
            request_identifier,
            upload_mode,
            uri,
            absolute_path,
            file_size,
            persistence,
            global_queue,
            priority,
            codecs if codecs else "<node-default>",
        )
        started_at = time.monotonic()
        try:
            put_result = node.put(uri, **put_kwargs)
        except Exception as err:
            elapsed = time.monotonic() - started_at
            LOGGER.error(
                "FCP put failed id=%s mode=%s uri=%s path=%s elapsed_s=%.3f",
                request_identifier,
                upload_mode,
                uri,
                absolute_path,
                elapsed,
            )
            raise FCPClientError(f"Failed to insert file {absolute_path} to {uri!r}.") from err
        chk = _normalize_uri(put_result)
        elapsed = time.monotonic() - started_at
        LOGGER.info(
            "FCP put complete id=%s mode=%s uri=%s chk=%s elapsed_s=%.3f",
            request_identifier,
            upload_mode,
            uri,
            chk,
            elapsed,
        )
        return chk

    def _can_use_dda(self, path: Path) -> bool:
        node = self._require_node()
        try:
            result = node.testDDA(
                Directory=str(path.parent),
                WantReadDirectory=True,
                WantWriteDirectory=False,
            )
        except (FCPException, FCPNodeFailure, ConnectionRefused, OSError):
            return False
        return bool(result)

    @staticmethod
    def _preferred_insert_codecs(node: Any) -> str | None:
        raw_codecs = getattr(node, "compressionCodecs", None)
        if not isinstance(raw_codecs, list):
            return None

        codec_names = [name for name, _number in raw_codecs if isinstance(name, str)]
        if not codec_names:
            return None

        if "LZMA" in codec_names and "LZMA_NEW" in codec_names:
            codec_names = [name for name in codec_names if name != "LZMA"]

        if not codec_names:
            return None
        return ", ".join(codec_names)

    def _require_node(self) -> Any:
        if self._node is None:
            self.connect()
        if self._node is None:
            raise FCPClientError("FCP client is not connected.")
        return self._node


def _normalize_uri(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    if isinstance(result, dict):
        for key in ("URI", "Uri", "uri"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    if isinstance(result, tuple) and result:
        first = result[0]
        if isinstance(first, str):
            return first
    raise FCPClientError(f"Unexpected response from FCP put operation: {type(result)!r}")


def _extract_get_payload(result: Any, uri: str) -> bytes:
    if isinstance(result, tuple):
        if len(result) < 2:
            raise FCPClientError(f"Unexpected get response tuple for URI {uri!r}.")
        payload = result[1]
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, bytearray):
            return bytes(payload)
        if isinstance(payload, str):
            return payload.encode("utf-8")
    if isinstance(result, bytes):
        return result
    if isinstance(result, str):
        return result.encode("utf-8")
    raise FCPClientError(f"Unexpected response from FCP get operation for URI {uri!r}.")


def _to_usk_root(uri: Any) -> str:
    if not isinstance(uri, str):
        raise FCPClientError(f"Unexpected key type from FCP genkey: {type(uri)!r}")
    normalized = uri.strip()
    if not normalized:
        raise FCPClientError("Empty key returned from FCP genkey.")
    if normalized.startswith("USK@"):
        return normalized
    if normalized.startswith("SSK@"):
        return normalized.replace("SSK@", "USK@", 1)
    raise FCPClientError(f"Unsupported key format returned by FCP: {normalized!r}")


def _to_info_base(usk_root: str) -> str:
    normalized = usk_root.strip()
    if normalized.endswith("/info/"):
        return normalized
    if not normalized.endswith("/"):
        normalized += "/"
    return f"{normalized}info/"


def _info_base_to_root(usk_base: str) -> str:
    normalized = usk_base.strip()
    if normalized.endswith("/info/"):
        return normalized[: -len("info/")]
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _new_fcp_identifier() -> str:
    return f"id{time.time_ns()}"
