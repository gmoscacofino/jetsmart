# ── Bastion EC2 para acceso al equipo de analytics ────────────────────────────
#
# t3.micro en subnet publica con SSM Agent. Sin SSH keys, sin puerto 22 abierto.
# El equipo de analytics se conecta con port-forwarding via SSM:
#
#   aws ssm start-session \
#     --target <bastion_instance_id> \
#     --document-name AWS-StartPortForwardingSessionToRemoteHost \
#     --parameters '{"host":["<rds_endpoint>"],"portNumber":["5432"],"localPortNumber":["5433"]}'
#
# Luego conectar el cliente PostgreSQL a localhost:5433.
# Requiere AWS CLI + Session Manager plugin instalados localmente.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

data "aws_iam_instance_profile" "lab" {
  name = "LabInstanceProfile"
}

resource "aws_security_group" "bastion" {
  name        = "${local.name_prefix}-sg-bastion"
  description = "Bastion SSM outbound only"
  vpc_id      = module.vpc.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound for SSM and RDS"
  }
}

resource "aws_instance" "bastion" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = "t3.micro"
  subnet_id                   = module.vpc.public_subnets[0]
  vpc_security_group_ids      = [aws_security_group.bastion.id]
  iam_instance_profile        = data.aws_iam_instance_profile.lab.name
  associate_public_ip_address = true

  user_data = <<-EOF
    #!/bin/bash
    yum install -y amazon-ssm-agent
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
  EOF

  tags = {
    Name = "${local.name_prefix}-bastion"
  }
}

resource "aws_security_group_rule" "rds_from_bastion" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.bastion.id
  security_group_id        = aws_security_group.rds.id
  description              = "PostgreSQL from bastion SSM"
}

resource "aws_security_group_rule" "proxy_from_bastion" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.bastion.id
  security_group_id        = aws_security_group.rds_proxy.id
  description              = "PostgreSQL from bastion SSM via proxy"
}

output "bastion_instance_id" {
  description = "ID del bastion para SSM port forwarding"
  value       = aws_instance.bastion.id
}
