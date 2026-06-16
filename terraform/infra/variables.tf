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

# ── Lambda ────────────────────────────────────────────────────────────────────

variable "lambda_timeout" {
  description = "Default timeout in seconds for Lambda functions"
  type        = number
  default     = 30
}

# ── Secrets ───────────────────────────────────────────────────────────────────

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude."
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

# ── Notifications ─────────────────────────────────────────────────────────────

variable "notification_email_subscribers" {
  description = <<-EOT
    Emails que se suscriben al SNS topic de notificaciones (booking confirmed/failed,
    handoff_ack, boarding pass mock). Cada endpoint recibe un mail de AWS "Confirm
    subscription" que tiene que clickearse manualmente — hasta entonces no llegan
    los mails reales.

    Nota arquitectónica: SNS hace fan-out broadcast al topic — todos los suscriptos
    reciben todas las notificaciones, no sólo las suyas. En producción real esto
    iría por SES con destino dinámico por usuario.
  EOT
  type        = list(string)
  default     = []
}
