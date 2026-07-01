"""Upload a local BIDS directory to OSF and maintain a dataset index."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

from osfclient.api import OSF
from osfclient.models.storage import File, Storage, checksum, file_empty
from requests.exceptions import RequestException
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

BIDS_ROOT_NAME = "landemard-2026-bids"
INDEX_FILENAME = "dataset_index.json"
CONSOLE = Console()
IndexEntry = dict[str, str | int | None]
DatasetIndex = dict[str, IndexEntry]
_RETRYABLE_KEYWORDS = (
    "status code 403",
    "status code 404",
    "status code 408",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 504",
    "connection error",
    "timed out",
)


def _print_retry_message(
    context: str,
    attempt: int,
    max_attempts: int,
    wait_seconds: int,
    exc: Exception,
) -> None:
    CONSOLE.print(
        f"[bold yellow]Retryable OSF error[/] during {context} "
        f"[dim](attempt {attempt}/{max_attempts})[/dim]: {exc}. "
        f"Retrying in [bold]{wait_seconds}s[/]..."
    )


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, AttributeError) and "'NoneType'" in str(exc):
        return True

    msg = str(exc).lower()
    if isinstance(exc, RequestException):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    return any(token in msg for token in _RETRYABLE_KEYWORDS)


def _get_storage(token: str, project_id: str) -> Storage:
    osf = OSF(token=token)
    project = osf.project(project_id)
    return project.storage()


def _get_storage_with_retry(
    token: str,
    project_id: str,
    max_attempts: int,
) -> Storage:
    for attempt in range(1, max_attempts + 1):
        try:
            return _get_storage(token, project_id)
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            wait_seconds = min(2 ** (attempt - 1), 30)
            time.sleep(wait_seconds)

    raise RuntimeError("Failed to connect to OSF storage after retries.")


def _parse_index_entry(value: object) -> IndexEntry | None:
    if not isinstance(value, dict):
        return None
    value_dict = cast(dict[str, object], value)

    osf_path = value_dict.get("osf_path")
    if not isinstance(osf_path, str):
        return None

    raw_size = value_dict.get("size")
    size: int | None = (
        raw_size
        if isinstance(raw_size, int) and not isinstance(raw_size, bool)
        else None
    )

    return {"osf_path": osf_path, "size": size}


def _get_index_osf_path(index: DatasetIndex, rel_path: str) -> str | None:
    entry = index.get(rel_path)
    if entry is None:
        return None
    osf_path = entry.get("osf_path")
    if not isinstance(osf_path, str):
        return None
    return osf_path


def _load_remote_index(bids_root_folder: Storage) -> DatasetIndex:
    try:
        index_file: File | None = None
        for remote_file in bids_root_folder.files:
            if remote_file.name == INDEX_FILENAME:
                index_file = remote_file
                break

        if index_file is None:
            return {}

        response = index_file._get(index_file._download_url)
        if response.status_code != 200:
            return {}
        payload = response.json()
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    index: DatasetIndex = {}
    for key, value in payload.items():
        entry = _parse_index_entry(value)
        if entry is None:
            msg = (
                "Invalid dataset_index.json schema: expected each entry to be "
                "an object with {'osf_path': str, 'size': int | null}."
            )
            raise RuntimeError(msg)
        index[str(key)] = entry

    return index


def _ensure_parent_folder(
    rel_dir: str,
    folder_cache: dict[str, Storage],
) -> tuple[str, Storage]:
    if rel_dir in ("", "."):
        return "", folder_cache[""]

    current = ""
    for part in rel_dir.split("/"):
        parent = current
        current = f"{current}/{part}" if current else part
        if current not in folder_cache:
            folder_cache[current] = folder_cache[parent].create_folder(
                part,
                exist_ok=True,
            )
    return current, folder_cache[current]


def _short_path(path: str, max_len: int = 72) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3) :]


def _file_from_osf_path(session, osf_path: str | None) -> File | None:
    if not osf_path:
        return None
    file_id = osf_path.strip("/")
    if not file_id:
        return None

    response = session.get(f"https://api.osf.io/v2/files/{file_id}/")
    if response.status_code != 200:
        return None

    payload = response.json().get("data")
    if not isinstance(payload, dict):
        return None
    return File(payload, session)


def _file_from_folder_name(folder: Storage, filename: str) -> File | None:
    files_url = getattr(folder, "_files_url", None)
    if not isinstance(files_url, str):
        return None

    response = folder.session.get(files_url, params={"filter[name]": filename})
    if response.status_code != 200:
        return None

    data = response.json().get("data")
    if not isinstance(data, list):
        return None

    for item in data:
        attrs = item.get("attributes", {})
        if attrs.get("kind") == "file" and attrs.get("name") == filename:
            return File(item, folder.session)
    return None


def _get_folder_file_map(
    folder_key: str,
    folder: Storage,
    folder_file_cache: dict[str, dict[str, File]],
) -> dict[str, File]:
    cached = folder_file_cache.get(folder_key)
    if cached is None:
        cached = {remote_file.name: remote_file for remote_file in folder.files}
        folder_file_cache[folder_key] = cached
    return cached


def _upload_file_once(
    folder: Storage,
    folder_key: str,
    filename: str,
    local_path: Path,
    *,
    update: bool,
    folder_file_cache: dict[str, dict[str, File]],
    local_md5_cache: dict[Path, str],
    known_osf_path: str | None,
) -> tuple[str, str | None]:
    with open(local_path, "rb") as fp:
        if file_empty(fp):
            response = folder._put(
                folder._new_file_url,
                params={"name": filename},
                data=b"",
            )
        else:
            response = folder._put(
                folder._new_file_url,
                params={"name": filename},
                data=fp,
            )

        if response.status_code in (200, 201):
            folder_file_cache.pop(folder_key, None)
            payload = response.json().get("data", {})
            osf_path = payload.get("attributes", {}).get("path")
            if isinstance(osf_path, str):
                return "uploaded", osf_path
            return "uploaded", None

        if response.status_code != 409:
            raise RuntimeError(
                f"Could not upload {local_path} (status code {response.status_code})."
            )

        existing = _get_folder_file_map(folder_key, folder, folder_file_cache).get(
            filename
        )
        if existing is None and known_osf_path is not None:
            existing = _file_from_osf_path(folder.session, known_osf_path)
        if existing is None:
            existing = _file_from_folder_name(folder, filename)

        if existing is None:
            return "skipped", known_osf_path

        if not update:
            return "skipped", existing.osf_path

        local_md5 = local_md5_cache.get(local_path)
        if local_md5 is None:
            local_md5 = checksum(local_path)
            local_md5_cache[local_path] = local_md5

        remote_md5 = (existing.hashes or {}).get("md5")
        if remote_md5 and local_md5 == remote_md5:
            return "skipped", existing.osf_path

        fp.seek(0)
        existing.update(fp)
        return "uploaded", existing.osf_path


def _upload_index_once(bids_root_folder: Storage, index_bytes: bytes) -> None:
    response = bids_root_folder._put(
        bids_root_folder._new_file_url,
        params={"name": INDEX_FILENAME},
        data=index_bytes,
    )

    if response.status_code in (200, 201):
        return

    if response.status_code != 409:
        raise RuntimeError(
            f"Could not upload {INDEX_FILENAME} (status code {response.status_code})."
        )

    existing = _file_from_folder_name(bids_root_folder, INDEX_FILENAME)
    if existing is None:
        raise RuntimeError(
            f"Could not resolve existing {INDEX_FILENAME} after conflict."
        )

    update_response = existing._put(existing._upload_url, data=index_bytes)
    if update_response.status_code != 200:
        raise RuntimeError(
            f"Could not update {INDEX_FILENAME} (status code {update_response.status_code})."
        )


def _generate_index(storage: Storage) -> DatasetIndex:
    """Walk remote OSF files and build the dataset index."""
    prefix = BIDS_ROOT_NAME + "/"
    index: DatasetIndex = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Scanning OSF storage...[/]", total=None)
        for remote_file in storage.files:
            materialized = remote_file.path.lstrip("/")
            if materialized.startswith(prefix):
                rel_path = materialized[len(prefix) :]
                if rel_path and rel_path != INDEX_FILENAME:
                    size = getattr(remote_file, "size", None)
                    if not isinstance(size, int) or isinstance(size, bool):
                        size = None
                    index[rel_path] = {
                        "osf_path": remote_file.osf_path,
                        "size": size,
                    }
                    progress.advance(task)

    return index


def generate_index_with_retry(
    token: str,
    project_id: str,
    max_attempts: int = 5,
) -> DatasetIndex:
    """Build the dataset index by scanning OSF storage with retries.

    Parameters
    ----------
    token : str
        OSF personal access token.
    project_id : str
        OSF project ID.
    max_attempts : int, default: 5
        Maximum number of attempts for the remote scan.

    Returns
    -------
    index : dict[str, dict[str, str | int | None]]
        Mapping from BIDS-relative path to ``{"osf_path", "size"}`` entries.
    """
    for attempt in range(1, max_attempts + 1):
        storage = _get_storage_with_retry(token, project_id, max_attempts=max_attempts)
        try:
            return _generate_index(storage)
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            wait_seconds = min(2 ** (attempt - 1), 30)
            _print_retry_message(
                context="dataset index scan",
                attempt=attempt,
                max_attempts=max_attempts,
                wait_seconds=wait_seconds,
                exc=exc,
            )
            time.sleep(wait_seconds)
            continue

    raise RuntimeError(
        f"Failed to build OSF dataset index after {max_attempts} retries."
    )


def upload_dataset(
    bids_dir: Path,
    token: str,
    project_id: str,
    update: bool = False,
) -> DatasetIndex:
    """Upload all files from a local BIDS directory to OSF.

    Files are uploaded under the ``landemard-2026-bids/`` folder in the
    project's OSF storage, regardless of the local directory name.

    Parameters
    ----------
    bids_dir : pathlib.Path
        Local BIDS root directory.
    token : str
        OSF personal access token.
    project_id : str
        OSF project ID.
    update : bool, default: False
        Re-upload files whose MD5 differs from the remote copy. When False,
        existing files are skipped.

    Returns
    -------
    index : dict[str, dict[str, str | int | None]]
        Incrementally updated mapping suitable for ``dataset_index.json``
        upload, containing OSF path and file size in bytes.
    """
    bids_dir = Path(bids_dir)
    all_files = sorted(path for path in bids_dir.rglob("*") if path.is_file())
    max_attempts = 5
    storage = _get_storage_with_retry(token, project_id, max_attempts=max_attempts)
    bids_root_folder = storage.create_folder(BIDS_ROOT_NAME, exist_ok=True)
    folder_cache: dict[str, Storage] = {"": bids_root_folder}
    folder_file_cache: dict[str, dict[str, File]] = {}
    local_md5_cache: dict[Path, str] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        progress.add_task(
            "[cyan]Loading existing dataset_index.json from OSF...[/]", total=None
        )
        remote_index = _load_remote_index(bids_root_folder)

    if remote_index:
        CONSOLE.print(f"[green]Loaded {len(remote_index)} existing index entries.[/]")
    else:
        CONSOLE.print("[yellow]No valid remote index found; starting from empty.[/]")

    index: DatasetIndex = dict(remote_index)
    uploaded = 0
    skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task(
            "[cyan]Uploading files to OSF...[/]",
            total=len(all_files),
        )

        for local_path in all_files:
            rel = local_path.relative_to(bids_dir)
            rel_path = rel.as_posix()
            rel_dir = rel.parent.as_posix()
            filename = rel.name
            known_osf_path = _get_index_osf_path(index, rel_path)

            progress.update(
                task,
                description=(
                    f"[cyan]Uploading[/] ({uploaded} up / {skipped} skip): "
                    f"[white]{_short_path(rel_path)}[/]"
                ),
            )

            if not update and rel_path in index:
                index[rel_path]["size"] = local_path.stat().st_size
                skipped += 1
                progress.advance(task)
                continue

            for attempt in range(1, max_attempts + 1):
                try:
                    folder_key, parent_folder = _ensure_parent_folder(
                        rel_dir, folder_cache
                    )
                    status, osf_path = _upload_file_once(
                        parent_folder,
                        folder_key,
                        filename,
                        local_path,
                        update=update,
                        folder_file_cache=folder_file_cache,
                        local_md5_cache=local_md5_cache,
                        known_osf_path=known_osf_path,
                    )
                except Exception as exc:
                    if attempt >= max_attempts or not _is_retryable(exc):
                        raise

                    wait_seconds = min(2 ** (attempt - 1), 30)
                    progress.update(
                        task,
                        description=(
                            f"[yellow]Retry {attempt}/{max_attempts - 1}[/] "
                            f"for [white]{local_path.name}[/] "
                            f"in [bold]{wait_seconds}s[/]..."
                        ),
                    )
                    time.sleep(wait_seconds)

                    storage = _get_storage_with_retry(
                        token,
                        project_id,
                        max_attempts=max_attempts,
                    )
                    bids_root_folder = storage.create_folder(
                        BIDS_ROOT_NAME, exist_ok=True
                    )
                    folder_cache = {"": bids_root_folder}
                    folder_file_cache = {}
                    continue

                if status == "uploaded":
                    uploaded += 1
                else:
                    skipped += 1

                if osf_path is None:
                    existing = _get_folder_file_map(
                        folder_key,
                        parent_folder,
                        folder_file_cache,
                    ).get(filename)
                    if existing is not None:
                        osf_path = existing.osf_path

                if osf_path is not None:
                    index[rel_path] = {
                        "osf_path": osf_path,
                        "size": local_path.stat().st_size,
                    }
                elif rel_path in index:
                    index[rel_path]["size"] = local_path.stat().st_size
                break

            progress.advance(task)

    CONSOLE.print(
        "[bold green]Upload complete[/]: "
        f"[green]{uploaded} uploaded[/], [yellow]{skipped} skipped[/]."
    )
    return index


def upload_index(
    index: DatasetIndex,
    token: str,
    project_id: str,
) -> None:
    """Upload ``dataset_index.json`` to OSF.

    Always overwrites any existing index file.

    Parameters
    ----------
    index : dict[str, dict[str, str | int | None]]
        Index dict as returned by `generate_index_with_retry` or
        `upload_dataset`.
    token : str
        OSF personal access token.
    project_id : str
        OSF project ID.
    """
    max_attempts = 5
    index_bytes = json.dumps(index, indent=2, sort_keys=True).encode()

    for attempt in range(1, max_attempts + 1):
        try:
            storage = _get_storage_with_retry(
                token,
                project_id,
                max_attempts=max_attempts,
            )
            bids_root_folder = storage.create_folder(BIDS_ROOT_NAME, exist_ok=True)
            _upload_index_once(bids_root_folder, index_bytes)
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            wait_seconds = min(2 ** (attempt - 1), 30)
            _print_retry_message(
                context="dataset index upload",
                attempt=attempt,
                max_attempts=max_attempts,
                wait_seconds=wait_seconds,
                exc=exc,
            )
            time.sleep(wait_seconds)
            continue
        else:
            return

    raise RuntimeError(
        f"Failed to upload {INDEX_FILENAME} after {max_attempts} retries."
    )
