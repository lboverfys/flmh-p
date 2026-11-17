from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from flmh_agent.adapters.inbound.client_ip import client_address
from flmh_agent.domain.identity import AuthenticationError, IdentityInputError, IdentityRateLimitedError, IdentityUnavailableError


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=320)


class ResetRequest(RecoveryRequest):
    code: str = Field(pattern=r"^[0-9]{6}$", repr=False)
    newPassword: str = Field(min_length=6, max_length=72, repr=False)

    @field_validator("newPassword")
    @classmethod
    def password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password exceeds 72 UTF-8 bytes")
        return value


def recovery_routes(json_body) -> list[Route]:
    async def endpoint(request: Request):
        headers = {"Cache-Control": "no-store"}
        try:
            ip = client_address(request, request.app.state.settings)
            if request.url.path.endswith("/forgot-password"):
                payload = RecoveryRequest.model_validate(await json_body(request))
                result = await request.app.state.identity_recovery.forgot_password(payload.email.strip(), client_ip=ip)
            else:
                payload = ResetRequest.model_validate(await json_body(request))
                result = await request.app.state.identity_recovery.reset_password(
                    payload.email.strip(), payload.code, payload.newPassword, client_ip=ip)
        except ValidationError:
            return JSONResponse({"error": "invalid_recovery_input"}, status_code=422, headers=headers)
        except IdentityRateLimitedError:
            return JSONResponse({"error": "too_many_authentication_attempts"}, status_code=429, headers=headers)
        except IdentityInputError:
            return JSONResponse({"error": "invalid_or_expired_recovery_input"}, status_code=400, headers=headers)
        except AuthenticationError:
            return JSONResponse({"error": "identity_service_unauthorized"}, status_code=503, headers=headers)
        except IdentityUnavailableError:
            return JSONResponse({"error": "identity_service_unavailable"}, status_code=503, headers=headers)
        except ValueError as error:
            return JSONResponse({"error": "invalid_recovery_input"}, status_code=413 if "exceeds" in str(error) else 400, headers=headers)
        return JSONResponse(result, headers=headers)

    return [Route("/v1/identity/forgot-password", endpoint, methods=["POST"]),
            Route("/v1/identity/reset-password", endpoint, methods=["POST"])]
