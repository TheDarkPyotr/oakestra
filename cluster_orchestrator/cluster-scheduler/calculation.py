import logging
import requests
import json
from mongodb_client import mongo_find_all_active_nodes


def calculate(app, job):
    print("calculating...")
    app.logger.info("calculating")

    # check here if job has any user preferences, e.g. on a specific node, cpu architecture,
    constraints = job.get("constraints")
    if constraints is not None:
        return constraint_based_scheduling(job, constraints)
    else:
        return greedy_load_balanced_algorithm(job=job)


def constraint_based_scheduling(job, constraints):
    mongo_find_all_active_nodes()
    for constraint in constraints:
        constraint_type = constraint.get("type")
        if constraint_type == "direct":
            return deploy_on_best_among_desired_nodes(job, constraint.get("node"))
    return greedy_load_balanced_algorithm(job=job)


def first_fit_algorithm(job):
    """Which of the worker nodes fits the Qos of the deployment file as the first"""
    active_nodes = mongo_find_all_active_nodes()

    print("active_nodes: ")
    for node in active_nodes:
        try:
            available_cpu = node.get("current_cpu_cores_free")
            available_memory = node.get("current_free_memory_in_MB")
            node_info = node.get("node_info")
            technology = node_info.get("technology")

            job_req = job.get("requirements")
            if (
                available_cpu >= job_req.get("cpu")
                and available_memory >= job_req.get("memory")
                and job.get("image_runtime") in technology
            ):
                return "positive", node
        except Exception:
            logging.error("Something wrong with job requirements or node infos")

    # no node found
    return "negative", None


def deploy_on_best_among_desired_nodes(job, nodes):
    active_nodes = mongo_find_all_active_nodes()
    selected_nodes = []
    if nodes is None or nodes == "":
        selected_nodes = active_nodes
    else:
        desired_nodes_list = nodes.split(";")
        for node in active_nodes:
            if node["node_info"]["host"] in desired_nodes_list:
                selected_nodes.append(node)
    return greedy_load_balanced_algorithm(job, active_nodes=selected_nodes)


def greedy_load_balanced_algorithm(job, active_nodes=None):
    """Which of the nodes within the cluster have the most capacity for a given job"""
    if active_nodes is None:
        active_nodes = mongo_find_all_active_nodes()
    qualified_nodes = []

    for node in active_nodes:
        if does_node_respects_requirements(extract_specs(node), job):
            qualified_nodes.append(node)

    target_node = None
    target_cpu = 0
    target_mem = 0

    # return if no qualified worker found
    if len(qualified_nodes) < 1:
        return "negative", None

    # return the cluster with the most cpu+ram
    for node in qualified_nodes:
        cpu = float(node.get("current_cpu_cores_free"))
        mem = float(node.get("current_free_memory_in_MB"))
        if cpu >= target_cpu and mem >= target_mem:
            target_cpu = cpu
            target_mem = mem
            target_node = node

    return "positive", target_node


def replicate(job):
    return 1


def extract_specs(node):
    return {
        "available_cpu": node.get("current_cpu_cores_free", 0)
        * (100 - node.get("current_memory_percent"))
        / 100,
        "available_memory": node.get("current_free_memory_in_MB", 0),
        "available_gpu": len(node.get("gpu_info", [])),
        "virtualization": node.get("node_info", {}).get("technology", []),
        "arch": node.get("node_info").get("architecture"),
    }


def get_registry_url(image_ref):
    # Supported registry URLs
    if "ghcr.io" in image_ref:
        return "https://ghcr.io"
    else:
        return "https://registry-1.docker.io"


def get_token(image_name):
    """Fetches an access token for the provided image name."""
    url = f"https://ghcr.io/token?scope=repository:{image_name}:pull"
    response = requests.get(url)
    response.raise_for_status()  # Raise exception for non-200 status codes
    data = json.loads(response.text)
    return data["token"]


