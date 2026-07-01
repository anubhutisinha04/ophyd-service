import io
import logging
import pprint
from typing import Optional

import pydantic
from queueserver_service.manager.conversions import spreadsheet_to_plan_list
from fastapi import APIRouter, Depends, File, Form, Security, UploadFile
from packaging import version

if version.parse(pydantic.__version__) < version.parse("2.0.0"):
    from pydantic import BaseSettings
else:
    from pydantic_settings import BaseSettings

from ..authentication import get_current_principal
from ..re_manager_schemas import (
    HistoryGetResponse,
    ItemGetResponse,
    ItemResponse,
    ItemsBatchResponse,
    QueueGetResponse,
    SuccessMsgResponse,
)
from ..resources import SERVER_RESOURCES as SR
from ..settings import get_settings
from ..utils import (
    get_api_access_manager,
    get_current_username,
    get_resource_access_manager,
    process_exception,
)

logger = logging.getLogger(__name__)

queue_router = APIRouter(prefix="/api")


@queue_router.post(
    "/queue/autostart",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Enable or disable queue autostart",
    description=(
        "When autostart is enabled, the queue starts automatically once the environment "
        "is opened, and the manager resumes the queue automatically after a plan pauses. "
        "Parameter: `enable` (bool). Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_autostart_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:control"]),
):
    """
    Enable or disable queue autostart.
    """
    try:
        msg = await SR.RM.queue_autostart(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/mode/set",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Set queue execution mode",
    description=(
        "Configure queue-level execution options such as `loop` (re-run the queue "
        "indefinitely) or `ignore_failures` (continue after a failed plan). "
        "Parameter: `mode` (dict of option names to values). "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_mode_set_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:control"]),
):
    """
    Set queue mode.
    """
    try:
        msg = await SR.RM.queue_mode_set(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.get(
    "/queue/get",
    response_model=QueueGetResponse,
    response_model_exclude_unset=True,
    summary="Get queue contents",
    description=(
        "Returns the current queue — the list of queued items, the currently running "
        "item (if any), the `plan_queue_uid` used for change detection, and the active "
        "`running_item_uid`. Required scope: `read:queue`."
    ),
    tags=["Queue"],
)
async def queue_get_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])):
    """
    Returns the contents of the current queue.
    """
    try:
        msg = await SR.RM.queue_get(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/clear",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Clear the queue",
    description=(
        "Remove all items from the queue. The currently running plan is not affected. "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue"],
)
async def queue_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:edit"])
):
    """
    Clear the plan queue.
    """
    try:
        msg = await SR.RM.queue_clear(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/start",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Start queue execution",
    description=(
        "Begin executing items from the queue. Additional items can be added to the queue "
        "while it is running. If the queue is empty, the request succeeds and nothing runs. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_start_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Start execution of the loaded queue. Additional runs can be added to the queue while
    it is executed. If the queue is empty, then nothing will happen.
    """
    try:
        msg = await SR.RM.queue_start(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/stop",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Request queue stop after current plan",
    description=(
        "Request the queue to stop after the currently running plan completes. The running "
        "plan itself is not interrupted. Rejected if no plan is currently running. "
        "Use `/queue/stop/cancel` to back out of a pending stop request. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_stop(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Activate the sequence of stopping the queue. The currently running plan will be completed,
    but the next plan will not be started. The request will be rejected if no plans are currently
    running
    """
    try:
        msg = await SR.RM.queue_stop(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/stop/cancel",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Cancel a pending queue-stop request",
    description=(
        "Cancel a previously-issued `/queue/stop` request while the running plan has not "
        "yet completed. Always succeeds; a no-op if no stop is pending. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_stop_cancel(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Cancel pending request to stop the queue while the current plan is still running.
    It may be useful if the `/queue/stop` request was issued by mistake or the operator
    changed his mind. Since `/queue/stop` takes effect only after the currently running
    plan is completed, user may have time to cancel the request and continue execution of
    the queue. The command always succeeds, but it has no effect if no queue stop
    requests are pending.
    """
    try:
        msg = await SR.RM.queue_stop_cancel(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/add",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Add an item to the queue",
    description=(
        "Add a single plan, instruction, or function to the queue. Parameter: `item` (dict) "
        "describes what to run; `pos` / `before_uid` / `after_uid` optionally control where "
        "in the queue the item is inserted. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_add_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add", params=payload)
        else:
            msg = await SR.RM.item_add(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/execute",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Execute an item immediately",
    description=(
        "Execute the supplied item once, outside the queue. The item does not join the queue "
        "and does not appear in queue listings. Parameter: `item` (dict). "
        "Required scope: `write:execute`."
    ),
    tags=["Queue Items"],
)
async def queue_item_execute_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:execute"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Immediately execute an item
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_execute", params=payload)
        else:
            msg = await SR.RM.item_execute(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/add/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Add a batch of items to the queue",
    description=(
        "Add multiple items to the queue in a single request. Parameter: `items` (list of "
        "item dicts). The server validates each item; per-item success/failure is returned "
        "in the response. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_add_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "items" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add_batch", params=payload)
        else:
            msg = await SR.RM.item_add_batch(**payload)
    except Exception:
        process_exception()

    return msg


@queue_router.post(
    "/queue/upload/spreadsheet",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Upload a spreadsheet and enqueue the resulting plans",
    description=(
        "Multipart upload: a spreadsheet file is processed (either by a user-provided "
        "`spreadsheet_to_plan_list` function in a loaded custom module or by the default "
        "processor) and the resulting plan list is added to the queue as a batch. "
        "Form fields: `spreadsheet` (file), `data_type` (optional str — hint used by custom "
        "processors to pick a parsing strategy). Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_upload_spreadsheet(
    spreadsheet: UploadFile = File(...),
    data_type: Optional[str] = Form(None),
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    The endpoint receives uploaded spreadsheet, converts it to the list of plans and adds
    the plans to the queue.

    Parameters
    ----------
    spreadsheet : File
        uploaded excel file
    data_type : str
        user defined spreadsheet type, which determines which processing function is used to
        process the spreadsheet.

    Returns
    -------
    success : boolean
        Indicates if the spreadsheet was successfully converted to a sequence of plans.
        ``True`` value does not indicate that the plans were accepted by the RE Manager and
        successfully added to the queue.
    msg : str
        Error message in case of failure to process the spreadsheet
    item_list : list(dict)
        The list of parameter dictionaries returned by RE Manager in response to requests
        to add each plan in the list. Check ``success`` parameter in each dictionary to
        see if the plan was accepted and ``msg`` parameter for an error message in case
        the plan was rejected. The list may be empty if the spreadsheet contains no items
        or processing of the spreadsheet failed.
    """
    try:
        # Create fully functional file object. The file object returned by FastAPI is not fully functional.
        # Use UploadFile.read() (async) so a large upload does not block the event loop.
        f = io.BytesIO(await spreadsheet.read())
        # File name is also passed to the processing function (may be useful in user created
        #   processing code, since processing may differ based on extension or file name)
        f_name = spreadsheet.filename
        logger.info(f"Spreadsheet file '{f_name}' was uploaded")

        # Determine which processing function should be used
        item_list = []
        processed = False

        # Select custom module from the list of loaded modules
        custom_module = None
        for module in SR.custom_code_modules:
            if "spreadsheet_to_plan_list" in module.__dict__:
                custom_module = module
                break

        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)

        if custom_module:
            logger.info("Processing spreadsheet using function from external module ...")
            # Try applying  the custom processing function. Some additional useful data is passed to
            #   the function. Unnecessary parameters can be ignored.
            item_list = custom_module.spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
            )
            # The function is expected to return None if it rejects the file (based on 'data_type').
            #   Then try to apply the default processing function.
            processed = item_list is not None

        if not processed:
            # Apply default spreadsheet processing function.
            logger.info("Processing spreadsheet using default function ...")
            item_list = spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
            )

        if item_list is None:
            raise RuntimeError("Failed to process the spreadsheet: unsupported data type or format")

        # Since 'item_list' may be returned by user defined functions, verify the type of the list.
        if not isinstance(item_list, (tuple, list)):
            raise ValueError(
                f"Spreadsheet processing function returned value of '{type(item_list)}' "
                f"type instead of 'list' or 'tuple'"
            )

        # Ensure, that 'item_list' is sent as a list
        item_list = list(item_list)

        # Set item type for all items that don't have item type already set (item list may contain
        #   instructions, but it is responsibility of the user to set item types correctly.
        #   By default an item is considered a plan.
        for item in item_list:
            if "item_type" not in item:
                item["item_type"] = "plan"

        logger.debug("The following plans were created: %s", pprint.pformat(item_list))
    except Exception as ex:
        msg = {"success": False, "msg": str(ex), "items": [], "results": []}
    else:
        try:
            params = {"user": displayed_name, "user_group": user_group}
            params["items"] = item_list
            msg = await SR.RM.item_add_batch(**params)
        except Exception:
            process_exception()
    return msg


@queue_router.post(
    "/queue/item/update",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Update an existing queue item",
    description=(
        "Replace or patch an existing queue item (identified by `item_uid`) with a new "
        "specification. Rejected if the item is not in the queue or is currently running. "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_update_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Update existing plan in the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        msg = await SR.RM.item_update(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/remove",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Remove an item from the queue",
    description=(
        "Remove a single item from the queue by position (`pos`) or UID (`uid`). "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_remove_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove plan from the queue
    """
    try:
        msg = await SR.RM.item_remove(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/remove/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Remove a batch of items from the queue",
    description=(
        "Remove multiple items from the queue in a single request. Parameter: `uids` (list "
        "of item UIDs). Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_remove_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove a batch of plans from the queue
    """
    try:
        if "uids" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_remove_batch", params=payload)
        else:
            msg = await SR.RM.item_remove_batch(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/move",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Move an item within the queue",
    description=(
        "Reposition a queue item. Source selected by `pos` or `uid`; destination by `pos_dest`, "
        "`before_uid`, or `after_uid`. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_move_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move plan in the queue
    """
    try:
        msg = await SR.RM.item_move(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/queue/item/move/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Move a batch of items within the queue",
    description=(
        "Reposition multiple items in the queue in a single request. Parameter: `uids` (list "
        "of item UIDs) plus a destination selector (`pos_dest`, `before_uid`, or `after_uid`). "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_move_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move a batch of plans in the queue
    """
    try:
        msg = await SR.RM.item_move_batch(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.get(
    "/queue/item/get",
    response_model=ItemGetResponse,
    response_model_exclude_unset=True,
    summary="Get a single queue item",
    description=(
        "Returns details for a single queue item by position (`pos`) or UID (`uid`). "
        "Required scope: `read:queue`."
    ),
    tags=["Queue Items"],
)
async def queue_item_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])
):
    """
    Get a plan from the queue
    """
    try:
        msg = await SR.RM.item_get(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.get(
    "/history/get",
    response_model=HistoryGetResponse,
    response_model_exclude_unset=True,
    summary="Get plan history",
    description=(
        "Returns the list of completed plans in chronological order, plus the `plan_history_uid` "
        "for change detection. Required scope: `read:history`."
    ),
    tags=["History"],
)
async def history_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:history"])
):
    """
    Returns the plan history (list of dicts).
    """
    try:
        msg = await SR.RM.history_get(**payload)
    except Exception:
        process_exception()
    return msg


@queue_router.post(
    "/history/clear",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Clear plan history",
    description=(
        "Remove all entries from the plan-history buffer. "
        "Required scope: `write:history:edit`."
    ),
    tags=["History"],
)
async def history_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:history:edit"])
):
    """
    Clear plan history.
    """
    try:
        msg = await SR.RM.history_clear(**payload)
    except Exception:
        process_exception()

    return msg
