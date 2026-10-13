"""
core/docker_manager.py
Thin wrapper around the docker SDK for building/running/stopping
per-bot containers. Falls back gracefully if the Docker daemon
isn't reachable (caller should catch DockerUnavailable).
"""
# Made by @Dev_Null_X  (v5.2 hardened)

import os
import logging
import config

logger = logging.getLogger("docker_manager")

try:
    import docker
    from docker.errors import DockerException
except ImportError:  # docker package not installed
    docker = None
    DockerException = Exception


class DockerUnavailable(Exception):
    pass


def _client():
    if docker is None:
        raise DockerUnavailable("docker SDK not installed")
    try:
        return docker.from_env()
    except DockerException as e:
        raise DockerUnavailable(str(e))


DOCKERFILE_PYTHON = """FROM {base_image}
WORKDIR /app
COPY . /app
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
CMD ["python", "{entrypoint}"]
"""

DOCKERFILE_NODE = """FROM {base_image}
WORKDIR /app
COPY . /app
RUN if [ -f package.json ]; then npm install --omit=dev; fi
CMD ["node", "{entrypoint}"]
"""


def write_dockerfile(project_path: str, runtime: str, entrypoint: str):
    if runtime == "python":
        content = DOCKERFILE_PYTHON.format(base_image=config.DOCKER_IMAGE_PYTHON, entrypoint=entrypoint)
    elif runtime == "node":
        content = DOCKERFILE_NODE.format(base_image=config.DOCKER_IMAGE_NODE, entrypoint=entrypoint)
    else:
        raise ValueError(f"Unsupported runtime: {runtime}")

    dockerfile_path = os.path.join(project_path, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(content)
    return dockerfile_path


def ensure_network(client=None) -> str | None:
    """Dedicated bridge for hosted bots: enable_icc=false => bots can't talk to
    each other; fixed subnet => harden_host.sh can firewall it."""
    name = getattr(config, "CONTAINER_NETWORK", None)
    if not name:
        return None
    client = client or _client()
    try:
        client.networks.get(name)
        return name
    except Exception:
        pass
    try:
        ipam_pool = docker.types.IPAMPool(subnet=getattr(config, "CONTAINER_SUBNET", "172.29.0.0/16"))
        client.networks.create(
            name, driver="bridge",
            ipam=docker.types.IPAMConfig(pool_configs=[ipam_pool]),
            options={"com.docker.network.bridge.enable_icc": "false"},
        )
        return name
    except Exception as e:
        logger.warning("could not create network %s (%s) — using default bridge", name, e)
        return None


def _mem_bytes(value) -> int:
    if isinstance(value, int):
        return value
    v = str(value).strip().lower()
    mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    if v and v[-1] in mult:
        return int(float(v[:-1]) * mult[v[-1]])
    return int(v)


def build_image(project_path: str, image_tag: str):
    client = _client()
    mem = _mem_bytes(getattr(config, "BUILD_MEMORY_LIMIT", "1g"))
    image, logs = client.images.build(
        path=project_path, tag=image_tag, rm=True, forcerm=True,
        # Untrusted requirements.txt / package.json run during the build:
        # cap what a malicious build step can eat.
        container_limits={"memory": mem, "memswap": mem, "cpushares": 256},
        timeout=int(getattr(config, "DEPENDENCY_TIMEOUT", 600)),
    )
    return image, [l.get("stream", "") for l in logs if "stream" in l]


def run_container(image_tag: str, container_name: str, env_vars: dict | None = None, cpu_limit: float | None = None):
    client = _client()
    # Remove any stale container with the same name first
    try:
        old = client.containers.get(container_name)
        old.remove(force=True)
    except Exception:
        pass

    effective_cpu = cpu_limit if cpu_limit is not None else config.CONTAINER_CPU_LIMIT
    network = ensure_network(client)
    container = client.containers.run(
        image_tag,
        name=container_name,
        detach=True,
        environment=env_vars or {},
        mem_limit=config.CONTAINER_MEMORY_LIMIT,
        nano_cpus=int(effective_cpu * 1_000_000_000),
        # Defense in depth: never mount host paths or the Docker socket.
        # Drop Linux capabilities, prevent privilege escalation, and cap process
        # count. Network stays enabled because hosted bots commonly need APIs.
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        pids_limit=int(getattr(config, "CONTAINER_PIDS_LIMIT", 128)),
        init=True,
        read_only=True,
        tmpfs={"/tmp": "rw,nosuid,size=64m"},
        restart_policy={"Name": "no"},
        # ── v5.2 extra hardening ──
        privileged=False,
        user=getattr(config, "CONTAINER_USER", "65534:65534"),   # never root
        memswap_limit=config.CONTAINER_MEMORY_LIMIT,             # no swap abuse
        ipc_mode="private",
        network=network,                                         # isolated bridge
        runtime=getattr(config, "DOCKER_RUNTIME", None),         # e.g. gVisor "runsc"
        ulimits=[docker.types.Ulimit(name="nofile", soft=int(getattr(config, "CONTAINER_NOFILE_LIMIT", 1024)),
                                     hard=int(getattr(config, "CONTAINER_NOFILE_LIMIT", 1024))),
                 docker.types.Ulimit(name="core", soft=0, hard=0)],
        log_config=docker.types.LogConfig(type="json-file", config={"max-size": "1m", "max-file": "2"}),
        labels={"hosting.managed": "1"},
    )
    return container.id


def stop_container(container_id: str):
    try:
        client = _client()
        c = client.containers.get(container_id)
        c.stop(timeout=10)
    except Exception:
        # Daemon unreachable / container already gone — nothing more we can
        # do here. Caller (deploy.stop_bot) still marks the DB row stopped.
        pass


def remove_container(container_id: str):
    try:
        client = _client()
        c = client.containers.get(container_id)
        c.remove(force=True)
    except Exception:
        pass


def restart_container(container_id: str):
    client = _client()  # let this raise — caller needs to know restart failed
    c = client.containers.get(container_id)
    c.restart(timeout=10)


def get_logs(container_id: str, tail: int = 100) -> str:
    client = _client()
    c = client.containers.get(container_id)
    return c.logs(tail=tail).decode(errors="replace")


def get_stats(container_id: str) -> dict:
    client = _client()
    c = client.containers.get(container_id)
    c.reload()
    stats = c.stats(stream=False)

    cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
    system_delta = stats["cpu_stats"].get("system_cpu_usage", 0) - stats["precpu_stats"].get("system_cpu_usage", 0)
    cpu_percent = 0.0
    if system_delta > 0 and cpu_delta > 0:
        online_cpus = stats["cpu_stats"].get("online_cpus", 1)
        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0

    mem_usage = stats["memory_stats"].get("usage", 0)
    mem_limit = stats["memory_stats"].get("limit", 1)

    return {
        "status": c.status,
        "cpu_percent": round(cpu_percent, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
    }


def remove_image(image_tag: str | None):
    """Removes a built image by tag. Safe to call with None or on an
    image that's already gone (e.g. shared/dangling) — never raises,
    since this always runs as best-effort cleanup after the thing that
    actually mattered (starting the new container / deleting the bot)
    already succeeded."""
    if not image_tag:
        return
    try:
        client = _client()
        client.images.remove(image_tag, force=True)
    except Exception:
        pass


def prune_dangling_images() -> int:
    """Removes dangling (untagged, unused) images left behind by builds
    that got interrupted or superseded. Returns MB reclaimed, or 0 if
    Docker isn't available."""
    try:
        client = _client()
        result = client.images.prune(filters={"dangling": True})
        return round((result.get("SpaceReclaimed", 0) or 0) / 1024 / 1024, 1)
    except Exception:
        return 0


def is_docker_available() -> bool:
    try:
        _client().ping()
        return True
    except Exception:
        return False
