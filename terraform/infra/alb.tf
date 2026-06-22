# ── Application Load Balancer (HTTP) ──────────────────────────────────────────
#
# Punto de entrada del chat-handler (el core, en Fargate). HTTP :80 — Academy no
# habilita ACM, así que no hay listener HTTPS; la validación del JWT de Cognito
# se hace DENTRO del contenedor (server.py valida contra el JWKS del User Pool).
# En producción real: ACM + listener 443 + auth Cognito nativa en el ALB.

resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${local.name_prefix}-alb" }
}

resource "aws_lb_target_group" "chat" {
  name        = "${local.name_prefix}-chat-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate awsvpc → targets por IP

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.name_prefix}-chat-tg" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.chat.arn
  }
}
