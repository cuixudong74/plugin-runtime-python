"""FastAPI router exposing the plugin runtime over HTTP.

Routes:

    GET    /plugins                       list installed plugins
    POST   /plugins                       install a manifest (JSON body or path)
    GET    /plugins/{plugin_id}           plugin detail
    DELETE /plugins/{plugin_id}           uninstall (must be disabled)
    POST   /plugins/{plugin_id}/enable    enable
    POST   /plugins/{plugin_id}/disable   disable (drains first)
    POST   /plugins/{plugin_id}/upgrade   upgrade to a new manifest version
    POST   /plugins/{plugin_id}/recover   recover from isolation
    GET    /plugins/{plugin_id}/isolation isolation status
    POST   /plugins/{plugin_id}/invoke/{capability}
                                          host invokes a capability
    GET    /audit                         audit log
    GET    /audit/rejections              audit log filtered to rejections
    POST   /resolve                        dry-run version resolution
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .errors import (
    DrainTimeoutError,
    IsolationError,
    LifecycleError,
    ManifestError,
    OperationCancelledError,
    PluginLoadError,
    PluginNotFoundError,
    PluginStateError,
    ResolutionError,
    UndeclaredCapabilityError,
)
from .manifest import parse_manifest
from .runtime import PluginRuntime

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class InstallRequest(BaseModel):
    manifest: Optional[Dict[str, Any]] = None
    path: Optional[str] = None


class UpgradeRequest(BaseModel):
    manifest: Optional[Dict[str, Any]] = None
    path: Optional[str] = None


class InvokeRequest(BaseModel):
    payload: Dict[str, Any] = {}


class ResolveRequest(BaseModel):
    targets: Dict[str, str]  # plugin id -> version range (empty string = any)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

def _error_status(exc: Exception) -> int:
    if isinstance(exc, ManifestError):
        return 400
    if isinstance(exc, ResolutionError):
        return 409
    if isinstance(exc, PluginNotFoundError):
        return 404
    if isinstance(exc, PluginStateError):
        return 409
    if isinstance(exc, IsolationError):
        return 409
    if isinstance(exc, PluginLoadError):
        return 500
    if isinstance(exc, DrainTimeoutError):
        return 503
    if isinstance(exc, OperationCancelledError):
        return 503
    if isinstance(exc, UndeclaredCapabilityError):
        return 403
    if isinstance(exc, LifecycleError):
        return 400
    return 500


def _raise(exc: Exception):
    raise HTTPException(status_code=_error_status(exc), detail=str(exc))


# ---------------------------------------------------------------------------
# Plugin CRUD
# ---------------------------------------------------------------------------

@router.get("/plugins")
async def list_plugins():
    runtime = get_runtime()
    return {"plugins": runtime.list_plugins()}


@router.post("/plugins", status_code=201)
async def install_plugin(req: InstallRequest):
    runtime = get_runtime()
    try:
        if req.path is not None:
            return await runtime.install_from_path(req.path)
        if req.manifest is None:
            raise HTTPException(
                status_code=400,
                detail="either 'manifest' or 'path' is required",
            )
        manifest = parse_manifest(req.manifest)
        return await runtime.install(manifest)
    except (ManifestError, PluginLoadError, LifecycleError) as exc:
        _raise(exc)


@router.get("/plugins/{plugin_id}")
async def get_plugin(plugin_id: str):
    runtime = get_runtime()
    try:
        return runtime.get_plugin(plugin_id)
    except PluginNotFoundError as exc:
        _raise(exc)


@router.delete("/plugins/{plugin_id}")
async def uninstall_plugin(plugin_id: str):
    runtime = get_runtime()
    try:
        return await runtime.uninstall(plugin_id)
    except (PluginNotFoundError, PluginStateError) as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@router.post("/plugins/{plugin_id}/enable")
async def enable_plugin(plugin_id: str):
    runtime = get_runtime()
    try:
        return await runtime.enable(plugin_id)
    except (PluginNotFoundError, ResolutionError, PluginLoadError,
            PluginStateError, IsolationError, DrainTimeoutError) as exc:
        _raise(exc)


@router.post("/plugins/{plugin_id}/disable")
async def disable_plugin(plugin_id: str):
    runtime = get_runtime()
    try:
        return await runtime.disable(plugin_id)
    except (PluginNotFoundError, ResolutionError, PluginStateError,
            DrainTimeoutError) as exc:
        _raise(exc)


@router.post("/plugins/{plugin_id}/upgrade")
async def upgrade_plugin(plugin_id: str, req: UpgradeRequest):
    runtime = get_runtime()
    try:
        if req.path is not None:
            return await runtime.upgrade_from_path(plugin_id, req.path)
        if req.manifest is None:
            raise HTTPException(
                status_code=400,
                detail="either 'manifest' or 'path' is required",
            )
        manifest = parse_manifest(req.manifest)
        return await runtime.upgrade(plugin_id, manifest)
    except (ManifestError, PluginNotFoundError, ResolutionError,
            PluginLoadError, PluginStateError, IsolationError,
            DrainTimeoutError, LifecycleError) as exc:
        _raise(exc)


@router.post("/plugins/{plugin_id}/recover")
async def recover_plugin(plugin_id: str):
    runtime = get_runtime()
    try:
        return await runtime.recover(plugin_id)
    except (PluginNotFoundError, ResolutionError, PluginLoadError,
            DrainTimeoutError) as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# Isolation / audit
# ---------------------------------------------------------------------------

@router.get("/plugins/{plugin_id}/isolation")
async def isolation_status(plugin_id: str):
    runtime = get_runtime()
    try:
        return runtime.isolation_status(plugin_id).to_dict()
    except PluginNotFoundError as exc:
        _raise(exc)


@router.get("/audit")
async def audit_log(caller: Optional[str] = None,
                    capability: Optional[str] = None,
                    kind: Optional[str] = None,
                    limit: int = 100):
    runtime = get_runtime()
    return {"entries": runtime.audit_entries(
        caller=caller, capability=capability, kind=kind, limit=limit
    )}


@router.get("/audit/rejections")
async def audit_rejections(caller: Optional[str] = None,
                           capability: Optional[str] = None,
                           limit: int = 100):
    runtime = get_runtime()
    return {"entries": runtime.audit_rejections(
        caller=caller, capability=capability, limit=limit
    )}


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

@router.post("/plugins/{plugin_id}/invoke/{capability}")
async def invoke_capability(plugin_id: str, capability: str, req: InvokeRequest):
    runtime = get_runtime()
    try:
        return await runtime.invoke(plugin_id, capability, req.payload)
    except (PluginNotFoundError, PluginStateError, IsolationError,
            UndeclaredCapabilityError, PluginLoadError) as exc:
        _raise(exc)
    except Exception as exc:
        # A plugin raised an unexpected error; surface it as a 500.
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Dry-run resolution
# ---------------------------------------------------------------------------

@router.post("/resolve")
async def resolve(req: ResolveRequest):
    runtime = get_runtime()
    try:
        from .versions import parse_range
        targets = {
            pid: (parse_range(rng) if rng else RangeSet())
            for pid, rng in req.targets.items()
        }
        resolution = resolve_fn(runtime._build_available(), targets)
        return {"selected": resolution.to_dict()}
    except ResolutionError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# Runtime accessor (set by main.py)
# ---------------------------------------------------------------------------

_runtime: Optional[PluginRuntime] = None


def set_runtime(runtime: PluginRuntime) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> PluginRuntime:
    if _runtime is None:
        raise RuntimeError("PluginRuntime not initialized; call set_runtime()")
    return _runtime


# Import here to avoid circular imports at module load.
from .resolver import resolve as resolve_fn  # noqa: E402
from .versions import RangeSet  # noqa: E402
