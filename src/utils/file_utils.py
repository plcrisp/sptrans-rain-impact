import hashlib


def compute_file_sha256(filepath: str) -> str:
    """Calcula hash SHA-256 de um arquivo em chunks."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()
