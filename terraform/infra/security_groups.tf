# ── Security Groups ───────────────────────────────────────────────────────────
#
# Patrón de least-privilege de red: el INBOUND se encadena por referencia de SG
# (no por CIDR), de modo que sólo el SG de origen correcto puede hablar con el
# destino. El egress se deja abierto en los SG de cómputo por correctness
# (DNS + 443 a endpoints/NAT); en producción real se acotaría el egress.
#
#   sg-alb       in 80  ← 0.0.0.0/0          → sg-chat:8000
#   sg-chat      in 8000 ← sg-alb            → all (NAT: Anthropic/JWKS, endpoints)
#   sg-poller    in —                         → all (NAT: climAPI, endpoints)
#   sg-lambda    in —                         → all (endpoints, NAT si hace falta)
#   sg-endpoints in 443  ← sg-chat/poller/lambda

# ── ALB ───────────────────────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-sg-alb"
  description = "ALB publico - HTTP 80 desde internet"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-sg-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP desde internet"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_chat" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward al target group de chat-handler"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.chat.id
}

# ── Fargate: chat-handler ─────────────────────────────────────────────────────

resource "aws_security_group" "chat" {
  name        = "${local.name_prefix}-sg-chat"
  description = "Fargate chat-handler - inbound solo desde el ALB"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-sg-chat" }
}

resource "aws_vpc_security_group_ingress_rule" "chat_from_alb" {
  security_group_id            = aws_security_group.chat.id
  description                  = "Trafico del ALB al contenedor"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "chat_all" {
  security_group_id = aws_security_group.chat.id
  description       = "Egress a endpoints + NAT (Anthropic, Cognito JWKS)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ── Fargate: weather-poller ───────────────────────────────────────────────────

resource "aws_security_group" "poller" {
  name        = "${local.name_prefix}-sg-poller"
  description = "Fargate weather-poller - solo egress (sin inbound)"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-sg-poller" }
}

resource "aws_vpc_security_group_egress_rule" "poller_all" {
  security_group_id = aws_security_group.poller.id
  description       = "Egress a endpoints + NAT (climAPI)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ── Lambdas en VPC ────────────────────────────────────────────────────────────

resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-sg-lambda"
  description = "Lambdas en subnets privadas - solo egress"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-sg-lambda" }
}

resource "aws_vpc_security_group_egress_rule" "lambda_all" {
  security_group_id = aws_security_group.lambda.id
  description       = "Egress a endpoints + NAT"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ── Interface VPC Endpoints ───────────────────────────────────────────────────

resource "aws_security_group" "endpoints" {
  name        = "${local.name_prefix}-sg-endpoints"
  description = "Interface endpoints - 443 desde el computo de la VPC"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-sg-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_chat" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS desde chat-handler"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.chat.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_poller" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS desde weather-poller"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.poller.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_lambda" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS desde las Lambdas en VPC"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.lambda.id
}

resource "aws_vpc_security_group_egress_rule" "endpoints_all" {
  security_group_id = aws_security_group.endpoints.id
  description       = "Respuestas de los endpoints"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
