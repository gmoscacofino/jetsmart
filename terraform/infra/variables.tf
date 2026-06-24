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

variable "weather_api_key" {
  description = "climAPI key for the weather-poller (Fargate)."
  type        = string
  sensitive   = true
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block de la VPC"
  type        = string
  default     = "10.0.0.0/16"
}

# ── Fargate / ECR ─────────────────────────────────────────────────────────────

variable "image_tag" {
  description = "Tag de las imágenes ECR (chat-handler / weather-poller). El workflow pasa github.sha."
  type        = string
  default     = "latest"
}

# ── Weather poller ────────────────────────────────────────────────────────────

variable "clima_api_base" {
  description = "Base URL de WeatherAPI.com (endpoint /forecast.json)."
  type        = string
  default     = "https://api.weatherapi.com/v1"
}

variable "weather_poll_interval_seconds" {
  description = "Cadencia de la pasada FORECAST del weather-poller en segundos (la pasada CURRENT corre cada 5 min)"
  type        = number
  default     = 1800
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
