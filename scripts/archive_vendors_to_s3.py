#!/usr/bin/env python3
"""Archive vendors/ to S3 using boto3.

The script writes immutable timestamped snapshots and, by default, refreshes a
stable latest/ pointer. S3 bucket versioning is optional and bucket-level:
timestamped keys provide explicit time travel, while bucket versioning preserves
prior versions of latest/vendors.tar.gz when latest is overwritten.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
from typing import Any
from urllib.parse import urlparse


DEFAULT_WORK_DIR = Path("/tmp/uxv-vendor-archives")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive vendors/ as a tar.gz and upload it to S3 with boto3.",
    )
    parser.add_argument("s3_uri", help="Destination prefix, e.g. s3://bucket/path")
    parser.add_argument("--vendors-dir", default="vendors", help="Directory to archive. Default: vendors")
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help=f"Scratch directory. Default: {DEFAULT_WORK_DIR}")
    parser.add_argument("--enable-bucket-versioning", action="store_true", help="Enable S3 bucket versioning before upload")
    parser.add_argument("--no-latest", action="store_true", help="Do not update latest/vendors.tar.gz")
    parser.add_argument("--profile", help="AWS profile name")
    parser.add_argument("--region", help="AWS region")
    parser.add_argument("--storage-class", default="STANDARD", help="S3 storage class. Default: STANDARD")
    parser.add_argument("--sse", help="Server-side encryption, e.g. AES256 or aws:kms")
    parser.add_argument("--kms-key-id", help="KMS key ID/ARN when --sse aws:kms")
    parser.add_argument("--no-zshrc", action="store_true", help="Do not load simple AWS exports from ~/.zshrc")
    parser.add_argument("--no-create-bucket", action="store_true", help="Fail if the target bucket does not already exist")
    parser.add_argument("--dry-run", action="store_true", help="Create archive and manifest but do not call AWS")
    return parser.parse_args()


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.strip("/")


def git_sha(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no-git"


def repo_root() -> Path:
    try:
        return Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def vendor_count(path: Path) -> int:
    return sum(1 for item in path.iterdir() if item.is_dir())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_archive(vendors_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(vendors_dir, arcname=vendors_dir.name)


def content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.name.endswith(".tar.gz"):
        return "application/gzip"
    return "binary/octet-stream"


def upload_file(
    s3_client: Any,
    source: Path,
    bucket: str,
    key: str,
    *,
    storage_class: str,
    sse: str | None,
    kms_key_id: str | None,
) -> None:
    extra_args: dict[str, str] = {
        "StorageClass": storage_class,
        "ContentType": content_type(source),
    }
    if sse:
        extra_args["ServerSideEncryption"] = sse
    if kms_key_id:
        extra_args["SSEKMSKeyId"] = kms_key_id
    try:
        s3_client.upload_file(str(source), bucket, key, ExtraArgs=extra_args)
    except Exception as exc:
        code = client_error_code(exc)
        if code in {"AccessDenied", "UnauthorizedOperation"}:
            raise aws_permission_error("s3:PutObject", exc) from exc
        raise


def s3_join(prefix: str, *parts: str) -> str:
    return "/".join(part.strip("/") for part in (prefix, *parts) if part.strip("/"))


def load_aws_env_from_zshrc(zshrc_path: Path | None = None, skip_keys: set[str] | None = None) -> list[str]:
    """Load simple AWS-related assignments from ~/.zshrc into os.environ.

    This intentionally does not execute shell code. It supports lines like:
    `export AWS_ACCESS_KEY_ID=...`, `AWS_PROFILE=...`, and quoted values.
    Existing environment variables win.
    """
    path = zshrc_path or Path.home() / ".zshrc"
    if not path.exists():
        return []
    skip = skip_keys or set()

    allowed = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
    }
    loaded: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key in skip or key not in allowed or key in os.environ:
            continue
        try:
            parts = shlex.split(raw_value, posix=True)
        except ValueError:
            continue
        if len(parts) != 1:
            continue
        os.environ[key] = parts[0]
        loaded.append(key)
    return loaded


def make_boto3_session(profile: str | None, region: str | None) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise SystemExit("boto3 is required: python3 -m pip install boto3") from exc

    session_kwargs: dict[str, str] = {}
    if profile:
        session_kwargs["profile_name"] = profile
        os.environ.pop("AWS_PROFILE", None)
    if region:
        session_kwargs["region_name"] = region
    return boto3.Session(**session_kwargs)


def profile_uses_login_session(profile: str | None) -> bool:
    config_path = Path(os.environ.get("AWS_CONFIG_FILE", Path.home() / ".aws" / "config")).expanduser()
    if not config_path.exists():
        return False
    parser = configparser.ConfigParser()
    parser.read(config_path)
    section = "default" if not profile else f"profile {profile}"
    return parser.has_option(section, "login_session")


def client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", ""))


def client_error_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", {})
    meta = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    status = meta.get("HTTPStatusCode")
    return int(status) if isinstance(status, int) else None


def aws_permission_error(action: str, exc: Exception) -> SystemExit:
    code = client_error_code(exc) or type(exc).__name__
    return SystemExit(f"AWS denied {action} ({code}). Check IAM permission: {action}")


def resolve_region(session: Any, requested_region: str | None) -> str:
    return requested_region or session.region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def assert_aws_identity(session: Any, profile: str | None) -> None:
    try:
        sts = session.client("sts")
        identity = sts.get_caller_identity()
    except Exception as exc:
        code = client_error_code(exc)
        if type(exc).__name__ in {"NoCredentialsError", "PartialCredentialsError"}:
            if profile_uses_login_session(profile):
                raise SystemExit(
                    f"Profile {profile!r} uses AWS CLI login credentials, but boto3 could not load them. "
                    "Install/update boto3 with AWS CRT support: python3 -m pip install --upgrade 'boto3[crt]'"
                ) from exc
            raise SystemExit("AWS credentials were not found or are incomplete. Export them or add simple AWS_* exports to ~/.zshrc.") from exc
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            raise aws_permission_error("sts:GetCallerIdentity", exc) from exc
        raise
    arn = identity.get("Arn") or identity.get("UserId") or identity.get("Account")
    print(f"[aws] authenticated as {arn}", flush=True)


def ensure_bucket(s3_client: Any, bucket: str, region: str, *, create_bucket: bool) -> None:
    try:
        s3_client.head_bucket(Bucket=bucket)
        print(f"[aws] bucket exists: s3://{bucket}", flush=True)
        return
    except Exception as exc:
        status = client_error_status(exc)
        code = client_error_code(exc)
        if status == 403 or code in {"403", "AccessDenied"}:
            raise aws_permission_error("s3:HeadBucket", exc) from exc
        if status not in {404, 301, 400} and code not in {"404", "NoSuchBucket", "NotFound"}:
            raise

    if not create_bucket:
        raise SystemExit(f"S3 bucket does not exist or is not accessible: s3://{bucket}")

    print(f"[aws] creating bucket s3://{bucket} in {region}", flush=True)
    params: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        s3_client.create_bucket(**params)
    except Exception as exc:
        code = client_error_code(exc)
        if code == "BucketAlreadyOwnedByYou":
            pass
        elif code == "BucketAlreadyExists":
            raise SystemExit(f"S3 bucket name is already taken by another account: {bucket}") from exc
        elif code in {"AccessDenied", "UnauthorizedOperation"}:
            raise aws_permission_error("s3:CreateBucket", exc) from exc
        else:
            raise
    s3_client.get_waiter("bucket_exists").wait(Bucket=bucket)
    print(f"[aws] bucket ready: s3://{bucket}", flush=True)


def enable_bucket_versioning(s3_client: Any, bucket: str) -> None:
    try:
        s3_client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
    except Exception as exc:
        code = client_error_code(exc)
        if code in {"AccessDenied", "UnauthorizedOperation"}:
            raise aws_permission_error("s3:PutBucketVersioning", exc) from exc
        raise


def main() -> int:
    args = parse_args()
    bucket, prefix = parse_s3_uri(args.s3_uri)
    vendors_dir = Path(args.vendors_dir).expanduser().resolve()
    if not vendors_dir.is_dir():
        raise SystemExit(f"vendors directory not found: {vendors_dir}")

    if not args.no_zshrc:
        loaded = load_aws_env_from_zshrc(skip_keys={"AWS_PROFILE"} if args.profile else set())
        if loaded:
            print(f"[aws] loaded {', '.join(loaded)} from ~/.zshrc", flush=True)

    root = repo_root()
    commit = git_sha(root)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"vendors-{timestamp}-{commit}.tar.gz"
    manifest_name = f"vendors-{timestamp}-{commit}.manifest.json"
    work_dir = Path(args.work_dir).expanduser().resolve()
    archive_path = work_dir / archive_name
    manifest_path = work_dir / manifest_name

    snapshot_archive_key = s3_join(prefix, "snapshots", archive_name)
    snapshot_manifest_key = s3_join(prefix, "snapshots", manifest_name)
    latest_archive_key = s3_join(prefix, "latest", "vendors.tar.gz")
    latest_manifest_key = s3_join(prefix, "latest", "vendors.manifest.json")

    session = None
    s3_client = None
    if not args.dry_run:
        session = make_boto3_session(args.profile, args.region)
        region = resolve_region(session, args.region)
        assert_aws_identity(session, args.profile)
        s3_client = session.client("s3", region_name=region)
        ensure_bucket(s3_client, bucket, region, create_bucket=not args.no_create_bucket)

        if args.enable_bucket_versioning:
            print(f"[archive] enabling versioning on s3://{bucket}", flush=True)
            enable_bucket_versioning(s3_client, bucket)

    print(f"[archive] creating {archive_path} from {vendors_dir}", flush=True)
    create_archive(vendors_dir, archive_path)

    manifest = {
        "created_at": timestamp,
        "git_commit": commit,
        "source_dir": str(vendors_dir),
        "vendor_count": vendor_count(vendors_dir),
        "file_count": file_count(vendors_dir),
        "archive": {
            "filename": archive_name,
            "bytes": archive_path.stat().st_size,
            "sha256": sha256_file(archive_path),
            "s3_uri": f"s3://{bucket}/{snapshot_archive_key}",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    uploads = [
        (archive_path, snapshot_archive_key),
        (manifest_path, snapshot_manifest_key),
    ]
    if not args.no_latest:
        uploads.extend(
            [
                (archive_path, latest_archive_key),
                (manifest_path, latest_manifest_key),
            ]
        )

    if args.dry_run:
        if args.enable_bucket_versioning:
            print(f"[dry-run] would enable versioning on s3://{bucket}")
        if not args.no_create_bucket:
            print(f"[dry-run] would create s3://{bucket} if it does not exist")
        for source, key in uploads:
            print(f"[dry-run] would upload {source} to s3://{bucket}/{key}")
        print(f"[archive] manifest: {manifest_path}")
        return 0

    for source, key in uploads:
        print(f"[archive] uploading {source.name} to s3://{bucket}/{key}", flush=True)
        assert s3_client is not None
        upload_file(
            s3_client,
            source,
            bucket,
            key,
            storage_class=args.storage_class,
            sse=args.sse,
            kms_key_id=args.kms_key_id,
        )

    print("[archive] complete")
    print(f"archive:  {archive_path}")
    print(f"manifest: {manifest_path}")
    print(f"s3:       s3://{bucket}/{snapshot_archive_key}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
