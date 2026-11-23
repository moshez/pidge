"""Shared helpers for the Cognito scripts.

UpdateUserPool and UpdateUserPoolClient reset every setting the request
leaves out, so these helpers resend what Describe returned.
"""

from collections.abc import Mapping

# Describe returns both the legacy verification fields and the template;
# sending both makes UpdateUserPool fail, so the template wins.
LEGACY_VERIFICATION = (
    "EmailVerificationMessage",
    "EmailVerificationSubject",
    "SmsVerificationMessage",
)


def update_members(client: object, operation: str) -> frozenset[str]:
    """List the request members an operation accepts.

    Args:
        client: A boto3 cognito-idp client.
        operation: The operation name, such as ``UpdateUserPool``.

    Returns:
        The member names.
    """
    model = getattr(client, "meta").service_model.operation_model(operation)
    return frozenset(model.input_shape.members)


def pool_update(client: object, described: Mapping[str, object]) -> dict[str, object]:
    """Build an UpdateUserPool request that keeps every current setting.

    Args:
        client: A boto3 cognito-idp client.
        described: The ``UserPool`` member of DescribeUserPool.

    Returns:
        The request (without ``UserPoolId``).
    """
    accepted = update_members(client, "UpdateUserPool")
    request = {key: value for key, value in described.items() if key in accepted}
    if "VerificationMessageTemplate" in request:
        for key in LEGACY_VERIFICATION:
            request.pop(key, None)
    admin = request.get("AdminCreateUserConfig")
    if isinstance(admin, dict):
        # Deprecated in favour of PasswordPolicy.TemporaryPasswordValidityDays;
        # sending both is rejected.
        admin.pop("UnusedAccountValidityDays", None)
    return request