def get_manifest(image_ref, registry_url):

    print("Registry url is {}".format(registry_url))

    headers = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json, "
                  "application/vnd.docker.distribution.manifest.list.v2+json"
    }
    try:
        # Split the image reference into repository and tag
        repo, tag = image_ref.split(":", 1)

        # Determine the manifest URL
        sha = image_ref.split("@")[1] if "@" in image_ref else tag
        url = f"{registry_url}/v2/{repo}/manifests/{sha if sha else tag}"

        # Retrieve authentication token for Docker Hub
        if registry_url == "https://registry-1.docker.io":
            response = requests.get(
                f"https://auth.docker.io/token?service=registry.docker.io"
                f"&scope=repository:{repo}:pull"
            )
            response.raise_for_status()  # Raise exception for non-2xx status codes
            token = response.json().get("token")
            headers["Authorization"] = f"Bearer {token}"

            # Fetch remote manifest with authorization (if applicable)
            response = requests.get(url, headers=headers)

        # If unauthorized (Docker Hub), try with Basic authentication (optional)
        elif registry_url == "https://ghcr.io":
            headers = {"Authorization": f"Bearer {get_token(repo)}"}
            accept_header = "application/vnd.oci.image.index.v1+json"
            if accept_header:
                headers["Accept"] = accept_header
                response = requests.get(url, headers=headers)
                response.raise_for_status()

        return response.json()

    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"Error retrieving remote manifest: {e}")
        return None


def get_image_size(image_ref):

    if image_ref.startswith("docker.io/"):
        image_ref = image_ref.replace("docker.io/", "")

    registry_url = get_registry_url(image_ref)
    manifest = get_manifest(image_ref, registry_url)
    if not manifest:
        return None
    else:
        print("Retrieved manifest: {}".format(manifest))

        if "layers" in manifest:
            total_bytes = sum(layer["size"] for layer in manifest["layers"])
            print("Total bytes: {}".format(total_bytes))
            return total_bytes
        elif "manifests" in manifest:
            total_bytes = sum(entry["size"] for entry in manifest["manifests"])
            print("Total bytes: {}".format(total_bytes))

            return total_bytes
        else:
            return 1  # Unable to extract image size from manifest, all-allow policy


def memory_estimation(available_memory, image, memory, storage=None):

    estimation_factor = 3
    if not image or image == "noImageDetected":
        print("No image defined in job description")
        return available_memory >= memory

    # Get and convert image size to GB with error handling
    image_size_in_bytes = get_image_size(image)

    if image_size_in_bytes is None:
        return available_memory >= memory  # Just evaluate the constraint without image size

    image_size_in_gb = round(image_size_in_bytes / (1024**3), 2) * estimation_factor

    total_memory_required = memory + image_size_in_gb
    if available_memory >= total_memory_required:
        print(f"Memory estimation for image '{image}' is {image_size_in_gb:.2f} GB")
        print(
            f"Available memory ({available_memory} GB) is sufficient "
            f"({total_memory_required:.2f} GB required)"
        )
        print(f"Storage not considered as constraints is {storage}")
        return True
    return False


def does_node_respects_requirements(node_specs, job):
    memory = 0
    if job.get("memory"):
        memory = job.get("memory")

    vcpu = 0
    if job.get("vcpu"):
        vcpu = job.get("vcpu")

    vgpu = 0
    if job.get("vgpu"):
        vgpu = job.get("vgpu")
    
    storage = 0
    if job.get("storage"):
        storage = job.get("storage")

    image = "noImageDetected"
    if job.get("image"):
        image = job.get("image")

    virtualization = job.get("virtualization")
    if virtualization == "unikernel":
        arch = job.get("arch")
        arch_fit = False
        if arch:
            for a in arch:
                if a == node_specs["arch"]:
                    arch_fit = True
                    break
        if not arch_fit:
            return False

    if (
        node_specs["available_cpu"] >= vcpu
        and memory_estimation(node_specs["available_memory"], image, memory, storage=storage)
        and virtualization in node_specs["virtualization"]
        and node_specs["available_gpu"] >= vgpu
    ):
        return True
    return False
