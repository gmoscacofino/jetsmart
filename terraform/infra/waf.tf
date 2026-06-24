# ── WAF (AWS WAFv2) del ALB del chat-handler ──────────────────────────────────
#
# Web ACL REGIONAL asociado al ALB público del chat-handler — la única superficie
# que recibe input del usuario (va a Anthropic + DynamoDB). default_action = allow;
# las reglas bloquean lo malicioso:
#   1. Common Rule Set (CRS)  → XSS y patrones OWASP genéricos
#   2. Known Bad Inputs       → payloads/exploits conocidos (Log4j, etc.)
#   3. SQLi Rule Set          → inyección SQL (defensa en profundidad)
#   4. Rate-based 2000/5min   → bots / DoS aplicativo por IP
#
# El API Gateway de auth no se asocia: ya tiene throttling_rate_limit=5 y sólo
# habla con Cognito (sin input de negocio del usuario).

resource "aws_wafv2_web_acl" "chat" {
  name        = "${local.name_prefix}-chat-waf"
  description = "WAF del ALB del chat-handler: managed rules XSS/SQLi/bad inputs + rate limit por IP"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "common-rule-set"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "known-bad-inputs"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "sqli-rule-set"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-sqli-rule-set"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-per-ip"
    priority = 4

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-rate-limit-per-ip"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-chat-waf"
    sampled_requests_enabled   = true
  }

  tags = {
    Name = "${local.name_prefix}-chat-waf"
  }
}

resource "aws_wafv2_web_acl_association" "chat_alb" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.chat.arn
}
