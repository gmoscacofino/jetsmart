locals {
  # Prefijo usado en el nombre de todos los recursos
  name_prefix = "${var.project_name}-${var.environment}"

  # Tags aplicados a todos los recursos via default_tags del provider
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
  }

  # AZs disponibles (primeras 2 de la región)
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  # CIDRs de subnets calculados automáticamente desde el CIDR de la VPC
  public_subnet_cidrs          = [cidrsubnet(var.vpc_cidr, 8, 1)]
  private_compute_subnet_cidrs = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i + 3)]
  private_data_subnet_cidrs    = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i + 5)]
}
