"""Idempotent region bootstrap, run inside the operator toolbox as the cloud admin.

Creates the service catalog, service users, a flavor and an image, then the
tenant projects and users the tenant workload signs in as.
"""

import json

import openstack


def ensure(finder, creator, name, **attrs):
    found = finder(name, ignore_missing=True)
    return found if found is not None else creator(name=name, **attrs)


SERVICE_TYPES = {
    "nova": "compute",
    "placement": "placement",
    "glance": "image",
    "neutron": "network",
    "swift": "object-store",
}


def catalog(cloud, endpoints, passwords):
    """Register a service user (admin in the service project) and endpoints for each service."""
    identity = cloud.identity
    service_project = ensure(identity.find_project, identity.create_project, "service", domain_id="default")
    admin_role = identity.find_role("admin")
    for name, url in endpoints.items():
        account = identity.find_user(name, domain_id="default")
        if account is None:
            account = identity.create_user(name=name, password=passwords[name], domain_id="default")
        identity.assign_project_role_to_user(service_project, account, admin_role)
        kind = SERVICE_TYPES[name]
        service = next((s for s in identity.services() if s.type == kind), None)
        if service is None:
            service = identity.create_service(name=name, type=kind)
        existing = {e.interface for e in identity.endpoints(service_id=service.id)}
        for interface in ("public", "internal", "admin"):
            if interface not in existing:
                identity.create_endpoint(service_id=service.id, interface=interface, url=url, region_id="RegionOne")


def resources(cloud, image_bytes):
    flavor = cloud.compute.find_flavor("m1.small")
    if flavor is None:
        flavor = cloud.compute.create_flavor(name="m1.small", ram=512, vcpus=1, disk=1, is_public=True)
    image = cloud.image.find_image("cirros")
    if image is None or image.status != "active":
        if image is not None:
            cloud.image.delete_image(image)
        image = cloud.image.create_image(
            name="cirros", data=image_bytes, disk_format="raw", container_format="bare", visibility="public"
        )
    return {"flavor": flavor.id, "image": image.id}


def tenants(cloud, accounts):
    identity = cloud.identity
    member = identity.find_role("member")
    for account in accounts:
        project = ensure(identity.find_project, identity.create_project, account["project"], domain_id="default")
        user = identity.find_user(account["user"], domain_id="default")
        if user is None:
            user = identity.create_user(
                name=account["user"], password=account["password"], domain_id="default", default_project_id=project.id
            )
        identity.assign_project_role_to_user(project, user, member)
        cloud.set_compute_quotas(project.id, instances=200, cores=400, ram=400 * 1024)


def main(endpoints, passwords, accounts):
    cloud = openstack.connect(cloud="admin")
    catalog(cloud, endpoints, passwords)
    return {"accounts": len(accounts)}


def provision(accounts):
    cloud = openstack.connect(cloud="admin")
    ids = resources(cloud, b"\0" * 1024 * 1024)
    tenants(cloud, accounts)
    return ids


def run(mode, **arguments):
    result = {"catalog": main, "provision": provision}[mode](**arguments)
    print(json.dumps(result))
