# The raw store (content-addressed blobs) + Litestream DB backups live here.
# One bucket, two prefixes: raw/ and litestream/.

resource "aws_s3_bucket" "raw" {
  bucket = var.raw_bucket_name
}

# Keep old versions so an accidental overwrite/delete is recoverable.
resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt objects at rest (SSE-S3, no key management for you).
resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# This is a private store — block every avenue of public access.
resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Cost control (plan §14.2): raw blobs are write-once/read-rarely, so slide them
# to Infrequent Access after 90 days. Old versions expire after 90 days too.
resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    id     = "raw-to-ia"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
