# ── IAM ───────────────────────────────────────────────────────────────────────
#
# AWS Academy no permite crear roles IAM ni policies propias (iam:CreateRole,
# iam:CreatePolicy). Todos los recursos usan el LabRole preexistente, que ya
# tiene permisos amplios sobre los servicios usados (Lambda, DynamoDB, RDS,
# S3, SNS, SQS, Step Functions, Secrets Manager, CloudWatch, etc.).
#
# En un entorno de producción real, cada Lambda tendría su propio rol con
# permisos mínimos (least-privilege): solo las acciones y recursos que
# necesita, nada más.

data "aws_iam_role" "lab_role" {
  name = "LabRole"
}
