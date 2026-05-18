variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as prefix in all resource names"
  type        = string
  default     = "jetsmart"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment debe ser dev, staging o prod."
  }
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

# ── Lambda ────────────────────────────────────────────────────────────────────

variable "lambda_timeout" {
  description = "Default timeout in seconds for Lambda functions"
  type        = number
  default     = 30
}

# ── Database ──────────────────────────────────────────────────────────────────

variable "rds_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.micro"
}

variable "rds_allocated_storage" {
  description = "Allocated storage in GB for RDS"
  type        = number
  default     = 20
}

variable "rds_db_name" {
  description = "Name of the initial database in RDS"
  type        = string
  default     = "jetsmart_analytics"
}

variable "rds_username" {
  description = "Master username for RDS"
  type        = string
  default     = "jetsmart_admin"
  sensitive   = true
}

variable "rds_password" {
  description = "Master password for RDS"
  type        = string
  sensitive   = true
}

# ── Secrets ───────────────────────────────────────────────────────────────────

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude — never commit this value"
  type        = string
  sensitive   = true
}

# ── Chatbot ───────────────────────────────────────────────────────────────────

variable "rutas_disponibles" {
  description = "List of available JetSmart routes injected into the Claude system prompt"
  type        = list(string)
  default = [
    "Buenos Aires (AEP) → Mendoza (MDZ)",
    "Buenos Aires (AEP) → Córdoba (COR)",
    "Buenos Aires (AEP) → Bariloche (BRC)",
    "Buenos Aires (AEP) → Salta (SLA)",
    "Buenos Aires (AEP) → Iguazú (IGR)",
    "Mendoza (MDZ) → Buenos Aires (AEP)",
    "Córdoba (COR) → Buenos Aires (AEP)"
  ]
}

# ── S3 ────────────────────────────────────────────────────────────────────────

variable "state_bucket_suffix" {
  description = "Suffix used in the S3 state bucket name — must match the value used in 00-backend"
  type        = string
}
