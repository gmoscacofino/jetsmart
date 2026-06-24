# ── ECS Fargate ───────────────────────────────────────────────────────────────
#
# Dos workloads en contenedor (el core en VPC, lo que pidió Faustino):
#   - chat-handler   → detrás del ALB, en subnets privadas, con Auto Scaling.
#   - weather-poller → solo egress (NAT → climAPI), sin ALB, desired_count=1.
#
# execution_role y task_role = LabRole (limitación Academy; CONTEXT.md confirma
# que LabRole se usa como task/execution role de ECS).

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"
}

resource "aws_cloudwatch_log_group" "chat_handler" {
  name              = "/ecs/${local.name_prefix}-chat-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "weather_poller" {
  name              = "/ecs/${local.name_prefix}-weather-poller"
  retention_in_days = 30
}

# ── Task Definition: chat-handler ─────────────────────────────────────────────

resource "aws_ecs_task_definition" "chat_handler" {
  family                   = "${local.name_prefix}-chat-handler"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = data.aws_iam_role.lab_role.arn
  task_role_arn            = data.aws_iam_role.lab_role.arn

  container_definitions = jsonencode([
    {
      name      = "chat-handler"
      image     = "${aws_ecr_repository.chat_handler.repository_url}:${var.image_tag}"
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = [
        { name = "AWS_REGION_VAR", value = var.aws_region },
        { name = "CONVERSATIONS_TABLE_NAME", value = aws_dynamodb_table.conversations.name },
        { name = "BUSINESS_TABLE_NAME", value = aws_dynamodb_table.business.name },
        { name = "SNS_TOPIC_ARN", value = aws_sns_topic.events.arn },
        { name = "ANTHROPIC_SECRET_ARN", value = aws_secretsmanager_secret.anthropic_key.arn },
        { name = "STEP_FUNCTIONS_ARN", value = aws_sfn_state_machine.booking.arn },
        { name = "FRONTEND_URL", value = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}" },
        { name = "PII_TOKEN_SECRET", value = random_password.pii_token_secret.result },
        { name = "COGNITO_USER_POOL_ID", value = module.auth.user_pool_id },
        { name = "COGNITO_CLIENT_ID", value = module.auth.client_id },
        { name = "COGNITO_REGION", value = var.aws_region },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.chat_handler.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "chat-handler"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "chat_handler" {
  name                              = "${local.name_prefix}-chat-handler"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.chat_handler.arn
  desired_count                     = 2 # 1 por AZ → Multi-AZ real
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.private_fargate[*].id
    security_groups  = [aws_security_group.chat.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.chat.arn
    container_name   = "chat-handler"
    container_port   = 8000
  }

  # El Auto Scaling maneja el desired_count en runtime — no pelear con Terraform.
  lifecycle {
    ignore_changes = [desired_count]
  }

  # La red tiene que estar lista antes de lanzar tasks: el ALB para registrar
  # targets, y los endpoints/NAT para que la task pullee la imagen (ECR) y loguee.
  depends_on = [
    aws_lb_listener.http,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
    aws_nat_gateway.main,
  ]
}

# ── Auto Scaling del chat-handler (elasticidad) ───────────────────────────────

resource "aws_appautoscaling_target" "chat" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.chat_handler.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 2
  max_capacity       = 6
}

resource "aws_appautoscaling_policy" "chat_cpu" {
  name               = "${local.name_prefix}-chat-cpu-tracking"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.chat.service_namespace
  resource_id        = aws_appautoscaling_target.chat.resource_id
  scalable_dimension = aws_appautoscaling_target.chat.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 60
    scale_out_cooldown = 60
  }
}

# ── Task Definition: weather-poller ───────────────────────────────────────────

resource "aws_ecs_task_definition" "weather_poller" {
  family                   = "${local.name_prefix}-weather-poller"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = data.aws_iam_role.lab_role.arn
  task_role_arn            = data.aws_iam_role.lab_role.arn

  container_definitions = jsonencode([
    {
      name      = "weather-poller"
      image     = "${aws_ecr_repository.weather_poller.repository_url}:${var.image_tag}"
      essential = true
      environment = [
        { name = "AWS_REGION_VAR", value = var.aws_region },
        { name = "BUSINESS_TABLE_NAME", value = aws_dynamodb_table.business.name },
        { name = "WEATHER_SECRET_ARN", value = aws_secretsmanager_secret.weather_key.arn },
        { name = "CLIMA_API_BASE", value = var.clima_api_base },
        { name = "FORECAST_INTERVAL_SECONDS", value = tostring(var.weather_poll_interval_seconds) }, # planning: 30 min
        { name = "CURRENT_INTERVAL_SECONDS", value = "300" },                                        # go/no-go: 5 min
        { name = "ACTIVE_FLIGHT_STATES", value = "EN_HORARIO,DEMORADO" },
        { name = "LOOKAHEAD_HOURS", value = "48" },     # ventana del forecast
        { name = "CURRENT_WINDOW_HOURS", value = "2" }, # ventana near-departure del current
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.weather_poller.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "weather-poller"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "weather_poller" {
  name            = "${local.name_prefix}-weather-poller"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.weather_poller.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private_fargate[*].id
    security_groups  = [aws_security_group.poller.id]
    assign_public_ip = false
  }

  depends_on = [
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
    aws_nat_gateway.main,
  ]
}
