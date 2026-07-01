import logging

from fastapi import APIRouter, Security

from ..authentication import get_current_principal
from ..re_manager_schemas import (
    PermissionsGetResponse,
    SuccessMsgResponse,
)
from ..resources import SERVER_RESOURCES as SR
from ..utils import (
    process_exception,
)

logger = logging.getLogger(__name__)

permissions_router = APIRouter(prefix="/api")


@permissions_router.post(
    "/permissions/reload",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Reload permissions from disk",
    description=(
        "Reload allowed-plans, allowed-devices, and user-group-permissions definitions from "
        "the paths configured on the manager. Use after editing the underlying files on "
        "disk. Required scope: `write:config`."
    ),
    tags=["Permissions"],
)
async def permissions_reload_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:config"]),
):
    """
    Reloads the list of allowed plans and devices and user group permission from the default location
    or location set using command line parameters of RE Manager. Use this request to reload the data
    if the respective files were changed on disk.
    """
    try:
        msg = await SR.RM.permissions_reload(**payload)
    except Exception:
        process_exception()
    return msg


@permissions_router.get(
    "/permissions/get",
    response_model=PermissionsGetResponse,
    response_model_exclude_unset=True,
    summary="Get user-group permissions",
    description=(
        "Returns the current user-group permissions dictionary. "
        "Required scope: `read:config`."
    ),
    tags=["Permissions"],
)
async def permissions_get_handler(principal=Security(get_current_principal, scopes=["read:config"])):
    """
    Download the dictionary of user group permissions.
    """
    try:
        msg = await SR.RM.permissions_get()
    except Exception:
        process_exception()
    return msg


@permissions_router.post(
    "/permissions/set",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Set user-group permissions",
    description=(
        "Replace the current user-group permissions. Parameter: `user_group_permissions` "
        "(dict). Required scope: `write:permissions`."
    ),
    tags=["Permissions"],
)
async def permissions_set_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["write:permissions"])
):
    """
    Upload the dictionary of user group permissions (parameter: ``user_group_permissions``).
    """
    try:
        if "user_group_permissions" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="permissions_set", params=payload)
        else:
            msg = await SR.RM.permissions_set(**payload)
    except Exception:
        process_exception()
    return msg
