import base64
import copy

from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/mutate")
async def mutate_pod(request: Request):
    admission_review = await request.json()

    req = admission_review.get("request", {})
    uid = req.get("uid")
    object_meta = req.get("object", {})

    original_containers = object_meta.get("spec", {}).get("containers", [])
    mutated_containers = copy.deepcopy(original_containers)

    proxy_envs = [
        {"name": "HTTP_PROXY", "value": "http://10.254.254.254:8080"},
        {"name": "HTTPS_PROXY", "value": "http://10.254.254.254:8080"},
    ]

    for container in mutated_containers:
        if "env" not in container:
            container["env"] = []

        existing_envs = {env["name"] for env in container["env"]}
        for proxy in proxy_envs:
            if proxy["name"] not in existing_envs:
                container["env"].append(proxy)

    patch = [{"op": "replace", "path": "/spec/containers", "value": mutated_containers}]

    patch_str = str(patch).replace("'", '"')
    patch_b64 = base64.b64encode(patch_str.encode()).decode()

    response_body = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True, "patchType": "JSONPatch", "patch": patch_b64},
    }

    return response_body
