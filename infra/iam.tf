# The EC2 box assumes this role via an "instance profile" — so the app on the box
# talks to S3 with NO access keys on disk (plan §14.3). Credentials are handed to
# the instance automatically and rotated by AWS.

data "aws_caller_identity" "current" {}

# Trust policy: only EC2 may assume this role.
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "box" {
  name               = "${var.project}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Permissions: read/write ONLY this bucket, and read secrets under the project's
# SSM path (API keys, IMAP creds — added in a later phase). Nothing else.
data "aws_iam_policy_document" "box" {
  statement {
    sid       = "RawBucketObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.raw.arn}/*"]
  }
  statement {
    sid       = "RawBucketList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.raw.arn]
  }
  statement {
    sid       = "ReadProjectSecrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/*"]
  }
}

resource "aws_iam_role_policy" "box" {
  name   = "${var.project}-ec2-policy"
  role   = aws_iam_role.box.id
  policy = data.aws_iam_policy_document.box.json
}

resource "aws_iam_instance_profile" "box" {
  name = "${var.project}-ec2"
  role = aws_iam_role.box.name
}
