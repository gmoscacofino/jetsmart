"""
Lambda: Cognito Post-Confirmation trigger.

Runs after a user confirms registration. Automatically assigns
them to the 'users' Cognito group so the frontend routes them
to the chat screen (not admin).
"""
import os, logging
import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

cognito = boto3.client("cognito-idp")
GROUP   = "users"


def handler(event, context):
    user_pool_id = event["userPoolId"]
    username     = event["userName"]
    trigger      = event.get("triggerSource", "")

    # Only run on new confirmed users, not on admin-created accounts
    if trigger != "PostConfirmation_ConfirmSignUp":
        return event

    try:
        cognito.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=username,
            GroupName=GROUP,
        )
        log.info("Added %s to group %s", username, GROUP)
    except cognito.exceptions.ResourceNotFoundException:
        log.warning("Group %s not found in pool %s", GROUP, user_pool_id)
    except Exception as e:
        log.error("Failed to add user to group: %s", e)
        # Do NOT raise — a trigger failure blocks the user from confirming

    return event
