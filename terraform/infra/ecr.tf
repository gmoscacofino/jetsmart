# ── ECR: repos de las imágenes Fargate ────────────────────────────────────────
#
# El workflow de GitHub Actions hace `docker build` + `docker push` de las dos
# imágenes a estos repos ANTES del apply completo (crea primero los repos con un
# apply targeteado para evitar el chicken-and-egg). El tag se pasa por
# var.image_tag (= github.sha). force_delete=true permite teardown con imágenes.
#
# Nota Academy: el push lo hacen las credenciales de la CLI/Actions (escritura);
# Fargate pullea con LabRole (lectura). Ver CONTEXT.md.

resource "aws_ecr_repository" "chat_handler" {
  name                 = "${local.name_prefix}-chat-handler"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = false
  }
}

resource "aws_ecr_repository" "weather_poller" {
  name                 = "${local.name_prefix}-weather-poller"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = false
  }
}
