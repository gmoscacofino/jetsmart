variable "aws_region" {
  description = "AWS region where the backend resources will be created"
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_suffix" {
  description = "Unique suffix for the S3 state bucket name (must be globally unique)"
  type        = string
}
