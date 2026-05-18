output "state_bucket_name" {
  description = "Name of the S3 bucket that stores the Terraform state"
  value       = aws_s3_bucket.terraform_state.bucket
}

output "lock_table_name" {
  description = "Name of the DynamoDB table used for state locking"
  value       = aws_dynamodb_table.terraform_lock.name
}
