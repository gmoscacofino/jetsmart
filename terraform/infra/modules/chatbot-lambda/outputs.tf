output "api_url" {
  description = "Base URL of the chatbot API Gateway (add /api/chat, /api/reservations, /api/payment)"
  value       = aws_api_gateway_stage.prod.invoke_url
}

output "chat_handler_arn" {
  description = "ARN of the chat handler Lambda"
  value       = aws_lambda_function.chat_handler.arn
}
