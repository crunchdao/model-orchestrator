def __getattr__(name):
    if name in ("create_local_deploy_api", "LocalDeployServices"):
        from .local_deploy_api import create_local_deploy_api, LocalDeployServices
        return {"create_local_deploy_api": create_local_deploy_api, "LocalDeployServices": LocalDeployServices}[name]
    if name in ("create_phala_deploy_api", "PhalaDeployServices"):
        from .phala_deploy_api import create_phala_deploy_api, PhalaDeployServices
        return {"create_phala_deploy_api": create_phala_deploy_api, "PhalaDeployServices": PhalaDeployServices}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "create_local_deploy_api", "LocalDeployServices",
    "create_phala_deploy_api", "PhalaDeployServices",
]
