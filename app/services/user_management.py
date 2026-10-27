"""Admin-only account API. Passwords and validation inputs never appear in responses."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, SecretStr, StrictStr

from app.services import auth


class AccountRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default validation output can echo submitted passwords.
                response = JSONResponse({"detail": "輸入資料不合法，請檢查必填欄位"}, status_code=422)
            except auth.AccountError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            response.headers["Cache-Control"] = "no-store"
            return response

        return private_handler


def require_admin(request: Request):
    user = auth.get_user_by_token(request.cookies.get("session"))
    if user is None:
        raise HTTPException(401, "未登入")
    if not user.get("is_admin"):
        raise HTTPException(403, "需管理員權限")
    return user


class CreateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: StrictStr
    name: StrictStr
    password: SecretStr


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: StrictStr
    name: StrictStr
    password: SecretStr | None = None


class PublicUser(BaseModel):
    id: int
    username: str
    name: str
    is_admin: bool
    is_active: bool
    created_at: datetime


router = APIRouter(prefix="/api/users", tags=["users"], route_class=AccountRoute)


@router.get("", response_model=list[PublicUser])
def get_users(actor=Depends(require_admin)):
    return auth.list_users(actor_id=actor["id"])


@router.post("", status_code=201, response_model=PublicUser)
def post_user(body: CreateUser, actor=Depends(require_admin)):
    return auth.create_user(body.username, body.name, body.password.get_secret_value(), actor_id=actor["id"])


@router.delete("/{user_id}", response_model=PublicUser)
def delete_user(user_id: int, actor=Depends(require_admin)):
    return auth.disable_user(user_id, actor_id=actor["id"])


@router.patch("/{user_id}", response_model=PublicUser)
def patch_user(user_id: int, body: UpdateUser, actor=Depends(require_admin)):
    password = body.password.get_secret_value() if body.password is not None else None
    return auth.update_user(user_id, body.username, body.name, password, actor_id=actor["id"])
