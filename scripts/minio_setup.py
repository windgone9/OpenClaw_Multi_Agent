#!/usr/bin/env python3
"""MinIO 测试数据初始化 — 建 bucket + 设 public-read + 上传测试文件。

服务器部署后, MinIO 是空库, 需跑此脚本灌入 E2E/模拟器所需测试文件:
  - test_image.png    (256x256 红底黄圆, 供 vision/multimodal E2E)
  - speech_test.wav   (真实中文语音 16kHz/16bit/mono, 供 ASR/WS ASR E2E)

用法:
  # 默认连本机 kind (端口转发 9000)
  kubectl port-forward svc/minio -n openclaw 9000:9000
  python3 scripts/minio_setup.py

  # 服务器 (改 endpoint/凭据)
  MINIO_ENDPOINT=localhost:9000 MINIO_ACCESS_KEY=minioadmin \\
  MINIO_SECRET_KEY=minioadmin MINIO_BUCKET=openclaw-test \\
  python3 scripts/minio_setup.py

退出码: 0 成功, 非0 失败。
"""
import os
import sys
import json
from pathlib import Path

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: 需 boto3 — pip install boto3", file=sys.stderr)
    sys.exit(2)

ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
BUCKET = os.getenv("MINIO_BUCKET", "openclaw-test")
REGION = os.getenv("MINIO_REGION", "us-east-1")
SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# endpoint 形如 http://localhost:9000 或 localhost:9000
endpoint_url = ENDPOINT if ENDPOINT.startswith("http") else (
    ("https://" if SECURE else "http://") + ENDPOINT
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FILES = [
    ("test_image.png", "image/png"),
    ("speech_test.wav", "audio/wav"),
    ("test_document.pdf", "application/pdf"),
    ("test_document.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
]

PUBLIC_READ_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{BUCKET}/*"],
        }
    ],
}


def main() -> int:
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    # 1. 建 bucket
    try:
        s3.create_bucket(Bucket=BUCKET)
        print(f"[1/3] bucket '{BUCKET}' 已创建")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"[1/3] bucket '{BUCKET}' 已存在")
        else:
            print(f"[1/3] ERROR 建 bucket: {e}", file=sys.stderr)
            return 1

    # 2. 设 public-read (浏览器代理路径 + litellm 内部 fetch 都能匿名读)
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(PUBLIC_READ_POLICY))
    print(f"[2/3] bucket public-read 策略已设置")

    # 3. 上传测试文件
    for name, ctype in FILES:
        fp = FIXTURES / name
        if not fp.exists():
            print(f"[3/3] ERROR 缺少 fixture: {fp}", file=sys.stderr)
            return 1
        s3.put_object(Bucket=BUCKET, Key=name, Body=fp.read_bytes(), ContentType=ctype)
        print(f"[3/3] 上传 {name} ({fp.stat().st_size} bytes)")

    # 列出
    objs = s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    print(f"\n完成. bucket '{BUCKET}' 现有对象: {[o['Key'] for o in objs]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
