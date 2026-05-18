variable "name_prefix" {
  description = "Prefix used in all resource names"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "frontend_url" {
  description = "S3 frontend URL used as Cognito callback and logout URL"
  type        = string
}

variable "environment" {
  description = "Deployment environment (used as API Gateway stage name)"
  type        = string
}
