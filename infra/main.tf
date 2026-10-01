terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  default = "us-east-1"
}

variable "ntfy_topic" {
  type      = string
  sensitive = true
}

variable "ui_password" {
  description = "password the web page asks for once per device; update.sh asks you for one"
  type        = string
  sensitive   = true
}

variable "budget_email" {
  description = "optional: email for an AWS budget alert if the bill ever goes above $1/month"
  type        = string
  default     = ""
}

variable "timezone" {
  default = "America/Toronto"
}

data "aws_caller_identity" "me" {}

locals {
  name = "job-watcher"
}

# ---------------------------------------------------------------- state bucket (holds jobs.db)
resource "aws_s3_bucket" "state" {
  bucket        = "${local.name}-state-${data.aws_caller_identity.me.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------- lambda
data "archive_file" "code" {
  type        = "zip"
  output_path = "${path.module}/build/job-watcher.zip"
  source_dir  = "${path.module}/.."
  excludes = ["infra", "jobs.db", "jobs.db-wal", "jobs.db-shm", "jobs.db.scan.lock", "config.env", "config.env.example",
    "README.md", ".gitignore", "__pycache__", ".git", "watcher.log", "serve.log", "status.json", "open_jobs.json", "deploy.sh",
  "update.sh", "site", "crontab.before-deploy", "docs"]
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

# least privilege: its own log group + its one state object
resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.lambda.arn}:*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["${aws_s3_bucket.state.arn}/jobs.db", "${aws_s3_bucket.state.arn}/status.json"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "watcher" {
  function_name    = local.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 600
  memory_size      = 1024 # peak ~600 MB (Simplify list is 13 MB of JSON); ~170k GB-s/month, free tier is 400k
  environment {
    variables = {
      JOBS_BUCKET      = aws_s3_bucket.state.id
      JOBS_NTFY_TOPIC  = var.ntfy_topic
      JOBS_UI_PASSWORD = var.ui_password
    }
  }
  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ---------------------------------------------------------------- schedules
resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.watcher.arn }]
  })
}

locals {
  schedules = {
    # :00/:15/:30/:45 so the :00 run is the hourly S3 save (see lambda_function.py)
    scan           = { expr = "cron(0/15 * * * ? *)", input = { action = "scan" } }
    digest-morning = { expr = "cron(5 8 * * ? *)", input = { action = "digest", hours = 12 } }
    digest-night   = { expr = "cron(5 21 * * ? *)", input = { action = "digest", hours = 13 } }
  }
}

resource "aws_scheduler_schedule" "s" {
  for_each                     = local.schedules
  name                         = "${local.name}-${each.key}"
  schedule_expression          = each.value.expr
  schedule_expression_timezone = var.timezone
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.watcher.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode(each.value.input)
    retry_policy {
      maximum_retry_attempts = 0 # next scan is only 15 min away
    }
  }
}

# ---------------------------------------------------------------- web UI (public link; the page asks for ui_password)
resource "aws_lambda_function_url" "ui" {
  function_name      = aws_lambda_function.watcher.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "ui" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.watcher.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
# AWS also wants lambda:InvokeFunction limited to function-URL calls; this provider version can't
# express that condition, so deploy.sh adds it with `aws lambda add-permission --invoked-via-function-url`.

# ---------------------------------------------------------------- optional $1 budget alarm (budgets are free)
resource "aws_budgets_budget" "guard" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "job-watcher-guard"
  budget_type  = "COST"
  limit_amount = "1"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

output "ui_url" {
  value = aws_lambda_function_url.ui.function_url
}

output "bucket" {
  value = aws_s3_bucket.state.id
}

output "function" {
  value = aws_lambda_function.watcher.function_name
}
