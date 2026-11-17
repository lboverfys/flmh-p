from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from flmh_agent.adapters.outbound.accounts import AccountRequestError


Role = Literal["USER", "ADMIN", "SUPER_ADMIN"]
Membership = Literal["FREE", "BASIC", "PRO", "VOICE_AGENT", "ENTERPRISE", "ENTERPRISE_SUPREME"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ProfileUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=64)
    inviteCodeHidden: StrictBool | None = None


class PasswordUpdate(Input):
    currentPassword: str = Field(min_length=1, max_length=1024, repr=False)
    newPassword: str = Field(min_length=6, max_length=72, repr=False)


class UserCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=72, repr=False)
    phone: str | None = Field(default=None, max_length=64)
    role: Role = "USER"


class UserUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    role: Role | None = None
    status: Literal["ACTIVE", "DISABLED"] | None = None
    password: str | None = Field(default=None, min_length=6, max_length=72, repr=False)


class PointAdjustment(Input):
    amount: StrictInt = Field(ge=-2147483648, le=2147483647)
    reason: str = Field(min_length=1, max_length=500)


class MembershipUpdate(Input):
    membershipLevel: Membership
    membershipExpireAt: str | None = Field(default=None, max_length=40)


class UserQuery(Input):
    search: str = Field(default="", max_length=200)
    role: Role | None = None
    status: Literal["ACTIVE", "DISABLED"] | None = None
    membershipLevel: Membership | None = None
    sortBy: Literal["createdAt", "points"] = "createdAt"
    sortOrder: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=1000000)
    pageSize: int = Field(default=10, ge=1, le=100)


class AccountView(BaseModel):
    id: str
    name: str
    email: str
    role: Role
    status: Literal["ACTIVE", "DISABLED"]
    membershipLevel: Membership
    points: int
    phone: str | None = None
    membershipExpireAt: str | None = None
    agentLevel: str | None = None
    createdAt: str | None = None
    source: Literal["LOCAL_MOCK", "LEGACY_JDBC"]


class ProfileView(AccountView):
    inviteCode: str | None = None
    inviteCodeHidden: bool = False


class UsageView(BaseModel):
    total: int
    succeeded: int
    failed: int
    pending: int


class ParentView(BaseModel):
    name: str


class ListedAccount(AccountView):
    parentAgent: ParentView | None = None
    usage: UsageView | None = None


class UserPage(BaseModel):
    users: list[ListedAccount] = Field(max_length=100)
    total: int
    page: int
    pageSize: int
    totalPages: int


class Success(BaseModel):
    success: Literal[True]


class Balance(BaseModel):
    points: int


class Statistics(BaseModel):
    totalUsers: int
    dailyActiveUsers: int


def account_routes(read_json) -> list[Route]:
    def handler(path: str, inputs: dict, output, *, admin: bool = False):
        async def endpoint(request: Request):
            headers = {"Cache-Control": "no-store"}
            identity = request.state.identity
            if admin and identity.role not in {"ADMIN", "SUPER_ADMIN"}:
                return JSONResponse({"error": "admin_required"}, status_code=403, headers=headers)
            target = request.path_params.get("user_id")
            if target is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", target):
                return JSONResponse({"error": "invalid_account_id"}, status_code=400, headers=headers)
            try:
                payload = None
                params = None
                input_model = inputs.get(request.method)
                if input_model is UserQuery:
                    params = input_model.model_validate(dict(request.query_params)).model_dump(exclude_none=True)
                elif input_model is not None:
                    payload = input_model.model_validate(await read_json(request)).model_dump(exclude_unset=True)
                    if not payload:
                        return JSONResponse({"error": "empty_account_update"}, status_code=400, headers=headers)
            except ValidationError:
                return JSONResponse({"error": "invalid_account_input"}, status_code=422, headers=headers)
            except ValueError as error:
                return JSONResponse({"error": "invalid_account_input"},
                                    status_code=413 if "exceeds" in str(error) else 400, headers=headers)
            try:
                result = await request.app.state.accounts.request(request.method, path.format(user_id=target), identity,
                                                                  params=params, payload=payload)
                response_model = output[request.method] if isinstance(output, dict) else output
                safe_result = response_model.model_validate(result).model_dump(exclude_unset=True)
            except AccountRequestError as error:
                return JSONResponse({"error": str(error)}, status_code=error.status, headers=headers)
            except ValidationError:
                return JSONResponse({"error": "invalid_account_response"}, status_code=502, headers=headers)
            return JSONResponse(safe_result, headers=headers)
        return endpoint

    base = "/api/v1/identity"
    routes = [
        ("/profile", {"GET": None, "PUT": ProfileUpdate}, ProfileView, False),
        ("/password", {"PUT": PasswordUpdate}, Success, False),
        ("/users", {"GET": UserQuery, "POST": UserCreate}, {"GET": UserPage, "POST": AccountView}, True),
        ("/users/stats", {"GET": None}, Statistics, True),
        ("/users/{user_id}", {"PUT": UserUpdate, "DELETE": None}, {"PUT": AccountView, "DELETE": Success}, True),
        ("/users/{user_id}/points", {"PUT": PointAdjustment}, Balance, True),
        ("/users/{user_id}/membership", {"PUT": MembershipUpdate}, AccountView, True),
    ]
    return [Route("/v1/identity" + suffix, handler(base + suffix, inputs, output, admin=admin), methods=list(inputs))
            for suffix, inputs, output, admin in routes]
