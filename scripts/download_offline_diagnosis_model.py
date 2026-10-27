"""Download pinned, checksummed ONNX artifacts for offline diagnosis."""

import hashlib
import os
from pathlib import Path

import requests


MODEL_REPOSITORY = "imaflower/plantvillage-mobilenetv3"
MODEL_REVISION = "d76fe187be1c4c3a5474f835a7a70cd08c7ab085"
MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "models" / "offline_diagnosis"
ARTIFACTS = {
    "model.onnx": {
        "sha256": "c6a962f699ddc820108231b23454b6a4e242407044c2c2514a0f6294d35428fc",
        "size": 293463,
    },
    "model.onnx.data": {
        "sha256": "c03d114add89db13d850e5bbe0a7c09040cf6caf1acfbbd489c21234fa6dcb5e",
        "size": 6160384,
    },
}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_artifact(filename, metadata):
    target = MODEL_DIRECTORY / filename
    if target.is_file() and target.stat().st_size == metadata["size"]:
        if _sha256(target) == metadata["sha256"]:
            print(f"Verified existing model artifact: {filename}")
            return

    url = (
        f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/"
        f"{MODEL_REVISION}/{filename}"
    )
    temporary = target.with_name(f"{filename}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with temporary.open("wb") as artifact:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        size += len(chunk)
                        digest.update(chunk)
                        artifact.write(chunk)
        if size != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
            raise RuntimeError(f"Checksum or size verification failed for {filename}")
        os.replace(temporary, target)
        print(f"Downloaded and verified model artifact: {filename}")
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for filename, metadata in ARTIFACTS.items():
        _download_artifact(filename, metadata)


if __name__ == "__main__":
    main()
